# run_forever.ps1 - supervisor loop for the signal monitor.
# ASCII-only on purpose: Windows PowerShell 5.1 mis-decodes UTF-8 .ps1 without BOM.
#
# Keeps the Python monitor alive forever:
#   - restarts it if it crashes (exponential backoff, capped)
#   - restarts it if it exits cleanly but unexpectedly
#   - stops only when the STOP flag file exists
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File run_forever.ps1

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$MaxBackoffSeconds = 60
)

$ErrorActionPreference = "Continue"

$logDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

$supervisorLog = Join-Path $logDir ("supervisor-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$stopFlag      = Join-Path $ProjectRoot "STOP_MONITOR.flag"
$pythonExe     = Join-Path $ProjectRoot "scripts\python_resolve.ps1"

function Write-Sup {
    param([string]$Message)
    $line = "{0} [supervisor] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $supervisorLog -Value $line -Encoding UTF8
    Write-Host $line
}

# Resolve a working Python interpreter (machine install preferred).
function Resolve-Python {
    $candidates = @(
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Program Files\Python313\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    # fall back to whatever is on PATH, but reject the 0-byte Store stub
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Path $cmd.Source)) {
        $len = (Get-Item $cmd.Source).Length
        if ($len -gt 100000) { return $cmd.Source }
    }
    # last resort: registry
    foreach ($root in @("HKLM:\SOFTWARE\Python\PythonCore", "HKCU:\SOFTWARE\Python\PythonCore")) {
        if (Test-Path $root) {
            foreach ($ver in Get-ChildItem $root -ErrorAction SilentlyContinue) {
                $ip = Join-Path $ver.PSPath "InstallPath"
                if (Test-Path $ip) {
                    $exe = (Get-ItemProperty $ip -ErrorAction SilentlyContinue).'(default)'
                    if ($exe) {
                        $cand = Join-Path $exe "python.exe"
                        if (Test-Path $cand) { return $cand }
                    }
                }
            }
        }
    }
    return $null
}

$python = Resolve-Python
if (-not $python) {
    Write-Sup "FATAL: no usable Python found. Run scripts\install.ps1 first."
    exit 1
}
Write-Sup ("supervisor starting. python={0} root={1}" -f $python, $ProjectRoot)

$backoff = 5
while ($true) {
    if (Test-Path $stopFlag) {
        Write-Sup "STOP flag detected. Supervisor exiting."
        break
    }

    $started = Get-Date
    Write-Sup "launching: python run.py --loop"
    try {
        & $python (Join-Path $ProjectRoot "run.py") --loop
        $exitCode = $LASTEXITCODE
    } catch {
        $exitCode = -1
        Write-Sup ("exception while running monitor: {0}" -f $_.Exception.Message)
    }
    $ranFor = ((Get-Date) - $started).TotalSeconds
    Write-Sup ("monitor exited with code {0} after {1:N0}s" -f $exitCode, $ranFor)

    # If it ran healthy for a long time, reset the backoff.
    if ($ranFor -gt 300) { $backoff = 5 }

    Write-Sup ("restarting in {0}s" -f $backoff)
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min($backoff * 2, $MaxBackoffSeconds)
}
