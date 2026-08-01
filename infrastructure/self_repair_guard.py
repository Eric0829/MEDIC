"""
self_repair_guard.py
─────────────────────────────────────────────────────────────────────
패치 안전 게이트.

자동 수정 시스템의 가장 큰 위험:
  "잘못된 패치 → 시스템 악화"

SelfRepairGuard는 모든 치료 처방을 적용 전에 검증한다.

검증 단계:
  1. dry-run    — 실제 적용 없이 처방 내용 검사
  2. risk_score — 위험도 계산 (0~1)
  3. rollback   — 실패 시 이전 상태로 복구

위험도 기준:
  0.0 ~ 0.3  → LOW    → 자동 적용
  0.3 ~ 0.7  → MEDIUM → 적용 + 즉시 검증
  0.7 ~ 1.0  → HIGH   → 사람 승인 필요
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class GuardVerdict:
    """안전 게이트 판정 결과."""
    allowed      : bool
    risk_score   : float        # 0.0~1.0
    risk_level   : str          # LOW / MEDIUM / HIGH
    reasons      : list[str]    = field(default_factory=list)
    dry_run_ok   : bool         = True
    requires_approval: bool     = False

    def summary(self) -> str:
        icon = "✅" if self.allowed else "🚫"
        return (
            f"{icon} risk={self.risk_score:.2f} [{self.risk_level}] "
            + (f"— {'; '.join(self.reasons)}" if self.reasons else "OK")
        )


class SelfRepairGuard:
    """
    모든 치료 처방을 적용 전에 검증하는 안전 게이트.

    MedicLocal.treat()에서 apply_treatment() 직전에 호출된다.

    사용:
        guard = SelfRepairGuard(
            auto_approve_below = 0.7,  # 0.7 미만은 자동 승인
            require_human_above= 0.9,  # 0.9 초과는 사람 승인 필요
        )

        verdict = await guard.check(patient, prescription)
        if not verdict.allowed:
            # 차단
        else:
            # 적용
    """

    # 치료 유형별 기본 위험도
    BASE_RISK = {
        "monitor"         : 0.0,
        "prompt_patch"    : 0.1,
        "config_change"   : 0.2,
        "weight_rollback" : 0.3,
        "scale_down"      : 0.4,
        "restart"         : 0.5,
        "rollback"        : 0.5,
        "patch_code"      : 0.7,
        "quarantine"      : 0.7,
        "fine_tune_trigger": 0.8,
        "manual_intervention": 0.9,
    }

    def __init__(
        self,
        auto_approve_below : float = 0.7,
        require_human_above: float = 0.9,
        max_daily_high_risk: int   = 3,    # 하루 HIGH 위험 처방 최대 횟수
    ) -> None:
        self._auto_below    = auto_approve_below
        self._human_above   = require_human_above
        self._max_high      = max_daily_high_risk
        self._high_risk_log : list[float] = []  # HIGH 위험 처방 타임스탬프

    async def check(
        self,
        patient     : Any,   # BasePatient
        prescription: Any,   # Prescription
    ) -> GuardVerdict:
        """
        처방을 검증하고 판정을 반환한다.

        검증 항목:
          1. 치료 유형 기본 위험도
          2. payload 내용 검사
          3. 반복 처방 위험도 가중
          4. 시스템 상태 기반 위험도
          5. HIGH 위험 처방 일일 한도
        """
        reasons    = []
        risk_score = self.BASE_RISK.get(
            prescription.treatment_type.value, 0.5
        )

        # 1. payload 내용 검사
        payload = getattr(prescription, "payload", {}) or {}
        payload_risk = self._check_payload(
            prescription.treatment_type.value, payload
        )
        if payload_risk["risk"] > 0:
            risk_score = min(1.0, risk_score + payload_risk["risk"])
            reasons.extend(payload_risk["reasons"])

        # 2. 신뢰도 기반 위험도 조정
        confidence = getattr(prescription, "confidence", 0.5)
        if confidence < 0.4:
            risk_score = min(1.0, risk_score + 0.2)
            reasons.append(f"처방 신뢰도 낮음 ({confidence:.2f})")

        # 3. 처방 발행자 확인
        issued_by = getattr(prescription, "issued_by", "")
        if "escalation" in issued_by:
            risk_score = min(1.0, risk_score + 0.1)
            reasons.append("에스컬레이션 처방")

        # 4. HIGH 위험 일일 한도 확인
        now = time.time()
        # 24시간 이내 HIGH 위험 처방 카운트
        self._high_risk_log = [
            t for t in self._high_risk_log if now - t < 86400
        ]
        if risk_score >= 0.7 and len(self._high_risk_log) >= self._max_high:
            risk_score = min(1.0, risk_score + 0.2)
            reasons.append(
                f"일일 HIGH 위험 처방 한도 초과 ({len(self._high_risk_log)}/{self._max_high})"
            )

        # 위험 레벨 분류
        if risk_score < 0.3:
            risk_level = "LOW"
        elif risk_score < 0.7:
            risk_level = "MEDIUM"
        else:
            risk_level = "HIGH"

        # 허용 여부 결정
        allowed          = risk_score < self._human_above
        requires_approval = risk_score >= self._human_above

        # HIGH 위험 처방 기록
        if risk_score >= 0.7:
            self._high_risk_log.append(now)

        # dry-run (monitor, prompt_patch 등은 항상 통과)
        dry_run_ok = risk_score < 0.7

        verdict = GuardVerdict(
            allowed           = allowed,
            risk_score        = round(risk_score, 3),
            risk_level        = risk_level,
            reasons           = reasons,
            dry_run_ok        = dry_run_ok,
            requires_approval = requires_approval,
        )

        log_fn = logger.debug if allowed else logger.warning
        log_fn(
            f"[Guard] {prescription.treatment_type.value} | "
            f"{verdict.summary()}"
        )

        return verdict

    def _check_payload(self, tx_type: str, payload: dict) -> dict:
        """payload 내용을 검사해서 추가 위험도를 반환한다."""
        extra_risk = 0.0
        reasons    = []

        if tx_type == "prompt_patch":
            prompt = payload.get("system_prompt", "")
            if len(prompt) > 500:
                extra_risk += 0.1
                reasons.append("프롬프트 길이 과다")
            # 잠재적 위험 키워드
            danger_kw = ["ignore", "bypass", "override", "sudo", "root"]
            if any(kw in prompt.lower() for kw in danger_kw):
                extra_risk += 0.3
                reasons.append("위험 키워드 감지")

        elif tx_type == "config_change":
            if payload.get("ollama_unload"):
                extra_risk += 0.1  # 낮은 위험
            if payload.get("force_kill"):
                extra_risk += 0.4
                reasons.append("강제 종료 포함")
            decode_preflight = payload.get("decode_preflight", {}) or {}
            decode_risk = float(decode_preflight.get("risk_score", 0.0) or 0.0)
            if decode_risk > 0:
                extra_risk += min(0.25, decode_risk * 0.4)
                reasons.append(f"DeCODE 설정 변경 위험도 {decode_risk:.2f}")
            actions = list(decode_preflight.get("recommended_actions", []) or [])
            if actions:
                reasons.append("DeCODE 권장조치: " + ", ".join(actions[:2]))

        elif tx_type == "patch_code":
            patch = payload.get("diff_patch") or payload.get("patch", "")
            if "os.system" in patch or "subprocess" in patch:
                extra_risk += 0.4
                reasons.append("시스템 명령 포함")
            if "rm -rf" in patch or "del /f" in patch.lower():
                extra_risk += 0.9
                reasons.append("삭제 명령 감지")
            if payload.get("report_only"):
                extra_risk = max(0.0, extra_risk - 0.2)
                reasons.append("report-only 모드")
            elif payload.get("dry_run"):
                extra_risk = max(0.0, extra_risk - 0.1)
                reasons.append("dry-run 모드")
            if payload.get("staged", True):
                extra_risk = max(0.0, extra_risk - 0.1)
                reasons.append("staged apply")
            if payload.get("create_snapshot", True):
                extra_risk = max(0.0, extra_risk - 0.1)
                reasons.append("snapshot rollback 준비")

            decode_preflight = payload.get("decode_preflight", {}) or {}
            decode_risk = float(decode_preflight.get("risk_score", 0.0) or 0.0)
            if decode_risk > 0:
                extra_risk += min(0.35, decode_risk * 0.5)
                reasons.append(f"DeCODE 사전검토 위험도 {decode_risk:.2f}")
            patterns = list(decode_preflight.get("suspicious_patterns", []) or [])
            if patterns:
                reasons.append("DeCODE 패턴: " + ", ".join(patterns[:3]))

        elif tx_type == "rollback":
            if payload.get("snapshot_id"):
                extra_risk = max(0.0, extra_risk - 0.2)
                reasons.append("snapshot rollback")
            if not payload.get("rollback_cmd") and not payload.get("target_version"):
                extra_risk += 0.1
                reasons.append("롤백 대상 미지정")

        return {"risk": extra_risk, "reasons": reasons}

    def snapshot(self, patient: Any) -> dict:
        """
        치료 전 상태 스냅샷을 저장한다 (rollback 기준점).
        실제 rollback은 patient.apply_treatment(rollback_rx)로 처리.
        """
        return {
            "patient_id" : getattr(patient, "patient_id", ""),
            "model_name" : getattr(patient, "_model_name", ""),
            "system_prompt": getattr(patient, "_system_prompt", ""),
            "timestamp"  : time.time(),
        }

    def stats(self) -> dict:
        now = time.time()
        high_today = len([t for t in self._high_risk_log if now - t < 86400])
        return {
            "high_risk_today": high_today,
            "limit"          : self._max_high,
            "auto_below"     : self._auto_below,
            "human_above"    : self._human_above,
        }
