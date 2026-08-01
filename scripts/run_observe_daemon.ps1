param(
    [string]$Python = "",
    [string]$Config = "",
    [double]$Interval = -1,
    [int]$MaxCycles = -1,
    [switch]$StopOnBlocked,
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
    "--observe-daemon",
    "--observe-daemon-config", $Config
)

if ($Interval -ge 0) {
    $ArgsList += @("--daemon-interval", [string]$Interval)
}

if ($MaxCycles -ge 0) {
    $ArgsList += @("--daemon-max-cycles", [string]$MaxCycles)
}

if ($StopOnBlocked) {
    $ArgsList += "--daemon-stop-on-blocked"
}

if ($Json) {
    $ArgsList += "--json"
}

& $PythonExe @ArgsList
exit $LASTEXITCODE
