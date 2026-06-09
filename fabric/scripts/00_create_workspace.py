#!/usr/bin/env python3
"""Create (or attach to) the Zava demo Microsoft Fabric workspace, assign the F64 capacity,
and provision the Fabric **Workspace Identity** — all idempotently and as code (R7, R10).

What it does
------------
1. Resolves all identifiers from ``deploy_config.json`` (Step 1 schema): ``workspace.name`` /
   ``workspace.use_existing`` / ``workspace.existing_workspace_id`` and the capacity to bind
   (``workspace.capacity_ref`` if set, else ``capacity.name`` / ``capacity.existing_capacity_id``).
   CLI / environment overrides take precedence. **No secrets are read from config.**
2. Authenticates to the Fabric REST API (scope ``https://api.fabric.microsoft.com/.default``) via
   ``DefaultAzureCredential`` (``az login`` / managed identity / env service principal), falling
   back to the Azure CLI (``az account get-access-token``) when ``azure-identity`` is absent.
   Requires the SP tenant settings from Step 2 ("Service principals can create workspaces…" — R7).
3. **Find-or-create** the workspace (idempotent):
     * Existing path (``workspace.use_existing=true`` + ``existing_workspace_id``): GET the
       workspace by id and reuse it — never creates a duplicate.
     * Fresh path: list workspaces and match by ``displayName``; reuse if found, else
       ``POST /v1/workspaces`` to create it.
4. **Assign the capacity** (idempotent): resolve the Fabric capacity GUID by display name via
   ``GET /v1/capacities`` (the ``assignToCapacity`` body needs the capacity's Fabric **object id**,
   not its ARM resource id), then ``POST /v1/workspaces/{id}/assignToCapacity`` only when the
   workspace is not already bound to that capacity.
5. **Provision the Workspace Identity** (idempotent, long-running): ``POST
   /v1/workspaces/{id}/provisionIdentity`` and poll the returned operation to completion, then read
   the identity's object id (``workspaceIdentity.servicePrincipalId``) from ``GET`` and surface it
   so it can be written into ``workspace.identity_object_id`` for the Step 12 ADLS hardening rule.
6. (Optional) Add an admin **role assignment** for the deploy SP when an object id is supplied
   (``--admin-object-id`` / ``FABRIC_WORKSPACE_ADMIN_OBJECT_ID``) — see the Step 2 manual-steps row.

REST endpoints used (Fabric Core Workspaces API — R7 §2):
    POST   /v1/workspaces                                   (create)
    GET    /v1/workspaces                                   (list / find-by-name, paginated)
    GET    /v1/workspaces/{workspaceId}                     (read state + identity)
    POST   /v1/workspaces/{workspaceId}/assignToCapacity    (bind F64 capacity)
    POST   /v1/workspaces/{workspaceId}/provisionIdentity   (Workspace Identity — long-running)
    POST   /v1/workspaces/{workspaceId}/roleAssignments     (optional admin grant)
    GET    /v1/capacities                                   (resolve capacity GUID by name)

Workspace Identity — automation vs. UI (R7 / R10)
-------------------------------------------------
The Fabric **provisionIdentity** REST endpoint automates Workspace Identity creation. However, per
R7/R10 the operation can require a **one-time tenant/admin consent** and is **not always supported
for service-principal callers** in every tenant; when the API path is unavailable it must be created
in the UI (**Workspace settings -> Workspace identity -> + Workspace identity**). This script tries
the REST path first and, on a clear "not supported / forbidden" response, **falls back to a clear
instruction** instead of failing the whole deployment. The UI fallback is also recorded in
``docs/manual-steps.md`` (Step 10 row). Either way, capture the resulting identity object id into
``workspace.identity_object_id`` (consumed by Step 12).

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names / ids / placeholders only); auth is acquired at
  runtime via ``DefaultAzureCredential`` / ``az login``. Nothing is written to disk unless you opt in
  with ``--write-config`` (which refuses to touch any committed ``*.sample.json``).
* **Authoring phase:** do **not** run this against a live tenant unless you intend to create/modify a
  workspace. Use ``--dry-run`` to preview every intended REST call with **no** authentication and
  **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/00_create_workspace.py --dry-run

    # Create/attach using identifiers from deploy_config.json:
    python fabric/scripts/00_create_workspace.py

    # Fully explicit, and persist the captured identity object id into a real (non-sample) config:
    python fabric/scripts/00_create_workspace.py \
        --workspace-name zava-fabric-ws \
        --capacity zava-fabric-cap \
        --config fabric/config/deploy_config.json --write-config
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
import urllib.request
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fabric REST control plane (R7 §2). Auth scope per the Fabric "using-fabric-apis" article.
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"

# Retry / long-running-operation tuning. Fabric uses standard Azure throttling (HTTP 429 +
# Retry-After) and 202-Accepted long-running operations (Operation-Location + Retry-After) — R7 §9.
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5
LRO_POLL_DEFAULT_SECONDS = 5
LRO_TIMEOUT_SECONDS = 600

# Repo-relative default config locations (real config preferred, sample as fallback so the script
# is demonstrable in a fresh clone without a populated deploy_config.json).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)

# A value like "<WORKSPACE_GUID>" in the sample config is an unresolved placeholder, not a value.
_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Fabric/capacities/<name>
_CAPACITY_ARM_ID_RE = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/"
    r"Microsoft\.Fabric/capacities/(?P<name>[^/]+)/?$",
    re.IGNORECASE,
)

LOG = logging.getLogger("zava.fabric.workspace")


class WorkspaceError(RuntimeError):
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
    """Return the config file to read, or None (CLI/env can still supply identity)."""
    if explicit:
        if not os.path.isfile(explicit):
            raise WorkspaceError(f"--config path does not exist: {explicit}")
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
        raise WorkspaceError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceError(f"config {path} must be a JSON object")
    return data


class WorkspacePlan:
    """Resolved, validated plan for the workspace operation (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_name: Optional[str],
        use_existing: bool,
        existing_workspace_id: Optional[str],
        description: str,
        capacity_name: Optional[str],
        capacity_id: Optional[str],
        enable_identity: bool,
        admin_object_id: Optional[str],
    ) -> None:
        self.config_path = config_path
        self.workspace_name = workspace_name
        self.use_existing = use_existing
        self.existing_workspace_id = existing_workspace_id
        self.description = description
        self.capacity_name = capacity_name
        self.capacity_id = capacity_id
        self.enable_identity = enable_identity
        self.admin_object_id = admin_object_id


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_name: Optional[str] = None,
    existing_workspace_id: Optional[str] = None,
    capacity_name: Optional[str] = None,
    capacity_id: Optional[str] = None,
    enable_identity: Optional[bool] = None,
    admin_object_id: Optional[str] = None,
) -> WorkspacePlan:
    """Resolve the workspace operation plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable.
    Raises ``WorkspaceError`` naming any field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    cap_cfg = cfg.get("capacity", {}) if isinstance(cfg.get("capacity"), dict) else {}

    use_existing = bool(ws_cfg.get("use_existing"))

    name = workspace_name or _clean(ws_cfg.get("name")) or os.environ.get("FABRIC_WORKSPACE_NAME")
    existing_id = (
        existing_workspace_id
        or _clean(ws_cfg.get("existing_workspace_id"))
        or os.environ.get("FABRIC_WORKSPACE_ID")
    )

    description = (
        _clean(ws_cfg.get("description"))
        or "Zava car-rental demo workspace (created by fabric/scripts/00_create_workspace.py)"
    )

    # Capacity to bind. A direct Fabric capacity GUID (CLI/env) short-circuits the name lookup.
    # Otherwise resolve by name from workspace.capacity_ref > capacity.name > capacity.existing_capacity_id.
    cap_id = capacity_id or _clean(os.environ.get("FABRIC_CAPACITY_ID"))
    cap_name = (
        capacity_name
        or _clean(ws_cfg.get("capacity_ref"))
        or _clean(cap_cfg.get("name"))
        or os.environ.get("FABRIC_CAPACITY_NAME")
    )
    if not cap_name:
        existing_cap_arm = _clean(cap_cfg.get("existing_capacity_id"))
        if existing_cap_arm:
            match = _CAPACITY_ARM_ID_RE.match(existing_cap_arm)
            if match and not _is_placeholder(match.group("name")):
                cap_name = match.group("name")

    if enable_identity is None:
        # Default ON (fresh path provisions the Workspace Identity); config can turn it off.
        cfg_flag = ws_cfg.get("enable_workspace_identity")
        enable_identity = bool(cfg_flag) if isinstance(cfg_flag, bool) else True

    admin = (
        admin_object_id
        or _clean(ws_cfg.get("admin_object_id"))
        or os.environ.get("FABRIC_WORKSPACE_ADMIN_OBJECT_ID")
    )

    # Validation: the existing path needs a real GUID; the fresh path needs a name + a capacity ref.
    if use_existing:
        if not existing_id:
            raise WorkspaceError(
                "workspace.use_existing=true but no workspace id resolved "
                "(set workspace.existing_workspace_id, --workspace-id, or FABRIC_WORKSPACE_ID)"
            )
        if not _GUID_RE.match(existing_id):
            raise WorkspaceError(
                f"resolved existing workspace id is not a GUID: {existing_id!r}"
            )
    else:
        if not name:
            raise WorkspaceError(
                "could not resolve workspace name "
                "(set workspace.name, --workspace-name, or FABRIC_WORKSPACE_NAME)"
            )
        if not cap_id and not cap_name:
            raise WorkspaceError(
                "could not resolve a capacity to assign (set workspace.capacity_ref / "
                "capacity.name in deploy_config.json, --capacity, or FABRIC_CAPACITY_ID)"
            )

    if cap_id and not _GUID_RE.match(cap_id):
        raise WorkspaceError(f"--capacity-id / FABRIC_CAPACITY_ID must be a GUID: {cap_id!r}")
    if admin and not _GUID_RE.match(admin):
        raise WorkspaceError(f"admin object id must be a GUID: {admin!r}")

    return WorkspacePlan(
        config_path=resolved_path,
        workspace_name=name,
        use_existing=use_existing,
        existing_workspace_id=existing_id,
        description=description,
        capacity_name=cap_name,
        capacity_id=cap_id,
        enable_identity=enable_identity,
        admin_object_id=admin,
    )


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential, with az CLI fallback) — mirrors scripts/pause_capacity.py
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
        raise WorkspaceError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI (`az`) "
            "is available. Install azure-identity or run `az login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise WorkspaceError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise WorkspaceError("could not parse access token from az CLI output") from exc
    return token


# ---------------------------------------------------------------------------
# Fabric REST helpers (stdlib only — no SDK dependency) with retry/backoff + LRO polling
# ---------------------------------------------------------------------------

def _fabric_request(
    method: str,
    url: str,
    token: str,
    body: Optional[dict] = None,
) -> Tuple[int, Dict[str, str], dict]:
    """Issue a single Fabric REST call, retrying on throttling (429) and transient 5xx.

    Returns (status_code, response_headers, json_payload). Raises ``WorkspaceError`` on a
    non-retryable HTTP error or once retries are exhausted.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    attempt = 0
    while True:
        attempt += 1
        request = urllib.request.Request(url=url, method=method, data=data)
        request.add_header("Authorization", f"Bearer {token}")
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
            # 429 (throttled) and transient 5xx are retried with backoff; everything else is fatal.
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt <= MAX_RETRIES:
                    delay = _retry_delay(retry_after, attempt)
                    LOG.warning(
                        "Fabric %s %s -> HTTP %s; retrying in %ss (attempt %s/%s)",
                        method, url, exc.code, delay, attempt, MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
            raise WorkspaceError(
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
            raise WorkspaceError(f"Fabric {method} {url} failed: {exc.reason}") from exc


def _retry_delay(retry_after: Optional[str], attempt: int) -> int:
    """Honor a server Retry-After when present, else exponential backoff."""
    if retry_after:
        try:
            return max(1, int(float(retry_after)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _poll_long_running(
    headers: Dict[str, str], token: str, *, timeout: int = LRO_TIMEOUT_SECONDS
) -> dict:
    """Poll a Fabric long-running operation (202 + Operation-Location) until terminal state.

    Returns the final operation payload (or result, when a Location result url is provided).
    Raises ``WorkspaceError`` on Failed status or timeout.
    """
    op_url = headers.get("operation-location") or headers.get("location")
    if not op_url:
        # No async handle: the operation completed synchronously.
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
        status, op_headers, payload = _fabric_request("GET", op_url, token)
        state = str(payload.get("status", "")).lower()
        LOG.debug("LRO %s -> status=%s (http %s)", op_url, state or "<none>", status)
        if state in ("succeeded", "completed"):
            # Fetch the result body if the service points to one.
            result_url = op_headers.get("location")
            if result_url and result_url != op_url:
                _, _, result = _fabric_request("GET", result_url, token)
                return result
            return payload
        if state in ("failed", "canceled", "cancelled"):
            raise WorkspaceError(
                f"Fabric long-running operation {op_url} ended in state {state!r}: {payload}"
            )
        if time.monotonic() >= deadline:
            raise WorkspaceError(
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
            url = f"{base}{sep}continuationToken={urllib.request.quote(cont_token)}"
        else:
            url = ""
    return items


# ---------------------------------------------------------------------------
# Workspace operations (find-or-create, capacity assignment, identity)
# ---------------------------------------------------------------------------

def find_workspace_by_name(token: str, name: str) -> Optional[dict]:
    """Return the workspace dict whose displayName matches ``name`` (case-insensitive), or None."""
    target = name.strip().lower()
    for ws in _list_paginated("/workspaces", token):
        if str(ws.get("displayName", "")).strip().lower() == target:
            return ws
    return None


def get_workspace(token: str, workspace_id: str) -> dict:
    _, _, payload = _fabric_request("GET", f"{FABRIC_API_BASE}/workspaces/{workspace_id}", token)
    return payload


def create_workspace(token: str, name: str, description: str) -> dict:
    LOG.info("Creating Fabric workspace %r ...", name)
    _, _, payload = _fabric_request(
        "POST",
        f"{FABRIC_API_BASE}/workspaces",
        token,
        body={"displayName": name, "description": description},
    )
    return payload


def resolve_capacity_id(token: str, capacity_name: str) -> str:
    """Resolve a Fabric capacity's object id (GUID) by display name via GET /v1/capacities.

    The assignToCapacity body requires the capacity's Fabric **object id**, not its ARM resource id.
    """
    target = capacity_name.strip().lower()
    matches = [
        c for c in _list_paginated("/capacities", token)
        if str(c.get("displayName", "")).strip().lower() == target
    ]
    if not matches:
        raise WorkspaceError(
            f"no Fabric capacity named {capacity_name!r} is visible to this principal "
            f"(check the capacity exists (Step 5) and the principal has capacity admin/contributor access)"
        )
    if len(matches) > 1:
        LOG.warning("multiple capacities named %r found; using the first.", capacity_name)
    cap_id = matches[0].get("id")
    if not cap_id:
        raise WorkspaceError(f"capacity {capacity_name!r} has no 'id' field in the API response")
    return str(cap_id)


def assign_capacity(token: str, workspace_id: str, capacity_id: str) -> None:
    LOG.info("Assigning workspace %s to capacity %s ...", workspace_id, capacity_id)
    status, headers, _ = _fabric_request(
        "POST",
        f"{FABRIC_API_BASE}/workspaces/{workspace_id}/assignToCapacity",
        token,
        body={"capacityId": capacity_id},
    )
    if status == 202:
        _poll_long_running(headers, token)
    LOG.info("Capacity assignment complete.")


def provision_identity(token: str, workspace_id: str) -> Optional[str]:
    """Provision the Workspace Identity (long-running) and return its object id, or None.

    Tries the REST ``provisionIdentity`` endpoint first. Per R7/R10 this can require one-time
    tenant/admin consent and is **not always SP-supported**; on a clear unsupported/forbidden
    response we log a UI-fallback instruction (also captured in docs/manual-steps.md) and return
    None rather than aborting the whole deployment.
    """
    # Idempotency: if the workspace already has an identity, just read & return its object id.
    ws = get_workspace(token, workspace_id)
    existing = _identity_object_id(ws)
    if existing:
        LOG.info("Workspace already has a Workspace Identity (object id %s) — no-op.", existing)
        return existing

    LOG.info("Provisioning Workspace Identity for workspace %s (long-running) ...", workspace_id)
    try:
        status, headers, _ = _fabric_request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/provisionIdentity",
            token,
        )
        if status == 202:
            _poll_long_running(headers, token)
    except WorkspaceError as exc:
        # 403 / 400 "not supported" => fall back to the documented UI step (R7/R10).
        msg = str(exc)
        if " HTTP 403" in msg or " HTTP 400" in msg or "not supported" in msg.lower():
            LOG.warning(
                "Workspace Identity could not be provisioned via REST (%s). "
                "Create it in the UI: Workspace settings -> Workspace identity -> + Workspace identity, "
                "then record its object id in workspace.identity_object_id. "
                "See docs/manual-steps.md (Step 10).",
                msg,
            )
            return None
        raise

    ws = get_workspace(token, workspace_id)
    obj_id = _identity_object_id(ws)
    if obj_id:
        LOG.info("Workspace Identity provisioned (object id %s).", obj_id)
    else:
        LOG.warning(
            "Workspace Identity provisioned but no object id was returned by GET workspace; "
            "read it from Workspace settings -> Workspace identity and set workspace.identity_object_id."
        )
    return obj_id


def _identity_object_id(workspace: dict) -> Optional[str]:
    """Extract the Workspace Identity's object id (servicePrincipalId) from a GET workspace body."""
    identity = workspace.get("workspaceIdentity")
    if not isinstance(identity, dict):
        return None
    # Prefer the service principal (object) id used for resource-instance / trusted-workspace rules.
    return _clean(identity.get("servicePrincipalId")) or _clean(identity.get("applicationId"))


def add_admin_role(token: str, workspace_id: str, object_id: str) -> None:
    """Add a service principal as workspace Admin (optional; see Step 2 manual-steps row)."""
    LOG.info("Adding admin role assignment for principal %s ...", object_id)
    try:
        _fabric_request(
            "POST",
            f"{FABRIC_API_BASE}/workspaces/{workspace_id}/roleAssignments",
            token,
            body={"principal": {"id": object_id, "type": "ServicePrincipal"}, "role": "Admin"},
        )
        LOG.info("Admin role assignment complete.")
    except WorkspaceError as exc:
        # A duplicate role assignment is a benign idempotent outcome.
        if " HTTP 409" in str(exc):
            LOG.info("Principal %s already has a role assignment — no-op.", object_id)
            return
        raise


# ---------------------------------------------------------------------------
# Optional config write-back (never touches committed *.sample.json)
# ---------------------------------------------------------------------------

def write_identity_to_config(config_path: Optional[str], object_id: str) -> None:
    if not config_path:
        LOG.warning("--write-config requested but no config file is resolved; skipping write-back.")
        return
    if os.path.basename(config_path).endswith(".sample.json"):
        LOG.warning(
            "refusing to write the captured identity object id into the committed sample config %s; "
            "copy it to deploy_config.json (gitignored) and re-run with --write-config.",
            config_path,
        )
        return
    cfg = _load_config(config_path)
    cfg.setdefault("workspace", {})
    if not isinstance(cfg["workspace"], dict):
        raise WorkspaceError(f"{config_path}: 'workspace' is not an object")
    cfg["workspace"]["identity_object_id"] = object_id
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    LOG.info("Wrote workspace.identity_object_id=%s into %s", object_id, config_path)


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------

def run(plan: WorkspacePlan, *, dry_run: bool = False, write_config: bool = False) -> int:
    """Execute the find-or-create + assign-capacity + provision-identity flow. Returns exit code."""
    if dry_run:
        return _print_dry_run(plan)

    token = get_access_token()

    # 1. Find-or-create the workspace (idempotent).
    if plan.use_existing:
        LOG.info("Existing-workspace path: attaching to %s", plan.existing_workspace_id)
        workspace = get_workspace(token, plan.existing_workspace_id)  # type: ignore[arg-type]
        workspace_id = str(workspace.get("id") or plan.existing_workspace_id)
        LOG.info("Attached to workspace %s (%s).", workspace.get("displayName"), workspace_id)
    else:
        existing = find_workspace_by_name(token, plan.workspace_name)  # type: ignore[arg-type]
        if existing:
            workspace_id = str(existing["id"])
            LOG.info(
                "Workspace %r already exists (%s) — reusing (idempotent).",
                plan.workspace_name, workspace_id,
            )
            workspace = existing
        else:
            workspace = create_workspace(token, plan.workspace_name, plan.description)  # type: ignore[arg-type]
            workspace_id = str(workspace["id"])
            LOG.info("Created workspace %r (%s).", plan.workspace_name, workspace_id)

    # 2. Assign capacity (idempotent — skip when already bound to the target capacity).
    capacity_id = plan.capacity_id or resolve_capacity_id(token, plan.capacity_name)  # type: ignore[arg-type]
    current_cap = _clean((workspace or {}).get("capacityId"))
    if current_cap and current_cap.lower() == capacity_id.lower():
        LOG.info("Workspace already bound to capacity %s — no-op.", capacity_id)
    else:
        assign_capacity(token, workspace_id, capacity_id)

    # 3. Provision the Workspace Identity (long-running; UI fallback per R7/R10).
    identity_object_id: Optional[str] = None
    if plan.enable_identity:
        identity_object_id = provision_identity(token, workspace_id)
        if identity_object_id:
            LOG.info(
                "Workspace Identity object id: %s "
                "(set this into workspace.identity_object_id for Step 12 ADLS hardening).",
                identity_object_id,
            )
            if write_config:
                write_identity_to_config(plan.config_path, identity_object_id)
    else:
        LOG.info("Workspace Identity provisioning disabled (workspace.enable_workspace_identity=false).")

    # 4. Optional admin role assignment for the deploy SP.
    if plan.admin_object_id:
        add_admin_role(token, workspace_id, plan.admin_object_id)

    LOG.info(
        "Done. workspace_id=%s capacity_id=%s identity_object_id=%s",
        workspace_id, capacity_id, identity_object_id or "<not captured>",
    )
    return 0


def _print_dry_run(plan: WorkspacePlan) -> int:
    """Credential-free preview of every intended REST call. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    if plan.use_existing:
        LOG.info(
            "[DRY-RUN] Would GET %s/workspaces/%s (attach to existing).",
            FABRIC_API_BASE, plan.existing_workspace_id,
        )
    else:
        LOG.info(
            "[DRY-RUN] Would GET %s/workspaces then, if %r absent, POST %s/workspaces "
            "{displayName=%r}.",
            FABRIC_API_BASE, plan.workspace_name, FABRIC_API_BASE, plan.workspace_name,
        )
    if plan.capacity_id:
        LOG.info(
            "[DRY-RUN] Would POST %s/workspaces/{id}/assignToCapacity {capacityId=%s}.",
            FABRIC_API_BASE, plan.capacity_id,
        )
    else:
        LOG.info(
            "[DRY-RUN] Would GET %s/capacities to resolve %r -> GUID, then POST "
            "%s/workspaces/{id}/assignToCapacity {capacityId=<guid>}.",
            FABRIC_API_BASE, plan.capacity_name, FABRIC_API_BASE,
        )
    if plan.enable_identity:
        LOG.info(
            "[DRY-RUN] Would POST %s/workspaces/{id}/provisionIdentity (long-running) and read "
            "workspaceIdentity.servicePrincipalId; UI fallback if SP-unsupported (R7/R10).",
            FABRIC_API_BASE,
        )
    if plan.admin_object_id:
        LOG.info(
            "[DRY-RUN] Would POST %s/workspaces/{id}/roleAssignments "
            "{principal.id=%s, role=Admin}.",
            FABRIC_API_BASE, plan.admin_object_id,
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/attach the Zava Fabric workspace, assign F64 capacity, and provision "
                    "the Workspace Identity (Fabric Core Workspaces REST API).",
    )
    parser.add_argument(
        "--config",
        help="Path to deploy_config.json (defaults to fabric/config/deploy_config.json, then "
             "the committed .sample.json).",
    )
    parser.add_argument("--workspace-name", help="Workspace display name (overrides config/env).")
    parser.add_argument(
        "--workspace-id", dest="existing_workspace_id",
        help="Existing workspace GUID to attach to (overrides config/env).",
    )
    parser.add_argument(
        "--capacity", dest="capacity_name",
        help="Fabric capacity display name to assign (overrides config capacity.name).",
    )
    parser.add_argument(
        "--capacity-id",
        help="Fabric capacity object id (GUID); skips the name->GUID lookup.",
    )
    parser.add_argument(
        "--admin-object-id",
        help="Object id (GUID) of a service principal to add as workspace Admin (optional).",
    )
    identity_group = parser.add_mutually_exclusive_group()
    identity_group.add_argument(
        "--provision-identity", dest="enable_identity", action="store_true", default=None,
        help="Force Workspace Identity provisioning (default unless disabled in config).",
    )
    identity_group.add_argument(
        "--skip-identity", dest="enable_identity", action="store_false",
        help="Skip Workspace Identity provisioning.",
    )
    parser.add_argument(
        "--write-config", action="store_true",
        help="Persist the captured identity object id into the resolved (non-sample) config.",
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
            capacity_name=args.capacity_name,
            capacity_id=args.capacity_id,
            enable_identity=args.enable_identity,
            admin_object_id=args.admin_object_id,
        )
        return run(plan, dry_run=args.dry_run, write_config=args.write_config)
    except WorkspaceError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
