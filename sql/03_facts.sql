-- Fact table: customer transactions only (sales and returns of real products).
--
-- `is_reversed` marks a sale line that the same customer later returned in full (same product, same
-- quantity; the k-th such return cancels the k-th such sale). Those lines stay in gross sales, but
-- they are not demand, so the forecasting mart leaves them out. Two of them are single orders of
-- 81,000 and 74,000 units that were cancelled within minutes.
CREATE OR REPLACE TABLE fact_sales AS
WITH lines AS (
    SELECT
        line_id, invoice, invoice_ts, invoice_date, stock_code, customer_id, country,
        quantity, unit_price, amount, line_type,
        row_number() OVER (
            PARTITION BY customer_id, stock_code, abs(quantity), line_type ORDER BY invoice_ts, line_id
        ) AS k
    FROM stg_lines
    WHERE line_type IN ('sale', 'return')
)
SELECT
    s.* EXCLUDE (k),
    s.line_type = 'sale' AND r.line_id IS NOT NULL AS is_reversed
FROM lines s
LEFT JOIN lines r
    ON  s.line_type = 'sale' AND r.line_type = 'return'
    AND s.customer_id = r.customer_id
    AND s.stock_code = r.stock_code
    AND s.quantity = -r.quantity
    AND s.k = r.k
    AND r.invoice_ts >= s.invoice_ts;
