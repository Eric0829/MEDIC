"""Foreground daemon for continuous MEDIC observe-only supervision."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.file_store import append_jsonl_locked, read_lines_locked, read_text_locked, write_text_locked
from control.incident_queue import IncidentQueue
from control.observe_supervisor import ObserveSupervisorRunner


@dataclass
class ObserveDaemonConfig:
    """Runtime settings for the observe daemon."""

    source: str = "default"
    observe_config: str = ""
    interval_seconds: float = 60.0
    supervisor_cycles: int = 1
    supervisor_cycle_interval_seconds: float = 0.0
    max_cycles: int = 0
    stop_on_blocked: bool = False
    latest_path: str = ""
    alert_path: str = ""
    incident_path: str = ""
    incident_stale_after_seconds: float = 86400.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "observe_config": self.observe_config,
            "interval_seconds": self.interval_seconds,
            "supervisor_cycles": self.supervisor_cycles,
            "supervisor_cycle_interval_seconds": self.supervisor_cycle_interval_seconds,
            "max_cycles": self.max_cycles,
            "stop_on_blocked": self.stop_on_blocked,
            "latest_path": self.latest_path,
            "alert_path": self.alert_path,
            "incident_path": self.incident_path,
            "incident_stale_after_seconds": self.incident_stale_after_seconds,
        }


class ObserveDaemonRunner:
    """Run observe supervisor repeatedly and maintain a latest status file."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.observe_dir = self.root / "observe_runs"

    async def run(
        self,
        config_path: str = "",
        interval_seconds: float | None = None,
        max_cycles: int | None = None,
        stop_on_blocked: bool | None = None,
        actor: str = "medic.observe_daemon",
    ) -> dict[str, Any]:
        config = load_observe_daemon_config(config_path, self.root)
        if interval_seconds is not None:
            config.interval_seconds = max(0.0, float(interval_seconds))
        if max_cycles is not None:
            config.max_cycles = max(0, int(max_cycles))
        if stop_on_blocked is not None:
            config.stop_on_blocked = bool(stop_on_blocked)

        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        cycles_completed = 0
        recent_cycles: list[dict[str, Any]] = []
        last_status = "healthy"
        stop_reason = ""
        incident_queue = IncidentQueue(config.incident_path)

        while config.max_cycles <= 0 or cycles_completed < config.max_cycles:
            cycle_number = cycles_completed + 1
            cycle_started = time.monotonic()
            supervisor = await ObserveSupervisorRunner(self.root).run(
                config_path=config.observe_config,
                cycles=config.supervisor_cycles,
                cycle_interval_seconds=config.supervisor_cycle_interval_seconds,
                actor=actor,
            )
            cycles_completed += 1
            last_status = str(supervisor.get("status", "unknown"))
            alerts = self._alerts_from_supervisor(supervisor, cycle_number)
            incident_updates: list[dict[str, Any]] = []
            incidents_created = 0
            for alert in alerts:
                append_jsonl_locked(config.alert_path, alert)
                result = incident_queue.upsert_from_alert(alert)
                incident = result["incident"]
                incidents_created += 1 if result["created"] else 0
                incident_updates.append({
                    "incident_id": incident.incident_id,
                    "status": incident.status,
                    "severity": incident.severity,
                    "created": bool(result["created"]),
                    "seen_count": incident.seen_count,
                })
            incident_triage = incident_queue.triage_report(
                stale_after_seconds=config.incident_stale_after_seconds
            )

            cycle = {
                "cycle": cycle_number,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": round((time.monotonic() - cycle_started) * 1000, 3),
                "status": last_status,
                "alerts": alerts,
                "alert_count": len(alerts),
                "incident_count": len(incident_updates),
                "incidents_created": incidents_created,
                "incidents_updated": len(incident_updates) - incidents_created,
                "incidents": incident_updates,
                "incident_stats": incident_triage,
                "incident_triage": incident_triage,
                "supervisor_summary_file": supervisor.get("summary_file", ""),
                "targets_observed": supervisor.get("targets_observed", 0),
                "failed_targets": supervisor.get("failed_targets", 0),
                "attention_targets": supervisor.get("attention_targets", 0),
                "patient_status_counts": supervisor.get("patient_status_counts", {}),
                "trace_ids": supervisor.get("trace_ids", []),
            }
            recent_cycles.append(cycle)
            recent_cycles = recent_cycles[-20:]

            latest = self._latest_payload(
                config=config,
                started_at=started_at,
                cycles_completed=cycles_completed,
                last_cycle=cycle,
                recent_cycles=recent_cycles,
                last_status=last_status,
            )
            write_text_locked(
                config.latest_path,
                json.dumps(latest, ensure_ascii=False, indent=2),
            )

            if config.stop_on_blocked and last_status == "blocked":
                stop_reason = "blocked"
                break
            if config.max_cycles > 0 and cycles_completed >= config.max_cycles:
                stop_reason = "max_cycles"
                break
            if config.interval_seconds:
                await asyncio.sleep(config.interval_seconds)

        summary = {
            "kind": "observe_daemon",
            "observe_only": True,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "status": last_status,
            "stop_reason": stop_reason or "completed",
            "config": config.to_dict(),
            "cycles_completed": cycles_completed,
            "latest_path": config.latest_path,
            "alert_path": config.alert_path,
            "incident_path": config.incident_path,
            "incident_stats": incident_queue.triage_report(
                stale_after_seconds=config.incident_stale_after_seconds
            ),
            "recent_cycles": recent_cycles,
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    def _latest_payload(
        self,
        config: ObserveDaemonConfig,
        started_at: datetime,
        cycles_completed: int,
        last_cycle: dict[str, Any],
        recent_cycles: list[dict[str, Any]],
        last_status: str,
    ) -> dict[str, Any]:
        return {
            "kind": "observe_daemon_latest",
            "observe_only": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started_at.isoformat(),
            "status": last_status,
            "config": config.to_dict(),
            "cycles_completed": cycles_completed,
            "last_cycle": last_cycle,
            "recent_cycles": recent_cycles,
        }

    def _alerts_from_supervisor(
        self,
        supervisor: dict[str, Any],
        daemon_cycle: int,
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for cycle in list(supervisor.get("cycles_detail", []) or []):
            for target in list(cycle.get("targets", []) or []):
                status = str(target.get("status", ""))
                patient_status = str(target.get("patient_status", ""))
                failure = str(target.get("failure", ""))
                if status not in {"failed", "blocked"} and patient_status not in {"attention", "critical"}:
                    continue
                severity = "critical" if status in {"failed", "blocked"} or patient_status == "critical" else "warning"
                alerts.append({
                    "kind": "observe_alert",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "severity": severity,
                    "daemon_cycle": daemon_cycle,
                    "supervisor_summary_file": supervisor.get("summary_file", ""),
                    "target_name": target.get("name", ""),
                    "target": target.get("target", ""),
                    "status": status,
                    "patient_status": patient_status,
                    "failure": failure,
                    "trace_ids": target.get("trace_ids", []),
                    "message": self._alert_message(target),
                })
        return alerts

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.observe_dir.mkdir(parents=True, exist_ok=True)
        path = self.observe_dir / f"observe_daemon_{stamp}_summary.json"
        summary["summary_file"] = str(path)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _alert_message(target: dict[str, Any]) -> str:
        name = str(target.get("name", "target"))
        status = str(target.get("status", "unknown"))
        patient_status = str(target.get("patient_status", "unknown"))
        failure = str(target.get("failure", ""))
        if failure:
            return f"{name} observe failed: {failure}"
        return f"{name} observe status={status}, patient_status={patient_status}"


def load_observe_daemon_config(path: str | Path | None, root: str | Path) -> ObserveDaemonConfig:
    """Load daemon settings, or return a safe default config."""
    root_path = Path(root)
    observe_dir = root_path / "observe_runs"
    if not path:
        return ObserveDaemonConfig(
            source="default",
            latest_path=str(observe_dir / "observe_daemon_latest.json"),
            alert_path=str(observe_dir / "observe_alerts.jsonl"),
            incident_path=str(root_path / "control_state" / "incident_cases.jsonl"),
            incident_stale_after_seconds=86400.0,
        )

    config_path = _resolve_path(path, root_path=root_path, base_dir=Path.cwd())
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("observe daemon config must be a JSON object")

    observe_config = str(data.get("observe_config", "") or "")
    if observe_config:
        observe_config = str(_resolve_path(
            observe_config,
            root_path=root_path,
            base_dir=config_path.parent,
        ))

    latest_path = str(data.get("latest_path", "") or "")
    if not latest_path:
        latest_path = str(observe_dir / "observe_daemon_latest.json")
    else:
        latest_path = str(_resolve_output_path(latest_path, root_path=root_path, base_dir=config_path.parent))

    alert_path = str(data.get("alert_path", "") or "")
    if not alert_path:
        alert_path = str(observe_dir / "observe_alerts.jsonl")
    else:
        alert_path = str(_resolve_output_path(alert_path, root_path=root_path, base_dir=config_path.parent))

    incident_path = str(data.get("incident_path", "") or "")
    if not incident_path:
        incident_path = str(root_path / "control_state" / "incident_cases.jsonl")
    else:
        incident_path = str(_resolve_output_path(
            incident_path,
            root_path=root_path,
            base_dir=config_path.parent,
        ))
    incident_stale_after_seconds = max(
        1.0,
        float(data.get("incident_stale_after_seconds", 86400.0) or 86400.0),
    )

    return ObserveDaemonConfig(
        source=str(config_path),
        observe_config=observe_config,
        interval_seconds=float(data.get("interval_seconds", 60.0) or 0.0),
        supervisor_cycles=max(1, int(data.get("supervisor_cycles", 1) or 1)),
        supervisor_cycle_interval_seconds=max(
            0.0,
            float(data.get("supervisor_cycle_interval_seconds", 0.0) or 0.0),
        ),
        max_cycles=max(0, int(data.get("max_cycles", 0) or 0)),
        stop_on_blocked=bool(data.get("stop_on_blocked", False)),
        latest_path=latest_path,
        alert_path=alert_path,
        incident_path=incident_path,
        incident_stale_after_seconds=incident_stale_after_seconds,
    )


def observe_daemon_config_template() -> dict[str, Any]:
    """Return an example foreground daemon config."""
    return {
        "version": 1,
        "observe_config": "observe_targets.example.json",
        "interval_seconds": 60.0,
        "supervisor_cycles": 1,
        "supervisor_cycle_interval_seconds": 0.0,
        "max_cycles": 0,
        "stop_on_blocked": False,
        "latest_path": "../observe_runs/observe_daemon_latest.json",
        "alert_path": "../observe_runs/observe_alerts.jsonl",
        "incident_path": "../control_state/incident_cases.jsonl",
        "incident_stale_after_seconds": 86400.0,
    }


def write_observe_daemon_config_template(path: str | Path, root: str | Path) -> dict[str, Any]:
    """Write a ready-to-edit daemon config template."""
    root_path = Path(root)
    target = _resolve_output_path(path, root_path=root_path, base_dir=Path.cwd())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(observe_daemon_config_template(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "status": "written",
        "path": str(target),
    }


def read_observe_daemon_status(
    root: str | Path,
    config_path: str = "",
    alert_limit: int = 20,
) -> dict[str, Any]:
    """Read the latest daemon status and recent alerts."""
    config = load_observe_daemon_config(config_path, root)
    latest_path = Path(config.latest_path)
    latest = _read_json_file_locked(latest_path)
    alerts = read_observe_alerts(root, config_path=config_path, limit=alert_limit)
    incident = IncidentQueue(config.incident_path).triage_report(
        stale_after_seconds=config.incident_stale_after_seconds
    )
    process = _observe_daemon_process_status()
    status = str(latest.get("status", "missing") if latest else "missing")
    return {
        "kind": "observe_daemon_status",
        "status": status,
        "latest_exists": bool(latest),
        "latest_path": str(latest_path),
        "alert_path": config.alert_path,
        "incident_path": config.incident_path,
        "updated_at": latest.get("updated_at", "") if latest else "",
        "cycles_completed": int(latest.get("cycles_completed", 0) or 0) if latest else 0,
        "last_cycle": latest.get("last_cycle", {}) if latest else {},
        "recent_alerts": alerts["alerts"],
        "recent_alert_count": len(alerts["alerts"]),
        "total_alert_lines": alerts["total_lines"],
        "incident": incident,
        "process": process,
    }


def _observe_daemon_process_status() -> dict[str, Any]:
    script = r"""
$ErrorActionPreference = "SilentlyContinue"
$Rows = Get-CimInstance Win32_Process | Where-Object {
    $Name = [string]$_.Name
    $Cmd = [string]$_.CommandLine
    if (-not $Cmd) { return $false }
    $PythonDaemon = ($Name -like "python*") -and ($Cmd -like "*medic_control.py*") -and ($Cmd -match "(^|\s)--observe-daemon(\s|$)")
    $ScriptDaemon = (($Name -like "powershell*") -or ($Name -like "pwsh*")) -and ($Cmd -like "*run_observe_daemon.ps1*") -and ($Cmd -notlike "*Get-CimInstance*")
    return ($PythonDaemon -or $ScriptDaemon)
} | Select-Object ProcessId,Name,CommandLine
$Rows | ConvertTo-Json -Depth 5
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return {
            "status": "unknown",
            "alive": False,
            "count": 0,
            "processes": [],
            "error": f"{type(exc).__name__}: {exc}",
        }

    if completed.returncode != 0:
        return {
            "status": "unknown",
            "alive": False,
            "count": 0,
            "processes": [],
            "error": (completed.stderr or completed.stdout or "").strip(),
        }

    raw = (completed.stdout or "").strip()
    if not raw:
        rows: list[dict[str, Any]] = []
    else:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                rows = [parsed]
            elif isinstance(parsed, list):
                rows = [row for row in parsed if isinstance(row, dict)]
            else:
                rows = []
        except Exception as exc:
            return {
                "status": "unknown",
                "alive": False,
                "count": 0,
                "processes": [],
                "error": f"parse_error: {exc}",
            }

    processes = [
        {
            "pid": row.get("ProcessId"),
            "name": row.get("Name", ""),
            "command_line": row.get("CommandLine", ""),
        }
        for row in rows
    ]
    return {
        "status": "running" if processes else "missing",
        "alive": bool(processes),
        "count": len(processes),
        "processes": processes,
        "error": "",
    }


def read_observe_alerts(
    root: str | Path,
    config_path: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Read recent observe alert JSONL rows."""
    config = load_observe_daemon_config(config_path, root)
    alert_path = Path(config.alert_path)
    if not alert_path.exists():
        return {
            "kind": "observe_alerts",
            "status": "empty",
            "path": str(alert_path),
            "limit": max(1, int(limit or 1)),
            "total_lines": 0,
            "alerts": [],
            "invalid_lines": 0,
        }

    lines = read_lines_locked(alert_path)
    limit = max(1, int(limit or 1))
    alerts: list[dict[str, Any]] = []
    invalid = 0
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                alerts.append(parsed)
            else:
                invalid += 1
        except Exception:
            invalid += 1

    status = "healthy" if not invalid else "warning"
    return {
        "kind": "observe_alerts",
        "status": status,
        "path": str(alert_path),
        "limit": limit,
        "total_lines": len(lines),
        "alerts": alerts,
        "invalid_lines": invalid,
    }


def _resolve_path(path: str | Path, root_path: Path, base_dir: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    options = [
        candidate,
        base_dir / candidate,
        root_path / candidate,
    ]
    for option in options:
        if option.exists():
            return option.resolve()
    return (root_path / candidate).resolve()


def _resolve_output_path(path: str | Path, root_path: Path, base_dir: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    parent = candidate.parent if str(candidate.parent) != "." else Path(".")
    if parent.exists():
        return candidate.resolve()
    if (base_dir / parent).exists():
        return (base_dir / candidate).resolve()
    return (root_path / candidate).resolve()


def _read_json_file_locked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(read_text_locked(path))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
