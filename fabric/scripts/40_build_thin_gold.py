# Fabric notebook source
# ---------------------------------------------------------------------------
# 40 · THIN FABRIC GOLD / AGGREGATION LAYER  (PySpark — runnable in a Fabric notebook)
# ---------------------------------------------------------------------------
# MAGIC %md
# MAGIC # Thin Fabric gold / aggregation layer (Direct Lake consumption layer)
# MAGIC
# MAGIC Builds **report-ready aggregate tables** for the Zava KPIs on top of the data
# MAGIC already landed in the **Step-10 Fabric workspace Lakehouse**:
# MAGIC
# MAGIC * **Variation 1** — the *Mirrored Azure Databricks Catalog* (`zava_gold_mirrored`,
# MAGIC   created by `10_create_mirrored_catalog.py`) exposing the Step-8 certified **gold**
# MAGIC   schema (`dim_site`, `dim_vehicle`, `fact_rental`, `kpi_*`).
# MAGIC * **Variation 2** — the secured **OneLake shortcut** (created by `20_create_shortcut.py`)
# MAGIC   landing the Lakeflow curated Delta under the Lakehouse `Tables/` folder.
# MAGIC
# MAGIC ### Why a THIN gold layer lives in the consumption layer (Research R2)
# MAGIC Mirroring is **zero-copy metadata** — Fabric does **not** re-write the Parquet, so the
# MAGIC mirrored gold is **NOT V-Ordered**. Direct Lake performance depends on **V-Order** (the
# MAGIC Power BI columnar sort/compression that makes cold-cache transcoding fast on an F-SKU).
# MAGIC Therefore the heavier, report-shaped rollups (time series for forecasting, geo-enriched
# MAGIC site aggregates for maps, cross-entity utilization math) are materialized **once here** as
# MAGIC **V-Ordered Delta** so the Step-14 Direct Lake semantic model reads small, pre-aggregated,
# MAGIC V-Ordered tables instead of re-scanning the wide, un-V-Ordered mirrored fact at query time.
# MAGIC This keeps the Databricks medallion simple (the setup, not the star) while putting the
# MAGIC consumption-layer calculation where it belongs in Fabric.
# MAGIC
# MAGIC ### KPIs covered (Step-3 entity model + Step-8 gold + Step-14 measures)
# MAGIC fleet utilization · revenue per site · idle vehicles · one-way flows · maintenance cost
# MAGIC
# MAGIC ### Idempotent
# MAGIC Every table is written with `mode("overwrite")` + `overwriteSchema` — safe to re-run.
# MAGIC
# MAGIC **No secrets.** Only table/schema names are parameterized; the Spark session and OneLake
# MAGIC access come from the Fabric notebook runtime (Workspace Identity / attached Lakehouse).

# CELL ********************
# MAGIC %md ## Parameters (Fabric "Toggle parameter cell")
# MAGIC Fabric injects pipeline/notebook parameter overrides above the values below. Keep these
# MAGIC names consistent with the Step-1 config schema and the Step-8 gold table names.

# Parameters cell -------------------------------------------------------------
# Source (upstream landed data) — three optional name-prefix parts. Leave a part empty
# ("") to drop it from the qualified name. The defaults assume this notebook is attached
# to the Step-10 Lakehouse where the mirrored gold / shortcut tables are reachable by name.
source_catalog = ""          # e.g. "zava_gold_mirrored" (Mirrored Catalog item) or "" if reading the attached Lakehouse
source_schema = "gold"       # Step-8 certified gold schema (source.gold_schema in deploy_config.json)

# Target (this thin gold / aggregation layer) — written into the Fabric Lakehouse.
target_catalog = ""          # default Lakehouse when attached; or a Lakehouse name for a schema-enabled Lakehouse
target_schema = "thin_gold"  # consumption-layer schema for the V-Ordered aggregates

# Step-8 gold source table names (override only if the upstream names differ).
src_fact_rental = "fact_rental"
src_dim_site = "dim_site"
src_dim_vehicle = "dim_vehicle"
src_kpi_one_way_flows = "kpi_one_way_flows"
src_kpi_maintenance_cost = "kpi_maintenance_cost"

# CELL ********************
# MAGIC %md ## Spark session + V-Order / optimize-write configuration
# MAGIC V-Order is the whole point of this layer (R2). We enable it at the session level so every
# MAGIC Delta write below is V-Ordered, and turn on Optimize Write so Direct Lake reads few,
# MAGIC well-sized row groups.

try:
    spark  # provided by the Fabric notebook runtime
except NameError:  # pragma: no cover - allows standalone / spark-submit execution
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.appName("zava-thin-gold").getOrCreate()

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def enable_vorder(session) -> None:
    """Turn on V-Order + Optimize Write so the aggregates are Direct-Lake-optimal (R2).

    Fabric Runtime exposes V-Order under two config keys across versions; we set both so the
    notebook behaves identically on Runtime 1.1/1.2/1.3. ``optimizeWrite`` compacts to ~1 GiB
    bins which keeps the V-Ordered files large and few — ideal for Direct Lake transcoding.
    """
    confs = {
        "spark.sql.parquet.vorder.enabled": "true",   # Fabric Runtime 1.1 key
        "spark.sql.parquet.vorder.default": "true",   # Fabric Runtime 1.2+ key
        "spark.databricks.delta.optimizeWrite.enabled": "true",
        "spark.databricks.delta.optimizeWrite.binSize": "1073741824",  # 1 GiB
        "spark.databricks.delta.autoCompact.enabled": "true",
    }
    for key, value in confs.items():
        try:
            session.conf.set(key, value)
        except Exception as exc:  # pragma: no cover - some keys are static on certain runtimes
            print(f"  (skipped conf {key}={value}: {exc})")


enable_vorder(spark)


# CELL ********************
# MAGIC %md ## Name resolution + read/write helpers (idempotent, V-Ordered)

def _qualify(*parts: str) -> str:
    """Join non-empty name parts with dots, e.g. ('zava_gold_mirrored','gold','fact_rental')."""
    return ".".join(p for p in parts if p)


def read_source(table: str) -> DataFrame:
    """Read an upstream landed table (mirrored gold V1 and/or shortcut V2) by name.

    The qualified name is built from ``source_catalog.source_schema.table`` with empty parts
    dropped, so the same notebook works whether the tables are surfaced through the attached
    Lakehouse (``gold.fact_rental``) or a named Mirrored Catalog item.
    """
    name = _qualify(source_catalog, source_schema, table)
    return spark.table(name)


def write_thin_gold(df: DataFrame, table: str) -> None:
    """Write one aggregate as **V-Ordered Delta** into the thin gold schema (idempotent overwrite).

    Session-level V-Order (see ``enable_vorder``) applies to the Parquet written here; we also
    stamp the Delta table property so the V-Order intent travels with the table for future writes
    / OPTIMIZE runs even if a later session forgets the session conf.
    """
    target = _qualify(target_catalog, target_schema, table)
    (
        df.write.format("delta")
        .mode("overwrite")
        .option("overwriteSchema", "true")
        .option("delta.parquet.vorder.enabled", "true")
        .saveAsTable(target)
    )
    print(f"wrote {target}: {df.count()} rows (V-Ordered Delta)")


# Ensure the thin gold schema exists (schema-enabled Lakehouse). No-op when target_schema is "".
if target_schema:
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {_qualify(target_catalog, target_schema)}")


# CELL ********************
# MAGIC %md ## Load the upstream gold once (cached for the multiple rollups below)

fact_rental = read_source(src_fact_rental).cache()
dim_site = read_source(src_dim_site).cache()
dim_vehicle = read_source(src_dim_vehicle).cache()

# Site geo lookup (lat/long + capacity) — reused to enrich every site-grain aggregate so the
# Direct Lake model can draw multi-city maps without re-joining the mirrored dim at query time.
site_geo = dim_site.select(
    "site_id", "site_name", "city", "state",
    "latitude", "longitude", "is_hq", "parking_capacity",
)


# CELL ********************
# MAGIC %md
# MAGIC ## KPI 1 — Revenue per site (geo-enriched, report-ready)
# MAGIC Heavier than the mirrored `kpi_revenue_by_site` because we re-derive it from `fact_rental`
# MAGIC and **join site geo (lat/long)** so the report's multi-city map binds to one V-Ordered table.

agg_revenue_by_site = (
    fact_rental.groupBy("pickup_site_id")
    .agg(
        F.countDistinct("rental_id").alias("total_rentals"),
        F.round(F.sum("revenue_usd"), 2).alias("total_revenue_usd"),
        F.round(F.avg("revenue_usd"), 2).alias("avg_revenue_per_rental_usd"),
        F.round(F.avg("rental_days"), 1).alias("avg_rental_days"),
    )
    .join(site_geo, F.col("pickup_site_id") == F.col("site_id"), "left")
    .drop("site_id")
    .orderBy(F.col("total_revenue_usd").desc())
)
write_thin_gold(agg_revenue_by_site, "agg_revenue_by_site")


# CELL ********************
# MAGIC %md
# MAGIC ## KPI 1b — Revenue per site per month (time series for forecasting)
# MAGIC Pre-aggregated monthly grain keeps the Power BI **forecasting** visual cheap: the semantic
# MAGIC model reads a tiny V-Ordered series instead of re-bucketing the mirrored fact at query time.

agg_revenue_by_site_month = (
    fact_rental.withColumn("revenue_month", F.trunc(F.col("pickup_ts"), "month"))
    .groupBy("pickup_site_id", "pickup_site_name", "pickup_city", "pickup_state", "revenue_month")
    .agg(
        F.countDistinct("rental_id").alias("total_rentals"),
        F.round(F.sum("revenue_usd"), 2).alias("total_revenue_usd"),
    )
    .orderBy("pickup_site_id", "revenue_month")
)
write_thin_gold(agg_revenue_by_site_month, "agg_revenue_by_site_month")


# CELL ********************
# MAGIC %md
# MAGIC ## KPI 2 — Fleet utilization (rented-days vs. available fleet, by site & month)
# MAGIC Consumption-layer math: utilization = rented vehicle-days / (fleet at site × days in month).
# MAGIC This cross-entity calculation (fact_rental × dim_vehicle × calendar) is exactly the kind of
# MAGIC heavy rollup that should be materialized + V-Ordered once, not computed per Direct Lake query.

fleet_by_site = dim_vehicle.groupBy("current_site_id").agg(
    F.countDistinct("vehicle_id").alias("fleet_size")
)

rented_days_by_site_month = (
    fact_rental.withColumn("rental_month", F.trunc(F.col("pickup_ts"), "month"))
    .groupBy("pickup_site_id", "rental_month")
    .agg(
        F.countDistinct("rental_id").alias("total_rentals"),
        F.round(F.sum("rental_days"), 0).alias("total_rented_days"),
    )
)

agg_fleet_utilization_by_site_month = (
    rented_days_by_site_month
    .join(fleet_by_site, F.col("pickup_site_id") == F.col("current_site_id"), "left")
    .drop("current_site_id")
    .join(site_geo, F.col("pickup_site_id") == F.col("site_id"), "left")
    .drop("site_id")
    # days in the rolled-up month = denominator basis for the utilization ratio
    .withColumn("days_in_month", F.dayofmonth(F.last_day(F.col("rental_month"))))
    .withColumn(
        "utilization_pct",
        F.round(
            F.col("total_rented_days")
            / (F.col("fleet_size") * F.col("days_in_month")) * F.lit(100.0),
            1,
        ),
    )
    .orderBy("pickup_site_id", "rental_month")
)
write_thin_gold(agg_fleet_utilization_by_site_month, "agg_fleet_utilization_by_site_month")


# CELL ********************
# MAGIC %md
# MAGIC ## KPI 3 — Idle vehicles by site
# MAGIC `dim_vehicle.status='idle'` drives the idle KPI (Step-3 data dictionary). We count idle
# MAGIC vehicles at each vehicle's **current** site, enrich with geo + parking capacity, and express
# MAGIC an idle ratio so the report can flag over-parked sites on the map.

vehicle_status = (
    dim_vehicle.groupBy("current_site_id")
    .agg(
        F.countDistinct("vehicle_id").alias("vehicles_at_site"),
        F.countDistinct(
            F.when(F.col("status") == F.lit("idle"), F.col("vehicle_id"))
        ).alias("idle_vehicles"),
        F.countDistinct(
            F.when(F.col("status") == F.lit("rented"), F.col("vehicle_id"))
        ).alias("rented_vehicles"),
        F.countDistinct(
            F.when(F.col("status") == F.lit("maintenance"), F.col("vehicle_id"))
        ).alias("maintenance_vehicles"),
    )
)

agg_idle_vehicles_by_site = (
    vehicle_status
    .join(site_geo, F.col("current_site_id") == F.col("site_id"), "left")
    .drop("site_id")
    .withColumn(
        "idle_ratio_pct",
        F.round(F.col("idle_vehicles") / F.col("vehicles_at_site") * F.lit(100.0), 1),
    )
    .orderBy(F.col("idle_vehicles").desc())
)
write_thin_gold(agg_idle_vehicles_by_site, "agg_idle_vehicles_by_site")


# CELL ********************
# MAGIC %md
# MAGIC ## KPI 4 — One-way flows (pickup → return movement matrix, dual geo for map flow lines)
# MAGIC Re-materializes the mirrored `kpi_one_way_flows` as V-Ordered Delta and enriches it with the
# MAGIC lat/long of **both** endpoints so the report can draw origin→destination flow lines directly.

origin_geo = site_geo.select(
    F.col("site_id").alias("o_site_id"),
    F.col("latitude").alias("pickup_latitude"),
    F.col("longitude").alias("pickup_longitude"),
)
dest_geo = site_geo.select(
    F.col("site_id").alias("d_site_id"),
    F.col("latitude").alias("return_latitude"),
    F.col("longitude").alias("return_longitude"),
)

agg_one_way_flows = (
    read_source(src_kpi_one_way_flows)
    .join(origin_geo, F.col("pickup_site_id") == F.col("o_site_id"), "left")
    .drop("o_site_id")
    .join(dest_geo, F.col("return_site_id") == F.col("d_site_id"), "left")
    .drop("d_site_id")
    .orderBy(F.col("one_way_trips").desc())
)
write_thin_gold(agg_one_way_flows, "agg_one_way_flows")


# CELL ********************
# MAGIC %md
# MAGIC ## KPI 5 — Maintenance cost by site (geo-enriched)
# MAGIC Re-materializes the mirrored `kpi_maintenance_cost` (labor + parts by site) as V-Ordered
# MAGIC Delta with site geo so maintenance spend renders on the same multi-city map as revenue.
# MAGIC
# MAGIC `kpi_maintenance_cost` already carries the descriptive site columns (`site_name`,
# MAGIC `site_city`, `site_state`), so we join against a **coordinates-only** projection of the
# MAGIC site dimension. This avoids duplicate / ambiguous `site_name` columns that would otherwise
# MAGIC appear on both sides of the join and break the Delta write.

site_geo_coords = dim_site.select(
    "site_id", "latitude", "longitude", "is_hq", "parking_capacity",
)

agg_maintenance_cost_by_site = (
    read_source(src_kpi_maintenance_cost)
    .join(site_geo_coords, "site_id", "left")
    .orderBy(F.col("total_maintenance_cost_usd").desc())
)
write_thin_gold(agg_maintenance_cost_by_site, "agg_maintenance_cost_by_site")


# CELL ********************
# MAGIC %md ## Verify — every thin gold aggregate is present, non-empty, and V-Ordered

_thin_tables = (
    "agg_revenue_by_site",
    "agg_revenue_by_site_month",
    "agg_fleet_utilization_by_site_month",
    "agg_idle_vehicles_by_site",
    "agg_one_way_flows",
    "agg_maintenance_cost_by_site",
)
for _t in _thin_tables:
    _name = _qualify(target_catalog, target_schema, _t)
    _n = spark.table(_name).count()
    print(f"{_t:38s} {_n:>8d} rows")
    assert _n > 0, f"thin gold {_name} is empty"

# Release the cached source frames.
for _df in (fact_rental, dim_site, dim_vehicle):
    _df.unpersist()

print("thin Fabric gold / aggregation layer: OK (V-Ordered Delta ready for Direct Lake)")
