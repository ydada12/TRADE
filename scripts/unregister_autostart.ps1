# unregister_autostart.ps1 - stop and remove auto-start for the monitor.
# ASCII-only on purpose (Windows PowerShell 5.1 + UTF-8 without BOM = mojibake).

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "DualTrackSignalMonitor"
)

$ErrorActionPreference = "Continue"

function Ok   { param([string]$m) Write-Host "[  ok   ] $m" -ForegroundColor Green }
function Info { param([string]$m) Write-Host "[ info  ] $m" -ForegroundColor Cyan }

# 1) stop the supervisor gracefully via its flag file
$flag = Join-Path $ProjectRoot "STOP_MONITOR.flag"
"stopped at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Set-Content -Path $flag -Encoding UTF8
Ok "STOP flag written: $flag"

# 2) stop + delete the scheduled task
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Ok "scheduled task removed: $TaskName"
} else {
    Info "scheduled task not found: $TaskName"
}

# 3) remove the startup shortcut
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "DualTrackSignalMonitor.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force; Ok "startup shortcut removed" }

# 4) kill any leftover monitor processes started from this project
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$ProjectRoot*" }
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Ok "killed python pid $($p.ProcessId)"
}

Info "to re-enable: run scripts\register_autostart.ps1 (and delete STOP_MONITOR.flag)"
