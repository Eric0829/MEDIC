"""
control_gateway.py
─────────────────────────────────────────────────────────────────────
MEDIC 외부 감독 게이트웨이.

다른 AI/에이전트/서비스는 처방을 직접 적용하지 않고 이 게이트웨이에
먼저 제출한다.

흐름:
  Prescription
    -> SelfRepairGuard
    -> SecondOpinionGate
    -> PolicyEngine
    -> AuditLog
    -> ApprovalQueue, if needed

이 게이트웨이는 환자의 apply_treatment()를 호출하지 않는다.
실행은 별도의 approved-treatment runner가 맡아야 한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from control.approval_queue import ApprovalQueue
from control.audit_log import AuditLog
from control.policy_engine import PolicyDecision, PolicyEngine
from control.second_opinion_gate import SecondOpinionGate, SecondOpinionVerdict
from infrastructure.self_repair_guard import GuardVerdict, SelfRepairGuard


@dataclass
class GatewayResult:
    """게이트웨이 처리 결과."""
    status: str                  # observed / allowed / queued / blocked
    policy: dict[str, Any]
    guard: dict[str, Any]
    second_opinion: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    audit_event_id: str = ""
    approval_request_id: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "policy": self.policy,
            "guard": self.guard,
            "second_opinion": self.second_opinion,
            "trace_id": self.trace_id,
            "audit_event_id": self.audit_event_id,
            "approval_request_id": self.approval_request_id,
            "notes": self.notes,
        }


class ControlGateway:
    """External supervisor gateway for proposed MEDIC prescriptions."""

    def __init__(
        self,
        root: str | Path,
        guard: Optional[SelfRepairGuard] = None,
        policy: Optional[PolicyEngine] = None,
        second_opinion_gate: Optional[SecondOpinionGate] = None,
        approval_queue: Optional[ApprovalQueue] = None,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        self.root = Path(root)
        self.guard = guard or SelfRepairGuard()
        self.policy = policy or PolicyEngine()
        self.second_opinion_gate = second_opinion_gate or SecondOpinionGate(self.root)
        self.approval_queue = approval_queue or ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.audit_log = audit_log or AuditLog(
            self.root / "control_state" / "audit.jsonl"
        )

    async def review(
        self,
        patient: Any,
        prescription: Any,
        observe_only: bool = True,
        actor: str = "medic.control_gateway",
        approved_request_id: str = "",
        trace_id: str = "",
    ) -> GatewayResult:
        guard_verdict = await self.guard.check(patient, prescription)
        second_opinion_verdict = await self.second_opinion_gate.review(
            patient=patient,
            prescription=prescription,
            guard_verdict=guard_verdict,
        )
        decision = self.policy.evaluate(
            prescription,
            guard_verdict=guard_verdict,
            second_opinion_verdict=second_opinion_verdict,
            observe_only=observe_only,
            approval_verified=bool(approved_request_id),
        )

        status = self._status(decision, observe_only)
        approval_request_id = ""
        if decision.requires_approval and not observe_only:
            req = self.approval_queue.submit(
                prescription,
                decision.reason,
                trace_id=trace_id,
            )
            approval_request_id = req.request_id

        event = self.audit_log.record(
            event_type="policy_review",
            actor=actor,
            patient_id=str(getattr(prescription, "patient_id", "")),
            message=f"{status}: {decision.reason}",
            context={
                "trace_id": trace_id,
                "observe_only": observe_only,
                "prescription_id": str(getattr(prescription, "prescription_id", "")),
                "treatment_type": self._treatment_type(prescription),
                "policy": decision.to_dict(),
                "guard": self._guard_dict(guard_verdict),
                "second_opinion": self._second_opinion_dict(second_opinion_verdict),
                "approval_request_id": approval_request_id,
                "approved_request_id": approved_request_id,
            },
        )

        notes = []
        if observe_only:
            notes.append("observe_only: no treatment execution and no approval queue submission")
        if decision.requires_approval and not observe_only:
            notes.append("queued for human approval")
        if decision.action == "block":
            notes.append("blocked before patient execution")
        if second_opinion_verdict.required:
            if second_opinion_verdict.rejected:
                notes.append("second opinion rejected prescription")
            elif second_opinion_verdict.escalated:
                notes.append("second opinion escalated to human approval")
            else:
                notes.append("second opinion approved prescription")

        return GatewayResult(
            status=status,
            policy=decision.to_dict(),
            guard=self._guard_dict(guard_verdict),
            second_opinion=self._second_opinion_dict(second_opinion_verdict),
            trace_id=trace_id,
            audit_event_id=event.event_id,
            approval_request_id=approval_request_id,
            notes=notes,
        )

    @staticmethod
    def _status(decision: PolicyDecision, observe_only: bool) -> str:
        if observe_only:
            return "observed"
        if decision.action == "queue":
            return "queued"
        if decision.action == "block":
            return "blocked"
        return "allowed"

    @staticmethod
    def _guard_dict(verdict: GuardVerdict) -> dict[str, Any]:
        return {
            "allowed": verdict.allowed,
            "risk_score": verdict.risk_score,
            "risk_level": verdict.risk_level,
            "reasons": verdict.reasons,
            "dry_run_ok": verdict.dry_run_ok,
            "requires_approval": verdict.requires_approval,
        }

    @staticmethod
    def _second_opinion_dict(verdict: SecondOpinionVerdict) -> dict[str, Any]:
        return verdict.to_dict()

    @staticmethod
    def _treatment_type(prescription: Any) -> str:
        tx = getattr(prescription, "treatment_type", "")
        return str(getattr(tx, "value", tx) or "")
