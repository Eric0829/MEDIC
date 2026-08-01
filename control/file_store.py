"""
file_store.py
─────────────────────────────────────────────────────────────────────
MEDIC control-state 파일 저장 도우미.

ApprovalQueue, AuditLog, PipelineTrace는 여러 루프/CLI가 동시에 접근할 수
있으므로 같은 잠금 규칙과 원자적 rewrite 규칙을 공유한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class FileLockTimeout(TimeoutError):
    """Raised when a control-state file lock cannot be acquired."""


class FileLock:
    """Small cross-platform exclusive lock using a sibling .lock file."""

    def __init__(
        self,
        path: str | Path,
        timeout_seconds: float = 10.0,
        poll_seconds: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.timeout_seconds = max(0.1, float(timeout_seconds or 0.1))
        self.poll_seconds = max(0.01, float(poll_seconds or 0.01))
        self._fh: Optional[Any] = None
        self._locked = False

    def __enter__(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.lock_path.open("a+b")
        self._ensure_lock_byte()

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._lock_file()
                self._locked = True
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise FileLockTimeout(f"timed out waiting for {self.lock_path}") from exc
                time.sleep(self.poll_seconds)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._locked:
                self._unlock_file()
        finally:
            self._locked = False
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def _ensure_lock_byte(self) -> None:
        assert self._fh is not None
        self._fh.seek(0, os.SEEK_END)
        if self._fh.tell() == 0:
            self._fh.write(b"\0")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        self._fh.seek(0)

    def _lock_file(self) -> None:
        assert self._fh is not None
        self._fh.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(self) -> None:
        assert self._fh is not None
        self._fh.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)


def append_jsonl_locked(path: str | Path, payload: dict[str, Any]) -> None:
    """Append one JSON object under an exclusive file lock."""
    path = Path(path)
    with FileLock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def read_lines_locked(path: str | Path) -> list[str]:
    """Read all lines under the same lock used by writers."""
    path = Path(path)
    with FileLock(path):
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()


def read_text_locked(path: str | Path) -> str:
    """Read text under the same lock used by writers."""
    path = Path(path)
    with FileLock(path):
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")


def write_text_locked(path: str | Path, text: str) -> None:
    """Atomically replace a text file under an exclusive file lock."""
    path = Path(path)
    with FileLock(path):
        write_text_unlocked(path, text)


def write_text_unlocked(path: str | Path, text: str) -> None:
    """Atomically replace a text file while the caller already holds the lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def jsonl_health(path: str | Path, recent_limit: int = 10000) -> dict[str, Any]:
    """Return raw JSONL parse health without interpreting domain records."""
    path = Path(path)
    lines = read_lines_locked(path)
    recent = lines[-max(1, int(recent_limit or 1)):]
    invalid: list[dict[str, Any]] = []
    empty_lines = 0
    parseable = 0
    for index, line in enumerate(recent, start=max(1, len(lines) - len(recent) + 1)):
        if not line.strip():
            empty_lines += 1
            continue
        try:
            json.loads(line)
            parseable += 1
        except Exception as exc:
            invalid.append({
                "line": index,
                "error": str(exc),
                "preview": line[:160],
            })

    total_nonempty = sum(1 for line in lines if line.strip())
    size_bytes = path.stat().st_size if path.exists() else 0
    return {
        "path": str(path),
        "lock_path": str(path.with_name(f"{path.name}.lock")),
        "exists": path.exists(),
        "size_bytes": size_bytes,
        "total_lines": len(lines),
        "total_nonempty_lines": total_nonempty,
        "recent_lines_checked": len(recent),
        "parseable_recent_lines": parseable,
        "invalid_recent_lines": len(invalid),
        "empty_recent_lines": empty_lines,
        "invalid_examples": invalid[:5],
        "rotation_recommended": size_bytes > 50 * 1024 * 1024,
    }


def repair_jsonl_locked(path: str | Path, backup_dir: str | Path | None = None) -> dict[str, Any]:
    """Remove malformed JSONL lines after preserving the original file."""
    path = Path(path)
    backup_dir = Path(backup_dir) if backup_dir else path.parent / "repair_backups"
    with FileLock(path):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        valid_lines: list[str] = []
        invalid: list[dict[str, Any]] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                json.loads(line)
                valid_lines.append(line)
            except Exception as exc:
                invalid.append({
                    "line": index,
                    "error": str(exc),
                    "preview": line[:160],
                })

        backup_path = ""
        invalid_path = ""
        if invalid:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            suffix = uuid.uuid4().hex[:8]
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"{path.name}.{stamp}.{suffix}.bak"
            invalid_report = backup_dir / f"{path.name}.{stamp}.{suffix}.invalid.json"
            backup.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            invalid_report.write_text(
                json.dumps(invalid, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            write_text_unlocked(path, "\n".join(valid_lines) + ("\n" if valid_lines else ""))
            backup_path = str(backup)
            invalid_path = str(invalid_report)

    return {
        "path": str(path),
        "repaired": bool(invalid),
        "removed_lines": len(invalid),
        "backup_path": backup_path,
        "invalid_report_path": invalid_path,
        "invalid_examples": invalid[:5],
    }
