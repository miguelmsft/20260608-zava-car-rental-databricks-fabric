#!/usr/bin/env python3
"""Create the Zava **Operations Agent** (GA) — the OPTIONAL Microsoft Teams
enhancement for the watch+act layer — idempotently and as code (plan Step 20; R11).

================================ OPTIONAL STEP ================================
This script provisions the **Fabric Operations Agent** (generally available at
Microsoft Build 2026, Azure blog 2026-06-02). It is an **OPTIONAL** enhancement
layered on top of the **default, Teams-free** Activator email path (plan Step 19):
it adds LLM-reasoned, concept-aware recommendations with a human-in-the-loop
**Teams Yes/No approval** card.

It runs **only** when **`features.enable_operations_agent=true`** in deploy_config.
The default is **false** — the Teams-free Activator email (Step 19) is the default
watch+act channel and needs no Teams.

>>> THREE HARD REQUIREMENTS (each fails gracefully below with a clear message) <<<
  1. **`features.enable_operations_agent=true`.** When false (the default), this
     script logs a clear "skipped / Teams-free default is Step 19" message and
     exits 0 — it never creates anything.
  2. **A Microsoft Teams account + the Fabric Operations Agent Teams app.** The
     Operations Agent's default notification channel AND its Yes/No approval card
     are delivered in **Microsoft Teams** — there is NO native email channel, even
     at GA (R11 §6a). Installing the **Fabric Operations Agent Teams app** and
     performing the live Yes/No approval are **manual UI steps** (R11 §4a; plan
     Step 20 / consolidated in docs/manual-steps.md Step 25). This script cannot
     install the Teams app; if the tenant has no Teams, leave the flag false and
     use the Step-19 Activator email instead.
  3. **A Microsoft Entra USER token (NOT a service principal / managed identity).**
     The Operations Agent item REST APIs support **User identity only** — service
     principals and managed identities are **not** supported for these item
     operations (R11 §4b, verbatim from the Create Operations Agent reference).
     This script therefore acquires a **user** token (interactive `az login` /
     AzureCliCredential / InteractiveBrowserCredential) and **refuses** an SP token
     with a clear message (it detects env service-principal credentials and also
     inspects the acquired token's claims). Unattended CI/CD with an SP is NOT
     possible for this step today.

What it does
------------
1. Resolves identifiers from ``deploy_config.json`` (Step 1 schema): the target
   workspace (``workspace.use_existing`` / ``workspace.existing_workspace_id`` /
   ``workspace.name``, created by Step 10), the Step-18 Real-Time Intelligence
   names (``realtime.kql_database_name`` / ``realtime.kql_table_name``), the Teams
   recommendation **Recipient UPN** (``alerting.site_manager_email`` placeholder),
   and an OPTIONAL rebalancing/maintenance **pipeline** for the ``FabricJobAction``
   (``operations_agent.action_pipeline_name`` / ``--action-pipeline-name``). Honours
   the ``features.enable_operations_agent`` gate. CLI / env overrides take
   precedence. **No secrets are read from config.**
2. Authenticates with a **USER** token for the Fabric control plane
   (``https://api.fabric.microsoft.com``). Rejects service-principal credentials.
3. **Find-or-create the Operations Agent** (idempotent) via
   ``POST /v1/workspaces/{id}/operationsAgents`` with the ``Configurations.json``
   ``OperationsAgentDefinition`` part (InlineBase64) + a ``.platform`` part. The
   definition grounds on the Step-18 **`KustoDatabase`** source, carries the Zava
   single-condition rules (idle-vehicle / fault-spike / maintenance-overdue), an
   optional **`FabricJobAction`**, a ``messageDestination`` (Teams Recipient), and
   ``shouldRun``. On a subsequent run the existing agent's definition is updated in
   place (``.../updateDefinition``) — no duplicate item.

REST endpoints used (Fabric OperationsAgent item APIs — R11 §4b; USER token only):
    POST   /v1/workspaces/{workspaceId}/operationsAgents                       (create w/ definition)
    GET    /v1/workspaces/{workspaceId}/operationsAgents                       (list / find-by-name)
    POST   /v1/workspaces/{workspaceId}/operationsAgents/{id}/updateDefinition (idempotent update)
    GET    /v1/workspaces/{workspaceId}/kqlDatabases                           (resolve KustoDatabase source id)
    GET    /v1/workspaces/{workspaceId}/dataPipelines                          (resolve optional FabricJobAction target)

Definition part path (R11 §4b conflict — documented, verify in-tenant)
----------------------------------------------------------------------
The Operations agent **definition schema article** specifies the part path
``Configurations.json`` (followed here, authoritative), whereas the auto-generated
**Create/Update** REST examples still show ``OperationsAgentV1.json``. The ``format``
discriminator is ``OperationsAgentV1`` on both. Override with ``--definition-part-path``
if your tenant rejects ``Configurations.json``.

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names / ids / placeholder UPN only);
  auth is acquired at runtime via a user ``az login`` / azure-identity. The Teams
  Recipient is a placeholder address and is never a real, committed mailbox.
* **Authoring phase:** do **not** run this against a live tenant unless you intend
  to create the Operations Agent. Use ``--dry-run`` to preview every intended REST
  call with **no** authentication and **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe; ignores the feature gate for preview):
    python fabric/scripts/80_create_operations_agent.py --dry-run

    # Create the Operations Agent (requires enable_operations_agent=true + Teams + a USER token):
    python fabric/scripts/80_create_operations_agent.py

    # Stop monitoring without deleting (deploy shouldRun=false via updateDefinition):
    python fabric/scripts/80_create_operations_agent.py --should-run false
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
import uuid
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fabric REST control plane (R11 §3). Operations Agent item APIs are USER-only.
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

# Operations Agent item-definition envelope (R11 §4b).
DEFINITION_FORMAT = "OperationsAgentV1"
# R11 §4b conflict: the definition schema article says "Configurations.json"; the
# Create/Update REST examples say "OperationsAgentV1.json". We follow the schema
# article (authoritative) and expose --definition-part-path to override in-tenant.
DEFAULT_DEFINITION_PART_PATH = "Configurations.json"

# Retry / long-running-operation tuning (Fabric throttling: HTTP 429 + Retry-After;
# 202-Accepted LROs expose Operation-Location + Retry-After — R11 §4b).
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5
LRO_POLL_DEFAULT_SECONDS = 5
LRO_TIMEOUT_SECONDS = 600

# Operations Agent is available in all Fabric regions EXCEPT South Central US and
# East US (note: East US 2 IS supported). Warn — do not block — on an excluded region
# (R11 §3). Compared case/space-insensitively.
EXCLUDED_REGIONS = {"southcentralus", "eastus"}

# Repo-relative defaults.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)
DEFINITION_PATH = os.path.join(
    _REPO_ROOT, "fabric", "operations-agent", "Configurations.json"
)

# Placeholder tokens substituted into Configurations.json at deploy time.
TOKEN_WORKSPACE_ID = "__ZAVA_WORKSPACE_ID__"
TOKEN_KQL_DATABASE_ID = "__ZAVA_KQL_DATABASE_ID__"
TOKEN_PIPELINE_ITEM_ID = "__ZAVA_PIPELINE_ITEM_ID__"
TOKEN_ACTION_ID = "__ZAVA_ACTION_ID__"
TOKEN_SITE_MANAGER_UPN = "__ZAVA_SITE_MANAGER_UPN__"

# A value like "<WORKSPACE_GUID>" in the sample config is an unresolved placeholder.
_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

LOG = logging.getLogger("zava.fabric.operations_agent")


class OperationsAgentError(RuntimeError):
    """Raised on unrecoverable problems (bad identity, auth failure, API error)."""


class TeamsOrTokenError(OperationsAgentError):
    """Raised specifically when Teams/user-token preconditions are not met."""


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
            raise OperationsAgentError(f"--config path does not exist: {explicit}")
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
        raise OperationsAgentError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OperationsAgentError(f"config {path} must be a JSON object")
    return data


class OperationsAgentPlan:
    """Resolved, validated plan for the Operations Agent operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        region: Optional[str],
        workspace_name: Optional[str],
        use_existing_workspace: bool,
        existing_workspace_id: Optional[str],
        agent_name: str,
        kql_database_name: str,
        kql_table_name: str,
        recipient_upn: str,
        action_pipeline_name: Optional[str],
        action_pipeline_id: Optional[str],
        should_run: bool,
        definition_part_path: str,
        enable_operations_agent: bool,
    ) -> None:
        self.config_path = config_path
        self.region = region
        self.workspace_name = workspace_name
        self.use_existing_workspace = use_existing_workspace
        self.existing_workspace_id = existing_workspace_id
        self.agent_name = agent_name
        self.kql_database_name = kql_database_name
        self.kql_table_name = kql_table_name
        self.recipient_upn = recipient_upn
        self.action_pipeline_name = action_pipeline_name
        self.action_pipeline_id = action_pipeline_id
        self.should_run = should_run
        self.definition_part_path = definition_part_path
        self.enable_operations_agent = enable_operations_agent


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_name: Optional[str] = None,
    existing_workspace_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    kql_database_name: Optional[str] = None,
    kql_table_name: Optional[str] = None,
    recipient_upn: Optional[str] = None,
    action_pipeline_name: Optional[str] = None,
    action_pipeline_id: Optional[str] = None,
    should_run: Optional[bool] = None,
    definition_part_path: Optional[str] = None,
) -> OperationsAgentPlan:
    """Resolve the Operations Agent plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable.
    Raises ``OperationsAgentError`` naming any field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    rt_cfg = cfg.get("realtime", {}) if isinstance(cfg.get("realtime"), dict) else {}
    feat_cfg = cfg.get("features", {}) if isinstance(cfg.get("features"), dict) else {}
    alert_cfg = cfg.get("alerting", {}) if isinstance(cfg.get("alerting"), dict) else {}
    oa_cfg = (
        cfg.get("operations_agent", {})
        if isinstance(cfg.get("operations_agent"), dict)
        else {}
    )

    # Feature gate: this OPTIONAL step runs only when enable_operations_agent=true
    # (default FALSE — the Teams-free Activator email of Step 19 is the default).
    flag = feat_cfg.get("enable_operations_agent")
    enable_operations_agent = bool(flag) if isinstance(flag, bool) else False

    region = _clean(cfg.get("region")) or os.environ.get("FABRIC_REGION")

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

    # Teams recommendation Recipient UPN — reuse the site-manager placeholder.
    recipient = (
        recipient_upn
        or _clean(oa_cfg.get("message_recipient_upn"))
        or _clean(alert_cfg.get("site_manager_email"))
        or os.environ.get("FABRIC_OPS_AGENT_RECIPIENT_UPN")
    )

    # Optional FabricJobAction target pipeline (by name or explicit id). When neither
    # resolves, the FabricJobAction is dropped and the agent only sends a Teams
    # recommendation (no defined action) — still valid per R11 §4a.
    pipeline_name = (
        action_pipeline_name
        or _clean(oa_cfg.get("action_pipeline_name"))
        or os.environ.get("FABRIC_OPS_AGENT_PIPELINE_NAME")
    )
    pipeline_id = (
        action_pipeline_id
        or _clean(oa_cfg.get("action_pipeline_id"))
        or os.environ.get("FABRIC_OPS_AGENT_PIPELINE_ID")
    )

    agent = (
        agent_name
        or _clean(oa_cfg.get("agent_name"))
        or os.environ.get("FABRIC_OPS_AGENT_NAME")
        or "Zava Fleet Operations Agent"
    )

    # shouldRun: explicit override > config > default True.
    if should_run is None:
        cfg_run = oa_cfg.get("should_run")
        should_run = bool(cfg_run) if isinstance(cfg_run, bool) else True

    part_path = (
        definition_part_path
        or _clean(oa_cfg.get("definition_part_path"))
        or DEFAULT_DEFINITION_PART_PATH
    )

    # Validation.
    if use_existing:
        if not existing_id:
            raise OperationsAgentError(
                "workspace.use_existing=true but no workspace id resolved "
                "(set workspace.existing_workspace_id, --workspace-id, or FABRIC_WORKSPACE_ID)"
            )
        if not _GUID_RE.match(existing_id):
            raise OperationsAgentError(
                f"resolved existing workspace id is not a GUID: {existing_id!r}"
            )
    elif not name:
        raise OperationsAgentError(
            "could not resolve workspace name "
            "(set workspace.name, --workspace-name, or FABRIC_WORKSPACE_NAME)"
        )

    if not kql_db:
        raise OperationsAgentError(
            "could not resolve KQL database name for the KustoDatabase source "
            "(set realtime.kql_database_name, --kql-database-name, or FABRIC_KQL_DATABASE_NAME); "
            "run Step 18 (fabric/scripts/75_create_eventhouse.py) first."
        )
    if not recipient:
        raise OperationsAgentError(
            "could not resolve the Teams recommendation Recipient UPN "
            "(set operations_agent.message_recipient_upn, alerting.site_manager_email, "
            "--recipient-upn, or FABRIC_OPS_AGENT_RECIPIENT_UPN). This is a placeholder "
            "UPN — never commit a real mailbox."
        )
    if pipeline_id and not _GUID_RE.match(pipeline_id):
        raise OperationsAgentError(
            f"resolved action pipeline id is not a GUID: {pipeline_id!r}"
        )

    return OperationsAgentPlan(
        config_path=resolved_path,
        region=region,
        workspace_name=name,
        use_existing_workspace=use_existing,
        existing_workspace_id=existing_id,
        agent_name=agent,
        kql_database_name=kql_db,
        kql_table_name=kql_table,
        recipient_upn=recipient,
        action_pipeline_name=pipeline_name,
        action_pipeline_id=pipeline_id,
        should_run=bool(should_run),
        definition_part_path=part_path,
        enable_operations_agent=enable_operations_agent,
    )


# ---------------------------------------------------------------------------
# Authentication — USER token only (no service principal / managed identity)
# ---------------------------------------------------------------------------

def _service_principal_env_present() -> bool:
    """True when env service-principal credentials are configured.

    The Operations Agent item APIs do NOT support service principals (R11 §4b), so
    we refuse early if env SP creds are present (which DefaultAzureCredential /
    EnvironmentCredential would otherwise use to mint an unsupported app token).
    """
    client_id = os.environ.get("AZURE_CLIENT_ID")
    secret = os.environ.get("AZURE_CLIENT_SECRET")
    cert = os.environ.get("AZURE_CLIENT_CERTIFICATE_PATH")
    return bool(client_id and (secret or cert))


def _decode_jwt_claims(token: str) -> Dict[str, object]:
    """Best-effort decode of a JWT's payload claims (no signature verification)."""
    try:
        payload_b64 = token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(raw.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001 - claim inspection is best-effort only
        return {}


def _assert_user_token(token: str) -> None:
    """Raise if the acquired token looks like an app-only (service-principal) token.

    Heuristics on the Entra access-token claims: ``idtyp == "app"`` marks an app-only
    token; an app-only token also typically has ``roles`` and no ``scp`` and no user
    identifier (``upn`` / ``unique_name`` / ``preferred_username`` / ``name``). User
    delegated tokens carry ``scp`` and a user identifier. If we can't decode the
    token we do not block (the API itself will reject a non-user identity).
    """
    claims = _decode_jwt_claims(token)
    if not claims:
        LOG.debug("Could not decode token claims; deferring user-identity check to the API.")
        return
    idtyp = str(claims.get("idtyp", "")).lower()
    has_user_id = any(
        claims.get(k) for k in ("upn", "unique_name", "preferred_username", "name")
    )
    has_scope = bool(claims.get("scp"))
    looks_app = idtyp == "app" or (not has_user_id and not has_scope and bool(claims.get("roles")))
    if looks_app:
        raise TeamsOrTokenError(
            "the acquired Fabric token is a SERVICE-PRINCIPAL / app-only token, but the "
            "Operations Agent item APIs support USER identity only (R11 §4b). "
            "Sign in interactively as a user (run `az login` without --service-principal, "
            "or let InteractiveBrowserCredential prompt) and retry. Unattended SP/MI "
            "deployment of the Operations Agent is not supported today."
        )


def get_user_access_token(resource: str = FABRIC_RESOURCE) -> str:
    """Acquire a USER bearer token for ``resource``; refuse service-principal tokens.

    No secret is ever read from or written to disk: this relies on an existing
    interactive ``az login`` or an interactive browser sign-in. Env service-principal
    credentials are rejected up front (the Operations Agent APIs are user-only).
    """
    if _service_principal_env_present():
        raise TeamsOrTokenError(
            "service-principal environment credentials (AZURE_CLIENT_ID + "
            "AZURE_CLIENT_SECRET/AZURE_CLIENT_CERTIFICATE_PATH) are set, but the "
            "Operations Agent item APIs support USER identity only (R11 §4b). "
            "Unset them and sign in interactively as a user (`az login`), then retry. "
            "The Teams-free default (Activator email, Step 19) works with a service "
            "principal — use that for unattended deployments."
        )

    scope = resource.rstrip("/") + "/.default"
    token: Optional[str] = None

    # Prefer the signed-in Azure CLI user, then an interactive browser sign-in.
    try:
        from azure.identity import (  # type: ignore import-not-found
            AzureCliCredential,
            InteractiveBrowserCredential,
        )
    except ImportError:
        LOG.debug("azure-identity not installed; using `az account get-access-token`.")
        token = _get_token_via_az_cli(resource)
    else:
        try:
            token = AzureCliCredential().get_token(scope).token
        except Exception as exc:  # noqa: BLE001 - fall back to interactive sign-in
            LOG.debug("AzureCliCredential failed (%s); trying InteractiveBrowserCredential.", exc)
            try:
                token = InteractiveBrowserCredential().get_token(scope).token
            except Exception as exc2:  # noqa: BLE001 - last resort: az CLI subprocess
                LOG.debug("InteractiveBrowserCredential failed (%s); trying az CLI.", exc2)
                token = _get_token_via_az_cli(resource)

    if not token:
        raise TeamsOrTokenError(
            "could not acquire a Fabric USER token. Run `az login` (interactive user "
            "sign-in) or install azure-identity, then retry."
        )
    _assert_user_token(token)
    return token


def _get_token_via_az_cli(resource: str) -> str:
    try:
        proc = subprocess.run(
            ["az", "account", "get-access-token", "--resource", resource, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise TeamsOrTokenError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI "
            "(`az`) is available. Install azure-identity or run `az login` (as a USER)."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise TeamsOrTokenError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise TeamsOrTokenError("could not parse access token from az CLI output") from exc
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

    Returns (status_code, response_headers, json_payload). Raises
    ``OperationsAgentError`` on a non-retryable HTTP error or once retries exhaust.
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
            raise OperationsAgentError(
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
            raise OperationsAgentError(f"{method} {url} failed: {exc.reason}") from exc


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
    """Poll a Fabric long-running operation (202 + Operation-Location) until terminal."""
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
            raise OperationsAgentError(
                f"Fabric long-running operation {op_url} ended in state {state!r}: {payload}"
            )
        if time.monotonic() >= deadline:
            raise OperationsAgentError(
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
    """POST a create call; if it returns 202 (async), poll to completion and return."""
    status, headers, payload = _request("POST", f"{FABRIC_API_BASE}{path}", token, body=body)
    if status == 202:
        result = _poll_long_running(headers, token)
        return result or payload
    return payload


# ---------------------------------------------------------------------------
# Workspace / source / pipeline resolution (find-or-create)
# ---------------------------------------------------------------------------

def resolve_workspace_id(token: str, plan: OperationsAgentPlan) -> str:
    """Resolve the target workspace id (existing path by id, fresh path by name)."""
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
    raise OperationsAgentError(
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
    """Resolve the KQL database item id for the KustoDatabase data source."""
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/kqlDatabases", token), name
    )
    if not existing:
        raise OperationsAgentError(
            f"KQL database {name!r} not found in the workspace — run Step 18 "
            f"(fabric/scripts/75_create_eventhouse.py) first to create the Eventhouse/KQL "
            f"telematics source that the Operations Agent grounds on."
        )
    LOG.info("Resolved KustoDatabase source %r -> %s.", name, existing.get("id"))
    return str(existing["id"])


def resolve_pipeline_id(
    token: str, workspace_id: str, plan: OperationsAgentPlan
) -> Optional[str]:
    """Resolve the optional FabricJobAction target pipeline id (or None to drop it)."""
    if plan.action_pipeline_id:
        LOG.info("Using explicit FabricJobAction pipeline id %s.", plan.action_pipeline_id)
        return plan.action_pipeline_id
    if not plan.action_pipeline_name:
        LOG.info(
            "No rebalancing/maintenance pipeline configured — the FabricJobAction will be "
            "omitted; the agent will send a Teams recommendation with no defined action "
            "(valid per R11 §4a). Set operations_agent.action_pipeline_name to wire a pipeline."
        )
        return None
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/dataPipelines", token),
        plan.action_pipeline_name,
    )
    if not existing:
        LOG.warning(
            "FabricJobAction pipeline %r not found in the workspace — omitting the action "
            "(the agent will still send a Teams recommendation). Create the pipeline or fix "
            "operations_agent.action_pipeline_name to enable the FabricJobAction.",
            plan.action_pipeline_name,
        )
        return None
    LOG.info("Resolved FabricJobAction pipeline %r -> %s.",
             plan.action_pipeline_name, existing.get("id"))
    return str(existing["id"])


# ---------------------------------------------------------------------------
# Definition loading / token substitution
# ---------------------------------------------------------------------------

def _strip_comments(obj):
    """Drop authoring-only "_comment*" keys before sending to Fabric."""
    if isinstance(obj, dict):
        return {
            k: _strip_comments(v)
            for k, v in obj.items()
            if not (isinstance(k, str) and k.startswith("_comment"))
        }
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load_definition(
    *,
    workspace_id: str,
    kql_database_id: str,
    recipient_upn: str,
    pipeline_id: Optional[str],
    should_run: bool,
) -> dict:
    """Load Configurations.json, substitute deploy-time tokens, and finalize the def.

    * Substitutes the workspace id, KustoDatabase (KQL DB) id, and Recipient UPN.
    * If ``pipeline_id`` is provided, wires the FabricJobAction (and a fresh action
      GUID); otherwise removes the FabricJobAction entirely (Teams recommendation
      only — valid per R11 §4a).
    * Applies ``should_run`` and strips authoring-only "_comment" keys.
    """
    if not os.path.isfile(DEFINITION_PATH):
        raise OperationsAgentError(f"Operations Agent definition not found: {DEFINITION_PATH}")
    with open(DEFINITION_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()

    text = text.replace(TOKEN_WORKSPACE_ID, workspace_id)
    text = text.replace(TOKEN_KQL_DATABASE_ID, kql_database_id)
    text = text.replace(TOKEN_SITE_MANAGER_UPN, recipient_upn)
    if pipeline_id:
        text = text.replace(TOKEN_PIPELINE_ITEM_ID, pipeline_id)
        text = text.replace(TOKEN_ACTION_ID, str(uuid.uuid4()))

    try:
        definition = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OperationsAgentError(
            f"Operations Agent definition is not valid JSON after substitution: {exc}"
        ) from exc

    config = definition.get("configuration")
    if not isinstance(config, dict):
        raise OperationsAgentError("definition.configuration must be a JSON object")

    # Wire or drop the FabricJobAction depending on pipeline availability.
    actions = config.get("actions")
    if isinstance(actions, dict):
        if not pipeline_id:
            # No pipeline resolved: drop any action that still carries placeholder tokens.
            config["actions"] = {
                alias: action
                for alias, action in actions.items()
                if TOKEN_PIPELINE_ITEM_ID not in json.dumps(action)
                and TOKEN_ACTION_ID not in json.dumps(action)
            }

    definition["shouldRun"] = bool(should_run)
    if "playbook" not in definition:
        definition["playbook"] = {}

    cleaned = _strip_comments(definition)
    # Guard: no unresolved placeholder tokens may leak into the deployed definition.
    leaked = [
        tok
        for tok in (
            TOKEN_WORKSPACE_ID,
            TOKEN_KQL_DATABASE_ID,
            TOKEN_PIPELINE_ITEM_ID,
            TOKEN_ACTION_ID,
            TOKEN_SITE_MANAGER_UPN,
        )
        if tok in json.dumps(cleaned)
    ]
    if leaked:
        raise OperationsAgentError(
            f"unresolved placeholder token(s) remain in the definition: {', '.join(leaked)}"
        )
    return cleaned


def _platform_part(agent_name: str) -> dict:
    """Minimal .platform metadata part (R11 §4b; contents illustrative)."""
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "OperationsAgent", "displayName": agent_name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    }


def _b64(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _definition_envelope(
    definition: dict, agent_name: str, part_path: str
) -> dict:
    """Build the OperationsAgentV1 definition envelope (Configurations.json + .platform)."""
    return {
        "format": DEFINITION_FORMAT,
        "parts": [
            {"path": part_path, "payload": _b64(definition), "payloadType": "InlineBase64"},
            {"path": ".platform", "payload": _b64(_platform_part(agent_name)),
             "payloadType": "InlineBase64"},
        ],
    }


def find_or_create_operations_agent(
    token: str,
    workspace_id: str,
    name: str,
    definition: dict,
    part_path: str,
) -> dict:
    """Find-or-create the Operations Agent; update its definition in place if present."""
    envelope = _definition_envelope(definition, name, part_path)
    existing = _find_item_by_name(
        _list_paginated(f"/workspaces/{workspace_id}/operationsAgents", token), name
    )
    if existing:
        oa_id = str(existing.get("id"))
        LOG.info("Operations Agent %r already exists (%s) — updating definition (idempotent).",
                 name, oa_id)
        status, headers, _ = _request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/operationsAgents/{oa_id}/updateDefinition",
            token,
            body={"definition": envelope},
        )
        if status == 202:
            _poll_long_running(headers, token)
        return existing
    LOG.info("Creating Operations Agent %r ...", name)
    created = _create_item_lro(
        f"/workspaces/{workspace_id}/operationsAgents",
        token,
        {
            "displayName": name,
            "description": "Zava fleet-utilization Operations Agent (GA; plan Step 20). "
                           "OPTIONAL Teams enhancement over the Step-19 Activator email.",
            "definition": envelope,
        },
    )
    LOG.info("Created Operations Agent %r (%s).", name, created.get("id"))
    return created


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def _warn_excluded_region(region: Optional[str]) -> None:
    if region and region.replace(" ", "").lower() in EXCLUDED_REGIONS:
        LOG.warning(
            "region %r is in the Operations Agent exclusion list (South Central US / East US); "
            "the agent may be unavailable. East US 2 IS supported (R11 §3).",
            region,
        )


def run(plan: OperationsAgentPlan, *, dry_run: bool = False) -> int:
    """Execute the Operations Agent find-or-create flow. Returns an exit code."""
    # --dry-run previews every intended call (auth-free) REGARDLESS of the gate, so a
    # reviewer can inspect the plan without enabling the OPTIONAL Teams feature.
    if dry_run:
        return _print_dry_run(plan)

    # OPTIONAL-step gate: only run when explicitly enabled (default false).
    if not plan.enable_operations_agent:
        LOG.info(
            "features.enable_operations_agent=false (default) — SKIPPING the OPTIONAL "
            "Operations Agent. The default, Teams-free watch+act path is the Step-19 "
            "Activator email (fabric/scripts/78_create_activator_email.py), which needs "
            "no Microsoft Teams. Set enable_operations_agent=true (and ensure a Teams "
            "account + the Fabric Operations Agent Teams app + a USER token) to enable it."
        )
        return 0

    _warn_excluded_region(plan.region)
    LOG.info(
        "Operations Agent prerequisites (R11 §3): a Microsoft Teams account + the Fabric "
        "Operations Agent Teams app (install + Yes/No approval are MANUAL UI steps), tenant "
        "Copilot + Azure OpenAI enabled, and a Microsoft Entra USER token (no SP/MI)."
    )

    # 1. Acquire a USER token (refuses service-principal / app-only tokens).
    token = get_user_access_token()

    # 2. Resolve the Step-10 workspace + the Step-18 KustoDatabase (KQL DB) source.
    workspace_id = resolve_workspace_id(token, plan)
    kql_database_id = resolve_kql_database_id(token, workspace_id, plan.kql_database_name)

    # 3. Resolve the optional FabricJobAction target pipeline (may be None).
    pipeline_id = resolve_pipeline_id(token, workspace_id, plan)

    # 4. Build the definition (token substitution + action wiring + shouldRun).
    definition = load_definition(
        workspace_id=workspace_id,
        kql_database_id=kql_database_id,
        recipient_upn=plan.recipient_upn,
        pipeline_id=pipeline_id,
        should_run=plan.should_run,
    )

    # 5. Find-or-create (idempotent) the Operations Agent with that definition.
    agent = find_or_create_operations_agent(
        token, workspace_id, plan.agent_name, definition, plan.definition_part_path
    )

    LOG.info(
        "Done. workspace_id=%s operations_agent_id=%s shouldRun=%s source=KustoDatabase(%s)",
        workspace_id, agent.get("id"), plan.should_run, plan.kql_database_name,
    )
    LOG.info(
        "Manual one-time steps (UI-only; see docs/manual-steps.md Step 25): install the "
        "Fabric Operations Agent Teams app, and approve the live Yes/No recommendation card "
        "in Teams. This path REQUIRES Microsoft Teams (contrast the Teams-free Step-19 email)."
    )
    return 0


def _print_dry_run(plan: OperationsAgentPlan) -> int:
    """Credential-free preview of every intended REST call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    LOG.info("[DRY-RUN] enable_operations_agent=%s (OPTIONAL Teams enhancement; default false).",
             plan.enable_operations_agent)
    if not plan.enable_operations_agent:
        LOG.info("[DRY-RUN] Feature gate off — nothing would be created. The Teams-free "
                 "default is the Step-19 Activator email. (Preview continues for inspection.)")
    LOG.info("[DRY-RUN] Requires: Microsoft Teams + Fabric Operations Agent Teams app (manual UI), "
             "tenant Copilot + Azure OpenAI, and a USER token (NO service principal / MI — R11 §4b).")
    _warn_excluded_region(plan.region)
    if plan.use_existing_workspace:
        LOG.info("[DRY-RUN] Would GET %s/workspaces/%s (existing).",
                 FABRIC_API_BASE, plan.existing_workspace_id)
    else:
        LOG.info("[DRY-RUN] Would GET %s/workspaces to resolve %r -> id.",
                 FABRIC_API_BASE, plan.workspace_name)
    LOG.info("[DRY-RUN] Would resolve KustoDatabase source: GET "
             "%s/workspaces/{id}/kqlDatabases (find %r).",
             FABRIC_API_BASE, plan.kql_database_name)
    if plan.action_pipeline_id:
        LOG.info("[DRY-RUN] FabricJobAction -> explicit pipeline id %s.", plan.action_pipeline_id)
    elif plan.action_pipeline_name:
        LOG.info("[DRY-RUN] Would resolve FabricJobAction pipeline %r: GET "
                 "%s/workspaces/{id}/dataPipelines.", plan.action_pipeline_name, FABRIC_API_BASE)
    else:
        LOG.info("[DRY-RUN] No action pipeline configured — FabricJobAction omitted; "
                 "Teams recommendation only (R11 §4a).")
    LOG.info("[DRY-RUN] Would find-or-create the Operations Agent %r "
             "(POST %s/workspaces/{id}/operationsAgents; format=%s; definition part path %r; "
             "+ .platform; shouldRun=%s; messageDestination=Recipient(%s); idempotent "
             "updateDefinition on re-run).",
             plan.agent_name, FABRIC_API_BASE, DEFINITION_FORMAT, plan.definition_part_path,
             plan.should_run, plan.recipient_upn)

    # Validate the committed definition parses + substitutes (no auth needed).
    try:
        preview = load_definition(
            workspace_id="00000000-0000-0000-0000-000000000000",
            kql_database_id="00000000-0000-0000-0000-000000000000",
            recipient_upn=plan.recipient_upn,
            pipeline_id=(plan.action_pipeline_id or
                         ("00000000-0000-0000-0000-000000000000"
                          if plan.action_pipeline_name else None)),
            should_run=plan.should_run,
        )
        src = preview.get("configuration", {}).get("dataSources", {})
        n_actions = len(preview.get("configuration", {}).get("actions", {}))
        LOG.info("[DRY-RUN] Definition OK: %d dataSource(s) %s, %d action(s), shouldRun=%s.",
                 len(src), [v.get("type") for v in src.values()], n_actions,
                 preview.get("shouldRun"))
    except OperationsAgentError as exc:
        LOG.error("[DRY-RUN] Definition problem: %s", exc)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean, got {value!r}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the Zava Operations Agent (GA) — OPTIONAL Teams enhancement "
                    "(Fabric OperationsAgent REST API; USER token only).",
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
    parser.add_argument("--agent-name", help="Operations Agent display name (overrides config/env).")
    parser.add_argument("--kql-database-name",
                        help="KQL database name for the KustoDatabase source (overrides config/env).")
    parser.add_argument("--kql-table-name", help="KQL table name (overrides config/env).")
    parser.add_argument(
        "--recipient-upn",
        help="Teams recommendation Recipient UPN (placeholder; overrides config/env).",
    )
    parser.add_argument(
        "--action-pipeline-name",
        help="Optional DataPipeline display name for the FabricJobAction (overrides config/env).",
    )
    parser.add_argument(
        "--action-pipeline-id",
        help="Optional DataPipeline GUID for the FabricJobAction (overrides config/env).",
    )
    parser.add_argument(
        "--should-run", type=_parse_bool, metavar="{true,false}",
        help="Run-state for the agent (true=start monitoring, false=stop). Default true.",
    )
    parser.add_argument(
        "--definition-part-path", default=None,
        help="Definition part path. Default 'Configurations.json' (per the schema article); "
             "use 'OperationsAgentV1.json' if your tenant rejects it (R11 §4b conflict).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print intended REST calls + validate the definition without authenticating "
             "or mutating anything.",
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
            agent_name=args.agent_name,
            kql_database_name=args.kql_database_name,
            kql_table_name=args.kql_table_name,
            recipient_upn=args.recipient_upn,
            action_pipeline_name=args.action_pipeline_name,
            action_pipeline_id=args.action_pipeline_id,
            should_run=args.should_run,
            definition_part_path=args.definition_part_path,
        )
        return run(plan, dry_run=args.dry_run)
    except TeamsOrTokenError as exc:
        LOG.error("Operations Agent precondition not met: %s", exc)
        return 2
    except OperationsAgentError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
