#!/usr/bin/env python3
"""Pre-deploy preflight checks for the Zava Databricks + Fabric demo (plan Step 23).

Run this **before** ``scripts/deploy.py`` to fail fast — with actionable messages — on
anything that would otherwise blow up halfway through a multi-wave deployment. It is the
single front door that confirms the environment is sane: config is contract-valid, the
region/capacity match the demo's requirements, the right CLIs are installed, and the
operator is pointed at the correct (MCAPS) subscription.

What it checks (plan Step 23 + §1.7 + §3.4)
-------------------------------------------
1. **Config contract** — both configs validate against ``scripts/config_schema.py``
   (the canonical validator shared by every entry point).
2. **Region** (unconditional allow-list, plan §1.7) — the supported set is
   ``{eastus2, westus}`` and ``scripts/config_schema.py`` is the **authoritative region
   contract**: it rejects anything outside that set **unconditionally** (there is no
   "would-pass elsewhere" path). ``check_region()`` is therefore coherent with — and never
   contradicts — ``config_schema.validate_file()``: ``eastus2`` (primary) / ``westus``
   (documented backup) PASS; **East US / South Central US always FAIL**. The *reason* is
   context-appropriate: when ``features.enable_operations_agent=true`` the optional
   Operations Agent (GA) is unavailable there (R11 §8); when it is false the Teams-free
   Activator **email** feature is itself region-agnostic, **but** the full demo is validated
   only for ``eastus2``/``westus`` so config_schema enforces the allow-list — to deploy
   elsewhere a customer must deliberately edit config_schema's allow-list (we never silently
   allow unvalidated regions). Any other non-allow-list region also FAILs. The genuinely
   *conditional* gate is the **Teams check** (enforced only when Operations Agent is on).
3. **Capacity** — ``F64`` is the floor whenever any AI / Fabric IQ feature (ontology,
   Data Agent, Operations Agent) is enabled (R9). A smaller SKU with no AI feature is a
   **trial-capacity caveat** (warn), not a hard failure. (The 60-day trial capacity
   excludes Copilot / Data Agent / Operations Agent — R9, R11.) An **existing** capacity is
   additionally probed read-only for its live state (Active/Resumed vs Paused); a fresh
   provision or an unprobeable capacity reports an explicit gap (never a silent pass).
4. **Microsoft Teams** — when ``enable_operations_agent=true`` the optional Teams Yes/No
   approval card path needs a Teams account (R11 §6, §8). This can't be verified by API,
   so it is surfaced as an actionable reminder.
5. **Required tooling** — ``az`` (+ ``az bicep``), ``python``, ``ms-fabric-cli`` (``fab``),
   the Databricks CLI, and the ``semantic-link-labs`` Python package.
6. **Subscription** — the active ``az`` subscription name should start with
   ``ME-MngEnvMCAP`` (the MCAPS subscription — repo conventions / plan §1).
7. **Tenant settings + SP** — the required Fabric tenant settings (plan §3.4) and the
   deploy service principal can't be fully introspected from here; they are surfaced as a
   reviewer checklist pointing at ``docs/manual-steps.md``.

Exit code
---------
``0`` when there are **no hard failures** (warnings are allowed — "clearly reports gaps").
Non-zero when any check FAILS. ``--strict`` promotes every WARN to a FAIL (CI gate).

This script performs **read-only** probes (``which`` / ``--version`` / ``az account show``)
and **never** mutates anything, requires no secrets, and does not deploy.

Usage
-----
    # Validate the committed samples (offline-friendly; tool probes report gaps):
    python scripts/preflight_checks.py

    # Point at real local configs and gate hard on every gap:
    python scripts/preflight_checks.py \
        --config fabric/config/deploy_config.json \
        --databricks-config databricks/config/databricks_config.json \
        --strict
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

# Import the canonical config schema/validator. ``scripts/`` is this file's own directory,
# so make sure it is importable whether invoked as ``python scripts/preflight_checks.py``
# or imported as a module.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import config_schema  # noqa: E402  (path bootstrap above)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(_THIS_DIR)

DEFAULT_DEPLOY_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)
DEFAULT_DATABRICKS_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "databricks", "config", "databricks_config.json"),
    os.path.join(_REPO_ROOT, "databricks", "config", "databricks_config.sample.json"),
)

# Region policy (plan §1.7). Supported = the only US regions where every required
# capability is available together; REJECTED = excluded by the Operations Agent (GA).
SUPPORTED_REGIONS = config_schema.SUPPORTED_REGIONS          # {"eastus2", "westus"}
OPERATIONS_AGENT_REJECTED_REGIONS = config_schema.REJECTED_REGIONS  # {"eastus", "southcentralus"}
PRIMARY_REGION = "eastus2"

# AI / Fabric IQ capacity floor (F64) — plan Step 1 / R9.
MIN_AI_CAPACITY_UNITS = config_schema.MIN_AI_CAPACITY_UNITS  # 64

# MCAPS subscription name prefix (plan §1).
EXPECTED_SUBSCRIPTION_PREFIX = "ME-MngEnvMCAP"

# Status markers.
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

_MARK = {PASS: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}


# ---------------------------------------------------------------------------
# Check result model
# ---------------------------------------------------------------------------

class CheckResult:
    """One preflight check outcome with an actionable remediation message."""

    def __init__(self, name: str, status: str, message: str, remediation: str = "") -> None:
        self.name = name
        self.status = status
        self.message = message
        self.remediation = remediation

    def render(self) -> str:
        line = f"  {_MARK[self.status]} {self.name}: {self.message}"
        if self.remediation and self.status in (WARN, FAIL):
            line += f"\n         -> {self.remediation}"
        return line


# ---------------------------------------------------------------------------
# Config resolution helpers
# ---------------------------------------------------------------------------

def _resolve_config_path(explicit: Optional[str], candidates: Tuple[str, ...]) -> Optional[str]:
    """Return the explicit path (if given) or the first existing default candidate."""
    if explicit:
        return explicit
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


def _load_raw(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Tool probing
# ---------------------------------------------------------------------------

def _which(executable: str) -> Optional[str]:
    return shutil.which(executable)


def _probe_version(args: List[str]) -> Tuple[bool, str]:
    """Run a ``--version``-style probe; return (ok, first_line_of_output)."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    first = out[0] if out else ""
    return proc.returncode == 0, first


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_config_contract(
    deploy_path: Optional[str], databricks_path: Optional[str]
) -> List[CheckResult]:
    results: List[CheckResult] = []

    for label, path in (("Fabric/orchestration config", deploy_path),
                         ("Databricks config", databricks_path)):
        if not path:
            results.append(CheckResult(
                f"config ({label})", WARN,
                "no config file found",
                "Copy the matching *.sample.json to its local (gitignored) name and fill "
                "the <PLACEHOLDER> tokens, or pass --config / --databricks-config.",
            ))
            continue
        try:
            config_schema.validate_file(path)
            rel = os.path.relpath(path, _REPO_ROOT)
            results.append(CheckResult(
                f"config ({label})", PASS, f"{rel} is contract-valid",
            ))
        except config_schema.ConfigError as exc:
            results.append(CheckResult(
                f"config ({label})", FAIL,
                f"{os.path.relpath(path, _REPO_ROOT)} failed validation: {exc}",
                "Fix the offending JSON path named above (scripts/config_schema.py is the "
                "authoritative contract).",
            ))
    return results


def check_region(region: Optional[str], operations_agent: bool) -> CheckResult:
    """Region gate (plan Step 23 / §1.7) — coherent with ``config_schema``.

    The curated allow-list in ``config_schema`` (``eastus2`` primary / ``westus`` backup) is
    the canonical, **authoritative** region policy and is enforced **unconditionally** — there
    is no "would-pass in another region" branch. This check therefore never contradicts
    ``config_schema.validate_file()``:

    * ``eastus2`` / ``westus``                       -> PASS (primary / documented backup).
    * ``eastus`` / ``southcentralus``                -> FAIL **regardless** of Operations Agent
      (only the *reason* in the message changes).
    * any other region outside the allow-list        -> FAIL (config_schema rejects it too).

    ``operations_agent`` only changes the *explanation*, never the verdict for these regions.
    """
    if not region or config_schema.is_placeholder(region):
        return CheckResult(
            "region", WARN, "region is unset / placeholder",
            f"Set deploy_config.region (primary '{PRIMARY_REGION}', backup 'westus').",
        )
    region = region.strip().lower()

    if region in SUPPORTED_REGIONS:
        note = "primary" if region == PRIMARY_REGION else "documented backup"
        return CheckResult(
            "region", PASS,
            f"{region!r} is supported ({note}); all required capabilities available together",
        )

    remediation = (
        f"Use '{PRIMARY_REGION}' (primary) or 'westus' (documented backup) — the only regions "
        f"the full demo is validated in (config_schema enforces this allow-list unconditionally)."
    )

    if region in OPERATIONS_AGENT_REJECTED_REGIONS:
        if operations_agent:
            return CheckResult(
                "region", FAIL,
                f"{region!r} is rejected: features.enable_operations_agent=true and the "
                f"Operations Agent (GA) is unavailable in East US / South Central US (R11 §8). "
                f"config_schema enforces the {sorted(SUPPORTED_REGIONS)} allow-list unconditionally.",
                remediation,
            )
        return CheckResult(
            "region", FAIL,
            f"{region!r} is rejected by the curated allow-list {sorted(SUPPORTED_REGIONS)}. "
            f"The Teams-free Activator email feature is itself region-agnostic, BUT the full demo "
            f"is validated only for eastus2/westus, so config_schema enforces the allow-list "
            f"unconditionally — deploying elsewhere requires deliberately editing config_schema's "
            f"allow-list (we never silently allow unvalidated regions).",
            remediation,
        )

    # Any other non-allow-list region: config_schema also rejects it ("must be in supported
    # set"), so report FAIL to stay coherent — never claim it would pass.
    return CheckResult(
        "region", FAIL,
        f"{region!r} is not in the supported allow-list {sorted(SUPPORTED_REGIONS)}; "
        f"config_schema rejects it unconditionally (the full demo is unvalidated there)",
        remediation,
    )


def check_capacity(sku: Optional[str], ai_features_enabled: bool) -> CheckResult:
    if not sku or config_schema.is_placeholder(sku):
        return CheckResult(
            "capacity SKU", WARN, "capacity.sku is unset / placeholder",
            "Set capacity.sku to 'F64' (the AI / Fabric IQ floor — R9).",
        )
    units = config_schema.fabric_sku_units(sku)
    if units is None:
        return CheckResult(
            "capacity SKU", WARN, f"capacity.sku={sku!r} is not a recognised Fabric SKU",
            "Use a valid F-SKU (e.g. F64).",
        )
    if units >= MIN_AI_CAPACITY_UNITS:
        return CheckResult(
            "capacity SKU", PASS, f"{sku} meets the F64 floor for AI / Fabric IQ features",
        )
    if ai_features_enabled:
        return CheckResult(
            "capacity SKU", FAIL,
            f"{sku} is below F64 but AI / Fabric IQ features are enabled (ontology / Data "
            f"Agent / Operations Agent require >= F64 — R9)",
            "Raise capacity.sku to at least 'F64', or disable the AI features.",
        )
    return CheckResult(
        "capacity SKU", WARN,
        f"{sku} is below F64 (trial-capacity caveat): fine for non-AI data-engineering "
        f"phases only — the 60-day trial excludes Copilot / Data Agent / Operations Agent",
        "Use 'F64' if you want the AI pillars; otherwise this is acceptable for the "
        "data-engineering-only path.",
    )


def _probe_capacity_state(existing_id: str, subscription: Optional[str]) -> Tuple[bool, str]:
    """Read-only ``az`` probe of an existing Fabric capacity's ``properties.state``.

    Returns ``(ok, value_or_error)``. Never mutates anything. ``ok`` is True only when the
    state string was successfully read.
    """
    az_path = _which("az")
    if not az_path:
        return False, "az CLI not on PATH"
    cmd = [az_path, "resource", "show", "--ids", existing_id,
           "--query", "properties.state", "-o", "tsv"]
    if subscription:
        cmd += ["--subscription", subscription]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, (err[0] if err else f"az exited {proc.returncode}")
    state = (proc.stdout or "").strip()
    if not state:
        return False, "empty state returned"
    return True, state


def check_capacity_state(capacity: dict, subscription: Optional[str]) -> CheckResult:
    """Verify an **existing** Fabric capacity is Active/Resumed (read-only).

    A Paused/Suspended capacity will block the deploy, so this surfaces it early. When the
    state cannot be probed (fresh provision, no creds, placeholder id) we emit an explicit
    gap (never a silent pass) — a clearly-reported gap is acceptable for a preflight.
    """
    capacity = capacity or {}
    name = capacity.get("name")
    use_existing = bool(capacity.get("use_existing"))
    existing_id = capacity.get("existing_capacity_id")

    if not use_existing:
        return CheckResult(
            "capacity state", WARN,
            "fresh-provision path (capacity.use_existing=false) — live state not verified "
            "(the capacity is created by the Bicep wave)",
            "After provisioning, ensure the capacity is Resumed/Active before the Fabric "
            "waves run (a Paused capacity blocks workspace/item creation).",
        )

    # Existing-capacity path: we need a usable ARM resource id to probe.
    if not existing_id or config_schema.is_placeholder(existing_id):
        return CheckResult(
            "capacity state", WARN,
            f"capacity.use_existing=true but existing_capacity_id is unset/placeholder "
            f"for {name or '<capacity>'} — cannot probe live state",
            "Fill capacity.existing_capacity_id with the real ARM id so preflight can verify "
            "the capacity is Resumed/Active (or confirm it manually before deploy).",
        )

    ok, value = _probe_capacity_state(existing_id, subscription)
    if not ok:
        return CheckResult(
            "capacity state", WARN,
            f"could not read live state of {name or '<capacity>'} ({value}) — not verified",
            "Run 'az login' to the MCAPS subscription (read-only), or confirm manually that "
            "the capacity is Resumed/Active before deploy.",
        )

    normalized = value.strip().lower()
    if normalized in ("active", "resumed", "succeeded", "running"):
        return CheckResult(
            "capacity state", PASS,
            f"{name or '<capacity>'} is {value!r} (Active/Resumed) — ready for deploy",
        )
    if normalized in ("paused", "suspended", "suspending", "pausing"):
        return CheckResult(
            "capacity state", FAIL,
            f"{name or '<capacity>'} is {value!r} — a Paused/Suspended capacity blocks the "
            f"Fabric waves",
            "Resume the capacity before deploy: 'az resource update --ids <id> ...' / the "
            "Fabric portal / scripts/pause_capacity.py's resume path.",
        )
    return CheckResult(
        "capacity state", WARN,
        f"{name or '<capacity>'} reports an unrecognised state {value!r} — verify it is "
        f"Active/Resumed before deploy",
        "Confirm the capacity is Resumed/Active (a non-Active capacity blocks the Fabric waves).",
    )


def check_teams(operations_agent: bool) -> CheckResult:
    if not operations_agent:
        return CheckResult(
            "Microsoft Teams", SKIP,
            "features.enable_operations_agent=false — Teams not required "
            "(default Activator email path is Teams-free)",
        )
    return CheckResult(
        "Microsoft Teams", WARN,
        "features.enable_operations_agent=true requires a Microsoft Teams account for the "
        "Yes/No approval card (R11 §6, §8) — cannot be verified programmatically",
        "Confirm the deploying user has a Teams license and the Operations Agent will be "
        "wired to a Teams channel (see docs/manual-steps.md, Step 20).",
    )


def check_tools() -> List[CheckResult]:
    results: List[CheckResult] = []

    # az CLI
    az_path = _which("az")
    if az_path:
        ok, ver = _probe_version([az_path, "version", "-o", "json"])
        results.append(CheckResult("tool: az", PASS if ok else WARN,
                                   "Azure CLI present"
                                   if ok else f"found ('{az_path}') but version probe failed: {ver}",
                                   "" if ok else "Ensure the Azure CLI runs ('az version')."))
        # az bicep is a sub-extension of az.
        bok, bver = _probe_version([az_path, "bicep", "version"])
        results.append(CheckResult("tool: bicep", PASS if bok else WARN,
                                   bver or "az bicep available",
                                   "" if bok else "Run 'az bicep install' to add the Bicep CLI."))
    else:
        results.append(CheckResult("tool: az", WARN, "Azure CLI ('az') not found on PATH",
                                   "Install the Azure CLI: https://aka.ms/azcli"))
        results.append(CheckResult("tool: bicep", WARN, "skipped (az not present)",
                                   "Install az, then run 'az bicep install'."))

    # python (this interpreter)
    results.append(CheckResult(
        "tool: python", PASS,
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    ))

    # ms-fabric-cli (the `fab` command)
    fab_path = _which("fab")
    if fab_path:
        ok, ver = _probe_version([fab_path, "--version"])
        results.append(CheckResult("tool: ms-fabric-cli", PASS if ok else WARN,
                                   ver or "fab CLI present",
                                   "" if ok else "Reinstall: pip install ms-fabric-cli"))
    else:
        results.append(CheckResult("tool: ms-fabric-cli", WARN, "'fab' (ms-fabric-cli) not found on PATH",
                                   "Install: pip install ms-fabric-cli"))

    # Databricks CLI
    dbx_path = _which("databricks")
    if dbx_path:
        ok, ver = _probe_version([dbx_path, "--version"])
        results.append(CheckResult("tool: databricks CLI", PASS if ok else WARN,
                                   ver or "databricks CLI present",
                                   "" if ok else "Reinstall the Databricks CLI (v0.2+)."))
    else:
        results.append(CheckResult("tool: databricks CLI", WARN, "'databricks' CLI not found on PATH",
                                   "Install: https://docs.databricks.com/dev-tools/cli/"))

    # semantic-link-labs (Python package, used by the semantic-model step)
    try:
        import importlib.util
        spec = importlib.util.find_spec("sempy_labs")
        if spec is not None:
            results.append(CheckResult("tool: semantic-link-labs", PASS, "sempy_labs importable"))
        else:
            results.append(CheckResult("tool: semantic-link-labs", WARN,
                                       "'semantic-link-labs' (sempy_labs) not importable",
                                       "Install: pip install semantic-link-labs"))
    except Exception as exc:  # pragma: no cover - defensive
        results.append(CheckResult("tool: semantic-link-labs", WARN,
                                   f"could not probe sempy_labs ({exc})",
                                   "Install: pip install semantic-link-labs"))

    return results


def check_subscription() -> CheckResult:
    az_path = _which("az")
    if not az_path:
        return CheckResult(
            "subscription", SKIP, "az not present — cannot read the active subscription",
            f"Install az and 'az login' to the {EXPECTED_SUBSCRIPTION_PREFIX}... (MCAPS) subscription.",
        )
    try:
        proc = subprocess.run(
            [az_path, "account", "show", "-o", "json"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("subscription", WARN, f"az account show failed ({exc})",
                           "Run 'az login' first.")
    if proc.returncode != 0:
        return CheckResult(
            "subscription", WARN, "not logged in (az account show returned non-zero)",
            f"Run 'az login' and select the {EXPECTED_SUBSCRIPTION_PREFIX}... subscription.",
        )
    try:
        acct = json.loads(proc.stdout)
        name = acct.get("name", "")
    except (json.JSONDecodeError, AttributeError):
        return CheckResult("subscription", WARN, "could not parse az account show output",
                           "Run 'az login' again.")
    if name.startswith(EXPECTED_SUBSCRIPTION_PREFIX):
        return CheckResult("subscription", PASS, f"active subscription {name!r} (MCAPS)")
    return CheckResult(
        "subscription", WARN,
        f"active subscription is {name!r}, expected one starting with "
        f"{EXPECTED_SUBSCRIPTION_PREFIX!r}",
        f"Run 'az account set --subscription <{EXPECTED_SUBSCRIPTION_PREFIX}...>' "
        f"(do NOT use migmartinez@microsoft.com / HNLI-DEV).",
    )


def check_tenant_settings_reminder(operations_agent: bool, policy_weaver: bool) -> CheckResult:
    """Tenant settings + SP can't be introspected here — surface the checklist (plan §3.4)."""
    required = [
        "Service principals can use Fabric APIs (scoped security group)",
        "Mirroring: 'Enable new mirrored catalog items (preview)'",
        "Copilot/AI + Data Agent + Fabric IQ (GA) + ontology (preview)",
        "Graph (GA) tenant setting",
        "Fabric Activator (Reflex) enabled (default email path)",
        "Git integration + deployment pipelines",
        "OneLake security (preview)",
        "Purview Fabric live view / tenant scan",
    ]
    if operations_agent:
        required.append("Operations Agent (GA) admin switch + Real-Time Intelligence + Copilot/Azure OpenAI")
    if policy_weaver:
        required.append("OneLake security roles writable by the Policy Weaver identity (R5)")
    return CheckResult(
        "tenant settings + SP", WARN,
        "required Fabric tenant settings + deploy SP cannot be verified from here "
        f"({len(required)} settings — plan §3.4)",
        "Confirm with your Fabric admin (you have rights): " + "; ".join(required)
        + ". See docs/manual-steps.md.",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_preflight(
    deploy_config_path: Optional[str],
    databricks_config_path: Optional[str],
    *,
    operations_agent_override: Optional[bool] = None,
    strict: bool = False,
) -> Tuple[int, List[CheckResult]]:
    """Execute every check; return (exit_code, results). Exit 0 unless a hard FAIL."""
    results: List[CheckResult] = []

    deploy_path = _resolve_config_path(deploy_config_path, DEFAULT_DEPLOY_CONFIG_CANDIDATES)
    databricks_path = _resolve_config_path(databricks_config_path, DEFAULT_DATABRICKS_CONFIG_CANDIDATES)

    # 1. Config contract.
    results.extend(check_config_contract(deploy_path, databricks_path))

    # Pull the values the conditional checks need from the *raw* deploy config. The region
    # gate stays **coherent** with config_schema (which already rejects non-allow-list
    # regions unconditionally); reading the raw values here just lets us craft
    # context-appropriate, actionable messaging without re-implementing the contract.
    region: Optional[str] = None
    features = {}
    governance = {}
    capacity = {}
    subscription: Optional[str] = None
    if deploy_path and os.path.exists(deploy_path):
        try:
            raw = _load_raw(deploy_path)
            region = raw.get("region")
            features = raw.get("features", {}) or {}
            governance = raw.get("governance", {}) or {}
            capacity = raw.get("capacity", {}) or {}
            subscription = raw.get("subscription") or (raw.get("azure", {}) or {}).get("subscription")
        except (OSError, json.JSONDecodeError) as exc:
            results.append(CheckResult("config (raw read)", FAIL,
                                       f"could not read {deploy_path}: {exc}",
                                       "Ensure the file is valid JSON."))

    operations_agent = (
        operations_agent_override
        if operations_agent_override is not None
        else bool(features.get("enable_operations_agent", False))
    )
    ai_features_enabled = bool(
        features.get("enable_ontology")
        or features.get("enable_data_agent")
        or operations_agent
    )

    # 2. Region (conditional).
    results.append(check_region(region, operations_agent))

    # 3. Capacity (SKU floor + live state probe).
    results.append(check_capacity(capacity.get("sku"), ai_features_enabled))
    results.append(check_capacity_state(capacity, subscription))

    # 4. Teams (only when Operations Agent on).
    results.append(check_teams(operations_agent))

    # 5. Tooling.
    results.extend(check_tools())

    # 6. Subscription.
    results.append(check_subscription())

    # 7. Tenant settings + SP reminder.
    results.append(check_tenant_settings_reminder(
        operations_agent, bool(governance.get("policy_weaver_enabled", False))
    ))

    # Decide the exit code.
    has_fail = any(r.status == FAIL for r in results)
    has_warn = any(r.status == WARN for r in results)
    if has_fail or (strict and has_warn):
        return 1, results
    return 0, results


def _print_report(results: List[CheckResult], strict: bool) -> None:
    print("=" * 78)
    print("Zava demo — preflight checks")
    print("=" * 78)
    for r in results:
        print(r.render())
    n_fail = sum(1 for r in results if r.status == FAIL)
    n_warn = sum(1 for r in results if r.status == WARN)
    n_pass = sum(1 for r in results if r.status == PASS)
    n_skip = sum(1 for r in results if r.status == SKIP)
    print("-" * 78)
    print(f"Summary: {n_pass} PASS, {n_warn} WARN, {n_fail} FAIL, {n_skip} SKIP"
          + ("  (--strict: warnings are failures)" if strict else ""))
    if n_fail or (strict and n_warn):
        print("Result: BLOCKED — resolve the FAIL items above before deploying.")
    elif n_warn:
        print("Result: OK with gaps — review the WARN items (deployment may proceed).")
    else:
        print("Result: OK — environment is ready for scripts/deploy.py.")
    print("=" * 78)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preflight_checks.py",
        description="Pre-deploy preflight checks for the Zava Databricks + Fabric demo.",
    )
    parser.add_argument(
        "--config",
        help="Path to the Fabric/orchestration deploy_config.json "
             "(defaults to fabric/config/deploy_config.json, then the committed sample).",
    )
    parser.add_argument(
        "--databricks-config",
        help="Path to databricks_config.json "
             "(defaults to databricks/config/databricks_config.json, then the sample).",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Promote every WARN to a FAIL (CI gate). Default: warnings are allowed.",
    )
    ops = parser.add_mutually_exclusive_group()
    ops.add_argument(
        "--operations-agent", dest="operations_agent", action="store_const", const=True,
        default=None,
        help="Force the Operations-Agent-on path for the conditional region/Teams checks "
             "(overrides config).",
    )
    ops.add_argument(
        "--no-operations-agent", dest="operations_agent", action="store_const", const=False,
        help="Force the Operations-Agent-off path for the conditional region/Teams checks.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    exit_code, results = run_preflight(
        args.config,
        args.databricks_config,
        operations_agent_override=args.operations_agent,
        strict=args.strict,
    )
    _print_report(results, args.strict)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
