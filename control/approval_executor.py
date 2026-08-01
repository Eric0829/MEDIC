"""
approval_executor.py
─────────────────────────────────────────────────────────────────────
승인된 처방 실행기.

ApprovalQueue에서 approved 상태인 요청만 꺼내 Prescription으로 복원하고,
ControlledTreatmentRunner를 통해 실행한다. 승인됐더라도 위험 payload
차단과 SelfRepairGuard는 다시 통과한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from control.approval_queue import ApprovalQueue, ApprovalRequest
from control.audit_log import AuditLog
from control.controlled_registry import ControlledPatientRegistry
from control.treatment_runner import ControlledTreatmentRunner
from patient_registry.base_patient import Prescription, TreatmentType


@dataclass
class ApprovalExecutionResult:
    """승인 요청 실행 결과."""
    status: str
    request_id: str
    runner: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)
    audit_event_id: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "request_id": self.request_id,
            "runner": self.runner,
            "request": self.request,
            "audit_event_id": self.audit_event_id,
            "notes": self.notes,
        }


class ApprovedTreatmentExecutor:
    """Execute only approved approval-queue requests."""

    def __init__(
        self,
        root: str | Path,
        queue: Optional[ApprovalQueue] = None,
        registry: Optional[ControlledPatientRegistry] = None,
        runner: Optional[ControlledTreatmentRunner] = None,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        self.root = Path(root)
        self.queue = queue or ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.audit_log = audit_log or AuditLog(
            self.root / "control_state" / "audit.jsonl"
        )
        self.registry = registry or ControlledPatientRegistry(
            self.root,
            audit_log=self.audit_log,
        )
        self.runner = runner or ControlledTreatmentRunner(
            self.root,
            audit_log=self.audit_log,
        )

    async def execute(
        self,
        request_id: str,
        actor: str = "medic.approval_executor",
        verify_health: bool = True,
    ) -> ApprovalExecutionResult:
        request = self.queue.get(request_id)
        if request.status != "approved":
            raise ValueError(f"approval request is {request.status}, not approved")

        prescription = self._restore_prescription(request)
        patient = self.registry.get(request.patient_id)
        trace_id = request.trace_id or ""

        if trace_id:
            self.runner.trace.record(
                trace_id,
                "approval_execution",
                "started",
                "Approved request handed to controlled treatment runner",
                patient_id=request.patient_id,
                prescription_id=request.prescription_id,
                context={"request_id": request.request_id},
            )

        runner_result = await self.runner.run(
            patient=patient,
            prescription=prescription,
            observe_only=False,
            actor=actor,
            verify_health=verify_health,
            approved_request_id=request.request_id,
            trace_id=trace_id,
        )
        success = runner_result.status == "applied"
        updated = self.queue.mark_execution(
            request_id=request.request_id,
            success=success,
            executed_by=actor,
            note=f"runner_status={runner_result.status}",
            result=runner_result.to_dict(),
        )
        if trace_id:
            self.runner.trace.record(
                trace_id,
                "approval_execution",
                updated.status,
                "Approved request execution completed",
                patient_id=request.patient_id,
                prescription_id=request.prescription_id,
                context={
                    "request_id": request.request_id,
                    "runner_status": runner_result.status,
                },
            )
        event = self.audit_log.record(
            event_type="approval_executed" if success else "approval_execution_failed",
            actor=actor,
            patient_id=request.patient_id,
            message=f"approved request execution {runner_result.status}",
            context={
                "trace_id": trace_id,
                "request_id": request.request_id,
                "prescription_id": request.prescription_id,
                "runner": runner_result.to_dict(),
            },
        )
        return ApprovalExecutionResult(
            status=updated.status,
            request_id=request.request_id,
            runner=runner_result.to_dict(),
            request=updated.to_dict(),
            audit_event_id=event.event_id,
        )

    @staticmethod
    def _restore_prescription(request: ApprovalRequest) -> Prescription:
        data = dict(request.prescription or {})
        treatment_type = data.get("treatment_type") or request.treatment_type
        try:
            tx = TreatmentType(treatment_type)
        except Exception:
            tx = TreatmentType.MANUAL_INTERVENTION
        return Prescription(
            prescription_id=str(data.get("prescription_id") or request.prescription_id),
            patient_id=str(data.get("patient_id") or request.patient_id),
            treatment_type=tx,
            payload=dict(data.get("payload", {}) or {}),
            issued_by=str(data.get("issued_by") or "approval_queue"),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            risk_level=str(data.get("risk_level") or request.risk_level),
            expires_at=data.get("expires_at"),
        )
