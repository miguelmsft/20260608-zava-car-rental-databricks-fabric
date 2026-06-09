#!/usr/bin/env python3
"""Zava — Microsoft Purview setup: catalog, scans, classifications, labels, glossary (Step 22, research R6).

Why
---
The fourth Zava demo pillar is **governance**. Microsoft Purview (2026 *Unified Catalog* +
*Data Map*, accessed via purview.microsoft.com but with data-plane APIs still hosted on
``https://{account}.purview.azure.com`` — R6 §6) catalogs metadata + lineage from both
**Azure Databricks Unity Catalog** and **Microsoft Fabric**, auto-classifies the synthetic Zava
customer PII, and (via Information Protection) carries **sensitivity labels** through the Fabric /
Power BI surface. This script automates the *programmatically-automatable* slice of that setup so a
customer cloning the repo can reproduce it; the parts that are **UI / PowerShell-only** are clearly
flagged in comments and deferred to the manual-steps appendix (consolidated by Step 25).

What it AUTOMATES (REST, idempotent) — endpoints/shapes are from R6, not invented
--------------------------------------------------------------------------------
1. **Collections** — Account Data Plane API
   ``PUT {endpoint}/account/collections/{name}?api-version=2019-11-01-preview`` (R6 §6.3).
2. **Register sources** — Scanning Data Plane API
   ``PUT {endpoint}/scan/datasources/{name}?api-version=2023-09-01`` for the Databricks Unity
   Catalog source and the Fabric tenant source (R6 §6.3).
   ⚠️ CAVEAT (R6 §6.3): the ``kind`` values ``AzureDatabricksUnityCatalog`` / ``Fabric`` (and the
   MSI scan kinds ``AzureDatabricksUnityCatalogMsi`` / ``FabricMsi``) are **inferred from the
   connector docs**, NOT published in the ``2023-09-01`` REST enum. If the API rejects them, fall
   back to **portal registration** for the source, then re-run this script for scans/triggers/runs
   (which are source-type-agnostic).
3. **Scans + weekly triggers + on-demand runs** — Scanning Data Plane API:
   ``PUT .../scans/{scanName}``, ``PUT .../scans/{scanName}/triggers/default``,
   ``POST .../scans/{scanName}:run?runId={guid}`` (all ``api-version=2023-09-01``, R6 §6.3).
   Databricks UC scan toggles ``lineageExtraction``; classifications are applied **automatically**
   during that scan (R6 §5 — no manual classification REST API; custom classifiers unsupported for
   the UC source; Fabric source does NOT auto-classify).
4. **Glossary terms** — Data Map Data Plane (Atlas v2) API:
   ``POST {endpoint}/datamap/api/atlas/v2/glossary/term`` (the exact endpoint per R6 §6.5). The
   default glossary GUID is read via ``GET {endpoint}/datamap/api/atlas/v2/glossary`` and the term
   is created with an ``anchor.glossaryGuid`` (standard Atlas ``AtlasGlossaryTerm`` shape).
   Idempotent: existing terms of the same name in the glossary are skipped.
5. **Sensitivity labels on Fabric / Power BI items** — Power BI Admin REST API
   ``POST https://api.powerbi.com/v1.0/myorg/admin/informationprotection/setLabels``
   (SetLabelsAsAdmin — R6 §6.4). Programmatic labeling is supported for **all** Fabric items, not
   just Power BI (R6 §5). Limits: <=25 req/hr, <=2000 items/req, ``Tenant.ReadWrite.All``, caller
   must be a Fabric admin. This labels the Zava customer-PII semantic model + report.

What is UI / PowerShell-ONLY (NOT done here — see manual-steps appendix, R6 §6.6)
--------------------------------------------------------------------------------
* **Governance domains + data products** — UI-primary in the Purview portal; no published REST CRUD
  reference as of June 2026 (R6 §6.6). This script ATTACHES glossary terms (which is REST-able) but
  the domain/product scaffolding is a documented manual step.
* **DLP policy for Fabric** — PowerShell-only (``New-DlpCompliancePolicy`` + ``New-DlpComplianceRule``,
  Security & Compliance) or the Purview compliance portal; choose a *custom* policy (templates
  unsupported for Fabric DLP) (R6 §5, §6.6).
* **Sensitivity-label *policy / definition*** — PowerShell / compliance portal. This script only
  *applies* an already-published label GUID; it does not create the label taxonomy.
* **Fabric tenant admin settings** — UI-only: "service principals can use read-only admin APIs"
  (security group incl. the Purview MSI) + "OneLake -> external apps" (R6 §3, §6.6).
* **Databricks prerequisites** — UI / Databricks CLI: add the Purview MSI as a workspace service
  principal, run a SQL Warehouse, enable the ``system.access`` schema + grants for lineage (R6 §2).
* **Live-view extended permissions toggle** — UI-only in the Purview portal (R6 §6.6).

Prerequisite: the Purview account itself
----------------------------------------
This script **assumes the Purview account already exists** (provisioned via Bicep —
``Microsoft.Purview/accounts``, R6 §6.1 — under the infra step, not here). It only reads the account
*name* from config (``governance.purview_account``) and builds the data-plane endpoint
``https://{account}.purview.azure.com`` (R6 §6: the data-plane host stays ``*.purview.azure.com``
even though the new portal is purview.microsoft.com).

Security / authoring-phase notes
--------------------------------
* **No secrets.** Auth is via :class:`azure.identity.DefaultAzureCredential` (managed identity /
  service principal / ``az login``). Only non-secret identifiers (account name, metastore id,
  workspace URL, SQL Warehouse HTTP path, tenant id, label/item GUIDs) are read from config / env.
* **Authoring phase:** ``azure.identity`` / ``requests`` are imported lazily so this module parses
  and ``--dry-run`` / ``--help`` work without those packages and **without** calling live Purview.
  ``--dry-run`` prints the full plan (endpoints + bodies, secrets never present) and exits 0.
* **Idempotent by construction:** every create is a ``PUT`` upsert except the glossary term
  (``POST``), which is guarded by a name-existence check; re-running converges with no duplicates.

Usage
-----
    # Safe preview — resolves identifiers, prints every endpoint + body, calls NOTHING live:
    python scripts/governance/purview/setup_purview_scans.py --dry-run

    # Real run — auth via DefaultAzureCredential; identifiers from deploy_config.json + env:
    python scripts/governance/purview/setup_purview_scans.py

    # Limit to a phase (sources/scans, glossary, or labels) for re-runs / debugging:
    python scripts/governance/purview/setup_purview_scans.py --only scans
    python scripts/governance/purview/setup_purview_scans.py --only labels
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants — API versions / scopes / endpoint shapes (all from R6 §6)
# ---------------------------------------------------------------------------

SCANNING_API_VERSION = "2023-09-01"          # R6 §6.3 — Scanning Data Plane
ACCOUNT_API_VERSION = "2019-11-01-preview"   # R6 §6.3 — Account Data Plane (collections)
DATAMAP_API_VERSION = "2023-09-01"           # R6 §6.5 — Data Map Data Plane (Atlas glossary)

# OAuth scopes (R6 §6.3 / §6.4).
PURVIEW_SCOPE = "https://purview.azure.net/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

POWERBI_ADMIN_BASE = "https://api.powerbi.com/v1.0/myorg/admin"

# Repo-relative default config (real config preferred; sample as fallback so a fresh clone can
# --dry-run without a populated deploy_config.json) — same pattern as the fabric scripts.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)

# A value like "<TENANT_ID>" in the sample config is an unresolved placeholder, not a value.
_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")

log = logging.getLogger("zava.purview.setup")


class PurviewSetupError(RuntimeError):
    """Raised on unrecoverable configuration / API problems."""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(level: int) -> None:
    fmt = logging.Formatter("%(levelname)s - %(asctime)s - %(name)s - %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    log.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        log.addHandler(handler)


# ---------------------------------------------------------------------------
# Config / identifier resolution (no secrets — IDs/URLs only)
# ---------------------------------------------------------------------------

def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.match(value.strip()))


def _clean(value: object) -> Optional[str]:
    """Return a usable string, or None for missing / blank / ``<PLACEHOLDER>`` values."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or _is_placeholder(value):
        return None
    return value


def _resolve_config_path(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        if not os.path.isfile(explicit):
            raise PurviewSetupError(f"--config path does not exist: {explicit}")
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
        raise PurviewSetupError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PurviewSetupError(f"config {path} must be a JSON object")
    return data


class Settings:
    """Resolved, non-secret identifiers for the run (config first, env override)."""

    def __init__(self, cfg: dict) -> None:
        gov = cfg.get("governance") or {}
        src = cfg.get("source") or {}

        # Purview account name -> data-plane endpoint host (R6 §6).
        self.purview_account = _clean(os.environ.get("PURVIEW_ACCOUNT")) or _clean(
            gov.get("purview_account")
        )

        # Databricks UC source identifiers (R6 §2/§6.3). All non-secret.
        self.dbx_metastore_id = _clean(os.environ.get("PURVIEW_DBX_METASTORE_ID"))
        self.dbx_workspace_url = _clean(os.environ.get("DATABRICKS_WORKSPACE_URL"))
        self.dbx_sql_http_path = _clean(os.environ.get("DATABRICKS_SQL_WAREHOUSE_HTTP_PATH"))
        self.databricks_catalog = _clean(src.get("databricks_catalog")) or "zava"
        self.gold_schema = _clean(src.get("gold_schema")) or "gold"

        # Fabric tenant id (R6 §3/§6.3) — non-secret directory id.
        self.fabric_tenant_id = _clean(os.environ.get("AZURE_TENANT_ID")) or _clean(
            os.environ.get("FABRIC_TENANT_ID")
        )

        # Sensitivity-label application targets (R6 §6.4). Non-secret GUIDs.
        self.pii_label_id = _clean(os.environ.get("PURVIEW_PII_LABEL_ID"))
        self.semantic_model_id = _clean(os.environ.get("FABRIC_SEMANTIC_MODEL_ID"))
        self.report_id = _clean(os.environ.get("FABRIC_REPORT_ID"))

    @property
    def scan_endpoint(self) -> str:
        # Data-plane host stays *.purview.azure.com even on the new portal (R6 §6).
        return f"https://{self.purview_account}.purview.azure.com"

    @property
    def account_endpoint(self) -> str:
        return f"https://{self.purview_account}.purview.azure.com/account"

    def require_account(self) -> None:
        if not self.purview_account:
            raise PurviewSetupError(
                "governance.purview_account is not set (config) and PURVIEW_ACCOUNT env is unset. "
                "The Purview account is a PREREQUISITE — provision it via Bicep "
                "(Microsoft.Purview/accounts, R6 §6.1) and set its name in deploy_config.json."
            )


# ---------------------------------------------------------------------------
# Auth + HTTP (lazy imports so the module parses / --dry-run works without deps)
# ---------------------------------------------------------------------------

class PurviewClient:
    """Thin REST client. Holds an azure-identity credential and issues token-per-scope requests."""

    def __init__(self) -> None:
        from azure.identity import DefaultAzureCredential  # noqa: WPS433 (lazy import)

        self._credential = DefaultAzureCredential()
        self._token_cache: Dict[str, str] = {}

    def _headers(self, scope: str) -> Dict[str, str]:
        # Cache the bearer per scope for the life of the run.
        token = self._token_cache.get(scope)
        if token is None:
            token = self._credential.get_token(scope).token
            self._token_cache[scope] = token
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def request(
        self,
        method: str,
        url: str,
        scope: str,
        body: Optional[dict] = None,
        *,
        tolerate: Tuple[int, ...] = (),
    ) -> Any:
        import requests  # noqa: WPS433 (lazy import)

        resp = requests.request(method, url, json=body, headers=self._headers(scope))
        if resp.status_code >= 400 and resp.status_code not in tolerate:
            snippet = resp.text[:500] if resp.text else ""
            raise PurviewSetupError(f"{method} {url} -> {resp.status_code}: {snippet}")
        log.info("%s %s -> %s", method, _short(url), resp.status_code)
        return resp


def _short(url: str) -> str:
    """Trim the query string for tidy logs."""
    return url.split("?", 1)[0]


# ---------------------------------------------------------------------------
# Plan model — every live call is described as a Step so --dry-run can print it
# ---------------------------------------------------------------------------

class PlanStep:
    def __init__(
        self,
        title: str,
        method: str,
        url: str,
        scope: str,
        body: Optional[dict] = None,
        *,
        tolerate: Tuple[int, ...] = (),
    ) -> None:
        self.title = title
        self.method = method
        self.url = url
        self.scope = scope
        self.body = body
        self.tolerate = tolerate


def _coll(name: str) -> dict:
    return {"referenceName": name, "type": "CollectionReference"}


def build_source_and_scan_plan(s: Settings) -> List[PlanStep]:
    """Collections -> register sources -> scans -> weekly triggers -> on-demand runs (R6 §6.3)."""
    ep = s.scan_endpoint
    acct = s.account_endpoint
    dbx_coll, fab_coll = "zava-databricks", "zava-fabric"
    dbx_source, fab_source = "zava-databricks-uc", "zava-fabric-tenant"
    dbx_scan, fab_scan = "scan-zava-uc-weekly", "scan-zava-fabric-weekly"

    steps: List[PlanStep] = []

    # 1) Collections (Account Data Plane). PUT == upsert (idempotent).
    for coll_name, friendly, desc in [
        (dbx_coll, "Zava Databricks Sources", "Databricks Unity Catalog sources"),
        (fab_coll, "Zava Fabric Sources", "Fabric tenant and Power BI items"),
    ]:
        steps.append(
            PlanStep(
                f"Create collection '{coll_name}'",
                "PUT",
                f"{acct}/collections/{coll_name}?api-version={ACCOUNT_API_VERSION}",
                PURVIEW_SCOPE,
                {
                    "friendlyName": friendly,
                    "description": desc,
                    "parentCollection": {"referenceName": s.purview_account},
                },
            )
        )

    # 2) Register Databricks Unity Catalog source.  kind INFERRED — see caveat (R6 §6.3).
    steps.append(
        PlanStep(
            "Register Databricks Unity Catalog source",
            "PUT",
            f"{ep}/scan/datasources/{dbx_source}?api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
            {
                "kind": "AzureDatabricksUnityCatalog",
                "properties": {
                    "metastoreId": s.dbx_metastore_id,
                    "collection": _coll(dbx_coll),
                },
            },
        )
    )

    # 3) Databricks UC scan (MSI auth, lineage ON -> captures UC-internal lineage; auto-classifies
    #    PII columns during the scan — R6 §2/§5).  kind INFERRED — see caveat (R6 §6.3).
    steps.append(
        PlanStep(
            "Create Databricks UC scan (MSI, lineage on)",
            "PUT",
            f"{ep}/scan/datasources/{dbx_source}/scans/{dbx_scan}?api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
            {
                "kind": "AzureDatabricksUnityCatalogMsi",
                "properties": {
                    "workspaceUrl": s.dbx_workspace_url,
                    "httpPath": s.dbx_sql_http_path,
                    "lineageExtraction": True,
                    "collection": _coll(dbx_coll),
                },
            },
        )
    )

    # 4) Databricks UC weekly trigger.
    steps.append(
        PlanStep(
            "Create Databricks UC weekly trigger",
            "PUT",
            f"{ep}/scan/datasources/{dbx_source}/scans/{dbx_scan}/triggers/default"
            f"?api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
            {
                "properties": {
                    "scanLevel": "Full",
                    "recurrence": {
                        "frequency": "Week",
                        "interval": 1,
                        "schedule": {"hours": [2], "minutes": [0], "weekDays": ["Sunday"]},
                    },
                }
            },
        )
    )

    # 5) Run Databricks UC scan now. POST :run with a fresh runId (each run is a distinct id).
    run_id = str(uuid.uuid4())
    steps.append(
        PlanStep(
            "Run Databricks UC scan now",
            "POST",
            f"{ep}/scan/datasources/{dbx_source}/scans/{dbx_scan}:run"
            f"?runId={run_id}&api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
        )
    )

    # 6) Register Fabric tenant source.  kind INFERRED — see caveat (R6 §6.3).
    steps.append(
        PlanStep(
            "Register Fabric tenant source",
            "PUT",
            f"{ep}/scan/datasources/{fab_source}?api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
            {
                "kind": "Fabric",
                "properties": {
                    "tenant": s.fabric_tenant_id,
                    "collection": _coll(fab_coll),
                },
            },
        )
    )

    # 7) Fabric scan (MSI auth — captures Fabric-INTERNAL lineage only; see lineage_runbook.md for
    #    the cross-system seam).  kind INFERRED — see caveat (R6 §6.3).
    steps.append(
        PlanStep(
            "Create Fabric tenant scan (MSI)",
            "PUT",
            f"{ep}/scan/datasources/{fab_source}/scans/{fab_scan}?api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
            {
                "kind": "FabricMsi",
                "properties": {"collection": _coll(fab_coll)},
            },
        )
    )

    # 8) Fabric weekly trigger + run now.
    steps.append(
        PlanStep(
            "Create Fabric weekly trigger",
            "PUT",
            f"{ep}/scan/datasources/{fab_source}/scans/{fab_scan}/triggers/default"
            f"?api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
            {
                "properties": {
                    "scanLevel": "Full",
                    "recurrence": {
                        "frequency": "Week",
                        "interval": 1,
                        "schedule": {"hours": [3], "minutes": [0], "weekDays": ["Sunday"]},
                    },
                }
            },
        )
    )
    run_id = str(uuid.uuid4())
    steps.append(
        PlanStep(
            "Run Fabric tenant scan now",
            "POST",
            f"{ep}/scan/datasources/{fab_source}/scans/{fab_scan}:run"
            f"?runId={run_id}&api-version={SCANNING_API_VERSION}",
            PURVIEW_SCOPE,
        )
    )
    return steps


# Zava glossary terms attached to the certified gold data product (R6 §1, §6.5).
GLOSSARY_TERMS: Tuple[Tuple[str, str], ...] = (
    (
        "Customer PII",
        "Personally identifiable information for Zava rental customers (name, email, phone, "
        "drivers license, payment data). Classified during the Databricks UC scan; labeled "
        "'Highly Confidential \\ PII' on the Fabric/Power BI surface.",
    ),
    (
        "Certified Rental Gold Asset",
        "The certified Zava gold data asset (Databricks UC catalog 'zava', schema 'gold') mirrored "
        "into Fabric OneLake and surfaced via Direct Lake. The trusted source for rental analytics.",
    ),
    (
        "Rental Transaction",
        "A completed Zava car-rental transaction linking a customer, vehicle, site, and payment.",
    ),
)


def build_label_plan(s: Settings) -> List[PlanStep]:
    """Apply the PII sensitivity label to the Zava semantic model + report (R6 §6.4)."""
    datasets = [{"id": s.semantic_model_id}] if s.semantic_model_id else []
    reports = [{"id": s.report_id}] if s.report_id else []
    artifacts: Dict[str, Any] = {}
    if datasets:
        artifacts["datasets"] = datasets
    if reports:
        artifacts["reports"] = reports

    return [
        PlanStep(
            "Apply 'Highly Confidential \\ PII' label to Zava semantic model + report "
            "(Power BI Admin SetLabelsAsAdmin)",
            "POST",
            f"{POWERBI_ADMIN_BASE}/informationprotection/setLabels",
            POWERBI_SCOPE,
            {
                "artifacts": artifacts,
                "labelId": s.pii_label_id,
                "assignmentMethod": "Standard",
            },
        )
    ]


# ---------------------------------------------------------------------------
# Glossary term creation (Atlas v2 — needs a live GUID lookup, so handled apart from PlanSteps)
# ---------------------------------------------------------------------------

def _datamap_url(endpoint: str, suffix: str) -> str:
    return f"{endpoint}/datamap/api/atlas/v2/glossary{suffix}?api-version={DATAMAP_API_VERSION}"


def ensure_glossary_terms(client: PurviewClient, s: Settings) -> None:
    """Create the Zava glossary terms idempotently (R6 §6.5).

    Steps:
      1. ``GET  {endpoint}/datamap/api/atlas/v2/glossary``        -> default glossary GUID
      2. ``GET  .../glossary/{guid}/terms``                       -> existing term names (idempotency)
      3. ``POST {endpoint}/datamap/api/atlas/v2/glossary/term``   -> create missing terms (R6 §6.5)
    """
    ep = s.scan_endpoint

    glossaries = client.request("GET", _datamap_url(ep, ""), PURVIEW_SCOPE).json()
    if not glossaries:
        raise PurviewSetupError(
            "No glossary found in the Purview account; create a glossary first (Atlas seeds a "
            "default glossary automatically once the Data Map is initialized)."
        )
    glossary_guid = glossaries[0].get("guid")
    log.info("Using glossary GUID %s", glossary_guid)

    existing = client.request(
        "GET", _datamap_url(ep, f"/{glossary_guid}/terms"), PURVIEW_SCOPE, tolerate=(404,)
    )
    existing_names = set()
    if existing.status_code < 400:
        for term in existing.json() or []:
            name = term.get("name") or term.get("displayText")
            if name:
                existing_names.add(name)

    for name, definition in GLOSSARY_TERMS:
        if name in existing_names:
            log.info("Glossary term '%s' already exists — skipping (idempotent).", name)
            continue
        body = {
            "name": name,
            "anchor": {"glossaryGuid": glossary_guid},
            "longDescription": definition,
            "status": "Approved",
        }
        client.request("POST", _datamap_url(ep, "/term"), PURVIEW_SCOPE, body)
        log.info("Created glossary term '%s'.", name)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_scan_inputs(s: Settings) -> List[str]:
    missing: List[str] = []
    if not s.dbx_metastore_id:
        missing.append("PURVIEW_DBX_METASTORE_ID (Databricks metastore id)")
    if not s.dbx_workspace_url:
        missing.append("DATABRICKS_WORKSPACE_URL")
    if not s.dbx_sql_http_path:
        missing.append("DATABRICKS_SQL_WAREHOUSE_HTTP_PATH (running SQL Warehouse)")
    if not s.fabric_tenant_id:
        missing.append("AZURE_TENANT_ID (Fabric directory/tenant id)")
    return missing


def validate_label_inputs(s: Settings) -> List[str]:
    missing: List[str] = []
    if not s.pii_label_id:
        missing.append("PURVIEW_PII_LABEL_ID (published sensitivity-label GUID)")
    if not s.semantic_model_id and not s.report_id:
        missing.append("FABRIC_SEMANTIC_MODEL_ID and/or FABRIC_REPORT_ID (at least one item to label)")
    return missing


# ---------------------------------------------------------------------------
# Manual-step reminder (printed; the canonical list lives in the manual-steps appendix)
# ---------------------------------------------------------------------------

_MANUAL_REMINDERS = (
    "Governance domain(s) + data product for the certified gold asset — Purview portal UI "
    "(no REST CRUD as of 2026-06; R6 §6.6). Attach the glossary terms this script created.",
    "DLP policy for Fabric — Security & Compliance PowerShell (New-DlpCompliancePolicy + "
    "New-DlpComplianceRule) or compliance portal; use a CUSTOM policy (R6 §5, §6.6).",
    "Sensitivity-label taxonomy/definition (the label GUID this script applies) — "
    "compliance portal / PowerShell (R6 §6.6).",
    "Fabric tenant admin settings: 'service principals can use read-only admin APIs' + "
    "'OneLake external apps' — Fabric Admin Portal UI (R6 §3, §6.6).",
    "Databricks prereqs: add Purview MSI as workspace SP; run a SQL Warehouse; enable "
    "system.access schema + grants for lineage — Databricks UI/CLI (R6 §2).",
    "Purview live-view extended-permissions toggle — Purview portal UI (R6 §6.6).",
)


def print_manual_reminders() -> None:
    log.info("UI / PowerShell-only follow-ups (consolidated in the manual-steps appendix):")
    for i, item in enumerate(_MANUAL_REMINDERS, 1):
        log.info("  %d. %s", i, item)


# ---------------------------------------------------------------------------
# Dry-run printing
# ---------------------------------------------------------------------------

def print_plan(title: str, steps: List[PlanStep]) -> None:
    log.info("-- %s -- (%d call(s))", title, len(steps))
    for i, step in enumerate(steps, 1):
        log.info("  [%d] %s", i, step.title)
        log.info("      %s %s", step.method, step.url)
        if step.body is not None:
            body_text = json.dumps(step.body, indent=2)
            for line in body_text.splitlines():
                log.info("      %s", line)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_plan(client: PurviewClient, steps: List[PlanStep]) -> None:
    for step in steps:
        log.info("-> %s", step.title)
        client.request(step.method, step.url, step.scope, step.body, tolerate=step.tolerate)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Set up Microsoft Purview for the Zava demo (Step 22, R6): register + scan the "
            "Databricks Unity Catalog and Fabric tenant sources, create Zava glossary terms, and "
            "apply the customer-PII sensitivity label to Fabric/Power BI items. Idempotent; "
            "no secrets (auth via DefaultAzureCredential). UI/PowerShell-only actions are printed "
            "as reminders and deferred to the manual-steps appendix."
        )
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to deploy_config.json (default: fabric/config/deploy_config.json, then the "
        "committed .sample.json).",
    )
    parser.add_argument(
        "--only",
        choices=["all", "scans", "glossary", "labels"],
        default="all",
        help="Limit the run to one phase (default: all). 'scans' = sources+scans+triggers+runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve identifiers and print every endpoint + body WITHOUT calling any live API.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(getattr(logging, args.log_level))

    log.info("Purview setup (Step 22, R6) — endpoints/shapes from research, NOT invented.")

    try:
        cfg = _load_config(_resolve_config_path(args.config))
    except PurviewSetupError as exc:
        log.error("%s", exc)
        return 2

    settings = Settings(cfg)
    try:
        settings.require_account()
    except PurviewSetupError as exc:
        log.error("%s", exc)
        return 3

    do_scans = args.only in ("all", "scans")
    do_glossary = args.only in ("all", "glossary")
    do_labels = args.only in ("all", "labels")

    scan_steps = build_source_and_scan_plan(settings) if do_scans else []
    label_steps = build_label_plan(settings) if do_labels else []

    # --- DRY-RUN: print everything, call nothing -------------------------------------------
    if args.dry_run:
        log.info("DRY-RUN — no live API called. Endpoint host: %s", settings.scan_endpoint)
        if do_scans:
            print_plan("Sources + scans + triggers + runs (R6 §6.3)", scan_steps)
            missing = validate_scan_inputs(settings)
            if missing:
                log.warning("Scan inputs unresolved (set before a real run): %s", ", ".join(missing))
        if do_glossary:
            log.info(
                "-- Glossary terms (R6 §6.5) -- POST %s/datamap/api/atlas/v2/glossary/term",
                settings.scan_endpoint,
            )
            for name, definition in GLOSSARY_TERMS:
                log.info("  * %s — %s", name, definition)
            log.info("  (idempotent: GET glossary GUID + existing terms first; create only missing)")
        if do_labels:
            print_plan("Sensitivity labels (R6 §6.4)", label_steps)
            missing = validate_label_inputs(settings)
            if missing:
                log.warning("Label inputs unresolved (set before a real run): %s", ", ".join(missing))
        print_manual_reminders()
        return 0

    # --- REAL RUN -------------------------------------------------------------------------
    if do_scans:
        missing = validate_scan_inputs(settings)
        if missing:
            log.error("Cannot register/scan sources — missing identifier(s): %s", ", ".join(missing))
            return 4
    if do_labels:
        missing = validate_label_inputs(settings)
        if missing:
            log.error("Cannot apply labels — missing identifier(s): %s", ", ".join(missing))
            return 4

    try:
        client = PurviewClient()
    except ImportError as exc:
        log.error(
            "azure-identity / requests are required for a real run (pip install azure-identity "
            "requests). Use --dry-run to preview without them. (%s)",
            exc,
        )
        return 5

    try:
        if do_scans:
            log.info("Registering sources, creating scans/triggers, starting initial runs ...")
            execute_plan(client, scan_steps)
        if do_glossary:
            log.info("Creating Zava glossary terms (idempotent) ...")
            ensure_glossary_terms(client, settings)
        if do_labels:
            log.info("Applying the customer-PII sensitivity label ...")
            execute_plan(client, label_steps)
    except PurviewSetupError as exc:
        log.error("%s", exc)
        return 6

    log.info("Purview programmatic setup complete.")
    print_manual_reminders()
    return 0


if __name__ == "__main__":
    sys.exit(main())
