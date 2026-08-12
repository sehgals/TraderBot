$ErrorActionPreference = "Stop"

$TaskName = "TraderBot_Watcher_Supervisor"
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Set-ScheduledTask `
    -TaskName $TaskName `
    -Principal $principal `
    -Settings $settings | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
if ($task.Principal.UserId -notin @("SYSTEM", "NT AUTHORITY\SYSTEM")) {
    throw "The task principal was not changed to SYSTEM."
}
if ($task.Principal.LogonType -ne "ServiceAccount") {
    throw "The task is not configured for service-account logon."
}

Write-Output "Configured $TaskName to run as SYSTEM without an interactive login."
