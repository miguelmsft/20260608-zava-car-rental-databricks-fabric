-- =====================================================================================
-- Zava — Unity Catalog ACCESS POLICIES (Step 9): row filter + column mask
-- =====================================================================================
-- Purpose : Define the Unity Catalog access policies that Policy Weaver (Step 21, R5) later
--           SYNCS into Fabric OneLake Security:
--             * ROW FILTER  — a Zava SITE MANAGER sees only their city's rentals.
--             * COLUMN MASK — customer PII (email) is masked for non-authorized principals.
--
--           These are authored against the CURATED Variation-2 outputs from Step 9
--           (the Lakeflow materialized view `<curated_schema>.rentals_curated`, which
--           carries `site_city`) and the certified GOLD `dim_customer` (which carries the
--           `email` PII column). Both are the policy SOURCE that R5's Policy Weaver maps to
--           Fabric OneLake Security data-access roles.
--
--   POLICY-WEAVER-COMPATIBLE PATTERNS (R5 §7/§8 — keep these SUPPORTED forms):
--     * Row filter : CASE WHEN IS_ACCOUNT_GROUP_MEMBER('grp') THEN true
--                         WHEN IS_ACCOUNT_GROUP_MEMBER('grp') THEN col = 'literal'
--                         ELSE false END
--                    -> Policy Weaver emits `SELECT * FROM {schema}.{table} WHERE col='...'`.
--     * Column mask: CASE WHEN IS_ACCOUNT_GROUP_MEMBER('grp') THEN col ELSE '<mask>' END
--                    -> Policy Weaver maps to binary include/exclude of the column.
--     Avoid subqueries / joins / current_user() / partial-hash masks — those fall back to
--     the configured `deny` and are NOT faithfully synced (R5 §7).
--
--   GOVERNANCE NOTE (R10 §5.1): these UC policies are enforced for Databricks queries and
--   (via Policy Weaver) for Fabric MIRRORED data (Variation 1). They are NOT enforced at the
--   storage layer for a direct-storage OneLake SHORTCUT (Variation 2) — re-enforce there via
--   OneLake security + storage RBAC. Authoring them here keeps the demo's governance story
--   consistent and gives Policy Weaver real source policies to weave.
--
-- Parameterization : catalog + schema names come from the bundle/job SQL task parameters as
--   named markers (:catalog, ...). DECLARE defaults keep this runnable standalone. Group
--   placeholders are account-level names that cannot be defaulted — REPLACE before running
--   (Step 2 provisions the groups). NO secrets.
--
-- Idempotent : CREATE OR REPLACE FUNCTION + SET ROW FILTER / SET MASK overwrite prior
--   definitions. DROP ... IF EXISTS guards re-apply. Safe to re-run.
-- Run by     : the OWNER of the target tables/schemas (required to set filters/masks).
-- =====================================================================================

-- ---- Parameters ----------------------------------------------------------------------
DECLARE OR REPLACE VARIABLE catalog_name   STRING DEFAULT 'zava';
DECLARE OR REPLACE VARIABLE curated_schema STRING DEFAULT 'curated';   -- Lakeflow target (Step 9)
DECLARE OR REPLACE VARIABLE gold_schema    STRING DEFAULT 'gold';      -- certified dims (Step 8)

SET VARIABLE catalog_name   = :catalog;
SET VARIABLE curated_schema = :curated_schema;
SET VARIABLE gold_schema    = :gold_schema;

-- Group placeholders — REPLACE with real account groups (Step 2). The per-city manager
-- groups + an all-sites admin group implement the row filter; the PII-authorized group
-- implements the column mask. These exact group names become Policy Weaver role sources.
DECLARE OR REPLACE VARIABLE grp_all_sites STRING DEFAULT 'zava_all_sites_admin';
DECLARE OR REPLACE VARIABLE grp_seattle   STRING DEFAULT 'zava_seattle_mgr';
DECLARE OR REPLACE VARIABLE grp_portland  STRING DEFAULT 'zava_portland_mgr';
DECLARE OR REPLACE VARIABLE grp_denver    STRING DEFAULT 'zava_denver_mgr';
DECLARE OR REPLACE VARIABLE grp_pii       STRING DEFAULT 'zava_pii_authorized';

-- =====================================================================================
-- 1) ROW FILTER — site managers see only their city's rentals
-- -------------------------------------------------------------------------------------
-- Function returns TRUE for an all-sites admin (no filter), restricts each city manager to
-- their own `site_city`, and denies everyone else. Applied to the curated rentals MV ON its
-- `site_city` column. Policy Weaver maps each city branch to a Fabric row-constraint role.
-- =====================================================================================
EXECUTE IMMEDIATE
  'CREATE OR REPLACE FUNCTION ' || catalog_name || '.' || curated_schema || '.rentals_site_filter(site_city STRING) '
  || 'RETURN CASE '
  || '  WHEN IS_ACCOUNT_GROUP_MEMBER(''' || grp_all_sites || ''') THEN true '
  || '  WHEN IS_ACCOUNT_GROUP_MEMBER(''' || grp_seattle   || ''') THEN site_city = ''Seattle'' '
  || '  WHEN IS_ACCOUNT_GROUP_MEMBER(''' || grp_portland  || ''') THEN site_city = ''Portland'' '
  || '  WHEN IS_ACCOUNT_GROUP_MEMBER(''' || grp_denver    || ''') THEN site_city = ''Denver'' '
  || '  ELSE false END';

-- Drop any existing filter first (re-apply safety), then attach the filter to site_city.
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || curated_schema || '.rentals_curated DROP ROW FILTER';
EXECUTE IMMEDIATE
  'ALTER TABLE ' || catalog_name || '.' || curated_schema || '.rentals_curated '
  || 'SET ROW FILTER ' || catalog_name || '.' || curated_schema || '.rentals_site_filter ON (site_city)';

-- =====================================================================================
-- 2) COLUMN MASK — mask customer PII (email) for non-authorized principals
-- -------------------------------------------------------------------------------------
-- Function returns the real email only for the PII-authorized group; everyone else sees a
-- fixed mask literal. Applied to gold.dim_customer.email. Policy Weaver maps this to binary
-- include/exclude of the email column in Fabric OneLake Security (R5 §8 fidelity note).
-- =====================================================================================
EXECUTE IMMEDIATE
  'CREATE OR REPLACE FUNCTION ' || catalog_name || '.' || gold_schema || '.mask_email(email STRING) '
  || 'RETURN CASE '
  || '  WHEN IS_ACCOUNT_GROUP_MEMBER(''' || grp_pii || ''') THEN email '
  || '  ELSE ''***@masked.zava'' END';

-- Drop any existing mask first (re-apply safety), then attach the mask to the email column.
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_customer ALTER COLUMN email DROP MASK';
EXECUTE IMMEDIATE
  'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_customer '
  || 'ALTER COLUMN email SET MASK ' || catalog_name || '.' || gold_schema || '.mask_email';

-- =====================================================================================
-- VERIFY — confirm the policies are attached.
--   * As a restricted principal (e.g., a Seattle-manager group member), the row filter
--     returns only Seattle rows; as a non-PII principal the email shows the mask literal.
-- =====================================================================================
EXECUTE IMMEDIATE 'DESCRIBE EXTENDED ' || catalog_name || '.' || curated_schema || '.rentals_curated';
EXECUTE IMMEDIATE 'DESCRIBE EXTENDED ' || catalog_name || '.' || gold_schema || '.dim_customer';
