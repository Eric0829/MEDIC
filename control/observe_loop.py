"""
observe_loop.py
─────────────────────────────────────────────────────────────────────
MEDIC 운영 감시 루프.

이 루프는 환자를 반복 관찰하고 진단/처방/게이트웨이 결과를 기록한다.
중요한 점은 observe-only 기본값이다. 처방을 실제 적용하지 않고,
"무엇을 하려고 했는지"와 "게이트가 어떻게 판단했는지"만 남긴다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.approval_queue import ApprovalQueue
from control.audit_log import AuditLog
from control.diagnostic_runner import ControlledDiagnosticRunner
from control.pipeline_trace import PipelineTrace


@dataclass
class ObserveLoopIteration:
    """One observe-loop pass over a patient."""
    iteration: int
    status: str
    trace_id: str
    duration_ms: float
    severity: str = ""
    root_cause: str = ""
    treatment_type: str = ""
    runner_status: str = ""
    gateway_status: str = ""
    second_opinion_status: str = ""
    second_opinion_verdict: str = ""
    failure: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "status": self.status,
            "trace_id": self.trace_id,
            "duration_ms": self.duration_ms,
            "severity": self.severity,
            "root_cause": self.root_cause,
            "treatment_type": self.treatment_type,
            "runner_status": self.runner_status,
            "gateway_status": self.gateway_status,
            "second_opinion_status": self.second_opinion_status,
            "second_opinion_verdict": self.second_opinion_verdict,
            "failure": self.failure,
        }


class ObserveLoopRunner:
    """Run repeated observe-only diagnostic checks against one patient."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.observe_dir = self.root / "observe_runs"
        self.approval_queue = ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.audit_log = AuditLog(self.root / "control_state" / "audit.jsonl")
        self.trace = PipelineTrace(self.root / "control_state" / "pipeline_trace.jsonl")
        self.diagnostic = ControlledDiagnosticRunner(self.root, trace=self.trace)

    def protect_patient(self, patient: Any) -> Any:
        """Return a proxy that blocks direct treatment calls."""
        return self.diagnostic.protect_patient(patient)

    async def run(
        self,
        patient: Any,
        iterations: int = 3,
        interval_seconds: float = 0.0,
        actor: str = "medic.observe_loop",
    ) -> dict[str, Any]:
        iterations = max(1, int(iterations or 1))
        interval_seconds = max(0.0, float(interval_seconds or 0.0))
        protected_patient = self.protect_patient(patient)
        patient_id = str(getattr(protected_patient, "patient_id", ""))
        patient_type = self._patient_type(protected_patient)

        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        before = self._state_snapshot()
        rows: list[ObserveLoopIteration] = []

        for index in range(1, iterations + 1):
            rows.append(await self._run_iteration(protected_patient, index, actor))
            if interval_seconds and index < iterations:
                await asyncio.sleep(interval_seconds)

        after = self._state_snapshot()
        pending_final = int(after["approval"].get("pending", 0) or 0)
        failures = [row.failure for row in rows if row.failure]
        status = "healthy"
        if failures or pending_final:
            status = "blocked" if pending_final else "warning"

        severity_counts = self._counts(row.severity for row in rows)
        patient_status = "healthy"
        if severity_counts.get("CRITICAL", 0):
            patient_status = "critical"
        elif severity_counts.get("HIGH", 0) or severity_counts.get("MEDIUM", 0):
            patient_status = "attention"

        summary = {
            "kind": "observe_loop",
            "observe_only": True,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "target_patient_id": patient_id,
            "target_patient_type": patient_type,
            "iterations": iterations,
            "interval_seconds": interval_seconds,
            "status": status,
            "patient_status": patient_status,
            "successful_iterations": sum(1 for row in rows if row.status == "observed"),
            "failed_iterations": sum(1 for row in rows if row.status != "observed"),
            "pending_approval_final": pending_final,
            "approval_events": self._delta(after["approval"], before["approval"], "total"),
            "audit_events": self._delta(after["audit"], before["audit"], "events_seen"),
            "trace_events": self._delta(after["trace"], before["trace"], "events_seen"),
            "severity_counts": severity_counts,
            "root_cause_counts": self._counts(row.root_cause for row in rows),
            "treatment_counts": self._counts(row.treatment_type for row in rows),
            "runner_status_counts": self._counts(row.runner_status for row in rows),
            "gateway_status_counts": self._counts(row.gateway_status for row in rows),
            "second_opinion_status_counts": self._counts(row.second_opinion_status for row in rows),
            "second_opinion_verdict_counts": self._counts(row.second_opinion_verdict for row in rows),
            "failures": failures,
            "iterations_detail": [row.to_dict() for row in rows],
            "before": before,
            "after": after,
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    async def _run_iteration(
        self,
        patient: Any,
        index: int,
        actor: str,
    ) -> ObserveLoopIteration:
        started = time.monotonic()
        try:
            result = await self.diagnostic.run(
                patient=patient,
                observe_only=True,
                actor=actor,
                verify_health=False,
            )
        except Exception as exc:
            return ObserveLoopIteration(
                iteration=index,
                status="failed",
                trace_id="",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                failure=str(exc),
            )

        data = result.to_dict()
        diagnosis = data.get("diagnosis", {}) or {}
        prescription = data.get("prescription", {}) or {}
        runner = data.get("runner", {}) or {}
        gateway = runner.get("gateway", {}) or {}
        second = gateway.get("second_opinion", {}) or {}
        second_status = str(second.get("status") or data.get("second_opinion", {}).get("status") or "")
        second_verdict = str(second.get("final_verdict") or data.get("second_opinion", {}).get("verdict") or "")

        return ObserveLoopIteration(
            iteration=index,
            status="observed" if data.get("status") == "observed" else str(data.get("status", "unknown")),
            trace_id=str(data.get("trace_id", "")),
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            severity=str(diagnosis.get("severity", "")),
            root_cause=str(diagnosis.get("root_cause", "")),
            treatment_type=str(prescription.get("treatment_type", "")),
            runner_status=str(runner.get("status", "")),
            gateway_status=str(gateway.get("status", "")),
            second_opinion_status=second_status,
            second_opinion_verdict=second_verdict,
        )

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "approval": self.approval_queue.stats(),
            "audit": self.audit_log.stats(),
            "trace": self.trace.stats(),
        }

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.observe_dir.mkdir(parents=True, exist_ok=True)
        path = self.observe_dir / f"observe_loop_{stamp}_summary.json"
        summary["summary_file"] = str(path)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _patient_type(patient: Any) -> str:
        patient_type = getattr(patient, "patient_type", "")
        return str(getattr(patient_type, "value", patient_type) or "")

    @staticmethod
    def _delta(after: dict[str, Any], before: dict[str, Any], key: str) -> int:
        return int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)

    @staticmethod
    def _counts(values: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = str(value or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts
