#!/usr/bin/env python3
"""Pause (suspend) the Zava demo Microsoft Fabric capacity to stop compute (CU) billing.

Why
---
An F64 Fabric capacity bills pay-as-you-go at ~$11.52/hour whenever it is **Active**
(see ``docs/cost.md`` / research R9). The single biggest cost control for this demo is to
**pause the capacity the moment it is idle**. This script issues the
``Microsoft.Fabric/capacities`` **suspend** action so that all CU consumption stops.
(OneLake *storage* keeps billing while paused — that is expected and negligible for demo data.)

What it does
------------
1. Resolves the target capacity identity (subscription / resource group / name) from
   ``deploy_config.json`` (``capacity.name`` and, on the existing-capacity path,
   ``capacity.existing_capacity_id``) — consistent with the Step 1 config schema — with optional
   CLI / environment overrides.
2. Authenticates to Azure Resource Manager via ``DefaultAzureCredential`` (``az login`` / managed
   identity / env service principal). Falls back to the ``az`` CLI for a token if the
   ``azure-identity`` package is not installed.
3. Reads the current capacity state (GET) and **idempotently** decides whether the action is
   needed (re-running pause on an already-paused capacity is a safe no-op).
4. Issues the ``suspend`` action (POST) — unless ``--dry-run`` is set, which prints the intended
   action and current state and makes **no** changes.

This module is also the **shared core** imported by ``scripts/resume_capacity.py``; the
``resume`` action reuses the same identity resolution, auth, idempotency, and logging.

Security / Phase-0 notes
------------------------
* **No secrets.** Credentials are never stored: identity comes from config (names/ids only) and
  auth is acquired at runtime via ``DefaultAzureCredential`` / ``az login``.
* **Authoring phase:** do **not** run the live ``suspend``/``resume`` action against a real
  capacity unless you intend to change its billing state. Use ``--dry-run`` to preview safely.

Usage
-----
    # Preview only — no mutation (safe):
    python scripts/pause_capacity.py --dry-run

    # Pause using identity from deploy_config.json + env (AZURE_SUBSCRIPTION_ID / AZURE_RESOURCE_GROUP):
    python scripts/pause_capacity.py

    # Fully explicit:
    python scripts/pause_capacity.py \
        --subscription <SUBSCRIPTION_ID> \
        --resource-group <RESOURCE_GROUP> \
        --capacity zava-fabric-cap
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Microsoft.Fabric/capacities action API version (R9 §5 / Azure REST reference).
DEFAULT_API_VERSION = "2023-11-01"
ARM_ENDPOINT = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
PROVIDER = "Microsoft.Fabric/capacities"

# Repo-relative default config locations (real config preferred, sample as fallback so the
# scripts are demonstrable in a fresh clone without a populated deploy_config.json).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)

# A value like "<SUBSCRIPTION_ID>" in the sample config is an unresolved placeholder, not a value.
_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
# /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Fabric/capacities/<name>
_CAPACITY_ID_RE = re.compile(
    r"^/subscriptions/(?P<sub>[^/]+)/resourceGroups/(?P<rg>[^/]+)/providers/"
    r"Microsoft\.Fabric/capacities/(?P<name>[^/]+)/?$",
    re.IGNORECASE,
)

# Capacity provisioning/state values. The REST API reports state via properties.state.
_PAUSED_STATES = {"paused", "pausing"}
_ACTIVE_STATES = {"active", "resuming"}

LOG = logging.getLogger("zava.capacity")


class CapacityError(RuntimeError):
    """Raised on unrecoverable problems (bad identity, auth failure, API error)."""


# ---------------------------------------------------------------------------
# Config / identity resolution
# ---------------------------------------------------------------------------

def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.match(value.strip()))


def _resolve_config_path(explicit: Optional[str]) -> Optional[str]:
    """Return the config file to read, or None if none exists (CLI/env can still supply identity)."""
    if explicit:
        if not os.path.isfile(explicit):
            raise CapacityError(f"--config path does not exist: {explicit}")
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
        raise CapacityError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CapacityError(f"config {path} must be a JSON object")
    return data


def resolve_capacity_identity(
    config_path: Optional[str] = None,
    subscription: Optional[str] = None,
    resource_group: Optional[str] = None,
    capacity_name: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Resolve (subscription_id, resource_group, capacity_name) for the Fabric capacity.

    Precedence for each field: explicit CLI arg > ``capacity.existing_capacity_id`` (ARM id, when
    present and not a placeholder) > environment variable > ``capacity.name`` (name only).

    Raises ``CapacityError`` naming any field that could not be resolved.
    """
    cfg = _load_config(_resolve_config_path(config_path))
    capacity_cfg = cfg.get("capacity", {}) if isinstance(cfg.get("capacity"), dict) else {}

    # Parse the existing-capacity ARM id when it is a real (non-placeholder) value. Only consult
    # it on the existing-capacity path (use_existing=true), per the Step 1 schema where
    # existing_capacity_id is only meaningful then. Any captured group that is itself an unresolved
    # placeholder (e.g. the sample's "<CAPACITY_NAME>") is discarded so we fall back to capacity.name.
    id_sub = id_rg = id_name = None
    use_existing = bool(capacity_cfg.get("use_existing"))
    existing_id = capacity_cfg.get("existing_capacity_id")
    if use_existing and isinstance(existing_id, str) and not _is_placeholder(existing_id):
        match = _CAPACITY_ID_RE.match(existing_id.strip())
        if match:
            id_sub = None if _is_placeholder(match.group("sub")) else match.group("sub")
            id_rg = None if _is_placeholder(match.group("rg")) else match.group("rg")
            id_name = None if _is_placeholder(match.group("name")) else match.group("name")
        else:
            LOG.warning(
                "capacity.existing_capacity_id is set but is not a Microsoft.Fabric/capacities "
                "ARM id; ignoring it for identity resolution: %s",
                existing_id,
            )

    cfg_name = capacity_cfg.get("name")
    cfg_name = cfg_name if isinstance(cfg_name, str) and not _is_placeholder(cfg_name) else None

    sub = subscription or id_sub or os.environ.get("AZURE_SUBSCRIPTION_ID")
    rg = resource_group or id_rg or os.environ.get("AZURE_RESOURCE_GROUP")
    name = capacity_name or id_name or cfg_name

    missing = []
    if not sub or _is_placeholder(sub):
        missing.append("subscription (use --subscription, AZURE_SUBSCRIPTION_ID, or a real "
                       "capacity.existing_capacity_id)")
    if not rg or _is_placeholder(rg):
        missing.append("resource group (use --resource-group, AZURE_RESOURCE_GROUP, or a real "
                       "capacity.existing_capacity_id)")
    if not name or _is_placeholder(name):
        missing.append("capacity name (use --capacity or set capacity.name in deploy_config.json)")
    if missing:
        raise CapacityError("could not resolve capacity identity: " + "; ".join(missing))

    return str(sub), str(rg), str(name)


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential, with az CLI fallback)
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Acquire an ARM bearer token via DefaultAzureCredential; fall back to the ``az`` CLI.

    No secret is ever read from or written to disk: this relies on an existing ``az login``,
    a managed identity, or environment service-principal credentials.
    """
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore import-not-found
    except ImportError:
        LOG.debug("azure-identity not installed; falling back to `az account get-access-token`")
        return _get_token_via_az_cli()

    try:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
        return credential.get_token(ARM_SCOPE).token
    except Exception as exc:  # noqa: BLE001 - surface any auth failure uniformly
        LOG.debug("DefaultAzureCredential failed (%s); falling back to az CLI", exc)
        return _get_token_via_az_cli()


def _get_token_via_az_cli() -> str:
    try:
        proc = subprocess.run(
            ["az", "account", "get-access-token", "--resource", ARM_ENDPOINT, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise CapacityError(
            "could not authenticate: neither the azure-identity package nor the Azure CLI (`az`) "
            "is available. Install azure-identity or run `az login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise CapacityError(
            f"`az account get-access-token` failed (run `az login` first): {exc.stderr.strip()}"
        ) from exc
    try:
        token = json.loads(proc.stdout)["accessToken"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise CapacityError("could not parse access token from az CLI output") from exc
    return token


# ---------------------------------------------------------------------------
# Azure Resource Manager REST helpers (stdlib only — no SDK dependency)
# ---------------------------------------------------------------------------

def _capacity_base_url(subscription: str, resource_group: str, name: str) -> str:
    return (
        f"{ARM_ENDPOINT}/subscriptions/{subscription}/resourceGroups/{resource_group}"
        f"/providers/{PROVIDER}/{name}"
    )


def _arm_request(method: str, url: str, token: str) -> Tuple[int, dict]:
    request = urllib.request.Request(url=url, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    if method == "POST":
        request.data = b""  # action endpoints take an empty body
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 - fixed ARM host
            status = response.status
            raw = response.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise CapacityError(
            f"ARM {method} {url} failed: HTTP {exc.code} {exc.reason} {body}".strip()
        ) from exc
    except urllib.error.URLError as exc:
        raise CapacityError(f"ARM {method} {url} failed: {exc.reason}") from exc
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    return status, payload


def get_capacity_state(
    token: str, subscription: str, resource_group: str, name: str, api_version: str
) -> str:
    """Return the current capacity state (lowercased), e.g. 'active', 'paused'."""
    url = f"{_capacity_base_url(subscription, resource_group, name)}?api-version={api_version}"
    _, payload = _arm_request("GET", url, token)
    state = (payload.get("properties", {}) or {}).get("state")
    return str(state).lower() if state else "unknown"


# ---------------------------------------------------------------------------
# Core action orchestration (shared by pause and resume)
# ---------------------------------------------------------------------------

def run_capacity_action(
    action: str,
    *,
    config_path: Optional[str] = None,
    subscription: Optional[str] = None,
    resource_group: Optional[str] = None,
    capacity_name: Optional[str] = None,
    api_version: str = DEFAULT_API_VERSION,
    dry_run: bool = False,
) -> int:
    """Idempotently issue 'suspend' or 'resume' on the target Fabric capacity.

    Returns a process exit code (0 = success / no-op needed).
    """
    if action not in ("suspend", "resume"):
        raise CapacityError(f"unsupported action: {action!r} (expected 'suspend' or 'resume')")

    sub, rg, name = resolve_capacity_identity(
        config_path=config_path,
        subscription=subscription,
        resource_group=resource_group,
        capacity_name=capacity_name,
    )
    target_word = "PAUSE (suspend)" if action == "suspend" else "RESUME"
    already_states = _PAUSED_STATES if action == "suspend" else _ACTIVE_STATES
    LOG.info("Target capacity: %s/%s/%s (%s)", sub, rg, name, PROVIDER)
    LOG.info("Requested action: %s", target_word)

    # In dry-run we short-circuit *before* authenticating or calling Azure: this is a safe,
    # credential-free preview of exactly what would happen. No GET, no POST, no mutation.
    if dry_run:
        action_url = f"{_capacity_base_url(sub, rg, name)}/{action}?api-version={api_version}"
        LOG.info(
            "[DRY-RUN] Would issue %s -> POST %s . No authentication and no changes made.",
            target_word, action_url,
        )
        return 0

    token = get_access_token()

    state = get_capacity_state(token, sub, rg, name, api_version)
    LOG.info("Current capacity state: %s", state)

    # Idempotency: skip the action if the capacity is already in (or moving to) the target state.
    if state in already_states:
        LOG.info("Capacity is already '%s' — nothing to do (idempotent no-op).", state)
        return 0

    action_url = (
        f"{_capacity_base_url(sub, rg, name)}/{action}?api-version={api_version}"
    )
    LOG.info("Issuing %s action ...", action)
    status, _ = _arm_request("POST", action_url, token)
    # 200 OK or 202 Accepted both indicate the action was accepted (async long-running op).
    if status in (200, 202):
        LOG.info("%s accepted (HTTP %s) for capacity '%s'.", target_word, status, name)
        return 0
    raise CapacityError(f"{target_word} returned unexpected HTTP {status} for capacity '{name}'")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser(action: str) -> argparse.ArgumentParser:
    verb = "Pause (suspend)" if action == "suspend" else "Resume"
    parser = argparse.ArgumentParser(
        description=f"{verb} the Zava demo Microsoft Fabric capacity ({PROVIDER} {action}).",
    )
    parser.add_argument(
        "--config",
        help="Path to deploy_config.json (defaults to fabric/config/deploy_config.json, "
             "then the committed .sample.json).",
    )
    parser.add_argument("--subscription", help="Azure subscription id (overrides config/env).")
    parser.add_argument(
        "--resource-group", help="Resource group of the capacity (overrides config/env)."
    )
    parser.add_argument(
        "--capacity",
        help="Capacity name (overrides config capacity.name / existing_capacity_id).",
    )
    parser.add_argument(
        "--api-version", default=DEFAULT_API_VERSION,
        help=f"Microsoft.Fabric/capacities API version (default {DEFAULT_API_VERSION}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the intended action and current state without mutating the capacity.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging.",
    )
    return parser


def main(action: str, argv: Optional[list] = None) -> int:
    args = build_arg_parser(action).parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return run_capacity_action(
            action,
            config_path=args.config,
            subscription=args.subscription,
            resource_group=args.resource_group,
            capacity_name=args.capacity,
            api_version=args.api_version,
            dry_run=args.dry_run,
        )
    except CapacityError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main("suspend"))
