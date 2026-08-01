# MEDIC observe daemon scripts

These scripts wrap the observe-only daemon. They do not modify treatments or
delete evidence logs.

Related operator docs:

- `MEDIC/docs/MEDIC_OVERVIEW.md`
- `MEDIC/docs/OPERATIONS.md`
- `MEDIC/docs/STATUS.md`
- `MEDIC/docs/INCIDENT_GUIDE.md`

## Run once in the foreground

```powershell
.\MEDIC\scripts\run_observe_daemon.ps1 -MaxCycles 1 -Interval 0
```

The PowerShell scripts try to find a local Python installation automatically.
If Python is installed somewhere unusual, pass the full path explicitly:

```powershell
.\MEDIC\scripts\run_observe_daemon.ps1 -Python "C:\Path\To\python.exe" -MaxCycles 1 -Interval 0
```

The `--daily-check` output also prints full-path fallback commands when the
short `python` command is not available.

## Run continuously in the foreground

```powershell
.\MEDIC\scripts\run_observe_daemon.ps1
```

## Start continuously in a hidden background process

This wrapper checks for an existing daemon first, then starts one hidden
background process if none is running.

```powershell
.\MEDIC\scripts\start_observe_daemon_hidden.ps1
```

If the current shell is not allowed to spawn hidden background processes, it
returns `status: failed` with the Windows error.

## Preview current-user Startup registration

This is the lower-permission alternative to Task Scheduler. It creates a
current-user Startup `.cmd` entry only when `-Apply` is passed.

```powershell
.\MEDIC\scripts\install_user_startup.ps1
.\MEDIC\scripts\install_user_startup.ps1 -Apply
```

To remove the Startup entry:

```powershell
.\MEDIC\scripts\install_user_startup.ps1 -Remove -Apply
```

## Run through the short Windows wrapper

The `.cmd` wrapper keeps scheduled-task commands short enough for `schtasks`.

```powershell
.\MEDIC\scripts\run_observe_daemon.cmd
```

## Show latest daemon status

```powershell
.\MEDIC\scripts\show_observe_status.ps1
```

## Run a bounded observe soak

This runs the observe daemon for a fixed number of cycles and writes an
`observe_soak_*_summary.json` file under `MEDIC/soak_runs`.

```powershell
.\MEDIC\scripts\run_observe_soak.ps1 -Cycles 3 -Interval 1
```

If the current shell blocks Python launch, the script returns `status: failed`
with the resolved Python path so the direct CLI command can be retried.

## Run the staged benchmark suite

This runs internal, external, adversarial, real local target, and observe soak
checks. The default observe soak is a short probe, not a multi-hour proof.

```powershell
.\MEDIC\scripts\run_benchmark_suite.ps1
```

If this Codex-hosted shell blocks Python launch, the wrapper returns
`status: failed`. In that case, use the direct `medic_control.py` command shown
in the error output or retry from a normal Windows PowerShell session.

For a longer observe stage, increase cycles and interval:

```powershell
.\MEDIC\scripts\run_benchmark_suite.ps1 -ObserveCycles 360 -ObserveInterval 60
```

## Preview Windows scheduled task registration

```powershell
.\MEDIC\scripts\install_observe_daemon_task.ps1
```

The install script is dry-run by default. It only registers the task when
`-Apply` is passed.

```powershell
.\MEDIC\scripts\install_observe_daemon_task.ps1 -Apply
```

If Windows blocks registration, the script now returns a structured
`status: failed` result with the Task Scheduler error and the resolved Python
path it tried to use.

## Show scheduled task status

```powershell
.\MEDIC\scripts\show_observe_task.ps1
```

## Preview scheduled task removal

```powershell
.\MEDIC\scripts\uninstall_observe_daemon_task.ps1
```

The uninstall script is also dry-run by default. It only unregisters the task
when `-Apply` is passed.

```powershell
.\MEDIC\scripts\uninstall_observe_daemon_task.ps1 -StopFirst -Apply
```
