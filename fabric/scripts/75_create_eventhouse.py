#!/usr/bin/env python3
"""Create the Zava Real-Time Intelligence stack — **Eventhouse + KQL database +
Telematics table + Eventstream** — idempotently and as code (plan Step 18; R7, R11).

What it does
------------
1. Resolves all identifiers from ``deploy_config.json`` (Step 1 schema): the target
   workspace (``workspace.use_existing`` / ``workspace.existing_workspace_id`` /
   ``workspace.name``, created by Step 10) and the Real-Time Intelligence names
   (``realtime.eventhouse_name`` / ``realtime.kql_database_name`` /
   ``realtime.kql_table_name``). Honours the ``features.enable_eventhouse`` gate
   (this whole step runs only when that flag is true). CLI / environment overrides
   take precedence. **No secrets are read from config.**
2. Authenticates via ``DefaultAzureCredential`` (``az login`` / managed identity /
   env service principal), falling back to the Azure CLI
   (``az account get-access-token``) when ``azure-identity`` is absent. Two audiences
   are used: the Fabric control plane (``https://api.fabric.microsoft.com``) for item
   management, and the Eventhouse's own Kusto query URI for running the KQL DDL.
3. **Find-or-create the Eventhouse** (idempotent) via ``POST
   /v1/workspaces/{id}/eventhouses`` — reused if an Eventhouse of the same display
   name already exists.
4. **Find-or-create the KQL database** (idempotent) via ``POST
   /v1/workspaces/{id}/kqlDatabases`` with a ``creationPayload`` bound to the
   parent Eventhouse (``databaseType=ReadWrite`` / ``parentEventhouseItemId``).
5. **Runs the KQL setup** (``fabric/realtime/eventhouse_setup.kql``) against the
   Eventhouse's Kusto **management** endpoint (``{queryServiceUri}/v1/rest/mgmt``)
   to create the flat ``Telematics`` table, its column docstrings, the
   ``TelematicsMapping`` JSON ingestion mapping, retention/caching policy, and the
   watch+act helper functions. Each KQL command is itself idempotent.
6. **Find-or-create the Eventstream** (idempotent) from
   ``fabric/realtime/eventstream_definition.json`` via ``POST
   /v1/workspaces/{id}/eventstreams`` with an InlineBase64 ``eventstream.json``
   definition part. Placeholder tokens in the definition (workspace id, KQL
   database item id, database/table names) are substituted at deploy time. On a
   subsequent run the existing Eventstream's definition is updated in place
   (``.../updateDefinition``) — no duplicate item.

REST endpoints used (Fabric Real-Time Intelligence APIs — R7 §2, item-management):
    POST   /v1/workspaces/{workspaceId}/eventhouses                        (create)
    GET    /v1/workspaces/{workspaceId}/eventhouses                        (list / find-by-name)
    GET    /v1/workspaces/{workspaceId}/eventhouses/{eventhouseId}         (read queryServiceUri)
    POST   /v1/workspaces/{workspaceId}/kqlDatabases                       (create, bound to eventhouse)
    GET    /v1/workspaces/{workspaceId}/kqlDatabases                       (list / find-by-name)
    POST   /v1/workspaces/{workspaceId}/eventstreams                       (create w/ definition)
    GET    /v1/workspaces/{workspaceId}/eventstreams                       (list / find-by-name)
    POST   /v1/workspaces/{workspaceId}/eventstreams/{id}/updateDefinition (idempotent update)
    POST   {queryServiceUri}/v1/rest/mgmt                                  (Kusto management — run KQL DDL)

Eventstream source connection — automation vs. UI (R11 §6; plan Step 18 manual note)
------------------------------------------------------------------------------------
The Eventstream **topology** (CustomEndpoint source -> Eventhouse destination) deploys
fully as code from ``eventstream_definition.json``. Wiring the synthetic feed to the
source is a **one-time, UI-assisted** step: open the Eventstream, copy the custom
endpoint's event-hub-compatible connection string, and point a small pusher /
``data/generate_telematics_stream.py`` replayer at it. That connection string is a
runtime secret and is **never committed** (documented in ``docs/manual-steps.md``,
Step 18 — appended by the docs-consolidation step).

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names / ids / placeholders only); auth
  is acquired at runtime via ``DefaultAzureCredential`` / ``az login``.
* **Authoring phase:** do **not** run this against a live tenant unless you intend to
  create Fabric items. Use ``--dry-run`` to preview every intended REST/KQL call with
  **no** authentication and **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/75_create_eventhouse.py --dry-run

    # Create the Eventhouse + KQL DB/table + Eventstream from deploy_config.json:
    python fabric/scripts/75_create_eventhouse.py

    # Fully explicit against a real (non-sample) config:
    python fabric/scripts/75_create_eventhouse.py \
        --workspace-name zava-fabric-ws \
        --eventhouse-name zava-eh \
        --kql-database-name zava_rt \
        --kql-table-name Telematics \
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
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

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
KQL_SETUP_PATH = os.path.join(_REPO_ROOT, "fabric", "realtime", "eventhouse_setup.kql")
EVENTSTREAM_DEF_PATH = os.path.join(_REPO_ROOT, "fabric", "realtime", "eventstream_definition.json")

# Placeholder tokens substituted into the Eventstream definition at deploy time.
TOKEN_KQL_DATABASE_NAME = "__ZAVA_KQL_DATABASE_NAME__"
TOKEN_KQL_TABLE_NAME = "__ZAVA_KQL_TABLE_NAME__"
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# A value like "<WORKSPACE_GUID>" in the sample config is an unresolved placeholder.
_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

LOG = logging.getLogger("zava.fabric.eventhouse")


class EventhouseError(RuntimeError):
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
            raise EventhouseError(f"--config path does not exist: {explicit}")
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
        raise EventhouseError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EventhouseError(f"config {path} must be a JSON object")
    return data


class EventhousePlan:
    """Resolved, validated plan for the Real-Time Intelligence operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_name: Optional[str],
        use_existing_workspace: bool,
        existing_workspace_id: Optional[str],
        eventhouse_name: str,
        kql_database_name: str,
        kql_table_name: str,
        eventstream_name: str,
        enable_eventhouse: bool,
    ) -> None:
        self.config_path = config_path
        self.workspace_name = workspace_name
        self.use_existing_workspace = use_existing_workspace
        self.existing_workspace_id = existing_workspace_id
        self.eventhouse_name = eventhouse_name
        self.kql_database_name = kql_database_name
        self.kql_table_name = kql_table_name
        self.eventstream_name = eventstream_name
        self.enable_eventhouse = enable_eventhouse


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_name: Optional[str] = None,
    existing_workspace_id: Optional[str] = None,
    eventhouse_name: Optional[str] = None,
    kql_database_name: Optional[str] = None,
    kql_table_name: Optional[str] = None,
    eventstream_name: Optional[str] = None,
) -> EventhousePlan:
    """Resolve the RTI operation plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable.
    Raises ``EventhouseError`` naming any field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    rt_cfg = cfg.get("realtime", {}) if isinstance(cfg.get("realtime"), dict) else {}
    feat_cfg = cfg.get("features", {}) if isinstance(cfg.get("features"), dict) else {}

    # Feature gate: this step runs only when enable_eventhouse=true (default true so a
    # fresh sample config is demonstrable). Required when activator/operations-agent on.
    flag = feat_cfg.get("enable_eventhouse")
    enable_eventhouse = bool(flag) if isinstance(flag, bool) else True

    use_existing = bool(ws_cfg.get("use_existing"))
    name = workspace_name or _clean(ws_cfg.get("name")) or os.environ.get("FABRIC_WORKSPACE_NAME")
    existing_id = (
        existing_workspace_id
        or _clean(ws_cfg.get("existing_workspace_id"))
        or os.environ.get("FABRIC_WORKSPACE_ID")
    )

    eh_name = (
        eventhouse_name
        or _clean(rt_cfg.get("eventhouse_name"))
        or os.environ.get("FABRIC_EVENTHOUSE_NAME")
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
    # Eventstream display name: derive from the Eventhouse name unless overridden.
    es_name = (
        eventstream_name
        or _clean(rt_cfg.get("eventstream_name"))
        or os.environ.get("FABRIC_EVENTSTREAM_NAME")
        or (f"{eh_name}-eventstream" if eh_name else None)
    )

    # Validation.
    if use_existing:
        if not existing_id:
            raise EventhouseError(
                "workspace.use_existing=true but no workspace id resolved "
                "(set workspace.existing_workspace_id, --workspace-id, or FABRIC_WORKSPACE_ID)"
            )
        if not _GUID_RE.match(existing_id):
            raise EventhouseError(f"resolved existing workspace id is not a GUID: {existing_id!r}")
    elif not name:
        raise EventhouseError(
            "could not resolve workspace name "
            "(set workspace.name, --workspace-name, or FABRIC_WORKSPACE_NAME)"
        )

    if not eh_name:
        raise EventhouseError(
            "could not resolve Eventhouse name "
            "(set realtime.eventhouse_name, --eventhouse-name, or FABRIC_EVENTHOUSE_NAME)"
        )
    if not kql_db:
        raise EventhouseError(
            "could not resolve KQL database name "
            "(set realtime.kql_database_name, --kql-database-name, or FABRIC_KQL_DATABASE_NAME)"
        )

    return EventhousePlan(
        config_path=resolved_path,
        workspace_name=name,
        use_existing_workspace=use_existing,
        existing_workspace_id=existing_id,
        eventhouse_name=eh_name,
        kql_database_name=kql_db,
        kql_table_name=kql_table,
        eventstream_name=es_name,
        enable_eventhouse=enable_eventhouse,
    )


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential, with az CLI fallback)
# ---------------------------------------------------------------------------

def get_access_token(resource: str = FABRIC_RESOURCE) -> str:
    """Acquire a bearer token for ``resource`` via DefaultAzureCredential; fall back to ``az``.

    No secret is ever read from or written to disk: this relies on an existing
    ``az login``, a managed identity, or environment service-principal credentials.
    The Fabric control plane and the Eventhouse Kusto endpoint are different
    audiences, so this is called twice with different resources.
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
        raise EventhouseError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI (`az`) "
            "is available. Install azure-identity or run `az login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise EventhouseError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise EventhouseError("could not parse access token from az CLI output") from exc
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

    Returns (status_code, response_headers, json_payload). Raises ``EventhouseError``
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
            raise EventhouseError(
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
            raise EventhouseError(f"{method} {url} failed: {exc.reason}") from exc


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
            raise EventhouseError(
                f"Fabric long-running operation {op_url} ended in state {state!r}: {payload}"
            )
        if time.monotonic() >= deadline:
            raise EventhouseError(
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
# Workspace / Eventhouse / KQL DB / Eventstream operations (find-or-create)
# ---------------------------------------------------------------------------

def resolve_workspace_id(token: str, plan: EventhousePlan) -> str:
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
    raise EventhouseError(
        f"workspace {plan.workspace_name!r} not found — run Step 10 "
        f"(fabric/scripts/00_create_workspace.py) first."
    )


def _find_item_by_name(items: List[dict], name: str) -> Optional[dict]:
    target = name.strip().lower()
    for item in items:
        if str(item.get("displayName", "")).strip().lower() == target:
            return item
    return None


def find_or_create_eventhouse(token: str, workspace_id: str, name: str) -> dict:
    """Find-or-create the Eventhouse (idempotent)."""
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/eventhouses", token), name
    )
    if existing:
        LOG.info("Eventhouse %r already exists (%s) — reusing.", name, existing.get("id"))
        return existing
    LOG.info("Creating Eventhouse %r ...", name)
    created = _create_item_lro(
        f"/workspaces/{workspace_id}/eventhouses",
        token,
        {"displayName": name, "description": "Zava telematics Real-Time Intelligence (plan Step 18)"},
    )
    LOG.info("Created Eventhouse %r (%s).", name, created.get("id"))
    return created


def get_eventhouse(token: str, workspace_id: str, eventhouse_id: str) -> dict:
    _, _, payload = _request(
        "GET", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/eventhouses/{eventhouse_id}", token
    )
    return payload


def find_or_create_kql_database(
    token: str, workspace_id: str, name: str, eventhouse_id: str
) -> dict:
    """Find-or-create the KQL database bound to the Eventhouse (idempotent)."""
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/kqlDatabases", token), name
    )
    if existing:
        LOG.info("KQL database %r already exists (%s) — reusing.", name, existing.get("id"))
        return existing
    LOG.info("Creating KQL database %r (bound to Eventhouse %s) ...", name, eventhouse_id)
    created = _create_item_lro(
        f"/workspaces/{workspace_id}/kqlDatabases",
        token,
        {
            "displayName": name,
            "creationPayload": {
                "databaseType": "ReadWrite",
                "parentEventhouseItemId": eventhouse_id,
            },
        },
    )
    LOG.info("Created KQL database %r (%s).", name, created.get("id"))
    return created


def _query_service_uri(eventhouse: dict, kql_database: dict) -> Optional[str]:
    """Extract the Kusto query/management URI from the Eventhouse or KQL DB properties."""
    for item in (kql_database, eventhouse):
        props = item.get("properties") if isinstance(item, dict) else None
        if isinstance(props, dict):
            uri = (
                _clean(props.get("queryServiceUri"))
                or _clean(props.get("clusterUri"))
                or _clean(props.get("ingestionServiceUri"))
            )
            if uri:
                return uri.rstrip("/")
    return None


def run_kql_setup(
    query_uri: str, database_name: str, table_name: str, kql_script: str
) -> None:
    """Run the multi-command KQL setup script against the Eventhouse management endpoint.

    The Kusto management endpoint accepts one control command per call, so the
    ``eventhouse_setup.kql`` script is split into individual commands (`.create-merge`,
    `.alter-merge`, `.create-or-alter`, `.alter`, ...) and each is POSTed to
    ``/v1/rest/mgmt``. Every command in the script is idempotent, so re-running the
    whole setup is safe. The ``__ZAVA_KQL_TABLE_NAME__`` placeholder is substituted
    with ``table_name`` first so the table created here matches the script's
    configured table name and the Eventstream destination tableName.
    """
    kql_script = kql_script.replace(TOKEN_KQL_TABLE_NAME, table_name)
    kusto_token = get_access_token(query_uri)
    mgmt_url = f"{query_uri}/v1/rest/mgmt"
    commands = _split_kql_commands(kql_script)
    LOG.info("Running %d KQL management command(s) against %s (db=%s) ...",
             len(commands), query_uri, database_name)
    for idx, command in enumerate(commands, start=1):
        first_line = command.strip().splitlines()[0][:70]
        LOG.info("  [%d/%d] %s ...", idx, len(commands), first_line)
        _request(
            "POST",
            mgmt_url,
            kusto_token,
            body={"db": database_name, "csl": command},
            accept="application/json",
        )
    LOG.info("KQL setup complete (Telematics table, mapping, policies, functions).")


def _split_kql_commands(script: str) -> List[str]:
    """Split a KQL script into individual control commands.

    Comments (``//``) and blank lines are stripped; commands begin with ``.`` and may
    span multiple lines (including ```...``` fenced ingestion-mapping bodies). Commands
    are separated by blank line(s) between top-level ``.`` statements.
    """
    # Drop full-line comments so they don't break command boundaries.
    lines = []
    for raw in script.splitlines():
        stripped = raw.strip()
        if stripped.startswith("//"):
            continue
        lines.append(raw)
    cleaned = "\n".join(lines)

    commands: List[str] = []
    current: List[str] = []
    in_fence = False
    for line in cleaned.splitlines():
        stripped = line.strip()
        if stripped == "```":
            in_fence = not in_fence
            current.append(line)
            continue
        if in_fence:
            current.append(line)
            continue
        if stripped.startswith(".") and current and "".join(current).strip():
            # Start of a new command -> flush the previous one.
            commands.append("\n".join(current).strip())
            current = [line]
        elif not stripped and not current:
            continue
        else:
            current.append(line)
    if current and "\n".join(current).strip():
        commands.append("\n".join(current).strip())
    return [c for c in commands if c.strip()]


def _load_eventstream_definition(
    workspace_id: str, kql_database_id: str, database_name: str, table_name: str
) -> dict:
    """Load eventstream_definition.json and substitute deploy-time placeholders."""
    with open(EVENTSTREAM_DEF_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace(TOKEN_KQL_DATABASE_NAME, database_name)
    text = text.replace(TOKEN_KQL_TABLE_NAME, table_name)
    definition = json.loads(text)
    # Bind the Eventhouse destination to the real workspace + KQL database item.
    for dest in definition.get("destinations", []):
        props = dest.get("properties")
        if isinstance(props, dict) and props.get("workspaceId") == ZERO_GUID:
            props["workspaceId"] = workspace_id
        if isinstance(props, dict) and props.get("itemId") == ZERO_GUID:
            props["itemId"] = kql_database_id
    # Drop authoring-only "_comment" keys before sending to Fabric.
    return _strip_comments(definition)


def _strip_comments(obj):
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if k != "_comment"}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _definition_part(definition: dict) -> dict:
    """Build the Fabric item-definition payload (InlineBase64 eventstream.json part)."""
    payload = base64.b64encode(
        json.dumps(definition, indent=2).encode("utf-8")
    ).decode("ascii")
    return {
        "parts": [
            {"path": "eventstream.json", "payload": payload, "payloadType": "InlineBase64"}
        ]
    }


def find_or_create_eventstream(
    token: str, workspace_id: str, name: str, definition: dict
) -> dict:
    """Find-or-create the Eventstream; update its definition in place if it already exists."""
    def_payload = _definition_part(definition)
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/eventstreams", token), name
    )
    if existing:
        es_id = str(existing.get("id"))
        LOG.info("Eventstream %r already exists (%s) — updating definition (idempotent).",
                 name, es_id)
        status, headers, _ = _request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/eventstreams/{es_id}/updateDefinition",
            token,
            body={"definition": def_payload},
        )
        if status == 202:
            _poll_long_running(headers, token)
        return existing
    LOG.info("Creating Eventstream %r ...", name)
    created = _create_item_lro(
        f"/workspaces/{workspace_id}/eventstreams",
        token,
        {
            "displayName": name,
            "description": "Zava telematics ingest -> Eventhouse Telematics (plan Step 18)",
            "definition": def_payload,
        },
    )
    LOG.info("Created Eventstream %r (%s).", name, created.get("id"))
    return created


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def run(plan: EventhousePlan, *, dry_run: bool = False, skip_eventstream: bool = False) -> int:
    """Execute the Eventhouse + KQL DB/table + Eventstream flow. Returns an exit code."""
    if not plan.enable_eventhouse:
        LOG.info(
            "features.enable_eventhouse=false — skipping Real-Time Intelligence provisioning "
            "(no Eventhouse/KQL/Eventstream). Set enable_eventhouse=true to enable Steps 18-20."
        )
        return 0

    if dry_run:
        return _print_dry_run(plan, skip_eventstream=skip_eventstream)

    if not os.path.isfile(KQL_SETUP_PATH):
        raise EventhouseError(f"KQL setup file not found: {KQL_SETUP_PATH}")
    if not skip_eventstream and not os.path.isfile(EVENTSTREAM_DEF_PATH):
        raise EventhouseError(f"Eventstream definition not found: {EVENTSTREAM_DEF_PATH}")

    token = get_access_token()

    # 1. Resolve the Step-10 workspace.
    workspace_id = resolve_workspace_id(token, plan)

    # 2. Find-or-create the Eventhouse.
    eventhouse = find_or_create_eventhouse(token, workspace_id, plan.eventhouse_name)
    eventhouse_id = str(eventhouse["id"])
    # Re-read for the queryServiceUri (the create response may omit extended properties).
    eventhouse = get_eventhouse(token, workspace_id, eventhouse_id)

    # 3. Find-or-create the KQL database bound to the Eventhouse.
    kql_database = find_or_create_kql_database(
        token, workspace_id, plan.kql_database_name, eventhouse_id
    )
    kql_database_id = str(kql_database["id"])

    # 4. Run the KQL setup (flat Telematics table + mapping + policies + functions).
    query_uri = _query_service_uri(eventhouse, kql_database)
    if not query_uri:
        raise EventhouseError(
            "could not determine the Eventhouse Kusto query URI (queryServiceUri) from the "
            "Eventhouse/KQL database properties; run the KQL in fabric/realtime/eventhouse_setup.kql "
            "manually via the KQL database query editor."
        )
    with open(KQL_SETUP_PATH, "r", encoding="utf-8") as fh:
        kql_script = fh.read()
    run_kql_setup(query_uri, plan.kql_database_name, plan.kql_table_name, kql_script)

    # 5. Find-or-create the Eventstream (ingest telematics -> Telematics table).
    if skip_eventstream:
        LOG.info("--skip-eventstream set: Eventhouse/KQL ready; Eventstream not created.")
    else:
        definition = _load_eventstream_definition(
            workspace_id, kql_database_id, plan.kql_database_name, plan.kql_table_name
        )
        eventstream = find_or_create_eventstream(
            token, workspace_id, plan.eventstream_name, definition
        )
        LOG.info("Eventstream ready: %s (%s).", plan.eventstream_name, eventstream.get("id"))
        LOG.info(
            "Manual one-time step: copy the Eventstream CustomEndpoint connection string from the "
            "Fabric UI and point a telematics pusher (data/generate_telematics_stream.py replayer) "
            "at it. The connection string is a runtime secret and is never committed "
            "(see docs/manual-steps.md, Step 18)."
        )

    LOG.info(
        "Done. workspace_id=%s eventhouse_id=%s kql_database_id=%s table=%s",
        workspace_id, eventhouse_id, kql_database_id, plan.kql_table_name,
    )
    return 0


def _print_dry_run(plan: EventhousePlan, *, skip_eventstream: bool) -> int:
    """Credential-free preview of every intended REST/KQL call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    LOG.info("[DRY-RUN] enable_eventhouse=%s", plan.enable_eventhouse)
    if not plan.enable_eventhouse:
        LOG.info("[DRY-RUN] Feature gate off — nothing would be created.")
        return 0
    if plan.use_existing_workspace:
        LOG.info("[DRY-RUN] Would GET %s/workspaces/%s (existing).",
                 FABRIC_API_BASE, plan.existing_workspace_id)
    else:
        LOG.info("[DRY-RUN] Would GET %s/workspaces to resolve %r -> id.",
                 FABRIC_API_BASE, plan.workspace_name)
    LOG.info("[DRY-RUN] Would find-or-create Eventhouse %r "
             "(POST %s/workspaces/{id}/eventhouses).", plan.eventhouse_name, FABRIC_API_BASE)
    LOG.info("[DRY-RUN] Would find-or-create KQL database %r bound to that Eventhouse "
             "(POST %s/workspaces/{id}/kqlDatabases, databaseType=ReadWrite).",
             plan.kql_database_name, FABRIC_API_BASE)
    try:
        with open(KQL_SETUP_PATH, "r", encoding="utf-8") as fh:
            commands = _split_kql_commands(fh.read())
        LOG.info("[DRY-RUN] Would POST %d KQL management command(s) from %s to "
                 "{queryServiceUri}/v1/rest/mgmt (db=%s) to create the flat %r table, "
                 "TelematicsMapping, policies, and watch+act functions.",
                 len(commands), os.path.relpath(KQL_SETUP_PATH, _REPO_ROOT),
                 plan.kql_database_name, plan.kql_table_name)
    except OSError:
        LOG.warning("[DRY-RUN] KQL setup file not readable: %s", KQL_SETUP_PATH)
    if skip_eventstream:
        LOG.info("[DRY-RUN] --skip-eventstream set: would not create the Eventstream.")
    else:
        LOG.info("[DRY-RUN] Would find-or-create Eventstream %r from %s "
                 "(POST %s/workspaces/{id}/eventstreams; InlineBase64 eventstream.json; "
                 "destination bound to the KQL database item; idempotent updateDefinition "
                 "on re-run).",
                 plan.eventstream_name, os.path.relpath(EVENTSTREAM_DEF_PATH, _REPO_ROOT),
                 FABRIC_API_BASE)
        LOG.info("[DRY-RUN] Manual one-time: wire the CustomEndpoint connection string to the "
                 "telematics feed (secret, never committed; docs/manual-steps.md Step 18).")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the Zava Eventhouse + KQL database/table + Eventstream "
                    "(Fabric Real-Time Intelligence REST APIs).",
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
    parser.add_argument("--eventhouse-name", help="Eventhouse display name (overrides config/env).")
    parser.add_argument("--kql-database-name", help="KQL database name (overrides config/env).")
    parser.add_argument("--kql-table-name", help="KQL table name (overrides config/env).")
    parser.add_argument("--eventstream-name", help="Eventstream display name (overrides config/env).")
    parser.add_argument(
        "--skip-eventstream", action="store_true",
        help="Create the Eventhouse + KQL DB/table only; do not create the Eventstream.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print intended REST/KQL calls without authenticating or mutating anything.",
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
            eventhouse_name=args.eventhouse_name,
            kql_database_name=args.kql_database_name,
            kql_table_name=args.kql_table_name,
            eventstream_name=args.eventstream_name,
        )
        return run(plan, dry_run=args.dry_run, skip_eventstream=args.skip_eventstream)
    except EventhouseError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
