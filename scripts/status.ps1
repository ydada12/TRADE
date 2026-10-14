# status.ps1 - health check for the running monitor.
# ASCII-only on purpose (Windows PowerShell 5.1 + UTF-8 without BOM = mojibake).

param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "DualTrackSignalMonitor",
    [int]$TailLines = 15
)

function Section { param([string]$m) Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Cyan }

Section "scheduled task"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host ("state        : {0}" -f $task.State)
    Write-Host ("last run     : {0}" -f $info.LastRunTime)
    Write-Host ("last result  : {0}" -f $info.LastTaskResult)
    Write-Host ("next run     : {0}" -f $info.NextRunTime)
} else {
    Write-Host "not registered. run scripts\register_autostart.ps1" -ForegroundColor Yellow
}

Section "processes"
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$ProjectRoot*" }
if ($procs) {
    $procs | ForEach-Object { Write-Host ("pid {0}  started {1}" -f $_.ProcessId, $_.CreationDate) }
} else {
    Write-Host "no monitor process found for $ProjectRoot" -ForegroundColor Yellow
}

Section "stop flag"
$flag = Join-Path $ProjectRoot "STOP_MONITOR.flag"
if (Test-Path $flag) {
    Write-Host "PRESENT - supervisor will exit and stay down:" -ForegroundColor Yellow
    Get-Content $flag -Encoding UTF8
} else {
    Write-Host "absent (normal)"
}

Section "keys"
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
    $content = Get-Content $envFile -Raw -Encoding UTF8
    # ASCII-only patterns on purpose: this file is read as ANSI by PS 5.1,
    # so any non-ASCII literal here would be mangled.
    # A real key is "sk-" / "SCT" followed by 16+ ASCII chars; the template
    # placeholders contain non-ASCII characters and therefore never match.
    $hasDs  = $content -match "DEEPSEEK_API_KEY=\s*sk-[A-Za-z0-9_-]{16,}"
    $hasSct = $content -match "SERVERCHAN_SENDKEY=\s*SCT[A-Za-z0-9]{16,}"
    Write-Host (".env exists      : yes")
    Write-Host ("  DEEPSEEK_API_KEY filled      : {0}" -f $hasDs)
    Write-Host ("  SERVERCHAN_SENDKEY filled    : {0}" -f $hasSct)
} else {
    Write-Host ".env not found" -ForegroundColor Yellow
}
Write-Host ("machine env  DEEPSEEK_API_KEY   : {0}" -f [bool]$env:DEEPSEEK_API_KEY)
Write-Host ("machine env  SERVERCHAN_SENDKEY : {0}" -f [bool]$env:SERVERCHAN_SENDKEY)

Section "latest logs"
$logDir = Join-Path $ProjectRoot "logs"
if (Test-Path $logDir) {
    # logs are UTF-8; Windows PowerShell 5.1 defaults to ANSI and would mojibake them
    Get-ChildItem $logDir -Filter *.log | Sort-Object LastWriteTime -Descending |
        Select-Object -First 2 | ForEach-Object {
            Write-Host ("--- {0}  (modified {1}, {2:N0} bytes) ---" -f $_.Name, $_.LastWriteTime, $_.Length)
            Get-Content $_.FullName -Tail $TailLines -Encoding UTF8 | ForEach-Object { Write-Host "  $_" }
        }
} else {
    Write-Host "no logs directory yet" -ForegroundColor Yellow
}

Section "state"
$stateFile = Join-Path $ProjectRoot "state\state.json"
if (Test-Path $stateFile) {
    $json = Get-Content $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "trend states:"
    $json.trend_state.PSObject.Properties | ForEach-Object {
        Write-Host ("  {0}: {1} (as of week {2})" -f $_.Name, $_.Value.state, $_.Value.as_of_week)
    }
    Write-Host ("events tracked : {0}" -f @($json.events.PSObject.Properties).Count)
    Write-Host ("signals sent   : {0}" -f @($json.sent.PSObject.Properties).Count)
} else {
    Write-Host "no state file yet (run the monitor once)"
}

Write-Host ""
