-- Marts: the aggregates the analytics, models, API and dashboard read.

CREATE OR REPLACE TABLE mart_monthly AS
SELECT
    d.month_start                                                          AS month,
    sum(f.amount) FILTER (WHERE f.line_type = 'sale')                      AS revenue,
    count(DISTINCT f.invoice) FILTER (WHERE f.line_type = 'sale')          AS orders,
    count(DISTINCT f.customer_id) FILTER (WHERE f.line_type = 'sale')      AS customers,
    sum(f.quantity) FILTER (WHERE f.line_type = 'sale')                    AS units,
    coalesce(-sum(f.amount) FILTER (WHERE f.line_type = 'return'), 0)      AS returned_value
FROM fact_sales f
JOIN dim_date d ON d.date = f.invoice_date
GROUP BY d.month_start;

CREATE OR REPLACE TABLE mart_country AS
SELECT
    country,
    sum(amount) FILTER (WHERE line_type = 'sale')                     AS revenue,
    count(DISTINCT invoice) FILTER (WHERE line_type = 'sale')         AS orders,
    count(DISTINCT customer_id)                                       AS customers,
    coalesce(-sum(amount) FILTER (WHERE line_type = 'return'), 0)     AS returned_value
FROM fact_sales
GROUP BY country;

CREATE OR REPLACE TABLE mart_product AS
SELECT
    f.stock_code,
    p.description,
    sum(f.amount) FILTER (WHERE f.line_type = 'sale')                     AS revenue,
    sum(f.quantity) FILTER (WHERE f.line_type = 'sale')                   AS units_sold,
    count(DISTINCT f.invoice) FILTER (WHERE f.line_type = 'sale')         AS orders,
    coalesce(-sum(f.quantity) FILTER (WHERE f.line_type = 'return'), 0)   AS units_returned,
    coalesce(-sum(f.amount) FILTER (WHERE f.line_type = 'return'), 0)     AS returned_value,
    median(f.unit_price) FILTER (WHERE f.line_type = 'sale')              AS median_price,
    coalesce(sum(f.quantity) FILTER (WHERE f.is_reversed), 0)             AS units_reversed,
    count(*) FILTER (WHERE f.line_type = 'sale')                          AS sale_lines,
    count(*) FILTER (WHERE f.line_type = 'return')                        AS return_lines
FROM fact_sales f
JOIN dim_product p USING (stock_code)
GROUP BY f.stock_code, p.description;

-- Recency, frequency, monetary value per identified customer, scored 1-5 by quintile.
CREATE OR REPLACE TABLE mart_customer_rfm AS
WITH base AS (
    SELECT
        customer_id,
        date_diff('day', max(invoice_date) FILTER (WHERE line_type = 'sale'),
                  (SELECT max(invoice_date) + 1 FROM fact_sales))        AS recency_days,
        count(DISTINCT invoice) FILTER (WHERE line_type = 'sale')        AS frequency,
        round(sum(amount), 2)                                            AS monetary       -- net of returns, to the penny
    FROM fact_sales
    WHERE customer_id IS NOT NULL
    GROUP BY customer_id
    HAVING count(*) FILTER (WHERE line_type = 'sale') > 0
),
scored AS (
    SELECT
        *,
        6 - ntile(5) OVER (ORDER BY recency_days, customer_id) AS r,
        ntile(5) OVER (ORDER BY frequency, customer_id)        AS f,
        ntile(5) OVER (ORDER BY monetary, customer_id)         AS m
    FROM base
)
SELECT
    *,
    CASE
        WHEN r >= 4 AND f + m >= 8 THEN 'Champions'
        WHEN r = 3 AND f + m >= 8  THEN 'Loyal'
        WHEN r >= 4 AND f + m <= 3 THEN 'New'
        WHEN r >= 3                THEN 'Promising'
        WHEN f + m >= 6            THEN 'At risk'
        ELSE 'Lost'
    END AS segment
FROM scored;

-- Monthly acquisition cohorts: how many of each month's new customers buy again in later months.
CREATE OR REPLACE TABLE mart_cohort AS
WITH activity AS (
    SELECT DISTINCT customer_id, CAST(date_trunc('month', invoice_date) AS DATE) AS month
    FROM fact_sales
    WHERE customer_id IS NOT NULL AND line_type = 'sale'
),
first_month AS (
    SELECT customer_id, min(month) AS cohort FROM activity GROUP BY customer_id
)
SELECT
    c.cohort,
    date_diff('month', c.cohort, a.month)  AS month_offset,
    count(*)                               AS customers
FROM activity a
JOIN first_month c USING (customer_id)
GROUP BY c.cohort, month_offset;

-- Weekly units per product, without fully reversed orders: the demand history the forecaster learns from.
CREATE OR REPLACE TABLE mart_product_weekly AS
SELECT
    d.week_start,
    f.stock_code,
    sum(f.quantity)           AS units,
    sum(f.amount)             AS revenue,
    count(DISTINCT f.invoice) AS orders
FROM fact_sales f
JOIN dim_date d ON d.date = f.invoice_date
WHERE f.line_type = 'sale' AND NOT f.is_reversed
GROUP BY d.week_start, f.stock_code;

-- One row per product per order: the baskets the recommender learns from.
CREATE OR REPLACE TABLE mart_basket AS
SELECT DISTINCT invoice, invoice_date, customer_id, stock_code
FROM fact_sales
WHERE line_type = 'sale';
