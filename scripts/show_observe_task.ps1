param(
    [string]$TaskName = "MEDIC Observe Daemon",
    [string]$TaskPath = "\MEDIC\",
    [switch]$Json
)

$ErrorActionPreference = "Stop"

$Task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
if (-not $Task) {
    $Result = [ordered]@{
        status = "missing"
        task_name = $TaskName
        task_path = $TaskPath
    }
    if ($Json) {
        $Result | ConvertTo-Json -Depth 6
    } else {
        Write-Output "MEDIC observe task is not registered."
    }
    exit 0
}

$Info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath
$Result = [ordered]@{
    status = "registered"
    task_name = $TaskName
    task_path = $TaskPath
    state = [string]$Task.State
    last_run_time = $Info.LastRunTime
    next_run_time = $Info.NextRunTime
    last_task_result = $Info.LastTaskResult
    actions = @($Task.Actions | ForEach-Object {
        [ordered]@{
            execute = $_.Execute
            arguments = $_.Arguments
        }
    })
    triggers = @($Task.Triggers | ForEach-Object {
        [ordered]@{
            enabled = $_.Enabled
            start_boundary = $_.StartBoundary
        }
    })
}

if ($Json) {
    $Result | ConvertTo-Json -Depth 8
} else {
    Write-Output "MEDIC Observe Task"
    Write-Output "status: $($Result.status)"
    Write-Output "state: $($Result.state)"
    Write-Output "last run: $($Result.last_run_time)"
    Write-Output "next run: $($Result.next_run_time)"
    Write-Output "last result: $($Result.last_task_result)"
}
