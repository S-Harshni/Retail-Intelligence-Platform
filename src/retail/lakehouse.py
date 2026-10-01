"""The same warehouse as a lakehouse: PySpark jobs writing Delta Lake tables in three layers.

    bronze   the source rows as published, appended one sheet at a time, with load metadata
    silver   typed, de-duplicated and classified invoice lines (the rules of sql/01_staging.sql)
    gold     business tables: monthly sales, weekly product demand, customer value

    python -m retail.lakehouse        # needs Java 17; writes data/lakehouse/

The DuckDB warehouse stays the reference: a test checks that gold reconciles with it to the penny.
"""
import os
from pathlib import Path

from delta import DeltaTable, configure_spark_with_delta_pip
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from retail.config import DATA, PARQUET

LAKEHOUSE = DATA / "lakehouse"
PRODUCT_CODE = r"^[0-9]{5}[A-Z]{0,2}$"


def spark_session() -> SparkSession:
    brew_java = Path("/opt/homebrew/opt/openjdk@17")
    if "JAVA_HOME" not in os.environ and brew_java.exists():
        os.environ["JAVA_HOME"] = str(brew_java)
    builder = (
        SparkSession.builder.appName("retail-lakehouse").master("local[2]")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.enabled", "false")
        .config("spark.ui.showConsoleProgress", "false")
        .config("spark.driver.memory", "2g")
    )
    spark = configure_spark_with_delta_pip(builder).getOrCreate()
    spark.sparkContext.setLogLevel("ERROR")
    return spark


def load_bronze(spark: SparkSession, source: Path | str, root: Path) -> None:
    """Append each sheet of the source as its own batch, so the table history shows two loads."""
    path = str(root / "bronze" / "invoice_lines")
    raw = spark.read.parquet(str(source))
    for n, sheet in enumerate(sorted(r[0] for r in raw.select("sheet").distinct().collect())):
        batch = raw.where(F.col("sheet") == sheet).withColumn("_loaded_at", F.current_timestamp())
        batch.write.format("delta").mode("overwrite" if n == 0 else "append").save(path)


def build_silver(spark: SparkSession, root: Path) -> DataFrame:
    bronze = spark.read.format("delta").load(str(root / "bronze" / "invoice_lines"))
    typed = bronze.select(
        "sheet", "row_no",
        F.trim("invoice").alias("invoice"),
        F.upper(F.trim("stock_code")).alias("stock_code"),
        F.when(F.trim("description") != "", F.trim("description")).alias("description"),
        F.col("quantity").cast("int").alias("quantity"),
        F.col("invoice_ts").cast("timestamp").alias("invoice_ts"),
        F.col("price").cast("double").alias("unit_price"),
        F.col("customer_id").cast("int").alias("customer_id"),
        F.trim("country").alias("country"),
    )
    # 1-9 December 2010 is published in both sheets; keep the second sheet's copy.
    second_start = typed.where(F.col("sheet") == 2).agg(F.min("invoice_ts")).first()[0]
    if second_start is not None:
        typed = typed.where(~((F.col("sheet") == 1) & (F.col("invoice_ts") >= F.lit(second_start))))
    cancelled = F.col("invoice").startswith("C")
    silver = (
        typed.withColumn("invoice_date", F.to_date("invoice_ts"))
        .withColumn("amount", F.col("quantity") * F.col("unit_price"))
        .withColumn(
            "line_type",
            F.when(~F.col("stock_code").rlike(PRODUCT_CODE), "non_product")
            .when(cancelled & (F.col("quantity") < 0) & (F.col("unit_price") > 0), "return")
            .when(~cancelled & (F.col("quantity") > 0) & (F.col("unit_price") > 0), "sale")
            .otherwise("adjustment"),
        )
    )
    silver.write.format("delta").mode("overwrite").option("overwriteSchema", "true").partitionBy("line_type").save(
        str(root / "silver" / "invoice_lines"))
    return spark.read.format("delta").load(str(root / "silver" / "invoice_lines"))


def build_gold(spark: SparkSession, root: Path) -> dict[str, DataFrame]:
    lines = spark.read.format("delta").load(str(root / "silver" / "invoice_lines"))
    sales, returns = lines.where("line_type = 'sale'"), lines.where("line_type = 'return'")
    monthly = (
        lines.where("line_type IN ('sale', 'return')")
        .groupBy(F.trunc("invoice_date", "month").alias("month"))
        .agg(
            F.sum(F.when(F.col("line_type") == "sale", F.col("amount"))).alias("revenue"),
            F.countDistinct(F.when(F.col("line_type") == "sale", F.col("invoice"))).alias("orders"),
            F.coalesce(-F.sum(F.when(F.col("line_type") == "return", F.col("amount"))), F.lit(0.0)).alias("returned_value"),
        )
    )
    weekly = sales.groupBy(F.date_trunc("week", "invoice_date").cast("date").alias("week_start"), "stock_code").agg(
        F.sum("quantity").alias("units"), F.sum("amount").alias("revenue"), F.countDistinct("invoice").alias("orders"))
    customers = (
        sales.where("customer_id IS NOT NULL").groupBy("customer_id")
        .agg(F.countDistinct("invoice").alias("orders"), F.max("invoice_date").alias("last_order_date"),
             F.sum("amount").alias("gross_spend"))
        .join(returns.where("customer_id IS NOT NULL").groupBy("customer_id").agg((-F.sum("amount")).alias("returned")),
              "customer_id", "left")
        .withColumn("net_spend", F.round(F.col("gross_spend") - F.coalesce("returned", F.lit(0.0)), 2))
        .drop("returned")
    )
    tables = {"monthly_sales": monthly, "product_weekly": weekly, "customer_value": customers}
    for name, frame in tables.items():
        frame.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(str(root / "gold" / name))
    return {name: spark.read.format("delta").load(str(root / "gold" / name)) for name in tables}


def build(source: Path | str = PARQUET, root: Path = LAKEHOUSE, spark: SparkSession | None = None) -> dict:
    spark = spark or spark_session()
    load_bronze(spark, source, root)
    silver = build_silver(spark, root)
    gold = build_gold(spark, root)
    history = DeltaTable.forPath(spark, str(root / "bronze" / "invoice_lines")).history().select("version", "operation")
    return {
        "bronze_rows": spark.read.format("delta").load(str(root / "bronze" / "invoice_lines")).count(),
        "bronze_versions": [(r.version, r.operation) for r in history.orderBy("version").collect()],
        "silver_rows": silver.count(),
        "silver_line_types": {r.line_type: r["count"] for r in silver.groupBy("line_type").count().collect()},
        "gold_rows": {name: frame.count() for name, frame in gold.items()},
        "gold_revenue": round(gold["monthly_sales"].agg(F.sum("revenue")).first()[0], 2),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(build(), indent=1, default=str))
