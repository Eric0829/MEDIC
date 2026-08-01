"""Run configured MEDIC observe-only targets as one supervisor pass."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.controlled_registry import ControlledPatientRegistry
from control.observe_config import ObserveConfig, ObserveTargetSpec, load_observe_config
from control.observe_loop import ObserveLoopRunner
from control.observe_targets import build_observe_patient


@dataclass
class ObserveSupervisorTargetResult:
    """One configured target result."""

    name: str
    target: str
    status: str
    patient_status: str = ""
    summary_file: str = ""
    trace_ids: list[str] = field(default_factory=list)
    failure: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "status": self.status,
            "patient_status": self.patient_status,
            "summary_file": self.summary_file,
            "trace_ids": self.trace_ids,
            "failure": self.failure,
            "result": self.result,
        }


class ObserveSupervisorRunner:
    """Run all configured observe targets and write a supervisor summary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.observe_dir = self.root / "observe_runs"

    async def run(
        self,
        config_path: str = "",
        cycles: int = 1,
        cycle_interval_seconds: float = 0.0,
        actor: str = "medic.observe_supervisor",
    ) -> dict[str, Any]:
        config = load_observe_config(config_path, self.root)
        cycles = max(1, int(cycles or 1))
        cycle_interval_seconds = max(0.0, float(cycle_interval_seconds or 0.0))
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()

        cycle_rows = []
        target_rows: list[ObserveSupervisorTargetResult] = []
        for cycle in range(1, cycles + 1):
            cycle_started = time.monotonic()
            rows = await self._run_cycle(config, cycle=cycle, actor=actor)
            target_rows.extend(rows)
            cycle_rows.append({
                "cycle": cycle,
                "duration_ms": round((time.monotonic() - cycle_started) * 1000, 3),
                "targets": [row.to_dict() for row in rows],
            })
            if cycle_interval_seconds and cycle < cycles:
                await asyncio.sleep(cycle_interval_seconds)

        failures = [row for row in target_rows if row.failure or row.status in {"failed", "blocked"}]
        attention = [
            row for row in target_rows
            if row.patient_status in {"attention", "critical"}
        ]
        observed = [row for row in target_rows if row.status != "skipped"]
        status = "healthy"
        if failures:
            status = "blocked"
        elif attention:
            status = "warning"

        summary = {
            "kind": "observe_supervisor",
            "observe_only": True,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "status": status,
            "config": config.to_dict(),
            "cycles": cycles,
            "cycle_interval_seconds": cycle_interval_seconds,
            "targets_configured": len(config.targets),
            "targets_enabled": len(config.enabled_targets()),
            "targets_observed": len(observed),
            "failed_targets": len(failures),
            "attention_targets": len(attention),
            "target_status_counts": self._counts(row.status for row in target_rows),
            "patient_status_counts": self._counts(row.patient_status for row in observed),
            "trace_ids": [
                trace_id for row in target_rows for trace_id in row.trace_ids
            ],
            "cycles_detail": cycle_rows,
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    async def _run_cycle(
        self,
        config: ObserveConfig,
        cycle: int,
        actor: str,
    ) -> list[ObserveSupervisorTargetResult]:
        rows = []
        for spec in config.targets:
            if not spec.enabled:
                rows.append(ObserveSupervisorTargetResult(
                    name=spec.name,
                    target=spec.target,
                    status="skipped",
                    result={"reason": "disabled", "cycle": cycle},
                ))
                continue
            rows.append(await self._run_target(spec, cycle=cycle, actor=actor))
        return rows

    async def _run_target(
        self,
        spec: ObserveTargetSpec,
        cycle: int,
        actor: str,
    ) -> ObserveSupervisorTargetResult:
        runner = ObserveLoopRunner(self.root)
        registry = ControlledPatientRegistry(self.root, audit_log=runner.audit_log)
        try:
            patient = build_observe_patient(
                target=spec.target,
                root=self.root,
                patient_id=spec.patient_id,
                service_url=spec.service_url,
                source_root=spec.source_root,
                health_path=spec.health_path,
                pid=spec.pid,
                watch_processes=spec.watch_process_csv(),
                disk_path=spec.disk_path,
                metadata=spec.metadata,
            )
            registered = registry.register(patient, replace=True)
            result = await runner.run(
                patient=registered,
                iterations=spec.iterations,
                interval_seconds=spec.interval_seconds,
                actor=actor,
            )
            result["observe_target"] = spec.target
            result["target_name"] = spec.name
            result["config_cycle"] = cycle
            result["registered_patient"] = registry.stats()
            trace_ids = [
                str(row.get("trace_id", ""))
                for row in list(result.get("iterations_detail", []) or [])
                if row.get("trace_id")
            ]
            return ObserveSupervisorTargetResult(
                name=spec.name,
                target=spec.target,
                status=str(result.get("status", "unknown")),
                patient_status=str(result.get("patient_status", "")),
                summary_file=str(result.get("summary_file", "")),
                trace_ids=trace_ids,
                result=result,
            )
        except Exception as exc:
            return ObserveSupervisorTargetResult(
                name=spec.name,
                target=spec.target,
                status="failed",
                failure=f"{type(exc).__name__}: {exc}",
                result={"cycle": cycle},
            )

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.observe_dir.mkdir(parents=True, exist_ok=True)
        path = self.observe_dir / f"observe_supervisor_{stamp}_summary.json"
        summary["summary_file"] = str(path)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _counts(values: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = str(value or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts
