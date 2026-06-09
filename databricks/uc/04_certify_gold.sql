-- =====================================================================================
-- Zava — Unity Catalog setup (4 of 4): CERTIFY the gold data asset
-- =====================================================================================
-- Purpose : Mark the gold-layer tables as a CERTIFIED data asset using Unity Catalog
--           tags + comments + ownership (R8 §6). Unity Catalog has no first-class
--           "certified" flag, so the recommended pattern is:
--             * tags    : certification_status / certified_by / certified_date / data_layer
--             * comment : human-readable certification statement
--             * owner   : transfer to the data-stewards group
--
--   Tag syntax used here is `ALTER TABLE ... SET TAGS (...)` (Databricks Runtime 13.3+).
--   On Runtime 16.1+ the newer `SET TAG ON <object>` syntax also works (R8 §6.1).
--
-- IMPORTANT — certification metadata does NOT auto-propagate to Fabric:
--   "Tags, comments, and ownership are NOT listed as synced metadata" for the mirrored
--   catalog (R8 §6.3). They are governance signals inside Unity Catalog (Catalog
--   Explorer / INFORMATION_SCHEMA / UC REST API) and are surfaced to Fabric/Power BI
--   either via Purview (Step 22) OR via the mirrorable registry table created at the
--   bottom of this script (`gold._table_certifications`), which IS a managed Delta table
--   and therefore visible after mirroring.
--
-- Run AFTER : the gold notebook (30_gold.py) has created the gold tables.
-- Idempotent: SET TAGS / COMMENT ON / OWNER TO overwrite prior values; the registry
--   table uses CREATE OR REPLACE. Safe to re-run.
-- =====================================================================================

-- ---- Parameters ----------------------------------------------------------------------
-- catalog + gold schema come from the bundle/job SQL task (`sql_task.parameters`) as
-- named parameter markers; DECLARE defaults keep this runnable standalone in the editor.
DECLARE OR REPLACE VARIABLE catalog_name   STRING DEFAULT 'zava';
DECLARE OR REPLACE VARIABLE gold_schema    STRING DEFAULT 'gold';
DECLARE OR REPLACE VARIABLE certified_by   STRING DEFAULT 'zava-data-engineering';
DECLARE OR REPLACE VARIABLE certified_date STRING DEFAULT '2026-06-08';  -- set by deploy orchestrator (Step 23)
DECLARE OR REPLACE VARIABLE stewards_group STRING DEFAULT '<zava-data-stewards>';  -- REPLACE (Step 2)
DECLARE OR REPLACE VARIABLE cert_comment   STRING DEFAULT
  'CERTIFIED gold-layer table. Validated, de-duplicated, referentially consistent, and approved for downstream consumption including Microsoft Fabric mirroring and Power BI Direct Lake.';

SET VARIABLE catalog_name = :catalog;
SET VARIABLE gold_schema  = :gold_schema;

-- =====================================================================================
-- dim_site
-- =====================================================================================
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_site SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', '
  || '''certified_by'' = ''' || certified_by || ''', '
  || '''certified_date'' = ''' || certified_date || ''', '
  || '''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.dim_site IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_site OWNER TO `' || stewards_group || '`';

-- =====================================================================================
-- dim_vehicle
-- =====================================================================================
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_vehicle SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', '
  || '''certified_by'' = ''' || certified_by || ''', '
  || '''certified_date'' = ''' || certified_date || ''', '
  || '''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.dim_vehicle IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_vehicle OWNER TO `' || stewards_group || '`';

-- =====================================================================================
-- dim_customer  (contains synthetic PII-like columns; column masks added in Step 9)
-- =====================================================================================
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_customer SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', '
  || '''certified_by'' = ''' || certified_by || ''', '
  || '''certified_date'' = ''' || certified_date || ''', '
  || '''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.dim_customer IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.dim_customer OWNER TO `' || stewards_group || '`';

-- =====================================================================================
-- fact_rental  (central certified fact: rentals enriched with revenue + dims)
-- =====================================================================================
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.fact_rental SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', '
  || '''certified_by'' = ''' || certified_by || ''', '
  || '''certified_date'' = ''' || certified_date || ''', '
  || '''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.fact_rental IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.fact_rental OWNER TO `' || stewards_group || '`';

-- =====================================================================================
-- KPI tables (revenue / fleet utilization / one-way flows / maintenance cost)
-- =====================================================================================
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_revenue_by_site SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', ''certified_by'' = ''' || certified_by
  || ''', ''certified_date'' = ''' || certified_date || ''', ''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.kpi_revenue_by_site IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_revenue_by_site OWNER TO `' || stewards_group || '`';

EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_fleet_utilization SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', ''certified_by'' = ''' || certified_by
  || ''', ''certified_date'' = ''' || certified_date || ''', ''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.kpi_fleet_utilization IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_fleet_utilization OWNER TO `' || stewards_group || '`';

EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_one_way_flows SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', ''certified_by'' = ''' || certified_by
  || ''', ''certified_date'' = ''' || certified_date || ''', ''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.kpi_one_way_flows IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_one_way_flows OWNER TO `' || stewards_group || '`';

EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_maintenance_cost SET TAGS ('
  || '''certification_status'' = ''CERTIFIED'', ''certified_by'' = ''' || certified_by
  || ''', ''certified_date'' = ''' || certified_date || ''', ''data_layer'' = ''gold'')';
EXECUTE IMMEDIATE 'COMMENT ON TABLE ' || catalog_name || '.' || gold_schema || '.kpi_maintenance_cost IS ''' || cert_comment || '''';
EXECUTE IMMEDIATE 'ALTER TABLE ' || catalog_name || '.' || gold_schema || '.kpi_maintenance_cost OWNER TO `' || stewards_group || '`';

-- =====================================================================================
-- Mirrorable certification registry (so Fabric/Power BI can SEE certification status,
-- since UC tags/comments do NOT sync to Fabric — R8 §6.3 recommendation #1).
-- This is a standard managed Delta table and is mirrored with the rest of gold.
-- =====================================================================================
EXECUTE IMMEDIATE
  'CREATE OR REPLACE TABLE ' || catalog_name || '.' || gold_schema || '._table_certifications ('
  || '  table_name STRING COMMENT ''Fully-qualified gold table'','
  || '  certification_status STRING,'
  || '  certified_by STRING,'
  || '  certified_date STRING,'
  || '  data_layer STRING'
  || ') COMMENT ''Certification registry mirrored into Fabric (UC tags/comments do not auto-sync).''';

EXECUTE IMMEDIATE
  'INSERT INTO ' || catalog_name || '.' || gold_schema || '._table_certifications VALUES '
  || '(''' || catalog_name || '.' || gold_schema || '.dim_site'',             ''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold''),'
  || '(''' || catalog_name || '.' || gold_schema || '.dim_vehicle'',          ''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold''),'
  || '(''' || catalog_name || '.' || gold_schema || '.dim_customer'',         ''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold''),'
  || '(''' || catalog_name || '.' || gold_schema || '.fact_rental'',          ''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold''),'
  || '(''' || catalog_name || '.' || gold_schema || '.kpi_revenue_by_site'',  ''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold''),'
  || '(''' || catalog_name || '.' || gold_schema || '.kpi_fleet_utilization'',''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold''),'
  || '(''' || catalog_name || '.' || gold_schema || '.kpi_one_way_flows'',    ''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold''),'
  || '(''' || catalog_name || '.' || gold_schema || '.kpi_maintenance_cost'', ''CERTIFIED'', ''' || certified_by || ''', ''' || certified_date || ''', ''gold'')';

-- =====================================================================================
-- VERIFY — tags should show certification_status=CERTIFIED for the gold tables.
-- =====================================================================================
EXECUTE IMMEDIATE
  'SELECT table_name, tag_name, tag_value FROM ' || catalog_name
  || '.information_schema.table_tags WHERE schema_name = ''' || gold_schema || ''' ORDER BY table_name, tag_name';

-- Spot-check one table's full metadata (tag + comment + owner visible here):
EXECUTE IMMEDIATE 'DESCRIBE EXTENDED ' || catalog_name || '.' || gold_schema || '.fact_rental';
