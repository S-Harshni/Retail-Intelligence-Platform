"""Convert the UCI Online Retail II workbook into one Parquet file.

    python -m retail.ingest      # downloads the archive if data/raw/online_retail_II.xlsx is missing

The workbook has two sheets (Dec 2009 - Dec 2010 and Dec 2010 - Dec 2011). Rows are kept exactly as
published, with the sheet number and row order added; all cleaning happens later in SQL.
"""
import hashlib
import urllib.request
import zipfile

import duckdb
import pandas as pd

from retail.config import PARQUET, RAW_XLSX, SOURCE_URL, SOURCE_ZIP_MD5

COLUMNS = {
    "Invoice": "invoice", "StockCode": "stock_code", "Description": "description", "Quantity": "quantity",
    "InvoiceDate": "invoice_ts", "Price": "price", "Customer ID": "customer_id", "Country": "country",
}


def download() -> None:
    RAW_XLSX.parent.mkdir(parents=True, exist_ok=True)
    archive = RAW_XLSX.with_suffix(".zip")
    urllib.request.urlretrieve(SOURCE_URL, archive)  # noqa: S310 - fixed https URL
    digest = hashlib.md5(archive.read_bytes()).hexdigest()  # noqa: S324 - integrity check, not security
    if digest != SOURCE_ZIP_MD5:
        raise ValueError(f"unexpected checksum {digest} for {archive}")
    with zipfile.ZipFile(archive) as z:
        z.extractall(RAW_XLSX.parent)


def read_workbook() -> pd.DataFrame:
    sheets = pd.read_excel(RAW_XLSX, sheet_name=None, dtype={"Invoice": str, "StockCode": str, "Description": str})
    frames = []
    for number, (_, frame) in enumerate(sheets.items(), start=1):
        frame = frame.rename(columns=COLUMNS)[list(COLUMNS.values())]
        frame.insert(0, "row_no", range(1, len(frame) + 1))
        frame.insert(0, "sheet", number)
        frames.append(frame)
    out = pd.concat(frames, ignore_index=True)
    out["customer_id"] = out["customer_id"].astype("Int64")
    return out


def main() -> None:
    if not RAW_XLSX.exists():
        download()
    raw = read_workbook()  # noqa: F841 - referenced by DuckDB below
    duckdb.sql(
        f"COPY (SELECT * FROM raw ORDER BY sheet, row_no) TO '{PARQUET}' (FORMAT parquet, COMPRESSION zstd)"
    )
    rows, first, last = duckdb.sql(f"SELECT count(*), min(invoice_ts), max(invoice_ts) FROM '{PARQUET}'").fetchone()
    print(f"{PARQUET.name}: {rows:,} rows, {first} to {last}, {PARQUET.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
