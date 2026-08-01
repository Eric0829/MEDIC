"""
approval_queue.py
─────────────────────────────────────────────────────────────────────
MEDIC 승인 큐.

고위험 처방은 바로 실행하지 않고 JSONL 큐에 기록한다. 이 모듈은
파일 기반이라 다른 UI나 CLI가 같은 큐를 읽어 승인/거부할 수 있다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from control.file_store import (
    FileLock,
    append_jsonl_locked,
    jsonl_health,
    write_text_unlocked,
)


@dataclass
class ApprovalRequest:
    """사람 승인이 필요한 요청."""
    request_id: str
    patient_id: str
    treatment_type: str
    reason: str
    status: str = "pending"
    risk_level: str = "UNKNOWN"
    prescription_id: str = ""
    trace_id: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_at: Optional[str] = None
    decided_by: str = ""
    decision_note: str = ""
    payload_summary: dict[str, Any] = field(default_factory=dict)
    prescription: dict[str, Any] = field(default_factory=dict)
    executed_at: Optional[str] = None
    executed_by: str = ""
    execution_note: str = ""
    execution_result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "patient_id": self.patient_id,
            "treatment_type": self.treatment_type,
            "reason": self.reason,
            "status": self.status,
            "risk_level": self.risk_level,
            "prescription_id": self.prescription_id,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
            "payload_summary": self.payload_summary,
            "prescription": self.prescription,
            "executed_at": self.executed_at,
            "executed_by": self.executed_by,
            "execution_note": self.execution_note,
            "execution_result": self.execution_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRequest":
        allowed = {
            "request_id", "patient_id", "treatment_type", "reason", "status",
            "risk_level", "prescription_id", "created_at", "decided_at",
            "decided_by", "decision_note", "payload_summary", "prescription",
            "trace_id", "executed_at", "executed_by", "execution_note",
            "execution_result",
        }
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)


class ApprovalQueue:
    """Append-only approval queue with status updates by rewrite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def submit(
        self,
        prescription: Any,
        reason: str,
        trace_id: str = "",
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            patient_id=str(getattr(prescription, "patient_id", "")),
            treatment_type=self._treatment_type(prescription),
            reason=reason,
            risk_level=str(getattr(prescription, "risk_level", "UNKNOWN")),
            prescription_id=str(getattr(prescription, "prescription_id", "")),
            trace_id=trace_id,
            payload_summary=self._summarize_payload(getattr(prescription, "payload", {}) or {}),
            prescription=self._prescription_dict(prescription),
        )
        append_jsonl_locked(self.path, req.to_dict())
        return req

    def list(self, status: Optional[str] = None) -> list[ApprovalRequest]:
        rows = self._read_all()
        if status:
            rows = [r for r in rows if r.status == status]
        return rows

    def get(self, request_id: str) -> ApprovalRequest:
        for row in self._read_all():
            if row.request_id == request_id:
                return row
        raise KeyError(request_id)

    def decide(
        self,
        request_id: str,
        status: str,
        decided_by: str = "human",
        note: str = "",
    ) -> ApprovalRequest:
        if status not in {"approved", "rejected"}:
            raise ValueError("status must be approved or rejected")
        with FileLock(self.path):
            rows = self._read_all_unlocked()
            found: Optional[ApprovalRequest] = None
            for row in rows:
                if row.request_id == request_id:
                    if row.status != "pending":
                        raise ValueError(f"request is already {row.status}")
                    row.status = status
                    row.decided_at = datetime.now(timezone.utc).isoformat()
                    row.decided_by = decided_by
                    row.decision_note = note
                    found = row
                    break
            if found is None:
                raise KeyError(request_id)
            self._write_all_unlocked(rows)
            return found

    def mark_execution(
        self,
        request_id: str,
        success: bool,
        executed_by: str = "medic.approval_executor",
        note: str = "",
        result: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        with FileLock(self.path):
            rows = self._read_all_unlocked()
            found: Optional[ApprovalRequest] = None
            for row in rows:
                if row.request_id == request_id:
                    if row.status != "approved":
                        raise ValueError(f"request is {row.status}, not approved")
                    row.status = "executed" if success else "execution_failed"
                    row.executed_at = datetime.now(timezone.utc).isoformat()
                    row.executed_by = executed_by
                    row.execution_note = note
                    row.execution_result = result or {}
                    found = row
                    break
            if found is None:
                raise KeyError(request_id)
            self._write_all_unlocked(rows)
            return found

    def stats(self) -> dict[str, Any]:
        rows = self._read_all()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        trace_linked = sum(1 for row in rows if row.trace_id)
        active_unlinked = sum(
            1
            for row in rows
            if row.status in {"pending", "approved"} and not row.trace_id
        )
        return {
            "path": str(self.path),
            "total": len(rows),
            "pending": counts.get("pending", 0),
            "approved": counts.get("approved", 0),
            "rejected": counts.get("rejected", 0),
            "executed": counts.get("executed", 0),
            "execution_failed": counts.get("execution_failed", 0),
            "by_status": counts,
            "trace_linked": trace_linked,
            "trace_unlinked": len(rows) - trace_linked,
            "active_unlinked_trace_id": active_unlinked,
            "latest_request_id": rows[-1].request_id if rows else "",
        }

    def _read_all(self) -> list[ApprovalRequest]:
        with FileLock(self.path):
            return self._read_all_unlocked()

    def _read_all_unlocked(self) -> list[ApprovalRequest]:
        if not self.path.exists():
            return []
        rows: list[ApprovalRequest] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(ApprovalRequest.from_dict(json.loads(line)))
            except Exception:
                continue
        return rows

    def _write_all(self, rows: list[ApprovalRequest]) -> None:
        with FileLock(self.path):
            self._write_all_unlocked(rows)

    def _write_all_unlocked(self, rows: list[ApprovalRequest]) -> None:
        text = "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in rows)
        write_text_unlocked(self.path, text + ("\n" if text else ""))

    def health(self) -> dict[str, Any]:
        return jsonl_health(self.path)

    @staticmethod
    def _treatment_type(prescription: Any) -> str:
        tx = getattr(prescription, "treatment_type", "")
        return str(getattr(tx, "value", tx) or "")

    @staticmethod
    def _summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"type": type(payload).__name__}
        return {
            "keys": sorted(str(k) for k in payload.keys())[:20],
            "size": len(str(payload)),
        }

    @classmethod
    def _prescription_dict(cls, prescription: Any) -> dict[str, Any]:
        tx = getattr(prescription, "treatment_type", "")
        payload = getattr(prescription, "payload", {}) or {}
        return {
            "prescription_id": str(getattr(prescription, "prescription_id", "")),
            "patient_id": str(getattr(prescription, "patient_id", "")),
            "treatment_type": str(getattr(tx, "value", tx) or ""),
            "payload": cls._safe_payload(payload),
            "issued_by": str(getattr(prescription, "issued_by", "")),
            "confidence": float(getattr(prescription, "confidence", 0.0) or 0.0),
            "risk_level": str(getattr(prescription, "risk_level", "")),
            "issued_at": str(getattr(prescription, "issued_at", "")),
            "expires_at": getattr(prescription, "expires_at", None),
        }

    @classmethod
    def _safe_payload(cls, payload: Any) -> Any:
        if isinstance(payload, dict):
            clean = {}
            for key, value in payload.items():
                key_s = str(key)
                if cls._is_secret_key(key_s):
                    clean[key_s] = "[REDACTED]"
                else:
                    clean[key_s] = cls._safe_payload(value)
            return clean
        if isinstance(payload, list):
            return [cls._safe_payload(v) for v in payload]
        try:
            json.dumps(payload, ensure_ascii=False)
            return payload
        except TypeError:
            return str(payload)

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        lowered = key.lower()
        secret_markers = ["secret", "token", "password", "passwd", "api_key", "apikey"]
        return any(marker in lowered for marker in secret_markers)
