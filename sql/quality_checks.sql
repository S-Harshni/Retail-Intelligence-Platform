-- Each row is one check. `failures` must be 0; the pipeline stops otherwise.
SELECT 'fact_sales has no missing keys' AS check_name,
       count(*) AS failures
FROM fact_sales WHERE invoice IS NULL OR stock_code IS NULL OR invoice_date IS NULL
UNION ALL
SELECT 'sale lines have positive quantity and amount', count(*)
FROM fact_sales WHERE line_type = 'sale' AND (quantity <= 0 OR amount <= 0)
UNION ALL
SELECT 'return lines have negative quantity and amount', count(*)
FROM fact_sales WHERE line_type = 'return' AND (quantity >= 0 OR amount >= 0)
UNION ALL
SELECT 'every fact product exists in dim_product', count(*)
FROM fact_sales f LEFT JOIN dim_product p USING (stock_code) WHERE p.stock_code IS NULL
UNION ALL
SELECT 'every fact customer exists in dim_customer', count(*)
FROM fact_sales f LEFT JOIN dim_customer c USING (customer_id)
WHERE f.customer_id IS NOT NULL AND c.customer_id IS NULL
UNION ALL
SELECT 'dim_product has one row per stock code', count(*) - count(DISTINCT stock_code) FROM dim_product
UNION ALL
SELECT 'monthly revenue reconciles with the fact table',
       CAST(abs((SELECT coalesce(sum(revenue), 0) FROM mart_monthly)
              - (SELECT coalesce(sum(amount), 0) FROM fact_sales WHERE line_type = 'sale')) > 0.01 AS INTEGER)
UNION ALL
SELECT 'product revenue reconciles with the fact table',
       CAST(abs((SELECT coalesce(sum(revenue), 0) FROM mart_product)
              - (SELECT coalesce(sum(amount), 0) FROM fact_sales WHERE line_type = 'sale')) > 0.01 AS INTEGER)
UNION ALL
SELECT 'no invoice date falls outside dim_date', count(*)
FROM fact_sales f LEFT JOIN dim_date d ON d.date = f.invoice_date WHERE d.date IS NULL
UNION ALL
-- 1-9 December 2010 is in both sheets: after staging it must add up to exactly one sheet's worth.
SELECT 'the overlapping week is counted exactly once',
       CAST(abs((SELECT coalesce(sum(amount), 0) FROM stg_lines WHERE invoice_date BETWEEN DATE '2010-12-01' AND DATE '2010-12-09')
              - (SELECT coalesce(sum(quantity * price), 0) FROM raw_lines
                 WHERE sheet = 2 AND CAST(invoice_ts AS DATE) BETWEEN DATE '2010-12-01' AND DATE '2010-12-09')) > 0.01 AS INTEGER)
UNION ALL
SELECT 'only sale lines are marked as reversed', count(*) FROM fact_sales WHERE is_reversed AND line_type <> 'sale'
UNION ALL
SELECT 'fact_sales has one row per staged sale or return line',
       abs((SELECT count(*) FROM fact_sales) - (SELECT count(*) FROM stg_lines WHERE line_type IN ('sale', 'return')));
