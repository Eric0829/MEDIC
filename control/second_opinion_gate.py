"""
second_opinion_gate.py
─────────────────────────────────────────────────────────────────────
ControlGateway용 2차 소견 게이트.

이 게이트는 외부 API나 LLM에 기대지 않는다. 가능한 경우 기존
LVectorReviewer를 쓰고, DeCODE가 없으면 정적 패치/페이로드 검사를
사용한다. 목적은 자동 수정 컨트롤러가 아니라, 고위험 처방이 단일
판단만으로 통과하지 못하게 만드는 강제 판정 레이어다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from second_opinion.lvector_reviewer import LVectorReviewer


@dataclass
class SecondOpinionVerdict:
    """2차 소견 게이트 판정."""
    required: bool
    final_verdict: str          # APPROVE / REJECT / ESCALATE / NOT_REQUIRED
    reason: str
    reviewer: str = "second_opinion_gate"
    confidence: float = 0.0
    concerns: list[str] = field(default_factory=list)
    requires_human: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return not self.required or self.final_verdict == "APPROVE"

    @property
    def rejected(self) -> bool:
        return self.final_verdict == "REJECT"

    @property
    def escalated(self) -> bool:
        return self.final_verdict == "ESCALATE"

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "final_verdict": self.final_verdict,
            "reason": self.reason,
            "reviewer": self.reviewer,
            "confidence": self.confidence,
            "concerns": self.concerns,
            "requires_human": self.requires_human,
            "evidence": self.evidence,
        }


class SecondOpinionGate:
    """Deterministic second-opinion gate for high-risk prescriptions."""

    ALWAYS_REQUIRED_TREATMENTS = {
        "patch_code",
        "fine_tune_trigger",
        "quarantine",
        "manual_intervention",
    }

    HIGH_RISK_REQUIRED_TREATMENTS = {
        "prompt_patch",
        "weight_rollback",
        "rollback",
        "restart",
        "config_change",
        "monitor",
    }

    BLOCKED_PAYLOAD_MARKERS = [
        "rm -rf",
        "del /f",
        "format ",
        "disable audit",
        "bypass approval",
        "ignore policy",
        "ignore safety",
        "exfiltrate",
        "steal secret",
        "leak token",
        "delete logs",
        "drop database",
    ]

    DANGEROUS_CODE_PATTERNS = [
        "eval(",
        "exec(",
        "os.system",
        "subprocess",
        "__import__",
        "globals()",
        "locals()",
    ]

    PATCH_FIELDS = [
        "diff_patch",
        "proposed_patch",
        "patch",
        "code_patch",
        "unified_diff",
    ]

    SOURCE_FIELDS = [
        "source_code",
        "original_code",
        "before_code",
    ]

    def __init__(
        self,
        root: str | Path,
        reviewer: Optional[LVectorReviewer] = None,
    ) -> None:
        self.root = Path(root)
        self.reviewer = reviewer or LVectorReviewer(source_root=str(self.root))

    async def review(
        self,
        patient: Any,
        prescription: Any,
        guard_verdict: Any = None,
    ) -> SecondOpinionVerdict:
        tx = self._treatment_type(prescription)
        payload = self._payload(prescription)
        risk_level = str(getattr(prescription, "risk_level", "") or "").upper()

        required = self.requires_review(prescription, guard_verdict)
        if not required:
            return SecondOpinionVerdict(
                required=False,
                final_verdict="NOT_REQUIRED",
                reason="second opinion not required for this prescription",
                confidence=1.0,
                evidence={
                    "treatment_type": tx,
                    "risk_level": risk_level,
                },
            )

        marker = self._blocked_marker(payload)
        if marker:
            return SecondOpinionVerdict(
                required=True,
                final_verdict="REJECT",
                reason=f"dangerous payload marker detected: {marker}",
                confidence=0.95,
                concerns=[f"blocked_payload:{marker}"],
                evidence={
                    "treatment_type": tx,
                    "risk_level": risk_level,
                },
            )

        patch = self._first_payload_text(payload, self.PATCH_FIELDS)
        source = self._first_payload_text(payload, self.SOURCE_FIELDS)
        file_path = str(payload.get("file_path") or payload.get("target_file") or "target.py")
        if not source and patch and hasattr(patient, "get_source_code"):
            try:
                source = str(await patient.get_source_code(file_path) or "")
            except Exception:
                source = ""

        if patch:
            return self._review_patch(
                patch=patch,
                source=source,
                file_path=file_path,
                treatment_type=tx,
                risk_level=risk_level,
            )

        return self._review_payload(
            payload=payload,
            treatment_type=tx,
            risk_level=risk_level,
        )

    def requires_review(self, prescription: Any, guard_verdict: Any = None) -> bool:
        tx = self._treatment_type(prescription)
        risk_level = str(getattr(prescription, "risk_level", "") or "").upper()
        if tx in self.ALWAYS_REQUIRED_TREATMENTS:
            return True
        if risk_level == "HIGH" and tx in self.HIGH_RISK_REQUIRED_TREATMENTS:
            return True
        if guard_verdict is not None and getattr(guard_verdict, "requires_approval", False):
            return True
        return False

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "reviewer": "lvector" if self.reviewer.is_available else "static",
            "always_required_treatments": sorted(self.ALWAYS_REQUIRED_TREATMENTS),
            "high_risk_required_treatments": sorted(self.HIGH_RISK_REQUIRED_TREATMENTS),
            "blocked_payload_markers": len(self.BLOCKED_PAYLOAD_MARKERS),
            "dangerous_code_patterns": len(self.DANGEROUS_CODE_PATTERNS),
        }

    def _review_patch(
        self,
        patch: str,
        source: str,
        file_path: str,
        treatment_type: str,
        risk_level: str,
    ) -> SecondOpinionVerdict:
        pattern = self._dangerous_code_pattern(patch)
        if pattern:
            return SecondOpinionVerdict(
                required=True,
                final_verdict="REJECT",
                reason=f"dangerous code pattern detected: {pattern}",
                confidence=0.95,
                concerns=[f"dangerous_code:{pattern}"],
                evidence={
                    "treatment_type": treatment_type,
                    "risk_level": risk_level,
                    "file_path": file_path,
                    "patch_chars": len(patch),
                    "reviewer": "static_pattern",
                },
            )

        lvector = self.reviewer.review(
            source_code=source,
            proposed_patch=patch,
            file_path=file_path,
        )
        final = str(getattr(lvector, "verdict", "ESCALATE") or "ESCALATE").upper()
        concerns = list(getattr(lvector, "concerns", []) or [])
        if final == "REJECT":
            return SecondOpinionVerdict(
                required=True,
                final_verdict="REJECT",
                reason=str(getattr(lvector, "reasoning", "LVector reviewer rejected patch")),
                confidence=float(getattr(lvector, "confidence", 0.0) or 0.0),
                concerns=concerns,
                evidence=self._lvector_evidence(lvector, treatment_type, risk_level, file_path, patch),
            )
        if final == "APPROVE":
            return SecondOpinionVerdict(
                required=True,
                final_verdict="APPROVE",
                reason=str(getattr(lvector, "reasoning", "LVector reviewer approved patch")),
                confidence=float(getattr(lvector, "confidence", 0.0) or 0.0),
                concerns=concerns,
                evidence=self._lvector_evidence(lvector, treatment_type, risk_level, file_path, patch),
            )
        return SecondOpinionVerdict(
            required=True,
            final_verdict="ESCALATE",
            reason="LVector reviewer requested escalation",
            confidence=float(getattr(lvector, "confidence", 0.0) or 0.0),
            concerns=concerns,
            requires_human=True,
            evidence=self._lvector_evidence(lvector, treatment_type, risk_level, file_path, patch),
        )

    def _review_payload(
        self,
        payload: dict[str, Any],
        treatment_type: str,
        risk_level: str,
    ) -> SecondOpinionVerdict:
        text = str(payload).lower()
        pattern = self._dangerous_code_pattern(text)
        if pattern:
            return SecondOpinionVerdict(
                required=True,
                final_verdict="REJECT",
                reason=f"dangerous code-like payload detected: {pattern}",
                confidence=0.90,
                concerns=[f"dangerous_payload:{pattern}"],
                evidence={
                    "treatment_type": treatment_type,
                    "risk_level": risk_level,
                    "payload_keys": sorted(str(key) for key in payload.keys()),
                },
            )

        if treatment_type == "manual_intervention" and not payload:
            return SecondOpinionVerdict(
                required=True,
                final_verdict="ESCALATE",
                reason="manual intervention lacks reviewable payload",
                confidence=0.45,
                requires_human=True,
                concerns=["missing_review_payload"],
                evidence={
                    "treatment_type": treatment_type,
                    "risk_level": risk_level,
                    "payload_keys": [],
                },
            )

        return SecondOpinionVerdict(
            required=True,
            final_verdict="APPROVE",
            reason="static second-opinion payload review found no explicit danger",
            confidence=0.70,
            evidence={
                "treatment_type": treatment_type,
                "risk_level": risk_level,
                "payload_keys": sorted(str(key) for key in payload.keys()),
            },
        )

    @staticmethod
    def _lvector_evidence(
        lvector: Any,
        treatment_type: str,
        risk_level: str,
        file_path: str,
        patch: str,
    ) -> dict[str, Any]:
        return {
            "treatment_type": treatment_type,
            "risk_level": risk_level,
            "file_path": file_path,
            "patch_chars": len(patch),
            "reviewer": "lvector",
            "collapse_risk": float(getattr(lvector, "collapse_risk", 0.0) or 0.0),
            "risk_delta": dict(getattr(lvector, "risk_delta", {}) or {}),
        }

    def _blocked_marker(self, payload: dict[str, Any]) -> str:
        text = str(payload).lower()
        for marker in self.BLOCKED_PAYLOAD_MARKERS:
            if marker in text:
                return marker
        return ""

    def _dangerous_code_pattern(self, text: str) -> str:
        lowered = text.lower()
        for pattern in self.DANGEROUS_CODE_PATTERNS:
            if pattern in lowered:
                return pattern
        return ""

    @staticmethod
    def _first_payload_text(payload: dict[str, Any], fields: list[str]) -> str:
        for field in fields:
            value = payload.get(field)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _payload(prescription: Any) -> dict[str, Any]:
        payload = getattr(prescription, "payload", {}) or {}
        return payload if isinstance(payload, dict) else {"value": payload}

    @staticmethod
    def _treatment_type(prescription: Any) -> str:
        tx = getattr(prescription, "treatment_type", "")
        return str(getattr(tx, "value", tx) or "")
