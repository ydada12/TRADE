# register_autostart.ps1 - register the monitor for auto-start on Windows.
# ASCII-only on purpose (Windows PowerShell 5.1 + UTF-8 without BOM = mojibake).
#
# Creates a Scheduled Task named "DualTrackSignalMonitor" that:
#   - starts at logon (default) or at boot (-RunAs system)
#   - auto-restarts the supervisor if it dies
#   - has no execution time limit (it is meant to run forever)
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_autostart.ps1
#   ... -RunAs system     # start before logon; requires keys in .env (see README)

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "DualTrackSignalMonitor",
    [ValidateSet("user", "system")]
    [string]$RunAs = "user",
    [int]$LogonDelaySeconds = 60,
    [switch]$NoStartupShortcut
)

$ErrorActionPreference = "Stop"

function Ok   { param([string]$m) Write-Host "[  ok   ] $m" -ForegroundColor Green }
function Info { param([string]$m) Write-Host "[ info  ] $m" -ForegroundColor Cyan }
function Fail { param([string]$m) Write-Host "[ fail  ] $m" -ForegroundColor Red }

$runForever = Join-Path $ProjectRoot "scripts\run_forever.ps1"
if (-not (Test-Path $runForever)) { Fail "missing $runForever"; exit 1 }

$pythonHint = @(
    "C:\Program Files\Python313\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python311\python.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pythonHint) {
    Fail "no Python found under C:\Program Files. Run scripts\install.ps1 first."
    exit 1
}
Info "python: $pythonHint"

# ---- action ----
$argLine = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -ProjectRoot "{1}"' -f $runForever, $ProjectRoot
$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argLine -WorkingDirectory $ProjectRoot

# ---- triggers ----
$triggers = @()
if ($RunAs -eq "user") {
    $t = New-ScheduledTaskTrigger -AtLogOn -User ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME)
    $t.Delay = "PT{0}S" -f $LogonDelaySeconds
    $triggers += $t
    Info "trigger: at logon (delay ${LogonDelaySeconds}s)"
} else {
    $triggers += New-ScheduledTaskTrigger -AtStartup
    Info "trigger: at system startup"
}

# ---- settings: restart on failure, never time out ----
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -Priority 4

# ---- principal ----
if ($RunAs -eq "system") {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive -RunLevel Highest
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Force `
    -Description "Read-only trading signal monitor. No trading API. Sends WeChat alerts only." | Out-Null
Ok "scheduled task registered: $TaskName"

# ---- belt and braces: startup folder shortcut ----
if (-not $NoStartupShortcut -and $RunAs -eq "user") {
    try {
        $startup = [Environment]::GetFolderPath("Startup")
        $lnk = Join-Path $startup "DualTrackSignalMonitor.lnk"
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath = "powershell.exe"
        $sc.Arguments = $argLine
        $sc.WorkingDirectory = $ProjectRoot
        $sc.WindowStyle = 7
        $sc.Description = "Read-only trading signal monitor"
        $sc.Save()
        Ok "startup shortcut created: $lnk"
    } catch {
        Info "startup shortcut skipped: $($_.Exception.Message)"
    }
}

# ---- start it right now ----
Start-ScheduledTask -TaskName $TaskName
Ok "task started"
Info "verify with: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\status.ps1"
