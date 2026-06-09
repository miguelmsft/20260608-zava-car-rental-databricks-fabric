-- =====================================================================================
-- Zava — Unity Catalog setup (1 of 4): enable EXTERNAL DATA ACCESS on the metastore
-- =====================================================================================
-- Purpose : Document + verify the metastore-level prerequisite that lets external
--           engines (Microsoft Fabric mirroring, Variation 1) read Unity Catalog
--           Delta tables via credential vending. This is the FIRST mirroring
--           prerequisite (R8 §8.3, R1 prerequisites #6).
--
-- IMPORTANT — this step is NOT pure SQL/Bicep:
--   "To allow external engines to access data in a metastore, a metastore admin must
--    enable external data access for the metastore. This option is disabled by default
--    to prevent unauthorized external access."
--    — https://learn.microsoft.com/en-us/azure/databricks/external-access/admin
--
--   There is NO `CREATE`/`ALTER METASTORE ... EXTERNAL DATA ACCESS` SQL statement and no
--   Bicep/Terraform resource for this toggle. It is enabled via the account/Catalog UI
--   or the Unity Catalog REST API. The repo end-to-end orchestrator (Step 23) automates
--   this with the REST call below; for a manual run, follow the UI steps.
--
-- Run order : 01 (this file) -> 02_catalog_schema -> 03_grants_mirroring -> 04_certify_gold
-- Runtime   : Databricks SQL warehouse or a UC-enabled cluster, run by a METASTORE ADMIN.
-- Public repo: NO secrets. Tokens are obtained at runtime (az login / Key Vault).
-- =====================================================================================

-- -------------------------------------------------------------------------------------
-- MANUAL / REST step (run ONCE per metastore by a metastore admin) — see Step 23 + docs:
-- -------------------------------------------------------------------------------------
--   UI:
--     1. Workspace -> Catalog (sidebar) -> gear icon -> Metastore
--     2. Details tab -> enable "External data access"
--
--   REST API (automatable — no secret committed; bearer token acquired at runtime):
--     PATCH https://<account-or-workspace-host>/api/2.1/unity-catalog/metastores/<metastore-id>
--       Header : Authorization: Bearer <RUNTIME_TOKEN>   # from `az`/`databricks auth`/Key Vault
--       Body   : { "external_access_enabled": true }
--
--   Databricks CLI (illustrative — verify flag against your installed CLI version, R8 §8.3):
--     databricks metastores update <metastore-id> --json '{"external_access_enabled": true}'
-- -------------------------------------------------------------------------------------

-- VERIFY (SQL we CAN run): confirm which metastore the workspace is attached to, so the
-- operator knows exactly which metastore id to toggle / verify above.
SELECT current_metastore() AS metastore_id;

-- VERIFY: list visible catalogs (confirms UC is enabled and the warehouse/cluster can
-- reach the metastore). The `zava` catalog itself is created in 02_catalog_schema.sql.
SHOW CATALOGS;

-- NOTE: external-data-access enablement state is not exposed as a SQL column. Confirm it
-- in the Metastore "Details" tab (UI) or via a GET on the REST endpoint above. The
-- functional end-to-end proof is Step 11 (Fabric "Mirrored Azure Databricks Catalog"
-- successfully reads zava.gold.* tables).
