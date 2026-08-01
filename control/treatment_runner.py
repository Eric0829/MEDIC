"""
treatment_runner.py
─────────────────────────────────────────────────────────────────────
승인된 처방 실행 러너.

이 모듈은 patient.apply_treatment()를 직접 호출해도 되는 유일한 경로로
쓰이도록 설계됐다. 실행 전 ControlGateway를 반드시 통과하고, 전체
단계를 PipelineTrace와 AuditLog에 남긴다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from control.audit_log import AuditLog
from control.control_gateway import ControlGateway
from control.execution_context import allow_treatment_execution
from control.patient_proxy import ControlledPatientProxy
from control.pipeline_trace import PipelineTrace


@dataclass
class RunnerResult:
    """처방 실행 러너 결과."""
    status: str                 # observed / queued / blocked / applied / failed
    trace_id: str
    gateway: dict[str, Any]
    treatment: dict[str, Any] = field(default_factory=dict)
    audit_event_id: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "trace_id": self.trace_id,
            "gateway": self.gateway,
            "treatment": self.treatment,
            "audit_event_id": self.audit_event_id,
            "notes": self.notes,
        }


class ControlledTreatmentRunner:
    """Run treatments only after control review, with full trace output."""

    def __init__(
        self,
        root: str | Path,
        gateway: Optional[ControlGateway] = None,
        trace: Optional[PipelineTrace] = None,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        self.root = Path(root)
        self.gateway = gateway or ControlGateway(self.root)
        self.trace = trace or PipelineTrace(
            self.root / "control_state" / "pipeline_trace.jsonl"
        )
        self.audit_log = audit_log or self.gateway.audit_log

    def protect_patient(self, patient: Any) -> ControlledPatientProxy:
        """Return a proxy that blocks direct apply_treatment calls."""
        if isinstance(patient, ControlledPatientProxy):
            return patient
        return ControlledPatientProxy(patient, audit_log=self.audit_log)

    async def run(
        self,
        patient: Any,
        prescription: Any,
        observe_only: bool = True,
        actor: str = "medic.controlled_runner",
        verify_health: bool = True,
        approved_request_id: str = "",
        trace_id: str = "",
    ) -> RunnerResult:
        trace_id = trace_id or self.trace.new_trace_id()
        patient_id = str(getattr(prescription, "patient_id", ""))
        prescription_id = str(getattr(prescription, "prescription_id", ""))

        self.trace.record(
            trace_id, "prescription_received", "ok",
            "Prescription submitted to controlled runner",
            patient_id=patient_id,
            prescription_id=prescription_id,
            context={"treatment_type": self._treatment_type(prescription)},
        )

        started = time.monotonic()
        gateway_result = await self.gateway.review(
            patient=patient,
            prescription=prescription,
            observe_only=observe_only,
            actor=actor,
            approved_request_id=approved_request_id,
            trace_id=trace_id,
        )
        self.trace.record(
            trace_id, "control_gateway", gateway_result.status,
            "Control gateway review completed",
            patient_id=patient_id,
            prescription_id=prescription_id,
            started_at=started,
            context=gateway_result.to_dict(),
        )

        if gateway_result.approval_request_id:
            self.trace.record(
                trace_id, "approval_queue", "queued",
                "Prescription queued for human approval",
                patient_id=patient_id,
                prescription_id=prescription_id,
                context={
                    "approval_request_id": gateway_result.approval_request_id,
                    "policy": gateway_result.policy,
                },
            )

        if observe_only or gateway_result.status in {"observed", "queued", "blocked"}:
            self.trace.record(
                trace_id, "treatment_execution", "skipped",
                f"Execution skipped: {gateway_result.status}",
                patient_id=patient_id,
                prescription_id=prescription_id,
            )
            self.trace.record(
                trace_id, "runner_complete", gateway_result.status,
                "Controlled treatment runner stopped before patient execution",
                patient_id=patient_id,
                prescription_id=prescription_id,
            )
            return RunnerResult(
                status=gateway_result.status,
                trace_id=trace_id,
                gateway=gateway_result.to_dict(),
                notes=["patient.apply_treatment was not called"],
            )

        blacklist = []
        if hasattr(patient, "get_treatment_blacklist"):
            blacklist = list(patient.get_treatment_blacklist() or [])
        if getattr(prescription, "treatment_type", None) in blacklist:
            event = self.audit_log.record(
                event_type="treatment_blacklisted",
                actor=actor,
                patient_id=patient_id,
                message="Patient blacklist rejected treatment after policy allow",
                context={
                    "trace_id": trace_id,
                    "prescription_id": prescription_id,
                    "treatment_type": self._treatment_type(prescription),
                },
            )
            self.trace.record(
                trace_id, "patient_blacklist", "blocked",
                "Patient treatment blacklist blocked execution",
                patient_id=patient_id,
                prescription_id=prescription_id,
            )
            self.trace.record(
                trace_id, "runner_complete", "blocked",
                "Controlled treatment runner completed with blacklist block",
                patient_id=patient_id,
                prescription_id=prescription_id,
            )
            return RunnerResult(
                status="blocked",
                trace_id=trace_id,
                gateway=gateway_result.to_dict(),
                audit_event_id=event.event_id,
                notes=["blocked by patient treatment blacklist"],
            )

        started = time.monotonic()
        try:
            with allow_treatment_execution(
                trace_id=trace_id,
                actor=actor,
                patient_id=patient_id,
                prescription_id=prescription_id,
            ):
                result = await patient.apply_treatment(prescription)
        except Exception as exc:
            event = self.audit_log.record(
                event_type="treatment_exception",
                actor=actor,
                patient_id=patient_id,
                message=str(exc),
                context={"trace_id": trace_id, "prescription_id": prescription_id},
            )
            self.trace.record(
                trace_id, "treatment_execution", "failed",
                "patient.apply_treatment raised an exception",
                patient_id=patient_id,
                prescription_id=prescription_id,
                started_at=started,
                context={"error": str(exc)},
            )
            self.trace.record(
                trace_id, "runner_complete", "failed",
                "Controlled treatment runner completed after treatment exception",
                patient_id=patient_id,
                prescription_id=prescription_id,
            )
            return RunnerResult(
                status="failed",
                trace_id=trace_id,
                gateway=gateway_result.to_dict(),
                audit_event_id=event.event_id,
                notes=[str(exc)],
            )

        treatment = self._treatment_result_dict(result)
        success = bool(treatment.get("success"))
        self.trace.record(
            trace_id, "treatment_execution", "ok" if success else "failed",
            "patient.apply_treatment completed",
            patient_id=patient_id,
            prescription_id=prescription_id,
            started_at=started,
            context=treatment,
        )

        health_ok: Optional[bool] = None
        if verify_health and hasattr(patient, "report_health"):
            started = time.monotonic()
            try:
                health_ok = bool(await patient.report_health())
            except Exception:
                health_ok = False
            self.trace.record(
                trace_id, "health_verification", "ok" if health_ok else "failed",
                "post-treatment health verification completed",
                patient_id=patient_id,
                prescription_id=prescription_id,
                started_at=started,
                context={"health_ok": health_ok},
            )

        final_ok = success and (health_ok is not False)
        final_status = "applied" if final_ok else "failed"
        event = self.audit_log.record(
            event_type="treatment_result",
            actor=actor,
            patient_id=patient_id,
            message=final_status,
            context={
                "trace_id": trace_id,
                "prescription_id": prescription_id,
                "treatment": treatment,
                "health_ok": health_ok,
            },
        )
        self.trace.record(
            trace_id, "runner_complete", final_status,
            "Controlled treatment runner completed",
            patient_id=patient_id,
            prescription_id=prescription_id,
        )
        return RunnerResult(
            status=final_status,
            trace_id=trace_id,
            gateway=gateway_result.to_dict(),
            treatment=treatment,
            audit_event_id=event.event_id,
        )

    @staticmethod
    def _treatment_type(prescription: Any) -> str:
        tx = getattr(prescription, "treatment_type", "")
        return str(getattr(tx, "value", tx) or "")

    @staticmethod
    def _treatment_result_dict(result: Any) -> dict[str, Any]:
        if result is None:
            return {"success": False, "message": "patient returned None"}
        return {
            "prescription_id": str(getattr(result, "prescription_id", "")),
            "patient_id": str(getattr(result, "patient_id", "")),
            "success": bool(getattr(result, "success", False)),
            "message": str(getattr(result, "message", "")),
            "side_effects": list(getattr(result, "side_effects", []) or []),
            "before_vitals": ControlledTreatmentRunner._vitals_dict(
                getattr(result, "before_vitals", None)
            ),
            "after_vitals": ControlledTreatmentRunner._vitals_dict(
                getattr(result, "after_vitals", None)
            ),
        }

    @staticmethod
    def _vitals_dict(vitals: Any) -> dict[str, Any]:
        if vitals is None:
            return {}
        patient_type = getattr(vitals, "patient_type", "")
        return {
            "patient_id": str(getattr(vitals, "patient_id", "")),
            "patient_type": str(getattr(patient_type, "value", patient_type) or ""),
            "is_alive": bool(getattr(vitals, "is_alive", False)),
            "cpu_percent": float(getattr(vitals, "cpu_percent", 0.0) or 0.0),
            "memory_percent": float(getattr(vitals, "memory_percent", 0.0) or 0.0),
            "error_rate": float(getattr(vitals, "error_rate", 0.0) or 0.0),
            "latency_p99_ms": float(getattr(vitals, "latency_p99_ms", 0.0) or 0.0),
            "symptoms": list(getattr(vitals, "symptoms", []) or []),
        }
