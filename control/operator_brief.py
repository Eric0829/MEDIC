"""Operator-facing MEDIC status brief.

The brief is a read-only rollup for a human operator. It does not approve,
reject, execute, repair, rotate, or delete anything.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.approval_queue import ApprovalQueue, ApprovalRequest
from control.incident_queue import DEFAULT_STALE_AFTER_SECONDS, IncidentQueue
from control.observe_daemon import read_observe_daemon_status
from control.self_control_layer import MedicSelfControlLayer


DEFAULT_DAEMON_STALE_AFTER_SECONDS = 60 * 60


@dataclass
class OperatorBrief:
    """One human-readable MEDIC operations snapshot."""

    status: str
    generated_at: str
    root: str
    summary: str
    top_action: str
    self_control: dict[str, Any] = field(default_factory=dict)
    role_contract: dict[str, Any] = field(default_factory=dict)
    incident: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    observe_daemon: dict[str, Any] = field(default_factory=dict)
    causal: dict[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    approved_waiting_execution: list[dict[str, Any]] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "operator_brief",
            "status": self.status,
            "generated_at": self.generated_at,
            "root": self.root,
            "summary": self.summary,
            "top_action": self.top_action,
            "self_control": self.self_control,
            "role_contract": self.role_contract,
            "incident": self.incident,
            "approval": self.approval,
            "observe_daemon": self.observe_daemon,
            "causal": self.causal,
            "storage": self.storage,
            "pending_approvals": self.pending_approvals,
            "approved_waiting_execution": self.approved_waiting_execution,
            "open_items": self.open_items,
            "commands": self.commands,
        }

    def render_text(self) -> str:
        causal_harness = dict(self.causal.get("harness", {}) or {})
        causal_trace = dict(self.causal.get("trace", {}) or {})
        storage_rotation = list(self.storage.get("rotation_recommended", []) or [])
        daemon_process = dict(self.observe_daemon.get("process", {}) or {})
        lines = [
            f"MEDIC Operator Brief ({self.generated_at})",
            f"root: {self.root}",
            f"status: {self.status}",
            f"top action: {self.top_action}",
            "",
            "Safety:",
            f"  self-control: {self.self_control.get('status', 'unknown')}",
            f"  role contract: {self.role_contract.get('status', 'unknown')}",
            f"  default execution: {self.role_contract.get('default_execution_mode', '') or 'unknown'}",
            f"  auto execute: {self.role_contract.get('auto_execute_enabled', False)}",
            "",
            "Work Queue:",
            f"  incidents: {self.incident.get('status', 'unknown')} "
            f"active={self.incident.get('active', 0)} "
            f"critical={self.incident.get('active_critical', 0)} "
            f"stale={self.incident.get('stale_active', 0)}",
            f"  approvals: pending={self.approval.get('pending', 0)} "
            f"approved={self.approval.get('approved', 0)}",
            "",
            "Operations:",
            f"  daemon: {self.observe_daemon.get('status', 'unknown')} "
            f"updated={self.observe_daemon.get('updated_at', '') or 'none'}",
            f"  daemon heartbeat: stale={self.observe_daemon.get('is_stale', False)} "
            f"age={int(float(self.observe_daemon.get('age_seconds', 0) or 0))}s",
            f"  daemon process: {daemon_process.get('status', 'unknown')} "
            f"count={daemon_process.get('count', 0)}",
            f"  recent alerts: {self.observe_daemon.get('recent_alert_count', 0)} / "
            f"{self.observe_daemon.get('total_alert_lines', 0)}",
            f"  storage: {self.storage.get('status', 'unknown')} "
            f"invalid={self.storage.get('invalid_recent_lines', 0)} "
            f"rotation={', '.join(storage_rotation) or 'none'}",
            f"  causal: {self.causal.get('status', 'unknown')} "
            f"root={float(causal_harness.get('root_cause_match_rate', 0) or 0):.1%} "
            f"treatment={float(causal_harness.get('treatment_strict_match_rate', 0) or 0):.1%} "
            f"chain={float(causal_trace.get('execution_chain_completeness', 0) or 0):.1%}",
            "",
            "Open Items:",
        ]
        if not self.open_items:
            lines.append("  none")
        for item in self.open_items:
            lines.append(f"  - {item}")

        incidents = list(self.incident.get("top_active_cases", []) or [])
        if incidents:
            lines.extend(["", "Top Incidents:"])
            for item in incidents[:5]:
                lines.append(
                    f"  {item.get('priority', '')} {item.get('incident_id', '')} "
                    f"{item.get('status', '')} {item.get('target_name', '')}"
                )
                lines.append(f"    {item.get('message', '')}")

        if self.pending_approvals:
            lines.extend(["", "Pending Approvals:"])
            for item in self.pending_approvals[:5]:
                lines.append(
                    f"  {item.get('request_id', '')} "
                    f"{item.get('risk_level', '')} {item.get('treatment_type', '')} "
                    f"{item.get('patient_id', '')}"
                )
                lines.append(f"    {item.get('reason', '')}")
        return "\n".join(lines)


class OperatorBriefBuilder:
    """Build a single operations brief from existing MEDIC control signals."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.approval_queue = ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.incident_queue = IncidentQueue(
            self.root / "control_state" / "incident_cases.jsonl"
        )

    def build(
        self,
        observe_daemon_config: str = "",
        incident_stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        daemon_stale_after_seconds: float = DEFAULT_DAEMON_STALE_AFTER_SECONDS,
        incident_limit: int = 5,
        approval_limit: int = 5,
        alert_limit: int = 5,
    ) -> OperatorBrief:
        self_report = MedicSelfControlLayer(str(self.root)).inspect().to_dict()
        incident = self.incident_queue.triage_report(
            stale_after_seconds=incident_stale_after_seconds,
            limit=incident_limit,
        )
        approval = self.approval_queue.stats()
        pending = self._request_rows("pending", approval_limit)
        approved = self._request_rows("approved", approval_limit)
        daemon = self._daemon_status(
            observe_daemon_config,
            alert_limit,
            daemon_stale_after_seconds=daemon_stale_after_seconds,
        )

        open_items = self._open_items(
            self_report=self_report,
            incident=incident,
            approval=approval,
            daemon=daemon,
        )
        status = self._status(
            self_report=self_report,
            incident=incident,
            approval=approval,
            daemon=daemon,
        )
        top_action = self._top_action(
            status=status,
            self_report=self_report,
            incident=incident,
            approval=approval,
            daemon=daemon,
        )
        return OperatorBrief(
            status=status,
            generated_at=datetime.now(timezone.utc).isoformat(),
            root=str(self.root),
            summary=self._summary(status, open_items),
            top_action=top_action,
            self_control={
                "status": self_report.get("status", "unknown"),
                "summary": self_report.get("summary", ""),
                "findings": self_report.get("findings", []),
            },
            role_contract=dict(self_report.get("role_contract", {}) or {}),
            incident=incident,
            approval=approval,
            observe_daemon=daemon,
            causal=dict(self_report.get("causal", {}) or {}),
            storage=dict(self_report.get("storage", {}) or {}),
            pending_approvals=pending,
            approved_waiting_execution=approved,
            open_items=open_items,
            commands=self._commands(),
        )

    def _request_rows(self, status: str, limit: int) -> list[dict[str, Any]]:
        rows = self.approval_queue.list(status=status)
        limit = max(1, int(limit or 1))
        return [self._approval_summary(row) for row in rows[:limit]]

    @staticmethod
    def _approval_summary(row: ApprovalRequest) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "status": row.status,
            "patient_id": row.patient_id,
            "treatment_type": row.treatment_type,
            "risk_level": row.risk_level,
            "reason": row.reason,
            "trace_id": row.trace_id,
            "created_at": row.created_at,
        }

    def _daemon_status(
        self,
        config_path: str,
        alert_limit: int,
        daemon_stale_after_seconds: float,
    ) -> dict[str, Any]:
        try:
            status = read_observe_daemon_status(
                self.root,
                config_path=config_path,
                alert_limit=alert_limit,
            )
            status.update(self._daemon_freshness(
                status.get("updated_at", ""),
                stale_after_seconds=daemon_stale_after_seconds,
            ))
            return status
        except Exception as exc:
            return {
                "kind": "observe_daemon_status",
                "status": "error",
                "message": f"{type(exc).__name__}: {exc}",
                "recent_alert_count": 0,
                "total_alert_lines": 0,
                "process": {
                    "status": "unknown",
                    "alive": False,
                    "count": 0,
                    "processes": [],
                    "error": f"{type(exc).__name__}: {exc}",
                },
                "is_stale": True,
                "age_seconds": 0.0,
            }

    @classmethod
    def _daemon_freshness(
        cls,
        updated_at: str,
        stale_after_seconds: float,
    ) -> dict[str, Any]:
        stale_after_seconds = max(1.0, float(stale_after_seconds or 1.0))
        updated = cls._parse_time(str(updated_at or ""))
        if updated is None:
            return {
                "is_stale": True,
                "age_seconds": 0.0,
                "stale_after_seconds": stale_after_seconds,
            }
        age = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
        return {
            "is_stale": age >= stale_after_seconds,
            "age_seconds": round(age, 3),
            "stale_after_seconds": stale_after_seconds,
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
    def _status(
        self_report: dict[str, Any],
        incident: dict[str, Any],
        approval: dict[str, Any],
        daemon: dict[str, Any],
    ) -> str:
        role = dict(self_report.get("role_contract", {}) or {})
        storage = dict(self_report.get("storage", {}) or {})
        causal = dict(self_report.get("causal", {}) or {})
        daemon_process = dict(daemon.get("process", {}) or {})
        blocked = any([
            self_report.get("status") == "blocked",
            role.get("status") == "blocked",
            storage.get("status") == "blocked",
            causal.get("status") == "blocked",
            daemon.get("status") == "blocked",
            daemon.get("status") == "error",
        ])
        if blocked:
            return "blocked"

        attention = any([
            self_report.get("status") == "warning",
            role.get("status") == "warning",
            storage.get("status") == "warning",
            causal.get("status") == "warning",
            incident.get("status") in {"active", "attention_required"},
            int(approval.get("pending", 0) or 0) > 0,
            int(approval.get("approved", 0) or 0) > 0,
            daemon.get("status") in {"missing", "warning"},
            bool(daemon.get("is_stale", False)),
            not bool(daemon_process.get("alive", False)),
        ])
        return "attention_required" if attention else "clear"

    @classmethod
    def _top_action(
        cls,
        status: str,
        self_report: dict[str, Any],
        incident: dict[str, Any],
        approval: dict[str, Any],
        daemon: dict[str, Any],
    ) -> str:
        if status == "blocked":
            return "Keep MEDIC observe-only and inspect self-control, storage, role contract, and daemon errors."
        if incident.get("status") == "attention_required":
            return "Run --incident-triage and resolve or acknowledge the highest-priority incident."
        if int(approval.get("pending", 0) or 0) > 0:
            return "Run --approval-list pending and record approve/reject decisions."
        if int(approval.get("approved", 0) or 0) > 0:
            return "Run --approval-list approved and execute only through the controlled runner."
        if incident.get("status") == "active":
            return "Review active incidents during the next operator pass."
        daemon_process = dict(daemon.get("process", {}) or {})
        if not bool(daemon_process.get("alive", False)):
            return "Start the continuous observe daemon or install user startup."
        if bool(daemon.get("is_stale", False)):
            return "Refresh or start the observe daemon; its latest heartbeat is stale."
        if daemon.get("status") in {"missing", "warning"}:
            return "Check --observe-daemon-status before relying on continuous monitoring."
        if self_report.get("status") == "warning":
            return "Review self-control findings before enabling execution."
        return "No immediate operator action; keep MEDIC in observe-only monitoring."

    @staticmethod
    def _open_items(
        self_report: dict[str, Any],
        incident: dict[str, Any],
        approval: dict[str, Any],
        daemon: dict[str, Any],
    ) -> list[str]:
        items: list[str] = []
        if self_report.get("status") != "healthy":
            items.append(f"self-control is {self_report.get('status')}")
        if incident.get("status") != "clear":
            items.append(
                f"incident triage is {incident.get('status')} "
                f"(active={incident.get('active', 0)}, stale={incident.get('stale_active', 0)})"
            )
        if int(approval.get("pending", 0) or 0) > 0:
            items.append(f"{approval.get('pending')} approval request(s) pending")
        if int(approval.get("approved", 0) or 0) > 0:
            items.append(f"{approval.get('approved')} approval request(s) approved but not executed")
        if daemon.get("status") in {"missing", "warning", "blocked", "error"}:
            items.append(f"observe daemon status is {daemon.get('status')}")
        daemon_process = dict(daemon.get("process", {}) or {})
        if not bool(daemon_process.get("alive", False)):
            items.append("observe daemon process is not running")
        if bool(daemon.get("is_stale", False)):
            items.append(
                f"observe daemon heartbeat is stale "
                f"(age={int(float(daemon.get('age_seconds', 0) or 0))}s)"
            )
        return items

    @staticmethod
    def _summary(status: str, open_items: list[str]) -> str:
        if status == "clear":
            return "MEDIC has no active incident, approval, storage, or self-control action items."
        return f"MEDIC has {len(open_items)} operator item(s) requiring attention."

    @staticmethod
    def _commands() -> dict[str, str]:
        python = OperatorBriefBuilder._quote_arg(sys.executable or "python")
        return {
            "self_control": f"{python} MEDIC\\medic_control.py --json",
            "incident_triage": f"{python} MEDIC\\medic_control.py --incident-triage",
            "approval_pending": f"{python} MEDIC\\medic_control.py --approval-list pending",
            "daemon_status": f"{python} MEDIC\\medic_control.py --observe-daemon-status",
            "start_daemon_hidden": ".\\MEDIC\\scripts\\start_observe_daemon_hidden.ps1",
            "install_user_startup": ".\\MEDIC\\scripts\\install_user_startup.ps1 -Apply",
            "benchmark_suite": ".\\MEDIC\\scripts\\run_benchmark_suite.ps1",
            "observe_soak": ".\\MEDIC\\scripts\\run_observe_soak.ps1 -Cycles 3 -Interval 1",
            "causal_report": f"{python} MEDIC\\medic_control.py --causal-report",
            "storage_health": f"{python} MEDIC\\medic_control.py --storage-health",
        }

    @staticmethod
    def _quote_arg(value: str) -> str:
        if any(char.isspace() for char in value):
            return '"' + value.replace('"', '\\"') + '"'
        return value
