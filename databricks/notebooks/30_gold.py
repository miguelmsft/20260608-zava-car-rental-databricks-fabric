# Databricks notebook source
# MAGIC %md
# MAGIC # 30 · GOLD — business-ready, mirrorable, certifiable data asset
# MAGIC
# MAGIC Builds the gold layer from `zava.silver.*`: conformed **dimensions**, a central
# MAGIC **fact** (`fact_rental`), and four business **KPI** tables that drive the demo's
# MAGIC report / ontology / agent (revenue, fleet utilization, one-way flows, maintenance
# MAGIC cost — see `data/schema/data_dictionary.md`).
# MAGIC
# MAGIC **Every gold table is a standard UC managed Delta table** — the only object type that
# MAGIC is **mirrorable into Microsoft Fabric** (Variation 1). No views, streaming tables, or
# MAGIC materialized views here (those are NOT mirrored — R1). Certification (tags / comments
# MAGIC / ownership) is applied next by `databricks/uc/04_certify_gold.sql`.
# MAGIC
# MAGIC `overwrite` writes → idempotent re-runs.

# COMMAND ----------

dbutils.widgets.text("catalog", "zava", "Unity Catalog catalog")
dbutils.widgets.text("silver_schema", "silver", "SILVER source schema")
dbutils.widgets.text("gold_schema", "gold", "GOLD target schema")
catalog = dbutils.widgets.get("catalog")
silver_schema = dbutils.widgets.get("silver_schema")
gold_schema = dbutils.widgets.get("gold_schema")
spark.sql(f"USE CATALOG {catalog}")
print(f"catalog={catalog} silver_schema={silver_schema} gold_schema={gold_schema}")

# COMMAND ----------

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def s(name: str) -> DataFrame:
    return spark.table(f"{catalog}.{silver_schema}.{name}")


def write_gold(df: DataFrame, name: str) -> None:
    (
        df.write.format("delta").mode("overwrite")
        .option("overwriteSchema", "true")
        .saveAsTable(f"{catalog}.{gold_schema}.{name}")
    )
    print(f"wrote {catalog}.{gold_schema}.{name}: {df.count()} rows")

# COMMAND ----------

# MAGIC %md ## Dimensions

# COMMAND ----------

dim_site = s("sites").select(
    "site_id", "site_code", "site_name", "city", "state",
    "latitude", "longitude", "is_hq", "parking_capacity", "opened_date",
)
write_gold(dim_site, "dim_site")

dim_vehicle = (
    s("vehicles").alias("v")
    .join(s("vehicle_classes").alias("c"), "vehicle_class_id", "left")
    .select(
        "v.vehicle_id", "v.vin", "v.make", "v.model", "v.model_year", "v.color",
        "v.license_plate", "v.status", "v.odometer_miles", "v.acquired_date",
        "v.home_site_id", "v.current_site_id",
        "vehicle_class_id", "c.class_name", "c.category",  # join key is unqualified after the join
        "c.daily_base_rate_usd", "c.seats", "c.is_ev",
    )
)
write_gold(dim_vehicle, "dim_vehicle")

# Synthetic PII-like columns (email/phone) are carried through for the governance demo;
# column masks are applied later (Step 9 / Policy Weaver).
dim_customer = s("customers").select(
    "customer_id", "first_name", "last_name", "email", "phone",
    "loyalty_tier", "home_city", "home_state", "join_date",
)
write_gold(dim_customer, "dim_customer")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Central fact: `fact_rental`
# MAGIC Rentals enriched with settled revenue (from `payments`), site names, and class —
# MAGIC the grain is one row per rental.

# COMMAND ----------

# One rental can have at most a few payment rows; roll revenue up to the rental grain.
pay_by_rental = (
    s("payments")
    .groupBy("rental_id")
    .agg(
        F.round(F.sum("total_amount_usd"), 2).alias("revenue_usd"),
        F.round(F.sum("base_amount_usd"), 2).alias("base_amount_usd"),
        F.round(F.sum("one_way_fee_usd"), 2).alias("one_way_fee_usd"),
        F.round(F.sum("taxes_fees_usd"), 2).alias("taxes_fees_usd"),
    )
)

pickup = s("sites").select(
    F.col("site_id").alias("pickup_site_id"),
    F.col("site_name").alias("pickup_site_name"),
    F.col("city").alias("pickup_city"),
    F.col("state").alias("pickup_state"),
)
ret = s("sites").select(
    F.col("site_id").alias("return_site_id"),
    F.col("site_name").alias("return_site_name"),
    F.col("city").alias("return_city"),
)
veh_class = s("vehicles").select(
    "vehicle_id", "vehicle_class_id"
).join(
    s("vehicle_classes").select("vehicle_class_id", "class_name", "category"),
    "vehicle_class_id", "left",
)

fact_rental = (
    s("rentals").alias("r")
    .join(pay_by_rental, "rental_id", "left")
    .join(pickup, "pickup_site_id", "left")
    .join(ret, "return_site_id", "left")
    .join(veh_class, "vehicle_id", "left")
    .select(
        "rental_id", "reservation_id", "customer_id", "vehicle_id",
        veh_class["vehicle_class_id"], "class_name", "category",
        "pickup_site_id", "pickup_site_name", "pickup_city", "pickup_state",
        "return_site_id", "return_site_name", "return_city",
        "pickup_ts", "return_ts", "rental_days", "miles_driven",
        "is_one_way", "status",
        F.coalesce(F.col("revenue_usd"), F.lit(0.0)).alias("revenue_usd"),
        "base_amount_usd", "one_way_fee_usd", "taxes_fees_usd",
    )
)
write_gold(fact_rental, "fact_rental")

# COMMAND ----------

# MAGIC %md ## KPI: revenue by site

# COMMAND ----------

kpi_revenue_by_site = (
    spark.table(f"{catalog}.{gold_schema}.fact_rental")
    .groupBy("pickup_site_id", "pickup_site_name", "pickup_city", "pickup_state")
    .agg(
        F.countDistinct("rental_id").alias("total_rentals"),
        F.round(F.sum("revenue_usd"), 2).alias("total_revenue_usd"),
        F.round(F.avg("revenue_usd"), 2).alias("avg_revenue_per_rental_usd"),
        F.round(F.avg("rental_days"), 1).alias("avg_rental_days"),
    )
    .orderBy(F.col("total_revenue_usd").desc())
)
write_gold(kpi_revenue_by_site, "kpi_revenue_by_site")

# COMMAND ----------

# MAGIC %md ## KPI: fleet utilization by vehicle class

# COMMAND ----------

rented_days_by_class = (
    spark.table(f"{catalog}.{gold_schema}.fact_rental")
    .groupBy("vehicle_class_id", "class_name", "category")
    .agg(
        F.countDistinct("rental_id").alias("total_rentals"),
        F.round(F.sum("rental_days"), 0).alias("total_rented_days"),
        F.round(F.avg("rental_days"), 1).alias("avg_rental_days"),
    )
)
fleet_by_class = s("vehicles").groupBy("vehicle_class_id").agg(
    F.countDistinct("vehicle_id").alias("fleet_size")
)
kpi_fleet_utilization = (
    rented_days_by_class.join(fleet_by_class, "vehicle_class_id", "left")
    .withColumn(
        "rentals_per_vehicle",
        F.round(F.col("total_rentals") / F.col("fleet_size"), 2),
    )
    .orderBy(F.col("total_rented_days").desc())
)
write_gold(kpi_fleet_utilization, "kpi_fleet_utilization")

# COMMAND ----------

# MAGIC %md ## KPI: one-way flows (pickup-site → return-site movement matrix)

# COMMAND ----------

kpi_one_way_flows = (
    spark.table(f"{catalog}.{gold_schema}.fact_rental")
    .filter(F.col("is_one_way") == True)  # noqa: E712  (Spark Column boolean compare)
    .groupBy(
        "pickup_site_id", "pickup_site_name", "pickup_city",
        "return_site_id", "return_site_name", "return_city",
    )
    .agg(
        F.countDistinct("rental_id").alias("one_way_trips"),
        F.round(F.sum("revenue_usd"), 2).alias("one_way_revenue_usd"),
    )
    .orderBy(F.col("one_way_trips").desc())
)
write_gold(kpi_one_way_flows, "kpi_one_way_flows")

# COMMAND ----------

# MAGIC %md ## KPI: maintenance cost by site

# COMMAND ----------

kpi_maintenance_cost = (
    s("maintenance").alias("m")
    .join(
        s("sites").select(
            F.col("site_id"),
            F.col("site_name"),
            F.col("city").alias("site_city"),
            F.col("state").alias("site_state"),
        ),
        "site_id", "left",
    )
    .groupBy("site_id", "site_name", "site_city", "site_state")
    .agg(
        F.count("maintenance_id").alias("maintenance_events"),
        F.round(F.sum("total_cost_usd"), 2).alias("total_maintenance_cost_usd"),
        F.round(F.sum("labor_cost_usd"), 2).alias("total_labor_cost_usd"),
        F.round(F.sum("parts_cost_usd"), 2).alias("total_parts_cost_usd"),
    )
    .orderBy(F.col("total_maintenance_cost_usd").desc())
)
write_gold(kpi_maintenance_cost, "kpi_maintenance_cost")

# COMMAND ----------

# MAGIC %md ## Verify — all gold tables present and non-empty

# COMMAND ----------

display(spark.sql(f"SHOW TABLES IN {catalog}.{gold_schema}"))
for t in (
    "dim_site", "dim_vehicle", "dim_customer", "fact_rental",
    "kpi_revenue_by_site", "kpi_fleet_utilization",
    "kpi_one_way_flows", "kpi_maintenance_cost",
):
    n = spark.table(f"{catalog}.{gold_schema}.{t}").count()
    print(f"{t:26s} {n:>8d} rows")
    assert n > 0, f"gold.{t} is empty"
print("gold layer: OK")
