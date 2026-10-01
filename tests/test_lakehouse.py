"""The PySpark + Delta Lake build must agree with the DuckDB warehouse."""
import shutil
from pathlib import Path

import pytest

pytest.importorskip("pyspark")
pytest.importorskip("delta")
if not (shutil.which("java") or Path("/opt/homebrew/opt/openjdk@17").exists()):
    pytest.skip("Java is not installed", allow_module_level=True)

from pyspark.sql import functions as F  # noqa: E402

from retail import lakehouse, warehouse  # noqa: E402


def test_lakehouse_reconciles_with_the_warehouse(conn, tmp_path):
    spark = lakehouse.spark_session()
    summary = lakehouse.build(root=tmp_path, spark=spark)
    profile = warehouse.profile(conn)

    assert summary["bronze_rows"] == profile["raw_rows"]
    assert [operation for _, operation in summary["bronze_versions"]] == ["WRITE", "WRITE"]  # one batch per sheet
    assert summary["silver_rows"] == profile["staged_rows"]
    assert summary["silver_line_types"] == profile["line_types"]

    gold = spark.read.format("delta").load(str(tmp_path / "gold" / "monthly_sales"))
    months = {str(r.month): (r.revenue, r.orders, r.returned_value) for r in gold.collect()}
    expected = conn.execute("SELECT CAST(month AS VARCHAR), revenue, orders, returned_value FROM mart_monthly").fetchall()
    assert len(months) == len(expected)
    for month, revenue, orders, returned in expected:
        assert months[month][0] == pytest.approx(revenue, abs=0.01)
        assert months[month][1] == orders
        assert months[month][2] == pytest.approx(returned, abs=0.01)

    customers = spark.read.format("delta").load(str(tmp_path / "gold" / "customer_value"))
    assert customers.count() == profile["tables"]["mart_customer_rfm"]
    assert customers.agg(F.sum("net_spend")).first()[0] == pytest.approx(
        conn.execute("SELECT sum(monetary) FROM mart_customer_rfm").fetchone()[0], abs=1.0)
    spark.stop()
