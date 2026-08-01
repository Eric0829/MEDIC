"""
storage_health.py
─────────────────────────────────────────────────────────────────────
MEDIC control-state 저장소 점검.

파일 기반 운영에서는 로그 자체가 제어층의 증거가 된다. 이 모듈은
approval/audit/trace/incident JSONL 파일이 파싱 가능한지, 잠금 파일 경로가
준비됐는지, 회전이 필요한 크기인지 점검한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from control.file_store import jsonl_health, repair_jsonl_locked


class ControlStorageHealth:
    """Inspect raw control-state JSONL storage."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.state_dir = self.root / "control_state"

    def inspect(self) -> dict[str, Any]:
        stores = {
            "approval_queue": jsonl_health(self.state_dir / "approval_queue.jsonl"),
            "audit": jsonl_health(self.state_dir / "audit.jsonl"),
            "trace": jsonl_health(self.state_dir / "pipeline_trace.jsonl"),
            "incident": jsonl_health(self.state_dir / "incident_cases.jsonl"),
        }
        invalid = sum(int(store.get("invalid_recent_lines", 0) or 0) for store in stores.values())
        rotation = [
            name for name, store in stores.items()
            if bool(store.get("rotation_recommended"))
        ]
        status = "healthy"
        if invalid:
            status = "blocked"
        elif rotation:
            status = "warning"

        return {
            "status": status,
            "state_dir": str(self.state_dir),
            "stores": stores,
            "invalid_recent_lines": invalid,
            "rotation_recommended": rotation,
            "lock_files": {
                name: store.get("lock_path", "")
                for name, store in stores.items()
            },
        }

    def repair(self) -> dict[str, Any]:
        backup_dir = self.state_dir / "repair_backups"
        repairs = {
            "approval_queue": repair_jsonl_locked(
                self.state_dir / "approval_queue.jsonl",
                backup_dir=backup_dir,
            ),
            "audit": repair_jsonl_locked(
                self.state_dir / "audit.jsonl",
                backup_dir=backup_dir,
            ),
            "trace": repair_jsonl_locked(
                self.state_dir / "pipeline_trace.jsonl",
                backup_dir=backup_dir,
            ),
            "incident": repair_jsonl_locked(
                self.state_dir / "incident_cases.jsonl",
                backup_dir=backup_dir,
            ),
        }
        removed = sum(int(row.get("removed_lines", 0) or 0) for row in repairs.values())
        return {
            "status": "repaired" if removed else "unchanged",
            "removed_lines": removed,
            "backup_dir": str(backup_dir),
            "repairs": repairs,
            "after": self.inspect(),
        }
