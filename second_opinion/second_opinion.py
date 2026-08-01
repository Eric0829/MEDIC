"""
second_opinion.py
─────────────────────────────────────────────────────────────────────
2차 소견 공통 타입 정의.

외부 API 없음. 타입/열거형만 정의한다.
실제 검증 로직:
  local_second_opinion.py  — SLM + L-벡터 이중 패널
  lvector_reviewer.py      — 수학적 구조 분석
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    APPROVE  = "APPROVE"
    REJECT   = "REJECT"
    ESCALATE = "ESCALATE"


@dataclass
class SingleOpinion:
    """검증자 한 명의 단일 소견."""
    model_id        : str
    verdict         : Verdict
    confidence      : float
    reasoning       : str
    suggested_patch : Optional[str] = None
    concerns        : list[str] = field(default_factory=list)


@dataclass
class ConsensusResult:
    """다수결 결과."""
    final_verdict       : Verdict
    consensus_score     : float
    opinions            : list[SingleOpinion]
    dissenting_concerns : list[str]
    requires_human      : bool = False
    explanation         : str  = ""
