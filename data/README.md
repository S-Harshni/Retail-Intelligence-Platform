# Data

`online_retail_ii.parquet` is the **Online Retail II** data set, converted from the published Excel workbook
by `python -m retail.ingest` (which also downloads the archive and checks its MD5). Rows and values are
unchanged; two columns are added, `sheet` and `row_no`, to record where each row came from.

- Source: UCI Machine Learning Repository, https://archive.ics.uci.edu/dataset/502/online+retail+ii
- Citation: Chen, D. (2012). *Online Retail II* [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5CG6D
- Licence: Creative Commons Attribution 4.0 International (CC BY 4.0)
- Contents: 1,067,371 invoice lines of a UK-based online gift-ware retailer, 1 December 2009 to 9 December 2011

| Column | Meaning |
| --- | --- |
| `invoice` | Invoice number; a leading `C` marks a cancellation |
| `stock_code` | Product code (five digits plus optional letters); other codes are postage, fees and manual entries |
| `description` | Product name |
| `quantity` | Units on the line; negative on cancellations and stock write-offs |
| `invoice_ts` | Invoice date and time |
| `price` | Unit price in pounds sterling |
| `customer_id` | Customer number; missing on about a fifth of lines |
| `country` | Customer's country |
