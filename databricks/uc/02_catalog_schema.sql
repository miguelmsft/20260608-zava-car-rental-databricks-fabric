-- =====================================================================================
-- Zava — Unity Catalog setup (2 of 4): create the catalog + medallion schemas
-- =====================================================================================
-- Purpose : Create the `zava` Unity Catalog catalog and the four medallion schemas
--           (raw / bronze / silver / gold) as standard UC objects. Gold tables written
--           into these schemas are managed Delta tables — the format that IS mirrorable
--           into Microsoft Fabric (R1 §"Supported objects": managed Delta = supported;
--           streaming tables / materialized views / views = NOT supported).
--
-- Parameterization (R8, repo convention "no hardcoded names"):
--   * `catalog_name`  must match  databricks_config.catalog          (Step 1 config schema; default "zava")
--   * `managed_acct`  must match  Bicep output managedStorageAccountName (Step 6; default "<prefix>uc")
--   * `uc_container`  must match  Bicep param ucContainerName         (Step 6; default "unitycatalog")
--   Override the DECLAREd defaults below (or set them from the deploy orchestrator,
--   Step 23) so this script is reusable across environments.
--
-- Idempotent : every statement uses IF NOT EXISTS — safe to re-run.
-- Run by     : a metastore admin / catalog owner on a UC-enabled warehouse or cluster.
-- =====================================================================================

-- ---- Parameters (session variables; IDENTIFIER() turns them into object names) -------
-- The catalog + schema names are supplied by the Databricks job/bundle SQL task
-- (`sql_task.parameters` in databricks.yml) as named parameter markers (`:catalog`,
-- `:raw_schema`, ...). The DECLARE defaults keep this script runnable on their own in the
-- SQL editor; the SET VARIABLE statements override them with the bundle/task values so
-- dev/prod target exactly the catalog chosen by `${var.catalog}` (e.g. dev = zava_dev).
-- For a manual editor run, either supply the parameters or comment out the SET lines.
DECLARE OR REPLACE VARIABLE catalog_name STRING DEFAULT 'zava';
DECLARE OR REPLACE VARIABLE raw_schema    STRING DEFAULT 'raw';
DECLARE OR REPLACE VARIABLE bronze_schema STRING DEFAULT 'bronze';
DECLARE OR REPLACE VARIABLE silver_schema STRING DEFAULT 'silver';
DECLARE OR REPLACE VARIABLE gold_schema   STRING DEFAULT 'gold';
DECLARE OR REPLACE VARIABLE managed_acct STRING DEFAULT 'zavauc';
DECLARE OR REPLACE VARIABLE uc_container STRING DEFAULT 'unitycatalog';

SET VARIABLE catalog_name  = :catalog;
SET VARIABLE raw_schema    = :raw_schema;
SET VARIABLE bronze_schema = :bronze_schema;
SET VARIABLE silver_schema = :silver_schema;
SET VARIABLE gold_schema   = :gold_schema;

-- ---- Catalog -------------------------------------------------------------------------
-- No explicit MANAGED LOCATION: the catalog inherits the metastore's managed storage,
-- which keeps the demo simple and avoids needing a storage credential + external
-- location (those are CLI/Terraform-only objects, not SQL DDL — R8 §3.4).
--
-- OPTIONAL (advanced): to pin the catalog to the Step-6 ADLS Gen2 account, first create
-- a storage credential + external location (CLI/Terraform), then uncomment:
--   CREATE CATALOG IF NOT EXISTS IDENTIFIER(catalog_name)
--   MANAGED LOCATION 'abfss://' || uc_container || '@' || managed_acct || '.dfs.core.windows.net/' || catalog_name
--   COMMENT 'Zava car-rental analytics catalog (managed Delta; mirrorable to Fabric).';
CREATE CATALOG IF NOT EXISTS IDENTIFIER(catalog_name)
COMMENT 'Zava car-rental analytics catalog. Source of the certified gold data asset mirrored into Microsoft Fabric (Variation 1).';

-- Set the active catalog from the parameter, so the schema DDL below uses bare,
-- unambiguous names (avoids multi-part IDENTIFIER concatenation).
USE CATALOG IDENTIFIER(catalog_name);

-- ---- Medallion schemas ---------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS IDENTIFIER(raw_schema)
COMMENT 'RAW landing zone: Step-3 synthetic Zava entities loaded as-is (managed Delta).';

CREATE SCHEMA IF NOT EXISTS IDENTIFIER(bronze_schema)
COMMENT 'BRONZE: raw entities + ingestion audit columns (managed Delta).';

CREATE SCHEMA IF NOT EXISTS IDENTIFIER(silver_schema)
COMMENT 'SILVER: cleaned, typed, de-duplicated, referentially-valid entities (managed Delta).';

CREATE SCHEMA IF NOT EXISTS IDENTIFIER(gold_schema)
COMMENT 'GOLD: business-ready, certified dims/facts/KPIs. Standard managed Delta — mirrored into Fabric OneLake.';

-- VERIFY: confirm the four medallion schemas exist in the active catalog.
SHOW SCHEMAS;
