param(
    [string]$TaskName = "PredictionBotDailyPaperLoop",
    [string]$StartTime = "09:00"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run-daily-paper-loop.bat"

if (-not (Test-Path $Runner)) {
    throw "Runner script not found at $Runner"
}

$taskCmd = "`"$Runner`""

$result = schtasks /Create /TN $TaskName /SC DAILY /ST $StartTime /TR $taskCmd /F 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "schtasks create failed: $result"
}

Write-Output "task_installed name=$TaskName time=$StartTime"
