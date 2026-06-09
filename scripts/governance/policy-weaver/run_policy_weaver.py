#!/usr/bin/env python3
"""Zava — Policy Weaver runner (Step 21, research R5).

Why
---
Microsoft Fabric **Mirroring** copies *data* from Azure Databricks Unity Catalog into
**OneLake**, but it does **not** copy *access policies*. Without a sync, the row filter and
column mask authored in Step 9 (``databricks/uc/05_access_policies.sql``) on the Step-8 gold
``zava`` catalog would simply not apply once business users read the mirrored data through
Fabric / Power BI Direct Lake — a governance gap.

``microsoft/Policy-Weaver`` (v0.4.0, MIT, **Beta** — an *accelerator provided "as-is" without
warranties*, **not** a supported Microsoft product) closes that gap: it reads Unity Catalog
grants / row filters / column masks via the Databricks SDK, resolves principals to Entra IDs
via Microsoft Graph, and writes Fabric **OneLake Security** data-access roles through the
Fabric ``dataAccessRoles`` REST API. This runner wraps Policy Weaver for the single ``zava``
source catalog (one catalog per run — R5 §1, Issue #20).

What it does
------------
1. Loads the committed template config
   (``scripts/governance/policy-weaver/policy_weaver_config.yaml``).
2. Resolves the ``${ENV_VAR}`` credential placeholders at runtime from the **environment** or,
   with ``--keyvault <name>``, from **Azure Key Vault** — so **no secrets are ever committed**.
3. Renders a **transient** config to a restricted-permission temp file and invokes Policy
   Weaver's documented entry point ``WeaverAgent.run(DatabricksSourceMap.from_yaml(path))``
   (R5 §4.2). The temp file is **deleted in a ``finally`` block**.
4. Is **idempotent** by construction: Policy Weaver matches roles by the configured suffix and
   issues a full-replacement ``PUT`` on ``dataAccessRoles`` (insert / update / delete) — so
   re-running converges to the same roles with no duplicates (R5 §6.3).
5. ``--dry-run`` validates + previews the resolved config (secrets **redacted**) and the
   planned source→target sync **without** writing the rendered file or calling any live API.

Beta / "accelerator, as-is" caveat (R5)
---------------------------------------
Policy Weaver is community-maintained by Microsoft engineers but is **Beta** and **unsupported**.
Some prerequisites are **manual** (OneLake Security toggle, Databricks Account-Admin SP + OAuth
secret, ``User.Read.All`` Graph consent, Entra SCIM/AIM identity sync) — see the manual-steps
appendix. Treat results as best-effort and review the created OneLake Security roles in the
Fabric UI before relying on them.

Security / authoring-phase notes
--------------------------------
* **No secrets.** Credentials are read at runtime from env / Key Vault and only ever written to
  a transient, 0600 temp file that is removed after the run. Logs **redact** secret values.
* **Authoring phase:** do **not** install Policy Weaver or run this against live Databricks /
  Fabric unless you intend to mutate OneLake Security roles. Use ``--dry-run`` to preview safely.

Usage
-----
    # Safe preview — validates config, resolves non-secret placeholders, prints the plan.
    # No file is written and no live API is called:
    python scripts/governance/policy-weaver/run_policy_weaver.py --dry-run

    # Real sync — secrets from environment variables (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET,
    # AZURE_TENANT_ID, FABRIC_WORKSPACE_ID, FABRIC_MIRROR_ID, DATABRICKS_WORKSPACE_URL,
    # DATABRICKS_ACCOUNT_ID, DATABRICKS_ACCOUNT_API_TOKEN):
    python scripts/governance/policy-weaver/run_policy_weaver.py

    # Real sync — secrets from Azure Key Vault (secret names == env var names, '_' -> '-'):
    python scripts/governance/policy-weaver/run_policy_weaver.py --keyvault zava-kv

    # Install the pinned Policy Weaver build first (author-phase opt-in), then sync:
    python scripts/governance/policy-weaver/run_policy_weaver.py --install
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pinned Policy Weaver build (R5: v0.4.0, released 2026-04-09, MIT, Beta).
POLICY_WEAVER_PACKAGE = "policy-weaver"
POLICY_WEAVER_VERSION = "0.4.0"
POLICY_WEAVER_SPEC = f"{POLICY_WEAVER_PACKAGE}=={POLICY_WEAVER_VERSION}"

# Committed template config (same directory as this script).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(_THIS_DIR, "policy_weaver_config.yaml")

# Policy Weaver's own logger name (R5 §4.2 — the tool logs under "POLICY_WEAVER").
PW_LOGGER_NAME = "POLICY_WEAVER"

# This runner's logger.
log = logging.getLogger("zava.policy_weaver")

# ``${VAR}`` placeholder used in the template config.
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

# Placeholders whose resolved values are SECRETS and must never be logged in clear text.
_SECRET_VARS = frozenset(
    {
        "AZURE_CLIENT_SECRET",
        "DATABRICKS_ACCOUNT_API_TOKEN",
    }
)

# Every env var the template may reference (used for dry-run reporting).
_KNOWN_VARS = (
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "FABRIC_WORKSPACE_ID",
    "FABRIC_MIRROR_ID",
    "DATABRICKS_WORKSPACE_URL",
    "DATABRICKS_ACCOUNT_ID",
    "DATABRICKS_ACCOUNT_API_TOKEN",
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging(level: int) -> None:
    """Configure this runner's logger and Policy Weaver's ``POLICY_WEAVER`` logger.

    Policy Weaver emits its progress under the ``POLICY_WEAVER`` logger (R5 §4.2); wiring a
    handler here surfaces that output alongside the runner's own messages.
    """
    fmt = logging.Formatter("%(levelname)s - %(asctime)s - %(name)s - %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(fmt)

    for logger in (log, logging.getLogger(PW_LOGGER_NAME)):
        logger.setLevel(level)
        # Avoid duplicate handlers if configure_logging is called more than once.
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            logger.addHandler(handler)


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------

def _keyvault_secret_name(var: str) -> str:
    """Map an env var name to a Key Vault secret name (KV allows only ``[0-9a-zA-Z-]``)."""
    return var.replace("_", "-").lower()


def _load_keyvault_secrets(vault_name: str, wanted: List[str]) -> Dict[str, str]:
    """Fetch the requested secrets from Azure Key Vault.

    Imports of ``azure-identity`` / ``azure-keyvault-secrets`` are deferred so this module
    parses and ``--dry-run`` works without those optional packages installed.
    """
    try:
        from azure.identity import DefaultAzureCredential  # noqa: WPS433 (lazy import)
        from azure.keyvault.secrets import SecretClient  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - depends on optional packages
        raise RuntimeError(
            "Key Vault mode requires 'azure-identity' and 'azure-keyvault-secrets'. "
            "Install them (pip install azure-identity azure-keyvault-secrets) or use "
            "environment variables instead."
        ) from exc

    vault_url = f"https://{vault_name}.vault.azure.net"
    log.info("Resolving %d secret(s) from Key Vault '%s'", len(wanted), vault_name)
    client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())

    resolved: Dict[str, str] = {}
    for var in wanted:
        secret_name = _keyvault_secret_name(var)
        try:
            resolved[var] = client.get_secret(secret_name).value
        except Exception as exc:  # pragma: no cover - live service
            raise RuntimeError(
                f"Failed to read secret '{secret_name}' (for {var}) from Key Vault "
                f"'{vault_name}': {exc}"
            ) from exc
    return resolved


def resolve_values(
    referenced: List[str],
    keyvault: str | None,
    require: bool,
) -> Tuple[Dict[str, str], List[str]]:
    """Resolve every referenced ``${VAR}`` from env (and Key Vault, if requested).

    Returns ``(values, missing)``. When ``require`` is True a non-empty ``missing`` list is a
    fatal error (caller raises); when False (dry-run) missing vars are reported, not fatal.
    """
    values: Dict[str, str] = {}

    # Environment first — explicit and always available.
    for var in referenced:
        env_val = os.environ.get(var)
        if env_val is not None and env_val != "":
            values[var] = env_val

    # Fill the rest from Key Vault, if a vault was provided.
    still_missing = [v for v in referenced if v not in values]
    if keyvault and still_missing:
        values.update(_load_keyvault_secrets(keyvault, still_missing))

    missing = [v for v in referenced if v not in values]
    if missing and require:
        raise RuntimeError(
            "Missing required credential value(s): "
            + ", ".join(missing)
            + ". Set them as environment variables"
            + (" or in the Key Vault" if keyvault else " (or pass --keyvault <name>)")
            + "."
        )
    return values, missing


# ---------------------------------------------------------------------------
# Config rendering
# ---------------------------------------------------------------------------

def read_template(path: str) -> str:
    """Read the committed template config text."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Policy Weaver config not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def referenced_vars(template_text: str) -> List[str]:
    """Return the unique ``${VAR}`` names referenced by the template, in first-seen order."""
    seen: List[str] = []
    for match in _PLACEHOLDER_RE.finditer(template_text):
        var = match.group(1)
        if var not in seen:
            seen.append(var)
    return seen


def render(template_text: str, values: Dict[str, str]) -> str:
    """Substitute ``${VAR}`` placeholders with resolved values; unknown vars are left intact."""

    def _sub(match: "re.Match[str]") -> str:
        var = match.group(1)
        return values.get(var, match.group(0))

    return _PLACEHOLDER_RE.sub(_sub, template_text)


def redact(template_text: str, values: Dict[str, str]) -> str:
    """Render for DISPLAY: secret vars are masked; non-secret resolved vars are substituted."""
    safe_values = {
        var: ("***REDACTED***" if var in _SECRET_VARS else val)
        for var, val in values.items()
    }
    return render(template_text, safe_values)


def validate_yaml(text: str) -> dict | None:
    """Best-effort structural validation of the rendered config.

    Returns the parsed mapping if PyYAML is available, else ``None`` (the runner proceeds; the
    real parse happens inside Policy Weaver via ``DatabricksSourceMap.from_yaml``).
    """
    try:
        import yaml  # noqa: WPS433 (lazy import — optional for validation)
    except ImportError:
        log.warning("PyYAML not installed; skipping local YAML validation.")
        return None

    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError("Rendered config did not parse to a YAML mapping.")

    # Spot-check the keys Policy Weaver 0.4.0 requires (R5 §4.1 / §5.1).
    for key in ("keyvault", "fabric", "constraints", "service_principal", "source", "type", "databricks"):
        if key not in parsed:
            raise ValueError(f"Rendered config is missing required top-level key: '{key}'")
    if parsed.get("type") != "UNITY_CATALOG":
        raise ValueError("Config 'type' must be 'UNITY_CATALOG' for the Databricks source.")
    if parsed["fabric"].get("policy_mapping") != "role_based":
        # Not fatal, but CLS/RLS sync silently no-ops under table_based (R5 §2.5).
        log.warning(
            "fabric.policy_mapping is not 'role_based'; column/row-level security will NOT sync."
        )
    return parsed


# ---------------------------------------------------------------------------
# Install / invoke Policy Weaver
# ---------------------------------------------------------------------------

def installed_version() -> str | None:
    """Return the installed ``policy-weaver`` distribution version, or ``None`` if absent.

    Uses ``importlib.metadata`` (stdlib) so no third-party dependency is needed just to read
    the version. The distribution name is ``policy-weaver`` even though the import package is
    ``policyweaver``.
    """
    from importlib import metadata  # noqa: WPS433 (local import keeps module import cheap)

    try:
        return metadata.version(POLICY_WEAVER_PACKAGE)
    except metadata.PackageNotFoundError:
        return None


def ensure_installed(do_install: bool) -> None:
    """Enforce the pinned Policy Weaver build; optionally pip-install it first.

    Step 21 / R5 target Policy Weaver **v0.4.0** specifically: the committed config uses the
    0.4.0 top-level schema keys. Running a different (e.g. 0.3.x or a future) build risks a
    config-schema mismatch, so this fails fast unless the installed distribution version equals
    ``POLICY_WEAVER_VERSION``.
    """
    if do_install:
        # Install EXACTLY the pinned spec (==0.4.0), so a previously-installed 0.3.x/other
        # version is replaced rather than left in place.
        log.info("Installing %s ...", POLICY_WEAVER_SPEC)
        subprocess.check_call([sys.executable, "-m", "pip", "install", POLICY_WEAVER_SPEC])

    # Confirm the import package is present (catches a broken/partial install).
    try:
        import policyweaver  # noqa: F401, WPS433 (probe import)
    except ImportError as exc:
        raise RuntimeError(
            f"Policy Weaver is not installed. Run with --install, or "
            f"`pip install {POLICY_WEAVER_SPEC}` first."
        ) from exc

    # Enforce the pinned version regardless of how it was installed.
    found = installed_version()
    if found is None:
        raise RuntimeError(
            f"Policy Weaver import succeeded but its distribution version could not be "
            f"determined. Reinstall the pinned build: `pip install {POLICY_WEAVER_SPEC}`."
        )
    if found != POLICY_WEAVER_VERSION:
        raise RuntimeError(
            f"Policy Weaver version mismatch: found {POLICY_WEAVER_PACKAGE}=={found}, but this "
            f"runner requires =={POLICY_WEAVER_VERSION} (the committed config uses the "
            f"{POLICY_WEAVER_VERSION} schema). Pin it with "
            f"`pip install {POLICY_WEAVER_SPEC}` (or re-run with --install)."
        )
    log.info("Verified %s is installed.", POLICY_WEAVER_SPEC)


def run_weaver(config_path: str) -> None:
    """Invoke Policy Weaver's documented entry point against the rendered config (R5 §4.2).

    ``WeaverAgent.run`` is async; we drive it with ``asyncio.run`` exactly as the upstream
    ``databricks_test.py`` example does.
    """
    import asyncio  # noqa: WPS433 (local import keeps module import cheap)

    from policyweaver.weaver import WeaverAgent  # noqa: WPS433
    from policyweaver.plugins.databricks.model import DatabricksSourceMap  # noqa: WPS433

    config = DatabricksSourceMap.from_yaml(config_path)
    log.info("Running Policy Weaver sync (one source catalog: 'zava') ...")
    asyncio.run(WeaverAgent.run(config))
    log.info("Policy Weaver sync complete. Review the OneLake Security roles in Fabric.")


def write_transient_config(text: str) -> str:
    """Write the rendered config to a 0600 temp file and return its path.

    The file holds resolved secrets, so it is created with owner-only permissions and MUST be
    removed by the caller in a ``finally`` block.
    """
    fd, path = tempfile.mkstemp(prefix="policy_weaver_", suffix=".yaml")
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 (best-effort on Windows)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run microsoft/Policy-Weaver to sync Databricks Unity Catalog access policies "
            "(Step 9, single 'zava' catalog) into Fabric OneLake Security roles (Step 21, R5)."
        )
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="Path to the Policy Weaver template config (default: %(default)s).",
    )
    parser.add_argument(
        "--keyvault",
        default=os.environ.get("ZAVA_KEYVAULT_NAME"),
        metavar="NAME",
        help="Azure Key Vault name to resolve secret placeholders from (default: env "
        "ZAVA_KEYVAULT_NAME, else environment variables only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + preview the resolved config and plan WITHOUT writing the rendered "
        "file or calling any live API.",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help=f"pip install {POLICY_WEAVER_SPEC} before running (author-phase opt-in).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: %(default)s).",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(getattr(logging, args.log_level))

    log.info(
        "Policy Weaver runner — package %s (Beta, MIT; 'accelerator, as-is' — review results).",
        POLICY_WEAVER_SPEC,
    )

    try:
        template_text = read_template(args.config)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2

    refs = referenced_vars(template_text)
    log.info("Config references %d credential placeholder(s): %s", len(refs), ", ".join(refs))

    # Resolve placeholders. In dry-run, missing values are reported but not fatal.
    try:
        values, missing = resolve_values(refs, args.keyvault, require=not args.dry_run)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 3

    # Validate the (best-effort) rendered YAML structurally before doing anything live.
    rendered = render(template_text, values)
    try:
        validate_yaml(rendered)
    except Exception as exc:  # noqa: BLE001 - surface any parse/validation error
        log.error("Config validation failed: %s", exc)
        return 4

    if args.dry_run:
        log.info("DRY-RUN — no rendered file written, no live API called.")
        if missing:
            log.warning(
                "Unresolved placeholder(s) (set before a real run): %s", ", ".join(missing)
            )
        log.info("Plan: sync ONE source catalog 'zava' (schemas: curated, gold) -> Fabric "
                 "OneLake Security data-access roles (suffix 'PWPolicy', role_based, fallback=deny).")
        log.info("Resolved config preview (secrets redacted):\n%s", redact(template_text, values))
        return 0

    # Real run — ensure the tool is present, render to a transient file, sync, then clean up.
    try:
        ensure_installed(args.install)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        log.error("%s", exc)
        return 5

    config_path = write_transient_config(rendered)
    log.info("Wrote transient rendered config (0600) to %s", config_path)
    try:
        run_weaver(config_path)
    except Exception as exc:  # noqa: BLE001 - report the primary failure, then clean up
        log.error("Policy Weaver run failed: %s", exc)
        return 6
    finally:
        try:
            os.remove(config_path)
            log.info("Removed transient rendered config.")
        except OSError as exc:  # pragma: no cover
            log.warning("Could not remove transient config %s: %s", config_path, exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
