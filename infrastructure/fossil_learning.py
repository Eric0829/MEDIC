"""
fossil_learning.py
─────────────────────────────────────────────────────────────────────
MEDIC FossilStore 학습 루프 (옵션 B 메인).

uics_v31의 FossilStore 철학을 MEDIC 처방 패턴에 적용:
  "살아남는 패턴 = 진짜 불변 구조 (L)"
  "새 증거가 충분히 강할 때만 수정 → 항상성 + 가소성의 균형"

UICS → MEDIC 매핑:
  개념(concept)     → 처방 패턴(PrescriptionPattern)
  FossilRecord      → PrescriptionFossil
  challenge(survived) → 처방이 치료에 성공/실패했는가
  화석화(fossilized) → 이 처방 패턴은 SLM 없이 즉시 적용 가능

학습이 쌓이면 일어나는 일:
  1. 처방 패턴 신뢰도 상승
  2. fossil_hit_rate 상승 (FossilStore에서 즉시 처방 가능)
  3. IndependenceTracker의 independence_score 상승
  4. SLM 호출 없이 처리되는 케이스 증가

처방 패턴 키 구조:
  "{patient_type}:{severity}:{root_cause_hash}:{treatment_type}"
  예: "python_service:HIGH:cpu_overload:restart"
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── 처방 화석 ────────────────────────────────────────────────────────

@dataclass
class PrescriptionFossil:
    """
    검증된 처방 패턴 화석.

    이 화석이 존재한다는 것은:
    "이 상황(patient_type + severity + root_cause)에서
     이 치료(treatment_type)를 쓰면 성공한다는 것이 반복 검증됐다"
    는 의미다.
    """
    key             : str        # 패턴 키
    patient_type    : str
    severity        : str
    root_cause_hint : str        # 원인 키워드 (hash 전 원본)
    treatment_type  : str
    confidence      : float      # 신뢰도 (0~1)
    created_at      : float = field(default_factory=time.time)

    # 도전/생존 이력 (uics_v31 FossilRecord와 동일 구조)
    challenge_count : int   = 0
    survival_count  : int   = 0
    last_challenged : float = 0.0
    last_survived   : float = 0.0
    fossilized      : bool  = False

    FOSSILIZE_THRESHOLD : int   = 5    # N회 성공 생존 시 화석화
    CONF_CAP            : float = 0.95 # 신뢰도 상한

    def challenge(self, survived: bool) -> None:
        """성공/실패 1회 기록."""
        self.challenge_count += 1
        self.last_challenged = time.time()

        if survived:
            self.survival_count += 1
            self.last_survived = time.time()
            # 성공 시 신뢰도 상승 (점점 작게)
            delta = 0.05 * (1 - self.confidence)
            self.confidence = min(self.CONF_CAP, self.confidence + delta)
            # 충분히 생존 → 화석화
            if self.survival_count >= self.FOSSILIZE_THRESHOLD:
                self.fossilized = True
        else:
            # 실패 시 신뢰도 하락
            self.confidence = max(0.1, self.confidence - 0.08)
            if self.confidence < 0.5:
                self.fossilized = False  # 화석화 해제

    @property
    def stability(self) -> float:
        """안정성 지수 = 생존율 × 신뢰도."""
        if self.challenge_count == 0:
            return self.confidence
        return round(
            (self.survival_count / self.challenge_count) * self.confidence, 4
        )

    @property
    def survival_rate(self) -> float:
        if self.challenge_count == 0:
            return 0.0
        return round(self.survival_count / self.challenge_count, 3)

    def to_dict(self) -> dict:
        return {
            "key"             : self.key,
            "patient_type"    : self.patient_type,
            "severity"        : self.severity,
            "root_cause_hint" : self.root_cause_hint,
            "treatment_type"  : self.treatment_type,
            "confidence"      : self.confidence,
            "created_at"      : self.created_at,
            "challenge_count" : self.challenge_count,
            "survival_count"  : self.survival_count,
            "last_challenged" : self.last_challenged,
            "last_survived"   : self.last_survived,
            "fossilized"      : self.fossilized,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PrescriptionFossil":
        obj = cls(
            key             = d["key"],
            patient_type    = d["patient_type"],
            severity        = d["severity"],
            root_cause_hint = d["root_cause_hint"],
            treatment_type  = d["treatment_type"],
            confidence      = d["confidence"],
            created_at      = d.get("created_at", time.time()),
        )
        obj.challenge_count = d.get("challenge_count", 0)
        obj.survival_count  = d.get("survival_count", 0)
        obj.last_challenged = d.get("last_challenged", 0.0)
        obj.last_survived   = d.get("last_survived", 0.0)
        obj.fossilized      = d.get("fossilized", False)
        return obj


# ── 처방 패턴 FossilStore ────────────────────────────────────────────

class MedicFossilStore:
    """
    MEDIC 처방 패턴 저장소.

    치료 결과가 쌓일수록:
      1. 자주 성공하는 처방 패턴의 confidence가 올라간다
      2. FOSSILIZE_THRESHOLD 이상 성공하면 화석화된다
      3. 화석화된 패턴은 SLM 없이 즉시 처방 가능 (fossil_hit)
      4. IndependenceTracker의 fossil_hit_rate가 올라간다

    저장 경로: persist_path (JSON, 재시작 후에도 유지)
    """

    MIN_CONFIDENCE_TO_REGISTER = 0.6  # 이 이상일 때만 등록

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._fossils    : dict[str, PrescriptionFossil] = {}
        self._persist    : Optional[Path] = (
            Path(persist_path) if persist_path else None
        )
        if self._persist and self._persist.exists():
            self._load()
            logger.info(
                f"[FossilStore] 로드 완료 | "
                f"{len(self._fossils)}개 패턴 복원"
            )

    # ── 패턴 키 생성 ─────────────────────────────────────────────────

    @staticmethod
    def make_key(
        patient_type : str,
        severity     : str,
        root_cause   : str,
        treatment    : str,
    ) -> str:
        """
        처방 패턴의 고유 키를 생성한다.
        root_cause는 키워드 정규화 후 hash.
        """
        cause_norm = MedicFossilStore._normalize_cause(root_cause)
        cause_hash = hashlib.md5(cause_norm.encode()).hexdigest()[:8]
        return f"{patient_type}:{severity}:{cause_hash}:{treatment}"

    @staticmethod
    def _normalize_cause(root_cause: str) -> str:
        """원인 문자열 정규화 — 숫자/퍼센트 제거 후 핵심 키워드만 남김."""
        import re
        # 숫자/퍼센트/괄호 제거
        cleaned = re.sub(r"[\d.]+%?", "", root_cause)
        cleaned = re.sub(r"[()→←:,]", " ", cleaned)
        # 2자 이상 단어만
        words = [w for w in cleaned.split() if len(w) >= 2]
        return " ".join(sorted(set(words)))[:50]

    # ── 학습 (치료 결과 → 화석 업데이트) ───────────────────────────────

    def learn(
        self,
        patient_type : str,
        severity     : str,
        root_cause   : str,
        treatment    : str,
        success      : bool,
        confidence   : float = 0.7,
    ) -> PrescriptionFossil:
        """
        치료 결과 1건을 학습한다.

        성공 → 해당 패턴 신뢰도 상승
        실패 → 신뢰도 하락
        N회 성공 → 화석화 (SLM 없이 즉시 처방 가능)
        """
        key = self.make_key(patient_type, severity, root_cause, treatment)

        if key not in self._fossils:
            # 새 패턴 등록 (최소 신뢰도 이상일 때만)
            if confidence < self.MIN_CONFIDENCE_TO_REGISTER and not success:
                # 실패한 저신뢰 패턴은 등록 안 함
                return PrescriptionFossil(
                    key=key, patient_type=patient_type,
                    severity=severity, root_cause_hint=root_cause[:40],
                    treatment_type=treatment, confidence=0.0
                )

            self._fossils[key] = PrescriptionFossil(
                key             = key,
                patient_type    = patient_type,
                severity        = severity,
                root_cause_hint = root_cause[:40],
                treatment_type  = treatment,
                confidence      = confidence,
            )
            logger.debug(f"[FossilStore] 신규 패턴 등록: {key}")

        fossil = self._fossils[key]
        old_fossilized = fossil.fossilized

        fossil.challenge(survived=success)

        if fossil.fossilized and not old_fossilized:
            logger.info(
                f"[FossilStore] 🪨 화석화 완료 | "
                f"pattern={key} "
                f"survival={fossil.survival_count}/{fossil.challenge_count} "
                f"confidence={fossil.confidence:.2f}"
            )

        if self._persist:
            self._save()

        return fossil

    # ── 처방 조회 (fossil_hit 경로) ────────────────────────────────────

    def lookup(
        self,
        patient_type : str,
        severity     : str,
        root_cause   : str,
        min_confidence: float = 0.70,  # 화석화된 패턴은 0.70 이상이면 충분
    ) -> Optional[PrescriptionFossil]:
        """
        주어진 상황에 맞는 화석화된 처방 패턴을 찾는다.

        반환값이 있으면 → fossil_hit (SLM 불필요)
        반환값이 없으면 → SLM 또는 규칙 기반으로 처방

        min_confidence: 이 이상의 신뢰도를 가진 패턴만 반환
        """
        cause_norm = self._normalize_cause(root_cause)
        cause_hash = hashlib.md5(cause_norm.encode()).hexdigest()[:8]
        prefix     = f"{patient_type}:{severity}:{cause_hash}:"

        candidates = [
            f for k, f in self._fossils.items()
            if k.startswith(prefix)
            and f.confidence >= min_confidence
            and f.fossilized
        ]

        # exact hash miss → keyword fuzzy 검색
        if not candidates:
            cause_words = set(cause_norm.split())
            candidates = [
                f for f in self._fossils.values()
                if f.patient_type == patient_type
                and f.severity == severity
                and f.confidence >= min_confidence
                and f.fossilized
                and cause_words & set(
                    self._normalize_cause(f.root_cause_hint).split()
                )
            ]

        if not candidates:
            return None

        # 안정성 점수 높은 것 반환
        return max(candidates, key=lambda f: f.stability)

    def lookup_any(
        self,
        patient_type : str,
        severity     : str,
        root_cause   : str,
        min_confidence: float = 0.65,
    ) -> Optional[PrescriptionFossil]:
        """
        화석화되지 않아도 신뢰도 기준 이상이면 반환.
        (화석화 전 단계에서도 참고용으로 사용)
        """
        cause_norm = self._normalize_cause(root_cause)
        cause_hash = hashlib.md5(cause_norm.encode()).hexdigest()[:8]
        prefix     = f"{patient_type}:{severity}:{cause_hash}:"

        candidates = [
            f for k, f in self._fossils.items()
            if k.startswith(prefix)
            and f.confidence >= min_confidence
        ]

        if not candidates:
            return None
        return max(candidates, key=lambda f: f.stability)

    # ── 통계 ────────────────────────────────────────────────────────

    def stats(self) -> dict:
        total      = len(self._fossils)
        fossilized = sum(1 for f in self._fossils.values() if f.fossilized)
        challenged = sum(1 for f in self._fossils.values() if f.challenge_count > 0)
        avg_conf   = (
            sum(f.confidence for f in self._fossils.values()) / total
            if total else 0.0
        )
        avg_stability = (
            sum(f.stability for f in self._fossils.values()) / total
            if total else 0.0
        )
        by_type = {}
        for f in self._fossils.values():
            by_type.setdefault(f.patient_type, {"total": 0, "fossilized": 0})
            by_type[f.patient_type]["total"] += 1
            if f.fossilized:
                by_type[f.patient_type]["fossilized"] += 1

        return {
            "total"        : total,
            "fossilized"   : fossilized,
            "challenged"   : challenged,
            "avg_confidence": round(avg_conf, 3),
            "avg_stability" : round(avg_stability, 3),
            "by_patient_type": by_type,
        }

    def top_fossils(self, n: int = 5) -> list[PrescriptionFossil]:
        """안정성 높은 상위 N개 화석."""
        return sorted(
            self._fossils.values(),
            key=lambda f: -f.stability
        )[:n]

    def render(self) -> str:
        s   = self.stats()
        ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "",
            f"  +-- MEDIC FossilStore ({ts}) ------------------------",
            f"  |  total={s['total']}  fossilized={s['fossilized']}  challenged={s['challenged']}",
            f"  |  avg_confidence={s['avg_confidence']:.2f}  avg_stability={s['avg_stability']:.2f}",
            f"  |",
            f"  |  Top patterns",
        ]
        for f in self.top_fossils(8):
            filled = min(10, int(f.stability * 10))
            bar = "#" * filled + "." * (10 - filled)
            icon = "[F]" if f.fossilized else "[ ]"
            lines.append(
                f"  |  {icon} [{bar}] "
                f"{f.patient_type:<16} {f.severity:<8} "
                f"-> {f.treatment_type:<20} "
                f"({f.survival_count}/{f.challenge_count})"
            )
        if s['by_patient_type']:
            lines.append("  |")
            lines.append("  |  By patient type")
            for ptype, cnt in s['by_patient_type'].items():
                lines.append(
                    f"  |    {ptype:<20} "
                    f"{cnt['fossilized']}/{cnt['total']} fossilized"
                )
        lines.append("  +-------------------------------------------------")
        return "\n".join(lines)

    # ── 영속화 ───────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            data = {k: v.to_dict() for k, v in self._fossils.items()}
            self._persist.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[FossilStore] 저장 실패: {exc}")

    def _load(self) -> None:
        try:
            raw = self._persist.read_text(encoding="utf-8").strip()
            if not raw:
                return
            data = json.loads(raw)
            for k, v in data.items():
                self._fossils[k] = PrescriptionFossil.from_dict(v)
        except Exception as exc:
            logger.warning(f"[FossilStore] 로드 실패: {exc}")
