param(
    [string]$Python = "",
    [string]$Config = "",
    [int]$Cycles = 3,
    [double]$Interval = 1.0,
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
    "--observe-soak",
    "--observe-daemon-config", $Config,
    "--observe-soak-cycles", [string]$Cycles,
    "--observe-soak-interval", [string]$Interval
)

if ($StopOnBlocked) {
    $ArgsList += "--observe-soak-stop-on-blocked"
}

if ($Json) {
    $ArgsList += "--json"
}

$Result = [ordered]@{
    status = "starting"
    medic_root = $MedicRoot
    python = $PythonExe
    config = $Config
    cycles = $Cycles
    interval = $Interval
    command = ($ArgsList -join " ")
}

try {
    & $PythonExe @ArgsList
    exit $LASTEXITCODE
} catch {
    $Result.status = "failed"
    $Result.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
    $Result.note = "Could not launch Python from this session. Try the same script from a normal Windows PowerShell session, or run medic_control.py directly with the printed Python path."
    $Result | ConvertTo-Json -Depth 6
    exit 1
}
