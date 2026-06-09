#!/usr/bin/env python3
"""End-to-end deployment orchestrator for the Zava Databricks + Fabric demo (plan Step 23).

This is the single command that stands the whole demo up — in dependency / **wave** order —
from a validated ``deploy_config.json``. It is **idempotent** (every underlying step script
is find-or-create / overwrite-safe), honours **fresh-vs-existing** flags, gates optional
phases behind **feature flags**, and **pauses** at the documented user-token / UI-only steps
with clear prompts. Long-running Fabric REST calls and the ~15-min mirror metadata sync are
wrapped with retry/backoff (delegated to the step scripts, which own their own auth + LRO
polling); this orchestrator adds a thin retry around the subprocess invocation itself.

What it orchestrates (waves)
----------------------------
  0. **Preflight + config validation** — ``scripts/preflight_checks.py`` + ``config_schema``.
  1. **Azure (Bicep)** — ``az deployment group create`` of ``infra/main.bicep`` (fresh or
     existing capacity / Databricks workspace via the chosen ``*.bicepparam``).
  2. **Databricks** — Asset Bundle validate/deploy + UC setup + medallion (gold certified
     asset, Variation-1 source) + Lakeflow SDP (Variation-2 source).
  3. **Fabric workspace** — capacity bind + workspace + **Workspace Identity** (Step 10).
  4. **Ingestion** — Variation 1 *Mirrored Azure Databricks Catalog* **and** Variation 2
     *secured OneLake shortcut* + ADLS network hardening (the ``ingestion.variation``
     2A/2B sub-pattern selects the shortcut source).
  5. **Thin gold** — the Fabric consumption-layer aggregation notebook (Step 13).
  6. **Semantic model** — Direct Lake model via ``semantic-link-labs`` (Step 14).
  7. **Report** — PBIP/PBIR deploy + rebind (Step 15).
  8. **Ontology / Graph** — preview ontology + auto Graph (Step 16) [feature-gated].
  9. **Data Agent** — NL insights (Step 17) [feature-gated].
 10. **Real-Time Intelligence** — Eventhouse / KQL DB+table / Eventstream (Step 18) [gated].
 11. **Activator email** — default Teams-free watch+act (Step 19) [gated].
 12. **Operations Agent** — optional Teams watch+act (Step 20) [gated].
 13. **Policy Weaver** — UC access -> OneLake security (Step 21) [gated].
 14. **Purview** — catalog / lineage / labels / DLP (Step 22).

Manual PAUSE points (plan Step 23 manual note)
----------------------------------------------
The orchestrator pauses (or, with ``--dry-run`` / ``--non-interactive``, just *prints*) at
each documented user-token / UI step: the **mirroring OAuth** consent, **Workspace
Identity** UI fallback, **generate-ontology-from-semantic-model**, the **Eventstream
connection** wiring, the **Activator rule design-mode** validation, and the optional
**Operations Agent Teams** wiring.

Safety
------
* ``--dry-run`` performs **no** authentication and **no** mutation — it prints the full
  ordered plan, every exact command, and every pause prompt, then exits 0. This is the
  primary authoring-time verification path.
* Step scripts are invoked as subprocesses (the same way an operator would run them), with
  ``--dry-run`` propagated, so this orchestrator never duplicates their auth/REST logic.
* No secrets: only config names / ids / placeholders flow through here; auth is acquired by
  each step at runtime (``az login`` / ``DefaultAzureCredential``).

Usage
-----
    # Preview the full ordered plan (no auth, no changes) — safe authoring check:
    python scripts/deploy.py --dry-run

    # Deploy everything from a real local config, pausing at manual steps:
    python scripts/deploy.py --config fabric/config/deploy_config.json \
        --databricks-config databricks/config/databricks_config.json

    # Resume from a wave (e.g. after fixing a manual step), skipping preflight:
    python scripts/deploy.py --start-at semantic_model --skip-preflight

    # Non-interactive (CI): auto-acknowledge manual pauses (they are still logged):
    python scripts/deploy.py --non-interactive
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional

# config_schema lives next to this file.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import config_schema  # noqa: E402

# ---------------------------------------------------------------------------
# Constants / paths
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

# Step-script locations (already implemented by Steps 5–22).
PREFLIGHT = os.path.join("scripts", "preflight_checks.py")
FABRIC_SCRIPTS = os.path.join("fabric", "scripts")
GOV_POLICY_WEAVER = os.path.join("scripts", "governance", "policy-weaver", "run_policy_weaver.py")
GOV_PURVIEW = os.path.join("scripts", "governance", "purview", "setup_purview_scans.py")

INFRA_TEMPLATE = os.path.join("infra", "main.bicep")
INFRA_PARAMS_FRESH = os.path.join("infra", "params", "dev.bicepparam")
INFRA_PARAMS_EXISTING = os.path.join("infra", "params", "existing-resources.bicepparam")
DATABRICKS_BUNDLE_DIR = os.path.join("databricks", "bundle")

# Databricks Asset Bundle resource names (databricks/bundle/databricks.yml).
DBX_JOB_UC_SETUP = "zava_uc_setup"
DBX_JOB_MEDALLION = "zava_medallion_pipeline"
DBX_JOB_CERTIFY = "zava_certify_gold"
DBX_PIPELINE_LAKEFLOW = "zava_lakeflow_curated"
DBX_JOB_ACCESS_POLICIES = "zava_access_policies"

# Databricks Asset Bundle definition (source of the per-target catalog name).
DATABRICKS_YML = os.path.join("databricks", "bundle", "databricks.yml")

# Thin retry around the *subprocess* (each step owns its own REST retry/backoff + LRO poll).
SUBPROCESS_MAX_ATTEMPTS = 3
SUBPROCESS_BACKOFF_SECONDS = 10

# Indicative wall-clock for the mirror metadata sync (R1 ~15 min) — surfaced in the plan.
MIRROR_SYNC_NOTE_MINUTES = 15

LOG = logging.getLogger("zava.deploy")


# ---------------------------------------------------------------------------
# Step kinds
# ---------------------------------------------------------------------------

KIND_INTERNAL = "internal"       # runs inside this process (e.g. config validation)
KIND_AZURE = "azure"             # az deployment group create
KIND_DATABRICKS = "databricks"   # databricks CLI / bundle
KIND_FABRIC = "fabric"           # a fabric/scripts/*.py step (supports --dry-run)
KIND_NOTEBOOK = "notebook"       # a Fabric notebook to run (fabric-cli / REST job)
KIND_GOVERNANCE = "governance"   # policy-weaver / purview step (supports --dry-run)
KIND_PAUSE = "pause"             # manual user-token / UI-only step — prompt the operator


class Wave:
    """One ordered unit of work in the deployment plan."""

    def __init__(
        self,
        key: str,
        title: str,
        kind: str,
        *,
        command: Optional[List[str]] = None,
        gate: Optional[Callable[[dict], bool]] = None,
        propagate_dry_run: bool = True,
        retry: bool = False,
        pause_prompt: str = "",
        note: str = "",
        run: Optional[Callable[["Deployer"], int]] = None,
    ) -> None:
        self.key = key
        self.title = title
        self.kind = kind
        self.command = command or []
        self.gate = gate
        self.propagate_dry_run = propagate_dry_run
        self.retry = retry
        self.pause_prompt = pause_prompt
        self.note = note
        self.run = run  # optional custom in-process callable (KIND_INTERNAL)

    def enabled(self, resolved: dict) -> bool:
        return self.gate(resolved) if self.gate else True


# ---------------------------------------------------------------------------
# Deployer
# ---------------------------------------------------------------------------

class Deployer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.dry_run: bool = args.dry_run
        self.non_interactive: bool = args.non_interactive or args.dry_run
        self.deploy_config_path = _resolve_config_path(
            args.config, DEFAULT_DEPLOY_CONFIG_CANDIDATES
        )
        self.databricks_config_path = _resolve_config_path(
            args.databricks_config, DEFAULT_DATABRICKS_CONFIG_CANDIDATES
        )
        self.raw: dict = {}
        self.resolved: dict = {}
        self.features: dict = {}
        self.ingestion_variation: str = "2A"

    # -- config -----------------------------------------------------------

    def load_and_validate_config(self) -> None:
        if not self.deploy_config_path:
            raise DeployError(
                "no Fabric/orchestration config found. Pass --config or create "
                "fabric/config/deploy_config.json from the committed sample."
            )
        LOG.info("Validating config: %s", _rel(self.deploy_config_path))
        # config_schema is the authoritative contract shared by every entry point.
        self.resolved = config_schema.validate_file(self.deploy_config_path)
        if self.resolved.get("kind") != "fabric":
            raise DeployError(
                f"{_rel(self.deploy_config_path)} is not a Fabric/orchestration config."
            )
        with open(self.deploy_config_path, "r", encoding="utf-8") as fh:
            self.raw = json.load(fh)
        self.features = self.raw.get("features", {}) or {}
        self.ingestion_variation = (self.raw.get("ingestion", {}) or {}).get("variation", "2A")

        # Validate the Databricks config too (it gates the Databricks wave).
        if self.databricks_config_path:
            LOG.info("Validating Databricks config: %s", _rel(self.databricks_config_path))
            config_schema.validate_file(self.databricks_config_path)
        else:
            LOG.warning(
                "No Databricks config found — the Databricks wave will use bundle defaults."
            )

        LOG.info(
            "Resolved plan: region=%s | capacity=%s | fabric-workspace=%s | "
            "databricks-workspace=%s | ingestion=%s | features=%s",
            self.resolved.get("region"),
            self.resolved.get("capacity"),
            self.resolved.get("workspace"),
            self._databricks_workspace_path(),
            self.ingestion_variation,
            ",".join(k for k, v in self.resolved.get("features", {}).items() if v) or "(none)",
        )

        self._check_catalog_coherence()

    def _check_catalog_coherence(self) -> None:
        """Fail fast unless the selected DAB target catalog == source.databricks_catalog.

        Single-source-of-truth (Step 28): whatever the Databricks Asset Bundle builds for the
        selected ``--databricks-target`` is exactly what the Fabric mirror/shortcut/governance
        read. A divergence (e.g. DAB builds ``zava_dev`` while Fabric reads ``zava``) silently
        points the mirror at a nonexistent catalog, so we reject it here.
        """
        target = self.args.databricks_target
        source_catalog = ((self.raw.get("source", {}) or {}).get("databricks_catalog"))
        dab_catalog = dab_target_catalog(os.path.join(_REPO_ROOT, DATABRICKS_YML), target)
        if not dab_catalog:
            LOG.warning(
                "Could not resolve the catalog for DAB target %r from %s; skipping catalog "
                "coherence check.", target, DATABRICKS_YML,
            )
            return
        LOG.info(
            "Catalog coherence: DAB target %r builds catalog %r; Fabric source.databricks_catalog=%r.",
            target, dab_catalog, source_catalog,
        )
        if (
            source_catalog
            and not config_schema.is_placeholder(source_catalog)
            and source_catalog != dab_catalog
        ):
            raise DeployError(
                f"catalog incoherence: Databricks Asset Bundle target {target!r} builds catalog "
                f"{dab_catalog!r} but deploy_config.source.databricks_catalog={source_catalog!r}. "
                f"A default deploy must use ONE catalog name end-to-end. Align them: set "
                f"source.databricks_catalog={dab_catalog!r}, or choose a --databricks-target whose "
                f"catalog matches, or edit databricks/bundle/databricks.yml."
            )

    def _databricks_workspace_path(self) -> str:
        if not self.databricks_config_path:
            return "(default)"
        try:
            with open(self.databricks_config_path, "r", encoding="utf-8") as fh:
                dbx = json.load(fh)
            return "existing" if (dbx.get("workspace", {}) or {}).get("use_existing") else "fresh"
        except (OSError, json.JSONDecodeError):
            return "(unknown)"

    # -- feature gates ----------------------------------------------------

    def _feat(self, name: str) -> bool:
        return bool(self.features.get(name, False))

    # -- the wave plan ----------------------------------------------------

    def build_plan(self) -> List[Wave]:
        cfg = ["--config", self.deploy_config_path] if self.deploy_config_path else []

        def fab(script: str, *extra: str) -> List[str]:
            return [sys.executable, os.path.join(FABRIC_SCRIPTS, script), *cfg, *extra]

        waves: List[Wave] = []

        # Wave 0 — preflight (skippable with --skip-preflight).
        if not self.args.skip_preflight:
            pf = [sys.executable, PREFLIGHT]
            if self.deploy_config_path:
                pf += ["--config", self.deploy_config_path]
            if self.databricks_config_path:
                pf += ["--databricks-config", self.databricks_config_path]
            waves.append(Wave(
                "preflight", "Preflight environment + config checks", KIND_FABRIC,
                command=pf, propagate_dry_run=False,
                note="Read-only probes; reports gaps. Use --skip-preflight to bypass.",
            ))

        # Wave 1 — Azure (Bicep).
        if not self.args.skip_azure:
            waves.append(Wave(
                "azure", "Provision Azure resources (Bicep)", KIND_AZURE,
                command=self._bicep_command(apply_hardening=False),
                note=f"Fresh-vs-existing via {_rel(self._bicep_params())}.",
            ))

        # Wave 2 — Databricks (bundle + UC + medallion + Lakeflow).
        if not self.args.skip_databricks:
            target = self.args.databricks_target
            waves.append(Wave(
                "dbx_validate", "Databricks Asset Bundle: validate", KIND_DATABRICKS,
                command=["databricks", "bundle", "validate", "-t", target],
            ))
            waves.append(Wave(
                "dbx_deploy", "Databricks Asset Bundle: deploy (jobs + Lakeflow pipeline)",
                KIND_DATABRICKS, command=["databricks", "bundle", "deploy", "-t", target],
            ))
            waves.append(Wave(
                "dbx_uc_setup", "Databricks: Unity Catalog setup + grants (run once)",
                KIND_DATABRICKS,
                command=["databricks", "bundle", "run", DBX_JOB_UC_SETUP, "-t", target],
            ))
            waves.append(Wave(
                "dbx_medallion", "Databricks: medallion pipeline -> certified GOLD (Variation 1 source)",
                KIND_DATABRICKS,
                command=["databricks", "bundle", "run", DBX_JOB_MEDALLION, "-t", target],
            ))
            waves.append(Wave(
                "dbx_certify", "Databricks: certify GOLD asset (tags/comments/owner)",
                KIND_DATABRICKS,
                command=["databricks", "bundle", "run", DBX_JOB_CERTIFY, "-t", target],
            ))
            # Lakeflow SDP is the Variation-2 (shortcut) source; deploy+run it for the
            # shortcut ingestion path (2A/2B both consume Lakeflow curated output).
            waves.append(Wave(
                "dbx_lakeflow", "Databricks: Lakeflow SDP curated pipeline (Variation 2 source)",
                KIND_DATABRICKS,
                command=["databricks", "bundle", "run", DBX_PIPELINE_LAKEFLOW, "-t", target],
            ))
            # UC access policies (row filter + column mask) — runs uc/05_access_policies.sql so
            # Policy Weaver (governance wave, far below) syncs REAL UC policies. Sequenced AFTER
            # Lakeflow (curated.rentals_curated must exist) and BEFORE the governance wave (Step 28).
            waves.append(Wave(
                "dbx_access_policies",
                "Databricks: apply UC access policies (row filter + column mask; Step 28)",
                KIND_DATABRICKS,
                command=["databricks", "bundle", "run", DBX_JOB_ACCESS_POLICIES, "-t", target],
                note="Runs uc/05_access_policies.sql so Policy Weaver later syncs real UC "
                     "row-filters/column-masks; after Lakeflow, before the governance wave.",
            ))

        # Wave 3 — Fabric workspace + capacity bind + Workspace Identity.
        waves.append(Wave(
            "fabric_workspace", "Fabric: workspace + capacity bind + Workspace Identity (Step 10)",
            KIND_FABRIC, command=fab("00_create_workspace.py", "--write-config"), retry=True,
            note="Fresh path creates the Workspace Identity (long-running REST). --write-config "
                 "persists workspace.workspace_id + workspace.identity_object_id for the hardening wave.",
        ))
        # PAUSE: Workspace Identity UI fallback (R7/R10 — provisionIdentity can be UI-only).
        waves.append(Wave(
            "pause_workspace_identity", "MANUAL: confirm Workspace Identity exists", KIND_PAUSE,
            pause_prompt=(
                "If 00_create_workspace.py could not provision the Workspace Identity via REST, "
                "create it in the UI (Workspace settings -> Workspace identity -> + Workspace "
                "identity) and set workspace.identity_object_id (and workspace.workspace_id, the "
                "workspace GUID) in deploy_config.json. See docs/manual-steps.md (Step 10)."
            ),
        ))

        # Wave 4 — Ingestion. Variation 1 (mirror) requires the one-time OAuth consent first.
        waves.append(Wave(
            "pause_mirror_oauth", "MANUAL: Databricks workspace connection + OAuth consent", KIND_PAUSE,
            pause_prompt=(
                "Create the Fabric connection to the Azure Databricks workspace once in the "
                "portal (one-time OAuth consent), then set mirroring.databricks_connection_id "
                "in deploy_config.json before the mirror step. See docs/manual-steps.md (Step 11)."
            ),
        ))
        waves.append(Wave(
            "mirror", "Fabric ingestion — Variation 1: Mirrored Azure Databricks Catalog (Step 11)",
            KIND_FABRIC, command=fab("10_create_mirrored_catalog.py"), retry=True,
            note=f"Metadata auto-sync is ~{MIRROR_SYNC_NOTE_MINUTES} min (R1); the step waits/polls.",
        ))
        waves.append(Wave(
            "shortcut", "Fabric ingestion — Variation 2: secured OneLake shortcut (Step 12)",
            KIND_FABRIC,
            command=fab("20_create_shortcut.py"),
            retry=True,
            note=f"ADLS source per ingestion.variation={self.ingestion_variation} (2A managed / 2B external).",
        ))
        # Apply the ADLS network hardening AFTER the shortcut + Workspace Identity exist
        # (trusted-workspace access) — a second Bicep pass with applyNetworkHardening=true.
        # Step 28: pass the Step-10 workspace GUID + Workspace Identity object id so the firewall
        # actually locks to default-deny + trusted-workspace rule (otherwise it stays Allow).
        if not self.args.skip_azure:
            if self._databricks_workspace_path() == "existing":
                # Important #5 — secured V2 hardening is fresh-Databricks only: the BYO ADLS
                # account is not provisioned by this template, so the hardening module is
                # intentionally skipped (main.bicep gates on !useExistingDatabricks). Make the
                # skip EXPLICIT and logged, with a pointer to the BYO procedure — never silent.
                waves.append(Wave(
                    "hardening_skipped",
                    "ADLS hardening SKIPPED — secured V2 hardening is fresh-Databricks only (BYO)",
                    KIND_PAUSE,
                    pause_prompt=(
                        "Existing-Databricks (BYO) path: the secured Variation-2 ADLS hardening "
                        "(firewall default-deny + Fabric trusted-workspace rule + Workspace-Identity "
                        "RBAC) is NOT applied by this IaC because it does not own your ADLS account. "
                        "Apply it manually on your storage account — see docs/manual-steps.md "
                        "(BYO Databricks: secure the Variation-2 shortcut storage)."
                    ),
                ))
            else:
                waves.append(Wave(
                    "hardening", "Apply ADLS Gen2 network hardening (Variation 2; Step 12)",
                    KIND_AZURE, command=self._bicep_command(apply_hardening=True),
                    note="Second Bicep pass: applyNetworkHardening=true + fabricWorkspaceId + "
                         "workspaceIdentityObjectId (trusted-workspace access). Fails fast if the "
                         "Step-10 workspace GUID / identity object id are missing.",
                ))

        # Wave 5 — Thin Fabric gold (consumption-layer aggregation notebook).
        # Step 29: 40_build_thin_gold.py is deployed as a Fabric **Notebook item** and run by
        # name with parameters (lakehouse / catalog / gold + thin-gold schema) resolved from the
        # Step-28 coherent config — not invoked as a bare local source path. The script's
        # `--deploy-and-run` driver emits `fab import` (create/update the item) + `fab job
        # run-sync` (run the item with `-P` params); `--dry-run` prints those without executing.
        waves.append(Wave(
            "thin_gold", "Fabric: thin gold / aggregation layer (Step 13)", KIND_FABRIC,
            command=fab("40_build_thin_gold.py", "--deploy-and-run",
                        "--notebook-name", "40_build_thin_gold"),
            retry=True,
            note="Deploys 40_build_thin_gold.py as a Fabric Notebook ITEM, then runs that item by "
                 "name with parameters (lakehouse/catalog/gold+thin-gold schema). Offline --dry-run "
                 "prints the fabric-cli import + job-run commands without executing.",
        ))

        # Wave 6 — Direct Lake semantic model.
        waves.append(Wave(
            "semantic_model", "Fabric: Direct Lake semantic model (Step 14)", KIND_FABRIC,
            command=fab("30_create_semantic_model.py"), retry=True,
        ))

        # Wave 7 — Report.
        waves.append(Wave(
            "report", "Fabric: Power BI report deploy + rebind (Step 15)", KIND_FABRIC,
            command=fab("50_deploy_report.py"), retry=True,
        ))

        # Wave 8 — Ontology / Graph (feature-gated).
        waves.append(Wave(
            "pause_ontology", "MANUAL: generate ontology from the semantic model", KIND_PAUSE,
            gate=lambda r: self._feat("enable_ontology"),
            pause_prompt=(
                "The ontology is generated from the Step-14 semantic model in the Fabric UI "
                "(Fabric IQ -> create ontology -> from semantic model) before the graph "
                "auto-builds. See docs/manual-steps.md (Step 16)."
            ),
        ))
        waves.append(Wave(
            "ontology", "Fabric: ontology (preview) + auto Graph (Step 16)", KIND_FABRIC,
            command=fab("60_create_ontology.py"), retry=True,
            gate=lambda r: self._feat("enable_ontology"),
        ))

        # Wave 9 — Data Agent (feature-gated).
        waves.append(Wave(
            "data_agent", "Fabric: Data Agent for NL insights (Step 17)", KIND_FABRIC,
            command=fab("70_create_data_agent.py"), retry=True,
            gate=lambda r: self._feat("enable_data_agent"),
        ))

        # Wave 10 — Real-Time Intelligence (Eventhouse/Eventstream) — gated.
        waves.append(Wave(
            "eventhouse", "Fabric RTI: Eventhouse + KQL DB/table + Eventstream (Step 18)",
            KIND_FABRIC, command=fab("75_create_eventhouse.py"), retry=True,
            gate=lambda r: self._feat("enable_eventhouse"),
        ))
        waves.append(Wave(
            "pause_eventstream", "MANUAL: wire the Eventstream source connection", KIND_PAUSE,
            gate=lambda r: self._feat("enable_eventhouse"),
            pause_prompt=(
                "Open the Eventstream, copy the custom endpoint's event-hub-compatible "
                "connection string (a runtime secret — never commit it), and point the "
                "telematics replayer at it. See docs/manual-steps.md (Step 18)."
            ),
        ))

        # Wave 11 — Activator email (default Teams-free watch+act) — gated.
        waves.append(Wave(
            "activator", "Fabric: Activator native Email alert (default; Step 19)", KIND_FABRIC,
            command=fab("78_create_activator_email.py"), retry=True,
            gate=lambda r: self._feat("enable_activator_email"),
        ))
        waves.append(Wave(
            "pause_activator", "MANUAL: validate the Activator rule in design mode", KIND_PAUSE,
            gate=lambda r: self._feat("enable_activator_email"),
            pause_prompt=(
                "Open the Activator (Reflex) in design mode and confirm the rule condition + "
                "Email action fire as expected against live Eventhouse data. "
                "See docs/manual-steps.md (Step 19)."
            ),
        ))

        # Wave 12 — Operations Agent (optional Teams watch+act) — gated.
        waves.append(Wave(
            "operations_agent", "Fabric: Operations Agent (optional, Teams; Step 20)", KIND_FABRIC,
            command=fab("80_create_operations_agent.py"), retry=True,
            gate=lambda r: self._feat("enable_operations_agent"),
        ))
        waves.append(Wave(
            "pause_ops_agent", "MANUAL: wire the Operations Agent to Teams", KIND_PAUSE,
            gate=lambda r: self._feat("enable_operations_agent"),
            pause_prompt=(
                "Connect the Operations Agent to the Microsoft Teams channel for the Yes/No "
                "approval card and confirm the recipient UPN. Requires a Teams account "
                "(R11 §6, §8). See docs/manual-steps.md (Step 20)."
            ),
        ))

        # Wave 13 — Policy Weaver (gated on governance.policy_weaver_enabled).
        waves.append(Wave(
            "policy_weaver", "Governance: Policy Weaver (UC access -> OneLake security; Step 21)",
            KIND_GOVERNANCE, command=[sys.executable, GOV_POLICY_WEAVER],
            gate=lambda r: bool((self.raw.get("governance", {}) or {}).get("policy_weaver_enabled")),
            retry=True,
        ))

        # Wave 14 — Purview (gated on governance.purview_account present).
        waves.append(Wave(
            "purview", "Governance: Microsoft Purview catalog/lineage/labels/DLP (Step 22)",
            KIND_GOVERNANCE, command=[sys.executable, GOV_PURVIEW],
            gate=lambda r: bool((self.raw.get("governance", {}) or {}).get("purview_account")),
            retry=True,
        ))

        return waves

    # -- bicep helpers ----------------------------------------------------

    def _bicep_params(self) -> str:
        """Choose the fresh vs existing parameter file from the resolved plan."""
        capacity_existing = self.resolved.get("capacity") == "existing"
        dbx_existing = self._databricks_workspace_path() == "existing"
        if capacity_existing or dbx_existing:
            return INFRA_PARAMS_EXISTING
        return INFRA_PARAMS_FRESH

    def _hardening_ids(self) -> tuple:
        """Resolve (fabricWorkspaceId GUID, workspaceIdentityObjectId) from the workspace config.

        The Step-10 ``00_create_workspace.py --write-config`` persists ``workspace.workspace_id``
        (the created/resolved Fabric workspace GUID) and ``workspace.identity_object_id``. The
        existing-workspace path supplies ``existing_workspace_id``. Values may be ``<PLACEHOLDER>``
        tokens before Step 10 runs; the fail-fast check (real runs only) rejects those.
        """
        ws = self.raw.get("workspace", {}) or {}
        guid = ws.get("workspace_id") or ws.get("existing_workspace_id")
        obj = ws.get("identity_object_id")
        return guid, obj

    def _bicep_command(self, *, apply_hardening: bool) -> List[str]:
        rg = self.args.resource_group or os.environ.get("AZURE_RESOURCE_GROUP", "zava-rg")
        cmd = [
            "az", "deployment", "group", "create",
            "--resource-group", rg,
            "--template-file", INFRA_TEMPLATE,
            "--parameters", self.args.bicep_params or self._bicep_params(),
        ]
        if self.args.subscription:
            cmd += ["--subscription", self.args.subscription]
        if apply_hardening:
            # Override the hardening switches on the second pass (idempotent).
            cmd += ["--parameters", "applyNetworkHardening=true",
                    "--parameters", "disableStoragePublicNetworkAccess=true"]
            # Step 28: forward the Step-10 workspace GUID + Workspace Identity object id so
            # network-hardening.bicep actually adds the trusted-workspace rule + defaultAction=Deny
            # + Workspace-Identity RBAC (main.bicep builds the R10 resourceId from fabricWorkspaceId).
            guid, obj = self._hardening_ids()
            if guid:
                cmd += ["--parameters", f"fabricWorkspaceId={guid}"]
            if obj:
                cmd += ["--parameters", f"workspaceIdentityObjectId={obj}"]
        return cmd

    def _require_hardening_ids(self) -> None:
        """Fail fast when the V2 hardening wave lacks the Step-10 workspace GUID / identity id."""
        guid, obj = self._hardening_ids()
        missing = []
        if not guid or config_schema.is_placeholder(guid):
            missing.append("workspace.workspace_id (or workspace.existing_workspace_id) — the Fabric workspace GUID")
        if not obj or config_schema.is_placeholder(obj):
            missing.append("workspace.identity_object_id — the Workspace Identity object id")
        if missing:
            raise DeployError(
                "ADLS V2 hardening cannot proceed — missing required Step-10 ids: "
                + "; ".join(missing)
                + ". Run fabric/scripts/00_create_workspace.py --write-config first (it persists "
                "both into the resolved deploy_config.json), or set them manually. Without them the "
                "storage firewall would stay open (defaultAction=Allow) and the shortcut would "
                "deploy UNSECURED."
            )

    def _reload_raw_config(self) -> None:
        """Re-read the deploy config from disk to pick up ids persisted mid-run (Step 10)."""
        if self.deploy_config_path and os.path.exists(self.deploy_config_path):
            try:
                with open(self.deploy_config_path, "r", encoding="utf-8") as fh:
                    self.raw = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                LOG.warning("could not reload config %s: %s", _rel(self.deploy_config_path), exc)

    def _run_hardening_wave(self, wave: "Wave") -> int:
        """Execute the ADLS hardening Bicep pass, refreshing ids and failing fast if missing."""
        # The workspace wave (00_create_workspace.py --write-config) persists the ids DURING the
        # run; reload so this wave sees them rather than the stale start-of-run snapshot.
        self._reload_raw_config()
        if self.dry_run:
            print("    -> dry-run: command not executed.")
            return 0
        self._require_hardening_ids()
        cmd = self._bicep_command(apply_hardening=True)
        LOG.info("    hardening cmd (refreshed): %s", _render_cmd(cmd))
        return self._invoke(cmd, wave, attempts=1)

    # -- execution --------------------------------------------------------

    def execute(self) -> int:
        waves = self.build_plan()

        # Honour --start-at / --skip.
        start_idx = 0
        if self.args.start_at:
            keys = [w.key for w in waves]
            if self.args.start_at not in keys:
                raise DeployError(
                    f"--start-at {self.args.start_at!r} is not a wave key. Valid keys: "
                    f"{', '.join(keys)}"
                )
            start_idx = keys.index(self.args.start_at)
        skip = set(self.args.skip or [])

        print_plan_header(self.dry_run)
        planned = waves[start_idx:]
        for idx, wave in enumerate(planned, start=start_idx + 1):
            enabled = wave.enabled(self.resolved)
            skipped = wave.key in skip
            self._print_wave_banner(idx, len(waves), wave, enabled, skipped)

            if not enabled:
                LOG.info("  -> skipped (feature flag off)")
                continue
            if skipped:
                LOG.info("  -> skipped (--skip %s)", wave.key)
                continue

            if wave.kind == KIND_PAUSE:
                self._handle_pause(wave)
                continue

            rc = self._run_wave(wave)
            if rc != 0:
                if self.dry_run:
                    # In dry-run we want the FULL ordered plan regardless of a previewed
                    # step's exit code (often non-zero only because a manual prerequisite —
                    # e.g. the mirror OAuth connection id — is not yet filled in).
                    LOG.warning(
                        "Wave %r preview returned %d (likely an unresolved manual "
                        "prerequisite) — continuing the dry-run plan.", wave.key, rc
                    )
                    continue
                LOG.error("Wave %r failed (exit %d). Stopping.", wave.key, rc)
                LOG.error(
                    "Fix the issue, then resume with: "
                    "python scripts/deploy.py --start-at %s ...", wave.key
                )
                return rc

        print_plan_footer(self.dry_run)
        return 0

    def _print_wave_banner(
        self, idx: int, total: int, wave: Wave, enabled: bool, skipped: bool
    ) -> None:
        status = ""
        if not enabled:
            status = "  [DISABLED — feature flag off]"
        elif skipped:
            status = "  [SKIPPED — --skip]"
        print("")
        print(f"--- Wave {idx}/{total}: {wave.title}  ({wave.kind}){status}")
        if wave.note:
            print(f"    note: {wave.note}")
        if wave.command:
            print(f"    cmd : {_render_cmd(self._final_command(wave))}")

    def _final_command(self, wave: Wave) -> List[str]:
        cmd = list(wave.command)
        if self.dry_run and wave.propagate_dry_run and wave.kind in (
            KIND_FABRIC, KIND_GOVERNANCE
        ):
            cmd = cmd + ["--dry-run"]
        # Azure / Databricks have their own preview modes; surface them in dry-run.
        if self.dry_run and wave.kind == KIND_AZURE:
            cmd = cmd + ["--what-if"]
        if self.dry_run and wave.kind == KIND_DATABRICKS and "validate" not in cmd:
            # `bundle deploy/run` don't have --dry-run; we simply do not execute them.
            pass
        return cmd

    def _handle_pause(self, wave: Wave) -> None:
        print(f"    PAUSE (manual step): {wave.pause_prompt}")
        if self.dry_run:
            print("    -> dry-run: would pause here for operator confirmation.")
            return
        if self.non_interactive:
            LOG.warning("    -> --non-interactive: auto-acknowledged. Ensure the manual "
                        "step above is complete before the dependent wave runs.")
            return
        try:
            input("    Press Enter once the manual step above is complete (Ctrl-C to abort)... ")
        except EOFError:
            LOG.warning("    -> no TTY available; treating as acknowledged.")

    def _run_wave(self, wave: Wave) -> int:
        # The hardening wave owns its own execution (reload ids + fail-fast + refreshed cmd).
        if wave.key == "hardening":
            return self._run_hardening_wave(wave)
        cmd = self._final_command(wave)
        if self.dry_run:
            # Fabric/governance steps support --dry-run, so we *do* invoke them to surface
            # their own previews; everything else is preview-only (no execution).
            if wave.kind in (KIND_FABRIC, KIND_GOVERNANCE):
                return self._invoke(cmd, wave, attempts=1)
            print("    -> dry-run: command not executed.")
            return 0
        attempts = SUBPROCESS_MAX_ATTEMPTS if wave.retry else 1
        return self._invoke(cmd, wave, attempts=attempts)

    def _invoke(self, cmd: List[str], wave: Wave, *, attempts: int) -> int:
        cwd = DATABRICKS_BUNDLE_DIR if wave.kind == KIND_DATABRICKS else _REPO_ROOT
        last_rc = 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                backoff = SUBPROCESS_BACKOFF_SECONDS * (2 ** (attempt - 2))
                LOG.warning("    retry %d/%d after %ds (transient failure / token refresh) ...",
                            attempt, attempts, backoff)
                time.sleep(backoff)
            try:
                proc = subprocess.run(cmd, cwd=cwd, check=False)
                last_rc = proc.returncode
            except FileNotFoundError as exc:
                LOG.error("    command not found: %s (%s)", cmd[0], exc)
                return 127
            except KeyboardInterrupt:
                LOG.error("    aborted by operator.")
                return 130
            if last_rc == 0:
                return 0
        return last_rc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DeployError(Exception):
    """Fatal orchestration error with an actionable message."""


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, _REPO_ROOT)
    except ValueError:
        return path


def _resolve_config_path(explicit: Optional[str], candidates) -> Optional[str]:
    if explicit:
        return explicit
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


def dab_target_catalog(databricks_yml_path: str, target: str) -> Optional[str]:
    """Resolve the Unity Catalog catalog a Databricks Asset Bundle target builds.

    Reads ``databricks/bundle/databricks.yml`` and returns the effective ``catalog`` variable
    for ``target``: the per-target override under ``targets.<target>.variables.catalog`` if
    present, else the top-level ``variables.catalog.default``. Returns ``None`` if the file or
    the value cannot be resolved.

    This is a deliberately small, stdlib-only indentation-aware parser (no PyYAML dependency)
    so the offline tests stay pure. The bundle file is repo-owned with a stable shape.
    """
    try:
        with open(databricks_yml_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    def _strip_val(raw: str) -> str:
        return raw.strip().strip('"').strip("'")

    default_catalog: Optional[str] = None
    target_catalog: Optional[str] = None

    # Top-level variables.catalog.default
    in_vars = False
    in_catalog_var = False
    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        if indent == 0:
            in_vars = content == "variables:"
            in_catalog_var = False
            continue
        if in_vars and indent == 2 and content.rstrip(":") == "catalog":
            in_catalog_var = True
            continue
        if in_vars and indent == 2 and content.endswith(":"):
            in_catalog_var = False
        if in_vars and in_catalog_var and content.startswith("default:"):
            default_catalog = _strip_val(content.split(":", 1)[1])
            in_catalog_var = False

    # targets.<target>.variables.catalog
    in_targets = False
    cur_target: Optional[str] = None
    in_target_vars = False
    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        if indent == 0:
            in_targets = content == "targets:"
            cur_target = None
            in_target_vars = False
            continue
        if not in_targets:
            continue
        if indent == 2 and content.endswith(":"):
            cur_target = content[:-1].strip()
            in_target_vars = False
            continue
        if cur_target == target and indent == 4 and content.rstrip(":") == "variables":
            in_target_vars = True
            continue
        if cur_target == target and in_target_vars and indent >= 6 and content.startswith("catalog:"):
            target_catalog = _strip_val(content.split(":", 1)[1])
            break

    return target_catalog or default_catalog


def _render_cmd(cmd: List[str]) -> str:
    out = []
    for part in cmd:
        if part == sys.executable:
            out.append("python")
        elif " " in part:
            out.append(f'"{part}"')
        else:
            out.append(part)
    return " ".join(out)


def print_plan_header(dry_run: bool) -> None:
    print("=" * 80)
    print("Zava demo — end-to-end deployment orchestrator")
    if dry_run:
        print("MODE: DRY-RUN (no authentication, no mutation — plan + previews only)")
    print("=" * 80)


def print_plan_footer(dry_run: bool) -> None:
    print("")
    print("=" * 80)
    if dry_run:
        print("DRY-RUN complete — the full ordered plan above is what a real run would do.")
    else:
        print("Deployment complete. Review each wave's output above.")
    print("=" * 80)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="End-to-end, wave-ordered deployment orchestrator for the Zava demo.",
    )
    parser.add_argument("--config", help="Fabric/orchestration deploy_config.json path.")
    parser.add_argument("--databricks-config", help="databricks_config.json path.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the full ordered plan + step previews; perform NO auth and NO changes.",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Auto-acknowledge manual PAUSE prompts (CI). Implied by --dry-run.",
    )
    parser.add_argument(
        "--start-at", metavar="WAVE_KEY",
        help="Resume the plan at the named wave key (e.g. semantic_model).",
    )
    parser.add_argument(
        "--skip", action="append", metavar="WAVE_KEY",
        help="Skip a wave by key (repeatable).",
    )
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the preflight wave (assume already run).")
    parser.add_argument("--skip-azure", action="store_true",
                        help="Skip the Azure (Bicep) waves (use existing infra).")
    parser.add_argument("--skip-databricks", action="store_true",
                        help="Skip the Databricks waves (UC/medallion already built).")
    parser.add_argument("--databricks-target", default="dev",
                        help="Databricks Asset Bundle target (dev/prod). Default: dev.")
    parser.add_argument("--resource-group",
                        help="Azure resource group for the Bicep deployment "
                             "(default $AZURE_RESOURCE_GROUP or 'zava-rg').")
    parser.add_argument("--subscription",
                        help="Azure subscription id/name for the Bicep deployment.")
    parser.add_argument("--bicep-params",
                        help="Override the Bicep parameter file (defaults chosen from "
                             "fresh-vs-existing flags).")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        deployer = Deployer(args)
        deployer.load_and_validate_config()
        return deployer.execute()
    except config_schema.ConfigError as exc:
        LOG.error("Config validation failed: %s", exc)
        return 2
    except DeployError as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.error("Aborted by operator.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
