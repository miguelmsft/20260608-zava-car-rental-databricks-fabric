#!/usr/bin/env python3
"""Create (or attach to) the **Mirrored Azure Databricks Catalog** item in the Step-10 Fabric
workspace, mirroring the Step-8 certified **gold** schema of the Databricks ``zava`` Unity Catalog
into Fabric OneLake — idempotently and as code (R1, R7).

What this is (R1)
-----------------
A *Mirrored Azure Databricks Catalog* is a **zero-copy** metadata mirror: Fabric creates OneLake
shortcuts over the Delta tables managed by Azure Databricks (no ETL, no data duplication) and keeps
the structure (catalog -> schemas -> tables) in sync. A SQL analytics endpoint is exposed for T-SQL
and Direct Lake (R2). Metadata auto-syncs every ~15 min when ``autoSync`` is ``Enabled``.

What this script does
---------------------
1. Resolves all identifiers from ``deploy_config.json`` (+ CLI / env overrides). **No secrets.**
     * Fabric workspace GUID  -> ``workspace.existing_workspace_id`` / ``FABRIC_WORKSPACE_ID``,
       else resolved at runtime from ``workspace.name`` via ``GET /v1/workspaces``.
     * Databricks **connection id** (UUID of the Fabric connection to the Databricks workspace) ->
       ``mirroring.databricks_connection_id`` / ``DATABRICKS_WORKSPACE_CONNECTION_ID``  (**required**).
     * Databricks **workspace url** (for human-readable messaging only) ->
       ``mirroring.databricks_workspace_url`` / ``source.databricks_workspace_url`` / env.
     * Unity Catalog **catalog**  -> ``source.databricks_catalog``  (default ``zava``).
     * Unity Catalog **gold schema** -> ``source.gold_schema``  (default ``gold``).
     * Optional ADLS Gen2 **storage connection id** -> ``mirroring.storage_connection_id``.
     * Item display name / mirroring mode / schema selection / auto-sync ->
       ``mirroring.item_name`` / ``mirroring.mode`` / ``mirroring.schemas`` / ``mirroring.auto_sync``.
2. Authenticates to the Fabric REST API (scope ``https://api.fabric.microsoft.com/.default``) via
   ``DefaultAzureCredential`` (``az login`` / managed identity / env SP), falling back to the Azure
   CLI (``az account get-access-token``) when ``azure-identity`` is absent. A small token provider
   re-acquires the token before it expires so the long-running create + first sync wait never fails
   on an expired bearer token (R7 §9).
3. **Auth-identity guard (R1/R7).** Creating the mirrored catalog item / its Databricks connection
   relies on the **Databricks workspace connection**, which authenticates with an *Organizational
   account (user)* or *Service principal*. Per R1/R7 the **one-time OAuth consent** for the
   connection is a UI step, and some tenants require a **user (delegated) token** to create the
   mirror — an **app-only service-principal token** may be rejected. This script inspects the
   acquired token's claims and, if it is app-only, **fails gracefully** with a clear message (use
   ``--allow-service-principal`` to proceed anyway in tenants where SP is supported).
4. **Find-or-create** the mirrored catalog item (idempotent):
     * ``GET /v1/workspaces/{id}/mirroredAzureDatabricksCatalogs`` and match by ``displayName`` —
       reuse if found (never creates a duplicate);
     * else ``POST /v1/workspaces/{id}/mirroredAzureDatabricksCatalogs`` using either the
       **creationPayload** (whole-catalog ``Full`` mirror) or the base64 **definition** (``Partial``
       mirror that scopes to the gold schema, with ``autoSync`` embedded) per R1 §4.1/§4.3.
5. Handles the **long-running** create: a synchronous ``201`` or an async ``202`` (Operation-Location
   + Retry-After) that is polled to completion (R7 §9).
6. Ensures **15-min auto-sync** via ``PATCH .../{id}`` (``properties.autoSync = "Enabled"``) — an
   update-only property that needs the ``Item.Execute.All`` scope (R1 §4.2). Idempotent (skips when
   already Enabled).
7. Waits (with backoff) for the **first metadata sync** to reach a healthy state
   (``mirrorStatus = Mirrored`` / ``syncDetails.status = Success``) so callers know the SQL analytics
   endpoint + OneLake table shortcuts are ready. The wait is best-effort and never hard-fails the
   deployment (Verification is run separately in plan Step 11).

REST endpoints used (Fabric ``MirroredAzureDatabricksCatalog`` API — R1 §4.4, all Preview)
------------------------------------------------------------------------------------------
    GET    /v1/workspaces                                                 (resolve workspace by name)
    GET    /v1/workspaces/{workspaceId}                                   (read workspace)
    GET    /v1/workspaces/{workspaceId}/mirroredAzureDatabricksCatalogs   (list / find-by-name)
    POST   /v1/workspaces/{workspaceId}/mirroredAzureDatabricksCatalogs   (create — LRO)
    GET    /v1/workspaces/{workspaceId}/mirroredAzureDatabricksCatalogs/{id}   (read state/sync)
    PATCH  /v1/workspaces/{workspaceId}/mirroredAzureDatabricksCatalogs/{id}   (enable autoSync)

Manual prerequisite (R1 / R10 — documented in plan.md Step 11; also in docs/manual-steps.md)
--------------------------------------------------------------------------------------------
The **Fabric connection to the Databricks workspace** (and its one-time **OAuth consent** for the
organizational account, when not using SP auth) is created **once in the Fabric portal** — the
Connections REST payload for the "Azure Databricks workspace" connection type is not fully documented
and the OAuth consent is interactive (R1 §4.9). After it exists, copy its **connection id** into
``mirroring.databricks_connection_id`` and run this script to automate the mirror end-to-end.

Security / Phase-0 notes
------------------------
* **No secrets.** Only names / ids / placeholders are read from config; the bearer token is acquired
  at runtime via ``DefaultAzureCredential`` / ``az login`` and never written to disk or logged.
* **Authoring phase:** do **not** run against a live tenant unless you intend to create the mirror.
  Use ``--dry-run`` to preview every intended REST call with **no** authentication and **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/10_create_mirrored_catalog.py --dry-run

    # Create/attach using identifiers from deploy_config.json:
    python fabric/scripts/10_create_mirrored_catalog.py

    # Explicit overrides:
    python fabric/scripts/10_create_mirrored_catalog.py \
        --workspace-id 00000000-0000-0000-0000-000000000000 \
        --connection-id 11111111-1111-1111-1111-111111111111 \
        --catalog zava --schema gold --mode Partial
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
from typing import Callable, Dict, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fabric REST control plane (R1 §4.4 / R7 §2).
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

# The MirroredAzureDatabricksCatalog item type + its collection path segment (R1 §4.4).
MIRRORED_ITEM_TYPE = "MirroredAzureDatabricksCatalog"
MIRRORED_COLLECTION = "mirroredAzureDatabricksCatalogs"

# JSON-schema ids for the definition / .platform parts (R1 §4.3 + Fabric git-integration schema).
DEFINITION_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/"
    "mirroredAzureDatabricksCatalog/definition/"
    "mirroredAzureDatabricksCatalogDefinition/1.0.0/schema.json"
)
PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)

# Retry / long-running-operation tuning. Fabric uses standard Azure throttling (HTTP 429 +
# Retry-After) and 202-Accepted long-running operations (Operation-Location + Retry-After) — R7 §9.
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5
LRO_POLL_DEFAULT_SECONDS = 10
LRO_TIMEOUT_SECONDS = 1800          # mirror create can take a while to provision
FIRST_SYNC_TIMEOUT_SECONDS = 1200   # best-effort wait for the initial metadata sync
FIRST_SYNC_POLL_SECONDS = 30

# Refresh the bearer token this many seconds before its 'exp' to survive long waits (R7 §9).
TOKEN_REFRESH_SKEW_SECONDS = 120

# Repo-relative default config locations (real config preferred; sample as a clean-clone fallback).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)

_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

LOG = logging.getLogger("zava.fabric.mirrored_catalog")

# Token provider callable: returns a (cached, auto-refreshed) bearer token string.
TokenLike = Union[str, Callable[[], str]]


class MirrorError(RuntimeError):
    """Raised on unrecoverable problems (bad identity, auth failure, API error)."""


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
            raise MirrorError(f"--config path does not exist: {explicit}")
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
        raise MirrorError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MirrorError(f"config {path} must be a JSON object")
    return data


class MirrorPlan:
    """Resolved, validated plan for the mirrored-catalog operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_id: Optional[str],
        workspace_name: Optional[str],
        connection_id: Optional[str],
        databricks_url: Optional[str],
        storage_connection_id: Optional[str],
        catalog_name: str,
        schemas: List[str],
        item_name: str,
        mirroring_mode: str,
        auto_sync: bool,
        description: str,
        allow_service_principal: bool,
    ) -> None:
        self.config_path = config_path
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.connection_id = connection_id
        self.databricks_url = databricks_url
        self.storage_connection_id = storage_connection_id
        self.catalog_name = catalog_name
        self.schemas = schemas
        self.item_name = item_name
        self.mirroring_mode = mirroring_mode
        self.auto_sync = auto_sync
        self.description = description
        self.allow_service_principal = allow_service_principal

    @property
    def is_partial(self) -> bool:
        return self.mirroring_mode.lower() == "partial"


def _as_str_list(value: object) -> List[str]:
    """Coerce a config value (str | list) into a clean list of schema names."""
    if isinstance(value, str):
        cleaned = _clean(value)
        return [cleaned] if cleaned else []
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            cleaned = _clean(item)
            if cleaned:
                out.append(cleaned)
        return out
    return []


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_id: Optional[str] = None,
    workspace_name: Optional[str] = None,
    connection_id: Optional[str] = None,
    storage_connection_id: Optional[str] = None,
    catalog_name: Optional[str] = None,
    schema: Optional[str] = None,
    item_name: Optional[str] = None,
    mirroring_mode: Optional[str] = None,
    auto_sync: Optional[bool] = None,
    allow_service_principal: bool = False,
) -> MirrorPlan:
    """Resolve the mirror operation plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable.
    Raises ``MirrorError`` naming any required field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    src_cfg = cfg.get("source", {}) if isinstance(cfg.get("source"), dict) else {}
    mir_cfg = cfg.get("mirroring", {}) if isinstance(cfg.get("mirroring"), dict) else {}

    # --- Fabric workspace (GUID preferred; otherwise resolve by name at runtime) ---
    ws_id = (
        workspace_id
        or _clean(ws_cfg.get("existing_workspace_id"))
        or _clean(os.environ.get("FABRIC_WORKSPACE_ID"))
    )
    ws_name = (
        workspace_name
        or _clean(ws_cfg.get("name"))
        or _clean(os.environ.get("FABRIC_WORKSPACE_NAME"))
    )
    if ws_id and not _GUID_RE.match(ws_id):
        raise MirrorError(f"resolved Fabric workspace id is not a GUID: {ws_id!r}")
    if not ws_id and not ws_name:
        raise MirrorError(
            "could not resolve the Fabric workspace (set workspace.existing_workspace_id or "
            "workspace.name in deploy_config.json, --workspace-id/--workspace-name, or "
            "FABRIC_WORKSPACE_ID/FABRIC_WORKSPACE_NAME). This is the Step-10 workspace."
        )

    # --- Databricks workspace connection id (REQUIRED — created once in the portal, R1 §4.9) ---
    conn_id = (
        connection_id
        or _clean(mir_cfg.get("databricks_connection_id"))
        or _clean(os.environ.get("DATABRICKS_WORKSPACE_CONNECTION_ID"))
    )
    if not conn_id:
        raise MirrorError(
            "no Databricks workspace connection id resolved. Create the Fabric connection to the "
            "Azure Databricks workspace once in the portal (one-time OAuth consent — see plan.md "
            "Step 11 / docs/manual-steps.md), then set mirroring.databricks_connection_id in "
            "deploy_config.json (or --connection-id / DATABRICKS_WORKSPACE_CONNECTION_ID)."
        )
    if not _GUID_RE.match(conn_id):
        raise MirrorError(
            f"databricks_connection_id must be the Fabric connection UUID, got: {conn_id!r}"
        )

    storage_conn = (
        storage_connection_id
        or _clean(mir_cfg.get("storage_connection_id"))
        or _clean(os.environ.get("DATABRICKS_STORAGE_CONNECTION_ID"))
    )
    if storage_conn and not _GUID_RE.match(storage_conn):
        raise MirrorError(f"storage_connection_id must be a UUID, got: {storage_conn!r}")

    databricks_url = (
        _clean(mir_cfg.get("databricks_workspace_url"))
        or _clean(src_cfg.get("databricks_workspace_url"))
        or _clean(os.environ.get("DATABRICKS_WORKSPACE_URL"))
    )

    # --- Unity Catalog source identifiers ---
    catalog = (
        catalog_name
        or _clean(src_cfg.get("databricks_catalog"))
        or _clean(os.environ.get("DATABRICKS_CATALOG"))
        or "zava"
    )
    gold_schema = _clean(src_cfg.get("gold_schema")) or "gold"

    # Schema selection: CLI --schema > mirroring.schemas > the certified gold schema.
    if schema:
        schemas = _as_str_list(schema)
    else:
        schemas = _as_str_list(mir_cfg.get("schemas")) or [gold_schema]

    # --- Mirroring mode: default Partial (scope to the gold schema, the certified asset) ---
    mode = (
        mirroring_mode
        or _clean(mir_cfg.get("mode"))
        or "Partial"
    ).capitalize()
    if mode not in ("Full", "Partial"):
        raise MirrorError(f"mirroring mode must be 'Full' or 'Partial', got: {mode!r}")
    if mode == "Partial" and not schemas:
        raise MirrorError(
            "mirroring mode 'Partial' requires at least one schema (set mirroring.schemas or "
            "source.gold_schema, or pass --schema)."
        )

    # --- Item display name + auto-sync ---
    item = item_name or _clean(mir_cfg.get("item_name"))
    if not item:
        if mode == "Partial" and schemas:
            item = f"{catalog}_{schemas[0]}_mirrored"
        else:
            item = f"{catalog}_mirrored"

    if auto_sync is None:
        cfg_auto = mir_cfg.get("auto_sync")
        auto = bool(cfg_auto) if isinstance(cfg_auto, bool) else True
    else:
        auto = auto_sync

    description = (
        _clean(mir_cfg.get("description"))
        or f"Zava: zero-copy mirror of Databricks Unity Catalog '{catalog}' "
        f"({'schemas ' + ', '.join(schemas) if mode == 'Partial' else 'full catalog'}) "
        "(created by fabric/scripts/10_create_mirrored_catalog.py)"
    )

    return MirrorPlan(
        config_path=resolved_path,
        workspace_id=ws_id,
        workspace_name=ws_name,
        connection_id=conn_id,
        databricks_url=databricks_url,
        storage_connection_id=storage_conn,
        catalog_name=catalog,
        schemas=schemas,
        item_name=item,
        mirroring_mode=mode,
        auto_sync=auto,
        description=description,
        allow_service_principal=allow_service_principal,
    )


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential, with az CLI fallback) + token provider
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Acquire a Fabric API bearer token via DefaultAzureCredential; fall back to the ``az`` CLI.

    No secret is ever read from or written to disk: this relies on an existing ``az login``, a
    managed identity, or environment service-principal credentials.
    """
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore import-not-found
    except ImportError:
        LOG.debug("azure-identity not installed; falling back to `az account get-access-token`")
        return _get_token_via_az_cli()

    try:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        return credential.get_token(FABRIC_SCOPE).token
    except Exception as exc:  # noqa: BLE001 - surface any auth failure uniformly
        LOG.debug("DefaultAzureCredential failed (%s); falling back to az CLI", exc)
        return _get_token_via_az_cli()


def _get_token_via_az_cli() -> str:
    try:
        proc = subprocess.run(
            ["az", "account", "get-access-token", "--resource", FABRIC_RESOURCE, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise MirrorError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI (`az`) "
            "is available. Install azure-identity or run `az login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise MirrorError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise MirrorError("could not parse access token from az CLI output") from exc
    return token


def _decode_jwt_claims(token: str) -> Dict[str, object]:
    """Best-effort decode of a JWT *payload* (no signature verification — claims inspection only)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001 - never let token introspection break the run
        return {}


def is_app_only_token(token: str) -> Optional[bool]:
    """Heuristically decide if ``token`` is an app-only (service-principal) token.

    Returns True (app-only SP), False (delegated user), or None (undetermined). Uses the Entra
    ``idtyp`` claim when present, with sensible fallbacks (delegated tokens carry ``scp`` and a
    user identifier such as ``upn``/``unique_name``; app-only tokens carry ``roles`` and no ``scp``).
    """
    claims = _decode_jwt_claims(token)
    if not claims:
        return None
    idtyp = str(claims.get("idtyp", "")).lower()
    if idtyp == "app":
        return True
    if idtyp == "user":
        return False
    if claims.get("scp") or claims.get("upn") or claims.get("unique_name") or claims.get("name"):
        return False
    if claims.get("roles") and not claims.get("scp"):
        return True
    return None


def _token_expiry(token: str) -> Optional[float]:
    """Return the JWT 'exp' (epoch seconds) if present, else None."""
    claims = _decode_jwt_claims(token)
    exp = claims.get("exp")
    try:
        return float(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


class TokenProvider:
    """Caches a Fabric bearer token and transparently refreshes it before it expires.

    Passed to the REST helpers as a callable so long-running create + first-sync waits never fail on
    an expired token (R7 §9 token-refresh requirement).
    """

    def __init__(self, fetch: Callable[[], str] = get_access_token) -> None:
        self._fetch = fetch
        self._token: Optional[str] = None
        self._exp: float = 0.0

    def __call__(self) -> str:
        now = time.time()
        if self._token is None or now >= (self._exp - TOKEN_REFRESH_SKEW_SECONDS):
            self._token = self._fetch()
            self._exp = _token_expiry(self._token) or (now + 3000.0)
            LOG.debug("Acquired/refreshed Fabric access token (valid until ~%.0f).", self._exp)
        return self._token


def _resolve_token(token: TokenLike) -> str:
    return token() if callable(token) else token


# ---------------------------------------------------------------------------
# Fabric REST helpers (stdlib only) with retry/backoff + LRO polling
# ---------------------------------------------------------------------------

def _fabric_request(
    method: str,
    url: str,
    token: TokenLike,
    body: Optional[dict] = None,
) -> Tuple[int, Dict[str, str], dict]:
    """Issue a single Fabric REST call, retrying on throttling (429) and transient 5xx.

    ``token`` may be a string or a callable (TokenProvider) — the latter enables token refresh
    across long-running operations. Returns (status_code, response_headers, json_payload).
    Raises ``MirrorError`` on a non-retryable HTTP error or once retries are exhausted.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(url=url, method=method, data=data)
        request.add_header("Authorization", f"Bearer {_resolve_token(token)}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:  # noqa: S310 - fixed Fabric host
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
                        "Fabric %s %s -> HTTP %s; retrying in %ss (attempt %s/%s)",
                        method, url, exc.code, delay, attempt, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
            raise MirrorError(
                f"Fabric {method} {url} failed: HTTP {exc.code} {exc.reason} {body_text}".strip()
            ) from exc
        except urllib.error.URLError as exc:
            if attempt <= MAX_RETRIES:
                delay = _retry_delay(None, attempt)
                LOG.warning(
                    "Fabric %s %s network error (%s); retrying in %ss (attempt %s/%s)",
                    method, url, exc.reason, delay, attempt, MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            raise MirrorError(f"Fabric {method} {url} failed: {exc.reason}") from exc


def _retry_delay(retry_after: Optional[str], attempt: int) -> int:
    """Honor a server Retry-After when present, else exponential backoff."""
    if retry_after:
        try:
            return max(1, int(float(retry_after)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _poll_long_running(
    headers: Dict[str, str], token: TokenLike, *, timeout: int = LRO_TIMEOUT_SECONDS
) -> dict:
    """Poll a Fabric long-running operation (202 + Operation-Location) until terminal state.

    Returns the final operation result payload. Raises ``MirrorError`` on Failed status or timeout.
    """
    op_url = headers.get("operation-location") or headers.get("location")
    if not op_url:
        return {}  # completed synchronously
    poll_interval = LRO_POLL_DEFAULT_SECONDS
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            poll_interval = max(1, int(float(retry_after)))
        except (TypeError, ValueError):
            pass

    deadline = time.monotonic() + timeout
    while True:
        status, op_headers, payload = _fabric_request("GET", op_url, token)
        state = str(payload.get("status", "")).lower()
        LOG.debug("LRO %s -> status=%s (http %s)", op_url, state or "<none>", status)
        if state in ("succeeded", "completed"):
            result_url = op_headers.get("location")
            if result_url and result_url != op_url:
                _, _, result = _fabric_request("GET", result_url, token)
                return result
            return payload
        if state in ("failed", "canceled", "cancelled"):
            raise MirrorError(
                f"Fabric long-running operation {op_url} ended in state {state!r}: {payload}"
            )
        if time.monotonic() >= deadline:
            raise MirrorError(
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


def _list_paginated(path: str, token: TokenLike) -> List[dict]:
    """Return all ``value`` entries across a paginated Fabric list endpoint."""
    items: List[dict] = []
    url = f"{FABRIC_API_BASE}{path}"
    seen_tokens = set()
    while url:
        _, _, payload = _fabric_request("GET", url, token)
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


# ---------------------------------------------------------------------------
# Workspace + mirrored-catalog operations (find-or-create, payload building)
# ---------------------------------------------------------------------------

def resolve_workspace_id(token: TokenLike, plan: MirrorPlan) -> str:
    """Return the Fabric workspace GUID: from the plan if present, else resolve by display name."""
    if plan.workspace_id:
        return plan.workspace_id
    target = (plan.workspace_name or "").strip().lower()
    for ws in _list_paginated("/workspaces", token):
        if str(ws.get("displayName", "")).strip().lower() == target:
            ws_id = _clean(ws.get("id"))
            if ws_id:
                LOG.info("Resolved workspace %r -> %s.", plan.workspace_name, ws_id)
                return ws_id
    raise MirrorError(
        f"no Fabric workspace named {plan.workspace_name!r} is visible to this principal "
        "(check Step 10 created/assigned it and the principal is a workspace member)."
    )


def _b64_part(payload: object) -> str:
    """JSON-serialize (dict) or pass-through (str), then base64-encode for a definition part."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def build_definition_json(plan: MirrorPlan) -> dict:
    """Build the ``definition.json`` content for the mirrored catalog (R1 §4.3).

    Includes ``autoSync`` inline (so a single create enables 15-min sync) and, for Partial mode, a
    ``mirrorConfiguration`` that scopes the mirror to the selected schema(s) — each mirrored Full.
    """
    definition: dict = {
        "$schema": DEFINITION_SCHEMA,
        "catalogName": plan.catalog_name,
        "databricksWorkspaceConnectionId": plan.connection_id,
        "autoSync": "Enabled" if plan.auto_sync else "Disabled",
        "mirroringMode": plan.mirroring_mode,
    }
    if plan.is_partial:
        definition["mirrorConfiguration"] = {
            "schemas": [
                {"name": schema, "mirroringMode": "Full"} for schema in plan.schemas
            ]
        }
    if plan.storage_connection_id:
        definition["storageConnectionId"] = plan.storage_connection_id
    return definition


def build_platform_json(plan: MirrorPlan) -> dict:
    """Build the ``.platform`` metadata part for a create-with-definition request."""
    return {
        "$schema": PLATFORM_SCHEMA,
        "metadata": {
            "type": MIRRORED_ITEM_TYPE,
            "displayName": plan.item_name,
            "description": plan.description,
        },
        "config": {
            "version": "2.0",
            "logicalId": "00000000-0000-0000-0000-000000000000",
        },
    }


def build_create_body(plan: MirrorPlan) -> dict:
    """Build the POST body for creating the mirrored catalog.

    * Partial mode -> the base64 **definition** method (definition.json + .platform parts), which is
      the only way to scope to specific schemas and to embed ``autoSync`` (R1 §4.3).
    * Full mode    -> the simpler **creationPayload** method (R1 §4.1, recommended); ``autoSync`` is
      then enabled separately via PATCH (it is an update-only property, R1 §4.2).
    """
    if plan.is_partial:
        return {
            "displayName": plan.item_name,
            "description": plan.description,
            "definition": {
                "parts": [
                    {
                        "path": "definition.json",
                        "payload": _b64_part(build_definition_json(plan)),
                        "payloadType": "InlineBase64",
                    },
                    {
                        "path": ".platform",
                        "payload": _b64_part(build_platform_json(plan)),
                        "payloadType": "InlineBase64",
                    },
                ]
            },
        }

    creation_payload: dict = {
        "catalogName": plan.catalog_name,
        "databricksWorkspaceConnectionId": plan.connection_id,
        "mirroringMode": plan.mirroring_mode,  # "Full"
    }
    if plan.storage_connection_id:
        creation_payload["storageConnectionId"] = plan.storage_connection_id
    return {
        "displayName": plan.item_name,
        "description": plan.description,
        "creationPayload": creation_payload,
    }


def find_mirrored_catalog(token: TokenLike, workspace_id: str, display_name: str) -> Optional[dict]:
    """Return the mirrored-catalog item whose displayName matches (case-insensitive), or None."""
    target = display_name.strip().lower()
    for item in _list_paginated(f"/workspaces/{workspace_id}/{MIRRORED_COLLECTION}", token):
        if str(item.get("displayName", "")).strip().lower() == target:
            return item
    return None


def get_mirrored_catalog(token: TokenLike, workspace_id: str, item_id: str) -> dict:
    _, _, payload = _fabric_request(
        "GET", f"{FABRIC_API_BASE}/workspaces/{workspace_id}/{MIRRORED_COLLECTION}/{item_id}", token
    )
    return payload


def create_mirrored_catalog(token: TokenLike, workspace_id: str, plan: MirrorPlan) -> dict:
    """POST the create request and resolve the long-running operation (201 sync / 202 async)."""
    LOG.info(
        "Creating Mirrored Azure Databricks Catalog %r (%s mode%s) ...",
        plan.item_name,
        plan.mirroring_mode,
        f", schemas={plan.schemas}" if plan.is_partial else "",
    )
    status, headers, payload = _fabric_request(
        "POST",
        f"{FABRIC_API_BASE}/workspaces/{workspace_id}/{MIRRORED_COLLECTION}",
        token,
        body=build_create_body(plan),
    )
    if status == 202:
        LOG.info("Create accepted (202) — polling the long-running provisioning operation ...")
        result = _poll_long_running(headers, token)
        if result:
            payload = result
    item_id = _clean(payload.get("id"))
    if not item_id:
        # Async result body may omit the id; fall back to a find-by-name lookup.
        found = find_mirrored_catalog(token, workspace_id, plan.item_name)
        if found:
            return found
        raise MirrorError(
            "mirrored catalog create returned no item id and it could not be found by name; "
            "inspect the workspace in the Fabric portal."
        )
    return payload


def ensure_auto_sync(token: TokenLike, workspace_id: str, item_id: str, plan: MirrorPlan) -> None:
    """Idempotently enable 15-min auto-sync via PATCH (update-only property; Item.Execute.All — R1 §4.2)."""
    desired = "Enabled" if plan.auto_sync else "Disabled"
    current = get_mirrored_catalog(token, workspace_id, item_id)
    current_props = current.get("properties") if isinstance(current.get("properties"), dict) else {}
    if str(current_props.get("autoSync", "")).lower() == desired.lower():
        LOG.info("Auto-sync already %s — no-op.", desired)
        return
    LOG.info("Setting auto-sync to %s (PATCH) ...", desired)
    try:
        _fabric_request(
            "PATCH",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/{MIRRORED_COLLECTION}/{item_id}",
            token,
            body={"properties": {"autoSync": desired, "mirroringMode": plan.mirroring_mode}},
        )
        LOG.info("Auto-sync set to %s.", desired)
    except MirrorError as exc:
        msg = str(exc)
        # Enabling autoSync needs Item.Execute.All; surface a clear, non-fatal hint on 403.
        if " HTTP 403" in msg:
            LOG.warning(
                "Could not set auto-sync via PATCH (%s). The token likely lacks the "
                "Item.Execute.All scope. Enable it in the portal (mirrored item -> settings) or "
                "grant the scope, then re-run. Continuing.",
                msg,
            )
            return
        raise


def wait_for_first_sync(token: TokenLike, workspace_id: str, item_id: str) -> Optional[str]:
    """Best-effort wait for the initial metadata sync to reach a healthy state.

    Returns the final mirrorStatus string (e.g. ``Mirrored``), or None if undetermined / timed out.
    Never hard-fails: the authoritative verification runs separately in plan Step 11.
    """
    LOG.info("Waiting (best-effort) for the first metadata sync to complete ...")
    deadline = time.monotonic() + FIRST_SYNC_TIMEOUT_SECONDS
    last_status: Optional[str] = None
    while True:
        item = get_mirrored_catalog(token, workspace_id, item_id)
        props = item.get("properties") if isinstance(item.get("properties"), dict) else {}
        mirror_status = str(props.get("mirrorStatus", "")).strip()
        sync = props.get("syncDetails") if isinstance(props.get("syncDetails"), dict) else {}
        sync_status = str(sync.get("status", "")).strip()
        last_status = mirror_status or last_status
        LOG.debug("mirrorStatus=%r syncDetails.status=%r", mirror_status, sync_status)
        if mirror_status.lower() in ("mirrored", "running") or sync_status.lower() == "success":
            LOG.info(
                "First sync healthy (mirrorStatus=%s, syncDetails.status=%s). SQL analytics "
                "endpoint + OneLake table shortcuts should now be available.",
                mirror_status or "?", sync_status or "?",
            )
            return mirror_status or "Mirrored"
        if sync_status.lower() in ("failed", "failure"):
            err = sync.get("errorInfo")
            LOG.warning("First metadata sync reported a failure: %s. Check the portal.", err)
            return mirror_status or "Failed"
        if time.monotonic() >= deadline:
            LOG.warning(
                "Timed out after %ss waiting for the first sync (last mirrorStatus=%r). The mirror "
                "may still finish; verify in the portal / run plan Step 11 verification.",
                FIRST_SYNC_TIMEOUT_SECONDS, last_status,
            )
            return last_status
        time.sleep(FIRST_SYNC_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Auth-identity guard (R1/R7): SP vs user token
# ---------------------------------------------------------------------------

def guard_token_identity(token: str, plan: MirrorPlan) -> None:
    """Fail gracefully when only an app-only SP token is available (R1/R7).

    Per R1/R7 the Databricks workspace connection's one-time OAuth consent is UI-assisted and some
    tenants require a **user (delegated) token** to create the mirror. If the acquired token is
    app-only (service principal) and ``--allow-service-principal`` was not passed, abort with a
    clear, actionable message instead of letting the create fail with an opaque 401/403.
    """
    app_only = is_app_only_token(token)
    if app_only is True and not plan.allow_service_principal:
        raise MirrorError(
            "the acquired Fabric token appears to be an app-only **service-principal** token. "
            "Creating a Mirrored Azure Databricks Catalog (and consenting to the Databricks "
            "connection) typically requires a **user (organizational-account) token** in this "
            "tenant (R1/R7). Run `az login` as an organizational user who is a member/admin of the "
            "Databricks workspace and re-run, OR pass --allow-service-principal if your tenant is "
            "configured to permit SP creation. See plan.md Step 11 / docs/manual-steps.md "
            "(one-time Databricks connection OAuth consent)."
        )
    if app_only is True:
        LOG.warning(
            "Proceeding with an app-only service-principal token because --allow-service-principal "
            "was set. If the create is rejected, retry with a user token (R1/R7)."
        )
    elif app_only is None:
        LOG.debug("Could not determine token identity type from claims; proceeding.")


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def run(plan: MirrorPlan, *, dry_run: bool = False, skip_sync_wait: bool = False) -> int:
    """Execute find-or-create + auto-sync + first-sync wait. Returns an exit code."""
    if dry_run:
        return _print_dry_run(plan)

    provider = TokenProvider()
    token = provider()  # initial acquisition (also used for the identity guard)

    # Auth-identity guard first — fail fast & graceful before any mutation (R1/R7).
    guard_token_identity(token, plan)

    workspace_id = resolve_workspace_id(provider, plan)

    # Find-or-create the mirrored catalog item (idempotent).
    existing = find_mirrored_catalog(provider, workspace_id, plan.item_name)
    if existing:
        item_id = str(existing["id"])
        LOG.info(
            "Mirrored catalog %r already exists (%s) — reusing (idempotent).",
            plan.item_name, item_id,
        )
    else:
        created = create_mirrored_catalog(provider, workspace_id, plan)
        item_id = str(created["id"])
        LOG.info("Created mirrored catalog %r (%s).", plan.item_name, item_id)

    # Ensure 15-min auto-sync (idempotent PATCH). For Partial mode autoSync was set inline at create,
    # but we re-assert it here so the find/reuse path and Full mode both end up Enabled.
    ensure_auto_sync(provider, workspace_id, item_id, plan)

    # Best-effort wait for the first sync so downstream steps see populated tables.
    final_status: Optional[str] = None
    if not skip_sync_wait:
        final_status = wait_for_first_sync(provider, workspace_id, item_id)

    LOG.info(
        "Done. workspace_id=%s mirrored_catalog_id=%s catalog=%s mode=%s schemas=%s "
        "auto_sync=%s mirrorStatus=%s",
        workspace_id, item_id, plan.catalog_name, plan.mirroring_mode,
        plan.schemas if plan.is_partial else "<all>", plan.auto_sync,
        final_status or "<not-waited>",
    )
    return 0


def _print_dry_run(plan: MirrorPlan) -> int:
    """Credential-free preview of every intended REST call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    LOG.info(
        "[DRY-RUN] Databricks workspace url: %s | connection id: %s",
        plan.databricks_url or "<not set>", plan.connection_id,
    )
    if plan.workspace_id:
        LOG.info("[DRY-RUN] Workspace id (from config/CLI): %s", plan.workspace_id)
    else:
        LOG.info(
            "[DRY-RUN] Would GET %s/workspaces and resolve %r -> workspace GUID.",
            FABRIC_API_BASE, plan.workspace_name,
        )
    LOG.info(
        "[DRY-RUN] Would GET %s/workspaces/{id}/%s and, if %r absent, POST to create it.",
        FABRIC_API_BASE, MIRRORED_COLLECTION, plan.item_name,
    )
    body = build_create_body(plan)
    if plan.is_partial:
        LOG.info(
            "[DRY-RUN] Create method: definition (Partial). definition.json = %s",
            json.dumps(build_definition_json(plan), indent=2),
        )
        LOG.info("[DRY-RUN] POST body (definition, base64 redacted): displayName=%r, parts=%s",
                 plan.item_name, [p["path"] for p in body["definition"]["parts"]])
    else:
        LOG.info(
            "[DRY-RUN] Create method: creationPayload (Full). POST body = %s",
            json.dumps(body, indent=2),
        )
    LOG.info(
        "[DRY-RUN] Would PATCH %s/workspaces/{id}/%s/{itemId} {properties.autoSync=%s} "
        "(needs Item.Execute.All — R1 §4.2).",
        FABRIC_API_BASE, MIRRORED_COLLECTION, "Enabled" if plan.auto_sync else "Disabled",
    )
    LOG.info(
        "[DRY-RUN] Would then GET the item and wait for mirrorStatus=Mirrored / "
        "syncDetails.status=Success (first sync).",
    )
    LOG.info(
        "[DRY-RUN] Auth note (R1/R7): create requires a USER token in most tenants; an app-only "
        "service-principal token would be rejected unless --allow-service-principal is set.",
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/attach the Zava Mirrored Azure Databricks Catalog in the Step-10 Fabric "
                    "workspace (Fabric MirroredAzureDatabricksCatalog REST API — R1).",
    )
    parser.add_argument(
        "--config",
        help="Path to deploy_config.json (defaults to fabric/config/deploy_config.json, then the "
             "committed .sample.json).",
    )
    parser.add_argument("--workspace-id", help="Fabric workspace GUID (overrides config/env).")
    parser.add_argument("--workspace-name", help="Fabric workspace display name (resolved to GUID).")
    parser.add_argument(
        "--connection-id",
        help="Fabric connection UUID for the Azure Databricks workspace (overrides config/env).",
    )
    parser.add_argument(
        "--storage-connection-id",
        help="Optional Fabric connection UUID for ADLS Gen2 storage (firewall scenarios).",
    )
    parser.add_argument("--catalog", dest="catalog_name", help="Unity Catalog catalog name (e.g. zava).")
    parser.add_argument(
        "--schema",
        help="Unity Catalog schema to mirror (Partial mode). Defaults to source.gold_schema.",
    )
    parser.add_argument("--item-name", help="Display name for the mirrored catalog item.")
    parser.add_argument(
        "--mode", dest="mirroring_mode", choices=["Full", "Partial", "full", "partial"],
        help="Mirroring mode: Partial (gold schema only, default) or Full (whole catalog).",
    )
    sync_group = parser.add_mutually_exclusive_group()
    sync_group.add_argument(
        "--auto-sync", dest="auto_sync", action="store_true", default=None,
        help="Enable 15-min auto-sync (default unless disabled in config).",
    )
    sync_group.add_argument(
        "--no-auto-sync", dest="auto_sync", action="store_false",
        help="Disable auto-sync (metadata refreshed manually).",
    )
    parser.add_argument(
        "--allow-service-principal", action="store_true",
        help="Proceed even if the token is an app-only service-principal token (R1/R7: some tenants "
             "require a user token to create the mirror).",
    )
    parser.add_argument(
        "--skip-sync-wait", action="store_true",
        help="Do not wait for the first metadata sync after create/attach.",
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
            workspace_id=args.workspace_id,
            workspace_name=args.workspace_name,
            connection_id=args.connection_id,
            storage_connection_id=args.storage_connection_id,
            catalog_name=args.catalog_name,
            schema=args.schema,
            item_name=args.item_name,
            mirroring_mode=args.mirroring_mode,
            auto_sync=args.auto_sync,
            allow_service_principal=args.allow_service_principal,
        )
        return run(plan, dry_run=args.dry_run, skip_sync_wait=args.skip_sync_wait)
    except MirrorError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
