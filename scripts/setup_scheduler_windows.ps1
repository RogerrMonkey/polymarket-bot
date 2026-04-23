# =============================================================================
# setup_scheduler_windows.ps1 - Register the PolymarketPaperLoop daily task,
# plus two companion tasks that bring Cloudflare WARP up (02:55 UTC) and
# tear it down (04:00 UTC) around the paper-loop window.
# =============================================================================
#
# The bot is DNS-blocked from India without WARP; historically the single
# biggest operational risk has been WARP being off at 03:00 UTC. We now
# bracket the paper-loop with OS-level warp-cli connect/disconnect tasks so
# the operator does not have to remember to toggle WARP each morning.
#
# USAGE:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\scripts\setup_scheduler_windows.ps1
#
# VERIFY:
#   schtasks /Query /TN PolymarketPaperLoop       /V /FO LIST
#   schtasks /Query /TN PolymarketWARPConnect     /V /FO LIST
#   schtasks /Query /TN PolymarketWARPDisconnect  /V /FO LIST
# =============================================================================

param(
    [string]$TaskName        = "PolymarketPaperLoop",
    [string]$TriggerUTC      = "03:00",
    [string]$WarpUpUTC       = "02:55",
    [string]$WarpDownUTC     = "04:00",
    [int]   $Cycles          = 20,
    [int]   $Interval        = 60
)

$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$LogsDir     = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir | Out-Null
}

$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    throw "python not found on PATH. Activate your venv or add Python to PATH first."
}

$PowerShellExe = (Get-Command powershell -ErrorAction SilentlyContinue).Source
if (-not $PowerShellExe) {
    $PowerShellExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
}

function Register-Task {
    param(
        [string]$Name,
        [string]$Description,
        [string]$TimeUTC,
        [string]$ExePath,
        [string]$ExeArgs,
        [string]$WorkDir
    )

    $Today = (Get-Date).ToString("yyyy-MM-dd")
    $Start = "${Today}T${TimeUTC}:00Z"

    $scheduler = New-Object -ComObject "Schedule.Service"
    $scheduler.Connect()

    $td = $scheduler.NewTask(0)
    $td.RegistrationInfo.Description          = $Description
    $td.RegistrationInfo.Author               = $env:USERNAME
    $td.Settings.Enabled                      = $true
    $td.Settings.Hidden                       = $false
    $td.Settings.StartWhenAvailable           = $true
    $td.Settings.RunOnlyIfNetworkAvailable    = $false
    $td.Settings.ExecutionTimeLimit           = "PT2H"
    $td.Settings.RunOnlyIfIdle                = $false
    $td.Principal.LogonType                   = 3
    $td.Principal.RunLevel                    = 0

    $trg = $td.Triggers.Create(2)
    $trg.StartBoundary = $Start
    $trg.DaysInterval  = 1
    $trg.Enabled       = $true

    $act = $td.Actions.Create(0)
    $act.Path             = $ExePath
    $act.Arguments        = $ExeArgs
    $act.WorkingDirectory = $WorkDir

    $rootFolder = $scheduler.GetFolder("\")
    $rootFolder.RegisterTaskDefinition($Name, $td, 6, $null, $null, 3) | Out-Null
    Write-Host "  [OK] $Name  ($TimeUTC UTC daily)"
}

# Build the warp connect/disconnect inline PowerShell snippets. Each one
# tolerates warp-cli being missing (logs a hint, exits 0) so the scheduled
# task never comes up red just because the CLI is not installed.

$WarpConnectLog    = Join-Path $LogsDir "warp_connect.log"
$WarpDisconnectLog = Join-Path $LogsDir "warp_disconnect.log"

$WarpConnectCmd = @"
try {
    if (Get-Command warp-cli -ErrorAction SilentlyContinue) {
        & warp-cli connect 2>&1 | Out-File -Append -Encoding utf8 '$WarpConnectLog'
    } else {
        'WARP CLI not found - install from https://1.1.1.1' | Out-File -Append -Encoding utf8 '$WarpConnectLog'
    }
} catch {
    \$_.Exception.Message | Out-File -Append -Encoding utf8 '$WarpConnectLog'
}
"@

$WarpDisconnectCmd = @"
try {
    if (Get-Command warp-cli -ErrorAction SilentlyContinue) {
        & warp-cli disconnect 2>&1 | Out-File -Append -Encoding utf8 '$WarpDisconnectLog'
    } else {
        'WARP CLI not found - skip disconnect' | Out-File -Append -Encoding utf8 '$WarpDisconnectLog'
    }
} catch {
    \$_.Exception.Message | Out-File -Append -Encoding utf8 '$WarpDisconnectLog'
}
"@

$WarpConnectArgs    = "-NoProfile -ExecutionPolicy Bypass -Command `"$WarpConnectCmd`""
$WarpDisconnectArgs = "-NoProfile -ExecutionPolicy Bypass -Command `"$WarpDisconnectCmd`""

Write-Host "Project root : $ProjectRoot"
Write-Host "Python       : $PythonExe"
Write-Host "PowerShell   : $PowerShellExe"
Write-Host ""
Write-Host "Registering tasks..."

Register-Task `
    -Name        "PolymarketWARPConnect" `
    -Description "Bring Cloudflare WARP up before the paper-loop run" `
    -TimeUTC     $WarpUpUTC `
    -ExePath     $PowerShellExe `
    -ExeArgs     $WarpConnectArgs `
    -WorkDir     $ProjectRoot

Register-Task `
    -Name        $TaskName `
    -Description "Polymarket Bot - daily paper-loop analysis run (WARP bracketed by WARPConnect/Disconnect tasks)" `
    -TimeUTC     $TriggerUTC `
    -ExePath     $PythonExe `
    -ExeArgs     "-m prediction_bot paper-loop --cycles $Cycles --interval $Interval" `
    -WorkDir     $ProjectRoot

Register-Task `
    -Name        "PolymarketWARPDisconnect" `
    -Description "Tear Cloudflare WARP down after the paper-loop finishes" `
    -TimeUTC     $WarpDownUTC `
    -ExePath     $PowerShellExe `
    -ExeArgs     $WarpDisconnectArgs `
    -WorkDir     $ProjectRoot

Write-Host ""
Write-Host "Daily schedule (UTC):"
Write-Host "  $WarpUpUTC  - PolymarketWARPConnect"
Write-Host "  $TriggerUTC  - $TaskName"
Write-Host "  $WarpDownUTC  - PolymarketWARPDisconnect"
Write-Host ""
Write-Host "Verify:  schtasks /Query /TN $TaskName /V /FO LIST"
