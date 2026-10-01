"""Descriptive analytics read straight from the marts: sales, returns and customers."""
import duckdb
import pandas as pd

SEGMENT_ORDER = ["Champions", "Loyal", "Promising", "New", "At risk", "Lost"]
MAX_COHORT_OFFSET = 12


MONEY = {"revenue", "returned_value", "gross_sales", "net_sales"}


def _records(conn: duckdb.DuckDBPyConnection, sql: str, digits: int = 2) -> list[dict]:
    """Rows as dicts. Money is rounded to pence (float sums vary in the last bits between runs), rates to `digits`."""
    frame = conn.execute(sql).df()
    for col in frame.select_dtypes("float").columns:
        frame[col] = frame[col].round(2 if col in MONEY else digits)
    for col in frame.select_dtypes(["datetime", "datetimetz"]).columns:
        frame[col] = frame[col].dt.strftime("%Y-%m-%d")
    return frame.astype(object).where(frame.notna(), None).to_dict("records")


def overview(conn: duckdb.DuckDBPyConnection) -> dict:
    totals = _records(conn, """
        SELECT
            sum(amount) FILTER (WHERE line_type = 'sale')                         AS gross_sales,
            -sum(amount) FILTER (WHERE line_type = 'return')                      AS returned_value,
            sum(amount)                                                           AS net_sales,
            count(DISTINCT invoice) FILTER (WHERE line_type = 'sale')             AS orders,
            count(DISTINCT customer_id)                                           AS customers,
            count(DISTINCT stock_code) FILTER (WHERE line_type = 'sale')          AS products,
            sum(quantity) FILTER (WHERE line_type = 'sale')                       AS units,
            count(DISTINCT country)                                               AS countries,
            sum(amount) FILTER (WHERE line_type = 'sale' AND country <> 'United Kingdom')
                / sum(amount) FILTER (WHERE line_type = 'sale')                   AS export_share
        FROM fact_sales""", 6)[0]
    totals["average_order_value"] = round(totals["gross_sales"] / totals["orders"], 2)
    return {
        "totals": totals,
        "monthly": _records(conn, "SELECT month, revenue, orders, customers, returned_value FROM mart_monthly ORDER BY month"),
        "countries": _records(conn, """
            SELECT country, revenue, orders, customers,
                   revenue / (SELECT sum(revenue) FROM mart_country) AS share
            FROM mart_country ORDER BY revenue DESC LIMIT 10""", 6),
        "top_products": _records(conn, """
            SELECT stock_code, description, revenue, units_sold, orders
            FROM mart_product WHERE units_reversed < 0.5 * units_sold
            ORDER BY revenue DESC LIMIT 10"""),
        "weekdays": _records(conn, """
            SELECT d.weekday, sum(f.amount) AS revenue, count(DISTINCT f.invoice) AS orders
            FROM fact_sales f JOIN dim_date d ON d.date = f.invoice_date
            WHERE f.line_type = 'sale' GROUP BY d.weekday ORDER BY d.weekday"""),
    }


def returns(conn: duckdb.DuckDBPyConnection) -> dict:
    summary = _records(conn, """
        SELECT
            -sum(amount) FILTER (WHERE line_type = 'return')
                / sum(amount) FILTER (WHERE line_type = 'sale')                    AS value_rate,
            -sum(quantity) FILTER (WHERE line_type = 'return')
                / sum(quantity) FILTER (WHERE line_type = 'sale')                  AS unit_rate,
            count(*) FILTER (WHERE line_type = 'return')                           AS return_lines,
            count(DISTINCT invoice) FILTER (WHERE line_type = 'return')            AS return_orders,
            count(*) FILTER (WHERE line_type = 'return') / count(*) FILTER (WHERE line_type = 'sale')
                                                                                   AS line_rate,
            -- A sale line the same customer later returned in full (see sql/03_facts.sql).
            count(*) FILTER (WHERE is_reversed)                                    AS reversed_lines,
            count(*) FILTER (WHERE is_reversed) / count(*) FILTER (WHERE line_type = 'return')
                                                                                   AS full_reversal_share
        FROM fact_sales""", 6)[0]
    return {
        "summary": summary,
        "monthly": _records(conn, """
            SELECT month, returned_value, returned_value / revenue AS value_rate FROM mart_monthly ORDER BY month""", 6),
        # Rate by lines, not units: two cancelled orders of 81,000 and 74,000 units would swamp a unit rate.
        "products": _records(conn, """
            SELECT stock_code, description, sale_lines, return_lines,
                   return_lines / sale_lines AS line_rate, returned_value
            FROM mart_product
            WHERE sale_lines >= 300
            ORDER BY line_rate DESC LIMIT 12""", 6),
        "countries": _records(conn, """
            SELECT country, orders, revenue, returned_value, returned_value / revenue AS value_rate
            FROM mart_country WHERE orders >= 80 ORDER BY value_rate DESC""", 6),
        "days_to_return": _records(conn, """
            WITH pairs AS (
                SELECT date_diff('day', s.invoice_date, min(r.invoice_date)) AS days
                FROM fact_sales s
                JOIN fact_sales r ON r.line_type = 'return' AND r.customer_id = s.customer_id
                    AND r.stock_code = s.stock_code AND r.quantity = -s.quantity AND r.invoice_ts >= s.invoice_ts
                WHERE s.is_reversed
                GROUP BY s.line_id, s.invoice_date
            )
            SELECT CASE WHEN days = 0 THEN 'Same day' WHEN days <= 7 THEN '1-7 days' WHEN days <= 30 THEN '8-30 days'
                        WHEN days <= 90 THEN '31-90 days' ELSE 'Over 90 days' END AS bucket,
                   min(days) AS first_day, count(*) AS lines
            FROM pairs GROUP BY bucket ORDER BY first_day"""),
    }


def customers(conn: duckdb.DuckDBPyConnection) -> dict:
    segments = conn.execute("""
        SELECT segment, count(*) AS customers, sum(monetary) AS revenue, avg(recency_days) AS recency_days,
               avg(frequency) AS orders, avg(monetary) AS value
        FROM mart_customer_rfm GROUP BY segment""").df()
    segments["customer_share"] = segments["customers"] / segments["customers"].sum()
    segments["revenue_share"] = segments["revenue"] / segments["revenue"].sum()
    segments = segments.set_index("segment").loc[SEGMENT_ORDER].reset_index().round(6)

    cohort = conn.execute("SELECT * FROM mart_cohort").df()
    size = cohort[cohort["month_offset"] == 0].set_index("cohort")["customers"]
    cohort["retention"] = cohort["customers"] / cohort["cohort"].map(size)
    matrix = cohort[cohort["month_offset"] <= MAX_COHORT_OFFSET].pivot(index="cohort", columns="month_offset", values="retention")
    cohorts = [
        {"cohort": pd.Timestamp(c).strftime("%Y-%m"), "size": int(size[c]),
         "retention": [None if pd.isna(v) else round(float(v), 6) for v in row]}
        for c, row in zip(matrix.index, matrix.to_numpy(), strict=True)
    ]
    # The first month holds every customer already active when the data starts, so it is not a true
    # acquisition cohort; the average below skips it.
    later = matrix.iloc[1:]
    summary = conn.execute("""
        WITH c AS (SELECT monetary, frequency, percent_rank() OVER (ORDER BY monetary DESC) AS pr FROM mart_customer_rfm)
        SELECT count(*) AS customers,
               avg(CAST(frequency >= 2 AS INTEGER)) AS repeat_rate,
               sum(monetary) FILTER (WHERE pr < 0.2) / sum(monetary) AS top20_revenue_share,
               median(monetary) AS median_value, median(frequency) AS median_orders
        FROM c""").df().round(6).to_dict("records")[0]
    summary["month1_retention"] = round(float(later[1].mean()), 6)
    summary["month3_retention"] = round(float(later[3].mean()), 6)
    return {"summary": summary, "segments": segments.to_dict("records"), "cohorts": cohorts}
