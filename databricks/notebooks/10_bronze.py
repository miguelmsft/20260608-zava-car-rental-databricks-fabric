# Databricks notebook source
# MAGIC %md
# MAGIC # 10 · BRONZE — ingest RAW into auditable Delta tables
# MAGIC
# MAGIC Promotes the nine `zava.raw.*` entities to `zava.bronze.*`, preserving every column
# MAGIC as ingested and adding lightweight **ingestion audit** columns. Bronze is the
# MAGIC immutable, append-of-record copy; cleaning/typing happens in silver.
# MAGIC
# MAGIC - Managed **Delta** tables (mirrorable format).
# MAGIC - `overwrite` writes → **idempotent** re-runs (no duplicate rows).

# COMMAND ----------

dbutils.widgets.text("catalog", "zava", "Unity Catalog catalog")
dbutils.widgets.text("raw_schema", "raw", "RAW source schema")
dbutils.widgets.text("bronze_schema", "bronze", "BRONZE target schema")
catalog = dbutils.widgets.get("catalog")
raw_schema = dbutils.widgets.get("raw_schema")
bronze_schema = dbutils.widgets.get("bronze_schema")
print(f"catalog={catalog} raw_schema={raw_schema} bronze_schema={bronze_schema}")

# COMMAND ----------

from pyspark.sql.functions import current_timestamp, lit

spark.sql(f"USE CATALOG {catalog}")

# The nine Step-3 entities landed by 00_generate_synthetic_data.py.
ENTITIES = [
    "vehicle_classes",
    "sites",
    "vehicles",
    "customers",
    "reservations",
    "rentals",
    "payments",
    "maintenance",
    "telematics",
]

BATCH_ID = spark.sql("SELECT uuid()").collect()[0][0]
print(f"batch_id={BATCH_ID}")

# COMMAND ----------

# MAGIC %md ## Ingest each entity raw -> bronze with audit columns

# COMMAND ----------

for name in ENTITIES:
    src = spark.table(f"{catalog}.{raw_schema}.{name}")
    bronze = (
        src
        .withColumn("_ingested_at", current_timestamp())
        .withColumn("_source_system", lit("zava-synthetic-generator"))
        .withColumn("_source_layer", lit(raw_schema))
        .withColumn("_batch_id", lit(BATCH_ID))
    )
    (
        bronze.write
        .format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(f"{catalog}.{bronze_schema}.{name}")
    )
    print(f"wrote {catalog}.{bronze_schema}.{name}: {bronze.count()} rows")

# COMMAND ----------

# MAGIC %md ## Verify

# COMMAND ----------

display(spark.sql(f"SHOW TABLES IN {catalog}.{bronze_schema}"))
for name in ("rentals", "payments", "customers"):
    print(name, spark.table(f"{catalog}.{bronze_schema}.{name}").count())
