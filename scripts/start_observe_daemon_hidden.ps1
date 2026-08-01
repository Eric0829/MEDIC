param(
    [string]$Python = "",
    [string]$Config = "",
    [double]$Interval = 60.0,
    [int]$MaxCycles = 0,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

$MedicRoot = Split-Path -Parent $PSScriptRoot
$ControlPy = Join-Path $MedicRoot "medic_control.py"
. (Join-Path $PSScriptRoot "resolve_python.ps1")
$PythonExe = Resolve-MedicPython $Python

if (-not $Config) {
    $Config = Join-Path $MedicRoot "config\observe_daemon.example.json"
}

function Quote-Arg([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Get-MedicObserveDaemonProcessRows {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $Name = [string]$_.Name
        $Cmd = [string]$_.CommandLine
        if (-not $Cmd) {
            return $false
        }
        $PythonDaemon = ($Name -like "python*") -and ($Cmd -like "*medic_control.py*") -and ($Cmd -match "(^|\s)--observe-daemon(\s|$)")
        $ScriptDaemon = (($Name -like "powershell*") -or ($Name -like "pwsh*")) -and ($Cmd -like "*run_observe_daemon.ps1*") -and ($Cmd -notlike "*Get-CimInstance*")
        return ($PythonDaemon -or $ScriptDaemon)
    } | Select-Object ProcessId,Name,CommandLine
}

$Existing = @(Get-MedicObserveDaemonProcessRows)
$OutLog = Join-Path $MedicRoot "observe_runs\observe_daemon_background.out.log"
$ErrLog = Join-Path $MedicRoot "observe_runs\observe_daemon_background.err.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutLog) | Out-Null

$ActionArgs = @(
    (Quote-Arg $ControlPy),
    "--root", (Quote-Arg $MedicRoot),
    "--observe-daemon",
    "--observe-daemon-config", (Quote-Arg $Config),
    "--daemon-interval", [string]$Interval,
    "--daemon-max-cycles", [string]$MaxCycles
) -join " "

$Result = [ordered]@{
    status = "dry_run"
    medic_root = $MedicRoot
    python = $PythonExe
    config = $Config
    interval = $Interval
    max_cycles = $MaxCycles
    existing_count = $Existing.Count
    existing_processes = @($Existing | ForEach-Object {
        [ordered]@{
            pid = $_.ProcessId
            name = $_.Name
            command_line = $_.CommandLine
        }
    })
    action_exe = $PythonExe
    action_args = $ActionArgs
    stdout_log = $OutLog
    stderr_log = $ErrLog
}

if ($Existing.Count -gt 0) {
    $Result.status = "already_running"
    $Result | ConvertTo-Json -Depth 8
    exit 0
}

if ($NoStart) {
    $Result | ConvertTo-Json -Depth 8
    exit 0
}

try {
    $Process = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $ActionArgs `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -PassThru `
        -ErrorAction Stop

    $Result.status = "started"
    $Result.pid = $Process.Id
    $Result | ConvertTo-Json -Depth 8
} catch {
    $Result.status = "failed"
    $Result.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
    $Result.note = "Could not start a hidden background process from this session. Try running the startup entry or this script from a normal Windows PowerShell session."
    $Result | ConvertTo-Json -Depth 8
    exit 1
}
