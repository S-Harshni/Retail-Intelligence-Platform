"""Build the DuckDB warehouse from the Parquet file by running the SQL files in order.

    python -m retail.warehouse
"""
from pathlib import Path

import duckdb

from retail.config import PARQUET, SQL, WAREHOUSE

STEPS = ["01_staging.sql", "02_dimensions.sql", "03_facts.sql", "04_marts.sql"]


def build(path: Path | str = WAREHOUSE, source: Path | str = PARQUET) -> duckdb.DuckDBPyConnection:
    """Create every table from scratch and return an open connection. Raises if a quality check fails."""
    conn = duckdb.connect(str(path))
    conn.execute(f"CREATE OR REPLACE TABLE raw_lines AS SELECT * FROM read_parquet('{source}')")
    for step in STEPS:
        conn.execute((SQL / step).read_text())
    failed = [(name, n) for name, n in quality_checks(conn) if n]
    if failed:
        raise ValueError(f"data quality checks failed: {failed}")
    return conn


def quality_checks(conn: duckdb.DuckDBPyConnection) -> list[tuple[str, int]]:
    return [(name, int(n)) for name, n in conn.execute((SQL / "quality_checks.sql").read_text()).fetchall()]


def profile(conn: duckdb.DuckDBPyConnection) -> dict:
    """What the cleaning did, in numbers."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    types = dict(conn.execute("SELECT line_type, count(*) FROM stg_lines GROUP BY 1").fetchall())
    sales = one("SELECT sum(amount) FROM fact_sales WHERE line_type = 'sale'")
    return {
        "raw_rows": one("SELECT count(*) FROM raw_lines"),
        "overlap_rows_removed": one("SELECT count(*) FROM raw_lines") - one("SELECT count(*) FROM stg_lines"),
        "staged_rows": one("SELECT count(*) FROM stg_lines"),
        "line_types": {k: int(v) for k, v in sorted(types.items())},
        "fact_rows": one("SELECT count(*) FROM fact_sales"),
        "lines_without_customer": one("SELECT count(*) FROM fact_sales WHERE customer_id IS NULL"),
        "revenue_share_without_customer": round(
            one("SELECT sum(amount) FROM fact_sales WHERE line_type = 'sale' AND customer_id IS NULL") / sales, 6),
        "repeated_identical_lines_kept": one(
            "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM fact_sales GROUP BY invoice, stock_code,"
            " quantity, unit_price, invoice_ts, customer_id HAVING count(*) > 1)"),
        "reversed_sale_lines": one("SELECT count(*) FROM fact_sales WHERE is_reversed"),
        "reversed_units": one("SELECT coalesce(sum(quantity), 0) FROM fact_sales WHERE is_reversed"),
        "first_date": str(one("SELECT min(invoice_date) FROM fact_sales")),
        "last_date": str(one("SELECT max(invoice_date) FROM fact_sales")),
        "tables": {t: one(f"SELECT count(*) FROM {t}") for t in (
            "dim_product", "dim_customer", "dim_date", "fact_sales", "mart_monthly", "mart_product",
            "mart_customer_rfm", "mart_cohort", "mart_product_weekly", "mart_basket")},
        "checks": [{"name": n, "failures": f} for n, f in quality_checks(conn)],
    }


if __name__ == "__main__":
    import json

    print(json.dumps(profile(build()), indent=1, default=str))
