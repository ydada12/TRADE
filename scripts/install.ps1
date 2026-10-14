# install.ps1 - one-shot setup for the dual-track signal monitor.
# ASCII-only on purpose (Windows PowerShell 5.1 + UTF-8 without BOM = mojibake).
#
# What it does:
#   1. detects / installs Python 3.11+
#   2. pip install -r requirements.txt
#   3. creates config.yaml and .env from the .example templates (never overwrites)
#   4. runs the built-in self check
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$SkipPythonInstall
)

$ErrorActionPreference = "Stop"

function Info  { param([string]$m) Write-Host "[install] $m" -ForegroundColor Cyan }
function Ok    { param([string]$m) Write-Host "[  ok   ] $m" -ForegroundColor Green }
function Warn  { param([string]$m) Write-Host "[ warn  ] $m" -ForegroundColor Yellow }
function Fail  { param([string]$m) Write-Host "[ fail  ] $m" -ForegroundColor Red }

Info "project root: $ProjectRoot"
if (-not (Test-Path (Join-Path $ProjectRoot "run.py"))) {
    Fail "run.py not found under $ProjectRoot - wrong -ProjectRoot?"
    exit 1
}

# ---------------------------------------------------------------- 1. Python
function Resolve-Python {
    $candidates = @(
        "C:\Program Files\Python313\python.exe",
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source) -and ((Get-Item $cmd.Source).Length -gt 100000)) {
        return $cmd.Source
    }
    return $null
}

$python = Resolve-Python
if (-not $python -and -not $SkipPythonInstall) {
    Warn "no usable Python found; installing Python 3.12 via winget"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Fail "winget not available. Install Python 3.11+ manually from python.org, then re-run."
        exit 1
    }
    & winget install --id Python.Python.3.12 -e --silent `
        --accept-package-agreements --accept-source-agreements --scope machine --disable-interactivity
    $python = Resolve-Python
}
if (-not $python) {
    Fail "Python still not found. Install Python 3.11+ and re-run."
    exit 1
}
$ver = & $python --version 2>&1
Ok "python: $python ($ver)"

# ---------------------------------------------------------------- 2. deps
Info "installing Python dependencies"
& $python -m pip install --no-input --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (exit $LASTEXITCODE)"; exit 1 }
Ok "dependencies installed"

# ---------------------------------------------------------------- 3. config
$cfg      = Join-Path $ProjectRoot "config.yaml"
$cfgEx    = Join-Path $ProjectRoot "config.example.yaml"
$envFile  = Join-Path $ProjectRoot ".env"
$envEx    = Join-Path $ProjectRoot ".env.example"

if (-not (Test-Path $cfg)) {
    Copy-Item $cfgEx $cfg
    Ok "created config.yaml from template"
} else {
    Warn "config.yaml already exists - left untouched"
}

if (-not (Test-Path $envFile)) {
    Copy-Item $envEx $envFile
    Ok "created .env from template (fill in your keys)"
} else {
    Warn ".env already exists - left untouched"
}

New-Item -ItemType Directory -Path (Join-Path $ProjectRoot "logs")  -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $ProjectRoot "state") -Force | Out-Null

# ---------------------------------------------------------------- 4. self check
Info "running self check"
& $python (Join-Path $ProjectRoot "run.py") --check

Write-Host ""
Ok "install finished."
Write-Host "next steps:" -ForegroundColor Cyan
Write-Host "  1) put your keys into $envFile  (or set system env vars)"
Write-Host "  2) powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_autostart.ps1"
Write-Host "  3) powershell -NoProfile -ExecutionPolicy Bypass -File scripts\status.ps1"
