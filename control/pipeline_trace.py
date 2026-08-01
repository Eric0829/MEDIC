"""
pipeline_trace.py
─────────────────────────────────────────────────────────────────────
MEDIC 파이프라인 추적 로그.

AuditLog가 "누가 무엇을 승인/차단했는가"를 남긴다면,
PipelineTrace는 "한 처방이 어떤 단계들을 거쳐 어떤 결과가 됐는가"를
trace_id로 묶어 남긴다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.file_store import append_jsonl_locked, jsonl_health, read_lines_locked


@dataclass
class TraceEvent:
    """파이프라인 단계 이벤트."""
    trace_id: str
    stage: str
    status: str
    message: str
    patient_id: str = ""
    prescription_id: str = ""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "patient_id": self.patient_id,
            "prescription_id": self.prescription_id,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceEvent":
        return cls(**data)


class PipelineTrace:
    """Append-only JSONL trace store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @staticmethod
    def new_trace_id() -> str:
        return str(uuid.uuid4())

    def record(
        self,
        trace_id: str,
        stage: str,
        status: str,
        message: str,
        patient_id: str = "",
        prescription_id: str = "",
        started_at: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> TraceEvent:
        duration_ms = 0.0
        if started_at is not None:
            duration_ms = round((time.monotonic() - started_at) * 1000, 3)
        event = TraceEvent(
            trace_id=trace_id,
            stage=stage,
            status=status,
            message=message,
            patient_id=patient_id,
            prescription_id=prescription_id,
            duration_ms=duration_ms,
            context=context or {},
        )
        append_jsonl_locked(self.path, event.to_dict())
        return event

    def events(self, trace_id: str) -> list[TraceEvent]:
        return [event for event in self.tail(limit=10000) if event.trace_id == trace_id]

    def tail(self, limit: int = 100) -> list[TraceEvent]:
        lines = read_lines_locked(self.path)[-limit:]
        return self._events_from_lines(lines)

    def stats(self) -> dict[str, Any]:
        lines = read_lines_locked(self.path)
        events = self._events_from_lines(lines[-10000:])
        total_events = sum(1 for line in lines if line.strip())
        by_stage: dict[str, int] = {}
        by_status: dict[str, int] = {}
        traces = set()
        for event in events:
            traces.add(event.trace_id)
            by_stage[event.stage] = by_stage.get(event.stage, 0) + 1
            by_status[event.status] = by_status.get(event.status, 0) + 1
        return {
            "path": str(self.path),
            "events_seen": total_events,
            "recent_events_seen": len(events),
            "traces_seen": len(traces),
            "by_stage": by_stage,
            "by_status": by_status,
            "latest_stage": events[-1].stage if events else "",
            "latest_status": events[-1].status if events else "",
        }

    def health(self) -> dict[str, Any]:
        return jsonl_health(self.path)

    @staticmethod
    def _events_from_lines(lines: list[str]) -> list[TraceEvent]:
        events: list[TraceEvent] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                events.append(TraceEvent.from_dict(json.loads(line)))
            except Exception:
                continue
        return events
