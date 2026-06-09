#!/usr/bin/env python3
"""Create (idempotent find-or-create) a **OneLake ADLS Gen2 shortcut** in the Step-10 Fabric
Lakehouse, pointing at the **Databricks-managed** ``abfss://`` storage path discovered in Step 9, so
the Lakeflow **Variation-2** curated Delta data is readable via **Direct Lake** (R10 §4 Step 4, §2.4).

This is the secured **Variation 2** ingestion path (the *shortcut-to-storage* alternative to the
zero-copy *Mirrored Catalog* of Variation 1 — see ``10_create_mirrored_catalog.py``). It lands
Databricks-curated data in OneLake WITHOUT ETL by creating a shortcut straight onto the ADLS Gen2
path that backs a Unity-Catalog table, then exposing it under the Lakehouse ``Tables/`` folder where
Direct Lake auto-recognizes the Delta folder as a table.

  !!! GOVERNANCE CAVEAT — sub-pattern 2A (managed-storage shortcut) — R10 §5.1 !!!
  --------------------------------------------------------------------------------
  When the shortcut targets the **managed-storage** path that backs a UC *managed* streaming
  table / materialized view (the ``__unitystorage/...`` layout), it reads the Delta files
  **directly at the storage layer** and therefore **BYPASSES Unity Catalog enforcement**:
    * UC row-level security (RLS), column-level masking (CLM), ABAC, and credential vending
      are NOT applied — the data is exposed exactly as it sits on disk.
    * The ``__unitystorage`` hash path is an INTERNAL, non-contractual layout that UC may
      relocate / compact at any time — treat it as best-effort, not a stable contract.
  Mitigations the demo relies on (R10 §5.1, §6.3, §6.4):
    * Re-enforce governance IN FABRIC: OneLake security (data-access roles) + storage RBAC
      (Storage Blob Data Reader scoped to the account/folder) + the network hardening applied
      by ``infra/main.bicep`` in Step 12 (firewall default-deny + trusted-workspace rule).
    * Avoid destructive operations on the source; treat the shortcut as read-only.
  The cleaner alternative (sub-pattern 2B) shortcuts a **dedicated external location** written by a
  Lakeflow sink — a stable path you own. This script supports BOTH: point ``shortcut.adls_subpath``
  (or ``shortcut.abfss_path``) at whichever path Step 9 discovered.

What this script does
---------------------
1. Resolves all identifiers from ``deploy_config.json`` (+ CLI / env overrides). **No secrets.**
     * Fabric workspace GUID  -> ``workspace.existing_workspace_id`` / ``FABRIC_WORKSPACE_ID``,
       else resolved at runtime from ``workspace.name`` via ``GET /v1/workspaces``.
     * Lakehouse (Step 10)     -> ``shortcut.lakehouse_id`` / ``FABRIC_LAKEHOUSE_ID`` or by name
       ``shortcut.lakehouse_name`` / ``lakehouse.name`` / ``FABRIC_LAKEHOUSE_NAME``
       (find-or-create — never duplicates).
     * ADLS connection id      -> ``shortcut.connection_id`` / ``FABRIC_ADLS_CONNECTION_ID``
       (the Fabric connection bound to the ADLS Gen2 account; created via the Connections REST
       API / portal — R10 §4 Step 2). **Required.**
     * ADLS target            -> ``shortcut.adls_location`` + ``shortcut.adls_subpath`` OR a single
       ``shortcut.abfss_path`` (``abfss://container@acct.dfs.core.windows.net/path``) that is parsed
       into the REST ``location`` (``https://acct.dfs.core.windows.net``) + ``subpath``
       (``/container/path``) — this is the Step-9 discovered managed-storage path.
     * Shortcut placement/name -> ``shortcut.path`` (default ``Tables``) / ``shortcut.name``.
2. Authenticates to the Fabric REST API (scope ``https://api.fabric.microsoft.com/.default``) via
   ``DefaultAzureCredential`` (``az login`` / **Workspace Identity** / managed identity / env service
   principal), falling back to the Azure CLI (``az account get-access-token``) when ``azure-identity``
   is absent. A token provider re-acquires the bearer token before it expires so long-running
   Lakehouse provisioning never fails on an expired token (R7 §9). Both **Workspace Identity** and
   **Service principal** are supported identities for the Create Shortcut API (R10 §4 Step 4, §5.2).
3. **Find-or-create** the Lakehouse (idempotent): match by ``displayName`` and reuse; else create
   (handles the 201 sync / 202 async long-running create).
4. **Find-or-create** the shortcut (idempotent): ``GET .../items/{itemId}/shortcuts/{path}/{name}``;
   reuse when it already targets the same ADLS path; else
   ``POST .../items/{itemId}/shortcuts?shortcutConflictPolicy=Abort`` with the ``adlsGen2`` target
   (``location`` / ``subpath`` / ``connectionId``) — R10 §4 Step 4. Retries on 429 / transient 5xx.

REST endpoints used (Fabric Core API — R10 §4, §3)
--------------------------------------------------
    GET    /v1/workspaces                                            (resolve workspace by name)
    GET    /v1/workspaces/{workspaceId}/lakehouses                   (list / find-by-name)
    POST   /v1/workspaces/{workspaceId}/lakehouses                   (create lakehouse — LRO)
    GET    /v1/workspaces/{workspaceId}/items/{itemId}/shortcuts/{path}/{name}   (get one shortcut)
    POST   /v1/workspaces/{workspaceId}/items/{itemId}/shortcuts     (create shortcut)

Manual prerequisites (R10 — documented in plan.md Step 12; also docs/manual-steps.md)
-------------------------------------------------------------------------------------
* The **ADLS Gen2 connection** the shortcut binds to is created via the Connections REST API or the
  portal (one-time **OAuth consent** when using an Organizational account — R10 §4 Step 2). Use a
  **Service principal / Workspace Identity** credential to stay fully programmatic. Copy its
  connection id into ``shortcut.connection_id``.
* The **Fabric Workspace Identity** (firewall-traversal identity for the hardened storage account)
  is created in Step 10 and granted Storage Blob Data Reader by the Step-12 ``infra/main.bicep``
  hardening (R10 §6.1–6.3).

Security / Phase-0 notes
------------------------
* **No secrets.** Only names / ids / placeholders are read from config; the bearer token is acquired
  at runtime via ``DefaultAzureCredential`` / ``az login`` and never written to disk or logged.
* **Authoring phase:** use ``--dry-run`` to preview every intended REST call with NO authentication
  and NO changes. Do not run against a live tenant unless you intend to create the shortcut.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/20_create_shortcut.py --dry-run

    # Create/attach using identifiers from deploy_config.json:
    python fabric/scripts/20_create_shortcut.py

    # Explicit overrides:
    python fabric/scripts/20_create_shortcut.py \
        --workspace-id 00000000-0000-0000-0000-000000000000 \
        --lakehouse-name zava_lakehouse \
        --connection-id 11111111-1111-1111-1111-111111111111 \
        --abfss-path abfss://unitycatalog@zavauc.dfs.core.windows.net/__unitystorage/rentals_curated \
        --shortcut-name rentals_curated
"""

from __future__ import annotations

import argparse
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

# Fabric REST control plane (R10 §3 / R7 §2).
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

# Shortcut placement defaults. Placing under "Tables" lets Direct Lake auto-recognize the Delta
# folder as a table (R10 §4 Step 4 note — "In the tables folder, you can create shortcuts only at
# the top level").
DEFAULT_SHORTCUT_PARENT_PATH = "Tables"
# shortcutConflictPolicy: Abort = fail (409) if a DIFFERENT shortcut already occupies the name; we
# pre-check with a GET for idempotency, so Abort is the safe default (never silently overwrites).
SHORTCUT_CONFLICT_POLICY = "Abort"

# Retry / long-running-operation tuning. Fabric uses standard Azure throttling (HTTP 429 +
# Retry-After) and 202-Accepted long-running operations (Operation-Location + Retry-After) — R7 §9.
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5
LRO_POLL_DEFAULT_SECONDS = 10
LRO_TIMEOUT_SECONDS = 900            # lakehouse provisioning is usually quick

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
# abfss://<container>@<account>.dfs.core.windows.net/<path...>
_ABFSS_RE = re.compile(
    r"^abfss://(?P<container>[^@/]+)@(?P<account>[^./]+)\.dfs\.core\.windows\.net(?P<path>/.*)?$",
    re.IGNORECASE,
)

LOG = logging.getLogger("zava.fabric.shortcut")

# Token provider callable: returns a (cached, auto-refreshed) bearer token string.
TokenLike = Union[str, Callable[[], str]]


class ShortcutError(RuntimeError):
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
            raise ShortcutError(f"--config path does not exist: {explicit}")
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
        raise ShortcutError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ShortcutError(f"config {path} must be a JSON object")
    return data


def parse_adls_target(
    *,
    abfss_path: Optional[str],
    adls_location: Optional[str],
    adls_subpath: Optional[str],
) -> Tuple[str, str]:
    """Resolve the shortcut's ADLS Gen2 ``location`` + ``subpath`` (R10 §4 Step 4).

    Accepts EITHER a single ``abfss://container@account.dfs.core.windows.net/path`` URI (parsed into
    its dfs https endpoint + ``/container/path`` subpath) OR an explicit ``location`` (the
    ``https://account.dfs.core.windows.net`` endpoint) + ``subpath`` (``/container/path``).
    Returns ``(location, subpath)``. Raises ``ShortcutError`` when neither form resolves.
    """
    if abfss_path:
        match = _ABFSS_RE.match(abfss_path.strip())
        if not match:
            raise ShortcutError(
                f"abfss_path is not a valid ADLS Gen2 abfss URI: {abfss_path!r} "
                "(expected abfss://<container>@<account>.dfs.core.windows.net/<path>)"
            )
        account = match.group("account")
        container = match.group("container")
        inner = (match.group("path") or "").lstrip("/")
        location = f"https://{account}.dfs.core.windows.net"
        subpath = "/" + "/".join(p for p in (container, inner) if p)
        return location, subpath

    location = _clean(adls_location)
    subpath = _clean(adls_subpath)
    if not location or not subpath:
        raise ShortcutError(
            "could not resolve the ADLS Gen2 target. Set shortcut.abfss_path (the Step-9 discovered "
            "abfss:// path), OR both shortcut.adls_location (https://<account>.dfs.core.windows.net) "
            "and shortcut.adls_subpath (/<container>/<path>). See R10 §3 (DESCRIBE DETAIL) for path "
            "discovery."
        )
    if not location.lower().startswith("https://"):
        raise ShortcutError(
            f"shortcut.adls_location must be an https dfs endpoint, got: {location!r}"
        )
    if not subpath.startswith("/"):
        subpath = "/" + subpath
    return location, subpath


class ShortcutPlan:
    """Resolved, validated plan for the shortcut operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_id: Optional[str],
        workspace_name: Optional[str],
        lakehouse_id: Optional[str],
        lakehouse_name: Optional[str],
        connection_id: str,
        adls_location: str,
        adls_subpath: str,
        parent_path: str,
        shortcut_name: str,
        create_lakehouse_if_missing: bool,
    ) -> None:
        self.config_path = config_path
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.lakehouse_id = lakehouse_id
        self.lakehouse_name = lakehouse_name
        self.connection_id = connection_id
        self.adls_location = adls_location
        self.adls_subpath = adls_subpath
        self.parent_path = parent_path
        self.shortcut_name = shortcut_name
        self.create_lakehouse_if_missing = create_lakehouse_if_missing


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_id: Optional[str] = None,
    workspace_name: Optional[str] = None,
    lakehouse_id: Optional[str] = None,
    lakehouse_name: Optional[str] = None,
    connection_id: Optional[str] = None,
    abfss_path: Optional[str] = None,
    adls_location: Optional[str] = None,
    adls_subpath: Optional[str] = None,
    parent_path: Optional[str] = None,
    shortcut_name: Optional[str] = None,
    no_create_lakehouse: bool = False,
) -> ShortcutPlan:
    """Resolve the shortcut plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable.
    Raises ``ShortcutError`` naming any required field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    src_cfg = cfg.get("source", {}) if isinstance(cfg.get("source"), dict) else {}
    sc_cfg = cfg.get("shortcut", {}) if isinstance(cfg.get("shortcut"), dict) else {}
    lh_cfg = cfg.get("lakehouse", {}) if isinstance(cfg.get("lakehouse"), dict) else {}

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
        raise ShortcutError(f"resolved Fabric workspace id is not a GUID: {ws_id!r}")
    if not ws_id and not ws_name:
        raise ShortcutError(
            "could not resolve the Fabric workspace (set workspace.existing_workspace_id or "
            "workspace.name in deploy_config.json, --workspace-id/--workspace-name, or "
            "FABRIC_WORKSPACE_ID/FABRIC_WORKSPACE_NAME). This is the Step-10 workspace."
        )

    # --- Lakehouse (Step 10): GUID preferred, else by display name (find-or-create) ---
    lh_id = (
        lakehouse_id
        or _clean(sc_cfg.get("lakehouse_id"))
        or _clean(lh_cfg.get("id"))
        or _clean(os.environ.get("FABRIC_LAKEHOUSE_ID"))
    )
    lh_name = (
        lakehouse_name
        or _clean(sc_cfg.get("lakehouse_name"))
        or _clean(lh_cfg.get("name"))
        or _clean(os.environ.get("FABRIC_LAKEHOUSE_NAME"))
    )
    if lh_id and not _GUID_RE.match(lh_id):
        raise ShortcutError(f"resolved Fabric lakehouse id is not a GUID: {lh_id!r}")
    if not lh_id and not lh_name:
        # Default to a deterministic, catalog-derived name so the demo is reusable.
        catalog = _clean(src_cfg.get("databricks_catalog")) or "zava"
        lh_name = f"{catalog}_lakehouse"
        LOG.debug("No lakehouse id/name supplied; defaulting to %r.", lh_name)

    # --- ADLS connection id (REQUIRED — created via Connections REST API / portal, R10 §4 Step 2) ---
    conn_id = (
        connection_id
        or _clean(sc_cfg.get("connection_id"))
        or _clean(os.environ.get("FABRIC_ADLS_CONNECTION_ID"))
    )
    if not conn_id:
        raise ShortcutError(
            "no ADLS Gen2 connection id resolved. Create the Fabric connection to the ADLS Gen2 "
            "account (Connections REST API or portal; one-time OAuth consent for organizational "
            "accounts — see plan.md Step 12 / docs/manual-steps.md), then set shortcut.connection_id "
            "in deploy_config.json (or --connection-id / FABRIC_ADLS_CONNECTION_ID)."
        )
    if not _GUID_RE.match(conn_id):
        raise ShortcutError(
            f"connection_id must be the Fabric connection UUID, got: {conn_id!r}"
        )

    # --- ADLS target path (the Step-9 discovered managed-storage / external abfss path) ---
    location, subpath = parse_adls_target(
        abfss_path=abfss_path or _clean(sc_cfg.get("abfss_path")),
        adls_location=adls_location or _clean(sc_cfg.get("adls_location")),
        adls_subpath=adls_subpath or _clean(sc_cfg.get("adls_subpath")),
    )

    # --- Shortcut placement + name ---
    parent = (
        parent_path
        or _clean(sc_cfg.get("path"))
        or DEFAULT_SHORTCUT_PARENT_PATH
    ).strip("/")
    name = (
        shortcut_name
        or _clean(sc_cfg.get("name"))
    )
    if not name:
        # Derive the shortcut name from the final segment of the target path (e.g. rentals_curated).
        tail = [seg for seg in subpath.split("/") if seg]
        name = tail[-1] if tail else "curated_shortcut"

    return ShortcutPlan(
        config_path=resolved_path,
        workspace_id=ws_id,
        workspace_name=ws_name,
        lakehouse_id=lh_id,
        lakehouse_name=lh_name,
        connection_id=conn_id,
        adls_location=location,
        adls_subpath=subpath,
        parent_path=parent,
        shortcut_name=name,
        create_lakehouse_if_missing=not no_create_lakehouse,
    )


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential, with az CLI fallback) + token provider
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Acquire a Fabric API bearer token via DefaultAzureCredential; fall back to the ``az`` CLI.

    No secret is ever read from or written to disk: this relies on an existing ``az login``, a
    Workspace Identity / managed identity, or environment service-principal credentials (R10 §5.2).
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
        raise ShortcutError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI (`az`) "
            "is available. Install azure-identity or run `az login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise ShortcutError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ShortcutError("could not parse access token from az CLI output") from exc
    return token


def _token_expiry(token: str) -> Optional[float]:
    """Return the JWT 'exp' (epoch seconds) if present, else None (no signature verification)."""
    import base64
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
        exp = claims.get("exp") if isinstance(claims, dict) else None
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 - never let token introspection break the run
        return None


class TokenProvider:
    """Caches a Fabric bearer token and transparently refreshes it before it expires (R7 §9)."""

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
    *,
    tolerate: Tuple[int, ...] = (),
) -> Tuple[int, Dict[str, str], dict]:
    """Issue a single Fabric REST call, retrying on throttling (429) and transient 5xx.

    ``token`` may be a string or a callable (TokenProvider) — the latter enables token refresh
    across long-running operations. ``tolerate`` lists HTTP status codes that should be RETURNED
    (not raised) — used for the idempotent 404-on-GET probe. Returns (status, headers, json).
    Raises ``ShortcutError`` on a non-retryable, non-tolerated HTTP error or exhausted retries.
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
            if exc.code in tolerate:
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                return exc.code, headers, {}
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
            raise ShortcutError(
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
            raise ShortcutError(f"Fabric {method} {url} failed: {exc.reason}") from exc


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
    """Poll a Fabric long-running operation (202 + Operation-Location) until terminal state."""
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
        _, op_headers, payload = _fabric_request("GET", op_url, token)
        state = str(payload.get("status", "")).lower()
        LOG.debug("LRO %s -> status=%s", op_url, state or "<none>")
        if state in ("succeeded", "completed"):
            result_url = op_headers.get("location")
            if result_url and result_url != op_url:
                _, _, result = _fabric_request("GET", result_url, token)
                return result
            return payload
        if state in ("failed", "canceled", "cancelled"):
            raise ShortcutError(
                f"Fabric long-running operation {op_url} ended in state {state!r}: {payload}"
            )
        if time.monotonic() >= deadline:
            raise ShortcutError(
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
# Workspace + lakehouse + shortcut operations (find-or-create, idempotent)
# ---------------------------------------------------------------------------

def resolve_workspace_id(token: TokenLike, plan: ShortcutPlan) -> str:
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
    raise ShortcutError(
        f"no Fabric workspace named {plan.workspace_name!r} is visible to this principal "
        "(check Step 10 created/assigned it and the principal is a workspace member)."
    )


def find_lakehouse(token: TokenLike, workspace_id: str, display_name: str) -> Optional[dict]:
    """Return the lakehouse item whose displayName matches (case-insensitive), or None."""
    target = display_name.strip().lower()
    for item in _list_paginated(f"/workspaces/{workspace_id}/lakehouses", token):
        if str(item.get("displayName", "")).strip().lower() == target:
            return item
    return None


def resolve_lakehouse_id(token: TokenLike, workspace_id: str, plan: ShortcutPlan) -> str:
    """Find-or-create the Step-10 lakehouse; return its item GUID (idempotent)."""
    if plan.lakehouse_id:
        return plan.lakehouse_id

    existing = find_lakehouse(token, workspace_id, plan.lakehouse_name or "")
    if existing:
        lh_id = str(existing["id"])
        LOG.info("Lakehouse %r already exists (%s) — reusing (idempotent).",
                 plan.lakehouse_name, lh_id)
        return lh_id

    if not plan.create_lakehouse_if_missing:
        raise ShortcutError(
            f"lakehouse {plan.lakehouse_name!r} not found and --no-create-lakehouse was set. "
            "Provide an existing lakehouse id/name (Step 10) or allow creation."
        )

    LOG.info("Creating Lakehouse %r ...", plan.lakehouse_name)
    status, headers, payload = _fabric_request(
        "POST",
        f"{FABRIC_API_BASE}/workspaces/{workspace_id}/lakehouses",
        token,
        body={"displayName": plan.lakehouse_name},
    )
    if status == 202:
        LOG.info("Lakehouse create accepted (202) — polling provisioning operation ...")
        result = _poll_long_running(headers, token)
        if result:
            payload = result
    lh_id = _clean(payload.get("id"))
    if not lh_id:
        found = find_lakehouse(token, workspace_id, plan.lakehouse_name or "")
        if found:
            return str(found["id"])
        raise ShortcutError(
            "lakehouse create returned no item id and it could not be found by name; "
            "inspect the workspace in the Fabric portal."
        )
    LOG.info("Created Lakehouse %r (%s).", plan.lakehouse_name, lh_id)
    return lh_id


def _shortcut_target_matches(existing: dict, plan: ShortcutPlan) -> bool:
    """True when an existing shortcut already targets the same ADLS Gen2 location + subpath."""
    target = existing.get("target") if isinstance(existing.get("target"), dict) else {}
    adls = target.get("adlsGen2") if isinstance(target.get("adlsGen2"), dict) else {}
    same_loc = str(adls.get("location", "")).rstrip("/").lower() == plan.adls_location.rstrip("/").lower()
    same_sub = str(adls.get("subpath", "")).rstrip("/") == plan.adls_subpath.rstrip("/")
    return bool(same_loc and same_sub)


def find_shortcut(token: TokenLike, workspace_id: str, item_id: str, plan: ShortcutPlan) -> Optional[dict]:
    """Return the shortcut at parent_path/name if it exists (GET, tolerating 404), else None."""
    enc_path = urllib.parse.quote(plan.parent_path, safe="")
    enc_name = urllib.parse.quote(plan.shortcut_name, safe="")
    url = (
        f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}"
        f"/shortcuts/{enc_path}/{enc_name}"
    )
    status, _, payload = _fabric_request("GET", url, token, tolerate=(404,))
    if status == 404:
        return None
    return payload


def build_shortcut_body(plan: ShortcutPlan) -> dict:
    """Build the Create Shortcut POST body (adlsGen2 target) — R10 §4 Step 4."""
    return {
        "path": plan.parent_path,
        "name": plan.shortcut_name,
        "target": {
            "adlsGen2": {
                "location": plan.adls_location,
                "subpath": plan.adls_subpath,
                "connectionId": plan.connection_id,
            }
        },
    }


def create_shortcut(token: TokenLike, workspace_id: str, item_id: str, plan: ShortcutPlan) -> dict:
    """POST the Create Shortcut request (conflictPolicy=Abort). Returns the created shortcut."""
    url = (
        f"{FABRIC_API_BASE}/workspaces/{workspace_id}/items/{item_id}"
        f"/shortcuts?shortcutConflictPolicy={SHORTCUT_CONFLICT_POLICY}"
    )
    LOG.info(
        "Creating ADLS Gen2 shortcut %r under %r -> %s%s ...",
        plan.shortcut_name, plan.parent_path, plan.adls_location, plan.adls_subpath,
    )
    _, _, payload = _fabric_request("POST", url, token, body=build_shortcut_body(plan))
    return payload


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def run(plan: ShortcutPlan, *, dry_run: bool = False) -> int:
    """Execute find-or-create of the lakehouse + shortcut. Returns an exit code."""
    if dry_run:
        return _print_dry_run(plan)

    provider = TokenProvider()
    provider()  # initial acquisition

    workspace_id = resolve_workspace_id(provider, plan)
    item_id = resolve_lakehouse_id(provider, workspace_id, plan)

    # Find-or-create the shortcut (idempotent).
    existing = find_shortcut(provider, workspace_id, item_id, plan)
    if existing:
        if _shortcut_target_matches(existing, plan):
            LOG.info(
                "Shortcut %r already exists under %r and targets the same ADLS path — reusing "
                "(idempotent).", plan.shortcut_name, plan.parent_path,
            )
        else:
            raise ShortcutError(
                f"a DIFFERENT shortcut named {plan.shortcut_name!r} already exists under "
                f"{plan.parent_path!r} (its target does not match {plan.adls_location}"
                f"{plan.adls_subpath}). Refusing to overwrite — rename or remove it first."
            )
    else:
        create_shortcut(provider, workspace_id, item_id, plan)
        LOG.info("Created shortcut %r under %r.", plan.shortcut_name, plan.parent_path)

    LOG.info(
        "Done. workspace_id=%s lakehouse_id=%s shortcut=%s/%s -> %s%s. "
        "GOVERNANCE: sub-pattern 2A reads managed storage DIRECTLY and BYPASSES UC RLS/CLM/ABAC "
        "enforcement — re-enforce via OneLake security + storage RBAC + the Step-12 firewall "
        "hardening (R10 §5.1/§6).",
        workspace_id, item_id, plan.parent_path, plan.shortcut_name,
        plan.adls_location, plan.adls_subpath,
    )
    return 0


def _print_dry_run(plan: ShortcutPlan) -> int:
    """Credential-free preview of every intended REST call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    if plan.workspace_id:
        LOG.info("[DRY-RUN] Workspace id (from config/CLI): %s", plan.workspace_id)
    else:
        LOG.info("[DRY-RUN] Would GET %s/workspaces and resolve %r -> workspace GUID.",
                 FABRIC_API_BASE, plan.workspace_name)
    if plan.lakehouse_id:
        LOG.info("[DRY-RUN] Lakehouse id (from config/CLI): %s", plan.lakehouse_id)
    else:
        LOG.info(
            "[DRY-RUN] Would GET %s/workspaces/{id}/lakehouses and, if %r absent, %s.",
            FABRIC_API_BASE, plan.lakehouse_name,
            "POST to create it" if plan.create_lakehouse_if_missing else "FAIL (--no-create-lakehouse)",
        )
    LOG.info(
        "[DRY-RUN] Would GET .../items/{lakehouseId}/shortcuts/%s/%s and, if absent, "
        "POST .../shortcuts?shortcutConflictPolicy=%s.",
        plan.parent_path, plan.shortcut_name, SHORTCUT_CONFLICT_POLICY,
    )
    LOG.info("[DRY-RUN] Create Shortcut POST body = %s",
             json.dumps(build_shortcut_body(plan), indent=2))
    LOG.info(
        "[DRY-RUN] GOVERNANCE CAVEAT (R10 §5.1): a managed-storage (sub-pattern 2A) shortcut reads "
        "Delta files directly and BYPASSES UC RLS/CLM/ABAC enforcement + credential vending. "
        "Re-enforce in Fabric (OneLake security + storage RBAC) and via the Step-12 network "
        "hardening (firewall default-deny + trusted-workspace rule).",
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a OneLake ADLS Gen2 shortcut (Variation 2) in the Step-10 Fabric "
                    "Lakehouse, pointing at the Step-9 discovered Databricks-managed abfss path "
                    "(Fabric OneLake Shortcut REST API — R10).",
    )
    parser.add_argument(
        "--config",
        help="Path to deploy_config.json (defaults to fabric/config/deploy_config.json, then the "
             "committed .sample.json).",
    )
    parser.add_argument("--workspace-id", help="Fabric workspace GUID (overrides config/env).")
    parser.add_argument("--workspace-name", help="Fabric workspace display name (resolved to GUID).")
    parser.add_argument("--lakehouse-id", help="Fabric Lakehouse item GUID (overrides config/env).")
    parser.add_argument("--lakehouse-name", help="Fabric Lakehouse display name (find-or-create).")
    parser.add_argument(
        "--connection-id",
        help="Fabric connection UUID bound to the ADLS Gen2 account (R10 §4 Step 2).",
    )
    parser.add_argument(
        "--abfss-path",
        help="Step-9 discovered abfss:// path "
             "(abfss://<container>@<account>.dfs.core.windows.net/<path>); parsed into location+subpath.",
    )
    parser.add_argument(
        "--adls-location",
        help="ADLS Gen2 https dfs endpoint (https://<account>.dfs.core.windows.net). "
             "Use with --adls-subpath instead of --abfss-path.",
    )
    parser.add_argument(
        "--adls-subpath",
        help="ADLS Gen2 subpath (/<container>/<path>). Use with --adls-location.",
    )
    parser.add_argument(
        "--shortcut-path", dest="parent_path",
        help=f"Parent folder for the shortcut (default {DEFAULT_SHORTCUT_PARENT_PATH!r} so Direct "
             "Lake auto-recognizes the Delta table).",
    )
    parser.add_argument("--shortcut-name", help="Shortcut display name (default: target path tail).")
    parser.add_argument(
        "--no-create-lakehouse", action="store_true",
        help="Fail instead of creating the lakehouse when it is not found.",
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
            lakehouse_id=args.lakehouse_id,
            lakehouse_name=args.lakehouse_name,
            connection_id=args.connection_id,
            abfss_path=args.abfss_path,
            adls_location=args.adls_location,
            adls_subpath=args.adls_subpath,
            parent_path=args.parent_path,
            shortcut_name=args.shortcut_name,
            no_create_lakehouse=args.no_create_lakehouse,
        )
        return run(plan, dry_run=args.dry_run)
    except ShortcutError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
