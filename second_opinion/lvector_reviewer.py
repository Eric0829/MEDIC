"""
lvector_reviewer.py
─────────────────────────────────────────────────────────────────────
L-벡터 기반 독립 검증기.

이것이 진짜 독립적인 2차 소견의 핵심이다.

LLM은 언어 패턴을 학습했기 때문에
같은 계열의 모델끼리는 같은 방식으로 틀린다.

L-벡터 분석은 LLM이 아니다.
코드 구조를 7개 수학적 차원(RECURRENCE, STABILITY, COMPOSITION,
DYNAMICS, CONSTRAINT, INFORMATION, EMERGENCE)으로 분해하는
결정론적 분석이다.

같은 입력 → 항상 같은 결과.
언어적 편향 없음.
외부 의존 없음.

역할:
  SLM이 처방을 제안하면
  L-벡터 검증기가 구조적으로 안전한지 독립 검토한다.
  두 결과가 일치해야 처방이 통과된다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# L-벡터 차원 임계값 (DeCODE debt_scanner 기준)
_THRESHOLDS = {
    "RECURRENCE" : 0.85,
    "DYNAMICS"   : 0.80,
    "INFORMATION": 0.75,
    "EMERGENCE"  : 0.70,
    "COMPOSITION": 0.85,
}

_COLLAPSE_SCORE = 0.72


@dataclass
class LVectorVerdict:
    """L-벡터 검증 결과."""
    verdict          : str          # APPROVE / REJECT / ESCALATE
    confidence       : float
    reasoning        : str
    concerns         : list[str] = field(default_factory=list)

    # 상세 분석
    patch_l_vector   : dict = field(default_factory=dict)   # 패치 후 예측 L벡터
    current_l_vector : dict = field(default_factory=dict)   # 현재 L벡터
    risk_delta       : dict = field(default_factory=dict)   # 차원별 위험도 변화
    collapse_risk    : float = 0.0


class LVectorReviewer:
    """
    DeCODE의 DebtScanner + SelfRepairGuard를 사용하는
    L-벡터 기반 독립 검증기.

    사용 예시:
        reviewer = LVectorReviewer(decode_root="/path/to/decode_final")
        verdict = reviewer.review(
            source_code   = original_code,
            proposed_patch= diff_patch,
            file_path     = "brain.py",
        )
    """

    def __init__(
        self,
        decode_root : Optional[str] = None,
        source_root : Optional[str] = None,
    ) -> None:
        self._decode_root = decode_root
        self._source_root = source_root
        self._scanner     = None
        self._guard       = None

        self._load_decode(decode_root, source_root)

    @property
    def is_available(self) -> bool:
        return self._scanner is not None

    def review(
        self,
        source_code   : str,
        proposed_patch: str,
        file_path     : str = "target.py",
        current_risk  : float = 0.0,
    ) -> LVectorVerdict:
        """
        패치를 L-벡터로 검증한다.

        1. 현재 코드의 L-벡터 계산
        2. 패치 적용 후 코드의 L-벡터 계산
        3. 위험도 변화 분석
        4. SelfRepairGuard 붕괴 게이트 통과 여부 확인
        """
        if not self._scanner:
            # DeCODE 없으면 정적 패치 분석만
            return self._static_patch_review(proposed_patch, current_risk)

        try:
            return self._full_lvector_review(
                source_code, proposed_patch, file_path, current_risk
            )
        except Exception as exc:
            logger.warning(f"[LVectorReviewer] L-벡터 검증 실패: {exc}")
            return self._static_patch_review(proposed_patch, current_risk)

    def analyze_code(self, code: str) -> dict:
        """
        코드 한 조각의 L-벡터를 직접 계산해 반환한다.
        환자 vitals 수집 시 호출.
        """
        if not self._scanner:
            return {}
        try:
            report = self._scanner.scan_code_string(code)
            if report:
                return report.l_vector
        except Exception:
            pass
        return {}

    # ── 내부 ──────────────────────────────────────────────────

    def _load_decode(
        self,
        decode_root : Optional[str],
        source_root : Optional[str],
    ) -> None:
        """DeCODE debt_scanner / self_repair 로드."""
        search_paths = []
        if decode_root:
            search_paths.append(str(Path(decode_root)))
        # 기본 위치 탐색
        for p in ["../../decode_final", "../decode_final", "./decode_final"]:
            search_paths.append(p)

        for base in search_paths:
            if not Path(base).exists():
                continue
            try:
                decode_path = str(Path(base) / "decode")
                uics_path   = str(Path(base) / "uics")

                for p in [base, decode_path, uics_path]:
                    if p not in sys.path:
                        sys.path.insert(0, p)

                # debt_scanner는 exec로 로드 (decode_final 방식)
                scanner_path = Path(base) / "debt_scanner.py"
                if scanner_path.exists():
                    ns = {}
                    exec(scanner_path.read_text(encoding="utf-8"), ns)
                    self._scanner = ns.get("DebtScanner")()
                    logger.info(f"[LVectorReviewer] DebtScanner 로드: {base}")

                # SelfRepairGuard
                repair_path = Path(base) / "self_repair.py"
                if repair_path.exists() and source_root:
                    ns2 = {}
                    # diff_store도 필요
                    diff_path = Path(base) / "diff_store.py"
                    if diff_path.exists():
                        exec(diff_path.read_text(encoding="utf-8"), ns2)
                    exec(scanner_path.read_text(encoding="utf-8"), ns2)
                    exec(repair_path.read_text(encoding="utf-8"), ns2)
                    guard_cls = ns2.get("SelfRepairGuard")
                    if guard_cls and source_root:
                        self._guard = guard_cls(source_root)
                        logger.info("[LVectorReviewer] SelfRepairGuard 로드 완료")

                if self._scanner:
                    return

            except Exception as exc:
                logger.debug(f"[LVectorReviewer] {base} 로드 실패: {exc}")
                continue

        logger.info("[LVectorReviewer] DeCODE 없음 — 정적 분석만 사용")

    def _full_lvector_review(
        self,
        source_code   : str,
        proposed_patch: str,
        file_path     : str,
        current_risk  : float,
    ) -> LVectorVerdict:
        """DeCODE가 있을 때 전체 L-벡터 검증 수행."""
        concerns = []

        # 1. 현재 코드 L-벡터
        cur_report = self._scan_code(source_code)

        cur_lv = cur_report.l_vector if cur_report else {}
        cur_risk = cur_report.risk_score if cur_report else current_risk

        # 2. 패치 적용 후 코드 생성
        patched_code = self._apply_patch_to_string(source_code, proposed_patch)

        # 3. 패치 후 L-벡터
        pat_report = self._scan_code(patched_code) if patched_code else None

        pat_lv   = pat_report.l_vector if pat_report else {}
        pat_risk = pat_report.risk_score if pat_report else current_risk

        # 4. 위험도 변화 분석
        risk_delta = {}
        for dim in _THRESHOLDS:
            before = cur_lv.get(dim, 0)
            after  = pat_lv.get(dim, before)
            delta  = after - before
            risk_delta[dim] = delta
            if after >= _THRESHOLDS[dim] and delta > 0:
                concerns.append(
                    f"{dim} 위험 임계 초과: {before:.2f} → {after:.2f} "
                    f"(임계값: {_THRESHOLDS[dim]})"
                )

        # 5. 전체 붕괴 위험도
        collapse_increased = pat_risk > cur_risk + 0.05
        if pat_risk >= _COLLAPSE_SCORE:
            concerns.append(f"붕괴점 도달: 전체 위험도 {pat_risk:.2f}")

        # 6. SelfRepairGuard 게이트 (있는 경우)
        guard_blocked = False
        if self._guard and hasattr(self._guard, "before_modify"):
            try:
                check = self._guard.scanner.check_modification(
                    file_path, patched_code,
                    self._guard._scan() if hasattr(self._guard, "_scan") else None
                )
                if check and check.get("verdict") in ("DANGER", "COLLAPSE"):
                    concerns.append(
                        f"SelfRepairGuard 차단: verdict={check.get('verdict')}"
                    )
                    guard_blocked = True
            except Exception:
                pass

        # ── 최종 판정 ──────────────────────────────────────────
        critical_concerns = [c for c in concerns if "붕괴" in c or "COLLAPSE" in c]

        if guard_blocked or critical_concerns:
            verdict    = "REJECT"
            confidence = 0.85
            reasoning  = f"L-벡터 구조 분석 거부: {concerns[0] if concerns else '위험 증가'}"

        elif concerns:
            # 우려는 있지만 치명적이지 않음
            if pat_risk > cur_risk + 0.1:
                verdict    = "REJECT"
                confidence = 0.75
                reasoning  = f"위험도 증가: {cur_risk:.2f} → {pat_risk:.2f}"
            else:
                verdict    = "APPROVE"
                confidence = 0.65
                reasoning  = f"경미한 위험 증가 허용: {len(concerns)}개 우려"

        else:
            verdict    = "APPROVE"
            confidence = 0.90
            reasoning  = f"L-벡터 구조 검증 통과: 위험도 {cur_risk:.2f} → {pat_risk:.2f}"

        return LVectorVerdict(
            verdict          = verdict,
            confidence       = confidence,
            reasoning        = reasoning,
            concerns         = concerns,
            patch_l_vector   = pat_lv,
            current_l_vector = cur_lv,
            risk_delta       = risk_delta,
            collapse_risk    = pat_risk,
        )

    def _static_patch_review(
        self,
        proposed_patch: str,
        current_risk  : float,
    ) -> LVectorVerdict:
        """DeCODE 없을 때 패치 텍스트 정적 분석."""
        concerns = []
        lines    = proposed_patch.split("\n")
        added    = [l[1:] for l in lines if l.startswith("+") and not l.startswith("+++")]
        removed  = [l[1:] for l in lines if l.startswith("-") and not l.startswith("---")]

        # 패치 규모
        if len(added) > 50:
            concerns.append(f"추가 라인 과다: {len(added)}줄")
        if len(removed) > 30:
            concerns.append(f"삭제 라인 과다: {len(removed)}줄")

        # 위험 패턴
        added_code = "\n".join(added)
        _DANGER = [
            ("os.system",   "DYNAMICS"),
            ("subprocess",  "DYNAMICS"),
            ("eval(",       "EMERGENCE"),
            ("exec(",       "EMERGENCE"),
            ("__import__",  "COMPOSITION"),
            ("globals()",   "EMERGENCE"),
            ("open(",       "INFORMATION"),
        ]
        for pattern, dim in _DANGER:
            if pattern in added_code:
                concerns.append(f"위험 패턴 [{dim}]: {pattern}")

        # 재귀 탐지 (RECURRENCE)
        import re
        func_names = re.findall(r"def (\w+)", added_code)
        for fn in func_names:
            if fn in added_code.replace(f"def {fn}", ""):
                concerns.append(f"재귀 가능성 [RECURRENCE]: {fn}")
                break

        if not concerns:
            return LVectorVerdict(
                verdict    = "APPROVE",
                confidence = 0.60,
                reasoning  = "정적 패치 분석 — 명시적 위험 없음",
                collapse_risk = current_risk,
            )

        critical = any("EMERGENCE" in c or "DYNAMICS" in c for c in concerns)
        return LVectorVerdict(
            verdict    = "REJECT" if critical else "APPROVE",
            confidence = 0.70,
            reasoning  = f"정적 분석 {'거부' if critical else '승인'}: {concerns[0]}",
            concerns   = concerns,
            collapse_risk = current_risk,
        )

    def _scan_code(self, code: str):
        """코드 문자열을 임시 파일로 scan_file 호출."""
        if not self._scanner or not code.strip():
            return None
        import tempfile, os
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as f:
                f.write(code)
                tmp = f.name
            result = self._scanner.scan_file(tmp)
            os.unlink(tmp)
            return result
        except Exception:
            return None

    @staticmethod
    def _apply_patch_to_string(source: str, patch: str) -> str:
        """
        unified diff 패치를 코드 문자열에 적용.
        실패하면 원본 반환.
        """
        import tempfile
        if not patch.strip():
            return source

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False, encoding="utf-8"
            ) as sf:
                sf.write(source)
                src_path = sf.name

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".patch", delete=False, encoding="utf-8"
            ) as pf:
                pf.write(patch)
                patch_path = pf.name

            if shutil.which("patch"):
                result = subprocess.run(
                    ["patch", "--dry-run", "-o", "-", src_path, patch_path],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    return result.stdout

            if shutil.which("git"):
                original = Path(src_path).read_text(encoding="utf-8")
                check = subprocess.run(
                    ["git", "apply", "--check", patch_path],
                    cwd=str(Path(src_path).parent),
                    capture_output=True, text=True, timeout=10
                )
                if check.returncode == 0:
                    apply_result = subprocess.run(
                        ["git", "apply", patch_path],
                        cwd=str(Path(src_path).parent),
                        capture_output=True, text=True, timeout=10
                    )
                    if apply_result.returncode == 0:
                        return Path(src_path).read_text(encoding="utf-8")
                Path(src_path).write_text(original, encoding="utf-8")
        except Exception:
            pass
        finally:
            for tmp_file in [locals().get("src_path"), locals().get("patch_path")]:
                if tmp_file and os.path.exists(tmp_file):
                    try:
                        os.unlink(tmp_file)
                    except Exception:
                        pass
        return source
