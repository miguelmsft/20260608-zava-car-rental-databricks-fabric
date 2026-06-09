-- =====================================================================================
-- Zava — Unity Catalog setup (3 of 4): grants + Fabric MIRRORING prerequisites
-- =====================================================================================
-- Purpose : Grant the privileges that (a) let the data-engineering identity build the
--           medallion and (b) let the Microsoft Fabric connector identity MIRROR the
--           certified gold tables (Variation 1).
--
-- The Fabric mirroring prerequisite grants are (R8 §3.5/§8.2, R1 prerequisites #6/#7):
--     USE CATALOG + USE SCHEMA + SELECT        -> read the objects
--     EXTERNAL USE SCHEMA                       -> request temporary credentials so the
--                                                  external engine (Fabric) can read the
--                                                  underlying Delta files (credential vending)
--
--   "The principal who requests the temporary credential must have: The EXTERNAL USE
--    SCHEMA privilege on the containing schema or its parent catalog. [...] SELECT
--    permission on the table, USE CATALOG on its parent catalog, and USE SCHEMA on its
--    parent schema."  — https://learn.microsoft.com/en-us/azure/databricks/external-access/admin
--
--   EXTERNAL USE SCHEMA must be granted EXPLICITLY by the catalog owner; it is NOT part
--   of ALL PRIVILEGES and schema owners do not get it by default (anti-exfiltration).
--
-- Parameterization : replace the principal placeholders below with the real identities
--   from your environment (Step 2 sets up the service principals / groups). These are
--   account-level names that cannot be defaulted — they are CLEARLY marked <...>.
--   `catalog_name` must match databricks_config.catalog (Step 1; default "zava").
--
-- Idempotent : GRANT is additive and safe to re-run. EXECUTE IMMEDIATE is used so the
--   catalog name stays parameterized while the SQL remains valid Databricks UC SQL.
-- Run by     : the catalog OWNER (required to grant EXTERNAL USE SCHEMA).
-- =====================================================================================

-- ---- Parameters ----------------------------------------------------------------------
-- catalog + schema names come from the bundle/job SQL task (`sql_task.parameters`) as
-- named parameter markers; DECLARE defaults keep this runnable standalone in the editor.
DECLARE OR REPLACE VARIABLE catalog_name STRING DEFAULT 'zava';
DECLARE OR REPLACE VARIABLE raw_schema    STRING DEFAULT 'raw';
DECLARE OR REPLACE VARIABLE bronze_schema STRING DEFAULT 'bronze';
DECLARE OR REPLACE VARIABLE silver_schema STRING DEFAULT 'silver';
DECLARE OR REPLACE VARIABLE gold_schema   STRING DEFAULT 'gold';

SET VARIABLE catalog_name  = :catalog;
SET VARIABLE raw_schema    = :raw_schema;
SET VARIABLE bronze_schema = :bronze_schema;
SET VARIABLE silver_schema = :silver_schema;
SET VARIABLE gold_schema   = :gold_schema;

-- Principal placeholders — REPLACE with real values before running (Step 2):
--   fabric_identity  : the service principal / managed identity Fabric uses to mirror
--   engineers_sp     : the data-engineering pipeline identity (runs the bundle job)
--   analysts_group   : read-only business consumers of gold
--   stewards_group   : owners/data stewards of the certified gold asset
DECLARE OR REPLACE VARIABLE fabric_identity STRING DEFAULT '<fabric-connector-identity>';
DECLARE OR REPLACE VARIABLE engineers_sp   STRING DEFAULT '<zava-data-engineering-sp>';
DECLARE OR REPLACE VARIABLE analysts_group STRING DEFAULT '<zava-analysts>';
DECLARE OR REPLACE VARIABLE stewards_group STRING DEFAULT '<zava-data-stewards>';

-- =====================================================================================
-- A) Data-engineering identity — build the medallion (create/write tables in all layers)
-- =====================================================================================
EXECUTE IMMEDIATE 'GRANT USE CATALOG ON CATALOG ' || catalog_name || ' TO `' || engineers_sp || '`';
EXECUTE IMMEDIATE 'GRANT USE SCHEMA ON CATALOG '  || catalog_name || ' TO `' || engineers_sp || '`';
EXECUTE IMMEDIATE 'GRANT CREATE SCHEMA ON CATALOG ' || catalog_name || ' TO `' || engineers_sp || '`';
EXECUTE IMMEDIATE 'GRANT CREATE TABLE ON SCHEMA ' || catalog_name || '.' || raw_schema    || ' TO `' || engineers_sp || '`';
EXECUTE IMMEDIATE 'GRANT CREATE TABLE ON SCHEMA ' || catalog_name || '.' || bronze_schema || ' TO `' || engineers_sp || '`';
EXECUTE IMMEDIATE 'GRANT CREATE TABLE ON SCHEMA ' || catalog_name || '.' || silver_schema || ' TO `' || engineers_sp || '`';
EXECUTE IMMEDIATE 'GRANT CREATE TABLE ON SCHEMA ' || catalog_name || '.' || gold_schema   || ' TO `' || engineers_sp || '`';
EXECUTE IMMEDIATE 'GRANT MODIFY ON SCHEMA '       || catalog_name || '.' || gold_schema   || ' TO `' || engineers_sp || '`';

-- =====================================================================================
-- B) Business analysts — read-only on the certified gold layer
-- =====================================================================================
EXECUTE IMMEDIATE 'GRANT USE CATALOG ON CATALOG ' || catalog_name || ' TO `' || analysts_group || '`';
EXECUTE IMMEDIATE 'GRANT USE SCHEMA ON SCHEMA '   || catalog_name || '.' || gold_schema || ' TO `' || analysts_group || '`';
EXECUTE IMMEDIATE 'GRANT SELECT ON SCHEMA '       || catalog_name || '.' || gold_schema || ' TO `' || analysts_group || '`';

-- =====================================================================================
-- C) FABRIC MIRRORING PREREQUISITE GRANTS  (Variation 1) — the critical part
-- =====================================================================================
-- USE CATALOG + USE SCHEMA + SELECT on the gold schema Fabric will mirror:
EXECUTE IMMEDIATE 'GRANT USE CATALOG ON CATALOG ' || catalog_name || ' TO `' || fabric_identity || '`';
EXECUTE IMMEDIATE 'GRANT USE SCHEMA ON SCHEMA '   || catalog_name || '.' || gold_schema || ' TO `' || fabric_identity || '`';
EXECUTE IMMEDIATE 'GRANT SELECT ON SCHEMA '       || catalog_name || '.' || gold_schema || ' TO `' || fabric_identity || '`';

-- EXTERNAL USE SCHEMA — credential vending for the external engine (Fabric). Granted at
-- the CATALOG level so it covers the gold schema (and any future mirrored schema). Must
-- be granted by the catalog owner; not included in ALL PRIVILEGES.
EXECUTE IMMEDIATE 'GRANT EXTERNAL USE SCHEMA ON CATALOG ' || catalog_name || ' TO `' || fabric_identity || '`';

-- =====================================================================================
-- VERIFY — these should show the grants above (esp. EXTERNAL USE SCHEMA for mirroring)
-- =====================================================================================
EXECUTE IMMEDIATE 'SHOW GRANTS ON CATALOG ' || catalog_name;
EXECUTE IMMEDIATE 'SHOW GRANTS ON SCHEMA '  || catalog_name || '.' || gold_schema;
