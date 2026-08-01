param(
    [string]$TaskName = "MEDIC Observe Daemon",
    [string]$TaskPath = "\MEDIC\",
    [string]$Python = "",
    [string]$Config = "",
    [double]$Interval = 60.0,
    [ValidateSet("Logon", "Startup")]
    [string]$Trigger = "Logon",
    [switch]$RunAsHighest,
    [switch]$RunNow,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$MedicRoot = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_observe_daemon.ps1"
. (Join-Path $PSScriptRoot "resolve_python.ps1")
$PythonExe = Resolve-MedicPython $Python

if (-not $Config) {
    $Config = Join-Path $MedicRoot "config\observe_daemon.example.json"
}

function Quote-Arg([string]$Value) {
    return '"' + $Value.Replace('"', '\"') + '"'
}

$ActionExe = "powershell.exe"
$ActionArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-Arg $Runner),
    "-Python", (Quote-Arg $PythonExe),
    "-Config", (Quote-Arg $Config),
    "-Interval", [string]$Interval
) -join " "

$Plan = [ordered]@{
    status = if ($Apply) { "apply" } else { "dry_run" }
    task_name = $TaskName
    task_path = $TaskPath
    trigger = $Trigger
    run_as_highest = [bool]$RunAsHighest
    run_now = [bool]$RunNow
    action_exe = $ActionExe
    action_args = $ActionArgs
    medic_root = $MedicRoot
    python = $PythonExe
    config = $Config
    note = "Use -Apply to register the scheduled task. Without -Apply this script changes nothing."
}

if (-not $Apply) {
    $Plan | ConvertTo-Json -Depth 6
    exit 0
}

try {
    if ($Trigger -eq "Startup") {
        $TaskTrigger = New-ScheduledTaskTrigger -AtStartup -ErrorAction Stop
    } else {
        $TaskTrigger = New-ScheduledTaskTrigger -AtLogOn -ErrorAction Stop
    }

    $TaskAction = New-ScheduledTaskAction `
        -Execute $ActionExe `
        -Argument $ActionArgs `
        -ErrorAction Stop
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -ErrorAction Stop

    $Description = "Runs MEDIC observe daemon in observe-only mode."

    if ($RunAsHighest) {
        $UserId = "$env:USERDOMAIN\$env:USERNAME"
        $Principal = New-ScheduledTaskPrincipal `
            -UserId $UserId `
            -LogonType Interactive `
            -RunLevel Highest `
            -ErrorAction Stop
        Register-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $TaskPath `
            -Action $TaskAction `
            -Trigger $TaskTrigger `
            -Settings $TaskSettings `
            -Principal $Principal `
            -Description $Description `
            -Force `
            -ErrorAction Stop | Out-Null
    } else {
        Register-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $TaskPath `
            -Action $TaskAction `
            -Trigger $TaskTrigger `
            -Settings $TaskSettings `
            -Description $Description `
            -Force `
            -ErrorAction Stop | Out-Null
    }

    if ($RunNow) {
        Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction Stop
    }

    $Plan.status = "registered"
    $Plan | ConvertTo-Json -Depth 6
} catch {
    $Plan.status = "failed"
    $Plan.error = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
    $Plan.note = "Task Scheduler registration failed. Run from a Windows session with permission to create scheduled tasks, or keep using the direct observe-daemon command."
    $Plan | ConvertTo-Json -Depth 6
    exit 1
}
