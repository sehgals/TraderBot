param([switch]$Install)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$reportPath = Join-Path $projectRoot 'runtime/dashboard-service-verification.json'
$report = [ordered]@{ checked_at = (Get-Date).ToUniversalTime().ToString('o'); passed = $false }
function Get-ServiceIdentity($taskName, $module) {
    $task = Get-ScheduledTask -TaskName $taskName
    if ($task.Principal.UserId -notin @('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -or
        $task.Principal.LogonType -ne 'ServiceAccount' -or $task.Principal.RunLevel -ne 'Highest') {
        throw "$taskName does not have the required SYSTEM principal."
    }
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -match $module })
    if ($processes.Count -ne 1) { throw "Expected one $taskName pythonw.exe process; found $($processes.Count)." }
    $process = $processes[0]
    $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner
    if ($process.SessionId -ne 0 -or $owner.User -ne 'SYSTEM') {
        throw "$taskName is not running as SYSTEM in session 0."
    }
    return [ordered]@{ task = $taskName; principal = $task.Principal.UserId;
        logon_type = [string]$task.Principal.LogonType; run_level = [string]$task.Principal.RunLevel;
        pid = $process.ProcessId; session = $process.SessionId; owner = $owner.User }
}
function Wait-Dashboard {
    $deadline = (Get-Date).AddSeconds(40)
    do {
        try {
            $response = Invoke-WebRequest 'http://127.0.0.1:8765/' -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw 'Dashboard did not respond within 40 seconds.'
}
try {
    $report.supervisor_before = Get-ServiceIdentity 'TraderBot_Watcher_Supervisor' 'traderbot\.cli\.supervisor|watcher_supervisor\.py'
    if ($Install) { & (Join-Path $PSScriptRoot 'configure_dashboard_task.ps1') -Start }
    Wait-Dashboard
    $report.dashboard_before = Get-ServiceIdentity 'TraderBot_Dashboard' 'traderbot\.cli\.dashboard_service'
    Stop-ScheduledTask -TaskName 'TraderBot_Dashboard'
    $deadline = (Get-Date).AddSeconds(15)
    while (Get-Process -Id $report.dashboard_before.pid -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $deadline) { throw 'Dashboard did not stop.' }
        Start-Sleep -Milliseconds 250
    }
    Start-ScheduledTask -TaskName 'TraderBot_Dashboard'
    Wait-Dashboard
    $report.dashboard_after = Get-ServiceIdentity 'TraderBot_Dashboard' 'traderbot\.cli\.dashboard_service'
    $report.supervisor_after = Get-ServiceIdentity 'TraderBot_Watcher_Supervisor' 'traderbot\.cli\.supervisor|watcher_supervisor\.py'
    if ($report.dashboard_before.pid -eq $report.dashboard_after.pid) { throw 'Dashboard PID did not change on restart.' }
    if ($report.supervisor_before.pid -ne $report.supervisor_after.pid) { throw 'Supervisor PID changed during verification.' }
    $listeners = @(Get-NetTCPConnection -State Listen -OwningProcess $report.dashboard_after.pid)
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1' -or $listeners[0].LocalPort -ne 8765) {
        throw 'Unexpected dashboard listener; expected only 127.0.0.1:8765.'
    }
    $report.listener = '127.0.0.1:8765'
    $report.http_status = 200
    $report.logs_present = Test-Path (Join-Path $projectRoot 'runtime/logs/dashboard-service.log')
    if (-not $report.logs_present) { throw 'Service log was not created.' }
    $report.passed = $true
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
}
