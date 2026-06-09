#!/usr/bin/env python3
"""Create the Zava Fabric **Activator (Reflex)** native **Email** alert — idempotently
and as code (plan Step 19; R11 §6c). **DEFAULT, Teams-free** watch+act.

What it does
------------
1. Resolves all identifiers from ``deploy_config.json`` (Step 1 schema): the target
   workspace (``workspace.use_existing`` / ``workspace.existing_workspace_id`` /
   ``workspace.name``, created by Step 10), the Real-Time Intelligence names it binds
   to (``realtime.kql_database_name`` / ``realtime.kql_table_name`` — the Step-18
   Eventhouse property source), and the alert recipient
   (``alerting.site_manager_email`` — a **placeholder** address). Honours the
   ``features.enable_activator_email`` gate (default ``true``; requires
   ``enable_eventhouse=true``). CLI / environment overrides take precedence.
   **No secrets are read from config.**
2. Authenticates via ``DefaultAzureCredential`` (``az login`` / managed identity /
   env service principal), falling back to the Azure CLI
   (``az account get-access-token``) when ``azure-identity`` is absent. A single
   audience is used: the Fabric control plane (``https://api.fabric.microsoft.com``).
3. **Resolves the Step-18 KQL database item id** (by ``realtime.kql_database_name``)
   so the Reflex rule's default, Teams-free data source binds to the live Eventhouse
   property (R11 §6c — Activator monitors Eventhouse properties). When
   ``features.enable_ontology=true`` the same rule can be enriched with a Fabric
   Ontology business-entity binding (preview) — see the commented note in
   ``fabric/activator/reflex_entities.json``; the default deploy stays Eventhouse-only
   so it works even when the only preview item (Ontology) is unavailable.
4. **Find-or-create the Reflex (Activator) item** (idempotent) and **deploy the
   definition as code** via ``POST /v1/workspaces/{id}/reflexes`` with
   ``definition.format="json"`` and a single ``ReflexEntities.json`` part
   (InlineBase64) + a ``.platform`` part (R11 §6c ``deploy_email_reflex`` pattern).
   On a subsequent run the existing Reflex's definition is updated in place
   (``.../updateDefinition``) — no duplicate item.

The ``ReflexEntities.json`` rule body comes from ``fabric/activator/reflex_entities.json``.
Its action kind is **``EmailMessage``** with the verbatim configuration properties
``messageLocale`` / ``sentTo`` / ``copyTo`` / ``bCCTo`` / ``subject`` / ``headline`` /
``optionalMessage`` / ``additionalInformation`` (R11 §6c). Placeholder tokens
(``__ZAVA_SITE_MANAGER_EMAIL__`` / ``__ZAVA_KQL_DATABASE_NAME__`` /
``__ZAVA_KQL_TABLE_NAME__``) and the all-zero GUIDs (workspace id / KQL database item
id / optional work-order Fabric item id) are substituted at deploy time.

Optional Fabric-item action (Teams-free; R11 §6c/§9)
----------------------------------------------------
The definition also contains an **optional** Activator ``FabricItemInvocation`` rule
that runs a Zava ``DataPipeline``/``Notebook`` to write a **work-order row** (passing
``SiteId``/``VehicleId`` parameters). It is **included only when**
``alerting.work_order_item_id`` is configured with a real Fabric item GUID; otherwise
the rule is dropped so the default deploy stays **Email-only**. **Email is always the
default action.**

REST endpoints used (Fabric Reflex / Activator + item-management APIs — R11 §6c):
    GET    /v1/workspaces/{workspaceId}/kqlDatabases                       (resolve source id)
    POST   /v1/workspaces/{workspaceId}/reflexes                           (create w/ definition)
    GET    /v1/workspaces/{workspaceId}/reflexes                           (list / find-by-name)
    POST   /v1/workspaces/{workspaceId}/reflexes/{id}/updateDefinition     (idempotent update)

Activator design-mode nuance (R11 §6c; plan Step 19 manual note)
----------------------------------------------------------------
Authoring/validating the rule in Activator **design mode** (Monitor -> Condition ->
Email Action) is **UI-assisted**, but the Reflex item + ``EmailMessage`` definition
deploy as code. Verify the ``ReflexEntities.json`` rule body against the live Reflex
schema (a smoke test) before relying on the scripted path. **No Teams required.**

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names / ids / placeholders only); auth
  is acquired at runtime via ``DefaultAzureCredential`` / ``az login``. The recipient
  ``alerting.site_manager_email`` is a placeholder and is never a real address in this
  repo.
* **Authoring phase:** do **not** run this against a live tenant unless you intend to
  create Fabric items. Use ``--dry-run`` to preview every intended REST call with
  **no** authentication and **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/78_create_activator_email.py --dry-run

    # Create/refresh the Activator email alert from deploy_config.json:
    python fabric/scripts/78_create_activator_email.py

    # Fully explicit against a real (non-sample) config:
    python fabric/scripts/78_create_activator_email.py \
        --workspace-name zava-fabric-ws \
        --kql-database-name zava_rt \
        --kql-table-name Telematics \
        --site-manager-email manager@zava.example \
        --reflex-name "Zava Email Alert" \
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

# Fabric REST control plane (R7 §2 / R11 §6c). Auth scope per the Fabric APIs article.
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

# Retry / long-running-operation tuning (Fabric throttling: HTTP 429 + Retry-After;
# 202-Accepted LROs expose Operation-Location + Retry-After — R7 §9).
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5
LRO_POLL_DEFAULT_SECONDS = 5
LRO_TIMEOUT_SECONDS = 600

# JSON-schema id for the .platform part (Fabric git-integration schema).
PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)
# Fabric item type for an Activator (Reflex) item.
REFLEX_ITEM_TYPE = "Reflex"

# Repo-relative defaults.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)
REFLEX_ENTITIES_PATH = os.path.join(_REPO_ROOT, "fabric", "activator", "reflex_entities.json")

# Placeholder tokens substituted into the ReflexEntities.json definition at deploy time.
TOKEN_SITE_MANAGER_EMAIL = "__ZAVA_SITE_MANAGER_EMAIL__"
TOKEN_KQL_DATABASE_NAME = "__ZAVA_KQL_DATABASE_NAME__"
TOKEN_KQL_TABLE_NAME = "__ZAVA_KQL_TABLE_NAME__"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# A value like "<WORKSPACE_GUID>" in the sample config is an unresolved placeholder.
_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

LOG = logging.getLogger("zava.fabric.activator")


class ActivatorError(RuntimeError):
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
            raise ActivatorError(f"--config path does not exist: {explicit}")
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
        raise ActivatorError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ActivatorError(f"config {path} must be a JSON object")
    return data


class ActivatorPlan:
    """Resolved, validated plan for the Activator email-alert operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_name: Optional[str],
        use_existing_workspace: bool,
        existing_workspace_id: Optional[str],
        kql_database_name: str,
        kql_table_name: str,
        site_manager_email: str,
        reflex_name: str,
        work_order_item_id: Optional[str],
        enable_activator_email: bool,
        enable_eventhouse: bool,
        enable_ontology: bool,
    ) -> None:
        self.config_path = config_path
        self.workspace_name = workspace_name
        self.use_existing_workspace = use_existing_workspace
        self.existing_workspace_id = existing_workspace_id
        self.kql_database_name = kql_database_name
        self.kql_table_name = kql_table_name
        self.site_manager_email = site_manager_email
        self.reflex_name = reflex_name
        self.work_order_item_id = work_order_item_id
        self.enable_activator_email = enable_activator_email
        self.enable_eventhouse = enable_eventhouse
        self.enable_ontology = enable_ontology


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_name: Optional[str] = None,
    existing_workspace_id: Optional[str] = None,
    kql_database_name: Optional[str] = None,
    kql_table_name: Optional[str] = None,
    site_manager_email: Optional[str] = None,
    reflex_name: Optional[str] = None,
    work_order_item_id: Optional[str] = None,
) -> ActivatorPlan:
    """Resolve the Activator operation plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable.
    Raises ``ActivatorError`` naming any field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    rt_cfg = cfg.get("realtime", {}) if isinstance(cfg.get("realtime"), dict) else {}
    al_cfg = cfg.get("alerting", {}) if isinstance(cfg.get("alerting"), dict) else {}
    feat_cfg = cfg.get("features", {}) if isinstance(cfg.get("features"), dict) else {}

    # Feature gates. enable_activator_email defaults true (Teams-free watch+act);
    # it requires enable_eventhouse=true (shared Eventhouse/KQL source). enable_ontology
    # toggles only the OPTIONAL ontology-entity enrichment note in the definition.
    flag = feat_cfg.get("enable_activator_email")
    enable_activator_email = bool(flag) if isinstance(flag, bool) else True
    eh_flag = feat_cfg.get("enable_eventhouse")
    enable_eventhouse = bool(eh_flag) if isinstance(eh_flag, bool) else True
    ont_flag = feat_cfg.get("enable_ontology")
    enable_ontology = bool(ont_flag) if isinstance(ont_flag, bool) else False

    use_existing = bool(ws_cfg.get("use_existing"))
    name = workspace_name or _clean(ws_cfg.get("name")) or os.environ.get("FABRIC_WORKSPACE_NAME")
    existing_id = (
        existing_workspace_id
        or _clean(ws_cfg.get("existing_workspace_id"))
        or os.environ.get("FABRIC_WORKSPACE_ID")
    )

    kql_db = (
        kql_database_name
        or _clean(rt_cfg.get("kql_database_name"))
        or os.environ.get("FABRIC_KQL_DATABASE_NAME")
    )
    kql_table = (
        kql_table_name
        or _clean(rt_cfg.get("kql_table_name"))
        or os.environ.get("FABRIC_KQL_TABLE_NAME")
        or "Telematics"
    )
    email = (
        site_manager_email
        or _clean(al_cfg.get("site_manager_email"))
        or os.environ.get("ZAVA_SITE_MANAGER_EMAIL")
    )
    # Reflex display name: derive from the KQL database name unless overridden.
    name_reflex = (
        reflex_name
        or _clean(al_cfg.get("reflex_name"))
        or os.environ.get("FABRIC_REFLEX_NAME")
        or (f"{kql_db}-email-alert" if kql_db else None)
    )
    # Optional work-order Fabric item id (enables the FabricItemInvocation rule).
    work_order = (
        work_order_item_id
        or _clean(al_cfg.get("work_order_item_id"))
        or os.environ.get("ZAVA_WORK_ORDER_ITEM_ID")
    )

    # Validation.
    if use_existing:
        if not existing_id:
            raise ActivatorError(
                "workspace.use_existing=true but no workspace id resolved "
                "(set workspace.existing_workspace_id, --workspace-id, or FABRIC_WORKSPACE_ID)"
            )
        if not _GUID_RE.match(existing_id):
            raise ActivatorError(f"resolved existing workspace id is not a GUID: {existing_id!r}")
    elif not name:
        raise ActivatorError(
            "could not resolve workspace name "
            "(set workspace.name, --workspace-name, or FABRIC_WORKSPACE_NAME)"
        )

    if not kql_db:
        raise ActivatorError(
            "could not resolve KQL database name "
            "(set realtime.kql_database_name, --kql-database-name, or FABRIC_KQL_DATABASE_NAME)"
        )
    if not email:
        raise ActivatorError(
            "could not resolve the alert recipient "
            "(set alerting.site_manager_email, --site-manager-email, or ZAVA_SITE_MANAGER_EMAIL) "
            "— use a placeholder address; never commit a real one"
        )
    if not name_reflex:
        raise ActivatorError(
            "could not resolve the Reflex (Activator) display name "
            "(set alerting.reflex_name, --reflex-name, or FABRIC_REFLEX_NAME)"
        )
    if work_order and not _GUID_RE.match(work_order):
        raise ActivatorError(
            f"resolved alerting.work_order_item_id is not a GUID: {work_order!r}"
        )

    return ActivatorPlan(
        config_path=resolved_path,
        workspace_name=name,
        use_existing_workspace=use_existing,
        existing_workspace_id=existing_id,
        kql_database_name=kql_db,
        kql_table_name=kql_table,
        site_manager_email=email,
        reflex_name=name_reflex,
        work_order_item_id=work_order,
        enable_activator_email=enable_activator_email,
        enable_eventhouse=enable_eventhouse,
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
        raise ActivatorError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI (`az`) "
            "is available. Install azure-identity or run `az login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ActivatorError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ActivatorError("could not parse access token from az CLI output") from exc
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

    Returns (status_code, response_headers, json_payload). Raises ``ActivatorError``
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
            raise ActivatorError(
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
            raise ActivatorError(f"{method} {url} failed: {exc.reason}") from exc


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
            raise ActivatorError(
                f"Fabric long-running operation {op_url} ended in state {state!r}: {payload}"
            )
        if time.monotonic() >= deadline:
            raise ActivatorError(
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
# Workspace / KQL-database resolution + ReflexEntities binding
# ---------------------------------------------------------------------------

def resolve_workspace_id(token: str, plan: ActivatorPlan) -> str:
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
    raise ActivatorError(
        f"workspace {plan.workspace_name!r} not found — run Step 10 "
        f"(fabric/scripts/00_create_workspace.py) first."
    )


def _find_item_by_name(items: List[dict], name: str) -> Optional[dict]:
    target = name.strip().lower()
    for item in items:
        if str(item.get("displayName", "")).strip().lower() == target:
            return item
    return None


def resolve_kql_database_id(token: str, workspace_id: str, name: str) -> str:
    """Resolve the Step-18 KQL database item id by display name (the email rule's source)."""
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/kqlDatabases", token), name
    )
    if not existing:
        raise ActivatorError(
            f"KQL database {name!r} not found in workspace {workspace_id} — run Step 18 "
            f"(fabric/scripts/75_create_eventhouse.py) first so the Activator email rule can "
            f"bind to the Eventhouse property."
        )
    LOG.info("Resolved KQL database %r -> %s.", name, existing.get("id"))
    return str(existing["id"])


def _strip_meta(obj):
    """Recursively drop authoring-only keys (``_comment*``, ``_optional_requires``)."""
    if isinstance(obj, dict):
        return {
            k: _strip_meta(v)
            for k, v in obj.items()
            if not (k == "_optional_requires" or k.startswith("_comment"))
        }
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def load_reflex_entities(plan: ActivatorPlan, kql_database_id: str, workspace_id: str) -> dict:
    """Load reflex_entities.json, substitute deploy-time tokens, and bind the source.

    * ``__ZAVA_SITE_MANAGER_EMAIL__`` -> the placeholder recipient.
    * ``__ZAVA_KQL_DATABASE_NAME__`` / ``__ZAVA_KQL_TABLE_NAME__`` -> the RTI names.
    * zero-GUID ``workspaceId``/``itemId`` on the Eventhouse data source -> the real ids.
    * The optional ``FabricItemInvocation`` work-order rule is kept ONLY when
      ``alerting.work_order_item_id`` is configured (its zero-GUID ``itemId`` is then
      substituted); otherwise it is dropped so the default deploy is Email-only.
    """
    with open(REFLEX_ENTITIES_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace(TOKEN_SITE_MANAGER_EMAIL, plan.site_manager_email)
    text = text.replace(TOKEN_KQL_DATABASE_NAME, plan.kql_database_name)
    text = text.replace(TOKEN_KQL_TABLE_NAME, plan.kql_table_name)
    entities = json.loads(text)

    # Bind the default (Teams-free) Eventhouse data source to the live ids.
    for source in entities.get("dataSources", []):
        props = source.get("properties")
        if isinstance(props, dict):
            if props.get("workspaceId") == ZERO_GUID:
                props["workspaceId"] = workspace_id
            if props.get("itemId") == ZERO_GUID:
                props["itemId"] = kql_database_id

    # Include / drop the optional FabricItemInvocation work-order rule.
    kept_rules: List[dict] = []
    for rule in entities.get("rules", []):
        requires = rule.get("_optional_requires")
        if requires == "work_order_item_id":
            if not plan.work_order_item_id:
                LOG.info(
                    "Optional FabricItemInvocation rule %r dropped (alerting.work_order_item_id "
                    "not set) — default deploy is Email-only.",
                    rule.get("name"),
                )
                continue
            action = rule.get("action")
            if isinstance(action, dict):
                if action.get("workspaceId") == ZERO_GUID:
                    action["workspaceId"] = workspace_id
                if action.get("itemId") == ZERO_GUID:
                    action["itemId"] = plan.work_order_item_id
            LOG.info(
                "Optional FabricItemInvocation rule %r included (work-order item %s).",
                rule.get("name"), plan.work_order_item_id,
            )
        kept_rules.append(rule)
    entities["rules"] = kept_rules

    if plan.enable_ontology:
        LOG.info(
            "features.enable_ontology=true: the default email rule binds to the Eventhouse "
            "property; the OPTIONAL ontology-business-entity enrichment is documented as a "
            "commented note in reflex_entities.json (verify against the live Reflex schema "
            "before wiring it)."
        )

    # Drop authoring-only meta keys before sending to Fabric.
    return _strip_meta(entities)


# ---------------------------------------------------------------------------
# Definition assembly + Reflex find-or-create (idempotent)
# ---------------------------------------------------------------------------

def _b64_part(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def build_platform_json(plan: ActivatorPlan) -> dict:
    """Build the ``.platform`` metadata part for a create-with-definition request."""
    return {
        "$schema": PLATFORM_SCHEMA,
        "metadata": {
            "type": REFLEX_ITEM_TYPE,
            "displayName": plan.reflex_name,
            "description": "Zava idle-vehicle / overdue-maintenance email alert (Teams-free).",
        },
        "config": {
            "version": "2.0",
            "logicalId": ZERO_GUID,
        },
    }


def build_definition(reflex_entities: dict, plan: ActivatorPlan) -> dict:
    """Build the Fabric item-definition payload (format=json; ReflexEntities.json + .platform)."""
    return {
        "format": "json",
        "parts": [
            {
                "path": "ReflexEntities.json",
                "payload": _b64_part(reflex_entities),
                "payloadType": "InlineBase64",
            },
            {
                "path": ".platform",
                "payload": _b64_part(build_platform_json(plan)),
                "payloadType": "InlineBase64",
            },
        ],
    }


def find_or_create_reflex(
    token: str, workspace_id: str, plan: ActivatorPlan, definition: dict
) -> dict:
    """Find-or-create the Reflex (Activator); update its definition in place if it exists."""
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/reflexes", token), plan.reflex_name
    )
    if existing:
        reflex_id = str(existing.get("id"))
        LOG.info(
            "Reflex (Activator) %r already exists (%s) — updating definition (idempotent).",
            plan.reflex_name, reflex_id,
        )
        status, headers, _ = _request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reflexes/{reflex_id}/updateDefinition",
            token,
            body={"definition": definition},
        )
        if status == 202:
            _poll_long_running(headers, token)
        return existing
    LOG.info("Creating Reflex (Activator) %r ...", plan.reflex_name)
    created = _create_item_lro(
        f"/workspaces/{workspace_id}/reflexes",
        token,
        {
            "displayName": plan.reflex_name,
            "description": "Zava idle-vehicle / overdue-maintenance email alert (Teams-free).",
            "definition": definition,
        },
    )
    LOG.info("Created Reflex (Activator) %r (%s).", plan.reflex_name, created.get("id"))
    return created


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def run(plan: ActivatorPlan, *, dry_run: bool = False) -> int:
    """Execute the Activator email-alert deploy flow. Returns an exit code."""
    if not plan.enable_activator_email:
        LOG.info(
            "features.enable_activator_email=false — skipping the Activator email alert. "
            "Set enable_activator_email=true (and enable_eventhouse=true) to enable Step 19."
        )
        return 0
    if not plan.enable_eventhouse:
        raise ActivatorError(
            "features.enable_activator_email=true requires features.enable_eventhouse=true "
            "(the Activator email rule binds to the Step-18 Eventhouse property). Enable the "
            "Eventhouse (Step 18) or set enable_activator_email=false."
        )

    if dry_run:
        return _print_dry_run(plan)

    if not os.path.isfile(REFLEX_ENTITIES_PATH):
        raise ActivatorError(f"ReflexEntities definition not found: {REFLEX_ENTITIES_PATH}")

    token = get_access_token()

    # 1. Resolve the Step-10 workspace.
    workspace_id = resolve_workspace_id(token, plan)

    # 2. Resolve the Step-18 KQL database item id (the email rule's default source).
    kql_database_id = resolve_kql_database_id(token, workspace_id, plan.kql_database_name)

    # 3. Build the ReflexEntities.json (tokens substituted; source bound; optional rule gated).
    reflex_entities = load_reflex_entities(plan, kql_database_id, workspace_id)
    definition = build_definition(reflex_entities, plan)

    # 4. Find-or-create the Reflex (Activator) item and deploy/refresh the definition.
    reflex = find_or_create_reflex(token, workspace_id, plan, definition)

    LOG.info(
        "Done. workspace_id=%s reflex_id=%s recipient=%s (Teams-free EmailMessage). "
        "Author/validate the rule in Activator design mode, then Save and start so it acts on "
        "new data (R11 §6c; see docs/manual-steps.md, Step 19).",
        workspace_id, reflex.get("id"), plan.site_manager_email,
    )
    return 0


def _print_dry_run(plan: ActivatorPlan) -> int:
    """Credential-free preview of every intended REST call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    LOG.info("[DRY-RUN] enable_activator_email=%s enable_eventhouse=%s enable_ontology=%s",
             plan.enable_activator_email, plan.enable_eventhouse, plan.enable_ontology)
    if not plan.enable_activator_email:
        LOG.info("[DRY-RUN] Feature gate off — nothing would be created.")
        return 0
    if plan.use_existing_workspace:
        LOG.info("[DRY-RUN] Would GET %s/workspaces/%s (existing).",
                 FABRIC_API_BASE, plan.existing_workspace_id)
    else:
        LOG.info("[DRY-RUN] Would GET %s/workspaces to resolve %r -> id.",
                 FABRIC_API_BASE, plan.workspace_name)
    LOG.info("[DRY-RUN] Would GET %s/workspaces/{id}/kqlDatabases to resolve %r -> KQL database "
             "item id (the email rule's default Eventhouse source).",
             FABRIC_API_BASE, plan.kql_database_name)
    try:
        with open(REFLEX_ENTITIES_PATH, "r", encoding="utf-8") as fh:
            text = fh.read()
        text = (text.replace(TOKEN_SITE_MANAGER_EMAIL, plan.site_manager_email)
                    .replace(TOKEN_KQL_DATABASE_NAME, plan.kql_database_name)
                    .replace(TOKEN_KQL_TABLE_NAME, plan.kql_table_name))
        entities = _strip_meta(json.loads(text))
        rule_names = [r.get("name") for r in entities.get("rules", [])]
        email_rules = [
            r.get("name") for r in entities.get("rules", [])
            if isinstance(r.get("action"), dict) and r["action"].get("kind") == "EmailMessage"
        ]
        LOG.info("[DRY-RUN] ReflexEntities.json rules: %s (EmailMessage rules: %s).",
                 rule_names, email_rules)
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("[DRY-RUN] ReflexEntities.json not readable/parseable: %s", exc)
    if plan.work_order_item_id:
        LOG.info("[DRY-RUN] Optional FabricItemInvocation work-order rule WOULD be included "
                 "(alerting.work_order_item_id=%s).", plan.work_order_item_id)
    else:
        LOG.info("[DRY-RUN] Optional FabricItemInvocation work-order rule would be DROPPED "
                 "(alerting.work_order_item_id not set) — Email-only default.")
    LOG.info("[DRY-RUN] Would find-or-create Reflex (Activator) %r "
             "(POST %s/workspaces/{id}/reflexes; definition.format='json'; "
             "InlineBase64 ReflexEntities.json + .platform; idempotent updateDefinition on "
             "re-run). Recipient (placeholder): %s. Teams-free EmailMessage action.",
             plan.reflex_name, FABRIC_API_BASE, plan.site_manager_email)
    LOG.info("[DRY-RUN] Manual one-time: author/validate the rule in Activator design mode, then "
             "Save and start (UI-assisted; no Teams; docs/manual-steps.md Step 19).")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the Zava Fabric Activator (Reflex) native Email alert "
                    "(DEFAULT, Teams-free watch+act; Reflex REST API + ReflexEntities.json).",
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
    parser.add_argument("--kql-database-name", help="KQL database name (overrides config/env).")
    parser.add_argument("--kql-table-name", help="KQL table name (overrides config/env).")
    parser.add_argument(
        "--site-manager-email",
        help="Alert recipient (placeholder address; overrides config/env). Never a real address.",
    )
    parser.add_argument("--reflex-name", help="Reflex (Activator) display name (overrides config/env).")
    parser.add_argument(
        "--work-order-item-id",
        help="Optional Fabric item GUID for the FabricItemInvocation work-order action "
             "(overrides config/env). When set, the optional Teams-free work-order rule is included.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print intended REST calls without authenticating or mutating anything.",
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
            kql_database_name=args.kql_database_name,
            kql_table_name=args.kql_table_name,
            site_manager_email=args.site_manager_email,
            reflex_name=args.reflex_name,
            work_order_item_id=args.work_order_item_id,
        )
        return run(plan, dry_run=args.dry_run)
    except ActivatorError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
