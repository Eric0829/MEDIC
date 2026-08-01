"""
audit_log.py
─────────────────────────────────────────────────────────────────────
MEDIC 감사 로그.

컨트롤 레이어가 내린 정책 판정, 승인 큐 제출, 처방 적용 결과를
JSONL 이벤트로 남긴다. 기록은 실행보다 먼저 있어야 한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.file_store import append_jsonl_locked, jsonl_health, read_lines_locked


@dataclass
class AuditEvent:
    """감사 이벤트."""
    event_id: str
    event_type: str
    actor: str
    patient_id: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "patient_id": self.patient_id,
            "message": self.message,
            "timestamp": self.timestamp,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEvent":
        return cls(**data)


class AuditLog:
    """Append-only JSONL audit log."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(
        self,
        event_type: str,
        actor: str,
        patient_id: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            actor=actor,
            patient_id=patient_id,
            message=message,
            context=context or {},
        )
        append_jsonl_locked(self.path, event.to_dict())
        return event

    def tail(self, limit: int = 50) -> list[AuditEvent]:
        lines = read_lines_locked(self.path)[-limit:]
        events: list[AuditEvent] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                events.append(AuditEvent.from_dict(json.loads(line)))
            except Exception:
                continue
        return events

    def stats(self) -> dict[str, Any]:
        lines = read_lines_locked(self.path)
        events = self._events_from_lines(lines[-10000:])
        total_events = sum(1 for line in lines if line.strip())
        by_type: dict[str, int] = {}
        for event in events:
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
        return {
            "path": str(self.path),
            "events_seen": total_events,
            "recent_events_seen": len(events),
            "by_type": by_type,
            "latest_event_type": events[-1].event_type if events else "",
        }

    def health(self) -> dict[str, Any]:
        return jsonl_health(self.path)

    @staticmethod
    def _events_from_lines(lines: list[str]) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                events.append(AuditEvent.from_dict(json.loads(line)))
            except Exception:
                continue
        return events
