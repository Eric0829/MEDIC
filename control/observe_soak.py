"""Observe-only daemon soak runner."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.approval_queue import ApprovalQueue
from control.audit_log import AuditLog
from control.observe_daemon import ObserveDaemonRunner
from control.pipeline_trace import PipelineTrace


class ObserveSoakRunner:
    """Run a bounded observe daemon soak and write a control summary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.soak_dir = self.root / "soak_runs"
        self.approval_queue = ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.audit_log = AuditLog(self.root / "control_state" / "audit.jsonl")
        self.trace = PipelineTrace(self.root / "control_state" / "pipeline_trace.jsonl")

    async def run(
        self,
        config_path: str = "",
        cycles: int = 3,
        interval_seconds: float = 1.0,
        stop_on_blocked: bool = False,
    ) -> dict[str, Any]:
        cycles = max(1, int(cycles or 1))
        interval_seconds = max(0.0, float(interval_seconds))
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        before = self._state_snapshot()

        daemon = await ObserveDaemonRunner(self.root).run(
            config_path=config_path,
            interval_seconds=interval_seconds,
            max_cycles=cycles,
            stop_on_blocked=stop_on_blocked,
            actor="medic.observe_soak",
        )

        after = self._state_snapshot()
        recent_cycles = list(daemon.get("recent_cycles", []) or [])
        cycle_statuses = [str(row.get("status", "unknown")) for row in recent_cycles]
        alert_count = sum(int(row.get("alert_count", 0) or 0) for row in recent_cycles)
        incident_count = sum(int(row.get("incident_count", 0) or 0) for row in recent_cycles)
        failed_targets = sum(int(row.get("failed_targets", 0) or 0) for row in recent_cycles)
        attention_targets = sum(int(row.get("attention_targets", 0) or 0) for row in recent_cycles)
        active_incidents = int(
            (daemon.get("incident_stats", {}) or {}).get("active", 0) or 0
        )

        approval_events = self._delta(after["approval"], before["approval"], "total")
        audit_events = self._delta(after["audit"], before["audit"], "events_seen")
        trace_events = self._delta(after["trace"], before["trace"], "events_seen")
        failures = self._failures(
            daemon=daemon,
            requested_cycles=cycles,
            alert_count=alert_count,
            active_incidents=active_incidents,
            approval_events=approval_events,
        )
        status = "healthy"
        if failures:
            status = "blocked" if str(daemon.get("status", "")) == "blocked" else "warning"

        summary = {
            "kind": "observe_soak",
            "observe_only": True,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "status": status,
            "requested_cycles": cycles,
            "cycles_completed": int(daemon.get("cycles_completed", 0) or 0),
            "healthy_cycles": sum(1 for value in cycle_statuses if value == "healthy"),
            "failed_cycles": sum(1 for value in cycle_statuses if value != "healthy"),
            "daemon_interval_seconds": interval_seconds,
            "daemon_status": daemon.get("status", "unknown"),
            "daemon_stop_reason": daemon.get("stop_reason", ""),
            "daemon_summary_file": daemon.get("summary_file", ""),
            "latest_path": daemon.get("latest_path", ""),
            "alert_path": daemon.get("alert_path", ""),
            "incident_path": daemon.get("incident_path", ""),
            "alert_count": alert_count,
            "incident_count": incident_count,
            "active_incidents": active_incidents,
            "failed_targets": failed_targets,
            "attention_targets": attention_targets,
            "approval_events": approval_events,
            "audit_events": audit_events,
            "trace_events": trace_events,
            "treatment_totals": {},
            "failures": failures,
            "cycle_status_counts": self._status_counts(cycle_statuses),
            "cycles_detail": recent_cycles,
            "before": before,
            "after": after,
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "approval": self.approval_queue.stats(),
            "audit": self.audit_log.stats(),
            "trace": self.trace.stats(),
        }

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.soak_dir.mkdir(parents=True, exist_ok=True)
        path = self.soak_dir / f"observe_soak_{stamp}_summary.json"
        summary["summary_file"] = str(path)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _failures(
        daemon: dict[str, Any],
        requested_cycles: int,
        alert_count: int,
        active_incidents: int,
        approval_events: int,
    ) -> list[str]:
        failures: list[str] = []
        daemon_status = str(daemon.get("status", "unknown"))
        cycles_completed = int(daemon.get("cycles_completed", 0) or 0)
        if daemon_status != "healthy":
            failures.append(f"observe_daemon={daemon_status}")
        if cycles_completed != requested_cycles:
            failures.append(f"cycles_completed={cycles_completed}/{requested_cycles}")
        if alert_count:
            failures.append(f"alerts={alert_count}")
        if active_incidents:
            failures.append(f"active_incidents={active_incidents}")
        if approval_events:
            failures.append(f"approval_events={approval_events}")
        return failures

    @staticmethod
    def _delta(after: dict[str, Any], before: dict[str, Any], key: str) -> int:
        return int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)

    @staticmethod
    def _status_counts(values: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = str(value or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts
