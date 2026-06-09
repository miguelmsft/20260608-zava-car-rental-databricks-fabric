#!/usr/bin/env python3
"""Create the Zava **Fabric IQ Ontology (preview)** item — and its auto **Graph (GA)** —
idempotently and as code (plan Step 16; R4).

What it does
------------
1. Resolves all identifiers from ``deploy_config.json`` (Step 1 schema): the target
   workspace (``workspace.use_existing`` / ``workspace.existing_workspace_id`` /
   ``workspace.name``, created by Step 10), the Step-10 **Lakehouse** that holds the
   gold tables (``semantic_model.lakehouse_name`` / ``shortcut.lakehouse_name`` /
   ``lakehouse.name`` / ``<source.databricks_catalog>_lakehouse``), the gold schema
   (``source.gold_schema``), and the Ontology display name. Honours the
   ``features.enable_ontology`` gate (this whole step runs only when that flag is
   true). CLI / environment overrides take precedence. **No secrets are read from
   config.**
2. Authenticates via ``DefaultAzureCredential`` (``az login`` / managed identity /
   env service principal), falling back to the Azure CLI
   (``az account get-access-token``) when ``azure-identity`` is absent. The audience
   is the Fabric control plane (``https://api.fabric.microsoft.com``).
3. Resolves the workspace id (Step 10) and the **Lakehouse item id** so the ontology
   data bindings point at concrete OneLake tables.
4. Loads ``fabric/ontology/ontology_definition.json`` (the Zava entity/relationship
   model), substitutes the deploy-time placeholder tokens
   (``__ZAVA_WORKSPACE_ID__`` / ``__ZAVA_LAKEHOUSE_ID__`` / ``__ZAVA_GOLD_SCHEMA__``),
   and **decomposes** it into the Fabric REST *create-with-definition* parts:

       .platform                                                  (item metadata)
       definition.json                                            (empty, required)
       EntityTypes/{entityTypeId}/definition.json                 (entity type)
       EntityTypes/{entityTypeId}/DataBindings/{guid}.json        (entity data binding)
       RelationshipTypes/{relTypeId}/definition.json              (relationship type)
       RelationshipTypes/{relTypeId}/Contextualizations/{guid}.json (relationship binding)

   Each part is InlineBase64-encoded (R4 §5.2; official Ontology item definition).
5. **Find-or-create the Ontology item** (idempotent) via the Fabric generic item
   create-with-definition endpoint — reused (definition updated in place via
   ``.../updateDefinition``) if an Ontology of the same display name already exists.
   When the ontology is created, a managed **Graph in Microsoft Fabric** item is
   **auto-created** (GA) — no separate Graph create call is needed (R4 §2.1/§2.3).

REST endpoints used (Fabric item-management APIs — R4 §5.2, R7 §2):
    POST   /v1/workspaces/{workspaceId}/items                          (create w/ definition)
    GET    /v1/workspaces/{workspaceId}/items?type=Ontology            (list / find-by-name)
    GET    /v1/workspaces/{workspaceId}/items?type=Lakehouse           (resolve lakehouse id)
    POST   /v1/workspaces/{workspaceId}/items/{itemId}/updateDefinition (idempotent update)

Primary path vs. this code-first fallback (R4 §2.1 — IMPORTANT)
---------------------------------------------------------------
The **primary, recommended** way to build the Zava ontology is to **"Generate an
ontology from the semantic model"** — but that generation action is **UI-ONLY**: at
the time of writing there is **no REST endpoint to trigger generation from a semantic
model** (R4 §2.1, "Generating from Semantic Model is a UI-only workflow"). When
generating from a semantic model, only **Direct Lake** mode supports full data
binding and querying (the Step-14 model is Direct Lake on OneLake, so it qualifies).

The UI click-path (documented for the customer; consolidated into
``docs/manual-steps.md`` by the Step-25 docs step, NOT by this script):
    1. Open the Fabric workspace -> the Step-14 Direct Lake semantic model
       ("Zava Fleet Analytics").
    2. Use the Fabric IQ "Generate ontology" action on the semantic model.
    3. Fabric creates the Ontology item AND its managed Graph automatically.

This script is the **code-first fallback** (graph-source path, which *is* fully
scriptable — R4 §5.2): it creates the Ontology item directly from the committed
``ontology_definition.json`` so the demo **degrades gracefully** if the UI generation
wobbles or for fully unattended redeploys. It produces the same Zava entity model
(RentalSite, Vehicle, VehicleClass, Customer, Reservation, Rental, Payment,
Maintenance) bound to the gold lakehouse tables.

Attaching the ontology as a **Fabric Data Agent data source** may be a **UI step** in
some tenants if the Data Agent REST source enum does not yet expose ontology (R4
§2.1/(f)). That attach is handled by **Step 17** (fabric/scripts/70_create_data_agent.py),
not here.

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names / ids / placeholders only); auth
  is acquired at runtime via ``DefaultAzureCredential`` / ``az login``.
* **Authoring phase:** do **not** run this against a live tenant unless you intend to
  create Fabric items. Use ``--dry-run`` to preview every intended REST call with
  **no** authentication and **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/60_create_ontology.py --dry-run

    # Create the Ontology (+ auto Graph) from deploy_config.json:
    python fabric/scripts/60_create_ontology.py

    # Fully explicit against a real (non-sample) config:
    python fabric/scripts/60_create_ontology.py \
        --workspace-name zava-fabric-ws \
        --ontology-name ZavaFleetOntology \
        --lakehouse-name zava_lakehouse \
        --gold-schema gold \
        --config fabric/config/deploy_config.json
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fabric REST control plane (R7 §2). Auth scope per the Fabric "using-fabric-apis" article.
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"

# Retry / long-running-operation tuning (Fabric throttling: HTTP 429 + Retry-After;
# 202-Accepted LROs expose Operation-Location + Retry-After — R7 §9).
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5
LRO_POLL_DEFAULT_SECONDS = 5
LRO_TIMEOUT_SECONDS = 600

# Repo-relative defaults.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)
ONTOLOGY_DEF_PATH = os.path.join(_REPO_ROOT, "fabric", "ontology", "ontology_definition.json")

# Placeholder tokens substituted into the ontology definition at deploy time.
TOKEN_WORKSPACE_ID = "__ZAVA_WORKSPACE_ID__"
TOKEN_LAKEHOUSE_ID = "__ZAVA_LAKEHOUSE_ID__"
TOKEN_GOLD_SCHEMA = "__ZAVA_GOLD_SCHEMA__"

DEFAULT_ONTOLOGY_NAME = "ZavaFleetOntology"
DEFAULT_GOLD_SCHEMA = "gold"
ONTOLOGY_ITEM_TYPE = "Ontology"
LAKEHOUSE_ITEM_TYPE = "Lakehouse"

# A value like "<WORKSPACE_GUID>" in the sample config is an unresolved placeholder.
_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

LOG = logging.getLogger("zava.fabric.ontology")


class OntologyError(RuntimeError):
    """Raised on unrecoverable problems (bad identity, auth failure, API error)."""


# ---------------------------------------------------------------------------
# Config / identity resolution
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
            raise OntologyError(f"--config path does not exist: {explicit}")
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
        raise OntologyError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OntologyError(f"config {path} must be a JSON object")
    return data


class OntologyPlan:
    """Resolved, validated plan for the Ontology operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_name: Optional[str],
        use_existing_workspace: bool,
        existing_workspace_id: Optional[str],
        ontology_name: str,
        lakehouse_name: Optional[str],
        lakehouse_id: Optional[str],
        gold_schema: str,
        enable_ontology: bool,
    ) -> None:
        self.config_path = config_path
        self.workspace_name = workspace_name
        self.use_existing_workspace = use_existing_workspace
        self.existing_workspace_id = existing_workspace_id
        self.ontology_name = ontology_name
        self.lakehouse_name = lakehouse_name
        self.lakehouse_id = lakehouse_id
        self.gold_schema = gold_schema
        self.enable_ontology = enable_ontology


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_name: Optional[str] = None,
    existing_workspace_id: Optional[str] = None,
    ontology_name: Optional[str] = None,
    lakehouse_name: Optional[str] = None,
    lakehouse_id: Optional[str] = None,
    gold_schema: Optional[str] = None,
) -> OntologyPlan:
    """Resolve the ontology operation plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable.
    Raises ``OntologyError`` naming any field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    sm_cfg = cfg.get("semantic_model", {}) if isinstance(cfg.get("semantic_model"), dict) else {}
    sc_cfg = cfg.get("shortcut", {}) if isinstance(cfg.get("shortcut"), dict) else {}
    lh_cfg = cfg.get("lakehouse", {}) if isinstance(cfg.get("lakehouse"), dict) else {}
    src_cfg = cfg.get("source", {}) if isinstance(cfg.get("source"), dict) else {}
    ont_cfg = cfg.get("ontology", {}) if isinstance(cfg.get("ontology"), dict) else {}
    feat_cfg = cfg.get("features", {}) if isinstance(cfg.get("features"), dict) else {}

    # Feature gate: this step runs only when enable_ontology=true (default true so a
    # fresh sample config is demonstrable).
    flag = feat_cfg.get("enable_ontology")
    enable_ontology = bool(flag) if isinstance(flag, bool) else True

    use_existing = bool(ws_cfg.get("use_existing"))
    name = workspace_name or _clean(ws_cfg.get("name")) or os.environ.get("FABRIC_WORKSPACE_NAME")
    existing_id = (
        existing_workspace_id
        or _clean(ws_cfg.get("existing_workspace_id"))
        or os.environ.get("FABRIC_WORKSPACE_ID")
    )

    ont_name = (
        ontology_name
        or _clean(ont_cfg.get("name"))
        or os.environ.get("FABRIC_ONTOLOGY_NAME")
        or DEFAULT_ONTOLOGY_NAME
    )

    # The Step-10 Lakehouse holds the gold star schema the ontology binds to. Reuse the
    # same resolution order as the Step-14 semantic model (30_create_semantic_model.py).
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
    if not lh_name and catalog:
        lh_name = f"{catalog}_lakehouse"  # matches the Step-12 shortcut default

    gold = (
        gold_schema
        or _clean(src_cfg.get("gold_schema"))
        or os.environ.get("FABRIC_GOLD_SCHEMA")
        or DEFAULT_GOLD_SCHEMA
    )

    # Validation.
    if use_existing:
        if not existing_id:
            raise OntologyError(
                "workspace.use_existing=true but no workspace id resolved "
                "(set workspace.existing_workspace_id, --workspace-id, or FABRIC_WORKSPACE_ID)"
            )
        if not _GUID_RE.match(existing_id):
            raise OntologyError(f"resolved existing workspace id is not a GUID: {existing_id!r}")
    elif not name:
        raise OntologyError(
            "could not resolve workspace name "
            "(set workspace.name, --workspace-name, or FABRIC_WORKSPACE_NAME)"
        )

    if lh_id and not _GUID_RE.match(lh_id):
        raise OntologyError(f"resolved lakehouse id is not a GUID: {lh_id!r}")
    if not lh_id and not lh_name:
        raise OntologyError(
            "could not resolve the Step-10 Lakehouse that holds the gold tables "
            "(set semantic_model.lakehouse_name / shortcut.lakehouse_name / lakehouse.name / "
            "source.databricks_catalog -> <catalog>_lakehouse, or --lakehouse-name/--lakehouse-id)."
        )

    return OntologyPlan(
        config_path=resolved_path,
        workspace_name=name,
        use_existing_workspace=use_existing,
        existing_workspace_id=existing_id,
        ontology_name=ont_name,
        lakehouse_name=lh_name,
        lakehouse_id=lh_id,
        gold_schema=gold,
        enable_ontology=enable_ontology,
    )


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential, with az CLI fallback)
# ---------------------------------------------------------------------------

def get_access_token(resource: str = FABRIC_RESOURCE) -> str:
    """Acquire a bearer token for ``resource`` via DefaultAzureCredential; fall back to ``az``.

    No secret is ever read from or written to disk: this relies on an existing
    ``az login``, a managed identity, or environment service-principal credentials.
    """
    scope = resource.rstrip("/") + "/.default"
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore import-not-found
    except ImportError:
        LOG.debug("azure-identity not installed; falling back to `az account get-access-token`")
        return _get_token_via_az_cli(resource)

    try:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        return credential.get_token(scope).token
    except Exception as exc:  # noqa: BLE001 - surface any auth failure uniformly
        LOG.debug("DefaultAzureCredential failed (%s); falling back to az CLI", exc)
        return _get_token_via_az_cli(resource)


def _get_token_via_az_cli(resource: str) -> str:
    try:
        proc = subprocess.run(
            ["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise OntologyError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI (`az`) "
            "is available. Install azure-identity or run `az login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise OntologyError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise OntologyError("could not parse access token from az CLI output") from exc
    return token


# ---------------------------------------------------------------------------
# REST helpers (stdlib only) with retry/backoff + LRO polling
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    token: str,
    body: Optional[dict] = None,
    *,
    content_type: str = "application/json",
    accept: str = "application/json",
) -> Tuple[int, Dict[str, str], dict]:
    """Issue a single REST call, retrying on throttling (429) and transient 5xx.

    Returns (status_code, response_headers, json_payload). Raises ``OntologyError``
    on a non-retryable HTTP error or once retries are exhausted.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(url=url, method=method, data=data)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", content_type)
        request.add_header("Accept", accept)
        try:
            with urllib.request.urlopen(request) as response:  # noqa: S310 - fixed hosts
                status = response.status
                headers = {k.lower(): v for k, v in response.headers.items()}
                raw = response.read().decode("utf-8") or ""
                payload = json.loads(raw) if raw.strip() else {}
                return status, headers, payload
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt <= MAX_RETRIES:
                    delay = _retry_delay(retry_after, attempt)
                    LOG.warning(
                        "%s %s -> HTTP %s; retrying in %ss (attempt %s/%s)",
                        method, url, exc.code, delay, attempt, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
            raise OntologyError(
                f"{method} {url} failed: HTTP {exc.code} {exc.reason} {body_text}".strip()
            ) from exc
        except urllib.error.URLError as exc:
            if attempt <= MAX_RETRIES:
                delay = _retry_delay(None, attempt)
                LOG.warning(
                    "%s %s network error (%s); retrying in %ss (attempt %s/%s)",
                    method, url, exc.reason, delay, attempt, MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            raise OntologyError(f"{method} {url} failed: {exc.reason}") from exc


def _retry_delay(retry_after: Optional[str], attempt: int) -> int:
    if retry_after:
        try:
            return max(1, int(float(retry_after)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _poll_long_running(
    headers: Dict[str, str], token: str, *, timeout: int = LRO_TIMEOUT_SECONDS
) -> dict:
    """Poll a Fabric long-running operation (202 + Operation-Location) until terminal state."""
    op_url = headers.get("operation-location") or headers.get("location")
    if not op_url:
        return {}
    poll_interval = LRO_POLL_DEFAULT_SECONDS
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            poll_interval = max(1, int(float(retry_after)))
        except (TypeError, ValueError):
            pass

    deadline = time.monotonic() + timeout
    while True:
        _, op_headers, payload = _request("GET", op_url, token)
        state = str(payload.get("status", "")).lower()
        LOG.debug("LRO %s -> status=%s", op_url, state or "<none>")
        if state in ("succeeded", "completed"):
            result_url = op_headers.get("location")
            if result_url and result_url != op_url:
                _, _, result = _request("GET", result_url, token)
                return result
            return payload
        if state in ("failed", "canceled", "cancelled"):
            raise OntologyError(
                f"Fabric long-running operation {op_url} ended in state {state!r}: {payload}"
            )
        if time.monotonic() >= deadline:
            raise OntologyError(
                f"timed out after {timeout}s waiting for Fabric operation {op_url} "
                f"(last state {state!r})"
            )
        ra = op_headers.get("retry-after")
        if ra:
            try:
                poll_interval = max(1, int(float(ra)))
            except (TypeError, ValueError):
                pass
        time.sleep(poll_interval)


def _list_paginated(path: str, token: str) -> List[dict]:
    """Return all ``value`` entries across a paginated Fabric list endpoint."""
    items: List[dict] = []
    url = f"{FABRIC_API_BASE}{path}"
    seen_tokens = set()
    while url:
        _, _, payload = _request("GET", url, token)
        items.extend(payload.get("value", []) or [])
        cont_uri = payload.get("continuationUri")
        cont_token = payload.get("continuationToken")
        if cont_uri:
            url = cont_uri
        elif cont_token and cont_token not in seen_tokens:
            seen_tokens.add(cont_token)
            base = f"{FABRIC_API_BASE}{path}"
            sep = "&" if "?" in base else "?"
            url = f"{base}{sep}continuationToken={urllib.parse.quote(cont_token)}"
        else:
            url = ""
    return items


def _create_item_lro(path: str, token: str, body: dict) -> dict:
    """POST a create call; if it returns 202 (async), poll to completion and return the result."""
    status, headers, payload = _request("POST", f"{FABRIC_API_BASE}{path}", token, body=body)
    if status == 202:
        result = _poll_long_running(headers, token)
        return result or payload
    return payload


# ---------------------------------------------------------------------------
# Workspace / Lakehouse / Ontology operations (find-or-create)
# ---------------------------------------------------------------------------

def resolve_workspace_id(token: str, plan: OntologyPlan) -> str:
    """Resolve the target workspace id (existing path by id, fresh path by display name)."""
    if plan.use_existing_workspace:
        _, _, ws = _request(
            "GET", f"{FABRIC_API_BASE}/workspaces/{plan.existing_workspace_id}", token
        )
        ws_id = str(ws.get("id") or plan.existing_workspace_id)
        LOG.info("Using existing workspace %s (%s).", ws.get("displayName"), ws_id)
        return ws_id
    target = plan.workspace_name.strip().lower()  # type: ignore[union-attr]
    for ws in _list_paginated("/workspaces", token):
        if str(ws.get("displayName", "")).strip().lower() == target:
            LOG.info("Resolved workspace %r -> %s.", plan.workspace_name, ws.get("id"))
            return str(ws["id"])
    raise OntologyError(
        f"workspace {plan.workspace_name!r} not found — run Step 10 "
        f"(fabric/scripts/00_create_workspace.py) first."
    )


def _find_item_by_name(items: List[dict], name: str) -> Optional[dict]:
    target = name.strip().lower()
    for item in items:
        if str(item.get("displayName", "")).strip().lower() == target:
            return item
    return None


def resolve_lakehouse_id(token: str, workspace_id: str, plan: OntologyPlan) -> str:
    """Resolve the Step-10 Lakehouse item id (by id passthrough or display-name lookup)."""
    if plan.lakehouse_id:
        LOG.info("Using configured lakehouse id %s.", plan.lakehouse_id)
        return plan.lakehouse_id
    items = _list_paginated(
        f"/workspaces/{workspace_id}/items?type={LAKEHOUSE_ITEM_TYPE}", token
    )
    existing = _find_item_by_name(items, plan.lakehouse_name)  # type: ignore[arg-type]
    if existing:
        LOG.info("Resolved lakehouse %r -> %s.", plan.lakehouse_name, existing.get("id"))
        return str(existing["id"])
    raise OntologyError(
        f"lakehouse {plan.lakehouse_name!r} not found in workspace {workspace_id} — run Step 10/12 "
        f"(create the Lakehouse + gold shortcut) first, or pass --lakehouse-id."
    )


# ---------------------------------------------------------------------------
# Ontology definition assembly (decompose ontology_definition.json -> Fabric parts)
# ---------------------------------------------------------------------------

def _to_b64(obj: object) -> str:
    return base64.b64encode(json.dumps(obj, indent=2).encode("utf-8")).decode("ascii")


def _strip_keys(obj, drop: Tuple[str, ...]):
    """Recursively drop authoring-only keys (e.g. ``_comment``) and the supplied keys."""
    if isinstance(obj, dict):
        return {k: _strip_keys(v, drop) for k, v in obj.items()
                if k != "_comment" and k not in drop}
    if isinstance(obj, list):
        return [_strip_keys(v, drop) for v in obj]
    return obj


def load_ontology_document(
    workspace_id: str, lakehouse_id: str, gold_schema: str
) -> dict:
    """Load ontology_definition.json and substitute the deploy-time placeholder tokens."""
    if not os.path.isfile(ONTOLOGY_DEF_PATH):
        raise OntologyError(f"ontology definition not found: {ONTOLOGY_DEF_PATH}")
    with open(ONTOLOGY_DEF_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace(TOKEN_WORKSPACE_ID, workspace_id)
    text = text.replace(TOKEN_LAKEHOUSE_ID, lakehouse_id)
    text = text.replace(TOKEN_GOLD_SCHEMA, gold_schema)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OntologyError(f"ontology_definition.json is not valid JSON: {exc}") from exc


def build_definition_parts(doc: dict, ontology_name: str) -> List[dict]:
    """Decompose the Zava ontology document into Fabric create-with-definition parts.

    Produces the directory-structured parts documented in the official Ontology item
    definition (R4 §5.2): ``.platform``, ``definition.json``, per-entity-type
    ``definition.json`` + ``DataBindings/{guid}.json``, and per-relationship-type
    ``definition.json`` + ``Contextualizations/{guid}.json``. Each payload is
    InlineBase64-encoded JSON.
    """
    parts: List[dict] = []

    # Item metadata + the required empty root definition.
    platform = {"metadata": {"type": ONTOLOGY_ITEM_TYPE, "displayName": ontology_name}}
    parts.append({"path": ".platform", "payload": _to_b64(platform), "payloadType": "InlineBase64"})
    parts.append({"path": "definition.json", "payload": _to_b64({}), "payloadType": "InlineBase64"})

    # Entity types + their data bindings.
    for entity in doc.get("entityTypes", []):
        entity_id = str(entity.get("id"))
        if not entity_id:
            raise OntologyError(f"entity type missing id: {entity.get('name')!r}")
        binding = entity.get("dataBinding")
        entity_def = _strip_keys(entity, ("dataBinding",))
        parts.append({
            "path": f"EntityTypes/{entity_id}/definition.json",
            "payload": _to_b64(entity_def),
            "payloadType": "InlineBase64",
        })
        if binding:
            binding_obj = _strip_keys(binding, ())
            binding_id = str(binding_obj.get("id") or entity_id)
            parts.append({
                "path": f"EntityTypes/{entity_id}/DataBindings/{binding_id}.json",
                "payload": _to_b64(binding_obj),
                "payloadType": "InlineBase64",
            })

    # Relationship types + their contextualizations (relationship data bindings).
    for rel in doc.get("relationshipTypes", []):
        rel_id = str(rel.get("id"))
        if not rel_id:
            raise OntologyError(f"relationship type missing id: {rel.get('name')!r}")
        contextualization = rel.get("contextualization")
        rel_def = _strip_keys(rel, ("contextualization",))
        parts.append({
            "path": f"RelationshipTypes/{rel_id}/definition.json",
            "payload": _to_b64(rel_def),
            "payloadType": "InlineBase64",
        })
        if contextualization:
            ctx_obj = _strip_keys(contextualization, ())
            ctx_id = str(ctx_obj.get("id") or rel_id)
            parts.append({
                "path": f"RelationshipTypes/{rel_id}/Contextualizations/{ctx_id}.json",
                "payload": _to_b64(ctx_obj),
                "payloadType": "InlineBase64",
            })

    return parts


def find_or_create_ontology(
    token: str, workspace_id: str, name: str, parts: List[dict]
) -> dict:
    """Find-or-create the Ontology item; update its definition in place if it exists.

    Creating the ontology auto-creates the managed Graph in Fabric (GA) — no separate
    Graph create is required (R4 §2.1/§2.3).
    """
    definition = {"parts": parts}
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/items?type={ONTOLOGY_ITEM_TYPE}", token),
        name,
    )
    if existing:
        item_id = str(existing.get("id"))
        LOG.info("Ontology %r already exists (%s) — updating definition (idempotent).",
                 name, item_id)
        status, headers, _ = _request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}/updateDefinition",
            token,
            body={"definition": definition},
        )
        if status == 202:
            _poll_long_running(headers, token)
        return existing
    LOG.info("Creating Ontology %r (+ auto Graph) ...", name)
    created = _create_item_lro(
        f"/workspaces/{workspace_id}/items",
        token,
        {
            "displayName": name,
            "type": ONTOLOGY_ITEM_TYPE,
            "description": "Zava car-rental business ontology over the Step-14 Direct Lake "
                           "semantic model / gold lakehouse (plan Step 16).",
            "definition": definition,
        },
    )
    LOG.info("Created Ontology %r (%s). Managed Graph auto-created (GA).",
             name, created.get("id"))
    return created


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def run(plan: OntologyPlan, *, dry_run: bool = False) -> int:
    """Execute the Ontology create/update flow. Returns an exit code."""
    if not plan.enable_ontology:
        LOG.info(
            "features.enable_ontology=false — skipping Ontology provisioning. "
            "Set enable_ontology=true to enable Step 16 (Ontology + auto Graph)."
        )
        return 0

    if dry_run:
        return _print_dry_run(plan)

    token = get_access_token()

    # 1. Resolve the Step-10 workspace + Lakehouse (data-binding target).
    workspace_id = resolve_workspace_id(token, plan)
    lakehouse_id = resolve_lakehouse_id(token, workspace_id, plan)

    # 2. Load + token-substitute the committed ontology document, then decompose to parts.
    doc = load_ontology_document(workspace_id, lakehouse_id, plan.gold_schema)
    parts = build_definition_parts(doc, plan.ontology_name)
    LOG.info(
        "Assembled %d definition part(s) for %d entity type(s) and %d relationship type(s).",
        len(parts), len(doc.get("entityTypes", [])), len(doc.get("relationshipTypes", [])),
    )

    # 3. Find-or-create the Ontology item (auto-creates the managed Graph).
    ontology = find_or_create_ontology(token, workspace_id, plan.ontology_name, parts)

    LOG.info(
        "Done. workspace_id=%s lakehouse_id=%s ontology_id=%s",
        workspace_id, lakehouse_id, ontology.get("id"),
    )
    LOG.info(
        "Note: the *recommended* primary path is the UI-only 'Generate ontology from "
        "semantic model' action (R4 §2.1); this script is the code-first graph-source "
        "fallback. Attaching the ontology as a Fabric Data Agent source may be a UI step "
        "and is handled by Step 17 (fabric/scripts/70_create_data_agent.py)."
    )
    return 0


def _print_dry_run(plan: OntologyPlan) -> int:
    """Credential-free preview of every intended REST call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    LOG.info("[DRY-RUN] enable_ontology=%s", plan.enable_ontology)
    if not plan.enable_ontology:
        LOG.info("[DRY-RUN] Feature gate off — nothing would be created.")
        return 0
    if plan.use_existing_workspace:
        LOG.info("[DRY-RUN] Would GET %s/workspaces/%s (existing).",
                 FABRIC_API_BASE, plan.existing_workspace_id)
    else:
        LOG.info("[DRY-RUN] Would GET %s/workspaces to resolve %r -> id.",
                 FABRIC_API_BASE, plan.workspace_name)
    if plan.lakehouse_id:
        LOG.info("[DRY-RUN] Would use configured lakehouse id %s.", plan.lakehouse_id)
    else:
        LOG.info("[DRY-RUN] Would GET %s/workspaces/{id}/items?type=Lakehouse to resolve %r -> id.",
                 FABRIC_API_BASE, plan.lakehouse_name)
    # Assemble the parts offline (placeholder ids) to validate the definition structure.
    try:
        doc = load_ontology_document(
            plan.existing_workspace_id or TOKEN_WORKSPACE_ID,
            plan.lakehouse_id or TOKEN_LAKEHOUSE_ID,
            plan.gold_schema,
        )
        parts = build_definition_parts(doc, plan.ontology_name)
        LOG.info("[DRY-RUN] ontology_definition.json -> %d definition part(s) "
                 "(%d entity type(s), %d relationship type(s)).",
                 len(parts), len(doc.get("entityTypes", [])),
                 len(doc.get("relationshipTypes", [])))
        for part in parts:
            LOG.info("[DRY-RUN]   part: %s", part["path"])
    except OntologyError as exc:
        LOG.warning("[DRY-RUN] could not assemble definition parts: %s", exc)
    LOG.info("[DRY-RUN] Would find-or-create Ontology %r "
             "(POST %s/workspaces/{id}/items, type=Ontology, InlineBase64 definition; "
             "idempotent updateDefinition on re-run). Creating it auto-creates the "
             "managed Graph (GA).", plan.ontology_name, FABRIC_API_BASE)
    LOG.info("[DRY-RUN] Primary path (UI-only, R4 §2.1): 'Generate ontology from semantic "
             "model' on the Step-14 Direct Lake model — documented in docs/manual-steps.md "
             "(Step-25 docs step). Data Agent attach is Step 17.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the Zava Fabric IQ Ontology (+ auto Graph) from "
                    "ontology_definition.json (Fabric item-management REST APIs).",
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
    parser.add_argument("--ontology-name", help="Ontology display name (overrides config/env).")
    parser.add_argument("--lakehouse-name", help="Step-10 Lakehouse display name (data-binding source).")
    parser.add_argument("--lakehouse-id", help="Step-10 Lakehouse GUID (data-binding source).")
    parser.add_argument("--gold-schema", help="Gold schema holding the dims/fact (default 'gold').")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print intended REST calls and the assembled definition parts without "
             "authenticating or mutating anything.",
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
            ontology_name=args.ontology_name,
            lakehouse_name=args.lakehouse_name,
            lakehouse_id=args.lakehouse_id,
            gold_schema=args.gold_schema,
        )
        return run(plan, dry_run=args.dry_run)
    except OntologyError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
