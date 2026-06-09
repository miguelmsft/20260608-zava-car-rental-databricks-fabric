#!/usr/bin/env python3
"""Offline, no-mutation tests for the Step 23 orchestration scripts.

These tests are **pure** — no real Azure / Fabric / Databricks calls, no network, no package
installs. Every subprocess boundary is monkeypatched, so they can run anywhere with just the
standard library:

    python scripts/test_preflight_checks.py
    # or
    python -m unittest scripts.test_preflight_checks

They lock in the three reworked behaviours the reviewer flagged:

1. ``check_region()`` is **coherent** with ``config_schema``'s unconditional allow-list:
   ``eastus2`` passes for both Operations-Agent states; ``eastus`` FAILs for both (with a
   message that only differs in its *reason*).
2. ``deploy.py --dry-run`` and ``teardown.py --dry-run`` perform **no destructive** subprocess
   invocations (dry-run never mutates).
3. ``teardown.py`` **stops** on a real (non 'not-found') destructive failure, but treats an
   idempotent 'ResourceNotFound' as success-equivalent and continues.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import preflight_checks  # noqa: E402
import deploy  # noqa: E402
import teardown  # noqa: E402

_REPO_ROOT = os.path.dirname(_THIS_DIR)
_SAMPLE_DEPLOY_CONFIG = os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json")
_DATABRICKS_YML = os.path.join(_REPO_ROOT, "databricks", "bundle", "databricks.yml")
_ACCESS_POLICIES_SQL = os.path.join(_REPO_ROOT, "databricks", "uc", "05_access_policies.sql")


def _fake_proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# Commands that must NEVER be executed during a dry-run (a non-exhaustive destructive set).
_DESTRUCTIVE_TOKENS = (
    "deploy", "run", "create", "delete", "rm", "group", "update",
)


class RegionCheckCoherenceTests(unittest.TestCase):
    """Finding 1 — the region allow-list is enforced unconditionally + coherently."""

    def test_eastus2_passes_regardless_of_operations_agent(self):
        for ops in (False, True):
            res = preflight_checks.check_region("eastus2", operations_agent=ops)
            self.assertEqual(res.status, preflight_checks.PASS,
                             f"eastus2 should PASS (operations_agent={ops})")

    def test_westus_passes(self):
        res = preflight_checks.check_region("westus", operations_agent=False)
        self.assertEqual(res.status, preflight_checks.PASS)

    def test_eastus_fails_in_both_operations_agent_states(self):
        fail_off = preflight_checks.check_region("eastus", operations_agent=False)
        fail_on = preflight_checks.check_region("eastus", operations_agent=True)
        self.assertEqual(fail_off.status, preflight_checks.FAIL,
                         "eastus must FAIL even when operations_agent=false (unconditional)")
        self.assertEqual(fail_on.status, preflight_checks.FAIL,
                         "eastus must FAIL when operations_agent=true")

    def test_southcentralus_fails_in_both_states(self):
        self.assertEqual(
            preflight_checks.check_region("southcentralus", operations_agent=False).status,
            preflight_checks.FAIL)
        self.assertEqual(
            preflight_checks.check_region("southcentralus", operations_agent=True).status,
            preflight_checks.FAIL)

    def test_eastus_message_cites_operations_agent_only_when_enabled(self):
        fail_off = preflight_checks.check_region("eastus", operations_agent=False)
        fail_on = preflight_checks.check_region("eastus", operations_agent=True)
        # Operations-Agent reason appears only in the ops=true message.
        self.assertIn("operations_agent", fail_on.message.lower())
        self.assertNotIn("enable_operations_agent=true", fail_off.message.lower())
        # The ops=false message must explain the region-agnostic email feature + allow-list.
        self.assertIn("allow-list", fail_off.message.lower())
        # Both must point the operator at the validated regions.
        for res in (fail_off, fail_on):
            self.assertIn("eastus2", res.remediation.lower())

    def test_other_non_allowlist_region_fails(self):
        res = preflight_checks.check_region("centralus", operations_agent=False)
        self.assertEqual(res.status, preflight_checks.FAIL,
                         "a non-allow-list region must FAIL (coherent with config_schema)")


class CapacityStateProbeTests(unittest.TestCase):
    """Finding 2 — capacity *state* is reported, never silently passed."""

    def test_fresh_provision_reports_gap_not_pass(self):
        res = preflight_checks.check_capacity_state(
            {"name": "zava-cap", "use_existing": False}, subscription=None)
        self.assertEqual(res.status, preflight_checks.WARN)
        self.assertNotEqual(res.status, preflight_checks.PASS)

    def test_existing_with_placeholder_id_reports_gap(self):
        res = preflight_checks.check_capacity_state(
            {"name": "zava-cap", "use_existing": True,
             "existing_capacity_id": "/subscriptions/<SUBSCRIPTION_ID>/x"},
            subscription=None)
        self.assertEqual(res.status, preflight_checks.WARN)

    def test_existing_active_passes(self):
        orig = preflight_checks._probe_capacity_state
        preflight_checks._probe_capacity_state = lambda *_a, **_k: (True, "Active")
        try:
            res = preflight_checks.check_capacity_state(
                {"name": "zava-cap", "use_existing": True,
                 "existing_capacity_id": "/subscriptions/abc/rg/x"},
                subscription=None)
        finally:
            preflight_checks._probe_capacity_state = orig
        self.assertEqual(res.status, preflight_checks.PASS)

    def test_existing_paused_fails(self):
        orig = preflight_checks._probe_capacity_state
        preflight_checks._probe_capacity_state = lambda *_a, **_k: (True, "Paused")
        try:
            res = preflight_checks.check_capacity_state(
                {"name": "zava-cap", "use_existing": True,
                 "existing_capacity_id": "/subscriptions/abc/rg/x"},
                subscription=None)
        finally:
            preflight_checks._probe_capacity_state = orig
        self.assertEqual(res.status, preflight_checks.FAIL)
        self.assertIn("resume", res.remediation.lower())

    def test_existing_unprobeable_reports_gap(self):
        orig = preflight_checks._probe_capacity_state
        preflight_checks._probe_capacity_state = lambda *_a, **_k: (False, "az CLI not on PATH")
        try:
            res = preflight_checks.check_capacity_state(
                {"name": "zava-cap", "use_existing": True,
                 "existing_capacity_id": "/subscriptions/abc/rg/x"},
                subscription=None)
        finally:
            preflight_checks._probe_capacity_state = orig
        self.assertEqual(res.status, preflight_checks.WARN)


class DeployDryRunSmokeTests(unittest.TestCase):
    """Finding-adjacent — deploy.py --dry-run never executes a destructive command."""

    def test_dry_run_invokes_only_preview_commands(self):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return _fake_proc(returncode=0)

        orig = deploy.subprocess.run
        deploy.subprocess.run = fake_run
        try:
            rc = deploy.main(["--dry-run", "--skip-preflight"])
        finally:
            deploy.subprocess.run = orig

        self.assertEqual(rc, 0)
        # Every subprocess invoked during a dry-run must be a *preview* (carry --dry-run);
        # bare destructive commands (databricks bundle deploy/run, az group delete, ...) must
        # never be executed.
        for cmd in calls:
            self.assertIn("--dry-run", cmd,
                          f"dry-run executed a non-preview command: {cmd}")


class TeardownDryRunSmokeTests(unittest.TestCase):
    """Finding-adjacent — teardown.py --dry-run executes nothing at all."""

    def test_dry_run_executes_no_subprocess(self):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return _fake_proc(returncode=0)

        orig = teardown.subprocess.run
        teardown.subprocess.run = fake_run
        try:
            rc = teardown.main(["--dry-run", "--workspace-name", "zava-ws",
                                "--capacity", "zava-cap"])
        finally:
            teardown.subprocess.run = orig

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [], "teardown --dry-run must not execute any subprocess")


class TeardownFailureHandlingTests(unittest.TestCase):
    """Finding 3 — stop on a real failure; continue on an idempotent not-found."""

    def _make_teardown(self, run_results):
        """Build a Teardown wired with a queued fake subprocess runner."""
        args = teardown.build_arg_parser().parse_args(
            ["--yes", "--non-interactive",
             "--workspace-name", "zava-ws", "--capacity", "zava-cap"])
        td = teardown.Teardown(args)
        td.load_config()

        queue = list(run_results)
        invoked = []

        def fake_run(cmd, *a, **k):
            invoked.append(list(cmd))
            if queue:
                return queue.pop(0)
            return _fake_proc(returncode=0)

        return td, fake_run, invoked

    def test_real_failure_stops_before_next_destructive_step(self):
        # First step (fabric workspace delete) fails with an auth error — a REAL failure.
        results = [_fake_proc(returncode=1,
                              stderr="ERROR: (AuthorizationFailed) does not have permission")]
        td, fake_run, invoked = self._make_teardown(results)

        orig = teardown.subprocess.run
        teardown.subprocess.run = fake_run
        try:
            rc = td.execute()
        finally:
            teardown.subprocess.run = orig

        self.assertNotEqual(rc, 0, "a real destructive failure must surface a non-zero exit")
        self.assertEqual(len(invoked), 1,
                         "teardown must STOP after the first real failure (not run later "
                         f"destructive steps); invoked={invoked}")

    def test_not_found_is_idempotent_and_continues(self):
        # First step 'not found' (idempotent) -> continue; capacity pause then succeeds.
        results = [
            _fake_proc(returncode=3, stderr="(ResourceNotFound) could not be found"),
            _fake_proc(returncode=0, stdout="paused"),
        ]
        td, fake_run, invoked = self._make_teardown(results)

        orig = teardown.subprocess.run
        teardown.subprocess.run = fake_run
        try:
            rc = td.execute()
        finally:
            teardown.subprocess.run = orig

        self.assertEqual(rc, 0, "an idempotent not-found teardown should complete cleanly")
        self.assertGreaterEqual(
            len(invoked), 2,
            "after an idempotent not-found, teardown must continue to the next step")

    def test_continue_on_error_overrides_stop(self):
        args = teardown.build_arg_parser().parse_args(
            ["--yes", "--non-interactive", "--continue-on-error",
             "--workspace-name", "zava-ws", "--capacity", "zava-cap"])
        td = teardown.Teardown(args)
        td.load_config()

        invoked = []
        queue = [_fake_proc(returncode=1, stderr="(AuthorizationFailed) nope"),
                 _fake_proc(returncode=0, stdout="paused")]

        def fake_run(cmd, *a, **k):
            invoked.append(list(cmd))
            return queue.pop(0) if queue else _fake_proc(returncode=0)

        orig = teardown.subprocess.run
        teardown.subprocess.run = fake_run
        try:
            rc = td.execute()
        finally:
            teardown.subprocess.run = orig

        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(invoked), 2,
                                "--continue-on-error must proceed past a real failure")


class NotFoundClassifierTests(unittest.TestCase):
    def test_known_not_found_markers(self):
        for text in ("ResourceNotFound", "could not be found", "does not exist",
                     "HTTP 404", "WorkspaceNotFound"):
            self.assertTrue(teardown._is_idempotent_not_found(text), text)

    def test_real_failures_are_not_idempotent(self):
        for text in ("AuthorizationFailed", "network is unreachable", "429 TooManyRequests",
                     ""):
            self.assertFalse(teardown._is_idempotent_not_found(text), text)


# ---------------------------------------------------------------------------
# Step 28 remediation — wiring & config coherence (offline, pure-stdlib)
# ---------------------------------------------------------------------------

def _load_sample_deploy_config() -> dict:
    with open(_SAMPLE_DEPLOY_CONFIG, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _make_dry_run_deployer() -> "deploy.Deployer":
    """Build a Deployer over the committed sample config in dry-run (no auth, no mutation)."""
    args = deploy.build_arg_parser().parse_args(["--dry-run", "--skip-preflight"])
    dep = deploy.Deployer(args)
    dep.load_and_validate_config()
    return dep


class CatalogCoherenceTests(unittest.TestCase):
    """Step 28 (a) — the selected DAB-target catalog must equal source.databricks_catalog."""

    def test_dev_target_catalog_matches_source(self):
        sample = _load_sample_deploy_config()
        source_catalog = sample["source"]["databricks_catalog"]
        dab_catalog = deploy.dab_target_catalog(_DATABRICKS_YML, "dev")
        self.assertEqual(
            dab_catalog, source_catalog,
            "default DAB target 'dev' must build the same catalog the Fabric config reads",
        )

    def test_prod_target_catalog_matches_source(self):
        sample = _load_sample_deploy_config()
        source_catalog = sample["source"]["databricks_catalog"]
        self.assertEqual(deploy.dab_target_catalog(_DATABRICKS_YML, "prod"), source_catalog)

    def test_deployer_load_does_not_raise_on_coherent_sample(self):
        # load_and_validate_config runs the fail-fast coherence guard; the sample is coherent.
        dep = _make_dry_run_deployer()
        self.assertEqual(dep.raw["source"]["databricks_catalog"],
                         deploy.dab_target_catalog(_DATABRICKS_YML, "dev"))

    def test_incoherent_catalog_fails_fast(self):
        args = deploy.build_arg_parser().parse_args(["--dry-run", "--skip-preflight"])
        dep = deploy.Deployer(args)
        dep.resolved = {"kind": "fabric", "features": {}}
        dep.raw = {"source": {"databricks_catalog": "definitely_not_the_dab_catalog"}}
        with self.assertRaises(deploy.DeployError):
            dep._check_catalog_coherence()


class ConfigContractCoverageTests(unittest.TestCase):
    """Step 28 (b) — every config key the Fabric scripts read is declared in the sample/schema."""

    # (section, key) pairs the fabric/scripts/*.py read (10/20/30/50/70/80 + 00).
    SCRIPT_READ_KEYS = {
        "source": ("databricks_catalog", "gold_schema", "databricks_workspace_url"),
        "mirroring": ("databricks_connection_id", "mode", "databricks_workspace_url",
                      "storage_connection_id", "item_name", "description", "auto_sync", "schemas"),
        "shortcut": ("connection_id", "abfss_path", "adls_location", "adls_subpath",
                     "lakehouse_id", "lakehouse_name", "name", "path"),
        "lakehouse": ("name", "id"),
        "semantic_model": ("name", "id", "lakehouse_id", "lakehouse_name", "rebind_report",
                           "source_name", "source_type", "thin_gold_schema"),
        "report": ("name", "semantic_model_id"),
        "ontology": ("name", "graph_name"),
        "data_agent": ("name", "graph_name", "semantic_model_name"),
        "operations_agent": ("agent_name", "message_recipient_upn", "should_run",
                             "action_pipeline_id", "action_pipeline_name", "definition_part_path"),
        "realtime": ("eventhouse_name", "kql_database_name", "kql_table_name"),
        "alerting": ("site_manager_email",),
        "workspace": ("name", "existing_workspace_id", "workspace_id", "identity_object_id"),
    }

    def test_every_script_read_key_is_declared_in_sample(self):
        sample = _load_sample_deploy_config()
        missing = []
        for section, keys in self.SCRIPT_READ_KEYS.items():
            sec = sample.get(section)
            if not isinstance(sec, dict):
                missing.append(f"{section} (section absent)")
                continue
            for key in keys:
                if key not in sec:
                    missing.append(f"{section}.{key}")
        self.assertEqual(missing, [], f"sample config missing script-read keys: {missing}")

    def test_sample_config_validates_against_schema(self):
        import config_schema  # noqa: WPS433 (local import keeps test self-contained)
        # Validation must pass with all the newly declared sections present (placeholders ok).
        result = config_schema.validate_file(_SAMPLE_DEPLOY_CONFIG)
        self.assertEqual(result["kind"], "fabric")

    def test_schema_requires_core_connection_ids(self):
        import config_schema  # noqa: WPS433
        # Removing a required connection id (manual OAuth) must now FAIL validation.
        sample = _load_sample_deploy_config()
        sample["mirroring"].pop("databricks_connection_id", None)
        with self.assertRaises(config_schema.ConfigError):
            config_schema.validate_deploy_config(sample)
        sample2 = _load_sample_deploy_config()
        sample2["shortcut"].pop("connection_id", None)
        with self.assertRaises(config_schema.ConfigError):
            config_schema.validate_deploy_config(sample2)


class HardeningBicepCommandTests(unittest.TestCase):
    """Step 28 (c) — the hardening Bicep command carries the workspace GUID + identity object id."""

    def test_hardening_command_includes_ids(self):
        dep = _make_dry_run_deployer()
        cmd = dep._bicep_command(apply_hardening=True)
        joined = " ".join(cmd)
        self.assertIn("applyNetworkHardening=true", joined)
        self.assertIn("fabricWorkspaceId=", joined,
                      "hardening command must pass the Fabric workspace GUID")
        self.assertIn("workspaceIdentityObjectId=", joined,
                      "hardening command must pass the Workspace Identity object id")

    def test_base_command_does_not_include_hardening_ids(self):
        dep = _make_dry_run_deployer()
        joined = " ".join(dep._bicep_command(apply_hardening=False))
        self.assertNotIn("fabricWorkspaceId=", joined)
        self.assertNotIn("applyNetworkHardening=true", joined)

    def test_real_run_fails_fast_on_placeholder_ids(self):
        # On a real (non-dry-run) run, placeholder ids must be rejected before deploying.
        args = deploy.build_arg_parser().parse_args(["--skip-preflight"])
        dep = deploy.Deployer(args)
        dep.load_and_validate_config()  # sample has <WORKSPACE_GUID> / <WORKSPACE_IDENTITY_GUID>
        with self.assertRaises(deploy.DeployError):
            dep._require_hardening_ids()


class AccessPolicyReachabilityTests(unittest.TestCase):
    """Step 28 (d) — 05_access_policies.sql is reachable from the deploy/DAB plan before governance."""

    def test_access_policy_wave_precedes_governance(self):
        dep = _make_dry_run_deployer()
        waves = dep.build_plan()
        keys = [w.key for w in waves]
        self.assertIn("dbx_access_policies", keys)
        self.assertIn("policy_weaver", keys)
        self.assertLess(
            keys.index("dbx_access_policies"), keys.index("policy_weaver"),
            "UC access policies must be applied before the Policy Weaver (governance) wave",
        )

    def test_access_policy_wave_runs_the_dab_job(self):
        dep = _make_dry_run_deployer()
        wave = next(w for w in dep.build_plan() if w.key == "dbx_access_policies")
        self.assertIn("zava_access_policies", wave.command)

    def test_dab_defines_access_policies_job_running_the_sql(self):
        with open(_DATABRICKS_YML, "r", encoding="utf-8") as fh:
            dab = fh.read()
        self.assertIn("zava_access_policies:", dab, "DAB must define the zava_access_policies job")
        self.assertIn("05_access_policies.sql", dab,
                      "DAB access-policy job must run uc/05_access_policies.sql")

    def test_access_policies_sql_is_dab_parameterized(self):
        self.assertTrue(os.path.exists(_ACCESS_POLICIES_SQL))
        with open(_ACCESS_POLICIES_SQL, "r", encoding="utf-8") as fh:
            sql = fh.read()
        # Named markers the DAB SQL task binds (catalog / curated_schema / gold_schema).
        for marker in (":catalog", ":curated_schema", ":gold_schema"):
            self.assertIn(marker, sql, f"05_access_policies.sql must bind {marker} from the DAB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
