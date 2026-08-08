$ErrorActionPreference = "Stop"

$TaskName = "TraderBot_Daily_Report"
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

Set-ScheduledTask -TaskName $TaskName -Principal $principal | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
if ($task.Principal.UserId -notin @("SYSTEM", "NT AUTHORITY\SYSTEM")) {
    throw "The task principal was not changed to SYSTEM."
}

Write-Output "Configured $TaskName to run as SYSTEM."
