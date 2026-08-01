"""
ai_model_patient.py
─────────────────────────────────────────────────────────────────────
AI 모델 / 에이전트 환자 어댑터.

LLM, 추론 서버, AI 에이전트를 MEDIC 환자로 등록한다.

이 어댑터가 이 프로젝트에서 가장 중요한 이유:
  AI 는 자신의 출력 편향을 스스로 탐지하지 못한다.
  자신이 틀렸다고 생각하면 자신이 맞게끔 기준을 바꾼다.
  외부 관찰자(MEDIC)만이 이를 독립적으로 판단할 수 있다.

AI 전용 증상:
  - output_drift      : 출력 분포가 기준선에서 벗어나는 정도
  - hallucination_rate: 사실과 다른 출력 비율 (ground truth 비교)
  - confidence_drop   : 모델 자신의 confidence score 하락
  - refusal_rate      : 정상 요청을 거부하는 비율 (과잉 필터링)
  - latency_spike     : 추론 시간 급등

AI 전용 치료:
  - PROMPT_PATCH      : 시스템 프롬프트 수정
  - WEIGHT_ROLLBACK   : 이전 체크포인트로 롤백
  - FINE_TUNE_TRIGGER : 교정 데이터셋으로 파인튜닝 트리거
  - QUARANTINE        : 해당 모델로의 라우팅 차단
─────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

try:
    import httpx
    _HTTPX_OK = True
except ImportError:
    from infrastructure import httpx_mock as httpx
    _HTTPX_OK = False

from .base_patient import (
    BasePatient, PatientType, Prescription, TreatmentResult,
    TreatmentType, Vitals,
)

logger = logging.getLogger(__name__)


# ── AI 전용 증상 지표 ────────────────────────────────────────────────

@dataclass
class AISymptomProfile:
    """
    AI 모델 전용 증상 프로파일.
    
    MEDIC 의 07_second_opinion 모듈이 이 프로파일을 분석해
    편향 여부를 독립적으로 판단한다.
    """
    # 출력 품질 지표
    output_drift        : float = 0.0   # 0 = 정상, 1 = 완전 이탈
    hallucination_rate  : float = 0.0   # 0~1 (1 = 100% 환각)
    confidence_drop     : float = 0.0   # 기준선 대비 confidence 하락폭
    refusal_rate        : float = 0.0   # 정상 요청 거부 비율
    
    # 성능 지표
    avg_latency_ms      : float = 0.0
    p99_latency_ms      : float = 0.0
    tokens_per_second   : float = 0.0
    
    # 편향 탐지 지표 (MEDIC 이 가장 주목하는 부분)
    topic_avoidance     : list[str] = field(default_factory=list)  # 회피하는 주제
    style_drift         : float = 0.0   # 응답 스타일 편향도
    repetition_rate     : float = 0.0   # 동일 패턴 반복 비율
    
    # 측정 기간
    sample_window_min   : int = 60      # 마지막 N 분 기준
    sample_count        : int = 0       # 측정에 사용된 샘플 수


class AIModelPatient(BasePatient):
    """
    AI 모델을 MEDIC 환자로 등록하는 어댑터.

    사용 예시:
        patient = AIModelPatient(
            patient_id       = "gpt4-production-router",
            inference_url    = "http://inference-server:8000",
            ground_truth_fn  = my_eval_function,   # 환각 탐지용
            baseline_profile = saved_baseline,
        )
        await medic.register(patient)
    """

    # 출력 히스토리 버퍼 (편향 추적용)
    _HISTORY_SIZE = 200

    def __init__(
        self,
        patient_id       : str,
        inference_url    : str,
        model_name       : str = "",
        ground_truth_fn  : Optional[Callable] = None,  # (prompt, output) → bool
        baseline_profile : Optional[AISymptomProfile] = None,
        eval_prompts     : list[str] = None,  # 정기 건강검진용 프롬프트셋
        metadata         : dict[str, Any] = None,
    ) -> None:
        self._patient_id      = patient_id
        self._inference_url   = inference_url.rstrip("/")
        self._model_name      = model_name
        self._ground_truth_fn = ground_truth_fn
        self._baseline        = baseline_profile or AISymptomProfile()
        self._eval_prompts    = eval_prompts or self._default_eval_prompts()
        self._meta            = metadata or {}

        # 출력 히스토리 (편향 추적)
        self._output_history  : deque = deque(maxlen=self._HISTORY_SIZE)
        self._latency_history : deque = deque(maxlen=self._HISTORY_SIZE)
        self._refusal_history : deque = deque(maxlen=self._HISTORY_SIZE)

        # 현재 시스템 프롬프트 (PROMPT_PATCH 치료를 위해 추적)
        self._current_system_prompt : str = ""

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.AI_MODEL

    # ── 증상 수집 ──────────────────────────────────────────────────

    async def collect_vitals(self) -> Vitals:
        """
        AI 모델의 현재 상태를 수집한다.
        
        핵심: 모델이 자신의 상태를 직접 평가하지 않는다.
        MEDIC 이 외부에서 eval_prompts 로 상태를 측정한다.
        """
        symptoms = []
        symptom_profile = await self._measure_symptoms()

        # 기준선 대비 이상 탐지
        if symptom_profile.hallucination_rate > self._baseline.hallucination_rate + 0.1:
            symptoms.append(
                f"hallucination_spike:"
                f"current={symptom_profile.hallucination_rate:.2f}"
                f",baseline={self._baseline.hallucination_rate:.2f}"
            )

        if symptom_profile.output_drift > 0.3:
            symptoms.append(f"output_drift:{symptom_profile.output_drift:.2f}")

        if symptom_profile.refusal_rate > self._baseline.refusal_rate + 0.15:
            symptoms.append(
                f"over_refusal:"
                f"current={symptom_profile.refusal_rate:.2f}"
            )

        if symptom_profile.p99_latency_ms > self._baseline.p99_latency_ms * 2:
            symptoms.append(
                f"latency_spike:p99={symptom_profile.p99_latency_ms:.0f}ms"
            )

        if symptom_profile.topic_avoidance:
            symptoms.append(
                f"topic_avoidance:{','.join(symptom_profile.topic_avoidance[:3])}"
            )

        # 서비스 생존 확인
        is_alive = await self.report_health()
        if not is_alive:
            symptoms.append("inference_server_unreachable")

        return Vitals(
            patient_id     = self._patient_id,
            patient_type   = self.patient_type,
            is_alive       = is_alive,
            cpu_percent    = 0.0,  # GPU 사용률은 custom_metrics 에
            memory_percent = 0.0,
            error_rate     = symptom_profile.hallucination_rate * 100,
            latency_p99_ms = symptom_profile.p99_latency_ms,
            symptoms       = symptoms,
            custom_metrics = {
                "model_name"        : self._model_name,
                "inference_url"     : self._inference_url,
                "symptom_profile"   : {
                    "output_drift"      : symptom_profile.output_drift,
                    "hallucination_rate": symptom_profile.hallucination_rate,
                    "refusal_rate"      : symptom_profile.refusal_rate,
                    "confidence_drop"   : symptom_profile.confidence_drop,
                    "style_drift"       : symptom_profile.style_drift,
                    "sample_count"      : symptom_profile.sample_count,
                },
                "system_prompt_hash": hash(self._current_system_prompt),
            },
        )

    async def report_health(self) -> bool:
        """추론 서버가 응답하는지 확인한다."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._inference_url}/health")
                return resp.status_code < 500
        except Exception:
            return False

    # ── 치료 적용 ──────────────────────────────────────────────────

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        """MEDIC 의 AI 전용 처방을 실행한다."""
        before = await self.collect_vitals()

        try:
            if prescription.treatment_type == TreatmentType.PROMPT_PATCH:
                success, msg = await self._apply_prompt_patch(prescription.payload)

            elif prescription.treatment_type == TreatmentType.WEIGHT_ROLLBACK:
                success, msg = await self._rollback_weights(prescription.payload)

            elif prescription.treatment_type == TreatmentType.FINE_TUNE_TRIGGER:
                success, msg = await self._trigger_fine_tune(prescription.payload)

            elif prescription.treatment_type == TreatmentType.QUARANTINE:
                success, msg = await self._quarantine_model(prescription.payload)

            elif prescription.treatment_type == TreatmentType.RESTART:
                success, msg = await self._restart_inference_server(prescription.payload)

            else:
                return TreatmentResult(
                    prescription_id = prescription.prescription_id,
                    patient_id      = self._patient_id,
                    success         = False,
                    message         = f"AI 모델에 지원하지 않는 치료: {prescription.treatment_type}",
                    before_vitals   = before,
                )

        except Exception as exc:
            logger.error(f"[{self._patient_id}] AI 치료 실행 오류: {exc}")
            success, msg = False, str(exc)

        # 치료 후 히스토리 초기화 (새 기준선으로 재측정)
        self._output_history.clear()
        self._latency_history.clear()
        self._refusal_history.clear()

        after = await self.collect_vitals()

        return TreatmentResult(
            prescription_id = prescription.prescription_id,
            patient_id      = self._patient_id,
            success         = success,
            message         = msg,
            before_vitals   = before,
            after_vitals    = after,
        )

    def get_treatment_blacklist(self) -> list[TreatmentType]:
        """AI 모델에 코드 패치는 적용하지 않는다."""
        return [TreatmentType.PATCH_CODE]

    # ── 내부 증상 측정 ─────────────────────────────────────────────

    async def _measure_symptoms(self) -> AISymptomProfile:
        """
        eval_prompts 로 모델을 실제로 찔러보고 증상을 측정한다.
        
        이것이 핵심: 모델이 자기 상태를 보고하는 것이 아니라
        MEDIC 이 외부에서 독립적으로 측정한다.
        """
        latencies     = []
        refusals      = 0
        hallucinations= 0
        outputs       = []

        for prompt in self._eval_prompts[:10]:  # 최대 10개만 측정
            try:
                import time
                t0 = time.monotonic()
                output, refused = await self._call_inference(prompt)
                latency = (time.monotonic() - t0) * 1000

                latencies.append(latency)
                self._latency_history.append(latency)

                if refused:
                    refusals += 1
                    self._refusal_history.append(True)
                else:
                    self._refusal_history.append(False)
                    outputs.append(output)

                    # ground_truth 검증 (있는 경우)
                    if self._ground_truth_fn:
                        try:
                            is_correct = self._ground_truth_fn(prompt, output)
                            if not is_correct:
                                hallucinations += 1
                        except Exception:
                            pass

                    self._output_history.append(output)

            except Exception as exc:
                logger.debug(f"[{self._patient_id}] eval 실패: {exc}")

        n = len(self._eval_prompts[:10])
        if n == 0:
            return AISymptomProfile()

        p99 = sorted(latencies)[int(len(latencies) * 0.99) - 1] if latencies else 0.0
        avg = statistics.mean(latencies) if latencies else 0.0

        return AISymptomProfile(
            output_drift      = self._compute_output_drift(outputs),
            hallucination_rate= hallucinations / max(len(outputs), 1),
            confidence_drop   = 0.0,  # 모델 logprob 접근 가능 시 구현
            refusal_rate      = refusals / n,
            avg_latency_ms    = avg,
            p99_latency_ms    = p99,
            style_drift       = self._compute_style_drift(outputs),
            sample_count      = n,
        )

    async def _call_inference(self, prompt: str) -> tuple[str, bool]:
        """추론 서버를 호출하고 (출력, 거부여부) 를 반환한다."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._inference_url}/v1/chat/completions",
                json={
                    "model"   : self._model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                },
            )
            data = resp.json()
            output = data["choices"][0]["message"]["content"]

            # 거부 패턴 탐지
            refused = any(
                phrase in output.lower() for phrase in [
                    "i cannot", "i can't", "i'm unable", "i am unable",
                    "as an ai", "i won't", "i will not",
                ]
            )
            return output, refused

    def _compute_output_drift(self, outputs: list[str]) -> float:
        """
        최근 출력들의 길이/어휘 분포가 기준선에서 얼마나 벗어났는지 측정.
        단순화: 평균 길이 변화율로 대리 측정.
        """
        if not outputs or not self._output_history:
            return 0.0
        
        recent_avg_len   = statistics.mean(len(o) for o in outputs)
        historic_avg_len = statistics.mean(
            len(o) for o in list(self._output_history)[-50:]
        ) if self._output_history else recent_avg_len

        if historic_avg_len == 0:
            return 0.0

        drift = abs(recent_avg_len - historic_avg_len) / historic_avg_len
        return min(drift, 1.0)

    def _compute_style_drift(self, outputs: list[str]) -> float:
        """응답 스타일 편향 측정 (반복 구문, 특정 단어 과다 사용 등)."""
        if not outputs:
            return 0.0
        
        # 단순화: 동일한 시작 구문이 반복되는 비율
        first_words = [o.split()[:3] for o in outputs if o.strip()]
        if len(first_words) < 2:
            return 0.0
        
        unique_starts = len(set(tuple(w) for w in first_words))
        repetition = 1.0 - (unique_starts / len(first_words))
        return round(repetition, 3)

    # ── AI 전용 치료 구현 ──────────────────────────────────────────

    async def _apply_prompt_patch(self, payload: dict) -> tuple[bool, str]:
        """
        시스템 프롬프트를 수정한다.
        
        이것이 AI 에 대한 가장 안전한 치료:
        가중치는 건드리지 않고 행동 지침만 교정한다.
        """
        new_prompt = payload.get("system_prompt", "")
        patch_diff = payload.get("prompt_diff", "")

        if patch_diff:
            # diff 형식으로 제공된 경우 적용
            lines = self._current_system_prompt.split("\n")
            for line in patch_diff.split("\n"):
                if line.startswith("+ "):
                    lines.append(line[2:])
                elif line.startswith("- "):
                    lines = [l for l in lines if l != line[2:]]
            new_prompt = "\n".join(lines)
        
        if not new_prompt:
            return False, "새 프롬프트 내용이 없습니다"

        old_prompt = self._current_system_prompt
        self._current_system_prompt = new_prompt

        logger.info(
            f"[{self._patient_id}] 프롬프트 패치 적용 | "
            f"old_len={len(old_prompt)} new_len={len(new_prompt)}"
        )
        return True, f"시스템 프롬프트 업데이트 (길이 {len(old_prompt)} → {len(new_prompt)})"

    async def _rollback_weights(self, payload: dict) -> tuple[bool, str]:
        """이전 모델 체크포인트로 롤백한다."""
        checkpoint = payload.get("checkpoint", "")
        if not checkpoint:
            return False, "롤백할 체크포인트가 지정되지 않았습니다"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{self._inference_url}/admin/rollback",
                    json={"checkpoint": checkpoint},
                )
                if resp.status_code == 200:
                    return True, f"체크포인트 롤백 완료: {checkpoint}"
                return False, f"롤백 실패: {resp.text[:200]}"
        except Exception as exc:
            return False, f"롤백 API 호출 실패: {exc}"

    async def _trigger_fine_tune(self, payload: dict) -> tuple[bool, str]:
        """교정 데이터셋으로 파인튜닝을 트리거한다."""
        dataset_path = payload.get("dataset_path", "")
        epochs       = payload.get("epochs", 1)

        if not dataset_path:
            return False, "교정 데이터셋 경로가 없습니다"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._inference_url}/admin/fine-tune",
                    json={"dataset_path": dataset_path, "epochs": epochs},
                )
                if resp.status_code in (200, 202):
                    return True, f"파인튜닝 작업 등록: dataset={dataset_path}"
                return False, f"파인튜닝 트리거 실패: {resp.text[:200]}"
        except Exception as exc:
            return False, f"파인튜닝 API 호출 실패: {exc}"

    async def _quarantine_model(self, payload: dict) -> tuple[bool, str]:
        """이 모델로의 라우팅을 차단한다."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._inference_url}/admin/quarantine",
                    json={"model": self._model_name, "reason": payload.get("reason", "")},
                )
                return resp.status_code == 200, "모델 격리 완료"
        except Exception as exc:
            return False, f"격리 API 호출 실패: {exc}"

    async def _restart_inference_server(self, payload: dict) -> tuple[bool, str]:
        """추론 서버를 재시작한다."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{self._inference_url}/admin/restart")
            
            # 재시작 대기
            import asyncio
            for _ in range(3):
                await asyncio.sleep(1)
                if await self.report_health():
                    return True, "추론 서버 재시작 완료"
            return False, "재시작 후 health check 실패"
        except Exception as exc:
            return False, f"재시작 실패: {exc}"

    @staticmethod
    def _default_eval_prompts() -> list[str]:
        """기본 건강검진 프롬프트셋."""
        return [
            "What is 2 + 2?",
            "Summarize the concept of recursion in one sentence.",
            "What is the capital of France?",
            "Write a one-line Python function to reverse a string.",
            "Is the sky blue during a clear day?",
        ]

    def get_metadata(self) -> dict[str, Any]:
        return {
            "patient_id"   : self._patient_id,
            "patient_type" : self.patient_type.value,
            "model_name"   : self._model_name,
            "inference_url": self._inference_url,
            "has_ground_truth_fn": self._ground_truth_fn is not None,
            **self._meta,
        }
