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


if __name__ == "__main__":
    unittest.main(verbosity=2)
