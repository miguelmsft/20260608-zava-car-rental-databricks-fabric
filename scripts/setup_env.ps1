#Requires -Version 5.1
<#
.SYNOPSIS
    Reproducible Python environment bootstrap for the Zava Databricks + Fabric demo (Windows).

.DESCRIPTION
    One command to create a virtual environment (.venv) at the repo root and install every
    Python dependency in requirements.txt. Idempotent: re-running reuses an existing .venv.

    Strategy:
      1. Prefer `uv` when on PATH (uv can provision Python 3.12 even when the host only has
         newer interpreters):  uv python install <ver> -> uv venv --python <ver> .venv
                               -> uv pip install -r requirements.txt
      2. Fallback to a system Python 3.12 then 3.11 via the `py` launcher (py -3.12 / py -3.11)
         or python3.12 / python3.11:  <python> -m venv .venv -> pip upgrade -> pip install.
      3. Validate the interpreter is >= 3.11 and < 3.13. If only 3.13+/3.10- is available and
         `uv` is absent, fail with an actionable message rather than building a broken venv.

    The Python version contract (>= 3.11, < 3.13) is required by Policy Weaver (plan §7).

    NOTE: Databricks CLI, Azure CLI, Bicep, and Power BI Desktop are NOT pip packages and are
    installed separately (see docs/prerequisites.md §6).

.PARAMETER PythonVersion
    Target Python version (default 3.12). Must be 3.11 or 3.12.

.PARAMETER Help
    Show this help text and exit.

.EXAMPLE
    .\scripts\setup_env.ps1

.EXAMPLE
    .\scripts\setup_env.ps1 -PythonVersion 3.11
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = '3.12',
    [Alias('h')]
    [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Show-Help {
    @'
Zava demo — Python environment setup (Windows / PowerShell)

USAGE:
    .\scripts\setup_env.ps1 [-PythonVersion <3.11|3.12>] [-Help]

WHAT IT DOES:
    Creates .venv at the repo root and installs requirements.txt. Idempotent.
    Prefers `uv` if present; otherwise uses a system Python 3.12/3.11.

OPTIONS:
    -PythonVersion   Target Python version (default: 3.12). Must be 3.11 or 3.12.
    -Help, -h        Show this help and exit.

AFTER SETUP:
    1. Activate:  .venv\Scripts\Activate.ps1
    2. Preflight: python scripts\preflight_checks.py
    3. Install the NON-pip tools (see docs/prerequisites.md §6):
         - Databricks CLI (standalone binary)
         - Azure CLI (`az`) + Bicep (`az bicep install`)
         - Power BI Desktop (Windows GUI)
'@ | Write-Host
}

if ($Help) {
    Show-Help
    exit 0
}

# --- Validate requested version is within contract (>= 3.11, < 3.13) ---------
$validVersions = @('3.11', '3.12')
if ($validVersions -notcontains $PythonVersion) {
    Write-Error "PythonVersion must be one of: $($validVersions -join ', ') (contract: >= 3.11, < 3.13). Got '$PythonVersion'."
    exit 1
}

# --- Resolve repo root (script lives in <repo>/scripts) ----------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$Requirements = Join-Path $RepoRoot 'requirements.txt'
$VenvDir   = Join-Path $RepoRoot '.venv'

if (-not (Test-Path -LiteralPath $Requirements)) {
    Write-Error "requirements.txt not found at '$Requirements'."
    exit 1
}

Write-Host "==> Zava demo Python environment setup" -ForegroundColor Cyan
Write-Host "    Repo root        : $RepoRoot"
Write-Host "    Target Python    : $PythonVersion (contract >= 3.11, < 3.13)"
Write-Host "    Virtual env (.venv): $VenvDir"
if (Test-Path -LiteralPath $VenvDir) {
    Write-Host "    .venv already exists -> reusing (idempotent re-install)." -ForegroundColor Yellow
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# $ErrorActionPreference = 'Stop' does NOT trap a native executable that exits
# non-zero — it only traps PowerShell errors. So every external command (uv, pip,
# python) MUST have its $LASTEXITCODE checked explicitly, otherwise the script
# would sail past a failed install and print a bogus success banner (BUG 2).
function Assert-LastExit([string]$What) {
    if ($LASTEXITCODE -ne 0) {
        Write-Error "FAILED (exit code $LASTEXITCODE): $What. The environment is incomplete; aborting."
        exit 1
    }
}

# uv's DEFAULT install strategy hardlinks wheels from its cache into the venv.
# On cloud-synced / network filesystems (OneDrive, Dropbox, SMB shares — and this
# repo commonly lives under 'OneDrive - Microsoft') hardlinking fails with
# 'os error 396 (incompatible hardlinks)', silently leaving a broken venv (BUG 1).
# Forcing copy link-mode is safe everywhere and only marginally slower, so we make
# it the unconditional default rather than asking the user to set anything.
$env:UV_LINK_MODE = 'copy'

$Activate = Join-Path $VenvDir 'Scripts\Activate.ps1'

# =============================================================================
# Path 1 — uv (preferred)
# =============================================================================
if (Test-Command 'uv') {
    Write-Host "==> Found 'uv' on PATH — using uv (preferred path)." -ForegroundColor Green

    Write-Host "--> uv python install $PythonVersion"
    uv python install $PythonVersion
    Assert-LastExit "uv python install $PythonVersion"

    Write-Host "--> uv venv --python $PythonVersion `"$VenvDir`""
    uv venv --python $PythonVersion $VenvDir
    Assert-LastExit "uv venv --python $PythonVersion '$VenvDir'"

    # --link-mode=copy (also set via UV_LINK_MODE above) avoids hardlink failures
    # on OneDrive / cloud-synced / network filesystems — see comment near top.
    Write-Host "--> uv pip install --link-mode=copy -r `"$Requirements`""
    uv pip install --python $VenvDir --link-mode=copy -r $Requirements
    Assert-LastExit "uv pip install --link-mode=copy -r '$Requirements'"

    Write-Host ""
    Write-Host "==> uv install command completed; verifying below before declaring ready." -ForegroundColor Cyan
}
else {
    # =========================================================================
    # Path 2 — system Python fallback (no uv)
    # =========================================================================
    Write-Host "==> 'uv' not found — falling back to a system Python interpreter." -ForegroundColor Yellow

    # Preference order: requested version first, then the other supported version.
    $candidates = @($PythonVersion) + ($validVersions | Where-Object { $_ -ne $PythonVersion })
    $chosen = $null

    foreach ($ver in $candidates) {
        # Try the py launcher first (Windows-native, handles multiple versions).
        if (Test-Command 'py') {
            & py "-$ver" -c "import sys; sys.exit(0)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $chosen = @('py', "-$ver")
                break
            }
        }
        # Then try a versioned python executable on PATH (e.g. python3.12).
        $exe = "python$ver"
        if (Test-Command $exe) {
            $chosen = @($exe)
            break
        }
    }

    if (-not $chosen) {
        Write-Error @"
No supported Python interpreter (3.11 or 3.12) was found, and 'uv' is not installed.
The host default Python may be 3.13+ or 3.10-, which is OUTSIDE the >= 3.11, < 3.13 contract
required by Policy Weaver (plan §7).

Choose ONE of:
  * Install uv (recommended — provisions 3.12 automatically):
        winget install --id=astral-sh.uv  -e
        # or:  irm https://astral.sh/uv/install.ps1 | iex
    then re-run:  .\scripts\setup_env.ps1
  * Install Python 3.12 from https://www.python.org/ and re-run this script.
"@
        exit 1
    }

    $pyCmd = $chosen
    Write-Host "--> Using interpreter: $($pyCmd -join ' ')"

    # Split the chosen command into exe + (optional) args. A versioned python
    # executable (e.g. python3.12) is a single-element array, so guard the slice:
    # `$pyCmd[1..($pyCmd.Length - 1)]` would evaluate `1..0` for length 1, which
    # under Set-StrictMode -Version Latest throws "Index was outside the bounds of
    # the array." Build an args array only when there is more than one element and
    # splat it (@pyArgs expands to nothing when empty).
    $pyExe  = $pyCmd[0]
    $pyArgs = if ($pyCmd.Length -gt 1) { $pyCmd[1..($pyCmd.Length - 1)] } else { @() }

    # Validate the chosen interpreter is >= 3.11 and < 3.13.
    $verCheck = & $pyExe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
    Write-Host "--> Detected Python version: $verCheck"
    $parts = $verCheck.Split('.')
    $maj = [int]$parts[0]; $min = [int]$parts[1]
    $okVersion = ($maj -eq 3) -and ($min -ge 11) -and ($min -lt 13)
    if (-not $okVersion) {
        Write-Error "Interpreter '$($pyCmd -join ' ')' reports Python $verCheck, which is outside the >= 3.11, < 3.13 contract. Install uv or Python 3.12."
        exit 1
    }

    Write-Host "--> Creating virtual environment at '$VenvDir'"
    & $pyExe @pyArgs -m venv $VenvDir
    Assert-LastExit "python -m venv '$VenvDir'"

    $VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Error "Virtual environment python not found at '$VenvPython' after venv creation."
        exit 1
    }

    Write-Host "--> Upgrading pip in the virtual environment"
    & $VenvPython -m pip install --upgrade pip
    Assert-LastExit "pip install --upgrade pip"

    Write-Host "--> pip install -r `"$Requirements`""
    & $VenvPython -m pip install -r $Requirements
    Assert-LastExit "pip install -r '$Requirements'"

    Write-Host ""
    Write-Host "==> pip install command completed (system Python $verCheck); verifying below before declaring ready." -ForegroundColor Cyan
}

# =============================================================================
# Post-install verification — never claim success on an incomplete env (BUG 2)
# =============================================================================
$VerifyPython = Join-Path $VenvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VerifyPython)) {
    Write-Error "Post-install check FAILED: venv python not found at '$VerifyPython'. The environment is incomplete."
    exit 1
}

Write-Host ""
Write-Host "==> Verifying the environment (smoke import of key packages)..." -ForegroundColor Cyan
& $VerifyPython -c "import sempy_labs, requests, yaml, pandas, pyarrow"
if ($LASTEXITCODE -ne 0) {
    Write-Error @"
Post-install verification FAILED: one or more key packages did not import
(sempy_labs / requests / yaml / pandas / pyarrow). The virtual environment is
INCOMPLETE — do NOT use it. Re-run this script after resolving the install error
above. (On OneDrive/cloud-synced paths, ensure uv uses copy link-mode.)
"@
    exit 1
}

# `fab` (ms-fabric-cli) installs as a console script in the venv Scripts/ dir.
$FabExe = Join-Path $VenvDir 'Scripts\fab.exe'
if (-not (Test-Path -LiteralPath $FabExe)) {
    Write-Error "Post-install verification FAILED: 'fab' (ms-fabric-cli) not found at '$FabExe'. The environment is incomplete."
    exit 1
}
Write-Host "    OK: sempy_labs, requests, yaml, pandas, pyarrow importable; 'fab' present." -ForegroundColor Green
Write-Host ""
Write-Host "==> Environment ready (smoke verification passed)." -ForegroundColor Green

# =============================================================================
# Next steps
# =============================================================================
Write-Host ""
Write-Host "NEXT STEPS" -ForegroundColor Cyan
Write-Host "  1. Activate the environment:"
Write-Host "         $Activate"
Write-Host "  2. Run the read-only preflight checks:"
Write-Host "         python scripts\preflight_checks.py"
Write-Host "  3. Install the NON-pip tools (see docs/prerequisites.md §6):"
Write-Host "         - Databricks CLI : https://learn.microsoft.com/en-us/azure/databricks/dev-tools/cli/"
Write-Host "                            winget install Databricks.DatabricksCLI"
Write-Host "         - Azure CLI (az) : https://learn.microsoft.com/en-us/cli/azure/install-azure-cli"
Write-Host "         - Bicep          : az bicep install   (confirm: az bicep version)"
Write-Host "         - Power BI Desktop (Windows GUI, for PBIP authoring)"
Write-Host ""
Write-Host "Done." -ForegroundColor Green
