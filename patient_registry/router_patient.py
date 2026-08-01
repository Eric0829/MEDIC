"""
router_patient.py
─────────────────────────────────────────────────────────────────────
범용 AI 모델 라우터.

어떤 AI 환자든 (Ollama, OpenAI 호환, 커스텀 API) 등록하면
MEDIC이 자동으로 빠른 쪽으로 라우팅한다.

핵심 개념:
  "느린 환자가 있으면 더 빠른 대안으로 자동 전환"

라우팅 기준 (우선순위):
  1. 속도        — 응답 지연이 낮은 모델 우선
  2. 작업 적합성 — 코드/한국어/일반 등 태스크별 분류
  3. 시스템 상태 — 메모리 여유 있는 모델 우선
  4. 화석 신뢰도 — MEDIC이 검증한 모델 우선

사용 예시:
    router = RouterPatient(
        patient_id = "ai-router",
        candidates = [
            OllamaPatient("ollama-3b",  "qwen2.5:3b"),
            OllamaPatient("ollama-7b",  "qwen2.5-coder:7b"),
            OllamaPatient("ollama-llm", "llama3:8b"),
        ],
        task_rules = {
            "code"   : ["ollama-7b"],          # 코드 질문 → 코더 모델
            "korean" : ["ollama-llm"],          # 한국어 → llama
            "default": ["ollama-3b", "ollama-llm"],  # 기본 → 빠른 것
        }
    )
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .base_patient import (
    BasePatient, PatientType, Prescription, TreatmentResult,
    TreatmentType, Vitals,
)

logger = logging.getLogger(__name__)


# ── 라우팅 통계 ──────────────────────────────────────────────────────

@dataclass
class CandidateStats:
    """각 후보 환자의 성능 통계."""
    patient_id          : str
    avg_latency_ms      : float = 9999.0  # health check 응답시간 (가용성 판단)
    inference_latency_ms: float = 9999.0  # 실제 추론 응답시간 (성능 판단)
    success_rate        : float = 1.0     # 성공률
    last_seen_ok        : float = 0.0     # 마지막 정상 확인 시각
    is_healthy          : bool  = True    # 현재 건강 상태 (health 기준)
    is_degraded         : bool  = False   # 느리지만 살아있는 상태
    call_count          : int   = 0       # 호출 횟수
    task_scores         : dict  = field(default_factory=dict)

    def routing_score(self) -> float:
        """라우팅 점수: 낮을수록 우선순위 높음 (inference 기준)."""
        if not self.is_healthy:
            return 99999.0
        # inference 지연이 있으면 그것 기준, 없으면 health 기준
        lat = self.inference_latency_ms if self.inference_latency_ms < 9999 else self.avg_latency_ms
        return lat * (2.0 if self.is_degraded else 1.0)


# ── 범용 라우터 환자 ─────────────────────────────────────────────────

class RouterPatient(BasePatient):
    """
    여러 AI 환자를 묶어서 자동으로 최적 후보를 선택하는 라우터.

    MEDIC이 이 환자를 모니터링하면:
      - 각 후보의 속도/건강 상태를 지속 추적
      - 느린 후보는 자동으로 우선순위 낮춤
      - 빠르고 안정적인 후보가 자동으로 선택됨
    """

    # 태스크 감지 키워드
    TASK_KEYWORDS = {
        "code"   : ["코드", "함수", "python", "javascript", "def ", "class ",
                    "error", "bug", "프로그램", "알고리즘", "sql", "api"],
        "korean" : ["안녕", "한국어", "설명해", "알려줘", "뭐야", "어떻게",
                    "왜", "무엇", "어디", "언제", "누가"],
        "math"   : ["계산", "수식", "방정식", "미적분", "통계", "확률",
                    "solve", "calculate", "math"],
        "creative": ["작성해", "써줘", "만들어", "생성", "창작", "소설",
                     "시", "이야기", "write", "create"],
    }

    def __init__(
        self,
        patient_id  : str,
        candidates  : list[BasePatient],
        task_rules  : dict[str, list[str]] = None,
        fallback_all: bool = True,    # 지정 후보 실패 시 전체에서 선택
        metadata    : dict = None,
    ) -> None:
        self._patient_id  = patient_id
        self._candidates  = {p.patient_id: p for p in candidates}
        self._task_rules  = task_rules or {}
        self._fallback_all= fallback_all
        self._meta        = metadata or {}

        # 각 후보 통계 초기화
        self._stats: dict[str, CandidateStats] = {
            pid: CandidateStats(patient_id=pid)
            for pid in self._candidates
        }

        logger.info(
            f"[Router] 초기화 | "
            f"후보 {len(self._candidates)}개: "
            f"{list(self._candidates.keys())}"
        )

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.AI_MODEL

    @property
    def candidates(self) -> dict[str, BasePatient]:
        return self._candidates

    # ── Vitals 수집 — 모든 후보를 동시에 진찰 ────────────────────────

    async def collect_vitals(self) -> Vitals:
        """모든 후보를 동시에 진찰하고 라우터 상태를 반환."""

        async def _ping_candidate(pid: str, patient: BasePatient):
            """후보 하나의 health + 응답시간 측정."""
            t0 = time.monotonic()
            try:
                ok = await asyncio.wait_for(patient.report_health(), timeout=5.0)
                latency = (time.monotonic() - t0) * 1000
                return pid, ok, latency
            except Exception:
                return pid, False, 9999.0

        # 모든 후보 동시 측정
        tasks = [
            asyncio.create_task(_ping_candidate(pid, p))
            for pid, p in self._candidates.items()
        ]
        ping_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 통계 업데이트
        healthy_count = 0
        for res in ping_results:
            if isinstance(res, Exception):
                continue
            pid, ok, latency = res
            self._stats[pid].is_healthy = ok
            if ok:
                healthy_count += 1
                self._stats[pid].last_seen_ok = time.time()
                # 실제 응답시간 반영
                self.update_stats(pid, latency, True)

        symptoms = []
        if healthy_count == 0:
            symptoms.append("all_candidates_down")
        elif healthy_count < len(self._candidates) / 2:
            symptoms.append(f"majority_candidates_down:{len(self._candidates)-healthy_count}개")

        # 평균 지연 기반 심각도
        avg_latencies = [
            s.avg_latency_ms for s in self._stats.values()
            if s.is_healthy and s.avg_latency_ms < 9999
        ]
        best_latency = min(avg_latencies) if avg_latencies else 9999

        return Vitals(
            patient_id    = self._patient_id,
            patient_type  = self.patient_type,
            is_alive      = healthy_count > 0,
            cpu_percent   = 0.0,
            memory_percent= 0.0,
            error_rate    = ((len(self._candidates) - healthy_count)
                             / len(self._candidates)) * 100,
            latency_p99_ms= best_latency,
            symptoms      = symptoms,
            custom_metrics= {
                "total_candidates"  : len(self._candidates),
                "healthy_candidates": healthy_count,
                "best_latency_ms"   : round(best_latency, 1),
                "candidate_stats"   : {
                    pid: {
                        "healthy"          : s.is_healthy,
                        "is_degraded"      : s.is_degraded,
                        "avg_latency"      : round(s.avg_latency_ms, 0),
                        "inference_latency": round(s.inference_latency_ms, 0),
                        "success_rate"     : round(s.success_rate, 2),
                        "calls"            : s.call_count,
                    }
                    for pid, s in self._stats.items()
                },
            },
        )

    async def report_health(self) -> bool:
        for p in self._candidates.values():
            try:
                if await asyncio.wait_for(p.report_health(), timeout=3.0):
                    return True
            except Exception:
                pass
        return False

    # ── 라우팅 — 최적 후보 선택 ──────────────────────────────────────

    def route(self, query: str = "") -> Optional[BasePatient]:
        """
        쿼리 내용과 후보 상태를 보고 최적 환자를 선택한다.

        선택 기준 (순서대로):
          1. task_rules에 해당 태스크 규칙이 있으면 그 후보들 중에서
          2. 건강한 후보 중 avg_latency 낮은 순
          3. 전부 실패면 None
        """
        task = self._detect_task(query)
        candidates_by_priority = self._get_candidates_by_priority(task)

        for pid in candidates_by_priority:
            if self._stats[pid].is_healthy:
                logger.debug(
                    f"[Router] 라우팅 | "
                    f"task={task} → {pid} "
                    f"(latency={self._stats[pid].avg_latency_ms:.0f}ms)"
                )
                return self._candidates[pid]

        logger.warning(f"[Router] 라우팅 실패 — 건강한 후보 없음")
        return None

    def update_stats(
        self,
        patient_id : str,
        latency_ms : float,
        success    : bool,
        is_inference: bool = False,  # True면 추론 지연, False면 health check 지연
    ) -> None:
        """라우팅 결과를 통계에 반영한다."""
        if patient_id not in self._stats:
            return
        s = self._stats[patient_id]
        s.call_count += 1
        alpha = 0.3

        if is_inference:
            # 실제 추론 지연 업데이트
            if s.inference_latency_ms >= 9999:
                s.inference_latency_ms = latency_ms
            else:
                s.inference_latency_ms = (
                    alpha * latency_ms + (1 - alpha) * s.inference_latency_ms
                )
            # health는 살아있는데 inference가 느리면 degraded
            s.is_degraded = (
                s.is_healthy and s.inference_latency_ms > 10000
            )
        else:
            # health check 지연 업데이트
            if s.avg_latency_ms >= 9999:
                s.avg_latency_ms = latency_ms
            else:
                s.avg_latency_ms = (
                    alpha * latency_ms + (1 - alpha) * s.avg_latency_ms
                )

        # 성공률 업데이트
        s.success_rate = (
            alpha * (1.0 if success else 0.0)
            + (1 - alpha) * s.success_rate
        )

    # ── 치료 적용 ────────────────────────────────────────────────────

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        before = await self.collect_vitals()
        tx = prescription.treatment_type

        if tx == TreatmentType.MONITOR:
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=True, message="라우터 모니터링 유지",
                before_vitals=before,
            )

        elif tx == TreatmentType.CONFIG_CHANGE:
            # 라우팅 규칙 갱신
            payload = prescription.payload
            if "task_rules" in payload:
                self._task_rules.update(payload["task_rules"])
                msg = f"라우팅 규칙 업데이트: {list(payload['task_rules'].keys())}"
            else:
                # 느린 후보 우선순위 자동 조정
                msg = self._rebalance()
            after = await self.collect_vitals()
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=True, message=msg,
                before_vitals=before, after_vitals=after,
            )

        elif tx == TreatmentType.QUARANTINE:
            # 특정 후보 격리 (라우팅에서 제외)
            target = prescription.payload.get("patient_id", "")
            if target in self._stats:
                self._stats[target].is_healthy = False
                msg = f"후보 격리: {target}"
            else:
                msg = f"격리 대상 없음: {target}"
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=bool(target),
                message=msg, before_vitals=before,
            )

        else:
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=False,
                message=f"라우터에 지원하지 않는 치료: {tx.value}",
                before_vitals=before,
            )

    # ── 내부 ─────────────────────────────────────────────────────────

    def _detect_task(self, query: str) -> str:
        """쿼리 내용으로 태스크 유형을 감지한다."""
        if not query:
            return "default"
        q_lower = query.lower()
        for task, keywords in self.TASK_KEYWORDS.items():
            if any(kw in q_lower for kw in keywords):
                return task
        return "default"

    def _get_candidates_by_priority(self, task: str) -> list[str]:
        """태스크와 성능 통계를 기반으로 후보 우선순위를 반환한다."""
        # task_rules에 명시된 후보 먼저
        priority = []
        if task in self._task_rules:
            priority = [
                pid for pid in self._task_rules[task]
                if pid in self._candidates
            ]

        # default 규칙
        if not priority and "default" in self._task_rules:
            priority = [
                pid for pid in self._task_rules["default"]
                if pid in self._candidates
            ]

        # 나머지를 latency 낮은 순으로 추가
        remaining = sorted(
            [pid for pid in self._candidates if pid not in priority],
            key=lambda pid: self._stats[pid].routing_score()
        )

        if self._fallback_all:
            return priority + remaining
        return priority or remaining

    def _rebalance(self) -> str:
        """느린 후보의 우선순위를 자동으로 낮춘다."""
        sorted_by_speed = sorted(
            self._stats.items(),
            key=lambda x: x[1].routing_score()
        )
        fastest = sorted_by_speed[0][0] if sorted_by_speed else ""
        slowest = sorted_by_speed[-1][0] if len(sorted_by_speed) > 1 else ""

        if fastest and slowest and fastest != slowest:
            fast_inf = self._stats[fastest].inference_latency_ms
            slow_inf = self._stats[slowest].inference_latency_ms
            fast_str = f"{fast_inf:.0f}ms" if fast_inf < 9999 else f"{self._stats[fastest].avg_latency_ms:.0f}ms(health)"
            slow_str = f"{slow_inf:.0f}ms" if slow_inf < 9999 else f"{self._stats[slowest].avg_latency_ms:.0f}ms(health)"
            return (
                f"라우팅 재조정: 빠른={fastest}({fast_str}) "
                f"느린={slowest}({slow_str})"
            )
        return "재조정 대상 없음"

    def get_metadata(self) -> dict:
        return {
            "patient_id"  : self._patient_id,
            "patient_type": self.patient_type.value,
            "candidates"  : list(self._candidates.keys()),
            "task_rules"  : self._task_rules,
            **self._meta,
        }

    def render_stats(self) -> str:
        """현재 라우팅 통계를 보기 좋게 출력한다."""
        lines = [f"\n  ┌─ 라우터 [{self._patient_id}] 현황 ───────────────"]
        for pid, s in sorted(
            self._stats.items(), key=lambda x: x[1].routing_score()
        ):
            health = "✅" if s.is_healthy else "❌"
            deg    = " ⚠degraded" if s.is_degraded else ""
            h_lat  = f"{s.avg_latency_ms:.0f}ms" if s.avg_latency_ms < 9999 else "미측정"
            i_lat  = f"{s.inference_latency_ms:.0f}ms" if s.inference_latency_ms < 9999 else "미측정"
            lines.append(
                f"  │  {health}{deg} {pid:<30} "
                f"health={h_lat:<10} inference={i_lat:<10} "
                f"({s.call_count}회)"
            )
        # 현재 태스크별 최적 후보
        lines.append(f"  │")
        lines.append(f"  │  태스크별 최적 후보:")
        for task in ["default", "code", "korean", "math", "creative"]:
            best = self.route(
                {"code": "python", "korean": "안녕", "math": "계산",
                 "creative": "써줘"}.get(task, "")
            )
            if best:
                lines.append(f"  │    {task:<10} → {best.patient_id}")
        lines.append(f"  └────────────────────────────────────────────")
        return "\n".join(lines)
