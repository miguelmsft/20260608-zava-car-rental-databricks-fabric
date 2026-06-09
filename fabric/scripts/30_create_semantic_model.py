#!/usr/bin/env python3
"""Create the Zava **Direct Lake on OneLake** semantic model — *as code* — idempotently
(plan Step 14; research R2/R3).

What it does
------------
1. Resolves all identifiers from ``deploy_config.json`` (Step 1 schema): the target workspace
   (``workspace.use_existing`` / ``workspace.existing_workspace_id`` / ``workspace.name``,
   created by Step 10), the Step-10 **Lakehouse** that holds the data
   (``semantic_model.lakehouse_name`` / ``shortcut.lakehouse_name`` / ``lakehouse.name`` /
   ``<catalog>_lakehouse``), the semantic-model display name
   (``semantic_model.name`` -> default ``Zava Fleet Analytics``), and the source schemas
   (``source.gold_schema`` for the dims/fact, ``semantic_model.thin_gold_schema`` for the
   V-Ordered aggregates). CLI / environment overrides take precedence. **No secrets are read
   from config.**
2. Authenticates via ``DefaultAzureCredential`` (``az login`` / managed identity / env service
   principal) when run outside a Fabric notebook; inside a Fabric notebook the ambient identity
   is used by ``semantic-link-labs``. An optional service-principal token provider is built when
   ``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` (+ a Key Vault or federated credential) are present.
3. **Find-or-create the Direct Lake semantic model** (idempotent) over the Step-13 thin gold
   (V-Ordered Delta) aggregates **and** the Zava gold star schema, using
   ``sempy_labs.directlake.generate_direct_lake_semantic_model()`` with a configurable
   ``source_type`` (``Lakehouse`` for the Variation-2 shortcut path, or
   ``MirroredAzureDatabricksCatalog`` for Variation 1) (R2 §3.2).
4. **Adds the Zava entity relationships, KPI measures, RLS role, OLS, and AI descriptions** via the
   TOM wrapper (``sempy_labs.tom.connect_semantic_model``) (R2 §3.3). Every add is guarded so a
   re-run is idempotent.

   * Relationships use the **verified** signature
     ``add_relationship(..., cross_filtering_behavior="OneDirection"|"BothDirections")`` —
     NOT the non-existent ``cross_filter_direction`` keyword.
   * Report rebinding (optional) uses ``sempy_labs.report.report_rebind(...)`` —
     NOT the non-existent ``rebind_report``.
5. **Optionally serializes** the live model back to TMDL into ``fabric/semantic-model/`` for
   Git/code deployment (``--export-tmdl``), keeping the committed definition in sync.

The model and relationships/measures below are the executable twin of the committed TMDL under
``fabric/semantic-model/`` — the two are intentionally kept consistent.

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names / ids / placeholders only); auth is acquired at
  runtime via ``DefaultAzureCredential`` / ``az login`` / the Fabric notebook identity.
* **Authoring phase:** do **not** run this against a live tenant unless you intend to create Fabric
  items. Use ``--dry-run`` to preview every intended ``semantic-link-labs`` call with **no**
  authentication and **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/30_create_semantic_model.py --dry-run

    # Create / refresh the Direct Lake model from deploy_config.json:
    python fabric/scripts/30_create_semantic_model.py

    # Variation 1 (mirrored Databricks catalog) as the source, explicit names:
    python fabric/scripts/30_create_semantic_model.py \
        --workspace-name zava-fabric-ws \
        --semantic-model-name "Zava Fleet Analytics" \
        --source-name zava-databricks-mirror \
        --source-type MirroredAzureDatabricksCatalog

    # Also rebind an existing report to this model and export TMDL back to the repo:
    python fabric/scripts/30_create_semantic_model.py \
        --rebind-report "Zava Fleet Dashboard" --export-tmdl
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SEMANTIC_MODEL_NAME = "Zava Fleet Analytics"
DEFAULT_GOLD_SCHEMA = "gold"
DEFAULT_THIN_GOLD_SCHEMA = "thin_gold"
# Direct Lake source types accepted by generate_direct_lake_semantic_model (R2 §3.2).
VALID_SOURCE_TYPES = (
    "Lakehouse",
    "Warehouse",
    "SQLDatabase",
    "MirroredAzureDatabricksCatalog",
    "MirroredDatabase",
)
# Verified cross-filter values for TOM add_relationship (R2 — NOT cross_filter_direction).
VALID_CROSS_FILTER = ("OneDirection", "BothDirections", "Automatic")

# Fabric control-plane scope (used only to validate auth fast when not in a notebook).
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"

# Retry / backoff tuning for transient sempy_labs / XMLA failures.
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5

# Repo-relative defaults.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)
SEMANTIC_MODEL_DIR = os.path.join(_REPO_ROOT, "fabric", "semantic-model")

_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

LOG = logging.getLogger("zava.fabric.semantic_model")


class SemanticModelError(RuntimeError):
    """Raised on unrecoverable problems (bad identity, auth failure, API error)."""


# ---------------------------------------------------------------------------
# Model definition — the executable twin of the committed TMDL.
# Bound to the Step-10 Lakehouse: dims/fact in the gold schema, V-Ordered aggregates in thin_gold.
# ---------------------------------------------------------------------------

# (display name, schema-key) — schema-key picks gold vs. thin_gold at deploy time.
TABLES: List[Dict[str, str]] = [
    {"name": "dim_site", "schema": "gold", "entity": "Sites"},
    {"name": "dim_vehicle", "schema": "gold", "entity": "Vehicles"},
    {"name": "dim_customer", "schema": "gold", "entity": "Customers"},
    {"name": "fact_rental", "schema": "gold", "entity": "Rentals"},
    {"name": "agg_revenue_by_site", "schema": "thin_gold", "entity": "RevenuePerSite"},
    {"name": "agg_revenue_by_site_month", "schema": "thin_gold", "entity": "RevenueByMonth"},
    {"name": "agg_fleet_utilization_by_site_month", "schema": "thin_gold", "entity": "FleetUtilization"},
    {"name": "agg_idle_vehicles_by_site", "schema": "thin_gold", "entity": "IdleVehicles"},
    {"name": "agg_one_way_flows", "schema": "thin_gold", "entity": "OneWayFlows"},
    {"name": "agg_maintenance_cost_by_site", "schema": "thin_gold", "entity": "Maintenance"},
    # Variation 2 (Lakeflow streaming-table -> OneLake shortcut) — the demonstrable V2 signal.
    {"name": "agg_telematics_freshness", "schema": "thin_gold", "entity": "TelematicsV2"},
]

# Zava entity relationships (star schema). cross_filtering_behavior uses the VERIFIED value set
# (OneDirection / BothDirections) — never the non-existent cross_filter_direction keyword (R2).
RELATIONSHIPS: List[Dict[str, object]] = [
    {
        "from_table": "fact_rental", "from_column": "pickup_site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "fact_rental", "from_column": "vehicle_id",
        "to_table": "dim_vehicle", "to_column": "vehicle_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "fact_rental", "from_column": "customer_id",
        "to_table": "dim_customer", "to_column": "customer_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "dim_vehicle", "from_column": "current_site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "agg_revenue_by_site", "from_column": "pickup_site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "agg_fleet_utilization_by_site_month", "from_column": "pickup_site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "agg_idle_vehicles_by_site", "from_column": "current_site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "agg_maintenance_cost_by_site", "from_column": "site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        "from_table": "agg_one_way_flows", "from_column": "pickup_site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": True,
    },
    {
        # Inactive — activate with USERELATIONSHIP() for return-side flow analysis.
        "from_table": "agg_one_way_flows", "from_column": "return_site_id",
        "to_table": "dim_site", "to_column": "site_id",
        "from_cardinality": "Many", "to_cardinality": "One",
        "cross_filtering_behavior": "OneDirection", "is_active": False,
    },
]

# KPI measures (Step-3 entity model + Step-8 gold + Step-13 thin gold).
MEASURES: List[Dict[str, str]] = [
    {
        "table": "fact_rental", "name": "Total Rentals",
        "expression": "COUNTROWS('fact_rental')",
        "format_string": "#,##0", "display_folder": "Volume",
        "description": "Total number of rental transactions.",
    },
    {
        "table": "fact_rental", "name": "Total Revenue",
        "expression": "SUM('fact_rental'[revenue_usd])",
        "format_string": "\\$#,0;(\\$#,0);\\$#,0", "display_folder": "Revenue",
        "description": "Total settled revenue (Payments rolled to the rental grain).",
    },
    {
        "table": "fact_rental", "name": "Revenue per Rental",
        "expression": "DIVIDE([Total Revenue], [Total Rentals])",
        "format_string": "\\$#,0.00;(\\$#,0.00);\\$#,0.00", "display_folder": "Revenue",
        "description": "Average revenue per rental transaction.",
    },
    {
        "table": "fact_rental", "name": "Revenue per Site",
        "expression": "DIVIDE([Total Revenue], DISTINCTCOUNT('dim_site'[site_id]))",
        "format_string": "\\$#,0;(\\$#,0);\\$#,0", "display_folder": "Revenue",
        "description": "Revenue divided across the active sites in context.",
    },
    {
        "table": "fact_rental", "name": "Average Rental Duration",
        "expression": "AVERAGE('fact_rental'[rental_days])",
        "format_string": "#,##0.0", "display_folder": "Volume",
        "description": "Average rental length in days.",
    },
    {
        "table": "fact_rental", "name": "Total Miles Driven",
        "expression": "SUM('fact_rental'[miles_driven])",
        "format_string": "#,##0", "display_folder": "Volume",
        "description": "Total miles driven across rentals.",
    },
    {
        "table": "fact_rental", "name": "Active Vehicles",
        "expression": "DISTINCTCOUNT('fact_rental'[vehicle_id])",
        "format_string": "#,##0", "display_folder": "Fleet",
        "description": "Distinct vehicles that were rented in context.",
    },
    {
        "table": "fact_rental", "name": "One-Way Rentals",
        "expression": "CALCULATE(COUNTROWS('fact_rental'), 'fact_rental'[is_one_way] = TRUE())",
        "format_string": "#,##0", "display_folder": "One-Way",
        "description": "Rentals returned to a different site than pickup.",
    },
    {
        "table": "fact_rental", "name": "One-Way Rental %",
        "expression": "DIVIDE([One-Way Rentals], [Total Rentals])",
        "format_string": "0.0%;-0.0%;0.0%", "display_folder": "One-Way",
        "description": "Share of rentals that are one-way.",
    },
    {
        "table": "agg_revenue_by_site", "name": "Site Revenue",
        "expression": "SUM('agg_revenue_by_site'[total_revenue_usd])",
        "format_string": "\\$#,0;(\\$#,0);\\$#,0", "display_folder": "Revenue",
        "description": "Revenue per site (thin gold, V-Ordered).",
    },
    {
        # Backs the report's revenue-forecast visual with a TRUE monthly revenue time series
        # (Step-29 fix; replaces the prior utilization-axis / revenue-by-site mismatch).
        "table": "agg_revenue_by_site_month", "name": "Monthly Revenue",
        "expression": "SUM('agg_revenue_by_site_month'[total_revenue_usd])",
        "format_string": "\\$#,0;(\\$#,0);\\$#,0", "display_folder": "Revenue",
        "description": "Revenue per site per month (thin gold, V-Ordered) — forecast time series.",
    },
    {
        "table": "agg_fleet_utilization_by_site_month", "name": "Fleet Utilization %",
        "expression": (
            "DIVIDE("
            "SUM('agg_fleet_utilization_by_site_month'[total_rented_days]), "
            "SUMX('agg_fleet_utilization_by_site_month', "
            "'agg_fleet_utilization_by_site_month'[fleet_size] * "
            "'agg_fleet_utilization_by_site_month'[days_in_month]))"
        ),
        "format_string": "0.0%;-0.0%;0.0%", "display_folder": "Utilization",
        "description": "Rented vehicle-days / (fleet x days in month) — weighted across sites.",
    },
    {
        "table": "agg_idle_vehicles_by_site", "name": "Idle Vehicle Count",
        "expression": "SUM('agg_idle_vehicles_by_site'[idle_vehicles])",
        "format_string": "#,##0", "display_folder": "Fleet Status",
        "description": "Number of idle vehicles at their current site.",
    },
    {
        "table": "agg_idle_vehicles_by_site", "name": "Idle Vehicle %",
        "expression": (
            "DIVIDE(SUM('agg_idle_vehicles_by_site'[idle_vehicles]), "
            "SUM('agg_idle_vehicles_by_site'[vehicles_at_site]))"
        ),
        "format_string": "0.0%;-0.0%;0.0%", "display_folder": "Fleet Status",
        "description": "Idle vehicles as a share of vehicles at the site.",
    },
    {
        "table": "agg_one_way_flows", "name": "One-Way Trips",
        "expression": "SUM('agg_one_way_flows'[one_way_trips])",
        "format_string": "#,##0", "display_folder": "One-Way",
        "description": "One-way trips in the pickup->return movement matrix.",
    },
    {
        "table": "agg_maintenance_cost_by_site", "name": "Maintenance Cost",
        "expression": "SUM('agg_maintenance_cost_by_site'[total_maintenance_cost_usd])",
        "format_string": "\\$#,0;(\\$#,0);\\$#,0", "display_folder": "Maintenance",
        "description": "Total maintenance spend (labor + parts) per site.",
    },
    # --- Variation 2 (Lakeflow streaming-table -> OneLake shortcut) signal measures ---
    {
        "table": "agg_telematics_freshness", "name": "Telematics Vehicles Tracked",
        "expression": "DISTINCTCOUNT('agg_telematics_freshness'[vehicle_id])",
        "format_string": "#,##0", "display_folder": "Variation 2 (Lakeflow shortcut)",
        "description": "Vehicles streaming telemetry via the V2 Lakeflow -> OneLake shortcut path.",
    },
    {
        "table": "agg_telematics_freshness", "name": "Avg Telematics Freshness (min)",
        "expression": "AVERAGE('agg_telematics_freshness'[minutes_since_last_snapshot])",
        "format_string": "#,##0.0", "display_folder": "Variation 2 (Lakeflow shortcut)",
        "description": "Average minutes since each vehicle's last V2 telematics snapshot.",
    },
    {
        "table": "agg_telematics_freshness", "name": "Max Telematics Idle (min)",
        "expression": "MAX('agg_telematics_freshness'[max_idle_minutes])",
        "format_string": "#,##0.0", "display_folder": "Variation 2 (Lakeflow shortcut)",
        "description": "Worst observed idle minutes from the V2 telematics stream.",
    },
]

# RLS role + city filter (demo). Members are assigned via the Power BI REST API / XMLA at deploy.
RLS_ROLE_NAME = "CityManager"
RLS_TABLE = "dim_site"
RLS_FILTER = "'dim_site'[city] = USERPRINCIPALNAME()"

# Object-level security: hide synthetic PII-like columns from the demo role.
OLS_COLUMNS = [
    {"table": "dim_customer", "column": "email"},
    {"table": "dim_customer", "column": "phone"},
]

# "Prep for AI" table descriptions (R3) — improve Fabric Data Agent (Step 17) accuracy.
TABLE_DESCRIPTIONS: Dict[str, str] = {
    "dim_site": "Zava rental locations (Seattle HQ + US cities) with latitude/longitude for maps.",
    "dim_vehicle": "The Zava fleet with its vehicle class / rate card; status drives the idle KPI.",
    "dim_customer": "Renters. email/phone are synthetic PII-like columns for the governance demo.",
    "fact_rental": "One row per rental; carries reservation link, settled revenue, and one-way flag.",
    "agg_revenue_by_site": "Revenue per site (V-Ordered) enriched with site geo for the map.",
    "agg_revenue_by_site_month": "Revenue per site per month (V-Ordered) — the forecast time series.",
    "agg_fleet_utilization_by_site_month": "Monthly fleet utilization by site (V-Ordered).",
    "agg_idle_vehicles_by_site": "Vehicle status counts by site (idle / rented / maintenance).",
    "agg_one_way_flows": "Pickup->return one-way movement matrix with dual geo for flow lines.",
    "agg_maintenance_cost_by_site": "Maintenance spend (labor + parts) per site (V-Ordered).",
    "agg_telematics_freshness": "Variation 2: per-vehicle telematics freshness/health from the "
                                "Lakeflow streaming table via the OneLake shortcut (not mirrorable).",
}

# Blue Zava branding + AI-prep model annotations (kept in sync with model.tmdl).
MODEL_ANNOTATIONS: Dict[str, str] = {
    "Zava_Brand": "blue",
    "Zava_ThemeFile": "fabric/theme/zava-blue-theme.json",
    "Zava_AIPrep": "enabled",
}


# ---------------------------------------------------------------------------
# Config / identity resolution (no secrets)
# ---------------------------------------------------------------------------

def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.match(value.strip()))


def _clean(value: object) -> Optional[str]:
    """Return a usable string value, or None for missing / blank / placeholder values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _is_placeholder(value):
        return None
    return value


def _resolve_config_path(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        if not os.path.isfile(explicit):
            raise SemanticModelError(f"--config path does not exist: {explicit}")
        return explicit
    for candidate in DEFAULT_CONFIG_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _load_config(path: Optional[str]) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticModelError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SemanticModelError(f"config {path} must be a JSON object")
    return data


class SemanticModelPlan:
    """Resolved, validated plan for the Direct Lake semantic-model operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_name: Optional[str],
        use_existing_workspace: bool,
        existing_workspace_id: Optional[str],
        semantic_model_name: str,
        lakehouse_name: Optional[str],
        lakehouse_id: Optional[str],
        source_name: Optional[str],
        source_type: str,
        gold_schema: str,
        thin_gold_schema: str,
        rebind_report: Optional[str],
        refresh: bool,
        enable: bool,
    ) -> None:
        self.config_path = config_path
        self.workspace_name = workspace_name
        self.use_existing_workspace = use_existing_workspace
        self.existing_workspace_id = existing_workspace_id
        self.semantic_model_name = semantic_model_name
        self.lakehouse_name = lakehouse_name
        self.lakehouse_id = lakehouse_id
        self.source_name = source_name
        self.source_type = source_type
        self.gold_schema = gold_schema
        self.thin_gold_schema = thin_gold_schema
        self.rebind_report = rebind_report
        self.refresh = refresh
        self.enable = enable

    @property
    def workspace_ref(self) -> Optional[str]:
        """Whichever workspace identifier sempy_labs should use (id preferred)."""
        return self.existing_workspace_id if self.use_existing_workspace else self.workspace_name

    def schema_for(self, schema_key: str) -> str:
        """Map a table's schema-key (gold/thin_gold) to the resolved schema name."""
        return self.thin_gold_schema if schema_key == "thin_gold" else self.gold_schema


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_name: Optional[str] = None,
    existing_workspace_id: Optional[str] = None,
    semantic_model_name: Optional[str] = None,
    lakehouse_name: Optional[str] = None,
    lakehouse_id: Optional[str] = None,
    source_name: Optional[str] = None,
    source_type: Optional[str] = None,
    gold_schema: Optional[str] = None,
    thin_gold_schema: Optional[str] = None,
    rebind_report: Optional[str] = None,
    no_refresh: bool = False,
) -> SemanticModelPlan:
    """Resolve the operation plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable > default.
    Raises ``SemanticModelError`` naming any field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    sm_cfg = cfg.get("semantic_model", {}) if isinstance(cfg.get("semantic_model"), dict) else {}
    sc_cfg = cfg.get("shortcut", {}) if isinstance(cfg.get("shortcut"), dict) else {}
    lh_cfg = cfg.get("lakehouse", {}) if isinstance(cfg.get("lakehouse"), dict) else {}
    src_cfg = cfg.get("source", {}) if isinstance(cfg.get("source"), dict) else {}
    feat_cfg = cfg.get("features", {}) if isinstance(cfg.get("features"), dict) else {}

    # Feature gate (default on so a fresh sample config is demonstrable).
    flag = feat_cfg.get("enable_direct_lake_model")
    enable = bool(flag) if isinstance(flag, bool) else True

    use_existing = bool(ws_cfg.get("use_existing"))
    name = workspace_name or _clean(ws_cfg.get("name")) or os.environ.get("FABRIC_WORKSPACE_NAME")
    existing_id = (
        existing_workspace_id
        or _clean(ws_cfg.get("existing_workspace_id"))
        or os.environ.get("FABRIC_WORKSPACE_ID")
    )

    sm_name = (
        semantic_model_name
        or _clean(sm_cfg.get("name"))
        or os.environ.get("FABRIC_SEMANTIC_MODEL_NAME")
        or DEFAULT_SEMANTIC_MODEL_NAME
    )

    # The Lakehouse (Step 10) holds both the gold star schema and the thin gold aggregates.
    lh_id = (
        lakehouse_id
        or _clean(sm_cfg.get("lakehouse_id"))
        or _clean(sc_cfg.get("lakehouse_id"))
        or os.environ.get("FABRIC_LAKEHOUSE_ID")
    )
    lh_name = (
        lakehouse_name
        or _clean(sm_cfg.get("lakehouse_name"))
        or _clean(sc_cfg.get("lakehouse_name"))
        or _clean(lh_cfg.get("name"))
        or os.environ.get("FABRIC_LAKEHOUSE_NAME")
    )
    catalog = _clean(src_cfg.get("databricks_catalog"))
    if not lh_name and not lh_id and catalog:
        lh_name = f"{catalog}_lakehouse"  # matches the Step-12 shortcut default

    # Direct Lake source: default to the Lakehouse (Variation-2 shortcut path). For Variation 1
    # set --source-type MirroredAzureDatabricksCatalog and --source-name <mirror item>.
    s_type = (
        source_type
        or _clean(sm_cfg.get("source_type"))
        or os.environ.get("FABRIC_SEMANTIC_MODEL_SOURCE_TYPE")
        or "Lakehouse"
    )
    if s_type not in VALID_SOURCE_TYPES:
        raise SemanticModelError(
            f"invalid --source-type {s_type!r}; expected one of {', '.join(VALID_SOURCE_TYPES)}"
        )
    # For a Lakehouse source the source item is the Lakehouse itself.
    s_name = (
        source_name
        or _clean(sm_cfg.get("source_name"))
        or os.environ.get("FABRIC_SEMANTIC_MODEL_SOURCE_NAME")
        or (lh_id or lh_name if s_type == "Lakehouse" else None)
    )

    gold = (
        gold_schema
        or _clean(src_cfg.get("gold_schema"))
        or os.environ.get("FABRIC_GOLD_SCHEMA")
        or DEFAULT_GOLD_SCHEMA
    )
    thin_gold = (
        thin_gold_schema
        or _clean(sm_cfg.get("thin_gold_schema"))
        or os.environ.get("FABRIC_THIN_GOLD_SCHEMA")
        or DEFAULT_THIN_GOLD_SCHEMA
    )

    report = (
        rebind_report
        or _clean(sm_cfg.get("rebind_report"))
        or os.environ.get("FABRIC_REBIND_REPORT")
    )

    # Validation.
    if use_existing:
        if not existing_id:
            raise SemanticModelError(
                "workspace.use_existing=true but no workspace id resolved "
                "(set workspace.existing_workspace_id, --workspace-id, or FABRIC_WORKSPACE_ID)"
            )
        if not _GUID_RE.match(existing_id):
            raise SemanticModelError(
                f"resolved existing workspace id is not a GUID: {existing_id!r}"
            )
    elif not name:
        raise SemanticModelError(
            "could not resolve workspace name "
            "(set workspace.name, --workspace-name, or FABRIC_WORKSPACE_NAME)"
        )

    if not s_name:
        raise SemanticModelError(
            "could not resolve the Direct Lake source. For a Lakehouse source set the lakehouse "
            "(semantic_model.lakehouse_name / shortcut.lakehouse_name / lakehouse.name / "
            "source.databricks_catalog -> <catalog>_lakehouse, or --lakehouse-name/--lakehouse-id). "
            "For a mirrored catalog set --source-name and --source-type "
            "MirroredAzureDatabricksCatalog."
        )

    return SemanticModelPlan(
        config_path=resolved_path,
        workspace_name=name,
        use_existing_workspace=use_existing,
        existing_workspace_id=existing_id,
        semantic_model_name=sm_name,
        lakehouse_name=lh_name,
        lakehouse_id=lh_id,
        source_name=s_name,
        source_type=s_type,
        gold_schema=gold,
        thin_gold_schema=thin_gold,
        rebind_report=report,
        refresh=not no_refresh,
        enable=enable,
    )


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential; Fabric-notebook identity when present)
# ---------------------------------------------------------------------------

def validate_auth() -> None:
    """Acquire a Fabric control-plane token to fail fast on auth problems (no secret persisted).

    Inside a Fabric notebook ``semantic-link-labs`` uses the ambient identity, so a missing local
    credential is not fatal there; we only warn. Outside a notebook this surfaces the need to run
    ``az login`` (or set service-principal env vars) before any mutation.
    """
    if _in_fabric_notebook():
        LOG.info("Running inside a Fabric notebook — using the ambient workspace identity.")
        return
    scope = FABRIC_RESOURCE.rstrip("/") + "/.default"
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore import-not-found
    except ImportError:
        LOG.warning(
            "azure-identity not installed; relying on semantic-link-labs / az login for auth."
        )
        return
    try:
        DefaultAzureCredential(exclude_interactive_browser_credential=False).get_token(scope)
        LOG.debug("Acquired a Fabric control-plane token via DefaultAzureCredential.")
    except Exception as exc:  # noqa: BLE001 - surface any auth failure uniformly
        raise SemanticModelError(
            f"could not acquire a Fabric token (run `az login` or set service-principal env "
            f"vars): {exc}"
        ) from exc


def _in_fabric_notebook() -> bool:
    """Best-effort detection of the Fabric/Spark notebook runtime."""
    if any(k in os.environ for k in ("MMLSPARK_PLATFORM_INFO", "SPARK_HOME", "AZURE_SERVICE")):
        return True
    try:  # the notebook runtime injects a `spark` global
        return "spark" in __builtins__  # type: ignore[operator]
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Retry helper (transient sempy_labs / XMLA failures)
# ---------------------------------------------------------------------------

def _with_retry(fn: Callable[[], object], *, what: str) -> object:
    """Run ``fn`` with exponential backoff on transient errors."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - sempy_labs raises a variety of error types
            if attempt > MAX_RETRIES or not _is_transient(exc):
                raise
            delay = DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1))
            LOG.warning(
                "%s failed (%s); retrying in %ss (attempt %s/%s)",
                what, exc, delay, attempt, MAX_RETRIES,
            )
            time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in ("429", "throttl", "timeout", "timed out", "temporarily", "503", "500",
                      "connection reset", "operation in progress")
    )


# ---------------------------------------------------------------------------
# Model generation + TOM enrichment (idempotent)
# ---------------------------------------------------------------------------

def _table_list_for_source(plan: SemanticModelPlan) -> List[str]:
    """Schema-qualified table names for generate_direct_lake_semantic_model."""
    return [f"{plan.schema_for(t['schema'])}.{t['name']}" for t in TABLES]


def ensure_semantic_model(plan: SemanticModelPlan) -> None:
    """Find-or-create the Direct Lake on OneLake semantic model (idempotent) (R2 §3.2)."""
    import sempy_labs as labs  # type: ignore import-not-found
    from sempy_labs import directlake  # type: ignore import-not-found

    existing = _with_retry(
        lambda: labs.list_semantic_models(workspace=plan.workspace_ref),
        what="list_semantic_models",
    )
    names = _existing_names(existing)
    if plan.semantic_model_name.lower() in names:
        LOG.info(
            "Semantic model %r already exists — skipping generation (idempotent).",
            plan.semantic_model_name,
        )
        return

    tables = _table_list_for_source(plan)
    LOG.info(
        "Creating Direct Lake semantic model %r over %d tables (source_type=%s, source=%s).",
        plan.semantic_model_name, len(tables), plan.source_type, plan.source_name,
    )
    _with_retry(
        lambda: directlake.generate_direct_lake_semantic_model(
            dataset=plan.semantic_model_name,
            tables=tables,
            source=plan.source_name,
            source_type=plan.source_type,
            workspace=plan.workspace_ref,
            refresh=plan.refresh,
        ),
        what="generate_direct_lake_semantic_model",
    )
    LOG.info("Semantic model %r created.", plan.semantic_model_name)


def _existing_names(listing: object) -> set:
    """Extract lower-cased item names from a sempy_labs listing (DataFrame or list)."""
    names: set = set()
    # pandas DataFrame path
    for col in ("Dataset Name", "Semantic Model Name", "Name", "name"):
        try:
            if hasattr(listing, "columns") and col in listing.columns:  # type: ignore[attr-defined]
                names.update(str(v).strip().lower() for v in listing[col])  # type: ignore[index]
                return names
        except Exception:  # noqa: BLE001
            continue
    # list-of-dicts path
    if isinstance(listing, list):
        for item in listing:
            if isinstance(item, dict):
                val = item.get("displayName") or item.get("name")
                if val:
                    names.add(str(val).strip().lower())
    return names


def enrich_with_tom(plan: SemanticModelPlan) -> None:
    """Add relationships, measures, RLS, OLS, and AI descriptions via the TOM wrapper (R2 §3.3).

    Every add is guarded for idempotency so re-runs do not raise on already-present objects.
    """
    from sempy_labs.tom import connect_semantic_model  # type: ignore import-not-found

    def _apply() -> None:
        with connect_semantic_model(
            dataset=plan.semantic_model_name,
            workspace=plan.workspace_ref,
            readonly=False,
        ) as tom:
            _apply_relationships(tom)
            _apply_measures(tom)
            _apply_rls_ols(tom)
            _apply_ai_metadata(tom)
        LOG.info("TOM enrichment complete (relationships, measures, RLS/OLS, AI metadata).")

    _with_retry(_apply, what="connect_semantic_model (TOM enrichment)")


def _relationship_exists(tom: object, rel: Dict[str, object]) -> bool:
    try:
        for existing in tom.model.Relationships:  # type: ignore[attr-defined]
            if (
                existing.FromTable.Name == rel["from_table"]
                and existing.FromColumn.Name == rel["from_column"]
                and existing.ToTable.Name == rel["to_table"]
                and existing.ToColumn.Name == rel["to_column"]
            ):
                return True
    except Exception:  # noqa: BLE001 - if introspection fails, fall through to add (guarded below)
        return False
    return False


def _apply_relationships(tom: object) -> None:
    for rel in RELATIONSHIPS:
        if _relationship_exists(tom, rel):
            LOG.debug(
                "Relationship %s[%s]->%s[%s] already present.",
                rel["from_table"], rel["from_column"], rel["to_table"], rel["to_column"],
            )
            continue
        # VERIFIED signature: cross_filtering_behavior (NOT cross_filter_direction) (R2).
        try:
            tom.add_relationship(  # type: ignore[attr-defined]
                from_table=rel["from_table"],
                from_column=rel["from_column"],
                to_table=rel["to_table"],
                to_column=rel["to_column"],
                from_cardinality=rel["from_cardinality"],
                to_cardinality=rel["to_cardinality"],
                cross_filtering_behavior=rel["cross_filtering_behavior"],
                is_active=rel["is_active"],
            )
            LOG.info(
                "Added relationship %s[%s]->%s[%s] (%s%s).",
                rel["from_table"], rel["from_column"], rel["to_table"], rel["to_column"],
                rel["cross_filtering_behavior"], "" if rel["is_active"] else ", inactive",
            )
        except Exception as exc:  # noqa: BLE001
            if "exist" in str(exc).lower():
                LOG.debug("Relationship already exists per provider; continuing.")
            else:
                raise


def _measure_exists(tom: object, table: str, name: str) -> bool:
    try:
        return any(
            m.Name == name for m in tom.model.Tables[table].Measures  # type: ignore[attr-defined]
        )
    except Exception:  # noqa: BLE001
        return False


def _apply_measures(tom: object) -> None:
    for m in MEASURES:
        if _measure_exists(tom, m["table"], m["name"]):
            LOG.debug("Measure %r already present on %s.", m["name"], m["table"])
            continue
        try:
            tom.add_measure(  # type: ignore[attr-defined]
                table_name=m["table"],
                measure_name=m["name"],
                expression=m["expression"],
                format_string=m.get("format_string"),
                description=m.get("description"),
                display_folder=m.get("display_folder"),
            )
            LOG.info("Added measure %r on %s.", m["name"], m["table"])
        except Exception as exc:  # noqa: BLE001
            if "exist" in str(exc).lower():
                LOG.debug("Measure already exists per provider; continuing.")
            else:
                raise


def _role_exists(tom: object, role_name: str) -> bool:
    try:
        return any(r.Name == role_name for r in tom.model.Roles)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def _apply_rls_ols(tom: object) -> None:
    # RLS role + filter.
    if not _role_exists(tom, RLS_ROLE_NAME):
        try:
            tom.add_role(role_name=RLS_ROLE_NAME, model_permission="Read")  # type: ignore[attr-defined]
            LOG.info("Added role %r.", RLS_ROLE_NAME)
        except Exception as exc:  # noqa: BLE001
            if "exist" not in str(exc).lower():
                raise
    try:
        tom.set_rls(  # type: ignore[attr-defined]
            role_name=RLS_ROLE_NAME,
            table_name=RLS_TABLE,
            filter_expression=RLS_FILTER,
        )
        LOG.info("Set RLS filter on %s for role %r.", RLS_TABLE, RLS_ROLE_NAME)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not set RLS filter (continuing): %s", exc)

    # OLS — hide synthetic PII-like columns from the demo role.
    for ols in OLS_COLUMNS:
        try:
            tom.set_ols(  # type: ignore[attr-defined]
                role_name=RLS_ROLE_NAME,
                table_name=ols["table"],
                column_name=ols["column"],
                permission="None",
            )
            LOG.info("Set OLS (hidden) on %s[%s] for role %r.",
                     ols["table"], ols["column"], RLS_ROLE_NAME)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not set OLS on %s[%s] (continuing): %s",
                        ols["table"], ols["column"], exc)


def _apply_ai_metadata(tom: object) -> None:
    """Set table descriptions + model annotations to improve Data Agent accuracy (R3)."""
    for table_name, desc in TABLE_DESCRIPTIONS.items():
        try:
            tom.model.Tables[table_name].Description = desc  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Could not set description on %s (continuing): %s", table_name, exc)
    for ann_name, ann_value in MODEL_ANNOTATIONS.items():
        try:
            tom.set_annotation(  # type: ignore[attr-defined]
                object=tom.model, name=ann_name, value=ann_value,
            )
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Could not set model annotation %s (continuing): %s", ann_name, exc)


def rebind_report(plan: SemanticModelPlan) -> None:
    """Rebind an existing report to this model using the VERIFIED ``report_rebind`` API (R2 §5.6).

    NOTE: the correct function name is ``report_rebind`` — NOT ``rebind_report``.
    """
    if not plan.rebind_report:
        return
    import sempy_labs.report as rep  # type: ignore import-not-found

    LOG.info("Rebinding report %r to semantic model %r.",
             plan.rebind_report, plan.semantic_model_name)
    _with_retry(
        lambda: rep.report_rebind(
            report=plan.rebind_report,
            dataset=plan.semantic_model_name,
            report_workspace=plan.workspace_ref,
            dataset_workspace=plan.workspace_ref,
        ),
        what="report_rebind",
    )
    LOG.info("Report %r rebound to %r.", plan.rebind_report, plan.semantic_model_name)


def export_tmdl(plan: SemanticModelPlan) -> None:
    """Serialize the live model back to TMDL under fabric/semantic-model/ for Git deployment."""
    import sempy_labs as labs  # type: ignore import-not-found

    LOG.info("Exporting semantic model %r to TMDL at %s.",
             plan.semantic_model_name, os.path.relpath(SEMANTIC_MODEL_DIR, _REPO_ROOT))
    tmdl = _with_retry(
        lambda: labs.get_semantic_model_definition(
            dataset=plan.semantic_model_name,
            workspace=plan.workspace_ref,
            format="TMDL",
        ),
        what="get_semantic_model_definition",
    )
    _write_tmdl_parts(tmdl)
    LOG.info("TMDL export complete.")


def _write_tmdl_parts(tmdl: object) -> None:
    """Write a sempy_labs TMDL definition (DataFrame of path/payload) to disk.

    Best-effort: handles the common DataFrame shape with 'path' and base64 'payload' columns.
    """
    import base64

    rows: List[Dict[str, str]] = []
    try:
        if hasattr(tmdl, "to_dict"):  # pandas DataFrame
            for _, row in tmdl.iterrows():  # type: ignore[attr-defined]
                rows.append({str(k): row[k] for k in row.index})
        elif isinstance(tmdl, list):
            rows = [r for r in tmdl if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not interpret TMDL export payload (%s); skipping write.", exc)
        return

    for row in rows:
        path = row.get("path") or row.get("Path")
        payload = row.get("payload") or row.get("Payload")
        if not path or payload is None:
            continue
        try:
            content = base64.b64decode(payload).decode("utf-8")
        except Exception:  # noqa: BLE001 - payload may already be plain text
            content = str(payload)
        dest = os.path.join(SEMANTIC_MODEL_DIR, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        LOG.debug("Wrote %s", os.path.relpath(dest, _REPO_ROOT))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(plan: SemanticModelPlan, *, dry_run: bool, do_export: bool) -> int:
    if not plan.enable:
        LOG.info("features.enable_direct_lake_model=false — nothing to do.")
        return 0
    if dry_run:
        return _print_dry_run(plan, do_export=do_export)

    validate_auth()
    ensure_semantic_model(plan)
    enrich_with_tom(plan)
    rebind_report(plan)
    if do_export:
        export_tmdl(plan)
    LOG.info(
        "Direct Lake semantic model %r ready in workspace %s.",
        plan.semantic_model_name, plan.workspace_ref,
    )
    return 0


def _print_dry_run(plan: SemanticModelPlan, *, do_export: bool) -> int:
    """Credential-free preview of every intended semantic-link-labs call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    LOG.info("[DRY-RUN] enable_direct_lake_model=%s", plan.enable)
    LOG.info("[DRY-RUN] Workspace: %s", plan.workspace_ref)
    LOG.info("[DRY-RUN] Semantic model: %r", plan.semantic_model_name)
    LOG.info("[DRY-RUN] Source: type=%s name=%s (gold schema=%s, thin gold schema=%s)",
             plan.source_type, plan.source_name, plan.gold_schema, plan.thin_gold_schema)
    LOG.info("[DRY-RUN] Would call directlake.generate_direct_lake_semantic_model("
             "dataset=%r, source_type=%r, refresh=%s) over %d tables:",
             plan.semantic_model_name, plan.source_type, plan.refresh, len(TABLES))
    for t in _table_list_for_source(plan):
        LOG.info("[DRY-RUN]     - %s", t)
    LOG.info("[DRY-RUN] Would open connect_semantic_model(readonly=False) and add:")
    LOG.info("[DRY-RUN]   %d relationships via add_relationship("
             "cross_filtering_behavior=...):", len(RELATIONSHIPS))
    for rel in RELATIONSHIPS:
        assert rel["cross_filtering_behavior"] in VALID_CROSS_FILTER  # guard correct API values
        LOG.info("[DRY-RUN]     - %s[%s] -> %s[%s] (%s%s)",
                 rel["from_table"], rel["from_column"], rel["to_table"], rel["to_column"],
                 rel["cross_filtering_behavior"], "" if rel["is_active"] else ", inactive")
    LOG.info("[DRY-RUN]   %d measures via add_measure(...):", len(MEASURES))
    for m in MEASURES:
        LOG.info("[DRY-RUN]     - [%s] %s", m["table"], m["name"])
    LOG.info("[DRY-RUN]   RLS role %r via add_role + set_rls on %s; OLS via set_ols on %s.",
             RLS_ROLE_NAME, RLS_TABLE, ", ".join(f"{o['table']}[{o['column']}]" for o in OLS_COLUMNS))
    LOG.info("[DRY-RUN]   AI 'Prep for AI' descriptions on %d tables + model annotations %s.",
             len(TABLE_DESCRIPTIONS), ", ".join(MODEL_ANNOTATIONS))
    if plan.rebind_report:
        LOG.info("[DRY-RUN] Would call report.report_rebind(report=%r, dataset=%r).",
                 plan.rebind_report, plan.semantic_model_name)
    if do_export:
        LOG.info("[DRY-RUN] Would export TMDL via get_semantic_model_definition(format='TMDL') "
                 "into %s.", os.path.relpath(SEMANTIC_MODEL_DIR, _REPO_ROOT))
    LOG.info("[DRY-RUN] Committed TMDL definition: %s",
             os.path.relpath(SEMANTIC_MODEL_DIR, _REPO_ROOT))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the Zava Direct Lake on OneLake semantic model "
                    "(semantic-link-labs + TOM).",
    )
    parser.add_argument(
        "--config",
        help="Path to deploy_config.json (defaults to fabric/config/deploy_config.json, then "
             "the committed .sample.json).",
    )
    parser.add_argument("--workspace-name", help="Workspace display name (overrides config/env).")
    parser.add_argument(
        "--workspace-id", dest="existing_workspace_id",
        help="Existing workspace GUID to target (overrides config/env).",
    )
    parser.add_argument(
        "--semantic-model-name", help="Semantic model display name (overrides config/env).",
    )
    parser.add_argument("--lakehouse-name", help="Step-10 Lakehouse display name (Direct Lake source).")
    parser.add_argument("--lakehouse-id", help="Step-10 Lakehouse GUID (Direct Lake source).")
    parser.add_argument(
        "--source-name",
        help="Direct Lake source item (defaults to the Lakehouse; the mirror item for Variation 1).",
    )
    parser.add_argument(
        "--source-type", choices=VALID_SOURCE_TYPES,
        help="Direct Lake source type (default Lakehouse; MirroredAzureDatabricksCatalog for V1).",
    )
    parser.add_argument("--gold-schema", help="Schema of the gold dims/fact (default gold).")
    parser.add_argument("--thin-gold-schema", help="Schema of the thin gold aggregates (default thin_gold).")
    parser.add_argument(
        "--rebind-report",
        help="Optionally rebind this report (by name) to the model via report_rebind.",
    )
    parser.add_argument(
        "--no-refresh", action="store_true",
        help="Skip the post-create framing refresh.",
    )
    parser.add_argument(
        "--export-tmdl", action="store_true",
        help="After enrichment, serialize the live model back to TMDL under fabric/semantic-model/.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print intended semantic-link-labs calls without authenticating or mutating anything.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        plan = resolve_plan(
            config_path=args.config,
            workspace_name=args.workspace_name,
            existing_workspace_id=args.existing_workspace_id,
            semantic_model_name=args.semantic_model_name,
            lakehouse_name=args.lakehouse_name,
            lakehouse_id=args.lakehouse_id,
            source_name=args.source_name,
            source_type=args.source_type,
            gold_schema=args.gold_schema,
            thin_gold_schema=args.thin_gold_schema,
            rebind_report=args.rebind_report,
            no_refresh=args.no_refresh,
        )
        return run(plan, dry_run=args.dry_run, do_export=args.export_tmdl)
    except SemanticModelError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
