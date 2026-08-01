"""Incident triage queue for MEDIC observe alerts.

Observe daemon alerts are raw signals. Incident cases are the human-facing
triage layer that groups repeated alerts, preserves causal context, and keeps
track of acknowledgement/resolution decisions.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from control.file_store import FileLock, jsonl_health, write_text_unlocked


ACTIVE_STATUSES = {"open", "acknowledged"}
FINAL_STATUSES = {"resolved", "rejected"}
ALL_STATUSES = ACTIVE_STATUSES | FINAL_STATUSES
DEFAULT_STALE_AFTER_SECONDS = 24 * 60 * 60


@dataclass
class IncidentCase:
    """A deduplicated observe alert that needs triage."""

    incident_id: str
    alert_fingerprint: str
    severity: str
    target_name: str
    target: str
    message: str
    status: str = "open"
    observer_status: str = ""
    patient_status: str = ""
    failure: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    first_seen_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_seen_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    seen_count: int = 1
    daemon_cycle: int = 0
    supervisor_summary_file: str = ""
    trace_ids: list[str] = field(default_factory=list)
    suggested_treatment: dict[str, Any] = field(default_factory=dict)
    approval_required: bool = True
    decided_at: Optional[str] = None
    decided_by: str = ""
    decision_note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "alert_fingerprint": self.alert_fingerprint,
            "severity": self.severity,
            "target_name": self.target_name,
            "target": self.target,
            "message": self.message,
            "status": self.status,
            "observer_status": self.observer_status,
            "patient_status": self.patient_status,
            "failure": self.failure,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "seen_count": self.seen_count,
            "daemon_cycle": self.daemon_cycle,
            "supervisor_summary_file": self.supervisor_summary_file,
            "trace_ids": self.trace_ids,
            "suggested_treatment": self.suggested_treatment,
            "approval_required": self.approval_required,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_note": self.decision_note,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncidentCase":
        allowed = {
            "incident_id",
            "alert_fingerprint",
            "severity",
            "target_name",
            "target",
            "message",
            "status",
            "observer_status",
            "patient_status",
            "failure",
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
            "seen_count",
            "daemon_cycle",
            "supervisor_summary_file",
            "trace_ids",
            "suggested_treatment",
            "approval_required",
            "decided_at",
            "decided_by",
            "decision_note",
            "metadata",
        }
        clean = {key: value for key, value in data.items() if key in allowed}
        clean.setdefault("trace_ids", [])
        clean.setdefault("suggested_treatment", {})
        clean.setdefault("metadata", {})
        clean["trace_ids"] = [str(item) for item in list(clean.get("trace_ids", []) or []) if str(item)]
        clean["seen_count"] = int(clean.get("seen_count", 1) or 1)
        clean["daemon_cycle"] = int(clean.get("daemon_cycle", 0) or 0)
        clean["approval_required"] = bool(clean.get("approval_required", True))
        return cls(**clean)


class IncidentQueue:
    """JSONL-backed incident queue with dedupe and status transitions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def upsert_from_alert(self, alert: dict[str, Any]) -> dict[str, Any]:
        """Create a new incident or refresh the active matching incident."""
        now = datetime.now(timezone.utc).isoformat()
        fingerprint = self.fingerprint_alert(alert)
        with FileLock(self.path):
            rows = self._read_all_unlocked()
            for row in rows:
                if row.alert_fingerprint == fingerprint and row.status in ACTIVE_STATUSES:
                    self._refresh_from_alert(row, alert, now)
                    self._write_all_unlocked(rows)
                    return {"created": False, "incident": row}

            incident = self._case_from_alert(alert, fingerprint=fingerprint, now=now)
            rows.append(incident)
            self._write_all_unlocked(rows)
            return {"created": True, "incident": incident}

    def list(self, status: Optional[str] = None, limit: Optional[int] = None) -> list[IncidentCase]:
        rows = self._read_all()
        if status and status != "all":
            if status == "active":
                rows = [row for row in rows if row.status in ACTIVE_STATUSES]
            else:
                rows = [row for row in rows if row.status == status]
        if limit is not None:
            rows = rows[-max(1, int(limit or 1)):]
        return rows

    def get(self, incident_id: str) -> IncidentCase:
        for row in self._read_all():
            if row.incident_id == incident_id:
                return row
        raise KeyError(incident_id)

    def transition(
        self,
        incident_id: str,
        status: str,
        decided_by: str = "human",
        note: str = "",
    ) -> IncidentCase:
        if status not in {"acknowledged", "resolved", "rejected"}:
            raise ValueError("status must be acknowledged, resolved, or rejected")

        with FileLock(self.path):
            rows = self._read_all_unlocked()
            found: Optional[IncidentCase] = None
            for row in rows:
                if row.incident_id != incident_id:
                    continue
                if row.status in FINAL_STATUSES:
                    raise ValueError(f"incident is already {row.status}")
                if status == "acknowledged" and row.status != "open":
                    raise ValueError(f"incident is {row.status}, not open")
                now = datetime.now(timezone.utc).isoformat()
                row.status = status
                row.updated_at = now
                row.decided_at = now
                row.decided_by = decided_by
                row.decision_note = note
                found = row
                break

            if found is None:
                raise KeyError(incident_id)
            self._write_all_unlocked(rows)
            return found

    def stats(self) -> dict[str, Any]:
        rows = self._read_all()
        by_status: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        active = 0
        active_critical = 0
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
            by_severity[row.severity] = by_severity.get(row.severity, 0) + 1
            if row.status in ACTIVE_STATUSES:
                active += 1
                if row.severity == "critical":
                    active_critical += 1
        return {
            "path": str(self.path),
            "total": len(rows),
            "open": by_status.get("open", 0),
            "acknowledged": by_status.get("acknowledged", 0),
            "resolved": by_status.get("resolved", 0),
            "rejected": by_status.get("rejected", 0),
            "active": active,
            "active_critical": active_critical,
            "by_status": by_status,
            "by_severity": by_severity,
            "latest_incident_id": rows[-1].incident_id if rows else "",
        }

    def triage_report(
        self,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Summarize active incidents by urgency and age."""
        rows = self._read_all()
        now = datetime.now(timezone.utc)
        stale_after_seconds = max(1.0, float(stale_after_seconds or DEFAULT_STALE_AFTER_SECONDS))
        limit = max(1, int(limit or 1))
        active_cases = [
            self._triage_case(row, now=now, stale_after_seconds=stale_after_seconds)
            for row in rows
            if row.status in ACTIVE_STATUSES
        ]
        active_cases.sort(
            key=lambda item: (
                self._priority_rank(str(item.get("priority", "P3"))),
                -float(item.get("age_seconds", 0.0) or 0.0),
            )
        )
        stale_active = sum(1 for item in active_cases if item.get("is_stale"))
        active_critical = sum(1 for item in active_cases if item.get("severity") == "critical")
        active_by_severity: dict[str, int] = {}
        for item in active_cases:
            severity = str(item.get("severity", "unknown") or "unknown")
            active_by_severity[severity] = active_by_severity.get(severity, 0) + 1

        status = "clear"
        if active_critical or stale_active:
            status = "attention_required"
        elif active_cases:
            status = "active"

        base = self.stats()
        base.update({
            "kind": "incident_triage",
            "status": status,
            "generated_at": now.isoformat(),
            "stale_after_seconds": stale_after_seconds,
            "active_by_severity": active_by_severity,
            "active_critical": active_critical,
            "stale_active": stale_active,
            "highest_priority": active_cases[0]["priority"] if active_cases else "",
            "next_action": self._next_action(status, active_critical, stale_active, len(active_cases)),
            "top_active_cases": active_cases[:limit],
            "top_active_count": min(len(active_cases), limit),
        })
        return base

    def health(self) -> dict[str, Any]:
        return jsonl_health(self.path)

    @staticmethod
    def fingerprint_alert(alert: dict[str, Any]) -> str:
        parts = [
            str(alert.get("target_name", "")),
            str(alert.get("target", "")),
            str(alert.get("status", "")),
            str(alert.get("patient_status", "")),
            str(alert.get("message", "")),
        ]
        raw = "\x1f".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _read_all(self) -> list[IncidentCase]:
        with FileLock(self.path):
            return self._read_all_unlocked()

    def _read_all_unlocked(self) -> list[IncidentCase]:
        if not self.path.exists():
            return []
        rows: list[IncidentCase] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    rows.append(IncidentCase.from_dict(parsed))
            except Exception:
                continue
        return rows

    def _write_all_unlocked(self, rows: list[IncidentCase]) -> None:
        text = "\n".join(json.dumps(row.to_dict(), ensure_ascii=False) for row in rows)
        write_text_unlocked(self.path, text + ("\n" if text else ""))

    @classmethod
    def _case_from_alert(
        cls,
        alert: dict[str, Any],
        fingerprint: str,
        now: str,
    ) -> IncidentCase:
        return IncidentCase(
            incident_id=str(uuid.uuid4()),
            alert_fingerprint=fingerprint,
            severity=str(alert.get("severity", "warning") or "warning"),
            target_name=str(alert.get("target_name", "") or ""),
            target=str(alert.get("target", "") or ""),
            message=str(alert.get("message", "") or ""),
            observer_status=str(alert.get("status", "") or ""),
            patient_status=str(alert.get("patient_status", "") or ""),
            failure=str(alert.get("failure", "") or ""),
            created_at=now,
            updated_at=now,
            first_seen_at=str(alert.get("created_at", "") or now),
            last_seen_at=str(alert.get("created_at", "") or now),
            daemon_cycle=int(alert.get("daemon_cycle", 0) or 0),
            supervisor_summary_file=str(alert.get("supervisor_summary_file", "") or ""),
            trace_ids=cls._trace_ids(alert),
            suggested_treatment=cls._suggest_treatment(alert),
            approval_required=True,
            metadata={
                "source": "observe_daemon",
                "alert_kind": str(alert.get("kind", "") or ""),
            },
        )

    @classmethod
    def _refresh_from_alert(cls, row: IncidentCase, alert: dict[str, Any], now: str) -> None:
        row.severity = cls._max_severity(row.severity, str(alert.get("severity", "") or "warning"))
        row.observer_status = str(alert.get("status", row.observer_status) or "")
        row.patient_status = str(alert.get("patient_status", row.patient_status) or "")
        row.failure = str(alert.get("failure", row.failure) or "")
        row.message = str(alert.get("message", row.message) or "")
        row.updated_at = now
        row.last_seen_at = str(alert.get("created_at", "") or now)
        row.seen_count += 1
        row.daemon_cycle = int(alert.get("daemon_cycle", row.daemon_cycle) or 0)
        row.supervisor_summary_file = str(
            alert.get("supervisor_summary_file", row.supervisor_summary_file) or ""
        )
        row.trace_ids = cls._merge_trace_ids(row.trace_ids, cls._trace_ids(alert))
        row.suggested_treatment = cls._suggest_treatment(alert)

    @staticmethod
    def _trace_ids(alert: dict[str, Any]) -> list[str]:
        return [str(item) for item in list(alert.get("trace_ids", []) or []) if str(item)]

    @staticmethod
    def _merge_trace_ids(existing: list[str], new: list[str]) -> list[str]:
        merged: list[str] = []
        for item in list(existing) + list(new):
            if item and item not in merged:
                merged.append(item)
        return merged

    @staticmethod
    def _max_severity(left: str, right: str) -> str:
        rank = {"info": 0, "warning": 1, "critical": 2}
        return left if rank.get(left, 0) >= rank.get(right, 0) else right

    @staticmethod
    def _suggest_treatment(alert: dict[str, Any]) -> dict[str, Any]:
        severity = str(alert.get("severity", "") or "")
        patient_status = str(alert.get("patient_status", "") or "")
        failure = str(alert.get("failure", "") or "")
        if failure:
            action = "inspect_observe_failure"
        elif severity == "critical" or patient_status == "critical":
            action = "triage_critical_target"
        else:
            action = "review_attention_target"
        return {
            "action": action,
            "observe_only": True,
            "approval_required_before_execution": True,
        }

    @classmethod
    def _triage_case(
        cls,
        row: IncidentCase,
        now: datetime,
        stale_after_seconds: float,
    ) -> dict[str, Any]:
        created_at = cls._parse_time(row.created_at) or now
        updated_at = cls._parse_time(row.updated_at) or created_at
        last_seen_at = cls._parse_time(row.last_seen_at) or updated_at
        age_seconds = max(0.0, (now - created_at).total_seconds())
        since_update_seconds = max(0.0, (now - updated_at).total_seconds())
        since_last_seen_seconds = max(0.0, (now - last_seen_at).total_seconds())
        is_stale = age_seconds >= stale_after_seconds
        priority = "P2"
        reason = "active incident waiting for triage"
        if row.severity == "critical":
            priority = "P0"
            reason = "critical observed target"
        elif is_stale:
            priority = "P1"
            reason = "active incident older than stale threshold"
        elif row.status == "acknowledged":
            priority = "P2"
            reason = "acknowledged incident still active"

        return {
            "incident_id": row.incident_id,
            "priority": priority,
            "reason": reason,
            "status": row.status,
            "severity": row.severity,
            "target_name": row.target_name,
            "target": row.target,
            "message": row.message,
            "age_seconds": round(age_seconds, 3),
            "since_update_seconds": round(since_update_seconds, 3),
            "since_last_seen_seconds": round(since_last_seen_seconds, 3),
            "is_stale": is_stale,
            "seen_count": row.seen_count,
            "trace_ids": row.trace_ids[:5],
            "suggested_treatment": row.suggested_treatment,
            "supervisor_summary_file": row.supervisor_summary_file,
        }

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _priority_rank(priority: str) -> int:
        return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(priority, 9)

    @staticmethod
    def _next_action(
        status: str,
        active_critical: int,
        stale_active: int,
        active: int,
    ) -> str:
        if status == "clear":
            return "No active incidents."
        if active_critical:
            return "Review critical incidents and record acknowledge or resolve decisions."
        if stale_active:
            return "Review stale incidents and either resolve, reject, or refresh the diagnosis."
        return f"Review {active} active incident(s) during the next operator pass."
