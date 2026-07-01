$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogDir = Join-Path $ProjectRoot "runtime\logs"
$OutLog = Join-Path $LogDir "daily_report.out.log"
$ErrLog = Join-Path $LogDir "daily_report.err.log"
$Python = if ($env:TRADERBOT_PYTHON) {
    $env:TRADERBOT_PYTHON
} else {
    "C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $ProjectRoot

$StartedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"
Add-Content -Path $OutLog -Value "[$StartedAt] Starting TraderBot daily report"

& $Python -m traderbot.cli.reports --config config/watchers.json --skip-non-trading-day >> $OutLog 2>> $ErrLog
$ExitCode = $LASTEXITCODE

$FinishedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"
if ($ExitCode -eq 0) {
    Add-Content -Path $OutLog -Value "[$FinishedAt] TraderBot daily report finished successfully"
} else {
    Add-Content -Path $ErrLog -Value "[$FinishedAt] TraderBot daily report failed with exit code $ExitCode"
}

exit $ExitCode
