#!/usr/bin/env python3
"""Idempotent, confirm-before-destroy teardown for the Zava demo (plan Step 23).

Tears the demo back down in **safe, reverse-dependency order**: Fabric workspace items
first, then the Fabric capacity is **paused** (reversible, stops billing), then — only when
explicitly asked — the capacity is deleted and the Azure resource group (Databricks, ADLS
Gen2, Key Vault, access connector, capacity) is removed last.

Safety contract (plan Step 23)
------------------------------
* **Nothing is destroyed without explicit confirmation.** ``--dry-run`` (the default-safe
  preview) only *lists* what would be removed and exits 0. Real destruction requires
  ``--yes`` **and** (unless ``--non-interactive``) typing the resource group / workspace
  name to confirm.
* **Idempotent** — every action tolerates an already-deleted / already-paused target.
* **Pause-first, delete-last** — by default teardown only **pauses** the capacity (cost
  control). Add ``--delete-capacity`` to delete it, and ``--delete-resource-group`` to
  remove the whole RG. The most destructive action runs last.
* **No secrets** — only resource names/ids from config; auth is acquired at runtime by the
  delegated CLIs (``az`` / ``fab`` / ``scripts/pause_capacity.py``).

This script does **not** authenticate or mutate anything in ``--dry-run`` mode.

Usage
-----
    # Safe preview — list everything that WOULD be removed, in order (no changes):
    python scripts/teardown.py --dry-run

    # Pause the capacity + delete the Fabric workspace items (reversible-ish):
    python scripts/teardown.py --yes

    # Full destroy (capacity + resource group), interactive confirmation:
    python scripts/teardown.py --yes --delete-capacity --delete-resource-group
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Callable, List, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import config_schema  # noqa: E402

_REPO_ROOT = os.path.dirname(_THIS_DIR)

DEFAULT_DEPLOY_CONFIG_CANDIDATES = (
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.json"),
    os.path.join(_REPO_ROOT, "fabric", "config", "deploy_config.sample.json"),
)

PAUSE_CAPACITY = os.path.join("scripts", "pause_capacity.py")

LOG = logging.getLogger("zava.teardown")

# Action kinds.
KIND_FABRIC = "fabric"        # delete Fabric workspace / items (user token)
KIND_CAPACITY_PAUSE = "pause"  # suspend the Fabric capacity (reversible, stops billing)
KIND_CAPACITY_DELETE = "capacity-delete"
KIND_RG_DELETE = "rg-delete"   # delete the whole Azure resource group
KIND_NOTE = "note"             # manual cleanup reminder (governance artifacts)

# Substrings (matched case-insensitively against a failed command's stdout/stderr) that mark
# an **idempotent not-found** outcome — already-removed targets are success-equivalent for a
# teardown. Anything NOT matching these is treated as a *real* failure (auth/permission/
# network/throttling/unexpected) and STOPS the teardown.
_NOT_FOUND_MARKERS = (
    "resourcenotfound",
    "resourcegroupnotfound",
    "not found",
    "notfound",
    "could not be found",
    "does not exist",
    "doesn't exist",
    "no longer exists",
    "was not found",
    "could not be located",
    "404",
    "errorcode: notfound",
    "workspacenotfound",
)


def _is_idempotent_not_found(output: str) -> bool:
    """True when a non-zero command's output indicates the target was already gone."""
    if not output:
        return False
    low = output.lower()
    return any(marker in low for marker in _NOT_FOUND_MARKERS)


class TeardownStep:
    def __init__(
        self,
        key: str,
        title: str,
        kind: str,
        *,
        command: Optional[List[str]] = None,
        destructive: bool = True,
        gate: Optional[Callable[[], bool]] = None,
        note: str = "",
    ) -> None:
        self.key = key
        self.title = title
        self.kind = kind
        self.command = command or []
        self.destructive = destructive
        self.gate = gate
        self.note = note

    def enabled(self) -> bool:
        return self.gate() if self.gate else True


class Teardown:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.dry_run: bool = args.dry_run or not args.yes
        # If --yes is absent we behave as a dry-run preview (never destroy without --yes).
        self.explicit_yes: bool = args.yes
        self.non_interactive: bool = args.non_interactive
        self.continue_on_error: bool = getattr(args, "continue_on_error", False)
        self.config_path = _resolve_config_path(args.config, DEFAULT_DEPLOY_CONFIG_CANDIDATES)
        self.raw: dict = {}
        self.workspace_name: str = ""
        self.workspace_id: str = ""
        self.capacity_name: str = ""
        self.resource_group: str = ""

    # -- config -----------------------------------------------------------

    def load_config(self) -> None:
        if self.config_path and os.path.exists(self.config_path):
            # Validate (best effort) so we fail loudly on a malformed config, but tolerate
            # placeholder-only samples (teardown still needs to render the plan).
            try:
                config_schema.validate_file(self.config_path)
            except config_schema.ConfigError as exc:
                LOG.warning("Config did not fully validate (%s) — continuing for teardown.", exc)
            with open(self.config_path, "r", encoding="utf-8") as fh:
                self.raw = json.load(fh)
        else:
            LOG.warning("No deploy_config found — relying on CLI overrides for names.")

        ws = self.raw.get("workspace", {}) or {}
        cap = self.raw.get("capacity", {}) or {}
        self.workspace_name = self.args.workspace_name or ws.get("name", "")
        self.workspace_id = self.args.workspace_id or ws.get("existing_workspace_id", "")
        self.capacity_name = self.args.capacity or cap.get("name", "")
        self.resource_group = (
            self.args.resource_group
            or os.environ.get("AZURE_RESOURCE_GROUP", "zava-rg")
        )

    # -- plan -------------------------------------------------------------

    def build_plan(self) -> List[TeardownStep]:
        steps: List[TeardownStep] = []

        # 1. Delete Fabric workspace + all contained items (reverse-dependency: items live
        #    in the workspace, which lives on the capacity). One DELETE cascades the items
        #    (Operations Agent, Activator, Eventstream/Eventhouse, Data Agent, ontology,
        #    report, semantic model, shortcut, mirror, thin gold).
        ws_ref = self.workspace_id or self.workspace_name or "<workspace>"
        steps.append(TeardownStep(
            "fabric_workspace",
            f"Delete Fabric workspace and all its items ({ws_ref})",
            KIND_FABRIC,
            command=["fab", "rm", f"{self.workspace_name or ws_ref}.Workspace", "-f"],
            note="Cascades every Fabric item (agents, Activator, Eventhouse, report, "
                 "semantic model, shortcut, mirror). User token required.",
        ))

        # 2. Pause the Fabric capacity (reversible; stops compute billing immediately).
        steps.append(TeardownStep(
            "capacity_pause",
            f"Pause (suspend) the Fabric capacity ({self.capacity_name or '<capacity>'})",
            KIND_CAPACITY_PAUSE, destructive=False,
            command=self._pause_capacity_command(),
            note="Reversible — stops billing without deleting. Always safe to run first.",
        ))

        # 3. Delete the Fabric capacity (optional; otherwise the RG delete removes it).
        if self.args.delete_capacity and not self.args.delete_resource_group:
            steps.append(TeardownStep(
                "capacity_delete",
                f"Delete the Fabric capacity ({self.capacity_name or '<capacity>'})",
                KIND_CAPACITY_DELETE,
                command=self._delete_capacity_command(),
                note="Destructive. Skipped automatically when --delete-resource-group is set "
                     "(the RG delete removes it).",
            ))

        # 4. Delete the resource group LAST (Databricks, ADLS Gen2, Key Vault, access
        #    connector, capacity — everything provisioned by Bicep).
        if self.args.delete_resource_group:
            steps.append(TeardownStep(
                "resource_group",
                f"Delete the Azure resource group ({self.resource_group}) — Databricks, "
                f"ADLS Gen2, Key Vault, access connector, capacity",
                KIND_RG_DELETE,
                command=self._delete_rg_command(),
                note="MOST DESTRUCTIVE — runs last. Removes every Bicep-provisioned resource.",
            ))

        # 5. Manual governance cleanup reminders (not auto-destroyed).
        steps.append(TeardownStep(
            "governance_note", "MANUAL: governance artifact cleanup", KIND_NOTE,
            destructive=False,
            note="Policy Weaver OneLake security roles and Purview scans/collections are not "
                 "auto-removed (they may be shared). Remove them in Purview / OneLake "
                 "security if this was the only consumer. See docs/manual-steps.md.",
        ))

        return steps

    # -- command builders -------------------------------------------------

    def _pause_capacity_command(self) -> List[str]:
        cmd = [sys.executable, PAUSE_CAPACITY]
        if self.args.subscription:
            cmd += ["--subscription", self.args.subscription]
        cmd += ["--resource-group", self.resource_group]
        if self.capacity_name:
            cmd += ["--capacity", self.capacity_name]
        return cmd

    def _delete_capacity_command(self) -> List[str]:
        cmd = ["az", "resource", "delete",
               "--resource-group", self.resource_group,
               "--resource-type", "Microsoft.Fabric/capacities",
               "--name", self.capacity_name or "<capacity>"]
        if self.args.subscription:
            cmd += ["--subscription", self.args.subscription]
        return cmd

    def _delete_rg_command(self) -> List[str]:
        cmd = ["az", "group", "delete", "--name", self.resource_group, "--yes"]
        if self.args.subscription:
            cmd += ["--subscription", self.args.subscription]
        return cmd

    # -- execution --------------------------------------------------------

    def execute(self) -> int:
        steps = self.build_plan()
        self._print_header(steps)

        if self.dry_run:
            self._print_plan(steps)
            print("")
            print("DRY-RUN / no --yes: nothing was destroyed. Re-run with --yes to execute.")
            return 0

        # Explicit destructive confirmation (in addition to --yes), unless --non-interactive.
        if not self._confirm_destroy(steps):
            LOG.error("Confirmation failed / aborted — nothing destroyed.")
            return 1

        for idx, step in enumerate(steps, start=1):
            if not step.enabled():
                continue
            print("")
            print(f"--- [{idx}/{len(steps)}] {step.title}  ({step.kind})")
            if step.note:
                print(f"    note: {step.note}")
            if step.kind == KIND_NOTE:
                print(f"    -> manual reminder only; no automated action.")
                continue
            print(f"    cmd : {_render_cmd(step.command)}")
            rc, output = self._invoke(step)
            if rc != 0:
                if _is_idempotent_not_found(output):
                    # 'not found' is success-equivalent for teardown — log and continue so a
                    # partially-torn-down environment can be finished cleanly.
                    LOG.info("    step %r: target already absent (not-found) — idempotent, "
                             "continuing.", step.key)
                    continue
                # A REAL failure (auth / permission / network / throttling / unexpected).
                # Do NOT blindly continue into further destructive steps.
                first = (output.strip().splitlines() or ["(no output captured)"])[0]
                LOG.error("    step %r FAILED (exit %d): %s", step.key, rc, first)
                if self.continue_on_error:
                    LOG.warning("    --continue-on-error set — proceeding past the failure "
                                "(NOT recommended; later destructive steps will still run).")
                    continue
                LOG.error("")
                LOG.error("    STOPPING teardown to avoid running further destructive steps "
                          "against an environment in an unknown state.")
                LOG.error("    Resolve the error above (e.g. 'az login' / permissions / "
                          "network), then re-run teardown to resume. Pass --continue-on-error "
                          "to override this safety stop.")
                return rc
        print("")
        print("Teardown complete (idempotent — re-run safely to finish any partial removal).")
        return 0

    def _confirm_destroy(self, steps: List[TeardownStep]) -> bool:
        destructive = [s for s in steps if s.destructive and s.enabled()
                       and s.kind != KIND_NOTE]
        if not destructive:
            return True
        if self.non_interactive:
            LOG.warning("--non-interactive + --yes: proceeding with %d destructive action(s).",
                        len(destructive))
            return True
        target = self.resource_group if self.args.delete_resource_group else (
            self.workspace_name or self.capacity_name or "DESTROY")
        print("")
        print(f"About to perform {len(destructive)} DESTRUCTIVE action(s).")
        print(f"Type the confirmation token to proceed: {target!r}")
        try:
            entered = input("    confirmation> ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if entered != target:
            print(f"    token mismatch ({entered!r} != {target!r}).")
            return False
        return True

    def _invoke(self, step: TeardownStep) -> "tuple[int, str]":
        """Run a destructive step, capturing output so failures can be classified.

        Returns ``(returncode, combined_stdout_stderr)``. Output is echoed live-ish so the
        operator still sees the CLI's messages.
        """
        try:
            proc = subprocess.run(
                step.command, cwd=_REPO_ROOT, check=False,
                capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            # A missing CLI is a real environment failure (not an idempotent not-found).
            msg = f"command not found: {step.command[0]} ({exc})"
            LOG.error("    %s", msg)
            return 127, msg
        output = (proc.stdout or "") + (proc.stderr or "")
        for line in output.splitlines():
            print(f"      | {line}")
        return proc.returncode, output

    # -- printing ---------------------------------------------------------

    def _print_header(self, steps: List[TeardownStep]) -> None:
        print("=" * 80)
        print("Zava demo — teardown")
        mode = "DRY-RUN (preview only — no auth, no changes)" if self.dry_run else \
               "EXECUTE (--yes given)"
        print(f"MODE: {mode}")
        print(f"workspace : {self.workspace_id or self.workspace_name or '(unknown)'}")
        print(f"capacity  : {self.capacity_name or '(unknown)'}")
        print(f"resource-group: {self.resource_group}")
        print(f"delete-capacity={self.args.delete_capacity} "
              f"delete-resource-group={self.args.delete_resource_group}")
        print("=" * 80)

    def _print_plan(self, steps: List[TeardownStep]) -> None:
        for idx, step in enumerate(steps, start=1):
            flag = "DESTRUCTIVE" if step.destructive and step.kind != KIND_NOTE else "safe"
            print("")
            print(f"--- [{idx}/{len(steps)}] {step.title}  ({step.kind}) [{flag}]")
            if step.note:
                print(f"    note: {step.note}")
            if step.command:
                print(f"    cmd : {_render_cmd(step.command)}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_config_path(explicit: Optional[str], candidates) -> Optional[str]:
    if explicit:
        return explicit
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teardown.py",
        description="Idempotent, confirm-before-destroy teardown for the Zava demo.",
    )
    parser.add_argument("--config", help="deploy_config.json path (for resource names).")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview the ordered teardown plan; perform NO changes (default-safe).",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Explicit confirmation REQUIRED to actually destroy anything. Without it, "
             "teardown only previews (like --dry-run).",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Skip the type-the-name confirmation (still requires --yes). For CI.",
    )
    parser.add_argument(
        "--continue-on-error", action="store_true",
        help="Continue past a REAL (non 'not-found') destructive failure instead of stopping. "
             "NOT recommended — defaults to stopping so later destructive steps don't run "
             "against an environment in an unknown state.",
    )
    parser.add_argument(
        "--delete-capacity", action="store_true",
        help="Delete the Fabric capacity (default only pauses it).",
    )
    parser.add_argument(
        "--delete-resource-group", action="store_true",
        help="Delete the whole Azure resource group LAST (Databricks, ADLS, Key Vault, "
             "capacity). Most destructive.",
    )
    parser.add_argument("--workspace-name", help="Fabric workspace display name override.")
    parser.add_argument("--workspace-id", help="Fabric workspace GUID override.")
    parser.add_argument("--capacity", help="Fabric capacity name override.")
    parser.add_argument("--resource-group",
                        help="Azure resource group (default $AZURE_RESOURCE_GROUP or 'zava-rg').")
    parser.add_argument("--subscription", help="Azure subscription id/name override.")
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
        teardown = Teardown(args)
        teardown.load_config()
        return teardown.execute()
    except KeyboardInterrupt:
        LOG.error("Aborted by operator.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
