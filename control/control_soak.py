"""
control_soak.py
─────────────────────────────────────────────────────────────────────
MEDIC control layer 반복 안정성 soak.

짧은 smoke/harness가 "한 번 맞는가"를 본다면, 이 runner는 같은 검사를
여러 번 반복해도 approval queue, causal report, self-control 상태가
흔들리지 않는지 본다. 실제 환자 치료 실행을 목적으로 하지 않으며,
harness가 생성한 승인 요청은 각 harness 내부에서 닫힌다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.approval_queue import ApprovalQueue
from control.audit_log import AuditLog
from control.causal_report import CausalReportBuilder
from control.diagnostic_harness import DiagnosticHarnessRunner
from control.pipeline_trace import PipelineTrace
from control.second_opinion_harness import SecondOpinionHarnessRunner
from control.self_control_layer import MedicSelfControlLayer


@dataclass
class ControlSoakIteration:
    """One control soak iteration."""
    iteration: int
    status: str
    duration_ms: float
    diagnostic_match_rate: float = 0.0
    diagnostic_cases: int = 0
    second_opinion_match_rate: float = 0.0
    second_opinion_cases: int = 0
    causal_status: str = ""
    self_control_status: str = ""
    pending_approval: int = 0
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "diagnostic_match_rate": self.diagnostic_match_rate,
            "diagnostic_cases": self.diagnostic_cases,
            "second_opinion_match_rate": self.second_opinion_match_rate,
            "second_opinion_cases": self.second_opinion_cases,
            "causal_status": self.causal_status,
            "self_control_status": self.self_control_status,
            "pending_approval": self.pending_approval,
            "failures": self.failures,
        }


class ControlSoakRunner:
    """Repeat core control checks and write a soak summary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.soak_dir = self.root / "soak_runs"
        self.approval_queue = ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.audit_log = AuditLog(self.root / "control_state" / "audit.jsonl")
        self.trace = PipelineTrace(self.root / "control_state" / "pipeline_trace.jsonl")

    async def run(self, iterations: int = 3) -> dict[str, Any]:
        iterations = max(1, int(iterations or 1))
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        before = self._state_snapshot()

        rows: list[ControlSoakIteration] = []
        for index in range(1, iterations + 1):
            rows.append(await self._run_iteration(index))

        after = self._state_snapshot()
        failures = [failure for row in rows for failure in row.failures]
        pending_final = int(after["approval"].get("pending", 0) or 0)
        status = "healthy"
        if failures or pending_final:
            status = "blocked" if pending_final else "warning"

        summary = {
            "kind": "control_soak",
            "observe_only": False,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "iterations": iterations,
            "status": status,
            "healthy_iterations": sum(1 for row in rows if row.status == "healthy"),
            "failed_iterations": sum(1 for row in rows if row.status != "healthy"),
            "diagnostic_min_match_rate": self._min_rate(rows, "diagnostic_match_rate"),
            "second_opinion_min_match_rate": self._min_rate(rows, "second_opinion_match_rate"),
            "causal_status_counts": self._status_counts(row.causal_status for row in rows),
            "self_control_status_counts": self._status_counts(row.self_control_status for row in rows),
            "pending_approval_final": pending_final,
            "approval_events": self._delta(after["approval"], before["approval"], "total"),
            "audit_events": self._delta(after["audit"], before["audit"], "events_seen"),
            "trace_events": self._delta(after["trace"], before["trace"], "events_seen"),
            "treatment_totals": {},
            "failures": failures,
            "iterations_detail": [row.to_dict() for row in rows],
            "before": before,
            "after": after,
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    async def _run_iteration(self, index: int) -> ControlSoakIteration:
        started = time.monotonic()
        failures: list[str] = []
        diagnostic_match_rate = 0.0
        diagnostic_cases = 0
        second_match_rate = 0.0
        second_cases = 0
        causal_status = ""
        self_status = ""

        try:
            diagnostic = await DiagnosticHarnessRunner(self.root).run()
            diagnostic_match_rate = float(diagnostic.get("baseline_match_rate", 0.0) or 0.0)
            baseline = self._baseline_report(diagnostic)
            diagnostic_cases = int(baseline.get("total_cases", 0) or 0)
            if diagnostic_match_rate < 1.0:
                failures.append(f"diagnostic_harness={diagnostic_match_rate:.1%}")
        except Exception as exc:
            failures.append(f"diagnostic_harness_exception:{exc}")

        try:
            second = await SecondOpinionHarnessRunner(self.root).run()
            second_match_rate = float(second.get("match_rate", 0.0) or 0.0)
            second_cases = int(second.get("total_cases", 0) or 0)
            if second_match_rate < 1.0:
                failures.append(f"second_opinion_harness={second_match_rate:.1%}")
        except Exception as exc:
            failures.append(f"second_opinion_harness_exception:{exc}")

        try:
            causal = CausalReportBuilder(self.root).build()
            causal_status = causal.status
            if causal.status != "healthy":
                failures.append(f"causal_report={causal.status}")
        except Exception as exc:
            failures.append(f"causal_report_exception:{exc}")

        pending = int(self.approval_queue.stats().get("pending", 0) or 0)
        if pending:
            failures.append(f"pending_approval={pending}")

        try:
            self_report = MedicSelfControlLayer(self.root).inspect()
            self_status = self_report.status
            if self_report.status != "healthy":
                failures.append(f"self_control={self_report.status}")
        except Exception as exc:
            failures.append(f"self_control_exception:{exc}")

        status = "healthy" if not failures else "warning"
        return ControlSoakIteration(
            iteration=index,
            status=status,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            diagnostic_match_rate=diagnostic_match_rate,
            diagnostic_cases=diagnostic_cases,
            second_opinion_match_rate=second_match_rate,
            second_opinion_cases=second_cases,
            causal_status=causal_status,
            self_control_status=self_status,
            pending_approval=pending,
            failures=failures,
        )

    def _state_snapshot(self) -> dict[str, Any]:
        return {
            "approval": self.approval_queue.stats(),
            "audit": self.audit_log.stats(),
            "trace": self.trace.stats(),
        }

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.soak_dir.mkdir(parents=True, exist_ok=True)
        path = self.soak_dir / f"control_soak_{stamp}_summary.json"
        summary["summary_file"] = str(path)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _baseline_report(summary: dict[str, Any]) -> dict[str, Any]:
        baseline = str(summary.get("baseline_variant", "baseline"))
        for report in list(summary.get("reports", []) or []):
            if report.get("variant") == baseline:
                return report
        reports = list(summary.get("reports", []) or [])
        return reports[0] if reports else {}

    @staticmethod
    def _delta(after: dict[str, Any], before: dict[str, Any], key: str) -> int:
        return int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)

    @staticmethod
    def _min_rate(rows: list[ControlSoakIteration], attr: str) -> float:
        if not rows:
            return 0.0
        return min(float(getattr(row, attr, 0.0) or 0.0) for row in rows)

    @staticmethod
    def _status_counts(values: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = str(value or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts
