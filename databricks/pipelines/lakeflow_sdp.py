# =====================================================================================
# Zava — Lakeflow Spark Declarative Pipeline (Variation 2 SOURCE)
# =====================================================================================
# Purpose : Author a SIMPLE Lakeflow (Spark Declarative Pipelines) pipeline that produces
#           CURATED Zava data as Unity Catalog **managed** objects:
#             * a MATERIALIZED VIEW  `<curated_schema>.rentals_curated`   (rentals + site city)
#             * a STREAMING TABLE    `<curated_schema>.telematics_curated` (vehicle telemetry)
#
#           These outputs are the **Variation 2** source. Per R10, streaming tables and
#           materialized views are, by definition, **Unity Catalog managed tables** whose
#           files live in UC-governed managed storage. Crucially:
#
#             *** Streaming tables / materialized views CANNOT be mirrored into Fabric ***
#                 ("Streaming tables" and "Views, Materialized views" are explicitly listed
#                  as unsupported table types for Azure Databricks mirroring — R10 §2.3).
#
#           That is exactly why Variation 2 exists: instead of mirroring, we shortcut the
#           Databricks-managed ADLS Gen2 storage that backs these tables (sub-pattern 2A),
#           OR write to a dedicated external location via a Lakeflow sink (sub-pattern 2B,
#           see the companion lakeflow_sink_external.py). The managed-storage abfss:// path
#           for sub-pattern 2A is discovered at run time with `DESCRIBE DETAIL` — the
#           procedure is documented in docs/runbook-end-to-end.md (Step-12 shortcut target).
#
# How it runs : as a Lakeflow pipeline (NOT a normal notebook/job). The pipeline's target
#           CATALOG + SCHEMA are set in the bundle `resources.pipelines` entry
#           (databricks/bundle/databricks.yml). Source catalog / silver schema are passed
#           via pipeline `configuration` (Spark conf) for reusability — no hardcoded
#           resource names (repo convention). Defaults keep this importable / AST-parseable
#           standalone.
#
# Public repo: NO secrets. Auth is via the bundle / `databricks auth` at runtime.
# =====================================================================================

import pyspark.pipelines as dp
from pyspark.sql import functions as F

# -------------------------------------------------------------------------------------
# Parameterization (Spark conf set by the pipeline `configuration` block in the bundle).
# `spark` is injected by the Lakeflow runtime. Defaults mirror the medallion schemas from
# Step 8 so the file is runnable/parseable without the bundle context.
# -------------------------------------------------------------------------------------
SOURCE_CATALOG = spark.conf.get("zava.source_catalog", "zava")
SILVER_SCHEMA = spark.conf.get("zava.silver_schema", "silver")


def _silver(table: str) -> str:
    """Fully-qualified name of a Step-8 silver (curated) source table."""
    return f"{SOURCE_CATALOG}.{SILVER_SCHEMA}.{table}"


# =====================================================================================
# 1) MATERIALIZED VIEW — rentals_curated
# -------------------------------------------------------------------------------------
# Curated rental fact enriched with the pickup-site CITY. `site_city` is the column the
# Step-9 UC ROW FILTER (05_access_policies.sql) keys on (a site manager sees only their
# city's rentals) and the natural shortcut/Direct-Lake reporting grain. Materialized as a
# UC managed table => NOT mirrorable => Variation-2 source.
# =====================================================================================
@dp.materialized_view(
    name="rentals_curated",
    comment="Curated Zava rentals enriched with pickup-site city. UC MANAGED (not mirrorable) — Variation-2 shortcut source.",
    table_properties={"quality": "curated", "zava.variation": "2"},
)
def rentals_curated():
    rentals = spark.read.table(_silver("rentals"))
    sites = spark.read.table(_silver("sites")).select(
        F.col("site_id").alias("_site_id"),
        F.col("city").alias("site_city"),
        F.col("state").alias("site_state"),
    )
    return (
        rentals.join(sites, rentals["pickup_site_id"] == F.col("_site_id"), "left")
        .drop("_site_id")
        .select(
            "rental_id",
            "vehicle_id",
            "customer_id",
            "pickup_site_id",
            "return_site_id",
            "site_city",
            "site_state",
            "pickup_ts",
            "return_ts",
            "miles_driven",
            "rental_days",
            "is_one_way",
            "status",
        )
    )


# =====================================================================================
# 2) STREAMING TABLE — telematics_curated
# -------------------------------------------------------------------------------------
# Continuously-curated vehicle telemetry (a STREAMING TABLE, the second managed output
# type). Demonstrates the streaming half of "streaming tables / materialized views". Also
# a UC managed table => not mirrorable => Variation-2 source. `spark.readStream` makes this
# an incremental flow over the silver telematics table.
# =====================================================================================
@dp.table(
    name="telematics_curated",
    comment="Curated Zava vehicle telemetry stream. UC MANAGED streaming table (not mirrorable) — Variation-2 shortcut source.",
    table_properties={"quality": "curated", "zava.variation": "2"},
)
def telematics_curated():
    return spark.readStream.table(_silver("telematics")).select(
        "telematics_id",
        "vehicle_id",
        "snapshot_ts",
        "latitude",
        "longitude",
        "speed_mph",
        "odometer_miles",
        "idle_minutes",
        "fuel_or_soc_pct",
    )
