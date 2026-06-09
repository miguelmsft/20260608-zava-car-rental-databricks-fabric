#!/usr/bin/env python3
"""Canonical config schema contract + validator for the Zava Databricks + Fabric demo.

This is the *single* loader/validator used by `deploy.py`, `preflight_checks.py`, and the
Fabric scripts so that every entry point enforces the same contract.

Two config shapes are validated:
  * Fabric/orchestration config  -> `fabric/config/deploy_config.sample.json`
  * Databricks config            -> `databricks/config/databricks_config.sample.json`

Design goals
------------
* **Fail-fast**: the first missing required field, wrong type, bad enum value, bad format,
  or unmet conditional raises `ConfigError` naming the exact JSON path (e.g.
  `deploy_config.capacity.existing_capacity_id is required when capacity.use_existing=true`).
* **No secrets**: configs only ever carry resource names / ids / placeholders. Secrets are
  referenced by Key Vault name or acquired via `az login` at runtime -- never stored here.
* **Placeholder-aware**: sample configs ship with documented `<PLACEHOLDER>` tokens. A value
  matching `^<[A-Z0-9_]+>$` is treated as "present but unresolved": it satisfies presence /
  non-empty checks and *format* checks are skipped (so the committed samples validate green),
  but it is reported in the resolved plan so operators know what they must still fill in.

CLI
---
  python scripts/config_schema.py --validate <file> [<file> ...]
      Sniff each file's shape, validate it, print the resolved fresh-vs-existing plan.
      Exit 0 if all valid; non-zero (with the offending JSON path) on the first failure.

  python scripts/config_schema.py --selftest
      Run the built-in positive (fresh + existing paths) and negative cases.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Constants / shared rules
# ---------------------------------------------------------------------------

SUPPORTED_REGIONS = {"eastus2", "westus"}
# Regions explicitly rejected by the region decision (plan.md §1.7): the Operations Agent
# (GA) is unavailable in East US and South Central US.
REJECTED_REGIONS = {"eastus", "southcentralus"}

CAPACITY_SKU_RE = re.compile(r"^F(2|4|8|16|32|64|128|256|512|1024|2048)$")
# AI / Fabric IQ phases (ontology, Data Agent, Operations Agent, Copilot) require at least
# this many Fabric capacity units (F64). The plan documents no Copilot-Capacity escape hatch
# field, so F64 is the hard floor whenever any AI feature is enabled.
MIN_AI_CAPACITY_UNITS = 64
CAPACITY_NAME_RE = re.compile(r"^[a-z0-9-]{1,63}$")
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ARM_ID_RE = re.compile(r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/.+", re.IGNORECASE)
HTTPS_RE = re.compile(r"^https://", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
INGESTION_VARIATIONS = {"2A", "2B"}
DATABRICKS_SKUS = {"premium"}


class ConfigError(ValueError):
    """Raised on the first contract violation, naming the exact JSON path."""


# ---------------------------------------------------------------------------
# Low-level field helpers (all raise ConfigError with a JSON path)
# ---------------------------------------------------------------------------

def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(PLACEHOLDER_RE.match(value))


def _get(obj: Dict[str, Any], key: str, path: str) -> Tuple[bool, Any]:
    """Return (present, value). Treats null and absent identically (absent)."""
    if not isinstance(obj, dict):
        raise ConfigError(f"{path} must be an object")
    if key not in obj or obj[key] is None:
        return False, None
    return True, obj[key]


def require(obj: Dict[str, Any], key: str, path: str) -> Any:
    present, value = _get(obj, key, path)
    if not present:
        raise ConfigError(f"{path}.{key} is required")
    return value


def require_string(obj: Dict[str, Any], key: str, path: str, *, non_empty: bool = True) -> str:
    value = require(obj, key, path)
    full = f"{path}.{key}"
    if not isinstance(value, str):
        raise ConfigError(f"{full} must be a string (got {type(value).__name__})")
    if non_empty and value.strip() == "":
        raise ConfigError(f"{full} must be a non-empty string")
    return value


def require_bool(obj: Dict[str, Any], key: str, path: str) -> bool:
    value = require(obj, key, path)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key} must be a boolean (got {type(value).__name__})")
    return value


def require_int(obj: Dict[str, Any], key: str, path: str) -> int:
    value = require(obj, key, path)
    # bool is a subclass of int -- reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key} must be an integer (got {type(value).__name__})")
    return value


def require_enum(obj: Dict[str, Any], key: str, path: str, allowed: set) -> str:
    value = require_string(obj, key, path)
    if value not in allowed:
        raise ConfigError(
            f"{path}.{key} must be one of {sorted(allowed)} (got {value!r})"
        )
    return value


def require_object(obj: Dict[str, Any], key: str, path: str) -> Dict[str, Any]:
    value = require(obj, key, path)
    if not isinstance(value, dict):
        raise ConfigError(f"{path}.{key} must be an object (got {type(value).__name__})")
    return value


def check_format(value: Any, pattern: re.Pattern, full_path: str, what: str) -> None:
    """Validate a string against a regex, but skip documented <PLACEHOLDER> tokens.

    Type-guards first: a non-string value raises a clear ConfigError naming the JSON path
    instead of letting an unhandled ``TypeError`` escape from ``pattern.match``.
    """
    if not isinstance(value, str):
        raise ConfigError(f"{full_path} must be a string (got {type(value).__name__})")
    if is_placeholder(value):
        return
    if not pattern.match(value):
        raise ConfigError(f"{full_path} must be {what} (got {value!r})")


def conditional_required(
    obj: Dict[str, Any], key: str, path: str, *, when_desc: str, as_string: bool = True
) -> Any:
    """Require a field that is conditionally mandatory; error message states the condition.

    When ``as_string`` is True (the default — every conditional field in the contract is a
    string), a present-but-wrong-type value raises a clear ConfigError naming the JSON path
    rather than allowing a downstream ``TypeError``.
    """
    present, value = _get(obj, key, path)
    if not present:
        raise ConfigError(f"{path}.{key} is required when {when_desc}")
    if as_string and not isinstance(value, str):
        raise ConfigError(
            f"{path}.{key} must be a string (got {type(value).__name__}) when {when_desc}"
        )
    if isinstance(value, str) and value.strip() == "":
        raise ConfigError(f"{path}.{key} is required when {when_desc}")
    return value


def fabric_sku_units(sku: str) -> int | None:
    """Return the numeric capacity-unit count for a Fabric SKU (e.g. 'F64' -> 64).

    Returns None for non-matching / placeholder values so callers can skip the comparison.
    """
    match = CAPACITY_SKU_RE.match(sku)
    return int(match.group(1)) if match else None


def optional_string(obj: Dict[str, Any], key: str, path: str) -> Any:
    """Validate an optional string field: typed if present, otherwise ignored.

    Used for the many soft-optional keys the Fabric scripts read with ``.get()`` defaults
    (Step 28 config-contract completeness). Presence is NOT required; a wrong (non-string)
    type still raises a ConfigError naming the JSON path.
    """
    present, value = _get(obj, key, path)
    if present and not isinstance(value, str):
        raise ConfigError(f"{path}.{key} must be a string (got {type(value).__name__})")
    return value if present else None


def optional_bool(obj: Dict[str, Any], key: str, path: str) -> Any:
    present, value = _get(obj, key, path)
    if present and not isinstance(value, bool):
        raise ConfigError(f"{path}.{key} must be a boolean (got {type(value).__name__})")
    return value if present else None


# ---------------------------------------------------------------------------
# Fabric / orchestration config  (deploy_config.json)
# ---------------------------------------------------------------------------

def validate_deploy_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    root = "deploy_config"
    if not isinstance(cfg, dict):
        raise ConfigError(f"{root} must be a JSON object")

    # --- region ---
    region = require_string(cfg, "region", root)
    if region in REJECTED_REGIONS:
        raise ConfigError(
            f"{root}.region={region!r} is rejected (plan.md §1.7): the Operations Agent (GA) "
            f"is unavailable in East US / South Central US. Use one of {sorted(SUPPORTED_REGIONS)} "
            f"(default 'eastus2')."
        )
    if region not in SUPPORTED_REGIONS:
        raise ConfigError(
            f"{root}.region must be in supported set {sorted(SUPPORTED_REGIONS)} "
            f"(default 'eastus2', plan.md §1.7); got {region!r}"
        )

    # --- capacity ---
    cap = require_object(cfg, "capacity", root)
    cap_path = f"{root}.capacity"
    sku = require_string(cap, "sku", cap_path)
    check_format(sku, CAPACITY_SKU_RE, f"{cap_path}.sku", "a valid Fabric SKU, e.g. F2/F4/.../F64/F128")
    cap_name = require_string(cap, "name", cap_path)
    check_format(cap_name, CAPACITY_NAME_RE, f"{cap_path}.name", "1-63 chars of lowercase letters, digits or hyphens")
    cap_use_existing = require_bool(cap, "use_existing", cap_path)
    if cap_use_existing:
        existing_cap_id = conditional_required(
            cap, "existing_capacity_id", cap_path, when_desc="capacity.use_existing=true"
        )
        check_format(existing_cap_id, ARM_ID_RE, f"{cap_path}.existing_capacity_id", "a valid ARM resource id")

    # --- workspace ---
    ws = require_object(cfg, "workspace", root)
    ws_path = f"{root}.workspace"
    require_string(ws, "name", ws_path)
    ws_use_existing = require_bool(ws, "use_existing", ws_path)
    if ws_use_existing:
        existing_ws_id = conditional_required(
            ws, "existing_workspace_id", ws_path, when_desc="workspace.use_existing=true"
        )
        check_format(existing_ws_id, GUID_RE, f"{ws_path}.existing_workspace_id", "a GUID")
    # identity_object_id is optional (placeholder until Workspace Identity exists, Step 10)
    present, identity = _get(ws, "identity_object_id", ws_path)
    if present:
        if not isinstance(identity, str):
            raise ConfigError(f"{ws_path}.identity_object_id must be a string")
        check_format(identity, GUID_RE, f"{ws_path}.identity_object_id", "a GUID")
    # workspace_id is optional: the resolved/created Fabric workspace GUID, persisted by
    # fabric/scripts/00_create_workspace.py --write-config (Step 28). Consumed by the ADLS
    # hardening wave to construct the R10 trusted-workspace resourceId. Placeholder until Step 10.
    present, ws_guid = _get(ws, "workspace_id", ws_path)
    if present:
        if not isinstance(ws_guid, str):
            raise ConfigError(f"{ws_path}.workspace_id must be a string")
        check_format(ws_guid, GUID_RE, f"{ws_path}.workspace_id", "a GUID")

    # --- source ---
    src = require_object(cfg, "source", root)
    src_path = f"{root}.source"
    require_string(src, "databricks_catalog", src_path)
    require_string(src, "gold_schema", src_path)

    # --- ingestion ---
    ing = require_object(cfg, "ingestion", root)
    require_enum(ing, "variation", f"{root}.ingestion", INGESTION_VARIATIONS)

    # --- features ---
    feat = require_object(cfg, "features", root)
    feat_path = f"{root}.features"
    enable_ontology = require_bool(feat, "enable_ontology", feat_path)
    enable_data_agent = require_bool(feat, "enable_data_agent", feat_path)
    enable_eventhouse = require_bool(feat, "enable_eventhouse", feat_path)
    enable_activator_email = require_bool(feat, "enable_activator_email", feat_path)
    enable_operations_agent = require_bool(feat, "enable_operations_agent", feat_path)

    if enable_activator_email and not enable_eventhouse:
        raise ConfigError(
            f"{feat_path}.enable_eventhouse must be true when features.enable_activator_email=true "
            f"(shared Eventhouse / KustoDatabase source)"
        )
    if enable_operations_agent and not enable_eventhouse:
        raise ConfigError(
            f"{feat_path}.enable_eventhouse must be true when features.enable_operations_agent=true "
            f"(shared Eventhouse / KustoDatabase source)"
        )
    if enable_operations_agent and region not in SUPPORTED_REGIONS:
        raise ConfigError(
            f"{root}.region must be in {sorted(SUPPORTED_REGIONS)} when "
            f"features.enable_operations_agent=true (plan.md §1.7 region exclusion)"
        )

    # AI / Fabric IQ phases (ontology, Data Agent, Operations Agent, Copilot) require a
    # capacity of at least F64 (plan.md Step 1). The contract documents no Copilot-Capacity
    # override field, so F64 is the hard floor whenever any AI feature is enabled. Placeholder
    # SKUs (e.g. "<SKU>") are skipped here and surfaced in the resolved plan instead.
    ai_features_enabled = enable_ontology or enable_data_agent or enable_operations_agent
    if ai_features_enabled and not is_placeholder(sku):
        units = fabric_sku_units(sku)
        if units is not None and units < MIN_AI_CAPACITY_UNITS:
            enabled_ai = [
                name
                for name, on in (
                    ("enable_ontology", enable_ontology),
                    ("enable_data_agent", enable_data_agent),
                    ("enable_operations_agent", enable_operations_agent),
                )
                if on
            ]
            raise ConfigError(
                f"{cap_path}.sku={sku!r} is below the F64 minimum required when AI / Fabric IQ "
                f"features are enabled ({', '.join('features.' + n for n in enabled_ai)}). "
                f"Use at least 'F64'."
            )

    # --- ingestion-path config sections (Step 28 config-contract completeness) -----------
    # The Fabric scripts (10/20/30/50/70/80) read these sections; the contract now declares
    # every key they consume so preflight/validation can no longer pass while a required
    # runtime field (e.g. a manual OAuth connection id) is missing. Required fields are
    # placeholder-aware (the committed sample validates green with <PLACEHOLDER> tokens) and
    # conditional sections only apply when their feature flag is on.

    # mirroring (Variation 1 — always runs; read by 10_create_mirrored_catalog.py). The
    # databricks_connection_id is the one-time OAuth connection from Step 11 (manual), so it
    # is required (placeholder until created). The rest are optional resolved-at-runtime hints.
    mir = require_object(cfg, "mirroring", root)
    mir_path = f"{root}.mirroring"
    require_string(mir, "databricks_connection_id", mir_path)
    for k in ("mode", "databricks_workspace_url", "storage_connection_id", "item_name", "description"):
        optional_string(mir, k, mir_path)
    optional_bool(mir, "auto_sync", mir_path)
    present, schemas = _get(mir, "schemas", mir_path)
    if present and not isinstance(schemas, list):
        raise ConfigError(f"{mir_path}.schemas must be a list of schema names")

    # shortcut (Variation 2 — always runs; read by 20_create_shortcut.py). connection_id is
    # the ADLS shortcut connection (manual consent, Step 12) -> required. The ADLS target must
    # be given as abfss_path OR (adls_location + adls_subpath).
    sc = require_object(cfg, "shortcut", root)
    sc_path = f"{root}.shortcut"
    require_string(sc, "connection_id", sc_path)
    abfss = optional_string(sc, "abfss_path", sc_path)
    adls_loc = optional_string(sc, "adls_location", sc_path)
    adls_sub = optional_string(sc, "adls_subpath", sc_path)
    if not abfss and not (adls_loc and adls_sub):
        raise ConfigError(
            f"{sc_path} requires the ADLS target as either {sc_path}.abfss_path or both "
            f"{sc_path}.adls_location + {sc_path}.adls_subpath (Variation-2 path discovery)"
        )
    for k in ("lakehouse_id", "lakehouse_name", "name", "path"):
        optional_string(sc, k, sc_path)

    # lakehouse (target for shortcut + semantic model; read by several scripts).
    lh = require_object(cfg, "lakehouse", root)
    lh_path = f"{root}.lakehouse"
    require_string(lh, "name", lh_path)
    optional_string(lh, "id", lh_path)

    # semantic_model (Direct Lake; read by 30_create_semantic_model.py).
    sm = require_object(cfg, "semantic_model", root)
    sm_path = f"{root}.semantic_model"
    require_string(sm, "name", sm_path)
    for k in ("id", "lakehouse_id", "lakehouse_name", "rebind_report",
              "source_name", "source_type", "thin_gold_schema"):
        optional_string(sm, k, sm_path)

    # report (read by 50_deploy_report.py).
    rpt = require_object(cfg, "report", root)
    rpt_path = f"{root}.report"
    require_string(rpt, "name", rpt_path)
    optional_string(rpt, "semantic_model_id", rpt_path)

    # ontology (conditional on enable_ontology; read by 60_create_ontology.py).
    if enable_ontology:
        ont = require_object(cfg, "ontology", root)
        ont_path = f"{root}.ontology"
        conditional_required(ont, "name", ont_path, when_desc="features.enable_ontology=true")
        optional_string(ont, "graph_name", ont_path)

    # data_agent (conditional on enable_data_agent; read by 70_create_data_agent.py).
    if enable_data_agent:
        da = require_object(cfg, "data_agent", root)
        da_path = f"{root}.data_agent"
        conditional_required(da, "name", da_path, when_desc="features.enable_data_agent=true")
        for k in ("graph_name", "semantic_model_name"):
            optional_string(da, k, da_path)

    # operations_agent (conditional on enable_operations_agent; read by 80_create_operations_agent.py).
    if enable_operations_agent:
        oa = require_object(cfg, "operations_agent", root)
        oa_path = f"{root}.operations_agent"
        conditional_required(oa, "agent_name", oa_path, when_desc="features.enable_operations_agent=true")
        conditional_required(
            oa, "message_recipient_upn", oa_path, when_desc="features.enable_operations_agent=true"
        )
        optional_bool(oa, "should_run", oa_path)
        for k in ("action_pipeline_id", "action_pipeline_name", "definition_part_path"):
            optional_string(oa, k, oa_path)

    # --- realtime (conditional on eventhouse) ---
    if enable_eventhouse:
        rt = require_object(cfg, "realtime", root)
        rt_path = f"{root}.realtime"
        conditional_required(rt, "eventhouse_name", rt_path, when_desc="features.enable_eventhouse=true")
        conditional_required(rt, "kql_database_name", rt_path, when_desc="features.enable_eventhouse=true")
        conditional_required(rt, "kql_table_name", rt_path, when_desc="features.enable_eventhouse=true")

    # --- alerting (conditional on activator email) ---
    if enable_activator_email:
        alerting = require_object(cfg, "alerting", root)
        conditional_required(
            alerting, "site_manager_email", f"{root}.alerting",
            when_desc="features.enable_activator_email=true",
        )

    # --- governance ---
    gov = require_object(cfg, "governance", root)
    require_bool(gov, "policy_weaver_enabled", f"{root}.governance")
    # purview_account is optional (required only if Purview steps enabled -> Step 22)

    return {
        "kind": "fabric",
        "region": region,
        "capacity": "existing" if cap_use_existing else "fresh",
        "workspace": "existing" if ws_use_existing else "fresh",
        "features": {
            "ontology": enable_ontology,
            "data_agent": enable_data_agent,
            "eventhouse": enable_eventhouse,
            "activator_email": enable_activator_email,
            "operations_agent": enable_operations_agent,
        },
    }


# ---------------------------------------------------------------------------
# Databricks config  (databricks_config.json)
# ---------------------------------------------------------------------------

def validate_databricks_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    root = "databricks_config"
    if not isinstance(cfg, dict):
        raise ConfigError(f"{root} must be a JSON object")

    ws = require_object(cfg, "workspace", root)
    ws_path = f"{root}.workspace"
    use_existing = require_bool(ws, "use_existing", ws_path)
    if use_existing:
        host_url = conditional_required(
            ws, "host_url", ws_path, when_desc="workspace.use_existing=true"
        )
        check_format(host_url, HTTPS_RE, f"{ws_path}.host_url", "an https:// URL")
        resource_id = conditional_required(
            ws, "resource_id", ws_path, when_desc="workspace.use_existing=true"
        )
        check_format(resource_id, ARM_ID_RE, f"{ws_path}.resource_id", "a valid ARM resource id")
    # sku is always required and must be premium (UC requirement, R8)
    sku = require_enum(ws, "sku", ws_path, DATABRICKS_SKUS)

    require_string(cfg, "catalog", root)
    require_string(cfg, "managed_storage_account", root)
    # access_connector_id is optional (populated by Bicep output on the fresh path)
    present, acc_id = _get(cfg, "access_connector_id", root)
    if present:
        if not isinstance(acc_id, str):
            raise ConfigError(f"{root}.access_connector_id must be a string")
        check_format(acc_id, ARM_ID_RE, f"{root}.access_connector_id", "a valid ARM resource id")
    require_int(cfg, "data_seed", root)

    return {
        "kind": "databricks",
        "workspace": "existing" if use_existing else "fresh",
        "sku": sku,
    }


# ---------------------------------------------------------------------------
# Shape sniffing + file loading
# ---------------------------------------------------------------------------

def sniff_kind(cfg: Dict[str, Any]) -> str:
    """Decide whether a parsed config is the Fabric or Databricks shape."""
    if not isinstance(cfg, dict):
        raise ConfigError("top-level config must be a JSON object")
    fabric_markers = {"capacity", "features", "source", "ingestion", "governance"}
    databricks_markers = {"catalog", "managed_storage_account", "data_seed"}
    if fabric_markers & cfg.keys():
        return "fabric"
    if databricks_markers & cfg.keys():
        return "databricks"
    raise ConfigError(
        "unable to determine config kind: expected Fabric markers "
        f"{sorted(fabric_markers)} or Databricks markers {sorted(databricks_markers)}"
    )


def load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")


def validate_file(path: str) -> Dict[str, Any]:
    cfg = load_json(path)
    kind = sniff_kind(cfg)
    if kind == "fabric":
        return validate_deploy_config(cfg)
    return validate_databricks_config(cfg)


def format_plan(path: str, result: Dict[str, Any]) -> str:
    if result["kind"] == "fabric":
        feats = ", ".join(k for k, v in result["features"].items() if v) or "(none)"
        return (
            f"  [OK] {path}\n"
            f"        kind: Fabric/orchestration\n"
            f"        region: {result['region']}\n"
            f"        capacity path: {result['capacity']}\n"
            f"        workspace path: {result['workspace']}\n"
            f"        enabled features: {feats}"
        )
    return (
        f"  [OK] {path}\n"
        f"        kind: Databricks\n"
        f"        workspace path: {result['workspace']}\n"
        f"        sku: {result['sku']}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_validate(paths: List[str]) -> int:
    results = []
    for path in paths:
        try:
            result = validate_file(path)
        except ConfigError as exc:
            print(f"  [FAIL] {path}\n        {exc}", file=sys.stderr)
            return 2
        results.append((path, result))

    print("Config validation passed. Resolved fresh-vs-existing plan:")
    for path, result in results:
        print(format_plan(path, result))
    return 0


def _expect_pass(name: str, validator, cfg: Dict[str, Any]) -> bool:
    try:
        validator(cfg)
        print(f"  [PASS] {name}")
        return True
    except ConfigError as exc:
        print(f"  [UNEXPECTED FAIL] {name}: {exc}", file=sys.stderr)
        return False


def _expect_fail(name: str, validator, cfg: Dict[str, Any], must_mention: str) -> bool:
    try:
        validator(cfg)
        print(f"  [UNEXPECTED PASS] {name} (should have been rejected)", file=sys.stderr)
        return False
    except ConfigError as exc:
        if must_mention and must_mention not in str(exc):
            print(
                f"  [WRONG MESSAGE] {name}: expected to mention {must_mention!r}, got: {exc}",
                file=sys.stderr,
            )
            return False
        print(f"  [PASS-NEGATIVE] {name} -> {exc}")
        return True


def cmd_selftest() -> int:
    ok = True

    fresh_fabric = load_json("fabric/config/deploy_config.sample.json")
    fresh_dbx = load_json("databricks/config/databricks_config.sample.json")

    print("Positive cases:")
    ok &= _expect_pass("fabric sample (fresh capacity + fresh workspace)", validate_deploy_config, fresh_fabric)
    ok &= _expect_pass("databricks sample (fresh workspace)", validate_databricks_config, fresh_dbx)

    # Existing Fabric path
    existing_fabric = json.loads(json.dumps(fresh_fabric))
    existing_fabric["capacity"]["use_existing"] = True
    existing_fabric["capacity"]["existing_capacity_id"] = (
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
        "providers/Microsoft.Fabric/capacities/cap"
    )
    existing_fabric["workspace"]["use_existing"] = True
    existing_fabric["workspace"]["existing_workspace_id"] = "11111111-1111-1111-1111-111111111111"
    ok &= _expect_pass("fabric existing-resources path (capacity + workspace)", validate_deploy_config, existing_fabric)

    # Existing Databricks path
    existing_dbx = json.loads(json.dumps(fresh_dbx))
    existing_dbx["workspace"]["use_existing"] = True
    existing_dbx["workspace"]["host_url"] = "https://adb-1234567890.1.azuredatabricks.net"
    existing_dbx["workspace"]["resource_id"] = (
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
        "providers/Microsoft.Databricks/workspaces/ws"
    )
    ok &= _expect_pass("databricks existing-resources path", validate_databricks_config, existing_dbx)

    print("\nNegative cases:")
    # capacity.use_existing=true without existing_capacity_id
    neg1 = json.loads(json.dumps(fresh_fabric))
    neg1["capacity"]["use_existing"] = True
    neg1["capacity"].pop("existing_capacity_id", None)
    ok &= _expect_fail(
        "capacity.use_existing=true without existing_capacity_id",
        validate_deploy_config, neg1, "capacity.existing_capacity_id is required when capacity.use_existing=true",
    )

    # enable_operations_agent=true with enable_eventhouse=false
    neg2 = json.loads(json.dumps(fresh_fabric))
    neg2["features"]["enable_eventhouse"] = False
    neg2["features"]["enable_activator_email"] = False
    neg2["features"]["enable_operations_agent"] = True
    ok &= _expect_fail(
        "enable_operations_agent=true with enable_eventhouse=false",
        validate_deploy_config, neg2, "enable_eventhouse must be true when features.enable_operations_agent=true",
    )

    # region=eastus (rejected, must cite §1.7)
    neg3 = json.loads(json.dumps(fresh_fabric))
    neg3["region"] = "eastus"
    ok &= _expect_fail("region=eastus rejected", validate_deploy_config, neg3, "§1.7")

    # databricks sku not premium
    neg4 = json.loads(json.dumps(fresh_dbx))
    neg4["workspace"]["sku"] = "standard"
    ok &= _expect_fail("databricks sku=standard rejected", validate_databricks_config, neg4, "workspace.sku")

    # missing required field
    neg5 = json.loads(json.dumps(fresh_fabric))
    neg5.pop("region", None)
    ok &= _expect_fail("missing region", validate_deploy_config, neg5, "deploy_config.region is required")

    # data_seed wrong type
    neg6 = json.loads(json.dumps(fresh_dbx))
    neg6["data_seed"] = "42"
    ok &= _expect_fail("data_seed as string", validate_databricks_config, neg6, "data_seed must be an integer")

    # Fabric SKU below F64 while AI / Fabric IQ features are enabled
    neg7 = json.loads(json.dumps(fresh_fabric))
    neg7["capacity"]["sku"] = "F32"
    ok &= _expect_fail(
        "capacity.sku=F32 with AI features enabled",
        validate_deploy_config, neg7, "below the F64 minimum",
    )

    # Conditional field with WRONG NON-STRING type: numeric existing_capacity_id must raise
    # a clear ConfigError (naming the path) -- not an unhandled TypeError.
    neg8 = json.loads(json.dumps(fresh_fabric))
    neg8["capacity"]["use_existing"] = True
    neg8["capacity"]["existing_capacity_id"] = 123456
    ok &= _expect_fail(
        "capacity.existing_capacity_id as number (wrong type)",
        validate_deploy_config, neg8, "deploy_config.capacity.existing_capacity_id must be a string",
    )

    # Conditional GUID field with wrong non-string type
    neg9 = json.loads(json.dumps(fresh_fabric))
    neg9["workspace"]["use_existing"] = True
    neg9["workspace"]["existing_workspace_id"] = 42
    ok &= _expect_fail(
        "workspace.existing_workspace_id as number (wrong type)",
        validate_deploy_config, neg9, "deploy_config.workspace.existing_workspace_id must be a string",
    )

    # Conditional realtime field with wrong non-string type
    neg10 = json.loads(json.dumps(fresh_fabric))
    neg10["realtime"]["eventhouse_name"] = 99
    ok &= _expect_fail(
        "realtime.eventhouse_name as number (wrong type)",
        validate_deploy_config, neg10, "deploy_config.realtime.eventhouse_name must be a string",
    )

    # Databricks conditional host_url with wrong non-string type
    neg11 = json.loads(json.dumps(fresh_dbx))
    neg11["workspace"]["use_existing"] = True
    neg11["workspace"]["host_url"] = 8080
    neg11["workspace"]["resource_id"] = (
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
        "providers/Microsoft.Databricks/workspaces/ws"
    )
    ok &= _expect_fail(
        "databricks workspace.host_url as number (wrong type)",
        validate_databricks_config, neg11, "databricks_config.workspace.host_url must be a string",
    )

    # Databricks conditional resource_id with wrong non-string type
    neg12 = json.loads(json.dumps(fresh_dbx))
    neg12["workspace"]["use_existing"] = True
    neg12["workspace"]["host_url"] = "https://adb-1234567890.1.azuredatabricks.net"
    neg12["workspace"]["resource_id"] = 777
    ok &= _expect_fail(
        "databricks workspace.resource_id as number (wrong type)",
        validate_databricks_config, neg12, "databricks_config.workspace.resource_id must be a string",
    )

    print()
    if ok:
        print("Self-test: ALL CASES PASSED")
        return 0
    print("Self-test: FAILURES DETECTED", file=sys.stderr)
    return 1


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Zava demo config schema validator (fail-fast).")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--validate", nargs="+", metavar="FILE", help="validate one or more config files")
    group.add_argument("--selftest", action="store_true", help="run built-in positive/negative cases")
    args = parser.parse_args(argv)

    if args.validate:
        return cmd_validate(args.validate)
    # Default (no args) and --selftest both run the built-in self-test.
    return cmd_selftest()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
