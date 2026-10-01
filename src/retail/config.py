"""Paths and the few constants shared across modules."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW_XLSX = DATA / "raw" / "online_retail_II.xlsx"
PARQUET = DATA / "online_retail_ii.parquet"
WAREHOUSE = DATA / "warehouse.duckdb"
SQL = ROOT / "sql"
EXPORT = ROOT / "docs" / "data"

SOURCE_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
SOURCE_ZIP_MD5 = "dc74ff2011818f6baebf525b50f19a07"
SEED = 42
