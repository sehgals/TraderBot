param(
    [string]$Pythonw = 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\pythonw.exe',
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$Start,
    [switch]$Restart
)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = 'TraderBot_Dashboard'
if (-not (Test-Path -LiteralPath $Pythonw -PathType Leaf)) {
    throw "Pythonw was not found: $Pythonw"
}
if ([IO.Path]::GetFileName($Pythonw) -ne 'pythonw.exe') {
    throw 'The dashboard task requires pythonw.exe for windowless execution.'
}
$python = Join-Path (Split-Path $Pythonw) 'python.exe'
Push-Location $projectRoot
try {
    & $python -c "from traderbot.cli.dashboard import main; import uvicorn; print('Dashboard dependencies verified')"
    if ($LASTEXITCODE -ne 0) { throw 'Install dashboard dependencies before configuring the task.' }
} finally { Pop-Location }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq 'Running') {
    if (-not $Restart) { throw 'Use -Restart to restart the running dashboard.' }
    Stop-ScheduledTask -TaskName $taskName
}
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$action = New-ScheduledTaskAction -Execute $Pythonw `
    -Argument "-m traderbot.cli.dashboard_service --port $Port" -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description 'Read-only loopback TraderBot dashboard' -Force | Out-Null
$task = Get-ScheduledTask -TaskName $taskName
if ($task.Principal.UserId -notin @('SYSTEM', 'NT AUTHORITY\SYSTEM', 'S-1-5-18') -or
    $task.Principal.LogonType -ne 'ServiceAccount' -or $task.Principal.RunLevel -ne 'Highest') {
    throw 'Dashboard task service identity verification failed.'
}
if ($Start -or $Restart) { Start-ScheduledTask -TaskName $taskName }
Write-Output "Configured $taskName as SYSTEM / ServiceAccount / Highest at http://127.0.0.1:$Port"
