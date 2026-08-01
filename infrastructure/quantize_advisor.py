"""
quantize_advisor.py
─────────────────────────────────────────────────────────────────────
범용 AI 모델 양자화 어드바이저.

MEDIC이 지속적으로 느린 모델을 감지하면
더 가벼운 양자화 버전으로 전환을 처방한다.

Ollama 양자화 수준:
  f16    → 원본 품질, 가장 무거움  (7B = ~14GB)
  q8_0   → 거의 동일 품질         (7B = ~7GB)
  q4_k_m → 권장 균형점            (7B = ~4GB) ← 기본 추천
  q4_0   → 빠름, 약간 품질 저하   (7B = ~3.5GB)
  q2_k   → 매우 빠름, 품질 저하   (7B = ~2.5GB)

Ollama 외 AI에도 적용 가능:
  OpenAI 호환 API → 더 작은 모델로 전환 권고
  커스텀 서버     → 설정 파일 변경 권고
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ── 양자화 수준 정의 ─────────────────────────────────────────────────

@dataclass
class QuantizeLevel:
    """양자화 수준 정보."""
    name        : str    # q4_k_m, q8_0 등
    size_ratio  : float  # 원본 대비 크기 비율 (0.5 = 절반)
    speed_gain  : float  # 속도 향상 비율 (2.0 = 2배)
    quality_loss: float  # 품질 손실 (0.0~1.0, 낮을수록 좋음)
    recommended : bool   # 권장 여부


QUANTIZE_LEVELS = [
    QuantizeLevel("f16",    1.0,  1.0, 0.00, False),   # 원본
    QuantizeLevel("q8_0",   0.5,  1.5, 0.01, False),   # 거의 무손실
    QuantizeLevel("q4_k_m", 0.28, 2.5, 0.05, True),    # 권장 ★
    QuantizeLevel("q4_0",   0.25, 3.0, 0.08, False),   # 빠름
    QuantizeLevel("q2_k",   0.18, 4.0, 0.15, False),   # 매우 빠름
]

# 현재 메모리 여유에 따른 추천 수준
def recommend_quantize(
    current_latency_ms : float,
    available_memory_gb: float,
    model_size_gb      : float,
    quality_priority   : float = 0.5,  # 0=속도, 1=품질
) -> QuantizeLevel:
    """
    현재 상황에 맞는 최적 양자화 수준을 추천한다.

    Args:
        current_latency_ms: 현재 응답 지연 (ms)
        available_memory_gb: 여유 메모리 (GB)
        model_size_gb: 현재 모델 크기 (GB)
        quality_priority: 품질 우선도 (0=속도, 1=품질)
    """
    candidates = []
    for level in QUANTIZE_LEVELS:
        required_mem = model_size_gb * level.size_ratio
        # 메모리 여유가 있고 품질 손실이 허용 범위 내인 것
        if required_mem <= available_memory_gb * 0.8:
            # 점수: 속도 향상 × (1-품질손실×품질우선도)
            score = level.speed_gain * (1 - level.quality_loss * quality_priority)
            candidates.append((score, level))

    if not candidates:
        return QUANTIZE_LEVELS[2]  # 기본값 q4_k_m

    return max(candidates, key=lambda x: x[0])[1]


class QuantizeAdvisor:
    """
    모델 양자화 어드바이저.

    MEDIC이 느린 모델을 감지하면 이 클래스가
    최적 양자화 수준을 계산하고 처방 payload에 담는다.

    Ollama 전용이 아님 — 어떤 AI 서버든 사용 가능.
    Ollama가 아닌 경우 "권고 메시지"로 반환.
    """

    # 이 정도 이상 느리면 양자화 고려
    LATENCY_THRESHOLD_MS = 10000  # 10초

    def __init__(
        self,
        available_memory_gb: float = 8.0,
        quality_priority   : float = 0.5,
    ) -> None:
        self._mem_gb   = available_memory_gb
        self._quality  = quality_priority

    def advise(
        self,
        model_name     : str,
        model_size_gb  : float,
        latency_ms     : float,
        is_ollama      : bool = True,
    ) -> dict:
        """
        양자화 권고를 반환한다.

        Returns:
            {
                "should_quantize": bool,
                "current_level"  : str,
                "recommended"    : str,
                "expected_speedup": float,
                "ollama_command" : str,   # Ollama인 경우
                "message"        : str,
            }
        """
        # 현재 양자화 수준 파악
        current_level = self._detect_current_level(model_name)

        # 양자화 필요 여부 판단
        if latency_ms < self.LATENCY_THRESHOLD_MS:
            return {
                "should_quantize": False,
                "message"        : f"지연 {latency_ms:.0f}ms — 양자화 불필요",
            }

        # 최적 수준 계산
        recommended = recommend_quantize(
            latency_ms, self._mem_gb, model_size_gb, self._quality
        )

        # 이미 충분히 양자화됐으면 다른 방법 권고
        if (current_level and
                current_level.size_ratio <= recommended.size_ratio):
            return {
                "should_quantize": False,
                "message"        : (
                    f"이미 {current_level.name} 적용됨. "
                    f"더 작은 모델로 교체 권장."
                ),
                "alternative"    : "weight_rollback",
            }

        # Ollama 명령어 생성
        base_name = model_name.split(":")[0]
        new_model  = f"{base_name}:{recommended.name}"
        ollama_cmd = f"ollama pull {new_model}" if is_ollama else ""

        expected_speedup = recommended.speed_gain / (
            current_level.speed_gain if current_level else 1.0
        )

        return {
            "should_quantize" : True,
            "current_level"   : current_level.name if current_level else "unknown",
            "recommended"     : recommended.name,
            "expected_speedup": round(expected_speedup, 1),
            "quality_loss"    : f"{recommended.quality_loss:.0%}",
            "size_reduction"  : f"{(1-recommended.size_ratio):.0%}",
            "ollama_command"  : ollama_cmd,
            "new_model_name"  : new_model if is_ollama else "",
            "message"         : (
                f"{model_name} ({latency_ms:.0f}ms) → "
                f"{new_model} (예상 {expected_speedup:.1f}배 빠름, "
                f"품질 손실 {recommended.quality_loss:.0%})"
            ),
        }

    def _detect_current_level(
        self, model_name: str
    ) -> Optional[QuantizeLevel]:
        """모델 이름에서 현재 양자화 수준을 파악한다."""
        name_lower = model_name.lower()
        for level in QUANTIZE_LEVELS:
            if level.name in name_lower:
                return level
        # 태그 없으면 f16 또는 기본값
        if ":" not in model_name or model_name.endswith(":latest"):
            return QUANTIZE_LEVELS[0]  # f16 가정
        return None
