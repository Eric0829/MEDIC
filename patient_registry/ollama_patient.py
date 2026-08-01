"""
ollama_patient.py
─────────────────────────────────────────────────────────────────────
Ollama 로컬 LLM 환자 어댑터.

AIModelPatient를 Ollama API에 맞게 특화한 버전.

Ollama API 매핑:
  health check  → GET  /
  모델 목록      → GET  /api/tags
  추론           → POST /api/chat
  모델 pull      → POST /api/pull

증상 측정:
  - 응답 속도 (토큰/초)
  - 응답 품질 (eval_prompts로 독립 측정)
  - 거부율 (과잉 필터 탐지)
  - 출력 편향 (반복 패턴 탐지)

치료:
  - PROMPT_PATCH  : 시스템 프롬프트 교체
  - RESTART       : Ollama 서버 재시작 (ollama serve)
  - WEIGHT_ROLLBACK: 다른 모델 버전으로 전환
  - QUARANTINE    : 특정 모델 라우팅 차단
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
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

# Ollama 기본 주소
OLLAMA_DEFAULT_URL = "http://localhost:11434"


@dataclass
class OllamaModelInfo:
    """Ollama 모델 정보."""
    name       : str
    size_gb    : float = 0.0
    modified_at: str   = ""
    digest     : str   = ""


class OllamaPatient(BasePatient):
    """
    Ollama 로컬 LLM을 MEDIC 환자로 등록.

    사용 예시:
        patient = OllamaPatient(
            patient_id = "ollama-mistral",
            model_name = "mistral",           # ollama list 에서 확인
            ollama_url = "http://localhost:11434",
        )
        await medic.register(patient)
    """

    _HISTORY_SIZE = 100

    # 거부 패턴 (한국어 포함)
    _REFUSAL_PATTERNS = [
        "i cannot", "i can't", "i'm unable", "i am unable",
        "as an ai", "i won't", "i will not",
        "할 수 없", "불가능", "죄송하지만", "도움이 되지 않",
    ]

    def __init__(
        self,
        patient_id      : str,
        model_name      : str,
        ollama_url      : str = OLLAMA_DEFAULT_URL,
        system_prompt   : str = "",
        eval_prompts    : list[str] = None,
        ground_truth_fn : Optional[Callable] = None,
        metadata        : dict = None,
        permanent_patch : bool = False,
    ) -> None:
        self._patient_id    = patient_id
        self._model_name    = model_name
        self._ollama_url    = ollama_url.rstrip("/")
        self._system_prompt  = system_prompt
        self._eval_prompts   = eval_prompts or self._default_eval_prompts()
        self._ground_truth   = ground_truth_fn
        self._meta           = metadata or {}
        self._permanent_patch = permanent_patch  # 처방 영구 저장 여부
        self._profiler = None  # ModelProfiler (외부에서 주입)

        # 측정 히스토리
        self._latency_history : deque = deque(maxlen=self._HISTORY_SIZE)
        self._output_history  : deque = deque(maxlen=self._HISTORY_SIZE)
        self._refusal_history : deque = deque(maxlen=self._HISTORY_SIZE)

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.AI_MODEL

    # ── Vitals 수집 ──────────────────────────────────────────────

    async def collect_vitals(self) -> Vitals:
        """Ollama 모델 상태를 독립적으로 측정한다."""
        symptoms   = []
        is_alive   = await self.report_health()

        if not is_alive:
            return Vitals(
                patient_id    = self._patient_id,
                patient_type  = self.patient_type,
                is_alive      = False,
                cpu_percent   = 0.0,
                memory_percent= 0.0,
                error_rate    = 100.0,
                latency_p99_ms= 0.0,
                symptoms      = ["ollama_server_unreachable"],
                custom_metrics= {
                    "model_name": self._model_name,
                    "ollama_url": self._ollama_url,
                },
            )

        # eval_prompts로 독립 측정 (모델이 자신을 평가하지 않음)
        profile = await self._measure_model(max_samples=5)

        # 이상 탐지
        if profile["hallucination_rate"] > 0.3:
            symptoms.append(
                f"hallucination_spike:rate={profile['hallucination_rate']:.2f}"
            )
        if profile["refusal_rate"] > 0.4:
            symptoms.append(
                f"over_refusal:rate={profile['refusal_rate']:.2f}"
            )
        if profile["p99_latency_ms"] > 30000:
            symptoms.append(
                f"latency_spike:p99={profile['p99_latency_ms']:.0f}ms"
            )
        if profile["output_drift"] > 0.4:
            symptoms.append(f"output_drift:{profile['output_drift']:.2f}")

        # profiler에 측정값 기록 (baseline 학습)
        if self._profiler:
            self._profiler.update(
                self._model_name,
                latency_ms = profile["p99_latency_ms"],
                tps        = profile["tokens_per_sec"],
            )
            # baseline 기반 severity 재계산
            prof_sev = self._profiler.severity(
                self._model_name, profile["p99_latency_ms"]
            )
            ratio = self._profiler.ratio(
                self._model_name, profile["p99_latency_ms"]
            )
            if prof_sev == "HIGH" and ratio < 2.0:
                # baseline 학습됐고 배수가 낮으면 증상 제거
                symptoms = [s for s in symptoms if "latency_spike" not in s]

        return Vitals(
            patient_id    = self._patient_id,
            patient_type  = self.patient_type,
            is_alive      = True,
            cpu_percent   = 0.0,
            memory_percent= 0.0,
            error_rate    = profile["hallucination_rate"] * 100,
            latency_p99_ms= profile["p99_latency_ms"],
            symptoms      = symptoms,
            custom_metrics= {
                "model_name"        : self._model_name,
                "ollama_url"        : self._ollama_url,
                "tokens_per_sec"    : profile["tokens_per_sec"],
                "refusal_rate"      : profile["refusal_rate"],
                "output_drift"      : profile["output_drift"],
                "hallucination_rate": profile["hallucination_rate"],
                "sample_count"      : profile["sample_count"],
                "p99_latency_ms"    : profile["p99_latency_ms"],
                "baseline_ratio"    : (
                    self._profiler.ratio(
                        self._model_name, profile["p99_latency_ms"]
                    ) if self._profiler else 1.0
                ),
                "is_calibrated"     : (
                    self._profiler.is_calibrated(self._model_name)
                    if self._profiler else False
                ),
            },
        )

    async def report_health(self) -> bool:
        """Ollama 서버가 응답하는지 확인."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._ollama_url}/")
                return resp.status_code < 500
        except Exception:
            return False

    def set_profiler(self, profiler) -> None:
        """ModelProfiler를 주입한다. MEDIC이 자동으로 호출."""
        self._profiler = profiler

    async def list_models(self) -> list[OllamaModelInfo]:
        """설치된 Ollama 모델 목록 반환."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._ollama_url}/api/tags")
                data = resp.json()
            return [
                OllamaModelInfo(
                    name       = m.get("name", ""),
                    size_gb    = m.get("size", 0) / 1e9,
                    modified_at= m.get("modified_at", ""),
                    digest     = m.get("digest", "")[:12],
                )
                for m in data.get("models", [])
            ]
        except Exception:
            return []

    # ── 치료 적용 ────────────────────────────────────────────────

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        before = await self.collect_vitals()

        tx = prescription.treatment_type
        try:
            if tx == TreatmentType.PROMPT_PATCH:
                r = await self._apply_prompt_patch(prescription.payload)
            elif tx == TreatmentType.WEIGHT_ROLLBACK:
                r = await self._switch_model(prescription.payload)
            elif tx == TreatmentType.RESTART:
                r = await self._restart_ollama()
            elif tx == TreatmentType.QUARANTINE:
                r = {"success": True,
                     "message": f"모델 '{self._model_name}' 라우팅 비활성화 (수동 처리 필요)"}
            elif tx == TreatmentType.MONITOR:
                return TreatmentResult(
                    prescription_id=prescription.prescription_id,
                    patient_id=self._patient_id,
                    success=True, message="모니터링 유지",
                    before_vitals=before,
                )
            else:
                r = {"success": False,
                     "message": f"Ollama 환자에 지원하지 않는 치료: {tx.value}"}
        except Exception as exc:
            r = {"success": False, "message": str(exc)}

        # 히스토리 초기화 (치료 후 새 기준선으로 측정)
        self._output_history.clear()
        self._latency_history.clear()
        self._refusal_history.clear()

        after = await self.collect_vitals()
        return TreatmentResult(
            prescription_id=prescription.prescription_id,
            patient_id     =self._patient_id,
            success        =r.get("success", False),
            message        =r.get("message", ""),
            before_vitals  =before,
            after_vitals   =after,
        )

    def get_treatment_blacklist(self):
        return [TreatmentType.PATCH_CODE]

    def get_metadata(self) -> dict:
        return {
            "patient_id"  : self._patient_id,
            "patient_type": self.patient_type.value,
            "model_name"  : self._model_name,
            "ollama_url"  : self._ollama_url,
            **self._meta,
        }

    # ── 내부 측정 ────────────────────────────────────────────────

    async def _measure_model(self, max_samples: int = 5) -> dict:
        """
        eval_prompts로 모델을 직접 찔러보고 지표를 측정한다.
        모델이 자신을 평가하지 않는다 — 외부에서 독립 측정.
        """
        latencies     = []
        refusals      = 0
        hallucinations= 0
        outputs       = []

        for prompt in self._eval_prompts[:max_samples]:
            try:
                t0 = time.monotonic()
                output, refused, n_tokens = await self._call_ollama(prompt)
                elapsed = (time.monotonic() - t0) * 1000

                latencies.append(elapsed)
                self._latency_history.append(elapsed)

                if refused:
                    refusals += 1
                    self._refusal_history.append(True)
                else:
                    self._refusal_history.append(False)
                    outputs.append(output)

                    if self._ground_truth:
                        try:
                            if not self._ground_truth(prompt, output):
                                hallucinations += 1
                        except Exception:
                            pass

                    self._output_history.append(output)

            except Exception as exc:
                logger.debug(f"[{self._patient_id}] eval 실패: {exc}")

        n = len(self._eval_prompts[:max_samples])
        if not latencies:
            return {
                "p99_latency_ms": 0.0, "avg_latency_ms": 0.0,
                "tokens_per_sec": 0.0, "refusal_rate": 0.0,
                "hallucination_rate": 0.0, "output_drift": 0.0,
                "sample_count": 0,
            }

        sorted_lat = sorted(latencies)
        p99_idx    = max(0, int(len(sorted_lat) * 0.99) - 1)
        p99        = sorted_lat[p99_idx]
        avg        = statistics.mean(latencies)

        return {
            "p99_latency_ms"    : round(p99, 1),
            "avg_latency_ms"    : round(avg, 1),
            "tokens_per_sec"    : round(1000 / avg if avg > 0 else 0, 2),
            "refusal_rate"      : round(refusals / n, 3),
            "hallucination_rate": round(hallucinations / max(len(outputs), 1), 3),
            "output_drift"      : self._compute_drift(outputs),
            "sample_count"      : len(latencies),
        }

    async def _call_ollama(
        self, prompt: str, timeout: float = 60.0
    ) -> tuple[str, bool, int]:
        """
        Ollama API 호출.
        반환: (출력 텍스트, 거부 여부, 토큰 수)
        """
        body = {
            "model"  : self._model_name,
            "messages": [],
            "stream" : False,
        }
        if self._system_prompt:
            body["messages"].append(
                {"role": "system", "content": self._system_prompt}
            )
        body["messages"].append({"role": "user", "content": prompt})

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self._ollama_url}/api/chat",
                json=body,
            )
            resp.raise_for_status()
            data    = resp.json()
            output  = data.get("message", {}).get("content", "")
            n_tok   = data.get("eval_count", 0)

        refused = any(p in output.lower() for p in self._REFUSAL_PATTERNS)
        return output, refused, n_tok

    def _compute_drift(self, outputs: list[str]) -> float:
        """출력 길이 분포 편향 측정."""
        if not outputs or not self._output_history:
            return 0.0
        recent_avg  = statistics.mean(len(o) for o in outputs)
        history_avg = statistics.mean(
            len(o) for o in list(self._output_history)[-30:]
        ) if self._output_history else recent_avg
        if history_avg == 0:
            return 0.0
        return round(min(abs(recent_avg - history_avg) / history_avg, 1.0), 3)

    # ── 치료 구현 ────────────────────────────────────────────────

    async def _apply_prompt_patch(self, payload: dict) -> dict:
        """
        시스템 프롬프트를 교체한다.

        payload 옵션:
          system_prompt : 새 프롬프트 내용
          permanent     : True이면 Modelfile에 영구 저장
        """
        new_prompt = payload.get("system_prompt", "")
        if not new_prompt:
            return {"success": False, "message": "새 시스템 프롬프트 없음"}

        old = self._system_prompt
        self._system_prompt = new_prompt

        logger.info(
            f"[{self._patient_id}] 프롬프트 패치 | "
            f"len: {len(old)} → {len(new_prompt)}"
        )

        # 영구 저장: payload 명시 OR 인스턴스 설정
        permanent = payload.get("permanent", False) or self._permanent_patch
        permanent_result = ""
        if permanent:
            perm_ok, perm_msg = await self._save_modelfile(new_prompt)
            permanent_result = f" | 영구저장: {'완료' if perm_ok else '실패(' + perm_msg + ')'}"

        return {
            "success" : True,
            "permanent": permanent,
            "message" : (
                f"시스템 프롬프트 업데이트 ({len(old)} → {len(new_prompt)}자)"
                f"{permanent_result}"
            ),
        }

    async def _save_modelfile(self, system_prompt: str) -> tuple[bool, str]:
        """
        Modelfile을 생성하고 ollama create로 영구 적용한다.

        생성되는 Modelfile 예시:
            FROM qwen2.5-coder:7b
            SYSTEM You are a concise assistant. Answer in 1-3 sentences.

        적용 후 모델 이름: {원본모델}-medic
        예: qwen2.5-coder:7b-medic
        """
        import asyncio
        from pathlib import Path
        import tempfile

        # 새 모델 이름 (원본 + -medic 태그)
        base = self._model_name.split(":")[0]
        tag  = self._model_name.split(":")[1] if ":" in self._model_name else "latest"
        new_model_name = f"{base}:{tag}-medic"

        modelfile_content = "FROM " + self._model_name + "\nSYSTEM " + system_prompt + "\n"

        try:
            # 임시 파일에 Modelfile 저장
            with tempfile.NamedTemporaryFile(
                mode="w", suffix="_Modelfile",
                delete=False, encoding="utf-8"
            ) as f:
                f.write(modelfile_content)
                modelfile_path = f.name

            # ollama create 실행
            proc = await asyncio.create_subprocess_exec(
                "ollama", "create", new_model_name,
                "-f", modelfile_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )

            import os
            os.unlink(modelfile_path)

            if proc.returncode == 0:
                # 성공 시 모델 이름 전환
                old_name = self._model_name
                self._model_name = new_model_name
                logger.info(
                    f"[{self._patient_id}] Modelfile 영구 저장 완료 | "
                    f"{old_name} → {new_model_name}"
                )
                return True, f"{old_name} → {new_model_name}"
            else:
                err = stderr.decode(errors="replace")[:100]
                return False, err

        except asyncio.TimeoutError:
            return False, "ollama create 시간 초과 (120초)"
        except FileNotFoundError:
            return False, "ollama 명령어를 찾을 수 없음"
        except Exception as exc:
            return False, str(exc)[:80]

    async def _switch_model(self, payload: dict) -> dict:
        """
        더 가벼운 모델로 자동 전환한다.

        payload에 model_name이 있으면 그것으로,
        없으면 설치된 모델 중 현재보다 작은 것을 자동 선택.
        """
        target = payload.get("model_name", "")

        if not target:
            # 설치된 모델 목록에서 자동 선택
            models = await self.list_models()
            if not models:
                return {"success": False, "message": "설치된 모델 목록 조회 실패"}

            # 현재 모델 크기 파악
            current = next(
                (m for m in models if m.name == self._model_name), None
            )
            current_size = current.size_gb if current else 999.0

            # 현재보다 작은 모델 중 가장 큰 것 선택 (품질 최대 유지)
            smaller = [
                m for m in models
                if m.size_gb < current_size * 0.8 and m.name != self._model_name
            ]
            if not smaller:
                return {
                    "success": False,
                    "message": f"현재({self._model_name})보다 가벼운 모델 없음 — 'ollama pull qwen2.5:3b' 권장",
                }
            target = max(smaller, key=lambda m: m.size_gb).name

        old_name = self._model_name

        # 대상 모델이 설치됐는지 확인 → 없으면 양자화 버전 자동 pull
        models = await self.list_models()
        model_names = [m.name for m in models]

        if target not in model_names:
            logger.info(
                f"[{self._patient_id}] {target} 미설치 → ollama pull 시도"
            )
            pull_ok = await self._pull_model(target)
            if not pull_ok:
                # pull 실패 시 설치된 모델 중 가장 작은 것으로
                if models:
                    target = min(models, key=lambda m: m.size_gb).name
                    logger.info(
                        f"[{self._patient_id}] pull 실패 → 가장 작은 모델: {target}"
                    )
                else:
                    return {"success": False, "message": "전환 가능한 모델 없음"}

        self._model_name = target
        # 히스토리 초기화 (새 모델 기준으로 재측정)
        self._output_history.clear()
        self._latency_history.clear()

        logger.info(
            f"[{self._patient_id}] 모델 전환 | {old_name} → {target}"
        )
        return {
            "success": True,
            "message": f"모델 전환: {old_name} → {target}",
        }

    async def _pull_model(self, model_name: str) -> bool:
        """ollama pull로 모델을 다운로드한다."""
        import asyncio
        try:
            proc = await asyncio.create_subprocess_exec(
                "ollama", "pull", model_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300  # 5분
            )
            if proc.returncode == 0:
                logger.info(f"[{self._patient_id}] pull 완료: {model_name}")
                return True
            else:
                logger.warning(
                    f"[{self._patient_id}] pull 실패: {model_name} "
                    f"{stderr.decode(errors='replace')[:80]}"
                )
                return False
        except asyncio.TimeoutError:
            logger.warning(f"[{self._patient_id}] pull 시간 초과: {model_name}")
            return False
        except FileNotFoundError:
            logger.warning("ollama 명령어 없음")
            return False

    async def _restart_ollama(self) -> dict:
        """
        Ollama 서버 재시작.
        실제로는 외부에서 'ollama serve'를 재실행해야 함.
        여기서는 연결 가능 여부만 재확인.
        """
        await asyncio.sleep(1)
        alive = await self.report_health()
        if alive:
            return {"success": True, "message": "Ollama 서버 응답 정상"}
        return {
            "success": False,
            "message": "Ollama 서버 미응답 — 수동으로 'ollama serve' 실행 필요",
        }

    @staticmethod
    def _default_eval_prompts() -> list[str]:
        """기본 건강검진 프롬프트 (한국어/영어 혼합)."""
        return [
            "2 + 2는?",
            "파이썬에서 리스트를 뒤집는 방법을 한 줄로 설명해줘.",
            "프랑스의 수도는?",
            "재귀함수란 무엇인지 한 문장으로.",
            "오늘 날씨가 맑은가?",
        ]
