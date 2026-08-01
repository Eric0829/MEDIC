"""
independence_tracker.py
─────────────────────────────────────────────────────────────────────
MEDIC 자립도 추적기.

uics_v31의 IndependenceTracker 철학을 MEDIC에 이식:
  "Teacher LLM 의존도 → 0"
  → MEDIC 버전: "SLM 의존도 → 0"

UICS의 해결 단계 대응:
  UICS              MEDIC
  ──────────────────────────────────────
  memory_hit      → fossil_hit      (FossilStore에서 즉시 처방)
  concept_block   → rule_hit        (RuleBasedFallback으로 해결)
  cache_hit       → record_hit      (MedicalRecords 과거 케이스 재사용)
  teacher_mock    → lvector_only    (L-벡터만으로 처방 결정)
  teacher_llm     → slm_call        (SLM 실제 호출)
  web_search      → external_call   (외부 리소스 필요)
  clarifier       → human_needed    (사람 개입 필요)

목표 지표:
  Independence Score = SLM 불필요 처방 / 전체 처방
  목표: 0.85 이상 (85%는 SLM 없이 처리)

이 점수가 올라간다는 것은:
  - FossilStore에 검증된 처방이 쌓이고 있다는 뜻
  - L-벡터 분석만으로 판단 가능한 케이스가 늘고 있다는 뜻
  - MEDIC이 점점 외부 의존 없이 자립하고 있다는 뜻
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import collections
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── 처방 해결 단계 정의 ──────────────────────────────────────────────

class Stage:
    FOSSIL_HIT    = "fossil_hit"     # FossilStore 즉시 매칭 → SLM 불필요
    RULE_HIT      = "rule_hit"       # RuleBasedFallback 처리 → SLM 불필요
    RECORD_HIT    = "record_hit"     # 과거 케이스 재사용 → SLM 불필요
    LVECTOR_ONLY  = "lvector_only"   # L-벡터 분석만으로 처방 → SLM 불필요
    SLM_CALL      = "slm_call"       # SLM 실제 호출 → SLM 필요
    EXTERNAL_CALL = "external_call"  # 외부 리소스 필요 → 의존
    HUMAN_NEEDED  = "human_needed"   # 에스컬레이션 → 사람 필요


# 단계별 SLM 필요 여부
_SLM_REQUIRED: dict[str, bool] = {
    Stage.FOSSIL_HIT   : False,
    Stage.RULE_HIT     : False,
    Stage.RECORD_HIT   : False,
    Stage.LVECTOR_ONLY : False,
    Stage.SLM_CALL     : True,
    Stage.EXTERNAL_CALL: True,
    Stage.HUMAN_NEEDED : False,  # 사람이 해결하지만 SLM은 불필요
}

# 단계 우선순위 (독립적일수록 앞)
_STAGE_PRIORITY = [
    Stage.FOSSIL_HIT,
    Stage.RECORD_HIT,
    Stage.RULE_HIT,
    Stage.LVECTOR_ONLY,
    Stage.SLM_CALL,
    Stage.EXTERNAL_CALL,
    Stage.HUMAN_NEEDED,
]


# ── 처방 기록 ────────────────────────────────────────────────────────

@dataclass
class PrescriptionRecord:
    """처방 1건의 해결 경로 기록."""
    turn          : int
    patient_id    : str
    patient_type  : str
    severity      : str
    stage         : str
    slm_used      : bool
    treatment_type: str
    success       : Optional[bool] = None
    confidence    : float = 0.0
    l_vector_hit  : bool  = False   # L-벡터가 주요 근거였는가
    ts            : float = field(default_factory=time.time)


# ── 자립도 추적기 ────────────────────────────────────────────────────

class MedicIndependenceTracker:
    """
    MEDIC 처방 파이프라인의 SLM 의존도를 추적한다.

    매 처방마다 "어느 단계에서 해결됐는가"를 기록하고
    SLM 없이 해결된 비율을 independence score로 계산한다.

    사용 예시:
        tracker = MedicIndependenceTracker()

        # 처방 후 기록
        tracker.record(
            patient_id    = "api-gateway",
            patient_type  = "python_service",
            severity      = "HIGH",
            treatment_type= "restart",
            stage         = Stage.RULE_HIT,
            success       = True,
        )

        print(tracker.render())
    """

    WINDOW = 50  # 독립도 계산 기준 최근 N건

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._history  : list[PrescriptionRecord] = []
        self._turn     : int = 0
        self._persist  : Optional[Path] = Path(persist_path) if persist_path else None

        if self._persist and self._persist.exists():
            self._load()

    # ── 해결 단계 자동 추론 ──────────────────────────────────────────

    def infer_stage(
        self,
        treat_result    : dict,
        slm_was_called  : bool = False,
        fossil_matched  : bool = False,
        record_matched  : bool = False,
        lvector_decided : bool = False,
    ) -> str:
        """
        treat() 결과와 내부 플래그로 해결 단계를 추론한다.

        우선순위: fossil > record > lvector > rule > slm > human
        """
        status = treat_result.get("status", "")

        if status == "escalated":
            return Stage.HUMAN_NEEDED

        if status == "monitoring":
            # 이상 없음 — 규칙으로 처리
            return Stage.RULE_HIT

        if status == "prescription_rejected":
            # L-벡터가 거부 → L-벡터가 판단
            return Stage.LVECTOR_ONLY

        if fossil_matched:
            return Stage.FOSSIL_HIT

        if record_matched:
            return Stage.RECORD_HIT

        if lvector_decided:
            return Stage.LVECTOR_ONLY

        if slm_was_called:
            return Stage.SLM_CALL

        # second_opinion이 SKIPPED이고 SLM도 없으면 rule
        second_op = treat_result.get("second_opinion", "")
        if second_op == "SKIPPED":
            return Stage.RULE_HIT

        return Stage.RULE_HIT

    # ── 기록 ─────────────────────────────────────────────────────────

    def record(
        self,
        patient_id    : str,
        patient_type  : str,
        severity      : str,
        treatment_type: str,
        stage         : str,
        success       : Optional[bool] = None,
        confidence    : float = 0.0,
        l_vector_hit  : bool  = False,
    ) -> PrescriptionRecord:
        """처방 1건을 기록한다."""
        self._turn += 1
        rec = PrescriptionRecord(
            turn          = self._turn,
            patient_id    = patient_id,
            patient_type  = patient_type,
            severity      = severity,
            stage         = stage,
            slm_used      = _SLM_REQUIRED.get(stage, False),
            treatment_type= treatment_type,
            success       = success,
            confidence    = confidence,
            l_vector_hit  = l_vector_hit,
        )
        self._history.append(rec)

        if self._persist:
            self._save()

        return rec

    def record_from_result(
        self,
        patient_id    : str,
        patient_type  : str,
        severity      : str,
        treat_result  : dict,
        slm_was_called: bool = False,
        fossil_matched: bool = False,
        record_matched: bool = False,
        lvector_decided: bool = False,
    ) -> PrescriptionRecord:
        """treat() 결과 dict에서 직접 기록한다."""
        stage = self.infer_stage(
            treat_result, slm_was_called,
            fossil_matched, record_matched, lvector_decided
        )
        treatment = treat_result.get("treatment", "unknown")
        success   = treat_result.get("success")
        return self.record(
            patient_id    = patient_id,
            patient_type  = patient_type,
            severity      = severity,
            treatment_type= treatment,
            stage         = stage,
            success       = success,
            l_vector_hit  = bool(treat_result.get("l_vector")),
        )

    # ── 독립도 계산 ──────────────────────────────────────────────────

    def score(self) -> float:
        """최근 WINDOW건 기준 independence score (0~1)."""
        recent = self._history[-self.WINDOW:]
        if not recent:
            return 0.0
        independent = sum(1 for r in recent if not r.slm_used)
        return round(independent / len(recent), 3)

    def slm_call_rate(self) -> float:
        """SLM 실제 호출 비율."""
        recent = self._history[-self.WINDOW:]
        if not recent:
            return 0.0
        slm_calls = sum(1 for r in recent if r.stage == Stage.SLM_CALL)
        return round(slm_calls / len(recent), 3)

    def lvector_contribution(self) -> float:
        """L-벡터가 처방 근거가 된 비율."""
        recent = self._history[-self.WINDOW:]
        if not recent:
            return 0.0
        return round(sum(1 for r in recent if r.l_vector_hit) / len(recent), 3)

    def fossil_hit_rate(self) -> float:
        """FossilStore 즉시 매칭 비율."""
        recent = self._history[-self.WINDOW:]
        if not recent:
            return 0.0
        return round(
            sum(1 for r in recent if r.stage == Stage.FOSSIL_HIT) / len(recent), 3
        )

    def success_rate(self) -> float:
        """치료 성공률 (결과 기록된 건만)."""
        with_result = [r for r in self._history if r.success is not None]
        if not with_result:
            return 0.0
        return round(sum(1 for r in with_result if r.success) / len(with_result), 3)

    def verdict(self) -> str:
        s = self.score()
        if s >= 0.85:
            return "INDEPENDENT - no SLM required"
        elif s >= 0.70:
            return "MOSTLY_INDEPENDENT - continue fossil learning"
        elif s >= 0.50:
            return "PARTIAL - SLM assist recommended"
        else:
            return "DEPENDENT - needs SLM or stronger rules"

    def trend(self, window: int = 10) -> str:
        """최근 추세: improving / stable / declining."""
        if len(self._history) < window * 2:
            return "insufficient_data"
        old = self._history[-(window * 2):-window]
        new = self._history[-window:]
        old_score = sum(1 for r in old if not r.slm_used) / len(old)
        new_score = sum(1 for r in new if not r.slm_used) / len(new)
        delta = new_score - old_score
        if delta >= 0.1:
            return "improving"
        elif delta <= -0.1:
            return "declining"
        return "stable"

    # ── 환자별 분석 ──────────────────────────────────────────────────

    def patient_stats(self, patient_id: str) -> dict:
        """특정 환자의 독립도 통계."""
        records = [r for r in self._history if r.patient_id == patient_id]
        if not records:
            return {}
        total   = len(records)
        slm_cnt = sum(1 for r in records if r.slm_used)
        return {
            "patient_id"  : patient_id,
            "total"       : total,
            "independence": round(1 - slm_cnt / total, 3),
            "slm_rate"    : round(slm_cnt / total, 3),
            "success_rate": round(
                sum(1 for r in records if r.success) /
                max(sum(1 for r in records if r.success is not None), 1), 3
            ),
            "stage_dist"  : dict(
                collections.Counter(r.stage for r in records)
            ),
        }

    def all_patient_stats(self) -> list[dict]:
        """모든 환자의 독립도 요약."""
        pids = list(dict.fromkeys(r.patient_id for r in self._history))
        return [self.patient_stats(pid) for pid in pids]

    # ── 표준 테스트 배터리 ──────────────────────────────────────────

    def run_benchmark(self, medic) -> dict:
        """
        MEDIC 독립성 벤치마크.

        uics_v31의 independence_checker.run()에 대응.
        다양한 시나리오를 시뮬레이션하고 SLM 없이 처리 가능한지 측정.

        실제 환자 없이도 RuleBasedFallback + SymptomAnalyzer만으로
        얼마나 커버 가능한지 측정한다.
        """
        from medic_core import SymptomAnalyzer
        from infrastructure.local_slm import RuleBasedFallback
        from patient_registry.base_patient import PatientType, Vitals, TreatmentType

        analyzer = SymptomAnalyzer()
        fallback = RuleBasedFallback()

        # (시나리오명, Vitals, 기대 severity, SLM 없이 처리 가능한지)
        BATTERY = [
            ("서비스 다운",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=False, cpu_percent=0, memory_percent=0,
                    error_rate=0, latency_p99_ms=0),
             "CRITICAL", True),
            ("CPU 과부하",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=True, cpu_percent=95, memory_percent=50,
                    error_rate=0, latency_p99_ms=200),
             "HIGH", True),
            ("메모리 압박",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=True, cpu_percent=40, memory_percent=90,
                    error_rate=0, latency_p99_ms=300),
             "HIGH", True),
            ("에러율 급등",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=True, cpu_percent=30, memory_percent=40,
                    error_rate=55, latency_p99_ms=500),
             "CRITICAL", True),
            ("AI 환각 급등",
             Vitals(patient_id="t", patient_type=PatientType.AI_MODEL,
                    is_alive=True, cpu_percent=30, memory_percent=50,
                    error_rate=0, latency_p99_ms=300,
                    symptoms=["hallucination_spike:current=0.4,baseline=0.05"]),
             "HIGH", True),
            ("AI 출력 이탈",
             Vitals(patient_id="t", patient_type=PatientType.AI_MODEL,
                    is_alive=True, cpu_percent=20, memory_percent=40,
                    error_rate=0, latency_p99_ms=200,
                    symptoms=["output_drift:0.45"]),
             "MEDIUM", True),
            ("응답 지연",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=True, cpu_percent=50, memory_percent=60,
                    error_rate=2, latency_p99_ms=8000),
             "HIGH", True),
            ("L-벡터 위험 (EMERGENCE+DYNAMICS)",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=True, cpu_percent=40, memory_percent=50,
                    error_rate=0, latency_p99_ms=300,
                    symptoms=["l_vector_risk_dims:EMERGENCE,DYNAMICS"]),
             "MEDIUM", True),
            ("정상 상태",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=True, cpu_percent=20, memory_percent=30,
                    error_rate=0, latency_p99_ms=100),
             "LOW", True),
            ("K8s 클러스터 문제",
             Vitals(patient_id="t", patient_type=PatientType.K8S_WORKLOAD,
                    is_alive=True, cpu_percent=85, memory_percent=80,
                    error_rate=8, latency_p99_ms=4000),
             "HIGH", True),
            # SLM 없이는 어려운 케이스 (복잡한 코드 패치)
            ("복잡한 코드 패치 필요",
             Vitals(patient_id="t", patient_type=PatientType.PYTHON_SERVICE,
                    is_alive=True, cpu_percent=50, memory_percent=60,
                    error_rate=5, latency_p99_ms=1500,
                    symptoms=["l_vector_risk_dims:RECURRENCE,COMPOSITION,DYNAMICS,EMERGENCE"]),
             "HIGH", False),  # 4개 위험 차원 → SLM 필요
        ]

        passed = 0
        total  = len(BATTERY)
        by_cat : dict = {}
        details: list = []

        for name, vitals, exp_severity, can_handle_without_slm in BATTERY:
            diag = analyzer.analyze(vitals)
            rx   = fallback.generate_prescription(
                diag.root_cause,
                vitals.custom_metrics.get("l_vector", {}) if vitals.custom_metrics else {},
                vitals.patient_type.value,
                vitals.custom_metrics.get("risk_dims", []) if vitals.custom_metrics else [],
                diag.severity,
            )

            severity_ok = diag.severity == exp_severity
            rx_ok       = rx["treatment_type"] != "manual_intervention"

            # 처리 성공 기준: severity 정확 + 처방 생성됨
            success = severity_ok and rx_ok

            if success and can_handle_without_slm:
                passed += 1
            elif not can_handle_without_slm and not success:
                passed += 1  # SLM 필요 케이스를 올바르게 에스컬레이션

            details.append({
                "name"      : name,
                "severity"  : diag.severity,
                "expected"  : exp_severity,
                "treatment" : rx["treatment_type"],
                "ok"        : success,
                "slm_needed": not can_handle_without_slm,
            })

        score = passed / total
        if score >= 0.85:
            verdict = "INDEPENDENT - rules + L-vector are enough"
        elif score >= 0.70:
            verdict = "MOSTLY_INDEPENDENT - some gaps remain"
        elif score >= 0.50:
            verdict = "PARTIAL - SLM recommended"
        else:
            verdict = "DEPENDENT - SLM required"

        return {
            "score"  : round(score, 3),
            "passed" : passed,
            "total"  : total,
            "verdict": verdict,
            "details": details,
        }

    # ── 전체 통계 ────────────────────────────────────────────────────

    def stats(self) -> dict:
        stage_dist = dict(collections.Counter(
            r.stage for r in self._history[-self.WINDOW:]
        ))
        return {
            "total"               : len(self._history),
            "independence_score"  : self.score(),
            "verdict"             : self.verdict(),
            "trend"               : self.trend(),
            "slm_call_rate"       : self.slm_call_rate(),
            "lvector_contribution": self.lvector_contribution(),
            "fossil_hit_rate"     : self.fossil_hit_rate(),
            "success_rate"        : self.success_rate(),
            "stage_distribution"  : stage_dist,
        }

    def render(self) -> str:
        s     = self.stats()
        score = s["independence_score"]
        filled = min(20, int(score * 20))
        bar   = "#" * filled + "." * (20 - filled)
        ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            "",
            f"  +-- MEDIC Independence Report ({ts}) --------------",
            "  |",
            f"  |  [{bar}]  {score:.1%}",
            f"  |  verdict: {s['verdict']}",
            f"  |  trend:   {s['trend']}",
            "  |",
            f"  |  total treatments: {s['total']}",
            f"  |  slm call rate:    {s['slm_call_rate']:.1%}",
            f"  |  l-vector usage:   {s['lvector_contribution']:.1%}",
            f"  |  fossil hit rate:  {s['fossil_hit_rate']:.1%}",
            f"  |  success rate:     {s['success_rate']:.1%}",
            "  |",
            "  |  stage distribution:",
        ]
        for stage, cnt in sorted(
            s["stage_distribution"].items(), key=lambda x: -x[1]
        ):
            slm_mark = " <- SLM" if _SLM_REQUIRED.get(stage) else ""
            lines.append(f"  |    {stage:<18} {cnt}{slm_mark}")

        lines.append("  +------------------------------------------------")
        return "\n".join(lines)

    # ── 영속화 ───────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            data = [
                {
                    "turn": r.turn, "patient_id": r.patient_id,
                    "patient_type": r.patient_type, "severity": r.severity,
                    "stage": r.stage, "slm_used": r.slm_used,
                    "treatment_type": r.treatment_type,
                    "success": r.success, "confidence": r.confidence,
                    "l_vector_hit": r.l_vector_hit, "ts": r.ts,
                }
                for r in self._history[-200:]  # 최근 200건만 저장
            ]
            self._persist.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _load(self) -> None:
        try:
            data = json.loads(self._persist.read_text(encoding="utf-8"))
            for d in data:
                self._history.append(PrescriptionRecord(**d))
            if self._history:
                self._turn = self._history[-1].turn
        except Exception:
            pass
