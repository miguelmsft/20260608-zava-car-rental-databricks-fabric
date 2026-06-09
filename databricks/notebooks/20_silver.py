# Databricks notebook source
# MAGIC %md
# MAGIC # 20 · SILVER — cleaned, typed, de-duplicated, referentially-valid entities
# MAGIC
# MAGIC Transforms `zava.bronze.*` into conformed `zava.silver.*` tables:
# MAGIC
# MAGIC - **Type casting** — ISO strings → `date`/`timestamp`; numeric strings → `double`/`int`;
# MAGIC   flags → `boolean`.
# MAGIC - **De-duplication** — one row per primary key.
# MAGIC - **Validation** — drop rows with a null PK; assert the child→parent foreign keys
# MAGIC   still resolve (referential integrity preserved from the Step-3 entities).
# MAGIC - Managed **Delta**, `overwrite` → idempotent.
# MAGIC
# MAGIC Kept intentionally **simple** — the medallion is the setup, not the star.

# COMMAND ----------

dbutils.widgets.text("catalog", "zava", "Unity Catalog catalog")
dbutils.widgets.text("bronze_schema", "bronze", "BRONZE source schema")
dbutils.widgets.text("silver_schema", "silver", "SILVER target schema")
catalog = dbutils.widgets.get("catalog")
bronze_schema = dbutils.widgets.get("bronze_schema")
silver_schema = dbutils.widgets.get("silver_schema")
spark.sql(f"USE CATALOG {catalog}")
print(f"catalog={catalog} bronze_schema={bronze_schema} silver_schema={silver_schema}")

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

TS_FMT = "yyyy-MM-dd'T'HH:mm:ss'Z'"  # generator emits ISO-8601 UTC with a trailing 'Z'
AUDIT_COLS = ["_ingested_at", "_source_system", "_source_layer", "_batch_id"]


def bronze(name: str) -> DataFrame:
    """Read a bronze entity, dropping ingestion audit columns (re-added per-layer)."""
    df = spark.table(f"{catalog}.{bronze_schema}.{name}")
    return df.drop(*[c for c in AUDIT_COLS if c in df.columns])


def dedup(df: DataFrame, pk: str) -> DataFrame:
    return df.filter(F.col(pk).isNotNull()).dropDuplicates([pk])


def write_silver(df: DataFrame, name: str) -> None:
    (
        df.write.format("delta").mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(f"{catalog}.{silver_schema}.{name}")
    )
    print(f"wrote {catalog}.{silver_schema}.{name}: {df.count()} rows")

# COMMAND ----------

# MAGIC %md ## Dimensions: vehicle_classes, sites, vehicles, customers

# COMMAND ----------

vehicle_classes = (
    dedup(bronze("vehicle_classes"), "vehicle_class_id")
    .withColumn("daily_base_rate_usd", F.col("daily_base_rate_usd").cast("double"))
    .withColumn("seats", F.col("seats").cast("int"))
    .withColumn("is_ev", F.col("is_ev").cast("boolean"))
)
write_silver(vehicle_classes, "vehicle_classes")

sites = (
    dedup(bronze("sites"), "site_id")
    .withColumn("latitude", F.col("latitude").cast("double"))
    .withColumn("longitude", F.col("longitude").cast("double"))
    .withColumn("is_hq", F.col("is_hq").cast("boolean"))
    .withColumn("parking_capacity", F.col("parking_capacity").cast("int"))
    .withColumn("opened_date", F.to_date("opened_date"))
    .withColumn("city", F.trim("city"))
    .withColumn("state", F.upper(F.trim("state")))
)
write_silver(sites, "sites")

vehicles = (
    dedup(bronze("vehicles"), "vehicle_id")
    .withColumn("model_year", F.col("model_year").cast("int"))
    .withColumn("odometer_miles", F.col("odometer_miles").cast("long"))
    .withColumn("acquired_date", F.to_date("acquired_date"))
    .withColumn("status", F.lower(F.trim("status")))
)
write_silver(vehicles, "vehicles")

customers = (
    dedup(bronze("customers"), "customer_id")
    .withColumn("email", F.trim("email"))
    .withColumn("phone", F.trim("phone"))
    .withColumn("join_date", F.to_date("join_date"))
)
write_silver(customers, "customers")

# COMMAND ----------

# MAGIC %md ## Facts / events: reservations, rentals, payments, maintenance, telematics

# COMMAND ----------

reservations = (
    dedup(bronze("reservations"), "reservation_id")
    .withColumn("reserved_at", F.to_timestamp("reserved_at", TS_FMT))
    .withColumn("pickup_date", F.to_date("pickup_date"))
    .withColumn("planned_return_date", F.to_date("planned_return_date"))
    .withColumn("status", F.lower(F.trim("status")))
)
write_silver(reservations, "reservations")

rentals = (
    dedup(bronze("rentals"), "rental_id")
    .withColumn("pickup_ts", F.to_timestamp("pickup_ts", TS_FMT))
    .withColumn("planned_return_ts", F.to_timestamp("planned_return_ts", TS_FMT))
    .withColumn("return_ts", F.to_timestamp("return_ts", TS_FMT))
    .withColumn("odometer_out", F.col("odometer_out").cast("long"))
    .withColumn("odometer_in", F.col("odometer_in").cast("long"))
    .withColumn("miles_driven", F.col("miles_driven").cast("long"))
    .withColumn("is_one_way", F.col("is_one_way").cast("boolean"))
    .withColumn("status", F.lower(F.trim("status")))
    # Derived measure: actual rental duration in days (>=1), null while still open.
    .withColumn(
        "rental_days",
        F.when(
            F.col("return_ts").isNotNull(),
            F.greatest(F.lit(1), F.datediff(F.col("return_ts"), F.col("pickup_ts"))),
        ),
    )
)
write_silver(rentals, "rentals")

payments = (
    dedup(bronze("payments"), "payment_id")
    .withColumn("rental_days", F.col("rental_days").cast("int"))
    .withColumn("base_amount_usd", F.col("base_amount_usd").cast("double"))
    .withColumn("one_way_fee_usd", F.col("one_way_fee_usd").cast("double"))
    .withColumn("taxes_fees_usd", F.col("taxes_fees_usd").cast("double"))
    .withColumn("total_amount_usd", F.col("total_amount_usd").cast("double"))
    .withColumn("payment_ts", F.to_timestamp("payment_ts", TS_FMT))
    .withColumn("status", F.lower(F.trim("status")))
)
write_silver(payments, "payments")

maintenance = (
    dedup(bronze("maintenance"), "maintenance_id")
    .withColumn("opened_ts", F.to_timestamp("opened_ts", TS_FMT))
    .withColumn("closed_ts", F.to_timestamp("closed_ts", TS_FMT))
    .withColumn("labor_cost_usd", F.col("labor_cost_usd").cast("double"))
    .withColumn("parts_cost_usd", F.col("parts_cost_usd").cast("double"))
    .withColumn("total_cost_usd", F.col("total_cost_usd").cast("double"))
    .withColumn("odometer_at_service", F.col("odometer_at_service").cast("long"))
    .withColumn("status", F.lower(F.trim("status")))
)
write_silver(maintenance, "maintenance")

telematics = (
    dedup(bronze("telematics"), "telematics_id")
    .withColumn("snapshot_ts", F.to_timestamp("snapshot_ts", TS_FMT))
    .withColumn("idle_minutes", F.col("idle_minutes").cast("int"))
    .withColumn("odometer_miles", F.col("odometer_miles").cast("long"))
    .withColumn("latitude", F.col("latitude").cast("double"))
    .withColumn("longitude", F.col("longitude").cast("double"))
    .withColumn("speed_mph", F.col("speed_mph").cast("int"))
    .withColumn("fuel_or_soc_pct", F.col("fuel_or_soc_pct").cast("int"))
)
write_silver(telematics, "telematics")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Referential-integrity checks (fail-fast)
# MAGIC Confirms the Step-3 FK relationships survived cleaning. Any orphan count > 0 aborts.

# COMMAND ----------


def assert_fk(child: str, child_col: str, parent: str, parent_col: str) -> None:
    c = spark.table(f"{catalog}.{silver_schema}.{child}")
    p = spark.table(f"{catalog}.{silver_schema}.{parent}").select(F.col(parent_col).alias("_pk"))
    orphans = (
        c.filter(F.col(child_col).isNotNull())
        .join(p, c[child_col] == F.col("_pk"), "left_anti")
        .count()
    )
    if orphans:
        raise AssertionError(f"FK violation: {child}.{child_col} -> {parent}.{parent_col} ({orphans} orphans)")
    print(f"OK  {child}.{child_col} -> {parent}.{parent_col}")


assert_fk("vehicles", "vehicle_class_id", "vehicle_classes", "vehicle_class_id")
assert_fk("vehicles", "home_site_id", "sites", "site_id")
assert_fk("vehicles", "current_site_id", "sites", "site_id")
assert_fk("rentals", "vehicle_id", "vehicles", "vehicle_id")
assert_fk("rentals", "customer_id", "customers", "customer_id")
assert_fk("rentals", "pickup_site_id", "sites", "site_id")
assert_fk("rentals", "return_site_id", "sites", "site_id")
assert_fk("payments", "rental_id", "rentals", "rental_id")
assert_fk("maintenance", "vehicle_id", "vehicles", "vehicle_id")
assert_fk("telematics", "vehicle_id", "vehicles", "vehicle_id")
print("silver referential integrity: OK")
