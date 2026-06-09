# =====================================================================================
# Zava — Lakeflow SINK to an EXTERNAL Delta location (Variation 2, sub-pattern 2B)
# =====================================================================================
# Purpose : The cleaner-governance ALTERNATIVE to shortcutting Databricks-managed storage
#           directly (sub-pattern 2A, lakeflow_sdp.py). Per R10 §5.1, sub-pattern 2B lands
#           the curated output on a **dedicated EXTERNAL location you own**, giving the
#           OneLake shortcut a stable, non-hashed abfss:// path and a cleaner governance
#           boundary.
#
#   WHY a sink at all (R10 §3 / §5.3 anti-pattern):
#     A managed streaming table / materialized view is NEVER auto-exported to an external
#     path. An external path is only populated by a **real write mechanism**. The documented
#     Lakeflow approach is a **Delta sink** (`dp.create_sink(format="delta", ...)`) fed by an
#     **append flow** (`@dp.append_flow(target=...)`). For UC managed/external tables the
#     sink uses `format="delta"` and addresses the target by `path` OR `tableName`.
#
#   SINK LIMITATIONS (R10 §5.1 — be explicit):
#     Lakeflow sinks are append-only, Python-only, streaming-only. A full-refresh update
#     does NOT clean previously computed data in the sink. Provide the external location +
#     external table (or path) as a stable, owned target (see 05_access_policies.sql notes
#     and the SQL block at the bottom of this file).
#
#   GOVERNANCE NOTE (R10 §5.1, applies to BOTH 2A and 2B):
#     A direct-storage OneLake shortcut bypasses UC RLS/CLM/ABAC enforcement at the storage
#     layer ("Unity Catalog privileges are not enforced when users access data files from
#     external systems"). Re-enforce access in Fabric (OneLake security / workspace
#     permissions) and storage-account RBAC. 2B's advantage over 2A is path STABILITY and a
#     decoupled lifecycle, not UC-policy propagation.
#
# How it runs : as part of the SAME Lakeflow pipeline as lakeflow_sdp.py (add this file to
#           the pipeline's libraries when 2B is desired), or as its own pipeline. The target
#           external path / external-table name are passed via pipeline `configuration`
#           (Spark conf) for reusability — NO hardcoded storage account (repo convention,
#           NO secrets). Defaults keep this file importable / AST-parseable standalone.
#
# Public repo: NO secrets. The abfss:// default below is a clearly-labelled PLACEHOLDER.
# =====================================================================================

import pyspark.pipelines as dp

# -------------------------------------------------------------------------------------
# Parameterization (Spark conf set by the pipeline `configuration` block in the bundle).
# `spark` is injected by the Lakeflow runtime. Provide EITHER an external table name OR a
# raw abfss:// path; this file defaults to the external-table form (cleanest governance,
# matches the pre-created external table in the SQL block below).
# -------------------------------------------------------------------------------------
SOURCE_CATALOG = spark.conf.get("zava.source_catalog", "zava")
SILVER_SCHEMA = spark.conf.get("zava.silver_schema", "silver")
CURATED_CATALOG = spark.conf.get("zava.curated_catalog", "zava")
CURATED_SCHEMA = spark.conf.get("zava.curated_schema", "curated")

# Fully-qualified EXTERNAL table that backs the owned abfss:// path (pre-created by the SQL
# block below). Override via `zava.external_table` to point at your own external location.
EXTERNAL_TABLE = spark.conf.get(
    "zava.external_table", f"{CURATED_CATALOG}.{CURATED_SCHEMA}.rentals_curated_ext"
)

# Source curated rentals (the silver rentals table; same upstream as the 2A materialized
# view). Streaming-only: sinks require a streaming source.
SOURCE_TABLE = f"{SOURCE_CATALOG}.{SILVER_SCHEMA}.rentals"

# -------------------------------------------------------------------------------------
# (i) Create the Delta SINK targeting the EXTERNAL table by fully-qualified name.
#     For UC managed/external tables: format="delta" + options {tableName | path}.
# -------------------------------------------------------------------------------------
dp.create_sink(
    name="rentals_curated_external_sink",
    format="delta",
    options={"tableName": EXTERNAL_TABLE},
)


# -------------------------------------------------------------------------------------
# (ii) APPEND FLOW — stream curated records into the sink. Append-only (see Limitations).
# -------------------------------------------------------------------------------------
@dp.append_flow(
    name="rentals_curated_external_flow",
    target="rentals_curated_external_sink",
)
def rentals_curated_external_flow():
    return spark.readStream.table(SOURCE_TABLE)


# =====================================================================================
# Pre-create the EXTERNAL LOCATION + EXTERNAL TABLE (the clean, stable shortcut target).
# Run this ONCE in a Databricks SQL editor BEFORE enabling the sink above. Requires a
# storage credential + external location on the owned ADLS Gen2 container (R10 §3). The
# abfss:// URI is a PLACEHOLDER — replace <storage-account>/<container> with your own; do
# NOT commit a real account. NO secrets.
#
#   -- One-time: external location must already point at the owned container/credential.
#   CREATE TABLE IF NOT EXISTS zava.curated.rentals_curated_ext (
#     rental_id      STRING,
#     vehicle_id     STRING,
#     customer_id    STRING,
#     pickup_site_id STRING,
#     return_site_id STRING,
#     pickup_ts      TIMESTAMP,
#     return_ts      TIMESTAMP,
#     miles_driven   BIGINT,
#     rental_days    INT,
#     is_one_way     BOOLEAN,
#     status         STRING
#   )
#   LOCATION 'abfss://curated@<storage-account>.dfs.core.windows.net/rentals_curated';
#
# Discover the resulting stable path for the Step-12 OneLake shortcut with:
#   DESCRIBE DETAIL zava.curated.rentals_curated_ext;   -- 'location' column
# (See docs/runbook-end-to-end.md.) Because this is an OWNED external path, it is NOT under
# __unitystorage and does NOT drift the way managed storage does (2A) — that is 2B's point.
#
# ALTERNATIVE to a sink (also valid, R10 §4 Step 1B note): a separate non-declarative job /
# CTAS (CREATE TABLE ... AS SELECT ... LOCATION 'abfss://...') can populate the same owned
# path while the declarative pipeline keeps producing the managed table. Choose the sink for
# a single declarative pipeline; choose the downstream job to fully decouple lifecycle.
# =====================================================================================
