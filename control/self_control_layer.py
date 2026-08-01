"""
self_control_layer.py
─────────────────────────────────────────────────────────────────────
MEDIC 자기 점검 컨트롤 레이어.

목표:
  - MEDIC 자신을 환자처럼 관찰한다.
  - harness 결과에서 판단 흔들림과 bias flag를 찾는다.
  - soak/audit 결과에서 observe-only 안전성이 유지됐는지 확인한다.
  - SelfRepairGuard와 IndependenceTracker 같은 제어 부품이 연결 가능한지 본다.

이 모듈은 observe-only다. 처방을 적용하거나 파일을 수정하지 않는다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from control.approval_queue import ApprovalQueue
from control.audit_log import AuditLog
from control.causal_report import CausalReportBuilder
from control.controlled_registry import ControlledPatientRegistry
from control.direct_call_detector import DirectTreatmentCallDetector
from control.incident_queue import IncidentQueue
from control.pipeline_trace import PipelineTrace
from control.policy_engine import PolicyEngine
from control.role_contract import inspect_role_contract
from control.second_opinion_gate import SecondOpinionGate
from control.storage_health import ControlStorageHealth
from infrastructure.independence_tracker import MedicIndependenceTracker, Stage
from infrastructure.self_repair_guard import SelfRepairGuard


@dataclass
class ControlFinding:
    """컨트롤 레이어가 발견한 점검 항목."""
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
class MedicSelfReport:
    """MEDIC 자기 점검 보고서."""
    status: str
    generated_at: str
    root: str
    summary: str
    findings: list[ControlFinding] = field(default_factory=list)
    harness: dict[str, Any] = field(default_factory=dict)
    soak: dict[str, Any] = field(default_factory=dict)
    observe: dict[str, Any] = field(default_factory=dict)
    independence: dict[str, Any] = field(default_factory=dict)
    guard: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    second_opinion: dict[str, Any] = field(default_factory=dict)
    second_opinion_harness: dict[str, Any] = field(default_factory=dict)
    approval: dict[str, Any] = field(default_factory=dict)
    incident: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    bypass: dict[str, Any] = field(default_factory=dict)
    registry: dict[str, Any] = field(default_factory=dict)
    causal: dict[str, Any] = field(default_factory=dict)
    role_contract: dict[str, Any] = field(default_factory=dict)
    next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at,
            "root": self.root,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "harness": self.harness,
            "soak": self.soak,
            "observe": self.observe,
            "independence": self.independence,
            "guard": self.guard,
            "policy": self.policy,
            "second_opinion": self.second_opinion,
            "second_opinion_harness": self.second_opinion_harness,
            "approval": self.approval,
            "incident": self.incident,
            "audit": self.audit,
            "trace": self.trace,
            "storage": self.storage,
            "bypass": self.bypass,
            "registry": self.registry,
            "causal": self.causal,
            "role_contract": self.role_contract,
            "next_actions": self.next_actions,
        }

    def render_text(self) -> str:
        lines = [
            f"MEDIC Self-Control Report ({self.generated_at})",
            f"root: {self.root}",
            f"status: {self.status}",
            "",
            self.summary,
            "",
            "Role Contract:",
            f"  status: {self.role_contract.get('status', '') or 'none'}",
            f"  source: {self.role_contract.get('source', '') or 'none'}",
            f"  agent kind: {self.role_contract.get('agent_kind', '') or 'none'}",
            f"  mode: {self.role_contract.get('operating_mode', '') or 'none'}",
            f"  default execution: {self.role_contract.get('default_execution_mode', '') or 'none'}",
            f"  auto execute: {self.role_contract.get('auto_execute_enabled', False)}",
            f"  violations: {len(self.role_contract.get('violations', []))}",
            "",
            "Harness:",
            f"  files: {self.harness.get('files_seen', 0)}",
            f"  latest: {self.harness.get('latest_file', 'none')}",
            f"  baseline match: {self.harness.get('baseline_match_rate', 0):.1%}",
            f"  worst variant: {self.harness.get('worst_variant', 'none')}",
            f"  bias flags: {len(self.harness.get('bias_flags', []))}",
            "",
            "Soak:",
            f"  files: {self.soak.get('files_seen', 0)}",
            f"  observe-only runs: {self.soak.get('observe_only_runs', 0)}",
            f"  control runs: {self.soak.get('control_runs', 0)}",
            f"  latest control: {self.soak.get('latest_control_status', '') or 'none'}",
            f"  latest iterations: {self.soak.get('latest_control_healthy_iterations', 0)} / {self.soak.get('latest_control_iterations', 0)}",
            f"  approval events: {self.soak.get('approval_events', 0)}",
            "",
            "Observe Loop:",
            f"  files: {self.observe.get('files_seen', 0)}",
            f"  latest: {self.observe.get('latest_file', '') or 'none'}",
            f"  status: {self.observe.get('latest_status', '') or 'none'}",
            f"  patient status: {self.observe.get('latest_patient_status', '') or 'none'}",
            f"  latest iterations: {self.observe.get('latest_successful_iterations', 0)} / {self.observe.get('latest_iterations', 0)}",
            f"  pending approval: {self.observe.get('latest_pending_approval', 0)}",
            f"  supervisor latest: {self.observe.get('latest_supervisor_file', '') or 'none'}",
            f"  supervisor status: {self.observe.get('latest_supervisor_status', '') or 'none'}",
            f"  supervisor targets: {self.observe.get('latest_supervisor_targets_observed', 0)} observed",
            f"  daemon status: {self.observe.get('latest_daemon_status', '') or 'none'}",
            f"  daemon cycles: {self.observe.get('latest_daemon_cycles_completed', 0)}",
            "",
            "Independence:",
            f"  score: {self.independence.get('independence_score', 0):.1%}",
            f"  verdict: {self.independence.get('verdict', 'unknown')}",
            f"  source: {self.independence.get('source', 'synthetic')}",
            "",
            "Guard:",
            f"  high risk today: {self.guard.get('high_risk_today', 0)} / {self.guard.get('limit', 0)}",
            f"  human above: {self.guard.get('human_above', 0):.2f}",
            "",
            "Policy:",
            f"  medium auto: {self.policy.get('allow_medium_auto', False)}",
            f"  high-risk treatments: {len(self.policy.get('high_risk_treatments', []))}",
            "",
            "Second Opinion:",
            f"  enabled: {self.second_opinion.get('enabled', False)}",
            f"  reviewer: {self.second_opinion.get('reviewer', 'unknown')}",
            f"  always required: {len(self.second_opinion.get('always_required_treatments', []))}",
            f"  harness: {self.second_opinion_harness.get('matched_cases', 0)} / {self.second_opinion_harness.get('total_cases', 0)}",
            f"  latest: {self.second_opinion_harness.get('latest_file', '') or 'none'}",
            "",
            "Approval:",
            f"  pending: {self.approval.get('pending', 0)}",
            f"  approved: {self.approval.get('approved', 0)}",
            f"  rejected: {self.approval.get('rejected', 0)}",
            f"  executed: {self.approval.get('executed', 0)}",
            f"  execution failed: {self.approval.get('execution_failed', 0)}",
            f"  total: {self.approval.get('total', 0)}",
            "",
            "Incident:",
            f"  active: {self.incident.get('active', 0)}",
            f"  open: {self.incident.get('open', 0)}",
            f"  acknowledged: {self.incident.get('acknowledged', 0)}",
            f"  active critical: {self.incident.get('active_critical', 0)}",
            f"  stale active: {self.incident.get('stale_active', 0)}",
            f"  highest priority: {self.incident.get('highest_priority', '') or 'none'}",
            f"  total: {self.incident.get('total', 0)}",
            f"  next: {self.incident.get('next_action', '') or 'none'}",
            "",
            "Audit:",
            f"  events: {self.audit.get('events_seen', 0)}",
            f"  latest: {self.audit.get('latest_event_type', '') or 'none'}",
            "",
            "Trace:",
            f"  traces: {self.trace.get('traces_seen', 0)}",
            f"  events: {self.trace.get('events_seen', 0)}",
            f"  latest: {self.trace.get('latest_stage', '') or 'none'} / {self.trace.get('latest_status', '') or 'none'}",
            "",
            "Storage:",
            f"  status: {self.storage.get('status', 'unknown')}",
            f"  invalid recent lines: {self.storage.get('invalid_recent_lines', 0)}",
            f"  rotation recommended: {', '.join(self.storage.get('rotation_recommended', [])) or 'none'}",
            "",
            "Bypass:",
            f"  unprotected call sites: {self.bypass.get('unprotected_call_sites', 0)}",
            f"  total call sites: {self.bypass.get('total_call_sites', 0)}",
            "",
            "Registry:",
            f"  persisted patients: {self.registry.get('persisted_patients', 0)}",
            f"  protected persisted: {self.registry.get('protected_persisted', 0)}",
            f"  unprotected persisted: {self.registry.get('unprotected_persisted', 0)}",
            "",
            "Causal:",
            f"  status: {self.causal.get('status', 'unknown')}",
            f"  root cause match: {self.causal.get('harness', {}).get('root_cause_match_rate', 0):.1%}",
            f"  treatment strict match: {self.causal.get('harness', {}).get('treatment_strict_match_rate', 0):.1%}",
            f"  execution chain: {self.causal.get('trace', {}).get('execution_chain_completeness', 0):.1%}",
            f"  diagnostic chain: {self.causal.get('trace', {}).get('diagnostic_chain_completeness', 0):.1%}",
            "",
            "Findings:",
        ]
        if not self.findings:
            lines.append("  none")
        for item in self.findings:
            lines.append(f"  [{item.severity}] {item.area}: {item.message}")
            if item.suggestion:
                lines.append(f"       -> {item.suggestion}")

        lines.append("")
        lines.append("Next actions:")
        for action in self.next_actions:
            lines.append(f"  - {action}")
        return "\n".join(lines)


class MedicSelfControlLayer:
    """Observe-only controller that audits MEDIC's own control signals."""

    def __init__(self, root: Optional[str] = None) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.harness_dir = self.root / "harness_runs"
        self.second_opinion_dir = self.root / "second_opinion_runs"
        self.soak_dir = self.root / "soak_runs"
        self.observe_dir = self.root / "observe_runs"
        self.guard = SelfRepairGuard()
        self.policy = PolicyEngine()
        self.second_opinion_gate = SecondOpinionGate(self.root)
        self.approval_queue = ApprovalQueue(self.root / "control_state" / "approval_queue.jsonl")
        self.incident_queue = IncidentQueue(self.root / "control_state" / "incident_cases.jsonl")
        self.audit_log = AuditLog(self.root / "control_state" / "audit.jsonl")
        self.pipeline_trace = PipelineTrace(self.root / "control_state" / "pipeline_trace.jsonl")
        self.direct_call_detector = DirectTreatmentCallDetector(self.root)
        self.patient_registry = ControlledPatientRegistry(self.root, audit_log=self.audit_log)
        self.causal_builder = CausalReportBuilder(self.root)
        self.storage_health = ControlStorageHealth(self.root)

    def inspect(self) -> MedicSelfReport:
        harness = self._inspect_harness()
        soak = self._inspect_soak()
        observe = self._inspect_observe_loop()
        independence = self._build_independence_snapshot(harness)
        guard = self.guard.stats()
        policy = self.policy.stats()
        second_opinion = self.second_opinion_gate.stats()
        second_opinion_harness = self._inspect_second_opinion_harness()
        approval = self.approval_queue.stats()
        incident = self.incident_queue.triage_report()
        audit = self.audit_log.stats()
        trace = self.pipeline_trace.stats()
        storage = self.storage_health.inspect()
        bypass = self.direct_call_detector.scan()
        registry = self.patient_registry.stats()
        causal = self.causal_builder.build().to_dict()
        role_contract = inspect_role_contract(self.root)

        findings: list[ControlFinding] = []
        findings.extend(self._harness_findings(harness))
        findings.extend(self._soak_findings(soak))
        findings.extend(self._observe_loop_findings(observe))
        findings.extend(self._independence_findings(independence))
        findings.extend(self._guard_findings(guard))
        findings.extend(self._second_opinion_findings(second_opinion))
        findings.extend(self._second_opinion_harness_findings(second_opinion_harness))
        findings.extend(self._policy_findings(policy, approval, audit))
        findings.extend(self._incident_findings(incident))
        findings.extend(self._trace_findings(trace))
        findings.extend(self._storage_findings(storage))
        findings.extend(self._bypass_findings(bypass))
        findings.extend(self._registry_findings(registry))
        findings.extend(self._causal_findings(causal))
        findings.extend(self._role_contract_findings(role_contract))

        status = self._status_from_findings(findings)
        summary = self._summary(status, harness, soak, observe, independence)
        next_actions = self._next_actions(
            findings,
            harness,
            observe,
            independence,
            approval,
            incident,
            audit,
            role_contract,
        )

        return MedicSelfReport(
            status=status,
            generated_at=datetime.now(timezone.utc).isoformat(),
            root=str(self.root),
            summary=summary,
            findings=findings,
            harness=harness,
            soak=soak,
            observe=observe,
            independence=independence,
            guard=guard,
            policy=policy,
            second_opinion=second_opinion,
            second_opinion_harness=second_opinion_harness,
            approval=approval,
            incident=incident,
            audit=audit,
            trace=trace,
            storage=storage,
            bypass=bypass,
            registry=registry,
            causal=causal,
            role_contract=role_contract,
            next_actions=next_actions,
        )

    def _inspect_harness(self) -> dict[str, Any]:
        files = sorted(self.harness_dir.glob("*_summary.json"))
        summaries = [self._load_json(p) for p in files]
        summaries = [s for s in summaries if s]
        latest = summaries[-1] if summaries else {}

        variants = list(latest.get("variants", []) or [])
        reports = list(latest.get("reports", []) or [])
        bias_flags = []
        for variant in variants:
            bias_flags.extend(variant.get("bias_flags", []) or [])
        for report in reports:
            bias_flags.extend(report.get("bias_flags", []) or [])

        worst_variant = ""
        worst_rate = None
        for variant in variants:
            rate = float(variant.get("match_rate", 0.0) or 0.0)
            if worst_rate is None or rate < worst_rate:
                worst_rate = rate
                worst_variant = str(variant.get("variant", "unknown"))

        regressions = list(latest.get("regressions", []) or [])
        mismatch_total = 0
        for report in reports:
            for count in (report.get("mismatch_counts", {}) or {}).values():
                mismatch_total += int(count or 0)

        return {
            "files_seen": len(files),
            "latest_file": files[-1].name if files else "",
            "scenario_set": latest.get("scenario_set", ""),
            "baseline_match_rate": float(latest.get("baseline_match_rate", 0.0) or 0.0),
            "variants": variants,
            "worst_variant": worst_variant,
            "worst_match_rate": worst_rate if worst_rate is not None else 0.0,
            "bias_flags": bias_flags,
            "regressions": regressions,
            "mismatch_total": mismatch_total,
        }

    def _inspect_second_opinion_harness(self) -> dict[str, Any]:
        files = sorted(self.second_opinion_dir.glob("*_summary.json"))
        latest = self._load_json(files[-1]) if files else {}
        return {
            "files_seen": len(files),
            "latest_file": files[-1].name if files else "",
            "scenario_set": latest.get("scenario_set", ""),
            "total_cases": int(latest.get("total_cases", 0) or 0),
            "matched_cases": int(latest.get("matched_cases", 0) or 0),
            "match_rate": float(latest.get("match_rate", 0.0) or 0.0),
            "bias_flags": list(latest.get("bias_flags", []) or []),
            "mismatch_counts": dict(latest.get("mismatch_counts", {}) or {}),
        }

    def _inspect_soak(self) -> dict[str, Any]:
        files = sorted(self.soak_dir.glob("*_summary.json"))
        summaries = [self._load_json(p) for p in files]
        summaries = [s for s in summaries if s]
        observe_only = [s for s in summaries if bool(s.get("observe_only"))]
        control_runs = [s for s in summaries if s.get("kind") == "control_soak"]
        latest_control = control_runs[-1] if control_runs else {}
        approval_events = sum(int(s.get("approval_events", 0) or 0) for s in summaries)
        observe_only_approval_events = sum(
            int(s.get("approval_events", 0) or 0)
            for s in observe_only
        )
        audit_events = sum(int(s.get("audit_events", 0) or 0) for s in summaries)
        treatments: dict[str, int] = {}
        for summary in summaries:
            for key, value in (summary.get("treatment_totals", {}) or {}).items():
                treatments[key] = treatments.get(key, 0) + int(value or 0)

        return {
            "files_seen": len(files),
            "observe_only_runs": len(observe_only),
            "control_runs": len(control_runs),
            "approval_events": approval_events,
            "observe_only_approval_events": observe_only_approval_events,
            "audit_events": audit_events,
            "treatment_totals": treatments,
            "latest_file": files[-1].name if files else "",
            "latest_control_file": Path(latest_control.get("summary_file", "")).name if latest_control.get("summary_file") else "",
            "latest_control_status": latest_control.get("status", ""),
            "latest_control_iterations": int(latest_control.get("iterations", 0) or 0),
            "latest_control_healthy_iterations": int(latest_control.get("healthy_iterations", 0) or 0),
            "latest_control_failed_iterations": int(latest_control.get("failed_iterations", 0) or 0),
            "latest_control_pending_approval": int(latest_control.get("pending_approval_final", 0) or 0),
            "latest_control_failures": list(latest_control.get("failures", []) or []),
            "latest_control_diagnostic_min": float(latest_control.get("diagnostic_min_match_rate", 0.0) or 0.0),
            "latest_control_second_opinion_min": float(latest_control.get("second_opinion_min_match_rate", 0.0) or 0.0),
        }

    def _inspect_observe_loop(self) -> dict[str, Any]:
        files = sorted(self.observe_dir.glob("*_summary.json"))
        loop_summaries = []
        supervisor_summaries = []
        for path in files:
            summary = self._load_json(path)
            patient_id = str(summary.get("target_patient_id", "") if summary else "")
            if (
                summary
                and summary.get("kind") == "observe_loop"
                and not patient_id.startswith("python-service-smoke-")
            ):
                loop_summaries.append((path, summary))
            if summary and summary.get("kind") == "observe_supervisor":
                supervisor_summaries.append((path, summary))
        latest_path, latest = loop_summaries[-1] if loop_summaries else (None, {})
        latest_supervisor_path, latest_supervisor = (
            supervisor_summaries[-1] if supervisor_summaries else (None, {})
        )
        daemon_latest_path = self.observe_dir / "observe_daemon_latest.json"
        daemon_latest = self._load_json(daemon_latest_path) if daemon_latest_path.exists() else {}
        daemon_last_cycle = dict(daemon_latest.get("last_cycle", {}) or {})
        return {
            "files_seen": len(loop_summaries),
            "summary_files_seen": len(files),
            "supervisor_files_seen": len(supervisor_summaries),
            "latest_file": latest_path.name if latest_path else "",
            "latest_status": latest.get("status", ""),
            "latest_patient_status": latest.get("patient_status", ""),
            "latest_target_patient_id": latest.get("target_patient_id", ""),
            "latest_target_patient_type": latest.get("target_patient_type", ""),
            "latest_iterations": int(latest.get("iterations", 0) or 0),
            "latest_successful_iterations": int(latest.get("successful_iterations", 0) or 0),
            "latest_failed_iterations": int(latest.get("failed_iterations", 0) or 0),
            "latest_pending_approval": int(latest.get("pending_approval_final", 0) or 0),
            "latest_severity_counts": dict(latest.get("severity_counts", {}) or {}),
            "latest_root_cause_counts": dict(latest.get("root_cause_counts", {}) or {}),
            "latest_treatment_counts": dict(latest.get("treatment_counts", {}) or {}),
            "latest_failures": list(latest.get("failures", []) or []),
            "latest_supervisor_file": latest_supervisor_path.name if latest_supervisor_path else "",
            "latest_supervisor_status": latest_supervisor.get("status", ""),
            "latest_supervisor_targets_observed": int(latest_supervisor.get("targets_observed", 0) or 0),
            "latest_supervisor_failed_targets": int(latest_supervisor.get("failed_targets", 0) or 0),
            "latest_supervisor_attention_targets": int(latest_supervisor.get("attention_targets", 0) or 0),
            "latest_supervisor_patient_status_counts": dict(
                latest_supervisor.get("patient_status_counts", {}) or {}
            ),
            "latest_daemon_file": daemon_latest_path.name if daemon_latest else "",
            "latest_daemon_status": daemon_latest.get("status", ""),
            "latest_daemon_updated_at": daemon_latest.get("updated_at", ""),
            "latest_daemon_cycles_completed": int(daemon_latest.get("cycles_completed", 0) or 0),
            "latest_daemon_last_alert_count": int(daemon_last_cycle.get("alert_count", 0) or 0),
            "latest_daemon_last_supervisor_file": daemon_last_cycle.get("supervisor_summary_file", ""),
        }

    def _build_independence_snapshot(self, harness: dict[str, Any]) -> dict[str, Any]:
        tracker = MedicIndependenceTracker()
        latest_reports = []
        latest_path = harness.get("latest_file")
        if latest_path:
            latest = self._load_json(self.harness_dir / latest_path)
            latest_reports = list(latest.get("reports", []) or []) if latest else []

        for report in latest_reports:
            for result in list(report.get("results", []) or []):
                treatment = str(result.get("treatment_actual", "unknown"))
                root_cause = str(result.get("root_cause_actual", "unknown"))
                stage = self._stage_from_harness(root_cause, treatment)
                tracker.record(
                    patient_id=str(result.get("patient_id", "harness")),
                    patient_type=self._patient_type_from_scenario(str(result.get("scenario_id", ""))),
                    severity=str(result.get("severity_actual", "")),
                    treatment_type=treatment,
                    stage=stage,
                    success=bool(result.get("matched", False)),
                    confidence=1.0 if result.get("matched") else 0.4,
                    l_vector_hit=stage == Stage.LVECTOR_ONLY,
                )

        stats = tracker.stats()
        stats["source"] = "latest_harness" if latest_reports else "empty"
        return stats

    @staticmethod
    def _stage_from_harness(root_cause: str, treatment: str) -> str:
        cause = root_cause.lower()
        if "l_vector" in cause or "drift" in cause:
            return Stage.LVECTOR_ONLY
        if treatment in {"restart", "monitor", "prompt_patch", "config_change"}:
            return Stage.RULE_HIT
        return Stage.RECORD_HIT

    @staticmethod
    def _patient_type_from_scenario(scenario_id: str) -> str:
        if scenario_id.startswith("ai_"):
            return "ai_model"
        if scenario_id.startswith("python_"):
            return "python_service"
        return "generic_process"

    def _harness_findings(self, harness: dict[str, Any]) -> list[ControlFinding]:
        findings: list[ControlFinding] = []
        if harness["files_seen"] == 0:
            findings.append(ControlFinding(
                "HIGH", "harness", "harness summary가 없습니다.",
                "편향/회귀 기준선을 만들기 위해 core harness를 먼저 실행하세요.",
            ))
            return findings

        if harness["baseline_match_rate"] < 0.95:
            findings.append(ControlFinding(
                "HIGH", "harness",
                f"baseline match rate가 낮습니다 ({harness['baseline_match_rate']:.1%}).",
                "기준 시나리오의 기대값과 처방 매핑을 먼저 고정하세요.",
            ))

        if harness["worst_match_rate"] < harness["baseline_match_rate"] - 0.05:
            findings.append(ControlFinding(
                "MEDIUM", "ablation",
                f"{harness['worst_variant']} variant에서 성능 저하가 있습니다.",
                "꺼진 모듈이 판단에 끼치는 영향을 bias flag로 승격하세요.",
            ))

        if harness["bias_flags"]:
            findings.append(ControlFinding(
                "MEDIUM", "bias",
                f"bias flag {len(harness['bias_flags'])}개가 보고됐습니다.",
                "bias flag별 재현 케이스를 control harness에 고정하세요.",
            ))

        if harness["regressions"]:
            findings.append(ControlFinding(
                "HIGH", "regression",
                f"regression {len(harness['regressions'])}개가 보고됐습니다.",
                "회귀가 있는 variant는 자동 처방 경로에서 제외하세요.",
            ))
        return findings

    @staticmethod
    def _soak_findings(soak: dict[str, Any]) -> list[ControlFinding]:
        findings: list[ControlFinding] = []
        if soak["files_seen"] == 0:
            findings.append(ControlFinding(
                "MEDIUM", "soak", "soak summary가 없습니다.",
                "observe-only soak를 짧게 실행해 audit baseline을 남기세요.",
            ))
        if soak.get("observe_only_approval_events", 0) > 0:
            findings.append(ControlFinding(
                "MEDIUM", "approval",
                "observe-only 실행 중 approval event가 기록됐습니다.",
                "관찰 모드에서 승인 큐가 열리지 않도록 정책을 분리하세요.",
            ))
        if soak.get("control_runs", 0) == 0:
            findings.append(ControlFinding(
                "LOW", "control_soak",
                "control soak summary가 아직 없습니다.",
                "--control-soak를 실행해 반복 안정성 기준선을 남기세요.",
            ))
        if soak.get("latest_control_status") and soak.get("latest_control_status") != "healthy":
            findings.append(ControlFinding(
                "HIGH", "control_soak",
                f"latest control soak status가 {soak.get('latest_control_status')}입니다.",
                "실패 iteration의 causal/self-control/approval 항목을 확인하세요.",
            ))
        if int(soak.get("latest_control_pending_approval", 0) or 0) > 0:
            findings.append(ControlFinding(
                "HIGH", "control_soak",
                "control soak 후 승인 대기 요청이 남아 있습니다.",
                "harness cleanup 또는 approval executor 경로를 점검하세요.",
            ))
        return findings

    @staticmethod
    def _observe_loop_findings(observe: dict[str, Any]) -> list[ControlFinding]:
        findings: list[ControlFinding] = []
        if int(observe.get("files_seen", 0) or 0) == 0:
            findings.append(ControlFinding(
                "LOW", "observe_loop",
                "observe loop summary가 아직 없습니다.",
                "--observe-loop를 실행해 운영 감시 기준선을 남기세요.",
            ))
            return findings

        if observe.get("latest_status") and observe.get("latest_status") != "healthy":
            findings.append(ControlFinding(
                "HIGH", "observe_loop",
                f"latest observe loop status가 {observe.get('latest_status')}입니다.",
                "감시 루프 실패와 pending approval 항목을 먼저 확인하세요.",
            ))

        if int(observe.get("latest_pending_approval", 0) or 0) > 0:
            findings.append(ControlFinding(
                "HIGH", "observe_loop",
                "observe loop 후 승인 대기 요청이 남아 있습니다.",
                "관찰 모드에서 승인 큐가 열렸는지 PolicyEngine 경로를 점검하세요.",
            ))

        if int(observe.get("latest_failed_iterations", 0) or 0) > 0:
            findings.append(ControlFinding(
                "MEDIUM", "observe_loop",
                f"observe loop failed iteration이 {observe.get('latest_failed_iterations')}개 있습니다.",
                "iterations_detail의 trace_id로 실패 지점을 확인하세요.",
            ))

        supervisor_status = str(observe.get("latest_supervisor_status", "") or "")
        if supervisor_status and supervisor_status != "healthy":
            severity = "HIGH" if supervisor_status == "blocked" else "MEDIUM"
            findings.append(ControlFinding(
                severity, "observe_supervisor",
                f"latest observe supervisor status가 {supervisor_status}입니다.",
                "observe_supervisor summary의 target별 result와 trace_id를 확인하세요.",
            ))

        daemon_status = str(observe.get("latest_daemon_status", "") or "")
        if daemon_status and daemon_status != "healthy":
            severity = "HIGH" if daemon_status == "blocked" else "MEDIUM"
            findings.append(ControlFinding(
                severity, "observe_daemon",
                f"latest observe daemon status가 {daemon_status}입니다.",
                "observe_daemon_latest.json과 observe_alerts.jsonl을 확인하세요.",
            ))

        if observe.get("latest_patient_status") in {"attention", "critical"}:
            findings.append(ControlFinding(
                "LOW", "observed_patient",
                f"최근 감시 대상 상태가 {observe.get('latest_patient_status')}입니다.",
                "이 항목은 MEDIC 고장이라기보다 관찰 대상의 상태 신호입니다.",
            ))
        return findings

    @staticmethod
    def _independence_findings(independence: dict[str, Any]) -> list[ControlFinding]:
        score = float(independence.get("independence_score", 0.0) or 0.0)
        if score < 0.70:
            return [ControlFinding(
                "MEDIUM", "independence",
                f"independence score가 낮습니다 ({score:.1%}).",
                "성공한 처방 패턴을 FossilStore에 등록해 SLM 의존도를 낮추세요.",
            )]
        return []

    @staticmethod
    def _guard_findings(guard: dict[str, Any]) -> list[ControlFinding]:
        if float(guard.get("human_above", 0.0) or 0.0) > 0.95:
            return [ControlFinding(
                "LOW", "guard", "사람 승인 임계값이 매우 높습니다.",
                "자기수정 경로를 열기 전 high-risk 승인 기준을 0.9 이하로 유지하세요.",
            )]
        return []

    @staticmethod
    def _second_opinion_findings(second_opinion: dict[str, Any]) -> list[ControlFinding]:
        if not second_opinion.get("enabled"):
            return [ControlFinding(
                "HIGH", "second_opinion",
                "SecondOpinionGate가 비활성 상태입니다.",
                "고위험 처방은 2차 소견 게이트를 통과해야 합니다.",
            )]
        if "patch_code" not in second_opinion.get("always_required_treatments", []):
            return [ControlFinding(
                "HIGH", "second_opinion",
                "patch_code가 필수 2차 소견 대상이 아닙니다.",
                "코드 패치는 항상 2차 소견 대상으로 유지하세요.",
            )]
        return []

    @staticmethod
    def _second_opinion_harness_findings(harness: dict[str, Any]) -> list[ControlFinding]:
        if int(harness.get("files_seen", 0) or 0) == 0:
            return [ControlFinding(
                "LOW", "second_opinion_harness",
                "SecondOpinionGate harness summary가 아직 없습니다.",
                "--second-opinion-harness를 실행해 차단/승인대기 회귀 기준선을 남기세요.",
            )]
        if float(harness.get("match_rate", 0.0) or 0.0) < 0.95:
            return [ControlFinding(
                "HIGH", "second_opinion_harness",
                f"SecondOpinionGate harness match rate가 낮습니다 ({harness.get('match_rate', 0):.1%}).",
                "위험 패치 차단과 승인 큐 전환 기대값을 먼저 고정하세요.",
            )]
        if harness.get("bias_flags"):
            return [ControlFinding(
                "MEDIUM", "second_opinion_harness",
                f"SecondOpinionGate bias flag {len(harness.get('bias_flags', []))}개가 있습니다.",
                "flag별 재현 케이스를 harness에 고정하세요.",
            )]
        return []

    @staticmethod
    def _policy_findings(
        policy: dict[str, Any],
        approval: dict[str, Any],
        audit: dict[str, Any],
    ) -> list[ControlFinding]:
        findings: list[ControlFinding] = []
        if policy.get("allow_medium_auto"):
            findings.append(ControlFinding(
                "MEDIUM", "policy",
                "medium-risk 자동 승인이 켜져 있습니다.",
                "외부 감독자 모드에서는 approval queue로 보내는 기본값을 유지하세요.",
            ))
        if int(approval.get("pending", 0) or 0) > 0:
            findings.append(ControlFinding(
                "MEDIUM", "approval",
                f"승인 대기 처방이 {approval.get('pending')}개 있습니다.",
                "자동 실행 전에 승인/거부 결정을 먼저 기록하세요.",
            ))
        if int(audit.get("events_seen", 0) or 0) == 0:
            findings.append(ControlFinding(
                "LOW", "audit",
                "control audit log가 아직 비어 있습니다.",
                "첫 실제 정책 판정부터 AuditLog.record()로 남기세요.",
            ))
        return findings

    @staticmethod
    def _incident_findings(incident: dict[str, Any]) -> list[ControlFinding]:
        active = int(incident.get("active", 0) or 0)
        active_critical = int(incident.get("active_critical", 0) or 0)
        stale_active = int(incident.get("stale_active", 0) or 0)
        if active_critical > 0:
            return [ControlFinding(
                "MEDIUM", "incident",
                f"활성 critical incident가 {active_critical}개 있습니다.",
                "자동 실행을 열기 전에 --incident-list active로 확인하고 ack/resolve 결정을 남기세요.",
            )]
        if stale_active > 0:
            return [ControlFinding(
                "MEDIUM", "incident",
                f"오래 열린 incident가 {stale_active}개 있습니다.",
                "--incident-triage로 우선순위를 확인하고 resolve/reject/refresh 결정을 남기세요.",
            )]
        if active > 0:
            return [ControlFinding(
                "LOW", "incident",
                f"활성 incident가 {active}개 있습니다.",
                "감시 알림이 사건 카드로 남아 있으니 운영 판단 기록을 이어가세요.",
            )]
        return []

    @staticmethod
    def _trace_findings(trace: dict[str, Any]) -> list[ControlFinding]:
        if int(trace.get("events_seen", 0) or 0) == 0:
            return [ControlFinding(
                "LOW", "trace",
                "pipeline trace가 아직 비어 있습니다.",
                "ControlledTreatmentRunner를 통해 observe/apply smoke를 남기세요.",
            )]
        return []

    @staticmethod
    def _storage_findings(storage: dict[str, Any]) -> list[ControlFinding]:
        findings: list[ControlFinding] = []
        invalid = int(storage.get("invalid_recent_lines", 0) or 0)
        if invalid:
            findings.append(ControlFinding(
                "HIGH", "storage",
                f"control-state JSONL에 파싱 실패 라인 {invalid}개가 있습니다.",
                "깨진 라인을 보존 백업한 뒤 approval/audit/trace 저장소를 복구하세요.",
            ))

        rotation = list(storage.get("rotation_recommended", []) or [])
        if rotation:
            findings.append(ControlFinding(
                "MEDIUM", "storage",
                "control-state 로그 회전이 필요한 파일이 있습니다.",
                "운영 전 archive/rotation 정책을 정하고 오래된 JSONL을 분리 보관하세요.",
            ))
        return findings

    @staticmethod
    def _bypass_findings(bypass: dict[str, Any]) -> list[ControlFinding]:
        count = int(bypass.get("unprotected_call_sites", 0) or 0)
        if count == 0:
            return []
        examples = bypass.get("call_sites", [])[:3]
        example_text = ", ".join(
            f"{site.get('file')}:{site.get('line')}"
            for site in examples
            if not site.get("allowed")
        )
        return [ControlFinding(
            "HIGH", "bypass",
            f"apply_treatment 직접 호출 지점 {count}개가 통제 러너 밖에 있습니다.",
            f"ControlledTreatmentRunner로 우회 호출을 교체하세요. {example_text}",
        )]

    @staticmethod
    def _registry_findings(registry: dict[str, Any]) -> list[ControlFinding]:
        unprotected = int(registry.get("unprotected_persisted", 0) or 0)
        if unprotected > 0:
            return [ControlFinding(
                "HIGH", "registry",
                f"보호되지 않은 등록 환자 {unprotected}개가 있습니다.",
                "모든 환자는 ControlledPatientRegistry.register()로 다시 등록하세요.",
            )]
        if int(registry.get("persisted_patients", 0) or 0) == 0:
            return [ControlFinding(
                "LOW", "registry",
                "아직 통제 등록소에 환자가 없습니다.",
                "운영 루프를 붙이기 전에 환자 등록 smoke를 한 번 실행하세요.",
            )]
        return []

    @staticmethod
    def _causal_findings(causal: dict[str, Any]) -> list[ControlFinding]:
        status = causal.get("status", "unknown")
        if status == "healthy":
            return []
        findings = []
        for item in causal.get("findings", [])[:4]:
            severity = item.get("severity", "LOW")
            area = f"causal:{item.get('area', 'unknown')}"
            findings.append(ControlFinding(
                severity,
                area,
                item.get("message", ""),
                item.get("suggestion", ""),
            ))
        return findings

    @staticmethod
    def _role_contract_findings(role_contract: dict[str, Any]) -> list[ControlFinding]:
        findings: list[ControlFinding] = []
        for item in list(role_contract.get("violations", []) or []):
            findings.append(ControlFinding(
                str(item.get("severity", "HIGH") or "HIGH"),
                f"role_contract:{item.get('area', 'unknown')}",
                str(item.get("message", "")),
                str(item.get("suggestion", "")),
            ))
        return findings

    @staticmethod
    def _status_from_findings(findings: list[ControlFinding]) -> str:
        severities = {f.severity for f in findings}
        if "HIGH" in severities:
            return "blocked"
        if "MEDIUM" in severities:
            return "warning"
        return "healthy"

    @staticmethod
    def _summary(
        status: str,
        harness: dict[str, Any],
        soak: dict[str, Any],
        observe: dict[str, Any],
        independence: dict[str, Any],
    ) -> str:
        if status == "healthy":
            return "MEDIC control signals are internally consistent in observe-only mode."
        return (
            "MEDIC control layer is present, but should remain observe-only until "
            f"harness={harness.get('baseline_match_rate', 0):.1%}, "
            f"soak_files={soak.get('files_seen', 0)}, "
            f"observe_files={observe.get('files_seen', 0)}, "
            f"independence={independence.get('independence_score', 0):.1%} are stable."
        )

    @staticmethod
    def _next_actions(
        findings: list[ControlFinding],
        harness: dict[str, Any],
        observe: dict[str, Any],
        independence: dict[str, Any],
        approval: dict[str, Any],
        incident: dict[str, Any],
        audit: dict[str, Any],
        role_contract: dict[str, Any],
    ) -> list[str]:
        actions = [
            "Treat MEDIC as an external control agent, not an unrestricted autonomous repair agent.",
            "Keep MEDIC self-control in observe-only mode until a fresh harness run passes.",
            "Route all medium/high-risk prescriptions through PolicyEngine before any patient can apply them.",
        ]
        if role_contract.get("status") != "healthy":
            actions.append("Restore medic_role_contract.json before enabling any execution path.")
        if harness.get("files_seen", 0) > 0:
            actions.append("Promote latest harness scenarios into a repeatable self-bias regression suite.")
        if int(observe.get("files_seen", 0) or 0) == 0:
            actions.append("Run --observe-loop once to create the first operational watch baseline.")
        if float(independence.get("independence_score", 0.0) or 0.0) >= 0.85:
            actions.append("Allow rule_hit and lvector_only findings to bypass SLM review, but keep SelfRepairGuard active.")
        if any(f.area == "bias" for f in findings):
            actions.append("Create one explicit scenario per bias flag before enabling automatic repair.")
        if int(approval.get("pending", 0) or 0) > 0:
            actions.append("Drain pending approval requests before enabling non-observe execution.")
        if int(incident.get("stale_active", 0) or 0) > 0:
            actions.append("Triage stale incidents before enabling non-observe execution.")
        if int(incident.get("active", 0) or 0) > 0:
            actions.append("Review active incidents and record acknowledge/resolve decisions before execution.")
        if int(audit.get("events_seen", 0) or 0) == 0:
            actions.append("Record a policy dry-run audit event during the next control-layer integration test.")
        return actions

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
