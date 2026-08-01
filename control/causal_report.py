"""
causal_report.py
─────────────────────────────────────────────────────────────────────
MEDIC 인과 보고서.

목적:
  - harness에서 원인/처방 판단 정확도를 계산한다.
  - pipeline trace에서 실행 사슬이 얼마나 완성됐는지 계산한다.
  - approval/audit에서 승인 후 실행까지 이어졌는지 확인한다.

중요:
  현재 MEDIC은 진단 엔진 원본 일부가 빠져 있으므로, 진단 인과 사슬과
  실행 인과 사슬을 분리해 보고한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from control.approval_queue import ApprovalQueue
from control.audit_log import AuditLog
from control.pipeline_trace import PipelineTrace, TraceEvent


@dataclass
class CausalFinding:
    """인과 보고서 finding."""
    severity: str
    area: str
    message: str
    suggestion: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "area": self.area,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class CausalReport:
    """MEDIC 인과성 요약."""
    status: str
    harness: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    intervention: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    findings: list[CausalFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "harness": self.harness,
            "trace": self.trace,
            "intervention": self.intervention,
            "approval": self.approval,
            "audit": self.audit,
            "findings": [f.to_dict() for f in self.findings],
        }

    def render_text(self) -> str:
        lines = [
            "MEDIC Causal Report",
            f"status: {self.status}",
            "",
            "Harness:",
            f"  cases: {self.harness.get('cases', 0)}",
            f"  severity match: {self.harness.get('severity_match_rate', 0):.1%}",
            f"  root cause match: {self.harness.get('root_cause_match_rate', 0):.1%}",
            f"  treatment strict match: {self.harness.get('treatment_strict_match_rate', 0):.1%}",
            f"  supported match: {self.harness.get('supported_match_rate', 0):.1%}",
            f"  false positive: {self.harness.get('false_positive_rate', 0):.1%}",
            f"  false negative: {self.harness.get('false_negative_rate', 0):.1%}",
            f"  worst ablation delta: {self.harness.get('worst_ablation_delta', 0):+.1%}",
            "",
            "Trace:",
            f"  traces: {self.trace.get('traces_seen', 0)}",
            f"  diagnostic traces: {self.trace.get('diagnostic_traces_seen', 0)}",
            f"  execution traces: {self.trace.get('execution_traces_seen', 0)}",
            f"  execution chain completeness: {self.trace.get('execution_chain_completeness', 0):.1%}",
            f"  diagnostic chain completeness: {self.trace.get('diagnostic_chain_completeness', 0):.1%}",
            f"  missing diagnostic stages: {', '.join(self.trace.get('missing_diagnostic_stages', [])) or 'none'}",
            "",
            "Intervention:",
            f"  applied rate: {self.intervention.get('applied_rate', 0):.1%}",
            f"  health verification rate: {self.intervention.get('health_verification_rate', 0):.1%}",
            "",
            "Approval:",
            f"  pending: {self.approval.get('pending', 0)}",
            f"  approved: {self.approval.get('approved', 0)}",
            f"  executed: {self.approval.get('executed', 0)}",
            f"  execution failed: {self.approval.get('execution_failed', 0)}",
            f"  active unlinked trace_id: {self.approval.get('active_unlinked_trace_id', 0)}",
            "",
            "Audit:",
            f"  policy reviews checked: {self.audit.get('policy_review_checked', 0)}",
            f"  policy trace link rate: {self.audit.get('policy_review_trace_link_rate', 0):.1%}",
            f"  latest policy trace linked: {self.audit.get('latest_policy_review_has_trace_id', False)}",
            "",
            "Findings:",
        ]
        if not self.findings:
            lines.append("  none")
        for finding in self.findings:
            lines.append(f"  [{finding.severity}] {finding.area}: {finding.message}")
            if finding.suggestion:
                lines.append(f"       -> {finding.suggestion}")
        return "\n".join(lines)


class CausalReportBuilder:
    """Build a causal report from current MEDIC artifacts."""

    DIAGNOSTIC_STAGES = [
        "collect_vitals",
        "diagnose",
        "prescribe",
        "second_opinion",
    ]
    EXECUTION_STAGES = [
        "prescription_received",
        "control_gateway",
        "treatment_execution",
        "health_verification",
        "runner_complete",
    ]

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.harness_dir = self.root / "harness_runs"
        self.trace = PipelineTrace(self.root / "control_state" / "pipeline_trace.jsonl")
        self.approval_queue = ApprovalQueue(
            self.root / "control_state" / "approval_queue.jsonl"
        )
        self.audit_log = AuditLog(self.root / "control_state" / "audit.jsonl")

    def build(self) -> CausalReport:
        harness = self._harness_metrics()
        trace = self._trace_metrics()
        intervention = self._intervention_metrics()
        approval = self.approval_queue.stats()
        audit = self._audit_metrics()
        findings = self._findings(harness, trace, intervention, approval, audit)
        status = self._status(findings)
        return CausalReport(
            status=status,
            harness=harness,
            trace=trace,
            intervention=intervention,
            approval=approval,
            audit=audit,
            findings=findings,
        )

    def _harness_metrics(self) -> dict[str, Any]:
        latest = self._latest_harness()
        reports = list(latest.get("reports", []) or [])
        baseline = self._baseline_report(reports)
        results = list(baseline.get("results", []) or [])
        total = len(results)

        severity_ok = 0
        root_ok = 0
        treatment_strict_ok = 0
        supported_ok = 0
        false_pos = 0
        false_neg = 0

        for row in results:
            severity_expected = str(row.get("severity_expected", ""))
            severity_actual = str(row.get("severity_actual", ""))
            root_expected = str(row.get("root_cause_expected", ""))
            root_actual = str(row.get("root_cause_actual", ""))
            treatment_expected = str(row.get("treatment_expected", ""))
            treatment_actual = str(row.get("treatment_actual", ""))

            severity_ok += int(severity_expected == severity_actual)
            root_ok += int(root_expected == root_actual)
            treatment_strict_ok += int(treatment_expected == treatment_actual)
            supported_ok += int(bool(row.get("matched", False)))

            expected_issue = severity_expected not in {"LOW", "OK", "HEALTHY"} and root_expected != "no_issue_detected"
            actual_issue = severity_actual not in {"LOW", "OK", "HEALTHY"} and root_actual != "no_issue_detected"
            false_pos += int(not expected_issue and actual_issue)
            false_neg += int(expected_issue and not actual_issue)

        variants = list(latest.get("variants", []) or [])
        deltas = [float(v.get("delta_vs_baseline", 0.0) or 0.0) for v in variants]
        worst_delta = min(deltas) if deltas else 0.0

        return {
            "latest_file": self._latest_harness_path().name if self._latest_harness_path() else "",
            "cases": total,
            "severity_match_rate": self._rate(severity_ok, total),
            "root_cause_match_rate": self._rate(root_ok, total),
            "treatment_strict_match_rate": self._rate(treatment_strict_ok, total),
            "supported_match_rate": self._rate(supported_ok, total),
            "false_positive_rate": self._rate(false_pos, total),
            "false_negative_rate": self._rate(false_neg, total),
            "worst_ablation_delta": worst_delta,
            "variants": len(variants),
        }

    def _trace_metrics(self) -> dict[str, Any]:
        events, boundary_dropped = self._recent_complete_trace_events(limit=10000)
        by_trace: dict[str, list[TraceEvent]] = {}
        for event in events:
            by_trace.setdefault(event.trace_id, []).append(event)

        execution_scores = []
        diagnostic_scores = []
        missing_diagnostic: set[str] = set()
        diagnostic_traces = 0
        execution_traces = 0
        for trace_events in by_trace.values():
            stages = {event.stage for event in trace_events}
            has_execution = any(stage in stages for stage in self.EXECUTION_STAGES)
            has_diagnostic = any(stage in stages for stage in self.DIAGNOSTIC_STAGES)

            if has_execution:
                expected_execution = self._expected_execution_stages(trace_events)
                execution_traces += 1
                execution_scores.append(
                    self._rate(
                        sum(1 for stage in expected_execution if stage in stages),
                        len(expected_execution),
                    )
                )
            if has_diagnostic:
                diagnostic_traces += 1
                diagnostic_scores.append(
                    self._rate(
                        sum(1 for stage in self.DIAGNOSTIC_STAGES if stage in stages),
                        len(self.DIAGNOSTIC_STAGES),
                    )
                )
                for stage in self.DIAGNOSTIC_STAGES:
                    if stage not in stages:
                        missing_diagnostic.add(stage)

        if not diagnostic_traces:
            missing_diagnostic.update(self.DIAGNOSTIC_STAGES)

        diagnostic_completeness = self._avg(diagnostic_scores)
        missing_diagnostic_stages = sorted(missing_diagnostic)
        if diagnostic_completeness >= 0.99:
            missing_diagnostic_stages = []

        return {
            "events_seen": len(events),
            "traces_seen": len(by_trace),
            "execution_traces_seen": execution_traces,
            "diagnostic_traces_seen": diagnostic_traces,
            "execution_chain_completeness": self._avg(execution_scores),
            "diagnostic_chain_completeness": diagnostic_completeness,
            "missing_diagnostic_stages": missing_diagnostic_stages,
            "boundary_events_dropped": boundary_dropped,
        }

    def _expected_execution_stages(self, events: list[TraceEvent]) -> list[str]:
        treatment_events = [event for event in events if event.stage == "treatment_execution"]
        skipped = any(event.status == "skipped" for event in treatment_events)
        if skipped:
            return [
                "prescription_received",
                "control_gateway",
                "treatment_execution",
                "runner_complete",
            ]
        return list(self.EXECUTION_STAGES)

    def _intervention_metrics(self) -> dict[str, Any]:
        events, _boundary_dropped = self._recent_complete_trace_events(limit=10000)
        runner_complete = [e for e in events if e.stage == "runner_complete"]
        applied = [e for e in runner_complete if e.status == "applied"]
        execution_attempts = [
            e for e in events
            if e.stage == "treatment_execution" and e.status != "skipped"
        ]
        health = [e for e in events if e.stage == "health_verification"]
        health_ok = [e for e in health if e.status == "ok"]
        return {
            "runner_complete": len(runner_complete),
            "execution_attempts": len(execution_attempts),
            "applied": len(applied),
            "applied_rate": self._rate(len(applied), len(execution_attempts)),
            "health_verifications": len(health),
            "health_ok": len(health_ok),
            "health_verification_rate": self._rate(len(health_ok), len(health)),
        }

    def _audit_metrics(self) -> dict[str, Any]:
        events = self.audit_log.tail(limit=10000)
        policy_reviews = [e for e in events if e.event_type == "policy_review"]
        approval_events = [e for e in events if e.event_type.startswith("approval_")]
        linked_policy = [e for e in policy_reviews if self._event_trace_id(e)]
        linked_approval = [e for e in approval_events if self._event_trace_id(e)]
        latest_policy = policy_reviews[-1] if policy_reviews else None
        return {
            "events_checked": len(events),
            "policy_review_checked": len(policy_reviews),
            "policy_review_trace_linked": len(linked_policy),
            "policy_review_trace_link_rate": self._rate(
                len(linked_policy),
                len(policy_reviews),
            ),
            "latest_policy_review_has_trace_id": (
                bool(self._event_trace_id(latest_policy)) if latest_policy else True
            ),
            "approval_events_checked": len(approval_events),
            "approval_events_trace_linked": len(linked_approval),
            "approval_events_trace_link_rate": self._rate(
                len(linked_approval),
                len(approval_events),
            ),
        }

    def _recent_complete_trace_events(self, limit: int = 10000) -> tuple[list[TraceEvent], int]:
        events = self.trace.tail(limit=limit)
        total_events = int(self.trace.stats().get("events_seen", len(events)) or 0)
        if total_events <= len(events) or not events:
            return events, 0
        boundary_trace_id = events[0].trace_id
        filtered = [event for event in events if event.trace_id != boundary_trace_id]
        return filtered, len(events) - len(filtered)

    def _findings(
        self,
        harness: dict[str, Any],
        trace: dict[str, Any],
        intervention: dict[str, Any],
        approval: dict[str, Any],
        audit: dict[str, Any],
    ) -> list[CausalFinding]:
        findings: list[CausalFinding] = []

        if harness.get("cases", 0) < 30:
            findings.append(CausalFinding(
                "MEDIUM",
                "harness",
                f"인과 판단 케이스가 {harness.get('cases', 0)}개뿐입니다.",
                "정상/경고/심각/편향/위험 payload 케이스를 최소 30개 이상으로 늘리세요.",
            ))
        if harness.get("root_cause_match_rate", 0.0) < 0.95:
            findings.append(CausalFinding(
                "HIGH",
                "root_cause",
                "root cause match rate가 낮습니다.",
                "원인 분류 규칙과 증상 매핑을 먼저 보강하세요.",
            ))
        if harness.get("treatment_strict_match_rate", 0.0) < 0.90:
            findings.append(CausalFinding(
                "MEDIUM",
                "treatment",
                "strict treatment match rate가 낮습니다.",
                "대체 처방이 허용되는 케이스인지, 기대값이 낡았는지 분리하세요.",
            ))
        if (
            trace.get("diagnostic_traces_seen", 0) == 0
            or trace.get("diagnostic_chain_completeness", 0.0) < 0.80
        ):
            findings.append(CausalFinding(
                "MEDIUM",
                "diagnostic_trace",
                "진단 단계 trace가 거의 없습니다.",
                "collect_vitals, diagnose, prescribe, second_opinion 단계에 trace를 추가하세요.",
            ))
        if trace.get("execution_chain_completeness", 0.0) < 0.80:
            findings.append(CausalFinding(
                "HIGH",
                "execution_trace",
                "실행 단계 trace가 불완전합니다.",
                "ControlledTreatmentRunner 경로 밖의 실행이 있는지 확인하세요.",
            ))
        if intervention.get("execution_attempts", 0) and intervention.get("applied_rate", 0.0) < 0.95:
            findings.append(CausalFinding(
                "MEDIUM",
                "intervention",
                "실행 완료 대비 applied 비율이 낮습니다.",
                "실패 trace의 treatment_execution 및 health_verification 단계를 확인하세요.",
            ))
        if int(approval.get("pending", 0) or 0) > 0:
            findings.append(CausalFinding(
                "MEDIUM",
                "approval",
                "승인 대기 요청이 남아 있습니다.",
                "운영 전 pending approval을 모두 승인/거부하세요.",
            ))
        if int(approval.get("active_unlinked_trace_id", 0) or 0) > 0:
            findings.append(CausalFinding(
                "MEDIUM",
                "approval_trace",
                "trace_id가 없는 활성 승인 요청이 있습니다.",
                "새 승인 요청은 ControlGateway trace_id를 ApprovalQueue에 함께 저장하세요.",
            ))
        if not bool(audit.get("latest_policy_review_has_trace_id", True)):
            findings.append(CausalFinding(
                "MEDIUM",
                "audit_trace",
                "최신 policy_review 감사 이벤트에 trace_id가 없습니다.",
                "ControlGateway.review() 호출 시 trace_id를 전달해 AuditLog와 PipelineTrace를 묶으세요.",
            ))
        return findings

    @staticmethod
    def _status(findings: list[CausalFinding]) -> str:
        severities = {finding.severity for finding in findings}
        if "HIGH" in severities:
            return "blocked"
        if "MEDIUM" in severities:
            return "warning"
        return "healthy"

    def _latest_harness_path(self) -> Optional[Path]:
        files = sorted(self.harness_dir.glob("*_summary.json"))
        return files[-1] if files else None

    def _latest_harness(self) -> dict[str, Any]:
        path = self._latest_harness_path()
        if not path:
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _baseline_report(reports: list[dict[str, Any]]) -> dict[str, Any]:
        for report in reports:
            if bool(report.get("decode_enabled", True)) and bool(report.get("uics_enabled", True)):
                return report
        return reports[0] if reports else {}

    @staticmethod
    def _rate(num: int | float, den: int | float) -> float:
        if not den:
            return 0.0
        return round(float(num) / float(den), 4)

    @staticmethod
    def _avg(values: list[float]) -> float:
        if not values:
            return 0.0
        return round(sum(values) / len(values), 4)

    @staticmethod
    def _event_trace_id(event: Any) -> str:
        if event is None:
            return ""
        context = getattr(event, "context", {}) or {}
        return str(context.get("trace_id") or "")
