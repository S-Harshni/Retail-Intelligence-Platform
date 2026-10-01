"""The SQL layer: cleaning rules on hand-made rows, then invariants on the real data."""
import duckdb
import pandas as pd
import pytest

from retail import analytics, warehouse


def tiny_warehouse(tmp_path, rows: list[tuple]) -> duckdb.DuckDBPyConnection:
    columns = ["sheet", "row_no", "invoice", "stock_code", "description", "quantity", "invoice_ts", "price",
               "customer_id", "country"]
    frame = pd.DataFrame(rows, columns=columns)  # noqa: F841 - read by DuckDB
    frame["invoice_ts"] = pd.to_datetime(frame["invoice_ts"])
    frame["customer_id"] = frame["customer_id"].astype("Int64")
    source = tmp_path / "raw.parquet"
    duckdb.sql(f"COPY (SELECT * FROM frame) TO '{source}' (FORMAT parquet)")
    return warehouse.build(tmp_path / "tiny.duckdb", source)


def test_line_classification(tmp_path):
    conn = tiny_warehouse(tmp_path, [
        (1, 1, "1001", "85123a", "HEART HOLDER", 6, "2010-01-04 10:00", 2.5, 17850, "United Kingdom"),    # sale
        (1, 2, "C1002", "85123A", "HEART HOLDER", -2, "2010-01-06 10:00", 2.5, 17850, "United Kingdom"),  # return
        (1, 3, "1003", "POST", "POSTAGE", 1, "2010-01-06 11:00", 18.0, 12583, "France"),                  # non-product
        (1, 4, "1004", "22423", "damaged", -40, "2010-01-07 09:00", 0.0, None, "United Kingdom"),        # write-off
        (1, 5, "1005", "22423", "CAKESTAND", 3, "2010-01-07 12:00", 0.0, None, "United Kingdom"),        # zero price
        (1, 6, "1006", "84997B", "CUTLERY SET", 4, "2010-01-08 12:00", 3.75, None, "United Kingdom"),    # sale, no id
    ])
    types = dict(conn.execute("SELECT invoice, line_type FROM stg_lines").fetchall())
    assert types == {"1001": "sale", "C1002": "return", "1003": "non_product", "1004": "adjustment",
                     "1005": "adjustment", "1006": "sale"}
    assert conn.execute("SELECT stock_code FROM stg_lines WHERE invoice = '1001'").fetchone()[0] == "85123A"
    assert conn.execute("SELECT count(*) FROM fact_sales").fetchone()[0] == 3
    assert conn.execute("SELECT description FROM dim_product WHERE stock_code = '85123A'").fetchone()[0] == "HEART HOLDER"


def test_sheet_overlap_is_removed_once(tmp_path):
    conn = tiny_warehouse(tmp_path, [
        (1, 1, "1001", "22423", "CAKESTAND", 1, "2010-11-30 10:00", 10.0, 1, "United Kingdom"),
        (1, 2, "1002", "22423", "CAKESTAND", 2, "2010-12-01 10:00", 10.0, 1, "United Kingdom"),  # also in sheet 2
        (2, 1, "1002", "22423", "CAKESTAND", 2, "2010-12-01 10:00", 10.0, 1, "United Kingdom"),
        (2, 2, "1003", "22423", "CAKESTAND", 5, "2010-12-10 10:00", 10.0, 1, "United Kingdom"),
    ])
    assert conn.execute("SELECT sum(quantity), count(*) FROM stg_lines").fetchone() == (8, 3)


def test_reversed_orders_are_matched_one_to_one(tmp_path):
    conn = tiny_warehouse(tmp_path, [
        (1, 1, "1001", "22423", "CAKESTAND", 10, "2010-01-04 10:00", 10.0, 7, "United Kingdom"),   # reversed by C1003
        (1, 2, "1002", "22423", "CAKESTAND", 10, "2010-01-05 10:00", 10.0, 7, "United Kingdom"),   # kept: one return only
        (1, 3, "C1003", "22423", "CAKESTAND", -10, "2010-01-06 10:00", 10.0, 7, "United Kingdom"),
        (1, 4, "1004", "22423", "CAKESTAND", 10, "2010-01-07 10:00", 10.0, 8, "United Kingdom"),   # other customer
        (1, 5, "C1005", "84997B", "CUTLERY SET", -3, "2010-01-08 10:00", 4.0, 9, "United Kingdom"),  # return first
        (1, 6, "1006", "84997B", "CUTLERY SET", 3, "2010-01-09 10:00", 4.0, 9, "United Kingdom"),    # not reversed
        (1, 7, "1007", "84997B", "CUTLERY SET", 5, "2010-01-11 10:00", 4.0, 9, "United Kingdom"),    # partial return
        (1, 8, "C1008", "84997B", "CUTLERY SET", -2, "2010-01-12 10:00", 4.0, 9, "United Kingdom"),
    ])
    reversed_invoices = [r[0] for r in conn.execute("SELECT invoice FROM fact_sales WHERE is_reversed").fetchall()]
    assert reversed_invoices == ["1001"]
    weekly = conn.execute("SELECT sum(units) FROM mart_product_weekly WHERE stock_code = '22423'").fetchone()[0]
    assert weekly == 20  # the reversed 10 units are not demand


def test_real_data_profile(conn):
    profile = warehouse.profile(conn)
    assert profile["raw_rows"] == 1_067_371
    assert profile["overlap_rows_removed"] == 22_523
    assert sum(profile["line_types"].values()) == profile["staged_rows"] == 1_044_848
    assert profile["fact_rows"] == profile["line_types"]["sale"] + profile["line_types"]["return"]
    assert all(check["failures"] == 0 for check in profile["checks"]) and len(profile["checks"]) == 12


def test_rfm_scores_and_segments(conn):
    rows = conn.execute("SELECT min(r), max(r), min(f), max(f), min(m), max(m), count(*), count(DISTINCT customer_id) "
                        "FROM mart_customer_rfm").fetchone()
    assert rows[:6] == (1, 5, 1, 5, 1, 5) and rows[6] == rows[7]
    assert conn.execute("SELECT count(*) FROM mart_customer_rfm WHERE segment = 'Champions' AND (r < 4 OR f + m < 8)").fetchone()[0] == 0
    # Quintile 5 really is the most recent fifth of customers.
    recent, old = conn.execute("SELECT max(recency_days) FILTER (WHERE r = 5), min(recency_days) FILTER (WHERE r = 1) "
                               "FROM mart_customer_rfm").fetchone()
    assert recent < old


def test_cohorts_and_customer_summary(conn):
    result = analytics.customers(conn)
    assert all(c["retention"][0] == 1.0 for c in result["cohorts"])
    assert sum(c["size"] for c in result["cohorts"]) == result["summary"]["customers"]
    assert sum(s["customers"] for s in result["segments"]) == result["summary"]["customers"]
    assert sum(s["revenue_share"] for s in result["segments"]) == pytest.approx(1.0, abs=1e-3)


def test_sales_and_returns_reconcile(conn):
    overview, returns = analytics.overview(conn), analytics.returns(conn)
    totals = overview["totals"]
    assert totals["net_sales"] == pytest.approx(totals["gross_sales"] - totals["returned_value"], abs=0.01)
    assert sum(m["revenue"] for m in overview["monthly"]) == pytest.approx(totals["gross_sales"], abs=1)
    assert returns["summary"]["value_rate"] == pytest.approx(totals["returned_value"] / totals["gross_sales"], abs=1e-4)
    assert returns["summary"]["reversed_lines"] == sum(b["lines"] for b in returns["days_to_return"])
