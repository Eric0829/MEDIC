param(
    [string]$Python = "",
    [string]$ExternalCases = "",
    [string]$AttackCases = "",
    [int]$ControlIterations = 1,
    [int]$ObserveCycles = 2,
    [double]$ObserveInterval = 0.0,
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$MedicRoot = Split-Path -Parent $PSScriptRoot
$ControlPy = Join-Path $MedicRoot "medic_control.py"
. (Join-Path $PSScriptRoot "resolve_python.ps1")
$PythonExe = Resolve-MedicPython $Python

$ArgsList = @(
    $ControlPy,
    "--root", $MedicRoot,
    "--benchmark-suite",
    "--benchmark-control-iterations", [string]$ControlIterations,
    "--benchmark-observe-cycles", [string]$ObserveCycles,
    "--benchmark-observe-interval", [string]$ObserveInterval
)

if ($ExternalCases) {
    $ArgsList += @("--benchmark-external-cases", $ExternalCases)
}

if ($AttackCases) {
    $ArgsList += @("--benchmark-attack-cases", $AttackCases)
}

if ($Json) {
    $ArgsList += "--json"
}

$Result = [ordered]@{
    status = "starting"
    medic_root = $MedicRoot
    python = $PythonExe
    external_cases = $ExternalCases
    attack_cases = $AttackCases
    control_iterations = $ControlIterations
    observe_cycles = $ObserveCycles
    observe_interval = $ObserveInterval
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
