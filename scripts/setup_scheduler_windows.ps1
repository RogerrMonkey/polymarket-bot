# =============================================================================
# setup_scheduler_windows.ps1 — Register the PolymarketPaperLoop daily task
# =============================================================================
#
# IMPORTANT: Cloudflare WARP must be active BEFORE this task fires.
#   - Task fires at 03:00 UTC (08:30 IST) daily.
#   - Polymarket + polygon.llamarpc.com are DNS-blocked from India without WARP.
#   - Enable WARP each morning BEFORE 03:00 UTC, or configure WARP to auto-connect
#     on startup so it is always active when the machine is running.
#
# USAGE:
#   Open PowerShell as the current user (no Administrator needed for
#   RunWhetherLoggedOnOrNot=false) and run:
#
#       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#       .\scripts\setup_scheduler_windows.ps1
#
#   Optional parameters:
#       -TaskName   (default: PolymarketPaperLoop)
#       -TriggerUTC (default: 03:00  — 08:30 IST)
#       -Cycles     (default: 20)
#       -Interval   (default: 60  seconds between cycles)
#
# VERIFY:
#   schtasks /Query /TN PolymarketPaperLoop /V /FO LIST
#   Check "Last Run Result" the morning after the first scheduled run.
# =============================================================================

param(
    [string]$TaskName   = "PolymarketPaperLoop",
    [string]$TriggerUTC = "03:00",
    [int]   $Cycles     = 20,
    [int]   $Interval   = 60
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Resolve project root (two levels up from this script)
# ---------------------------------------------------------------------------
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

# ---------------------------------------------------------------------------
# Build the command arguments
# ---------------------------------------------------------------------------
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    throw "python not found on PATH. Activate your venv or add Python to PATH first."
}

$Arguments = "-m prediction_bot paper-loop --cycles $Cycles --interval $Interval"

Write-Host "Project root : $ProjectRoot"
Write-Host "Python       : $PythonExe"
Write-Host "Arguments    : $Arguments"
Write-Host "Trigger UTC  : $TriggerUTC daily"
Write-Host ""

# ---------------------------------------------------------------------------
# Build trigger: daily at $TriggerUTC in UTC — StartBoundary must use a
# full ISO-8601 datetime; we use today's date to anchor it.
# ---------------------------------------------------------------------------
$Today     = (Get-Date).ToString("yyyy-MM-dd")
$StartBoundary = "${Today}T${TriggerUTC}:00Z"

# ---------------------------------------------------------------------------
# Create the task via the Scheduler COM object for full property control
# ---------------------------------------------------------------------------
$scheduler = New-Object -ComObject "Schedule.Service"
$scheduler.Connect()

$taskDef    = $scheduler.NewTask(0)
$taskDef.RegistrationInfo.Description = `
    "Polymarket Bot — daily paper-loop analysis run. WARP must be active before this fires."
$taskDef.RegistrationInfo.Author      = $env:USERNAME
$taskDef.Settings.Enabled             = $true
$taskDef.Settings.Hidden              = $false
$taskDef.Settings.StartWhenAvailable  = $true   # catch up if machine was off at trigger time
$taskDef.Settings.RunOnlyIfNetworkAvailable = $true
$taskDef.Settings.ExecutionTimeLimit  = "PT2H"  # 2-hour hard cap
# RunWhetherLoggedOnOrNot = false  →  only runs when the user is logged on
# (avoids credential-store complexity; user must be logged in for WARP anyway)
$taskDef.Settings.RunOnlyIfIdle       = $false
$taskDef.Principal.LogonType          = 3        # TASK_LOGON_INTERACTIVE_TOKEN
$taskDef.Principal.RunLevel           = 0        # TASK_RUNLEVEL_LUA (least privilege)

# Daily trigger
$trigger = $taskDef.Triggers.Create(2)           # TASK_TRIGGER_DAILY
$trigger.StartBoundary  = $StartBoundary
$trigger.DaysInterval   = 1
$trigger.Enabled        = $true

# Action: python -m prediction_bot paper-loop ...
$action                 = $taskDef.Actions.Create(0)  # TASK_ACTION_EXEC
$action.Path            = $PythonExe
$action.Arguments       = $Arguments
$action.WorkingDirectory = $ProjectRoot

# ---------------------------------------------------------------------------
# Register (overwrite if exists)
# ---------------------------------------------------------------------------
$rootFolder = $scheduler.GetFolder("\")
$rootFolder.RegisterTaskDefinition(
    $TaskName,
    $taskDef,
    6,          # TASK_CREATE_OR_UPDATE
    $null,      # user (current user implied by LogonType=3)
    $null,      # password (not needed for interactive token)
    3           # TASK_LOGON_INTERACTIVE_TOKEN
) | Out-Null

Write-Host "SUCCESS: task '$TaskName' registered."
Write-Host ""
Write-Host "Verify with:"
Write-Host "  schtasks /Query /TN `"$TaskName`" /V /FO LIST"
Write-Host ""
Write-Host "Manual test run:"
Write-Host "  schtasks /Run /TN `"$TaskName`""
Write-Host ""
Write-Host "REMINDER: Enable Cloudflare WARP before 03:00 UTC each day."
