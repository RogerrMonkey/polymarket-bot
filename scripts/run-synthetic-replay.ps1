param(
    [int]$Days = 14,
    [int]$LoopsPerDay = 12,
    [int]$CandidatesPerLoop = 5,
    [double]$ApproveRate = 0.45,
    [double]$ResolvedRate = 0.55,
    [int]$Seed = 7
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Python executable not found at $Python"
}

& $Python -m prediction_bot replay-synthetic --days $Days --loops-per-day $LoopsPerDay --candidates-per-loop $CandidatesPerLoop --approve-rate $ApproveRate --resolved-rate $ResolvedRate --seed $Seed
exit $LASTEXITCODE
