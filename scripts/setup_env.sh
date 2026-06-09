#!/usr/bin/env bash
# =============================================================================
# Reproducible Python environment bootstrap for the Zava Databricks + Fabric demo.
# Platform: macOS / Linux (bash).
# =============================================================================
# One command to create a virtual environment (.venv) at the repo root and
# install every Python dependency in requirements.txt. Idempotent: re-running
# reuses an existing .venv.
#
# Strategy:
#   1. Prefer `uv` when on PATH (uv can provision Python 3.12 even when the host
#      only has newer interpreters):
#        uv python install <ver> -> uv venv --python <ver> .venv
#        -> uv pip install -r requirements.txt
#   2. Fallback to a system Python 3.12 then 3.11 (python3.12 / python3.11):
#        <python> -m venv .venv -> pip upgrade -> pip install -r requirements.txt
#   3. Validate the interpreter is >= 3.11 and < 3.13. If only 3.13+/3.10- is
#      available and `uv` is absent, fail with an actionable message rather than
#      building a broken venv.
#
# The Python version contract (>= 3.11, < 3.13) is required by Policy Weaver
# (plan §7).
#
# NOTE: Databricks CLI, Azure CLI, Bicep, and Power BI Desktop are NOT pip
# packages and are installed separately (see docs/prerequisites.md §6).
# =============================================================================
set -euo pipefail

PYTHON_VERSION="3.12"

print_help() {
    cat <<'EOF'
Zava demo — Python environment setup (macOS / Linux / bash)

USAGE:
    ./scripts/setup_env.sh [--python-version <3.11|3.12>] [--help]

WHAT IT DOES:
    Creates .venv at the repo root and installs requirements.txt. Idempotent.
    Prefers `uv` if present; otherwise uses a system Python 3.12/3.11.

OPTIONS:
    --python-version <ver>   Target Python version (default: 3.12). Must be 3.11 or 3.12.
    --help, -h               Show this help and exit.

AFTER SETUP:
    1. Activate:  source .venv/bin/activate
    2. Preflight: python scripts/preflight_checks.py
    3. Install the NON-pip tools (see docs/prerequisites.md §6):
         - Databricks CLI (standalone binary)
         - Azure CLI (az) + Bicep (az bicep install)
         - Power BI Desktop (Windows GUI — PBIP authoring)
EOF
}

# --- Parse args --------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --python-version)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --python-version requires a value (3.11 or 3.12)." >&2
                exit 1
            fi
            PYTHON_VERSION="$2"
            shift 2
            ;;
        --python-version=*)
            PYTHON_VERSION="${1#*=}"
            shift
            ;;
        -h|--help)
            print_help
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument '$1'. Use --help for usage." >&2
            exit 1
            ;;
    esac
done

# --- Validate requested version is within contract (>= 3.11, < 3.13) ---------
if [[ "$PYTHON_VERSION" != "3.11" && "$PYTHON_VERSION" != "3.12" ]]; then
    echo "ERROR: --python-version must be 3.11 or 3.12 (contract: >= 3.11, < 3.13). Got '$PYTHON_VERSION'." >&2
    exit 1
fi

# --- Resolve repo root (script lives in <repo>/scripts) ----------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REQUIREMENTS="$REPO_ROOT/requirements.txt"
VENV_DIR="$REPO_ROOT/.venv"

if [[ ! -f "$REQUIREMENTS" ]]; then
    echo "ERROR: requirements.txt not found at '$REQUIREMENTS'." >&2
    exit 1
fi

echo "==> Zava demo Python environment setup"
echo "    Repo root          : $REPO_ROOT"
echo "    Target Python      : $PYTHON_VERSION (contract >= 3.11, < 3.13)"
echo "    Virtual env (.venv): $VENV_DIR"
if [[ -d "$VENV_DIR" ]]; then
    echo "    .venv already exists -> reusing (idempotent re-install)."
fi

has_command() { command -v "$1" >/dev/null 2>&1; }

# uv's DEFAULT install strategy hardlinks wheels from its cache into the venv.
# On cloud-synced / network filesystems (OneDrive, Dropbox, SMB/NFS shares)
# hardlinking can fail, silently leaving a broken venv. Forcing copy link-mode is
# safe everywhere and only marginally slower, so we make it the unconditional
# default rather than asking the user to set anything.
export UV_LINK_MODE=copy

# =============================================================================
# Path 1 — uv (preferred)
# =============================================================================
if has_command uv; then
    echo "==> Found 'uv' on PATH — using uv (preferred path)."

    echo "--> uv python install $PYTHON_VERSION"
    uv python install "$PYTHON_VERSION"

    echo "--> uv venv --python $PYTHON_VERSION \"$VENV_DIR\""
    uv venv --python "$PYTHON_VERSION" "$VENV_DIR"

    # --link-mode=copy (also set via UV_LINK_MODE above) avoids hardlink failures
    # on cloud-synced / network filesystems — see comment above. set -euo pipefail
    # aborts the script if this install exits non-zero (it is not piped or
    # ||-guarded), so a failed install can never reach the success banner.
    echo "--> uv pip install --link-mode=copy -r \"$REQUIREMENTS\""
    uv pip install --python "$VENV_DIR" --link-mode=copy -r "$REQUIREMENTS"

    echo ""
    echo "==> uv install command completed; verifying below before declaring ready."
else
    # =========================================================================
    # Path 2 — system Python fallback (no uv)
    # =========================================================================
    echo "==> 'uv' not found — falling back to a system Python interpreter."

    # Preference order: requested version first, then the other supported version.
    CHOSEN_PY=""
    if [[ "$PYTHON_VERSION" == "3.12" ]]; then
        CANDIDATES=("python3.12" "python3.11")
    else
        CANDIDATES=("python3.11" "python3.12")
    fi

    for exe in "${CANDIDATES[@]}"; do
        if has_command "$exe"; then
            CHOSEN_PY="$exe"
            break
        fi
    done

    if [[ -z "$CHOSEN_PY" ]]; then
        cat >&2 <<'EOF'
ERROR: No supported Python interpreter (3.11 or 3.12) was found, and 'uv' is not installed.
The host default Python may be 3.13+ or 3.10-, which is OUTSIDE the >= 3.11, < 3.13 contract
required by Policy Weaver (plan §7).

Choose ONE of:
  * Install uv (recommended — provisions 3.12 automatically):
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # or:  brew install uv
    then re-run:  ./scripts/setup_env.sh
  * Install Python 3.12 from https://www.python.org/ (or your package manager)
    and re-run this script.
EOF
        exit 1
    fi

    echo "--> Using interpreter: $CHOSEN_PY"

    # Validate the chosen interpreter is >= 3.11 and < 3.13.
    DETECTED="$("$CHOSEN_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    echo "--> Detected Python version: $DETECTED"
    MAJ="${DETECTED%%.*}"
    MIN="${DETECTED##*.}"
    if [[ "$MAJ" -ne 3 || "$MIN" -lt 11 || "$MIN" -ge 13 ]]; then
        echo "ERROR: Interpreter '$CHOSEN_PY' reports Python $DETECTED, which is outside the >= 3.11, < 3.13 contract. Install uv or Python 3.12." >&2
        exit 1
    fi

    echo "--> Creating virtual environment at '$VENV_DIR'"
    "$CHOSEN_PY" -m venv "$VENV_DIR"

    VENV_PYTHON="$VENV_DIR/bin/python"
    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "ERROR: Virtual environment python not found at '$VENV_PYTHON' after venv creation." >&2
        exit 1
    fi

    echo "--> Upgrading pip in the virtual environment"
    "$VENV_PYTHON" -m pip install --upgrade pip

    echo "--> pip install -r \"$REQUIREMENTS\""
    "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS"

    echo ""
    echo "==> pip install command completed (system Python $DETECTED); verifying below before declaring ready."
fi

# =============================================================================
# Post-install verification — never claim success on an incomplete env (BUG 2)
# =============================================================================
VERIFY_PYTHON="$VENV_DIR/bin/python"
if [[ ! -x "$VERIFY_PYTHON" ]]; then
    echo "ERROR: Post-install check FAILED: venv python not found at '$VERIFY_PYTHON'. The environment is incomplete." >&2
    exit 1
fi

echo ""
echo "==> Verifying the environment (smoke import of key packages)..."
if ! "$VERIFY_PYTHON" -c "import sempy_labs, requests, yaml, pandas, pyarrow"; then
    cat >&2 <<'EOF'
ERROR: Post-install verification FAILED: one or more key packages did not import
(sempy_labs / requests / yaml / pandas / pyarrow). The virtual environment is
INCOMPLETE — do NOT use it. Re-run this script after resolving the install error
above. (On cloud-synced/network paths, ensure uv uses copy link-mode.)
EOF
    exit 1
fi

# `fab` (ms-fabric-cli) installs as a console script in the venv bin/ dir.
if [[ ! -x "$VENV_DIR/bin/fab" ]]; then
    echo "ERROR: Post-install verification FAILED: 'fab' (ms-fabric-cli) not found at '$VENV_DIR/bin/fab'. The environment is incomplete." >&2
    exit 1
fi
echo "    OK: sempy_labs, requests, yaml, pandas, pyarrow importable; 'fab' present."
echo ""
echo "==> Environment ready (smoke verification passed)."

# =============================================================================
# Next steps
# =============================================================================
cat <<EOF

NEXT STEPS
  1. Activate the environment:
         source .venv/bin/activate
  2. Run the read-only preflight checks:
         python scripts/preflight_checks.py
  3. Install the NON-pip tools (see docs/prerequisites.md §6):
         - Databricks CLI : https://learn.microsoft.com/en-us/azure/databricks/dev-tools/cli/
                            brew install databricks/tap/databricks   (macOS)
         - Azure CLI (az) : https://learn.microsoft.com/en-us/cli/azure/install-azure-cli
         - Bicep          : az bicep install   (confirm: az bicep version)
         - Power BI Desktop (Windows GUI, for PBIP authoring)

Done.
EOF
