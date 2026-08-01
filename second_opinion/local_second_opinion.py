"""
local_second_opinion.py
─────────────────────────────────────────────────────────────────────
완전 로컬 2차 소견 엔진.

외부 API 없음. 외부 트랜스포머 없음.

두 개의 독립된 검증자가 병렬로 작동한다:

  검증자 A: LocalSLM (GGUF 로컬 모델)
    → 자연어 맥락 이해, 처방 의도 검토
    → 언어적 관점

  검증자 B: LVectorReviewer (DeCODE L-벡터)
    → 수학적 구조 분석, 결정론적
    → 언어 편향 없음, 항상 같은 입력 = 같은 결과

왜 이 둘의 조합인가:
  A는 B의 언어적 맹점을 보완한다.
  B는 A의 언어 편향을 차단한다.
  둘 다 외부에 의존하지 않는다.

판정 규칙:
  A=APPROVE, B=APPROVE → APPROVE (고신뢰)
  A=APPROVE, B=REJECT  → REJECT  (구조적 문제 우선)
  A=REJECT,  B=APPROVE → 재검토 (SLM 과잉 보수)
  A=REJECT,  B=REJECT  → REJECT  (확실한 거부)
  둘 중 하나라도 ESCALATE → ESCALATE
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from second_opinion.second_opinion import SingleOpinion, Verdict
from second_opinion.lvector_reviewer import LVectorReviewer
from infrastructure.local_slm import LocalSLM

logger = logging.getLogger(__name__)


@dataclass
class LocalConsensus:
    """로컬 2차 소견 결과."""
    final_verdict       : Verdict
    consensus_score     : float
    slm_opinion         : Optional[SingleOpinion]
    lvector_opinion     : Optional[SingleOpinion]
    dissenting_concerns : list[str] = field(default_factory=list)
    requires_human      : bool = False
    explanation         : str = ""

    # DeCODE 분석 상세
    l_vector_before     : dict = field(default_factory=dict)
    l_vector_after      : dict = field(default_factory=dict)
    collapse_risk       : float = 0.0


class LocalSecondOpinion:
    """
    완전 로컬 2차 소견 엔진.

    사용 예시:
        engine = LocalSecondOpinion(
            slm      = LocalSLM(model_path="/models/mistral.gguf"),
            reviewer = LVectorReviewer(decode_root="/path/to/decode_final"),
            quorum   = 0.66,
        )
        result = await engine.review(
            case_summary    = "...",
            proposed_patch  = "diff --git ...",
            source_code     = original_code,
            patient_type    = "python_service",
            risk_level      = "MEDIUM",
        )
    """

    def __init__(
        self,
        slm          : LocalSLM,
        reviewer     : LVectorReviewer,
        quorum       : float = 0.66,
        # L-벡터 검증자에 더 높은 신뢰를 줌 (언어 편향 없음)
        lvector_weight: float = 1.4,
        slm_weight    : float = 1.0,
    ) -> None:
        self._slm           = slm
        self._reviewer      = reviewer
        self._quorum        = quorum
        self._lvector_weight = lvector_weight
        self._slm_weight     = slm_weight

        logger.info(
            f"[LocalSecondOpinion] 초기화 완료 | "
            f"SLM={'있음' if slm.is_slm_available else 'fallback'} "
            f"LVector={'있음' if reviewer.is_available else 'static'} "
            f"quorum={quorum}"
        )

    async def review(
        self,
        case_summary   : str,
        proposed_patch : str,
        source_code    : str = "",
        patient_type   : str = "python_service",
        risk_level     : str = "LOW",
        file_path      : str = "target.py",
        l_vector       : dict = None,
        risk_dims      : list[str] = None,
    ) -> LocalConsensus:
        """
        두 검증자가 병렬로 독립 검토한다.
        서로의 결과를 볼 수 없다.
        """
        l_vector  = l_vector  or {}
        risk_dims = risk_dims or []

        logger.info(
            f"[LocalSecondOpinion] 2차 소견 시작 | "
            f"patient={patient_type} risk={risk_level}"
        )

        # ── 두 검증자 병렬 실행 ──────────────────────────────────
        slm_task      = asyncio.get_event_loop().run_in_executor(
            None,
            self._slm.review_patch,
            case_summary, proposed_patch, l_vector, risk_dims,
        )
        lvector_task  = asyncio.get_event_loop().run_in_executor(
            None,
            self._reviewer.review,
            source_code, proposed_patch, file_path,
            self._estimate_current_risk(l_vector),
        )

        slm_raw, lv_raw = await asyncio.gather(slm_task, lvector_task)

        # ── 결과를 SingleOpinion으로 변환 ────────────────────────
        slm_opinion = SingleOpinion(
            model_id   = "local_slm" if self._slm.is_slm_available else "rule_based",
            verdict    = Verdict(slm_raw.get("verdict", "ESCALATE")),
            confidence = float(slm_raw.get("confidence", 0.5)),
            reasoning  = slm_raw.get("reasoning", ""),
            concerns   = slm_raw.get("concerns", []),
        )

        lv_opinion = SingleOpinion(
            model_id   = "lvector_decode",
            verdict    = Verdict(lv_raw.verdict),
            confidence = lv_raw.confidence,
            reasoning  = lv_raw.reasoning,
            concerns   = lv_raw.concerns,
        )

        # ── 판정 로직 ────────────────────────────────────────────
        result = self._compute_verdict(
            slm_opinion, lv_opinion, lv_raw, risk_level
        )

        logger.info(
            f"[LocalSecondOpinion] 판정 완료 | "
            f"verdict={result.final_verdict.value} "
            f"score={result.consensus_score:.2f} "
            f"SLM={slm_opinion.verdict.value} "
            f"LV={lv_opinion.verdict.value}"
        )

        return result

    # ── 내부 ──────────────────────────────────────────────────

    def _compute_verdict(
        self,
        slm_op    : SingleOpinion,
        lv_op     : SingleOpinion,
        lv_raw    : object,
        risk_level: str,
    ) -> LocalConsensus:
        """
        두 검증자 결과를 종합한다.

        핵심 규칙:
          - L-벡터가 REJECT하면 SLM이 APPROVE해도 최종 REJECT
            (수학적 구조 문제는 언어적 판단보다 우선)
          - 고위험 패치는 둘 다 APPROVE해야 통과
        """
        all_concerns = list(set(slm_op.concerns + lv_op.concerns))

        # ESCALATE 우선
        if slm_op.verdict == Verdict.ESCALATE or lv_op.verdict == Verdict.ESCALATE:
            return LocalConsensus(
                final_verdict       = Verdict.ESCALATE,
                consensus_score     = 0.3,
                slm_opinion         = slm_op,
                lvector_opinion     = lv_op,
                dissenting_concerns = all_concerns,
                requires_human      = True,
                explanation         = "검증자 중 하나 이상이 에스컬레이션 요청",
                l_vector_before     = getattr(lv_raw, "current_l_vector", {}),
                l_vector_after      = getattr(lv_raw, "patch_l_vector", {}),
                collapse_risk       = getattr(lv_raw, "collapse_risk", 0.0),
            )

        # 고위험: 둘 다 APPROVE 필요
        if risk_level == "HIGH":
            if slm_op.verdict == Verdict.APPROVE and lv_op.verdict == Verdict.APPROVE:
                verdict = Verdict.APPROVE
                score   = (slm_op.confidence * self._slm_weight +
                           lv_op.confidence * self._lvector_weight) / (
                           self._slm_weight + self._lvector_weight)
                expl = f"고위험 패치 — 두 검증자 모두 승인 (score={score:.2f})"
            else:
                verdict = Verdict.REJECT
                score   = 0.3
                dissenter = "L-벡터" if lv_op.verdict != Verdict.APPROVE else "SLM"
                expl = f"고위험 패치 — {dissenter} 거부"
        else:
            # 일반: 가중 투표
            approve_w = 0.0
            reject_w  = 0.0
            total_w   = self._slm_weight + self._lvector_weight

            for op, w in [(slm_op, self._slm_weight), (lv_op, self._lvector_weight)]:
                weighted = op.confidence * w
                if op.verdict == Verdict.APPROVE:
                    approve_w += weighted
                else:
                    reject_w  += weighted

            approve_ratio = approve_w / (
                (approve_w + reject_w) if (approve_w + reject_w) > 0 else 1
            )

            # L-벡터가 REJECT하면 SLM의 APPROVE를 무시
            if lv_op.verdict == Verdict.REJECT:
                verdict = Verdict.REJECT
                score   = lv_op.confidence
                expl = (
                    f"L-벡터 구조 분석 거부 (SLM 무효화) | "
                    f"이유: {lv_op.reasoning}"
                )
            elif approve_ratio >= self._quorum:
                verdict = Verdict.APPROVE
                score   = approve_ratio
                expl = f"가중 투표 승인: {approve_ratio:.0%} (기준 {self._quorum:.0%})"
            else:
                verdict = Verdict.REJECT
                score   = 1 - approve_ratio
                expl = f"가중 투표 거부: 반대 {1-approve_ratio:.0%}"

        return LocalConsensus(
            final_verdict       = verdict,
            consensus_score     = round(min(score, 1.0), 3),
            slm_opinion         = slm_op,
            lvector_opinion     = lv_op,
            dissenting_concerns = all_concerns,
            requires_human      = verdict == Verdict.ESCALATE,
            explanation         = expl,
            l_vector_before     = getattr(lv_raw, "current_l_vector", {}),
            l_vector_after      = getattr(lv_raw, "patch_l_vector", {}),
            collapse_risk       = getattr(lv_raw, "collapse_risk", 0.0),
        )

    @staticmethod
    def _estimate_current_risk(l_vector: dict) -> float:
        """L-벡터로 현재 위험도를 추정한다."""
        if not l_vector:
            return 0.0
        thresholds = {
            "RECURRENCE": 0.85, "DYNAMICS": 0.80,
            "INFORMATION": 0.75, "EMERGENCE": 0.70, "COMPOSITION": 0.85,
        }
        scores = []
        for dim, thr in thresholds.items():
            val = l_vector.get(dim, 0)
            scores.append(min(val / thr, 1.0) * val)
        return round(sum(scores) / len(scores), 3) if scores else 0.0
