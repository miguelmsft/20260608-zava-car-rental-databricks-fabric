#!/usr/bin/env python3
"""Deploy the Zava **PBIP/PBIR** report — *as code* — idempotently (plan Step 15; research R2).

What it does
------------
1. Resolves all identifiers from ``deploy_config.json`` (Step 1 schema): the target workspace
   (``workspace.use_existing`` / ``workspace.existing_workspace_id`` / ``workspace.name``,
   created by Step 10), the report display name (``report.name`` -> default
   ``Zava Fleet Dashboard``), and the **Step-14 Direct Lake semantic model** it binds to
   (``semantic_model.name`` -> default ``Zava Fleet Analytics``, or an explicit
   ``semantic_model.id`` GUID). CLI / environment overrides take precedence.
   **No secrets are read from config.**
2. Authenticates via ``DefaultAzureCredential`` (``az login`` / managed identity / env service
   principal) when run outside a Fabric notebook; inside a Fabric notebook the ambient identity is
   used. A Fabric control-plane bearer token is acquired for the Fabric REST API.
3. **Assembles the PBIR definition parts** from ``fabric/report/Zava Fleet Dashboard.Report``
   (``definition.pbir`` + ``definition/**`` + ``StaticResources/**``), base64-encoding each part
   (``InlineBase64``). The ``__ZAVA_SEMANTIC_MODEL_ID__`` token in ``definition.pbir`` is replaced
   with the real semantic-model GUID, and the registered blue-Zava theme resource is injected from
   the **canonical** ``fabric/theme/zava-blue-theme.json`` so the committed theme is the single
   source of truth.
4. **Find-or-create the report** (idempotent): if a report of the same name already exists in the
   workspace it is updated in place via ``POST .../reports/{id}/updateDefinition``; otherwise it is
   created via ``POST .../reports``. Both are long-running operations (``202 Accepted``) and are
   polled to completion.
5. **Rebinds** the report to the Step-14 semantic model using the **verified** signature
   ``sempy_labs.report.report_rebind(report=..., dataset=..., report_workspace=...,
   dataset_workspace=...)`` — NOT the non-existent ``rebind_report`` — so the Direct Lake binding is
   guaranteed even when the import created a fresh connection.

The report definition under ``fabric/report/`` is the source of truth; the advanced visuals (map,
decomposition tree, forecasting) are authored in Power BI Desktop and committed as PBIR (R2 §5.5).

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names / ids / placeholders only); auth is acquired at
  runtime via ``DefaultAzureCredential`` / ``az login`` / the Fabric notebook identity.
* **Authoring phase:** do **not** run this against a live tenant unless you intend to create Fabric
  items. Use ``--dry-run`` to preview every intended REST call and the assembled part list with
  **no** authentication and **no** changes.

Usage
-----
    # Preview only — no auth, no mutation (safe):
    python fabric/scripts/50_deploy_report.py --dry-run

    # Create / update the report from deploy_config.json:
    python fabric/scripts/50_deploy_report.py

    # Explicit names + a known semantic-model GUID, skip the post-deploy rebind:
    python fabric/scripts/50_deploy_report.py \
        --workspace-name zava-fabric-ws \
        --report-name "Zava Fleet Dashboard" \
        --semantic-model-id 11111111-2222-3333-4444-555555555555 \
        --no-rebind
"""

from __future__ import annotations

import argparse
import base64
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

DEFAULT_REPORT_NAME = "Zava Fleet Dashboard"
DEFAULT_SEMANTIC_MODEL_NAME = "Zava Fleet Analytics"

# Token in definition.pbir replaced with the real semantic-model GUID at deploy time (never a secret).
SEMANTIC_MODEL_ID_TOKEN = "__ZAVA_SEMANTIC_MODEL_ID__"

# Fabric control-plane.
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"

# Retry / backoff tuning for transient REST failures.
MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 5
# LRO polling.
LRO_MAX_POLLS = 60
LRO_DEFAULT_RETRY_AFTER = 5

# Repo-relative defaults.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)
REPORT_DIR = os.path.join(_REPO_ROOT, "fabric", "report", "Zava Fleet Dashboard.Report")
THEME_FILE = os.path.join(_REPO_ROOT, "fabric", "theme", "zava-blue-theme.json")
# Registered-resource path (item-relative, forward slashes) that gets the canonical theme injected.
THEME_RESOURCE_REL = "StaticResources/RegisteredResources/zava-blue-theme.json"
# Item-root metadata file that is NOT part of the REST definition payload.
EXCLUDED_PARTS = {".platform"}

_PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

LOG = logging.getLogger("zava.fabric.report")


class ReportDeployError(RuntimeError):
    """Raised on unrecoverable problems (bad identity, auth failure, API error, missing files)."""


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
            raise ReportDeployError(f"--config path does not exist: {explicit}")
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
        raise ReportDeployError(f"failed to read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportDeployError(f"config {path} must be a JSON object")
    return data


class ReportPlan:
    """Resolved, validated plan for the PBIR report deployment (no secrets)."""

    def __init__(
        self,
        *,
        config_path: Optional[str],
        workspace_name: Optional[str],
        use_existing_workspace: bool,
        existing_workspace_id: Optional[str],
        report_name: str,
        semantic_model_name: str,
        semantic_model_id: Optional[str],
        rebind: bool,
        enable: bool,
    ) -> None:
        self.config_path = config_path
        self.workspace_name = workspace_name
        self.use_existing_workspace = use_existing_workspace
        self.existing_workspace_id = existing_workspace_id
        self.report_name = report_name
        self.semantic_model_name = semantic_model_name
        self.semantic_model_id = semantic_model_id
        self.rebind = rebind
        self.enable = enable

    @property
    def workspace_ref(self) -> Optional[str]:
        """Whichever workspace identifier sempy_labs should use (id preferred)."""
        return self.existing_workspace_id if self.use_existing_workspace else self.workspace_name


def resolve_plan(
    *,
    config_path: Optional[str] = None,
    workspace_name: Optional[str] = None,
    existing_workspace_id: Optional[str] = None,
    report_name: Optional[str] = None,
    semantic_model_name: Optional[str] = None,
    semantic_model_id: Optional[str] = None,
    no_rebind: bool = False,
) -> ReportPlan:
    """Resolve the deployment plan from config + CLI/env overrides.

    Precedence per field: explicit CLI arg > config value > environment variable > default.
    Raises ``ReportDeployError`` naming any field that could not be resolved.
    """
    resolved_path = _resolve_config_path(config_path)
    cfg = _load_config(resolved_path)
    ws_cfg = cfg.get("workspace", {}) if isinstance(cfg.get("workspace"), dict) else {}
    rpt_cfg = cfg.get("report", {}) if isinstance(cfg.get("report"), dict) else {}
    sm_cfg = cfg.get("semantic_model", {}) if isinstance(cfg.get("semantic_model"), dict) else {}
    feat_cfg = cfg.get("features", {}) if isinstance(cfg.get("features"), dict) else {}

    # Feature gate (default on so a fresh sample config is demonstrable).
    flag = feat_cfg.get("enable_report")
    enable = bool(flag) if isinstance(flag, bool) else True

    use_existing = bool(ws_cfg.get("use_existing"))
    name = workspace_name or _clean(ws_cfg.get("name")) or os.environ.get("FABRIC_WORKSPACE_NAME")
    existing_id = (
        existing_workspace_id
        or _clean(ws_cfg.get("existing_workspace_id"))
        or os.environ.get("FABRIC_WORKSPACE_ID")
    )

    rpt_name = (
        report_name
        or _clean(rpt_cfg.get("name"))
        or os.environ.get("FABRIC_REPORT_NAME")
        or DEFAULT_REPORT_NAME
    )

    sm_name = (
        semantic_model_name
        or _clean(sm_cfg.get("name"))
        or os.environ.get("FABRIC_SEMANTIC_MODEL_NAME")
        or DEFAULT_SEMANTIC_MODEL_NAME
    )
    sm_id = (
        semantic_model_id
        or _clean(sm_cfg.get("id"))
        or _clean(rpt_cfg.get("semantic_model_id"))
        or os.environ.get("FABRIC_SEMANTIC_MODEL_ID")
    )
    if sm_id and not _GUID_RE.match(sm_id):
        raise ReportDeployError(f"resolved semantic-model id is not a GUID: {sm_id!r}")

    # Validation.
    if use_existing:
        if not existing_id:
            raise ReportDeployError(
                "workspace.use_existing=true but no workspace id resolved "
                "(set workspace.existing_workspace_id, --workspace-id, or FABRIC_WORKSPACE_ID)"
            )
        if not _GUID_RE.match(existing_id):
            raise ReportDeployError(
                f"resolved existing workspace id is not a GUID: {existing_id!r}"
            )
    elif not name:
        raise ReportDeployError(
            "could not resolve workspace name "
            "(set workspace.name, --workspace-name, or FABRIC_WORKSPACE_NAME)"
        )

    if not os.path.isdir(REPORT_DIR):
        raise ReportDeployError(f"report project folder not found: {REPORT_DIR}")

    return ReportPlan(
        config_path=resolved_path,
        workspace_name=name,
        use_existing_workspace=use_existing,
        existing_workspace_id=existing_id,
        report_name=rpt_name,
        semantic_model_name=sm_name,
        semantic_model_id=sm_id,
        rebind=not no_rebind,
        enable=enable,
    )


# ---------------------------------------------------------------------------
# Authentication (DefaultAzureCredential; Fabric-notebook identity when present)
# ---------------------------------------------------------------------------

def _in_fabric_notebook() -> bool:
    """Best-effort detection of the Fabric/Spark notebook runtime."""
    if any(k in os.environ for k in ("MMLSPARK_PLATFORM_INFO", "SPARK_HOME", "AZURE_SERVICE")):
        return True
    try:  # the notebook runtime injects a `spark` global
        return "spark" in __builtins__  # type: ignore[operator]
    except Exception:  # noqa: BLE001
        return False


def get_fabric_token() -> Optional[str]:
    """Acquire a Fabric control-plane bearer token (no secret persisted).

    Returns the token string, or ``None`` inside a Fabric notebook (where ``semantic-link-labs``
    uses the ambient identity and direct REST is typically not needed). Raises on a hard auth
    failure outside a notebook.
    """
    if _in_fabric_notebook():
        LOG.info("Running inside a Fabric notebook — using the ambient workspace identity.")
        return None
    scope = FABRIC_RESOURCE.rstrip("/") + "/.default"
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ReportDeployError(
            "azure-identity is not installed; run `pip install azure-identity` and `az login`."
        ) from exc
    try:
        token = DefaultAzureCredential(
            exclude_interactive_browser_credential=False
        ).get_token(scope)
        LOG.debug("Acquired a Fabric control-plane token via DefaultAzureCredential.")
        return token.token
    except Exception as exc:  # noqa: BLE001 - surface any auth failure uniformly
        raise ReportDeployError(
            f"could not acquire a Fabric token (run `az login` or set service-principal env "
            f"vars): {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Retry helper (transient REST failures)
# ---------------------------------------------------------------------------

def _with_retry(fn: Callable[[], object], *, what: str) -> object:
    """Run ``fn`` with exponential backoff on transient errors."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - REST/sempy raise a variety of error types
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
# PBIR part assembly
# ---------------------------------------------------------------------------

def _b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


def _b64_file(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("utf-8")


def assemble_parts(semantic_model_id: Optional[str]) -> List[Dict[str, str]]:
    """Build the Fabric ``definition.parts`` array from the committed PBIR project.

    * ``definition.pbir`` has its ``__ZAVA_SEMANTIC_MODEL_ID__`` token replaced with the real GUID
      (left intact in dry-run when the GUID is unknown).
    * The registered blue-Zava theme is injected from the canonical ``fabric/theme`` file so the
      committed theme is the single source of truth.
    * ``.platform`` (Git metadata) is excluded — it is not part of the REST definition payload.
    """
    parts: List[Dict[str, str]] = []
    for root, _dirs, files in os.walk(REPORT_DIR):
        for file_name in sorted(files):
            abs_path = os.path.join(root, file_name)
            rel_path = os.path.relpath(abs_path, REPORT_DIR).replace(os.sep, "/")
            if rel_path in EXCLUDED_PARTS:
                continue

            if rel_path == "definition.pbir":
                with open(abs_path, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if semantic_model_id:
                    content = content.replace(SEMANTIC_MODEL_ID_TOKEN, semantic_model_id)
                payload = _b64_text(content)
            elif rel_path == THEME_RESOURCE_REL and os.path.isfile(THEME_FILE):
                # Inject the canonical theme so fabric/theme is the single source of truth.
                payload = _b64_file(THEME_FILE)
            else:
                payload = _b64_file(abs_path)

            parts.append({
                "path": rel_path,
                "payload": payload,
                "payloadType": "InlineBase64",
            })

    if not any(p["path"] == "definition.pbir" for p in parts):
        raise ReportDeployError(
            f"definition.pbir missing under {REPORT_DIR}; cannot deploy an unbound report."
        )
    return parts


# ---------------------------------------------------------------------------
# Fabric REST helpers
# ---------------------------------------------------------------------------

def _session(token: str):
    import requests  # type: ignore import-not-found

    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    return sess


def _poll_lro(sess, resp) -> None:
    """Poll a Fabric long-running operation to completion (best-effort)."""
    location = resp.headers.get("Location")
    retry_after = int(resp.headers.get("Retry-After", LRO_DEFAULT_RETRY_AFTER) or LRO_DEFAULT_RETRY_AFTER)
    if not location:
        return
    for _ in range(LRO_MAX_POLLS):
        time.sleep(retry_after)
        poll = sess.get(location)
        poll.raise_for_status()
        status = poll.json().get("status", "Unknown")
        LOG.info("  LRO status: %s", status)
        if status in ("Succeeded", "Completed"):
            return
        if status == "Failed":
            raise ReportDeployError(f"Fabric LRO failed: {poll.json()}")
    raise ReportDeployError("Fabric LRO did not complete within the poll budget.")


def resolve_workspace_id(sess, plan: ReportPlan) -> str:
    """Return the workspace GUID (from config, or resolved by display name via REST)."""
    if plan.use_existing_workspace and plan.existing_workspace_id:
        return plan.existing_workspace_id
    target = (plan.workspace_name or "").strip().lower()
    url: Optional[str] = f"{FABRIC_API_BASE}/workspaces"
    while url:
        resp = _with_retry(lambda u=url: sess.get(u), what="list workspaces")
        resp.raise_for_status()  # type: ignore[attr-defined]
        body = resp.json()  # type: ignore[attr-defined]
        for ws in body.get("value", []):
            if str(ws.get("displayName", "")).strip().lower() == target:
                return ws["id"]
        token = body.get("continuationToken")
        url = (f"{FABRIC_API_BASE}/workspaces?continuationToken={token}" if token else None)
    raise ReportDeployError(f"workspace {plan.workspace_name!r} not found in the tenant.")


def resolve_semantic_model_id(sess, workspace_id: str, plan: ReportPlan) -> str:
    """Return the Step-14 semantic-model GUID (from config, or resolved by name via REST)."""
    if plan.semantic_model_id:
        return plan.semantic_model_id
    target = plan.semantic_model_name.strip().lower()
    url: Optional[str] = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/semanticModels"
    while url:
        resp = _with_retry(lambda u=url: sess.get(u), what="list semantic models")
        resp.raise_for_status()  # type: ignore[attr-defined]
        body = resp.json()  # type: ignore[attr-defined]
        for item in body.get("value", []):
            if str(item.get("displayName", "")).strip().lower() == target:
                return item["id"]
        token = body.get("continuationToken")
        url = (f"{FABRIC_API_BASE}/workspaces/{workspace_id}/semanticModels"
               f"?continuationToken={token}" if token else None)
    raise ReportDeployError(
        f"semantic model {plan.semantic_model_name!r} not found in workspace {workspace_id} "
        "(run fabric/scripts/30_create_semantic_model.py first, or pass --semantic-model-id)."
    )


def find_report_id(sess, workspace_id: str, report_name: str) -> Optional[str]:
    """Return the existing report GUID by display name, or None (idempotency probe)."""
    target = report_name.strip().lower()
    url: Optional[str] = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports"
    while url:
        resp = _with_retry(lambda u=url: sess.get(u), what="list reports")
        resp.raise_for_status()  # type: ignore[attr-defined]
        body = resp.json()  # type: ignore[attr-defined]
        for item in body.get("value", []):
            if str(item.get("displayName", "")).strip().lower() == target:
                return item["id"]
        token = body.get("continuationToken")
        url = (f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports"
               f"?continuationToken={token}" if token else None)
    return None


def create_or_update_report(
    sess, workspace_id: str, plan: ReportPlan, parts: List[Dict[str, str]]
) -> str:
    """Find-or-create the report (idempotent). Returns the report GUID."""
    existing_id = find_report_id(sess, workspace_id, plan.report_name)
    definition = {"parts": parts}

    if existing_id:
        LOG.info("Report %r exists (id=%s) — updating definition in place (idempotent).",
                 plan.report_name, existing_id)
        url = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports/{existing_id}/updateDefinition"
        resp = _with_retry(
            lambda: sess.post(url, json={"definition": definition}),
            what="updateDefinition report",
        )
        if resp.status_code == 202:  # type: ignore[attr-defined]
            _poll_lro(sess, resp)
        else:
            resp.raise_for_status()  # type: ignore[attr-defined]
        return existing_id

    LOG.info("Report %r not found — creating it.", plan.report_name)
    url = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports"
    payload = {"displayName": plan.report_name, "definition": definition}
    resp = _with_retry(lambda: sess.post(url, json=payload), what="create report")
    if resp.status_code == 202:  # type: ignore[attr-defined]
        _poll_lro(sess, resp)
        new_id = find_report_id(sess, workspace_id, plan.report_name)
        if not new_id:
            raise ReportDeployError("report created (LRO) but could not be resolved afterwards.")
        return new_id
    resp.raise_for_status()  # type: ignore[attr-defined]
    return resp.json().get("id", "")  # type: ignore[attr-defined]


def rebind_report(plan: ReportPlan) -> None:
    """Rebind the report to the Step-14 model using the VERIFIED ``report_rebind`` API (R2 §5.6).

    NOTE: the correct function name is ``report_rebind`` — NOT ``rebind_report``.
    """
    if not plan.rebind:
        LOG.info("--no-rebind set — skipping report_rebind.")
        return
    try:
        import sempy_labs.report as rep  # type: ignore import-not-found
    except ImportError:
        LOG.warning(
            "semantic-link-labs not installed — skipping report_rebind "
            "(definition.pbir already binds by semantic-model id)."
        )
        return

    LOG.info("Rebinding report %r to semantic model %r (verified report_rebind signature).",
             plan.report_name, plan.semantic_model_name)
    _with_retry(
        lambda: rep.report_rebind(
            report=plan.report_name,
            dataset=plan.semantic_model_name,
            report_workspace=plan.workspace_ref,
            dataset_workspace=plan.workspace_ref,
        ),
        what="report_rebind",
    )
    LOG.info("Report %r rebound to %r.", plan.report_name, plan.semantic_model_name)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(plan: ReportPlan, *, dry_run: bool) -> int:
    if not plan.enable:
        LOG.info("features.enable_report=false — nothing to do.")
        return 0
    if dry_run:
        return _print_dry_run(plan)

    token = get_fabric_token()
    if token is None:
        raise ReportDeployError(
            "no Fabric REST token available (Fabric-notebook path); run this script outside a "
            "notebook, or deploy the report via Fabric Git integration / fabric-cicd instead."
        )
    sess = _session(token)
    workspace_id = resolve_workspace_id(sess, plan)
    LOG.info("Target workspace id: %s", workspace_id)
    semantic_model_id = resolve_semantic_model_id(sess, workspace_id, plan)
    LOG.info("Binding report to semantic model id: %s", semantic_model_id)

    parts = assemble_parts(semantic_model_id)
    LOG.info("Assembled %d PBIR definition parts.", len(parts))
    report_id = create_or_update_report(sess, workspace_id, plan, parts)
    LOG.info("Report %r deployed (id=%s).", plan.report_name, report_id)

    rebind_report(plan)
    LOG.info("Report %r ready in workspace %s, bound to %r.",
             plan.report_name, plan.workspace_ref, plan.semantic_model_name)
    return 0


def _print_dry_run(plan: ReportPlan) -> int:
    """Credential-free preview of every intended REST call + assembled parts. No auth, no mutation."""
    LOG.info("[DRY-RUN] No authentication and no changes will be made.")
    LOG.info("[DRY-RUN] Config: %s", plan.config_path or "<none>")
    LOG.info("[DRY-RUN] enable_report=%s", plan.enable)
    LOG.info("[DRY-RUN] Workspace: %s", plan.workspace_ref)
    LOG.info("[DRY-RUN] Report: %r", plan.report_name)
    LOG.info("[DRY-RUN] Binds to semantic model: name=%r id=%s",
             plan.semantic_model_name, plan.semantic_model_id or "<resolved at deploy>")
    LOG.info("[DRY-RUN] Project folder: %s", os.path.relpath(REPORT_DIR, _REPO_ROOT))

    parts = assemble_parts(plan.semantic_model_id)
    LOG.info("[DRY-RUN] Would assemble %d PBIR definition parts (InlineBase64):", len(parts))
    for p in parts:
        marker = ""
        if p["path"] == "definition.pbir":
            marker = (" <- semanticmodelid substituted" if plan.semantic_model_id
                      else f" <- token {SEMANTIC_MODEL_ID_TOKEN} (resolved at deploy)")
        elif p["path"] == THEME_RESOURCE_REL:
            marker = " <- canonical fabric/theme/zava-blue-theme.json injected"
        LOG.info("[DRY-RUN]     - %s%s", p["path"], marker)

    LOG.info("[DRY-RUN] Would GET %s/workspaces (resolve workspace id by name).", FABRIC_API_BASE)
    LOG.info("[DRY-RUN] Would GET .../semanticModels (resolve Step-14 model id by name).")
    LOG.info("[DRY-RUN] Would GET .../reports then POST .../reports (create) or "
             ".../reports/{id}/updateDefinition (update) — idempotent.")
    if plan.rebind:
        LOG.info("[DRY-RUN] Would call report.report_rebind(report=%r, dataset=%r, "
                 "report_workspace=%r, dataset_workspace=%r).",
                 plan.report_name, plan.semantic_model_name,
                 plan.workspace_ref, plan.workspace_ref)
    else:
        LOG.info("[DRY-RUN] --no-rebind set — report_rebind would be skipped.")
    LOG.info("[DRY-RUN] Committed PBIR definition: %s", os.path.relpath(REPORT_DIR, _REPO_ROOT))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deploy the Zava PBIP/PBIR report to the Step-10 workspace, bound to the "
                    "Step-14 Direct Lake semantic model (idempotent).",
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
    parser.add_argument("--report-name", help="Report display name (overrides config/env).")
    parser.add_argument(
        "--semantic-model-name",
        help="Step-14 semantic model display name to bind to (overrides config/env).",
    )
    parser.add_argument(
        "--semantic-model-id",
        help="Step-14 semantic model GUID to bind to (skips name resolution).",
    )
    parser.add_argument(
        "--no-rebind", action="store_true",
        help="Skip the post-deploy report_rebind (definition.pbir already binds by id).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print intended REST calls + assembled parts without authenticating or mutating.",
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
            report_name=args.report_name,
            semantic_model_name=args.semantic_model_name,
            semantic_model_id=args.semantic_model_id,
            no_rebind=args.no_rebind,
        )
        return run(plan, dry_run=args.dry_run)
    except ReportDeployError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
