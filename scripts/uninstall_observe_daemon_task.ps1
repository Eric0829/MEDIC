param(
    [string]$TaskName = "MEDIC Observe Daemon",
    [string]$TaskPath = "\MEDIC\",
    [switch]$StopFirst,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$Plan = [ordered]@{
    status = if ($Apply) { "apply" } else { "dry_run" }
    task_name = $TaskName
    task_path = $TaskPath
    stop_first = [bool]$StopFirst
    note = "Use -Apply to unregister the scheduled task. Without -Apply this script changes nothing."
}

if (-not $Apply) {
    $Plan | ConvertTo-Json -Depth 6
    exit 0
}

$Task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if (-not $Task) {
    $Plan.status = "missing"
    $Plan | ConvertTo-Json -Depth 6
    exit 0
}

if ($StopFirst) {
    Stop-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
}

Unregister-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Confirm:$false
$Plan.status = "unregistered"
$Plan | ConvertTo-Json -Depth 6
