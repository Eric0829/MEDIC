"""MEDIC staged benchmark suite.

This runner separates internal regression checks from file-based external and
adversarial cases. It is still a local benchmark, but the external case files
are data artifacts rather than scenarios hard-coded into MEDIC's rules.
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
from control.control_gateway import ControlGateway
from control.control_soak import ControlSoakRunner
from control.diagnostic_harness import DiagnosticHarnessRunner
from control.diagnostic_runner import ControlledDiagnosticRunner
from control.observe_soak import ObserveSoakRunner
from control.pipeline_trace import PipelineTrace
from control.python_service_smoke import PythonServiceSmokeRunner
from control.second_opinion_harness import SecondOpinionHarnessPatient
from control.second_opinion_harness import SecondOpinionHarnessRunner
from infrastructure.self_repair_guard import SelfRepairGuard
from patient_registry.base_patient import (
    PatientType,
    Prescription,
    TreatmentResult,
    TreatmentType,
    Vitals,
)


@dataclass
class BenchmarkCaseResult:
    """One benchmark case result."""

    case_id: str
    case_type: str
    matched: bool
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case_type": self.case_type,
            "matched": self.matched,
            "expected": self.expected,
            "actual": self.actual,
            "notes": self.notes,
        }


class BenchmarkPatient:
    """Small in-memory patient built from benchmark case data."""

    def __init__(self, case: dict[str, Any], patient_id: str) -> None:
        self.case = case
        self._patient_id = patient_id

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return _patient_type(str(self.case.get("patient_type", "generic_process")))

    async def collect_vitals(self) -> Vitals:
        if bool(self.case.get("fail_collect_vitals", False)):
            raise RuntimeError("benchmark vitals collection failure")
        data = dict(self.case.get("vitals", {}) or {})
        return Vitals(
            patient_id=self.patient_id,
            patient_type=self.patient_type,
            is_alive=bool(data.get("is_alive", True)),
            cpu_percent=float(data.get("cpu_percent", 0.0) or 0.0),
            memory_percent=float(data.get("memory_percent", 0.0) or 0.0),
            error_rate=float(data.get("error_rate", 0.0) or 0.0),
            latency_p99_ms=float(data.get("latency_p99_ms", 0.0) or 0.0),
            symptoms=[str(item) for item in list(self.case.get("symptoms", []) or [])],
        )

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        before = None
        try:
            before = await self.collect_vitals()
        except Exception:
            before = None
        return TreatmentResult(
            prescription_id=prescription.prescription_id,
            patient_id=self.patient_id,
            success=True,
            message="benchmark patient should not execute treatment",
            before_vitals=before,
            after_vitals=before,
        )

    async def report_health(self) -> bool:
        return True


class MedicBenchmarkSuiteRunner:
    """Run staged MEDIC benchmark checks and persist one summary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.run_dir = self.root / "benchmark_runs"
        self.trace = PipelineTrace(self.root / "control_state" / "pipeline_trace.jsonl")
        self.approval_queue = ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.audit_log = AuditLog(self.root / "control_state" / "audit.jsonl")

    async def run(
        self,
        external_cases_path: str = "",
        attack_cases_path: str = "",
        control_iterations: int = 1,
        observe_cycles: int = 2,
        observe_interval: float = 0.0,
    ) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        stages: list[dict[str, Any]] = []

        stages.append(await self._run_stage1_internal(control_iterations))
        stages.append(await self._run_stage2_external(external_cases_path))
        stages.append(await self._run_stage3_adversarial(attack_cases_path))
        stages.append(await self._run_stage4_real_target())
        stages.append(await self._run_stage5_observe_soak(observe_cycles, observe_interval))

        failed = [stage for stage in stages if stage.get("status") != "healthy"]
        attention = [stage for stage in stages if stage.get("maturity") == "short_probe"]
        status = "healthy" if not failed else "attention_required"
        summary = {
            "kind": "medic_benchmark_suite",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "started_at": started_at.isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "status": status,
            "stage_count": len(stages),
            "healthy_stages": sum(1 for stage in stages if stage.get("status") == "healthy"),
            "attention_stages": len(failed),
            "short_probe_stages": len(attention),
            "stages": stages,
            "notes": self._suite_notes(stages, observe_cycles, observe_interval),
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    async def _run_stage1_internal(self, control_iterations: int) -> dict[str, Any]:
        diagnostic = await DiagnosticHarnessRunner(self.root).run()
        second = await SecondOpinionHarnessRunner(self.root).run()
        control = await ControlSoakRunner(self.root).run(iterations=max(1, control_iterations))
        status = "healthy"
        failures = []
        if float(diagnostic.get("baseline_match_rate", 0.0) or 0.0) < 1.0:
            failures.append("diagnostic_harness")
        if float(second.get("match_rate", 0.0) or 0.0) < 1.0:
            failures.append("second_opinion_harness")
        if str(control.get("status", "")) != "healthy":
            failures.append("control_soak")
        if failures:
            status = "attention_required"
        return {
            "stage": 1,
            "name": "internal_evaluation",
            "status": status,
            "maturity": "internal_regression",
            "failures": failures,
            "metrics": {
                "diagnostic_cases": self._baseline_cases(diagnostic),
                "diagnostic_match_rate": diagnostic.get("baseline_match_rate", 0.0),
                "second_opinion_cases": second.get("total_cases", 0),
                "second_opinion_match_rate": second.get("match_rate", 0.0),
                "control_soak_status": control.get("status", ""),
                "control_soak_iterations": control.get("iterations", 0),
            },
            "artifacts": {
                "diagnostic_summary": diagnostic.get("summary_file", ""),
                "second_opinion_summary": second.get("summary_file", ""),
                "control_soak_summary": control.get("summary_file", ""),
            },
        }

    async def _run_stage2_external(self, path: str) -> dict[str, Any]:
        cases = self._load_cases(
            path,
            default=self.root / "benchmarks" / "external_cases.jsonl",
            default_case_type="diagnostic",
        )
        rows = []
        for index, case in enumerate(cases, start=1):
            rows.append(await self._run_diagnostic_case(case, index))
        matched = sum(1 for row in rows if row.matched)
        status = "healthy" if matched == len(rows) and rows else "attention_required"
        return {
            "stage": 2,
            "name": "external_case_evaluation",
            "status": status,
            "maturity": "file_based_external_seed",
            "total_cases": len(rows),
            "matched_cases": matched,
            "match_rate": _rate(matched, len(rows)),
            "case_file": str(self._case_path(path, self.root / "benchmarks" / "external_cases.jsonl")),
            "failures": [row.case_id for row in rows if not row.matched],
            "results": [row.to_dict() for row in rows],
        }

    async def _run_stage3_adversarial(self, path: str) -> dict[str, Any]:
        cases = self._load_cases(
            path,
            default=self.root / "benchmarks" / "adversarial_cases.jsonl",
            default_case_type="second_opinion",
        )
        rows = []
        for index, case in enumerate(cases, start=1):
            rows.append(await self._run_second_opinion_case(case, index))
        matched = sum(1 for row in rows if row.matched)
        status = "healthy" if matched == len(rows) and rows else "attention_required"
        return {
            "stage": 3,
            "name": "adversarial_evaluation",
            "status": status,
            "maturity": "file_based_attack_seed",
            "total_cases": len(rows),
            "matched_cases": matched,
            "match_rate": _rate(matched, len(rows)),
            "case_file": str(self._case_path(path, self.root / "benchmarks" / "adversarial_cases.jsonl")),
            "failures": [row.case_id for row in rows if not row.matched],
            "results": [row.to_dict() for row in rows],
        }

    async def _run_stage4_real_target(self) -> dict[str, Any]:
        result = await PythonServiceSmokeRunner(self.root).run()
        status = "healthy" if str(result.get("status", "")) == "healthy" else "attention_required"
        return {
            "stage": 4,
            "name": "real_local_target_evaluation",
            "status": status,
            "maturity": "local_service_smoke",
            "total_cases": result.get("total_cases", 0),
            "matched_cases": result.get("matched_cases", 0),
            "match_rate": result.get("match_rate", 0.0),
            "failures": list(result.get("failures", []) or []),
            "artifact": result.get("summary_file", ""),
        }

    async def _run_stage5_observe_soak(
        self,
        cycles: int,
        interval: float,
    ) -> dict[str, Any]:
        cycles = max(1, int(cycles or 1))
        interval = max(0.0, float(interval or 0.0))
        result = await ObserveSoakRunner(self.root).run(
            config_path=str(self.root / "config" / "observe_daemon.example.json"),
            cycles=cycles,
            interval_seconds=interval,
        )
        duration_seconds = max(0.0, (cycles - 1) * interval)
        maturity = "long_soak" if duration_seconds >= 3600 else "short_probe"
        status = "healthy" if str(result.get("status", "")) == "healthy" else "attention_required"
        return {
            "stage": 5,
            "name": "long_running_operations_evaluation",
            "status": status,
            "maturity": maturity,
            "requested_cycles": cycles,
            "interval_seconds": interval,
            "planned_duration_seconds": duration_seconds,
            "cycles_completed": result.get("cycles_completed", 0),
            "healthy_cycles": result.get("healthy_cycles", 0),
            "failed_cycles": result.get("failed_cycles", 0),
            "alert_count": result.get("alert_count", 0),
            "active_incidents": result.get("active_incidents", 0),
            "approval_events": result.get("approval_events", 0),
            "failures": list(result.get("failures", []) or []),
            "artifact": result.get("summary_file", ""),
        }

    async def _run_diagnostic_case(
        self,
        case: dict[str, Any],
        index: int,
    ) -> BenchmarkCaseResult:
        case_id = str(case.get("case_id") or f"external-{index:03d}")
        patient = BenchmarkPatient(case, f"benchmark-{index:03d}-{case_id}")
        diagnostic = ControlledDiagnosticRunner(self.root, trace=self.trace)
        protected = diagnostic.protect_patient(patient)
        result = await diagnostic.run(
            patient=protected,
            observe_only=True,
            actor="medic.benchmark.external",
            verify_health=False,
        )
        data = result.to_dict()
        diagnosis = dict(data.get("diagnosis", {}) or {})
        prescription = dict(data.get("prescription", {}) or {})
        expected = dict(case.get("expected", {}) or {})
        actual = {
            "severity": diagnosis.get("severity", ""),
            "root_cause": diagnosis.get("root_cause", ""),
            "treatment": prescription.get("treatment_type", ""),
            "status": data.get("status", ""),
            "trace_id": data.get("trace_id", ""),
        }
        matched = (
            str(expected.get("severity", "")) == actual["severity"]
            and str(expected.get("root_cause", "")) == actual["root_cause"]
            and str(expected.get("treatment", "")) == actual["treatment"]
        )
        notes = []
        if not matched:
            notes.append("expected diagnostic outputs did not match actual outputs")
        return BenchmarkCaseResult(
            case_id=case_id,
            case_type="diagnostic",
            matched=matched,
            expected=expected,
            actual=actual,
            notes=notes,
        )

    async def _run_second_opinion_case(
        self,
        case: dict[str, Any],
        index: int,
    ) -> BenchmarkCaseResult:
        case_id = str(case.get("case_id") or f"attack-{index:03d}")
        treatment_type = _treatment_type(str(case.get("treatment_type", "monitor")))
        prescription = Prescription(
            patient_id="benchmark-attack-patient",
            treatment_type=treatment_type,
            payload=dict(case.get("payload", {}) or {}),
            issued_by="medic.benchmark.adversarial",
            confidence=float(case.get("confidence", 0.90) or 0.90),
            risk_level=str(case.get("risk_level", "HIGH") or "HIGH"),
        )
        gateway = ControlGateway(
            self.root,
            guard=SelfRepairGuard(max_daily_high_risk=1000),
            approval_queue=self.approval_queue,
            audit_log=self.audit_log,
        )
        trace_id = self.trace.new_trace_id()
        self.trace.record(
            trace_id,
            "prescription_received",
            "ok",
            "Adversarial benchmark submitted prescription",
            patient_id=prescription.patient_id,
            prescription_id=prescription.prescription_id,
            context={"case_id": case_id},
        )
        result = await gateway.review(
            patient=SecondOpinionHarnessPatient(),
            prescription=prescription,
            observe_only=False,
            actor="medic.benchmark.adversarial",
            trace_id=trace_id,
        )
        data = result.to_dict()
        approval_request_id = str(data.get("approval_request_id", ""))
        cleanup_status = ""
        if approval_request_id:
            cleanup_status = self._close_approval(
                request_id=approval_request_id,
                prescription=prescription,
                trace_id=trace_id,
            )
        self.trace.record(
            trace_id,
            "treatment_execution",
            "skipped",
            "Adversarial benchmark never executes treatment",
            patient_id=prescription.patient_id,
            prescription_id=prescription.prescription_id,
        )
        expected = dict(case.get("expected", {}) or {})
        policy = dict(data.get("policy", {}) or {})
        second = dict(data.get("second_opinion", {}) or {})
        actual = {
            "status": data.get("status", ""),
            "policy_action": policy.get("action", ""),
            "second_verdict": second.get("final_verdict", ""),
            "approval_request": bool(approval_request_id),
            "approval_request_id": approval_request_id,
            "cleanup_status": cleanup_status,
            "trace_id": trace_id,
        }
        matched = (
            str(expected.get("status", "")) == actual["status"]
            and str(expected.get("policy_action", "")) == actual["policy_action"]
            and str(expected.get("second_verdict", "")) == actual["second_verdict"]
            and bool(expected.get("approval_request", False)) == actual["approval_request"]
        )
        notes = []
        if not matched:
            notes.append("expected gateway outputs did not match actual outputs")
        return BenchmarkCaseResult(
            case_id=case_id,
            case_type="second_opinion",
            matched=matched,
            expected=expected,
            actual=actual,
            notes=notes,
        )

    def _close_approval(
        self,
        request_id: str,
        prescription: Prescription,
        trace_id: str,
    ) -> str:
        item = self.approval_queue.decide(
            request_id,
            "rejected",
            decided_by="benchmark_suite",
            note="benchmark request closed without execution",
        )
        self.audit_log.record(
            event_type="approval_rejected",
            actor="benchmark_suite",
            patient_id=prescription.patient_id,
            message="benchmark approval request closed without execution",
            context={
                "trace_id": trace_id,
                "request_id": request_id,
                "prescription_id": prescription.prescription_id,
                "treatment_type": _enum_value(prescription.treatment_type),
            },
        )
        self.trace.record(
            trace_id,
            "approval_decision",
            item.status,
            "Benchmark suite closed queued request",
            patient_id=prescription.patient_id,
            prescription_id=prescription.prescription_id,
            context={"request_id": request_id},
        )
        return item.status

    def _load_cases(
        self,
        path: str,
        default: Path,
        default_case_type: str,
    ) -> list[dict[str, Any]]:
        case_path = self._case_path(path, default)
        rows = []
        for line_no, line in enumerate(case_path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            row = json.loads(stripped)
            if not isinstance(row, dict):
                raise ValueError(f"{case_path}:{line_no} must be a JSON object")
            row.setdefault("case_type", default_case_type)
            rows.append(row)
        return rows

    @staticmethod
    def _case_path(path: str, default: Path) -> Path:
        if not path:
            return default
        candidate = Path(path)
        if candidate.is_absolute():
            return candidate
        cwd_candidate = candidate
        root_candidate = default.parent.parent / candidate
        return cwd_candidate if cwd_candidate.exists() else root_candidate

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"medic_benchmark_{stamp}_summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _baseline_cases(summary: dict[str, Any]) -> int:
        baseline = str(summary.get("baseline_variant", "baseline"))
        for report in list(summary.get("reports", []) or []):
            if report.get("variant") == baseline:
                return int(report.get("total_cases", 0) or 0)
        return 0

    @staticmethod
    def _suite_notes(
        stages: list[dict[str, Any]],
        observe_cycles: int,
        observe_interval: float,
    ) -> list[str]:
        notes = []
        if any(stage.get("status") != "healthy" for stage in stages):
            notes.append("At least one benchmark stage requires attention.")
        planned = max(0, int(observe_cycles or 0) - 1) * max(0.0, float(observe_interval or 0.0))
        if planned < 3600:
            notes.append(
                "Stage 5 was a short probe, not a true multi-hour or multi-day soak."
            )
        notes.append(
            "External/adversarial case files are local seed benchmarks; third-party blind cases are still needed for independent validation."
        )
        return notes


def _patient_type(value: str) -> PatientType:
    try:
        return PatientType(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown patient_type: {value}") from exc


def _treatment_type(value: str) -> TreatmentType:
    try:
        return TreatmentType(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown treatment_type: {value}") from exc


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _rate(num: int | float, den: int | float) -> float:
    if not den:
        return 0.0
    return round(float(num) / float(den), 4)
