-- Dimensions.
--
-- The source spells some products and countries more than one way. Each product takes its most
-- frequent description on sale lines and each customer their most frequent country; ties go to the
-- alphabetically first value, so every build gives the same answer.
CREATE OR REPLACE TABLE dim_product AS
WITH names AS (
    SELECT
        stock_code,
        description,
        row_number() OVER (
            PARTITION BY stock_code
            ORDER BY count(*) FILTER (WHERE line_type = 'sale') DESC, count(*) DESC, description
        ) AS pick
    FROM stg_lines
    WHERE line_type IN ('sale', 'return') AND description IS NOT NULL
    GROUP BY stock_code, description
)
SELECT
    l.stock_code,
    n.description,
    count(DISTINCT l.description) FILTER (WHERE l.line_type = 'sale') AS description_variants,
    min(l.invoice_date) FILTER (WHERE l.line_type = 'sale')           AS first_sold,
    max(l.invoice_date) FILTER (WHERE l.line_type = 'sale')           AS last_sold
FROM stg_lines l
LEFT JOIN names n ON n.stock_code = l.stock_code AND n.pick = 1
WHERE l.line_type IN ('sale', 'return')
GROUP BY l.stock_code, n.description;

CREATE OR REPLACE TABLE dim_customer AS
WITH countries AS (
    SELECT
        customer_id,
        country,
        row_number() OVER (PARTITION BY customer_id ORDER BY count(*) DESC, country) AS pick
    FROM stg_lines
    WHERE customer_id IS NOT NULL AND line_type IN ('sale', 'return')
    GROUP BY customer_id, country
)
SELECT
    l.customer_id,
    c.country,
    min(l.invoice_date) FILTER (WHERE l.line_type = 'sale') AS first_order_date,
    max(l.invoice_date) FILTER (WHERE l.line_type = 'sale') AS last_order_date
FROM stg_lines l
JOIN countries c ON c.customer_id = l.customer_id AND c.pick = 1
WHERE l.customer_id IS NOT NULL AND l.line_type IN ('sale', 'return')
GROUP BY l.customer_id, c.country;

CREATE OR REPLACE TABLE dim_date AS
SELECT
    CAST(d AS DATE)                         AS date,
    year(d)                                 AS year,
    month(d)                                AS month,
    CAST(date_trunc('month', d) AS DATE)    AS month_start,
    CAST(date_trunc('week', d) AS DATE)     AS week_start,   -- Monday
    isodow(d)                               AS weekday
FROM generate_series(
    (SELECT min(invoice_date) FROM stg_lines),
    (SELECT max(invoice_date) FROM stg_lines),
    INTERVAL 1 DAY
) AS t(d);
