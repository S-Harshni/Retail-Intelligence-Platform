-- Staging: one row per published invoice line, typed, de-duplicated across the two sheets and classified.
CREATE OR REPLACE TABLE stg_lines AS
WITH typed AS (
    SELECT
        sheet,
        row_no,
        trim(invoice)                 AS invoice,
        upper(trim(stock_code))       AS stock_code,
        nullif(trim(description), '') AS description,
        CAST(quantity AS INTEGER)     AS quantity,
        CAST(invoice_ts AS TIMESTAMP) AS invoice_ts,
        CAST(price AS DOUBLE)         AS unit_price,
        CAST(customer_id AS INTEGER)  AS customer_id,
        trim(country)                 AS country
    FROM raw_lines
),
-- 1-9 December 2010 is published in both sheets; keep the second sheet's copy.
deduplicated AS (
    SELECT *
    FROM typed
    WHERE NOT (sheet = 1 AND coalesce(invoice_ts >= (SELECT min(invoice_ts) FROM typed WHERE sheet = 2), FALSE))
)
SELECT
    row_number() OVER (ORDER BY sheet, row_no) AS line_id,
    * EXCLUDE (sheet, row_no),
    CAST(invoice_ts AS DATE)                   AS invoice_date,
    quantity * unit_price                      AS amount,
    CASE
        -- Product codes are five digits plus up to two letters; everything else is postage, fees,
        -- vouchers, manual entries or test rows.
        WHEN NOT regexp_full_match(stock_code, '[0-9]{5}[A-Z]{0,2}')     THEN 'non_product'
        WHEN invoice LIKE 'C%' AND quantity < 0 AND unit_price > 0     THEN 'return'
        WHEN invoice NOT LIKE 'C%' AND quantity > 0 AND unit_price > 0 THEN 'sale'
        -- Stock write-offs and zero-price lines: not a customer transaction.
        ELSE 'adjustment'
    END                                        AS line_type
FROM deduplicated;
