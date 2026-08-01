"""
local_slm.py
─────────────────────────────────────────────────────────────────────
로컬 SLM 래퍼.

DeCODE의 SLMRunner를 MEDIC 처방 엔진으로 연결한다.
외부 API 없음. GGUF 모델을 로컬에서 직접 실행.

모델이 없는 환경에서는 RuleBasedFallback 이 대신 동작한다.
(L-벡터 분석 결과만으로 처방을 결정하는 순수 규칙 엔진)
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── 규칙 기반 폴백 (SLM 없을 때) ────────────────────────────────────

class RuleBasedFallback:
    """
    SLM 없이 L-벡터만으로 처방을 결정하는 규칙 엔진.

    언어 편향이 없다. 수학적 임계값만 사용한다.
    SLM보다 창의적이지 않지만, 편향도 없다.
    """

    # L-벡터 차원별 → 권장 치료 매핑
    _DIM_TO_TREATMENT = {
        "RECURRENCE" : "refactor_recursion",   # 재귀 → 반복문 전환
        "COMPOSITION": "split_module",          # 의존성 과다 → 파일 분리
        "DYNAMICS"   : "isolate_state",         # 전역 상태 → 함수형 전환
        "EMERGENCE"  : "add_enum_guard",        # 예측불가 분기 → Enum 명시
        "INFORMATION": "extract_function",      # 분기 복잡도 → 함수 분리
    }

    def generate_prescription(
        self,
        diagnosis_summary: str,
        l_vector         : dict,
        patient_type     : str,
        risk_dims        : list[str],
        severity         : str = "LOW",
    ) -> dict:
        """L-벡터 분석 결과 + severity 로 처방을 결정한다."""

        # L-벡터 이상 없지만 severity가 높으면 severity 기반으로 처방
        if not risk_dims:
            if severity == "CRITICAL":
                return {"treatment_type": "restart", "payload": {},
                        "risk_level": "HIGH",
                        "reasoning": f"CRITICAL severity — 즉시 재시작"}
            if severity == "HIGH":
                if "메모리" in diagnosis_summary or "memory" in diagnosis_summary.lower():
                    return {
                        "treatment_type": "config_change",
                        "payload"       : {"ollama_unload": True},
                        "risk_level"    : "HIGH",
                        "reasoning"     : "메모리 압박 — Ollama 모델 언로드 권고",
                    }
                tx = "prompt_patch" if patient_type == "ai_model" else "restart"
                payload = {}
                if tx == "prompt_patch":
                    payload = {
                        "system_prompt": (
                            "You are a concise assistant. "
                            "Always answer in 1-3 sentences maximum. "
                            "Be direct and brief. No unnecessary explanations."
                        )
                    }
                return {"treatment_type": tx, "payload": payload,
                        "risk_level": "MEDIUM",
                        "reasoning": f"HIGH severity — {tx}"}
            return {
                "treatment_type": "monitor",
                "payload"       : {},
                "risk_level"    : "LOW",
                "reasoning"     : "L-벡터 이상 없음 — 모니터링 유지",
            }

        # 위험 차원 중 가장 높은 것
        worst_dim = max(risk_dims, key=lambda d: l_vector.get(d, 0))
        treatment = self._DIM_TO_TREATMENT.get(worst_dim, "manual_intervention")

        # AI 모델은 다른 치료 경로
        if patient_type == "ai_model":
            if worst_dim in ("EMERGENCE", "DYNAMICS"):
                treatment = "prompt_patch"
            elif worst_dim in ("RECURRENCE", "COMPOSITION"):
                treatment = "weight_rollback"

        # prompt_patch payload 자동 생성
        if treatment == "prompt_patch" and patient_type == "ai_model":
            extra_payload = {
                "system_prompt": (
                    "You are a concise assistant. "
                    "Answer in 1-3 sentences maximum. "
                    "Be direct. No filler words."
                )
            }
        else:
            extra_payload = {}

        risk_level = "LOW"
        if len(risk_dims) >= 3:
            risk_level = "HIGH"
        elif len(risk_dims) >= 2:
            risk_level = "MEDIUM"

        dims_str = ", ".join(f"{d}={l_vector.get(d,0):.2f}" for d in risk_dims)

        final_payload = extra_payload.copy()
        final_payload.update({
            "target_dims" : risk_dims,
            "l_vector"    : {d: l_vector.get(d, 0) for d in risk_dims},
        })
        return {
            "treatment_type": treatment,
            "payload"       : final_payload,
            "risk_level"    : risk_level,
            "reasoning"     : (
                f"L-벡터 규칙 기반 처방 | "
                f"위험 차원: [{dims_str}] | "
                f"주요 원인: {worst_dim}"
            ),
        }


# ── MEDIC용 SLM 래퍼 ────────────────────────────────────────────────

class LocalSLM:
    """
    DeCODE의 SLMRunner를 MEDIC 처방 엔진으로 감싼 래퍼.

    사용 예시:
        slm = LocalSLM(model_path="/models/mistral-7b.gguf")
        prescription = slm.generate_prescription(diagnosis, vitals, l_vector)

    모델 없는 환경:
        slm = LocalSLM(model_path=None)  # RuleBasedFallback 자동 사용
    """

    # MEDIC 처방 전용 시스템 프롬프트
    _SYSTEM_PROMPT = """\
당신은 소프트웨어 시스템과 AI 에이전트를 치료하는 독립적인 의사(MEDIC)입니다.
환자의 증상과 L-벡터 구조 분석 결과를 보고 최소한의 처방을 내립니다.

규칙:
1. 처방은 반드시 JSON 형식으로만 출력합니다.
2. 가능한 가장 안전하고 최소한의 치료를 선택합니다.
3. 확신이 없으면 MANUAL_INTERVENTION을 선택합니다.
4. L-벡터의 위험 차원을 반드시 근거로 사용합니다.

출력 형식 (JSON만, 다른 텍스트 없음):
{
  "treatment_type": "patch_code|restart|rollback|prompt_patch|weight_rollback|quarantine|manual_intervention",
  "payload": {},
  "risk_level": "LOW|MEDIUM|HIGH",
  "reasoning": "한 줄 근거"
}"""

    def __init__(
        self,
        model_path      : Optional[str] = None,
        decode_root     : Optional[str] = None,
        n_gpu_layers    : int   = -1,
        n_ctx           : int   = 4096,
        timeout_sec     : float = 120.0,
    ) -> None:
        self._fallback = RuleBasedFallback()
        self._slm      = None

        # DeCODE SLMRunner 로드 시도
        if model_path and Path(model_path).exists():
            self._slm = self._load_slm(
                model_path, decode_root, n_gpu_layers, n_ctx, timeout_sec
            )

        if self._slm:
            logger.info(f"[LocalSLM] SLM 로드 완료 | model={model_path}")
        else:
            logger.info("[LocalSLM] SLM 없음 — RuleBasedFallback 사용")

    @property
    def is_slm_available(self) -> bool:
        return self._slm is not None

    def generate_prescription(
        self,
        diagnosis_summary : str,
        vitals_summary    : str,
        l_vector          : dict,
        risk_dims         : list[str],
        patient_type      : str = "python_service",
        temperature       : float = 0.2,
        severity          : str = "LOW",
    ) -> dict:
        """
        진단 결과로 처방을 생성한다.
        SLM이 있으면 SLM, 없으면 규칙 기반.
        """
        # SLM이 없으면 바로 규칙 기반
        if not self._slm:
            return self._fallback.generate_prescription(
                diagnosis_summary, l_vector, patient_type, risk_dims, severity
            )

        # SLM 프롬프트 구성
        dims_str = "\n".join(
            f"  {d}: {l_vector.get(d, 0):.3f} {'⚠ 위험' if d in risk_dims else ''}"
            for d in l_vector
        )
        user_msg = (
            f"## 환자 유형\n{patient_type}\n\n"
            f"## 진단 요약\n{diagnosis_summary}\n\n"
            f"## 생체 지표\n{vitals_summary}\n\n"
            f"## L-벡터 구조 분석\n{dims_str}\n\n"
            f"## 위험 차원\n{', '.join(risk_dims) if risk_dims else '없음'}\n\n"
            f"처방을 JSON으로 출력하세요:"
        )

        try:
            raw = self._slm.generate(
                system_prompt = self._SYSTEM_PROMPT,
                user_message  = user_msg,
                max_tokens    = 300,
                temperature   = temperature,
            )
            return self._parse_json(raw) or self._fallback.generate_prescription(
                diagnosis_summary, l_vector, patient_type, risk_dims
            )
        except Exception as exc:
            logger.warning(f"[LocalSLM] SLM 처방 생성 실패, 규칙 기반으로 폴백: {exc}")
            return self._fallback.generate_prescription(
                diagnosis_summary, l_vector, patient_type, risk_dims, severity
            )

    def review_patch(
        self,
        case_summary  : str,
        proposed_patch: str,
        l_vector      : dict,
        risk_dims     : list[str],
        temperature   : float = 0.15,  # 검토는 더 보수적으로
    ) -> dict:
        """
        제안된 패치를 SLM이 독립적으로 검토한다.
        (second_opinion의 SLM 패널 역할)
        """
        if not self._slm:
            # SLM 없으면 L-벡터 기반으로 간단히 판단
            return self._rule_based_review(proposed_patch, l_vector, risk_dims)

        review_prompt = """\
당신은 패치 안전성을 검토하는 독립 검증자입니다.
케이스 요약과 제안된 패치, L-벡터 구조 분석을 보고 판단합니다.

JSON만 출력:
{"verdict": "APPROVE|REJECT|ESCALATE", "confidence": 0.0-1.0,
 "reasoning": "한 줄", "concerns": []}"""

        dims_str = ", ".join(f"{d}={l_vector.get(d,0):.2f}" for d in risk_dims)
        user_msg = (
            f"## 케이스\n{case_summary}\n\n"
            f"## 제안 패치\n```\n{proposed_patch[:1000]}\n```\n\n"
            f"## L-벡터 위험 차원\n{dims_str or '없음'}\n\n"
            f"이 패치가 안전한지 검토하세요:"
        )

        try:
            raw = self._slm.generate(
                system_prompt = review_prompt,
                user_message  = user_msg,
                max_tokens    = 200,
                temperature   = temperature,
            )
            return self._parse_json(raw) or {"verdict": "ESCALATE",
                                              "confidence": 0.3,
                                              "reasoning": "SLM 파싱 실패",
                                              "concerns": []}
        except Exception as exc:
            logger.warning(f"[LocalSLM] 패치 검토 실패: {exc}")
            return self._rule_based_review(proposed_patch, l_vector, risk_dims)

    # ── 내부 ──────────────────────────────────────────────────

    def _load_slm(
        self,
        model_path  : str,
        decode_root : Optional[str],
        n_gpu_layers: int,
        n_ctx       : int,
        timeout_sec : float,
    ):
        """DeCODE SLMRunner 로드."""
        if decode_root:
            sys.path.insert(0, str(Path(decode_root) / "decode"))

        try:
            from inference.slm_runner import SLMRunner
            return SLMRunner(
                model_path   = model_path,
                n_gpu_layers = n_gpu_layers,
                n_ctx        = n_ctx,
                verbose      = False,
                timeout_sec  = timeout_sec,
            )
        except ImportError:
            logger.warning("[LocalSLM] DeCODE SLMRunner 없음 — 폴백 사용")
            return None
        except Exception as exc:
            logger.warning(f"[LocalSLM] SLM 로드 실패: {exc}")
            return None

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        """SLM 출력에서 JSON을 추출한다."""
        try:
            raw = raw.strip()
            # 코드 블록 제거
            raw = re.sub(r"```json?\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw)
            match = re.search(r"\{[\s\S]+\}", raw)
            if match:
                return json.loads(match.group())
        except Exception:
            pass
        return None

    @staticmethod
    def _rule_based_review(
        patch    : str,
        l_vector : dict,
        risk_dims: list[str],
    ) -> dict:
        """SLM 없을 때 패치를 규칙으로 간단히 검토한다."""
        concerns = []

        # 패치 크기
        lines = patch.count("\n")
        added = patch.count("\n+")
        if lines > 100 or added > 50:
            concerns.append(f"패치 규모 과다: {lines}줄, +{added}줄")

        # 위험 차원이 많으면 보수적
        if len(risk_dims) >= 3:
            concerns.append(f"L-벡터 위험 차원 {len(risk_dims)}개 — 고위험")

        # 위험 패턴
        danger = ["os.system", "subprocess", "eval(", "exec(", "import os",
                  "__builtins__", "open(", "shutil"]
        for d in danger:
            if d in patch:
                concerns.append(f"위험 패턴 감지: {d}")

        if concerns:
            return {
                "verdict"   : "REJECT",
                "confidence": 0.7,
                "reasoning" : "규칙 기반 검토 — 위험 요소 감지",
                "concerns"  : concerns,
            }

        return {
            "verdict"   : "APPROVE",
            "confidence": 0.6,
            "reasoning" : "규칙 기반 검토 — 명시적 위험 없음",
            "concerns"  : [],
        }
