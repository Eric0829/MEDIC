"""
direct_call_detector.py
─────────────────────────────────────────────────────────────────────
apply_treatment 직접 호출 탐지기.

프로덕션 경로에서는 ControlledTreatmentRunner와 ControlledPatientProxy
외부에서 apply_treatment()를 호출하면 안 된다. 이 탐지기는 AST로
직접 호출 지점을 찾아 self-control report에 올린다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


class DirectTreatmentCallDetector:
    """Find direct .apply_treatment(...) call sites."""

    ALLOWED_SUFFIXES = {
        str(Path("control") / "treatment_runner.py"),
        str(Path("control") / "patient_proxy.py"),
    }

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def scan(self) -> dict[str, Any]:
        call_sites: list[dict[str, Any]] = []
        for path in sorted(self.root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rel = str(path.relative_to(self.root))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "apply_treatment":
                    allowed = self._is_allowed(rel)
                    call_sites.append({
                        "file": rel,
                        "line": int(getattr(node, "lineno", 0) or 0),
                        "allowed": allowed,
                    })

        unprotected = [site for site in call_sites if not site["allowed"]]
        return {
            "total_call_sites": len(call_sites),
            "allowed_call_sites": len(call_sites) - len(unprotected),
            "unprotected_call_sites": len(unprotected),
            "call_sites": call_sites,
        }

    def _is_allowed(self, rel_path: str) -> bool:
        normalized = rel_path.replace("\\", "/")
        allowed = {suffix.replace("\\", "/") for suffix in self.ALLOWED_SUFFIXES}
        return normalized in allowed
