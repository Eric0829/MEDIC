"""
policy_engine.py
─────────────────────────────────────────────────────────────────────
MEDIC 외부 감독 정책 엔진.

PolicyEngine은 처방을 실행하지 않는다. 처방이 자동 적용 가능한지,
승인 큐로 가야 하는지, 차단해야 하는지만 결정한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PolicyDecision:
    """정책 판정 결과."""
    action: str                 # allow / queue / block
    reason: str
    rules_hit: list[str] = field(default_factory=list)
    requires_audit: bool = True

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.action == "queue"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "rules_hit": self.rules_hit,
            "requires_audit": self.requires_audit,
        }


class PolicyEngine:
    """
    MEDIC이 외부 감독자로 남기 위한 최소 정책.

    기본 원칙:
      - observe_only면 항상 allow지만 실행은 하지 않는다.
      - patch_code, fine_tune_trigger, quarantine은 승인 큐로 보낸다.
      - 위험 payload는 차단한다.
      - high risk 처방은 승인 큐로 보낸다.
    """

    HIGH_RISK_TREATMENTS = {
        "patch_code",
        "quarantine",
        "fine_tune_trigger",
        "manual_intervention",
    }

    MEDIUM_RISK_TREATMENTS = {
        "restart",
        "rollback",
        "weight_rollback",
        "scale_down",
        "k8s_rolling_update",
        "k8s_hpa_adjust",
    }

    BLOCKED_PAYLOAD_MARKERS = [
        "rm -rf",
        "del /f",
        "format ",
        "bypass approval",
        "ignore policy",
        "disable audit",
    ]

    def __init__(self, allow_medium_auto: bool = False) -> None:
        self.allow_medium_auto = allow_medium_auto

    def evaluate(
        self,
        prescription: Any,
        guard_verdict: Any = None,
        second_opinion_verdict: Any = None,
        observe_only: bool = True,
        approval_verified: bool = False,
    ) -> PolicyDecision:
        tx = self._treatment_type(prescription)
        payload = self._payload(prescription)
        risk_level = str(getattr(prescription, "risk_level", "") or "").upper()
        confidence = float(getattr(prescription, "confidence", 0.0) or 0.0)
        rules: list[str] = []

        if observe_only:
            return PolicyDecision(
                "allow",
                "observe_only mode: execution disabled by caller",
                ["observe_only"],
            )

        marker = self._blocked_marker(payload)
        if marker:
            return PolicyDecision(
                "block",
                f"blocked payload marker detected: {marker}",
                ["blocked_payload"],
            )

        if guard_verdict is not None:
            if not getattr(guard_verdict, "allowed", True):
                return PolicyDecision(
                    "block",
                    "SelfRepairGuard rejected prescription",
                    ["guard_rejected"],
                )
            if getattr(guard_verdict, "requires_approval", False) and not approval_verified:
                return PolicyDecision(
                    "queue",
                    "SelfRepairGuard requires human approval",
                    ["guard_requires_approval"],
                )

        if second_opinion_verdict is not None and getattr(second_opinion_verdict, "required", False):
            final_verdict = str(
                getattr(second_opinion_verdict, "final_verdict", "") or ""
            ).upper()
            if final_verdict == "REJECT":
                return PolicyDecision(
                    "block",
                    "SecondOpinionGate rejected prescription",
                    ["second_opinion_rejected"],
                )
            if final_verdict != "APPROVE" and not approval_verified:
                return PolicyDecision(
                    "queue",
                    "SecondOpinionGate requires human approval",
                    ["second_opinion_escalated"],
                )

        if approval_verified:
            return PolicyDecision(
                "allow",
                "approval request verified",
                ["approval_verified"],
            )

        if confidence and confidence < 0.4:
            return PolicyDecision(
                "queue",
                f"low prescription confidence: {confidence:.2f}",
                ["low_confidence"],
            )

        if risk_level == "HIGH":
            return PolicyDecision(
                "queue",
                "high risk prescription",
                ["high_risk"],
            )

        if tx in self.HIGH_RISK_TREATMENTS:
            return PolicyDecision(
                "queue",
                f"{tx} requires approval",
                ["high_risk_treatment"],
            )

        if tx in self.MEDIUM_RISK_TREATMENTS and not self.allow_medium_auto:
            return PolicyDecision(
                "queue",
                f"{tx} is medium risk and auto-approval is disabled",
                ["medium_risk_treatment"],
            )

        rules.append("low_risk_auto")
        return PolicyDecision("allow", "low risk prescription", rules)

    def stats(self) -> dict[str, Any]:
        return {
            "allow_medium_auto": self.allow_medium_auto,
            "high_risk_treatments": sorted(self.HIGH_RISK_TREATMENTS),
            "medium_risk_treatments": sorted(self.MEDIUM_RISK_TREATMENTS),
            "blocked_payload_markers": len(self.BLOCKED_PAYLOAD_MARKERS),
        }

    @staticmethod
    def _treatment_type(prescription: Any) -> str:
        tx = getattr(prescription, "treatment_type", "")
        return str(getattr(tx, "value", tx) or "")

    @staticmethod
    def _payload(prescription: Any) -> dict[str, Any]:
        payload = getattr(prescription, "payload", {}) or {}
        return payload if isinstance(payload, dict) else {"value": payload}

    def _blocked_marker(self, payload: dict[str, Any]) -> str:
        text = str(payload).lower()
        for marker in self.BLOCKED_PAYLOAD_MARKERS:
            if marker in text:
                return marker
        return ""
