param(
    [int]$Limit = 200,
    [switch]$NoDryRun,
    [switch]$NoStubMode,
    [string]$StubPath = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LogPath = Join-Path $Root "data\outcome_resolver.log"

if (-not (Test-Path $Python)) {
    throw "Python executable not found at $Python"
}

if (-not (Test-Path (Split-Path -Parent $LogPath))) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $LogPath) | Out-Null
}

$Args = @("-m", "prediction_bot", "resolve-outcomes", "--limit", "$Limit")
if ($NoDryRun) {
    $Args += "--no-dry-run"
}
if ($NoStubMode) {
    $Args += "--no-stub-mode"
}
if ($StubPath -ne "") {
    $Args += @("--stub-path", $StubPath)
}

"[$((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK'))] resolver start limit=$Limit dry_run=$(-not $NoDryRun) stub_mode=$(-not $NoStubMode)" | Out-File -FilePath $LogPath -Append -Encoding utf8
& $Python @Args *>&1 | Tee-Object -FilePath $LogPath -Append
$exitCode = $LASTEXITCODE
"[$((Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK'))] resolver exit_code=$exitCode" | Out-File -FilePath $LogPath -Append -Encoding utf8
exit $exitCode
