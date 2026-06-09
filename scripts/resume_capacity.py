#!/usr/bin/env python3
"""Resume the Zava demo Microsoft Fabric capacity (counterpart to ``pause_capacity.py``).

Why
---
Pausing a Fabric capacity stops compute (CU) billing but also makes all content unavailable.
Run this **resume** action (``Microsoft.Fabric/capacities`` resume) shortly before a demo or
development session to return the capacity to **Active** (billing resumes at ~$11.52/hour for F64 —
see ``docs/cost.md`` / research R9). Remember to ``pause_capacity.py`` again as soon as you are done.

This script is a thin counterpart that reuses the shared core in ``pause_capacity.py`` (identity
resolution from ``deploy_config.json``, ``DefaultAzureCredential`` / ``az login`` auth, idempotency,
and logging). It simply invokes the ``resume`` action instead of ``suspend``.

Security / Phase-0 notes
------------------------
* **No secrets.** Identity comes from config (names/ids only); auth is acquired at runtime via
  ``DefaultAzureCredential`` / ``az login``.
* **Authoring phase:** do **not** run the live ``resume`` action against a real capacity unless you
  intend to start compute billing. Use ``--dry-run`` to preview safely.

Usage
-----
    # Preview only — no mutation (safe):
    python scripts/resume_capacity.py --dry-run

    # Resume using identity from deploy_config.json + env:
    python scripts/resume_capacity.py

    # Fully explicit:
    python scripts/resume_capacity.py \
        --subscription <SUBSCRIPTION_ID> \
        --resource-group <RESOURCE_GROUP> \
        --capacity zava-fabric-cap
"""

from __future__ import annotations

import os
import sys

# Allow running both as a script (`python scripts/resume_capacity.py`) and as a module
# (`python -m scripts.resume_capacity`) by ensuring the scripts dir is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pause_capacity import main  # noqa: E402  (path tweak must precede import)


if __name__ == "__main__":
    sys.exit(main("resume"))
