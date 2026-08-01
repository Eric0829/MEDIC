"""
model_profiler.py
─────────────────────────────────────────────────────────────────────
모델 특성 학습기.

각 모델의 "이 환경에서의 정상 속도(baseline)"를 기억한다.

문제:
  지금 MEDIC은 절대값 기준으로 판단:
    latency > 5000ms → HIGH
  
  근데 3B 모델은 4800ms가 정상이고
  10B 모델은 30000ms도 정상일 수 있다.

해결:
  각 모델의 baseline을 학습해서 
  "지금이 평소보다 얼마나 느린가"로 판단:
    latency > baseline × 2.0 → HIGH
    latency > baseline × 1.5 → MEDIUM

  처음엔 baseline이 없으니 절대값 기준 사용,
  5번 측정 후부터 baseline 기반으로 전환.

양자화 자동 적용:
  escalation이 weight_rollback으로 넘어갈 때
  더 작은 모델이 없으면 양자화 버전을 자동으로 pull + 전환.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── 모델 baseline 프로필 ─────────────────────────────────────────────

@dataclass
class ModelProfile:
    """모델 1개의 성능 기준선."""
    model_id          : str
    baseline_latency  : float = 0.0   # 정상 응답시간 (ms)
    baseline_tps      : float = 0.0   # 정상 토큰/초
    sample_count      : int   = 0     # 측정 횟수
    last_updated      : float = field(default_factory=time.time)
    is_calibrated     : bool  = False  # 5회 이상 측정됐는가

    # 임계값 배수
    HIGH_RATIO   : float = 2.0   # baseline × 2배 초과 → HIGH
    MEDIUM_RATIO : float = 1.5   # baseline × 1.5배 초과 → MEDIUM
    CALIBRATE_N  : int   = 5     # 이 횟수 이상 측정 후 baseline 사용

    def update(self, latency_ms: float, tps: float = 0.0) -> None:
        """새 측정값으로 baseline 갱신 (이동 평균)."""
        self.sample_count += 1
        self.last_updated = time.time()

        if self.baseline_latency == 0:
            self.baseline_latency = latency_ms
            self.baseline_tps     = tps
        else:
            alpha = 0.3  # 새 값 반영 비율
            self.baseline_latency = (
                alpha * latency_ms + (1 - alpha) * self.baseline_latency
            )
            if tps > 0:
                self.baseline_tps = (
                    alpha * tps + (1 - alpha) * self.baseline_tps
                )

        if self.sample_count >= self.CALIBRATE_N:
            self.is_calibrated = True

    def severity(self, latency_ms: float) -> str:
        """
        현재 지연이 baseline 대비 얼마나 심각한지 반환.
        calibrated 전이면 절대값 기준 사용.
        """
        if not self.is_calibrated or self.baseline_latency == 0:
            # 절대값 기준 (기존 방식)
            if latency_ms > 5000:
                return "HIGH"
            elif latency_ms > 3000:
                return "MEDIUM"
            return "LOW"

        ratio = latency_ms / self.baseline_latency
        if ratio >= self.HIGH_RATIO:
            return "HIGH"
        elif ratio >= self.MEDIUM_RATIO:
            return "MEDIUM"
        return "LOW"

    def to_dict(self) -> dict:
        return {
            "model_id"        : self.model_id,
            "baseline_latency": self.baseline_latency,
            "baseline_tps"    : self.baseline_tps,
            "sample_count"    : self.sample_count,
            "last_updated"    : self.last_updated,
            "is_calibrated"   : self.is_calibrated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelProfile":
        obj = cls(model_id=d["model_id"])
        obj.baseline_latency = d.get("baseline_latency", 0.0)
        obj.baseline_tps     = d.get("baseline_tps", 0.0)
        obj.sample_count     = d.get("sample_count", 0)
        obj.last_updated     = d.get("last_updated", time.time())
        obj.is_calibrated    = d.get("is_calibrated", False)
        return obj

    def render(self) -> str:
        status = "calibrated" if self.is_calibrated else f"learning ({self.sample_count}/{self.CALIBRATE_N})"
        return (
            f"  {self.model_id:<40} "
            f"baseline={self.baseline_latency:.0f}ms "
            f"tps={self.baseline_tps:.1f} "
            f"{status}"
        )


# ── 모델 프로파일러 ──────────────────────────────────────────────────

class ModelProfiler:
    """
    모든 모델의 baseline을 관리한다.

    사용:
        profiler = ModelProfiler(persist_path="data/profiles.json")

        # 측정값 기록
        profiler.update("qwen2.5:3b", latency_ms=2500, tps=0.4)

        # 현재 상태 심각도 판단
        sev = profiler.severity("qwen2.5:3b", latency_ms=8000)
        # → "HIGH" (baseline 2500ms × 2 = 5000ms 초과)

        # baseline 대비 배수
        ratio = profiler.ratio("qwen2.5:3b", latency_ms=8000)
        # → 3.2
    """

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._profiles   : dict[str, ModelProfile] = {}
        self._persist    : Optional[Path] = (
            Path(persist_path) if persist_path else None
        )
        if self._persist and self._persist.exists():
            self._load()
            calibrated = sum(
                1 for p in self._profiles.values() if p.is_calibrated
            )
            logger.info(
                f"[Profiler] 로드 완료 | "
                f"{len(self._profiles)}개 모델 "
                f"({calibrated}개 학습 완료)"
            )

    def update(
        self,
        model_id  : str,
        latency_ms: float,
        tps       : float = 0.0,
    ) -> ModelProfile:
        """측정값을 기록하고 baseline을 갱신한다."""
        if model_id not in self._profiles:
            self._profiles[model_id] = ModelProfile(model_id=model_id)

        profile = self._profiles[model_id]
        was_calibrated = profile.is_calibrated
        profile.update(latency_ms, tps)

        if profile.is_calibrated and not was_calibrated:
            logger.info(
                f"[Profiler] ✅ baseline 학습 완료 | "
                f"{model_id} "
                f"baseline={profile.baseline_latency:.0f}ms "
                f"tps={profile.baseline_tps:.1f}"
            )

        if self._persist:
            self._save()

        return profile

    def severity(self, model_id: str, latency_ms: float) -> str:
        """모델의 현재 지연 심각도를 반환한다."""
        profile = self._profiles.get(model_id)
        if not profile:
            # 처음 보는 모델 — 절대값 기준
            if latency_ms > 5000: return "HIGH"
            if latency_ms > 3000: return "MEDIUM"
            return "LOW"
        return profile.severity(latency_ms)

    def ratio(self, model_id: str, latency_ms: float) -> float:
        """baseline 대비 현재 지연 배수를 반환한다."""
        profile = self._profiles.get(model_id)
        if not profile or profile.baseline_latency == 0:
            return 1.0
        return round(latency_ms / profile.baseline_latency, 2)

    def is_calibrated(self, model_id: str) -> bool:
        p = self._profiles.get(model_id)
        return p.is_calibrated if p else False

    def get_profile(self, model_id: str) -> Optional[ModelProfile]:
        return self._profiles.get(model_id)

    def all_profiles(self) -> list[ModelProfile]:
        return list(self._profiles.values())

    def render(self) -> str:
        if not self._profiles:
            return "  (no learned model profiles)"
        lines = ["\n  +-- Model baseline profiles ----------------------"]
        for p in sorted(
            self._profiles.values(),
            key=lambda x: x.baseline_latency
        ):
            lines.append(p.render())
        lines.append("  +------------------------------------------------")
        return "\n".join(lines)

    def _save(self) -> None:
        try:
            data = {k: v.to_dict() for k, v in self._profiles.items()}
            self._persist.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[Profiler] 저장 실패: {exc}")

    def _load(self) -> None:
        try:
            data = json.loads(
                self._persist.read_text(encoding="utf-8")
            )
            for k, v in data.items():
                self._profiles[k] = ModelProfile.from_dict(v)
        except Exception as exc:
            logger.warning(f"[Profiler] 로드 실패: {exc}")
