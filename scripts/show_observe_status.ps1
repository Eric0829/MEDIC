param(
    [string]$Python = "",
    [string]$Config = "",
    [int]$Alerts = 20,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$MedicRoot = Split-Path -Parent $PSScriptRoot
$ControlPy = Join-Path $MedicRoot "medic_control.py"
. (Join-Path $PSScriptRoot "resolve_python.ps1")
$PythonExe = Resolve-MedicPython $Python

if (-not $Config) {
    $Config = Join-Path $MedicRoot "config\observe_daemon.example.json"
}

$ArgsList = @(
    $ControlPy,
    "--root", $MedicRoot,
    "--observe-daemon-status",
    "--observe-daemon-config", $Config,
    "--observe-alerts", [string]$Alerts
)

if ($Json) {
    $ArgsList += "--json"
}

& $PythonExe @ArgsList
exit $LASTEXITCODE
