param(
    [int]$Cycles = 12,
    [int]$IntervalSeconds = 300,
    [int]$LimitPerVenue = 120,
    [int]$TopNForRisk = 5
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LogPath = Join-Path $Root "data\paper_loop_daily.log"

if (-not (Test-Path $Python)) {
    throw "Python executable not found at $Python"
}

if (-not (Test-Path (Split-Path -Parent $LogPath))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $LogPath) | Out-Null
}

$ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssK"
"[$ts] daily paper loop start cycles=$Cycles interval_seconds=$IntervalSeconds" | Out-File -FilePath $LogPath -Append -Encoding utf8

$loopArgs = @(
    "-m", "prediction_bot", "paper-loop",
    "--cycles", "$Cycles",
    "--interval-seconds", "$IntervalSeconds",
    "--limit-per-venue", "$LimitPerVenue",
    "--top-n-for-risk", "$TopNForRisk",
    "--dry-run"
)

& $Python @loopArgs *>&1 | Tee-Object -FilePath $LogPath -Append
$loopExit = $LASTEXITCODE
"[$((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK'))] paper-loop exit_code=$loopExit" | Out-File -FilePath $LogPath -Append -Encoding utf8

$scoreArgs = @("-m", "prediction_bot", "scorecard", "--check-gates")
& $Python @scoreArgs *>&1 | Tee-Object -FilePath $LogPath -Append
$scoreExit = $LASTEXITCODE
"[$((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK'))] scorecard exit_code=$scoreExit" | Out-File -FilePath $LogPath -Append -Encoding utf8

if ($loopExit -ne 0) {
    exit $loopExit
}
exit 0
