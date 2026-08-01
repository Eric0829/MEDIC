#!/usr/bin/env python3
"""MEDIC self-control CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from control.approval_executor import ApprovedTreatmentExecutor
from control.approval_queue import ApprovalQueue, ApprovalRequest
from control.audit_log import AuditLog
from control.benchmark_suite import MedicBenchmarkSuiteRunner
from control.causal_report import CausalReportBuilder
from control.control_gateway import ControlGateway
from control.control_soak import ControlSoakRunner
from control.controlled_registry import ControlledPatientRegistry
from control.diagnostic_harness import DiagnosticHarnessRunner
from control.diagnostic_runner import ControlledDiagnosticRunner
from control.incident_queue import DEFAULT_STALE_AFTER_SECONDS, IncidentCase, IncidentQueue
from control.observe_daemon import (
    ObserveDaemonRunner,
    read_observe_alerts,
    read_observe_daemon_status,
    write_observe_daemon_config_template,
)
from control.observe_loop import ObserveLoopRunner
from control.observe_soak import ObserveSoakRunner
from control.observe_config import write_observe_config_template
from control.observe_supervisor import ObserveSupervisorRunner
from control.observe_targets import build_observe_patient
from control.operator_brief import DEFAULT_DAEMON_STALE_AFTER_SECONDS, OperatorBriefBuilder
from control.patient_proxy import ControlledPatientProxy
from control.pipeline_trace import PipelineTrace
from control.python_service_smoke import PythonServiceSmokeRunner
from control.role_contract import (
    inspect_role_contract,
    render_role_contract_text,
    write_role_contract_template,
)
from control.self_control_layer import MedicSelfControlLayer
from control.second_opinion_harness import SecondOpinionHarnessRunner
from control.storage_health import ControlStorageHealth
from control.treatment_runner import ControlledTreatmentRunner
from patient_registry.base_patient import PatientType, Prescription, TreatmentResult, TreatmentType, Vitals


class _ControlSmokePatient:
    def __init__(self) -> None:
        self.applied = 0

    @property
    def patient_id(self) -> str:
        return "medic-self"

    @property
    def patient_type(self) -> PatientType:
        return PatientType.AI_MODEL

    async def collect_vitals(self) -> Vitals:
        return Vitals(
            patient_id=self.patient_id,
            patient_type=self.patient_type,
            is_alive=True,
            cpu_percent=1.0,
            memory_percent=1.0,
            error_rate=0.0,
            latency_p99_ms=1.0,
        )

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        self.applied += 1
        before = await self.collect_vitals()
        after = await self.collect_vitals()
        return TreatmentResult(
            prescription_id=prescription.prescription_id,
            patient_id=self.patient_id,
            success=True,
            message=f"smoke treatment accepted ({self.applied})",
            before_vitals=before,
            after_vitals=after,
        )

    async def report_health(self) -> bool:
        return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect MEDIC's observe-only self-control signals."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent),
        help="MEDIC root directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON",
    )
    parser.add_argument(
        "--control-smoke",
        action="store_true",
        help="run one observe-only Guard -> Policy -> Audit smoke review",
    )
    parser.add_argument(
        "--operator-brief",
        action="store_true",
        help="show a one-screen operator brief across self-control, incidents, approvals, daemon, storage, and causal signals",
    )
    parser.add_argument(
        "--daily-check",
        action="store_true",
        help="show the recommended daily operator check in one concise report",
    )
    parser.add_argument(
        "--brief-alert-limit",
        type=int,
        default=5,
        help="recent daemon alerts to include in --operator-brief",
    )
    parser.add_argument(
        "--brief-approval-limit",
        type=int,
        default=5,
        help="approval rows to include in --operator-brief",
    )
    parser.add_argument(
        "--brief-incident-limit",
        type=int,
        default=5,
        help="active incident rows to include in --operator-brief",
    )
    parser.add_argument(
        "--brief-daemon-stale-after",
        type=float,
        default=DEFAULT_DAEMON_STALE_AFTER_SECONDS,
        help="seconds before daemon heartbeat is considered stale in --operator-brief",
    )
    parser.add_argument(
        "--runner-smoke",
        action="store_true",
        help="run one ControlledTreatmentRunner smoke review",
    )
    parser.add_argument(
        "--bypass-smoke",
        action="store_true",
        help="prove direct proxy apply_treatment is blocked while runner execution works",
    )
    parser.add_argument(
        "--registry-smoke",
        action="store_true",
        help="register a patient through ControlledPatientRegistry and run it safely",
    )
    parser.add_argument(
        "--approval-list",
        nargs="?",
        const="pending",
        choices=["pending", "approved", "rejected", "executed", "execution_failed", "all"],
        help="list approval requests, defaulting to pending",
    )
    parser.add_argument(
        "--approval-show",
        metavar="REQUEST_ID",
        help="show one approval request",
    )
    parser.add_argument(
        "--approval-approve",
        metavar="REQUEST_ID",
        help="approve one pending approval request",
    )
    parser.add_argument(
        "--approval-reject",
        metavar="REQUEST_ID",
        help="reject one pending approval request",
    )
    parser.add_argument(
        "--approval-note",
        default="",
        help="decision note for approval approve/reject",
    )
    parser.add_argument(
        "--approval-by",
        default="human",
        help="decision actor for approval approve/reject",
    )
    parser.add_argument(
        "--approval-smoke",
        action="store_true",
        help="queue and close one high-risk approval request for smoke testing",
    )
    parser.add_argument(
        "--approval-execute",
        metavar="REQUEST_ID",
        help="execute one approved request using the current runtime registry",
    )
    parser.add_argument(
        "--approval-execute-smoke",
        action="store_true",
        help="queue, approve, and execute one safe high-risk request in-process",
    )
    parser.add_argument(
        "--causal-report",
        action="store_true",
        help="print MEDIC causal metrics from harness, trace, approval, and audit data",
    )
    parser.add_argument(
        "--role-contract",
        action="store_true",
        help="inspect MEDIC's role and safety contract",
    )
    parser.add_argument(
        "--role-contract-path",
        default="",
        help="optional JSON path for --role-contract",
    )
    parser.add_argument(
        "--role-contract-template",
        metavar="PATH",
        help="write an example MEDIC role contract to PATH",
    )
    parser.add_argument(
        "--storage-health",
        action="store_true",
        help="inspect control-state JSONL parse health and lock paths",
    )
    parser.add_argument(
        "--storage-repair",
        action="store_true",
        help="backup and remove malformed control-state JSONL lines",
    )
    parser.add_argument(
        "--diagnostic-smoke",
        action="store_true",
        help="run collect_vitals -> diagnose -> prescribe -> gateway trace smoke",
    )
    parser.add_argument(
        "--diagnostic-harness",
        action="store_true",
        help="run the 30+ case diagnostic/control causal harness",
    )
    parser.add_argument(
        "--second-opinion-smoke",
        action="store_true",
        help="prove high-risk patches pass through SecondOpinionGate before approval/execution",
    )
    parser.add_argument(
        "--second-opinion-harness",
        action="store_true",
        help="run the SecondOpinionGate regression harness",
    )
    parser.add_argument(
        "--control-soak",
        action="store_true",
        help="repeat diagnostic/second-opinion/causal/self-control checks",
    )
    parser.add_argument(
        "--benchmark-suite",
        action="store_true",
        help="run staged internal, external, adversarial, real-target, and soak benchmarks",
    )
    parser.add_argument(
        "--observe-soak",
        action="store_true",
        help="run a bounded observe daemon soak and write a soak summary",
    )
    parser.add_argument(
        "--observe-loop",
        action="store_true",
        help="run a repeated observe-only diagnostic watch loop",
    )
    parser.add_argument(
        "--observe-supervisor",
        action="store_true",
        help="run all observe targets from a JSON config",
    )
    parser.add_argument(
        "--observe-daemon",
        action="store_true",
        help="run observe supervisor continuously in the foreground",
    )
    parser.add_argument(
        "--observe-daemon-status",
        action="store_true",
        help="show latest observe daemon status",
    )
    parser.add_argument(
        "--observe-alerts",
        nargs="?",
        const=20,
        type=int,
        help="show recent observe daemon alerts, default 20",
    )
    parser.add_argument(
        "--incident-list",
        nargs="?",
        const="active",
        choices=["active", "open", "acknowledged", "resolved", "rejected", "all"],
        help="list incident cases, defaulting to active",
    )
    parser.add_argument(
        "--incident-show",
        metavar="INCIDENT_ID",
        help="show one incident case",
    )
    parser.add_argument(
        "--incident-ack",
        metavar="INCIDENT_ID",
        help="acknowledge one open incident case",
    )
    parser.add_argument(
        "--incident-resolve",
        metavar="INCIDENT_ID",
        help="resolve one active incident case",
    )
    parser.add_argument(
        "--incident-reject",
        metavar="INCIDENT_ID",
        help="reject one active incident case as false alarm/not actionable",
    )
    parser.add_argument(
        "--incident-note",
        default="",
        help="decision note for incident ack/resolve/reject",
    )
    parser.add_argument(
        "--incident-by",
        default="human",
        help="decision actor for incident ack/resolve/reject",
    )
    parser.add_argument(
        "--incident-stats",
        action="store_true",
        help="show incident queue counts",
    )
    parser.add_argument(
        "--incident-triage",
        action="store_true",
        help="show incident urgency, stale-active counts, and next operator action",
    )
    parser.add_argument(
        "--incident-stale-after",
        type=float,
        default=DEFAULT_STALE_AFTER_SECONDS,
        help="seconds before an active incident is considered stale",
    )
    parser.add_argument(
        "--incident-limit",
        type=int,
        default=10,
        help="maximum active incident cases to show in triage output",
    )
    parser.add_argument(
        "--observe-config",
        default="",
        help="JSON config path for --observe-supervisor",
    )
    parser.add_argument(
        "--observe-daemon-config",
        default="",
        help="JSON daemon config path for --observe-daemon",
    )
    parser.add_argument(
        "--observe-config-template",
        metavar="PATH",
        help="write an example observe target config to PATH",
    )
    parser.add_argument(
        "--observe-daemon-template",
        metavar="PATH",
        help="write an example observe daemon config to PATH",
    )
    parser.add_argument(
        "--python-service-smoke",
        action="store_true",
        help="start a local /health service and observe it through python-service target",
    )
    parser.add_argument(
        "--observe-target",
        choices=["medic-self", "system", "python-service"],
        default="medic-self",
        help="target to observe, default medic-self",
    )
    parser.add_argument(
        "--observe-patient-id",
        default="",
        help="patient id for system/python-service observe targets",
    )
    parser.add_argument(
        "--observe-service-url",
        default="",
        help="base URL for python-service observe target",
    )
    parser.add_argument(
        "--observe-source-root",
        default="",
        help="source root for python-service observe target",
    )
    parser.add_argument(
        "--observe-health-path",
        default="/health",
        help="health path for python-service observe target",
    )
    parser.add_argument(
        "--observe-pid",
        type=int,
        default=None,
        help="optional process id for python-service metrics",
    )
    parser.add_argument(
        "--observe-watch-process",
        default="",
        help="comma-separated process names for system observe target",
    )
    parser.add_argument(
        "--observe-disk-path",
        default="",
        help="disk path for system observe target",
    )
    parser.add_argument(
        "--observe-iterations",
        type=int,
        default=3,
        help="number of observe-loop iterations, default 3",
    )
    parser.add_argument(
        "--observe-interval",
        type=float,
        default=0.0,
        help="seconds to wait between observe-loop iterations, default 0",
    )
    parser.add_argument(
        "--observe-cycles",
        type=int,
        default=1,
        help="number of observe-supervisor cycles, default 1",
    )
    parser.add_argument(
        "--observe-cycle-interval",
        type=float,
        default=0.0,
        help="seconds to wait between observe-supervisor cycles, default 0",
    )
    parser.add_argument(
        "--daemon-interval",
        type=float,
        default=None,
        help="override observe daemon interval seconds",
    )
    parser.add_argument(
        "--daemon-max-cycles",
        type=int,
        default=None,
        help="override daemon max cycles; 0 means run until interrupted",
    )
    parser.add_argument(
        "--daemon-stop-on-blocked",
        action="store_true",
        default=None,
        help="stop the daemon after a blocked supervisor cycle",
    )
    parser.add_argument(
        "--soak-iterations",
        type=int,
        default=3,
        help="number of control soak iterations, default 3",
    )
    parser.add_argument(
        "--observe-soak-cycles",
        type=int,
        default=3,
        help="number of observe soak cycles, default 3",
    )
    parser.add_argument(
        "--observe-soak-interval",
        type=float,
        default=1.0,
        help="seconds between observe soak cycles, default 1",
    )
    parser.add_argument(
        "--observe-soak-stop-on-blocked",
        action="store_true",
        help="stop observe soak after a blocked cycle",
    )
    parser.add_argument(
        "--benchmark-external-cases",
        default="",
        help="JSONL external diagnostic benchmark cases",
    )
    parser.add_argument(
        "--benchmark-attack-cases",
        default="",
        help="JSONL adversarial gateway benchmark cases",
    )
    parser.add_argument(
        "--benchmark-control-iterations",
        type=int,
        default=1,
        help="control soak iterations for --benchmark-suite, default 1",
    )
    parser.add_argument(
        "--benchmark-observe-cycles",
        type=int,
        default=2,
        help="observe soak cycles for --benchmark-suite, default 2",
    )
    parser.add_argument(
        "--benchmark-observe-interval",
        type=float,
        default=0.0,
        help="seconds between observe benchmark cycles, default 0",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="allow runner/diagnostic smoke to execute its safe monitor treatment",
    )
    args = parser.parse_args()
    if args.json:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        logging.disable(logging.CRITICAL)

    if args.daily_check:
        result = _build_daily_check(args)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_daily_check(result)
        return 0

    if args.operator_brief:
        brief = OperatorBriefBuilder(args.root).build(
            observe_daemon_config=args.observe_daemon_config,
            incident_stale_after_seconds=args.incident_stale_after,
            daemon_stale_after_seconds=args.brief_daemon_stale_after,
            incident_limit=args.brief_incident_limit,
            approval_limit=args.brief_approval_limit,
            alert_limit=args.brief_alert_limit,
        )
        if args.json:
            print(json.dumps(brief.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(brief.render_text())
        return 0

    if args.causal_report:
        report = CausalReportBuilder(args.root).build()
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(report.render_text())
        return 0

    if args.role_contract_template:
        result = write_role_contract_template(args.role_contract_template, args.root)
        _print_or_json(result, args.json)
        return 0

    if args.role_contract:
        report = inspect_role_contract(args.root, args.role_contract_path)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_role_contract_text(report))
        return 0

    if args.storage_health:
        result = ControlStorageHealth(args.root).inspect()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Storage Health")
            print(f"status: {result['status']}")
            print(f"invalid recent lines: {result['invalid_recent_lines']}")
            print(f"rotation recommended: {', '.join(result['rotation_recommended']) or 'none'}")
            for name, store in result["stores"].items():
                print(
                    f"{name}: lines={store['total_nonempty_lines']} "
                    f"invalid={store['invalid_recent_lines']} "
                    f"size={store['size_bytes']} bytes"
                )
        return 0

    if args.storage_repair:
        result = ControlStorageHealth(args.root).repair()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Storage Repair")
            print(f"status: {result['status']}")
            print(f"removed lines: {result['removed_lines']}")
            print(f"backup dir: {result['backup_dir']}")
            print(f"after: {result['after']['status']}")
        return 0

    if args.approval_smoke:
        result = asyncio.run(_run_approval_smoke(args.root))
        _print_or_json(result, args.json)
        return 0

    if args.approval_execute_smoke:
        result = asyncio.run(_run_approval_execute_smoke(args.root))
        _print_or_json(result, args.json)
        return 0

    if args.approval_execute:
        result = asyncio.run(_execute_approved_request(args.root, args.approval_execute))
        _print_or_json(result, args.json)
        return 0

    if args.diagnostic_smoke:
        result = asyncio.run(
            _run_diagnostic_smoke(args.root, observe_only=not args.execute)
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Diagnostic Pipeline Smoke")
            print(f"status: {result['status']}")
            print(f"trace_id: {result['trace_id']}")
            print(f"diagnosis: {result['diagnosis']['severity']} / {result['diagnosis']['root_cause']}")
            print(f"prescription: {result['prescription']['treatment_type']}")
            print(f"runner: {result['runner']['status']}")
        return 0

    if args.diagnostic_harness:
        result = asyncio.run(_run_diagnostic_harness(args.root))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            baseline = next(
                report for report in result["reports"]
                if report["variant"] == result["baseline_variant"]
            )
            print("MEDIC Diagnostic Harness")
            print(f"summary: {result['summary_file']}")
            print(f"cases: {baseline['total_cases']}")
            print(f"matched: {baseline['matched_cases']}/{baseline['total_cases']}")
            print(f"baseline match: {result['baseline_match_rate']:.1%}")
            print(f"bias flags: {sum(len(v['bias_flags']) for v in result['variants'])}")
        return 0

    if args.second_opinion_smoke:
        result = asyncio.run(_run_second_opinion_smoke(args.root))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Second Opinion Smoke")
            for name, row in result["cases"].items():
                second = row["second_opinion"]
                print(
                    f"{name}: gateway={row['status']} "
                    f"second={second['final_verdict']} policy={row['policy']['action']}"
                )
        return 0

    if args.second_opinion_harness:
        result = asyncio.run(_run_second_opinion_harness(args.root))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Second Opinion Harness")
            print(f"summary: {result['summary_file']}")
            print(f"cases: {result['total_cases']}")
            print(f"matched: {result['matched_cases']}/{result['total_cases']}")
            print(f"match: {result['match_rate']:.1%}")
            print(f"bias flags: {len(result['bias_flags'])}")
        return 0

    if args.control_soak:
        result = asyncio.run(_run_control_soak(args.root, args.soak_iterations))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Control Soak")
            print(f"summary: {result['summary_file']}")
            print(f"status: {result['status']}")
            print(f"iterations: {result['healthy_iterations']}/{result['iterations']} healthy")
            print(f"diagnostic min: {result['diagnostic_min_match_rate']:.1%}")
            print(f"second opinion min: {result['second_opinion_min_match_rate']:.1%}")
            print(f"pending approval: {result['pending_approval_final']}")
            print(f"failures: {len(result['failures'])}")
        return 0

    if args.benchmark_suite:
        result = asyncio.run(_run_benchmark_suite(args))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Benchmark Suite")
            print(f"summary: {result['summary_file']}")
            print(f"status: {result['status']}")
            print(f"stages: {result['healthy_stages']}/{result['stage_count']} healthy")
            print(f"short probes: {result['short_probe_stages']}")
            for stage in list(result.get("stages", []) or []):
                matched = stage.get("matched_cases", stage.get("healthy_cycles", ""))
                total = stage.get("total_cases", stage.get("requested_cycles", ""))
                if stage.get("stage") == 1:
                    metrics = dict(stage.get("metrics", {}) or {})
                    matched = metrics.get("diagnostic_cases", "")
                    total = metrics.get("diagnostic_cases", "")
                print(
                    f"  stage {stage.get('stage')}: {stage.get('name')} "
                    f"{stage.get('status')} "
                    f"matched={matched}/{total}"
                )
            for note in list(result.get("notes", []) or []):
                print(f"note: {note}")
        return 0

    if args.observe_soak:
        result = asyncio.run(
            _run_observe_soak(
                args.root,
                config_path=args.observe_daemon_config,
                cycles=args.observe_soak_cycles,
                interval_seconds=args.observe_soak_interval,
                stop_on_blocked=args.observe_soak_stop_on_blocked,
            )
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Observe Soak")
            print(f"summary: {result['summary_file']}")
            print(f"status: {result['status']}")
            print(f"cycles: {result['healthy_cycles']}/{result['requested_cycles']} healthy")
            print(f"alerts: {result['alert_count']}")
            print(f"active incidents: {result['active_incidents']}")
            print(f"approval events: {result['approval_events']}")
            print(f"failures: {len(result['failures'])}")
        return 0

    if args.python_service_smoke:
        result = asyncio.run(_run_python_service_smoke(args.root))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Python Service Smoke")
            print(f"summary: {result['summary_file']}")
            print(f"status: {result['status']}")
            print(f"matched: {result['matched_cases']}/{result['total_cases']}")
            print(f"failures: {len(result['failures'])}")
        return 0

    if args.observe_config_template:
        result = write_observe_config_template(args.observe_config_template, args.root)
        _print_or_json(result, args.json)
        return 0

    if args.observe_daemon_template:
        result = write_observe_daemon_config_template(args.observe_daemon_template, args.root)
        _print_or_json(result, args.json)
        return 0

    if args.observe_daemon_status:
        result = read_observe_daemon_status(
            args.root,
            config_path=args.observe_daemon_config,
            alert_limit=args.observe_alerts or 20,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Observe Daemon Status")
            print(f"status: {result['status']}")
            print(f"updated: {result['updated_at'] or 'none'}")
            print(f"cycles: {result['cycles_completed']}")
            print(f"latest: {result['latest_path']}")
            print(f"recent alerts: {result['recent_alert_count']} / {result['total_alert_lines']}")
            incident = result.get("incident", {}) or {}
            print(f"active incidents: {incident.get('active', 0)}")
            print(f"incident triage: {incident.get('status', 'unknown')}")
            process = result.get("process", {}) or {}
            print(
                f"daemon process: {process.get('status', 'unknown')} "
                f"count={process.get('count', 0)}"
            )
            last = result.get("last_cycle", {}) or {}
            if last:
                print(f"last cycle: {last.get('status')} targets={last.get('targets_observed')} alerts={last.get('alert_count')}")
        return 0

    if args.observe_alerts is not None:
        result = read_observe_alerts(
            args.root,
            config_path=args.observe_daemon_config,
            limit=args.observe_alerts,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Observe Alerts")
            print(f"status: {result['status']}")
            print(f"path: {result['path']}")
            print(f"alerts: {len(result['alerts'])} / {result['total_lines']}")
            for alert in result["alerts"]:
                print(
                    f"{alert.get('created_at', '')} "
                    f"{alert.get('severity', ''):<8} "
                    f"{alert.get('target_name', '')}: "
                    f"{alert.get('message', '')}"
                )
        return 0

    if args.observe_daemon:
        result = asyncio.run(
            _run_observe_daemon(
                args.root,
                config_path=args.observe_daemon_config,
                interval_seconds=args.daemon_interval,
                max_cycles=args.daemon_max_cycles,
                stop_on_blocked=args.daemon_stop_on_blocked,
            )
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Observe Daemon")
            print(f"summary: {result['summary_file']}")
            print(f"status: {result['status']}")
            print(f"cycles: {result['cycles_completed']}")
            print(f"stop reason: {result['stop_reason']}")
            print(f"latest: {result['latest_path']}")
            print(f"alerts: {result['alert_path']}")
            print(f"incidents: {result['incident_path']}")
        return 0

    if args.observe_supervisor:
        result = asyncio.run(
            _run_observe_supervisor(
                args.root,
                config_path=args.observe_config,
                cycles=args.observe_cycles,
                cycle_interval_seconds=args.observe_cycle_interval,
            )
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Observe Supervisor")
            print(f"summary: {result['summary_file']}")
            print(f"status: {result['status']}")
            print(f"config: {result['config']['source']}")
            print(f"cycles: {result['cycles']}")
            print(f"targets: {result['targets_observed']} observed / {result['targets_enabled']} enabled")
            print(f"patient status: {result['patient_status_counts']}")
            print(f"failed targets: {result['failed_targets']}")
        return 0

    if args.observe_loop:
        result = asyncio.run(
            _run_observe_loop(
                args.root,
                target=args.observe_target,
                iterations=args.observe_iterations,
                interval_seconds=args.observe_interval,
                patient_id=args.observe_patient_id,
                service_url=args.observe_service_url,
                source_root=args.observe_source_root,
                health_path=args.observe_health_path,
                pid=args.observe_pid,
                watch_processes=args.observe_watch_process,
                disk_path=args.observe_disk_path,
            )
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Observe Loop")
            print(f"summary: {result['summary_file']}")
            print(f"status: {result['status']}")
            print(f"patient: {result['target_patient_id']} ({result['target_patient_type']})")
            print(f"patient status: {result['patient_status']}")
            print(f"iterations: {result['successful_iterations']}/{result['iterations']} observed")
            print(f"severity: {result['severity_counts']}")
            print(f"treatments: {result['treatment_counts']}")
            print(f"pending approval: {result['pending_approval_final']}")
        return 0

    if args.incident_stats:
        result = _incident_queue(args.root).stats()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Incidents")
            print(f"active: {result['active']}")
            print(f"open: {result['open']}")
            print(f"acknowledged: {result['acknowledged']}")
            print(f"resolved: {result['resolved']}")
            print(f"rejected: {result['rejected']}")
            print(f"active critical: {result['active_critical']}")
            print(f"total: {result['total']}")
        return 0

    if args.incident_triage:
        result = _incident_queue(args.root).triage_report(
            stale_after_seconds=args.incident_stale_after,
            limit=args.incident_limit,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_incident_triage(result)
        return 0

    if args.incident_show:
        item = _incident_queue(args.root).get(args.incident_show)
        _print_or_json({"incident": _incident_case_dict(item)}, args.json)
        return 0

    if args.incident_ack or args.incident_resolve or args.incident_reject:
        incident_id = args.incident_ack or args.incident_resolve or args.incident_reject
        status = "acknowledged"
        if args.incident_resolve:
            status = "resolved"
        if args.incident_reject:
            status = "rejected"
        result = _decide_incident(
            args.root,
            incident_id,
            status,
            decided_by=args.incident_by,
            note=args.incident_note,
        )
        _print_or_json(result, args.json)
        return 0

    if args.incident_list:
        queue = _incident_queue(args.root)
        rows = queue.list(status=args.incident_list)
        result = {
            "status": args.incident_list,
            "count": len(rows),
            "incidents": [_incident_case_dict(row) for row in rows],
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_incident_rows(rows)
        return 0

    if args.approval_show:
        queue = _approval_queue(args.root)
        item = queue.get(args.approval_show)
        _print_or_json(_approval_request_dict(item), args.json)
        return 0

    if args.approval_approve or args.approval_reject:
        request_id = args.approval_approve or args.approval_reject
        status = "approved" if args.approval_approve else "rejected"
        result = _decide_approval(
            args.root,
            request_id,
            status,
            decided_by=args.approval_by,
            note=args.approval_note,
        )
        _print_or_json(result, args.json)
        return 0

    if args.approval_list:
        queue = _approval_queue(args.root)
        status = None if args.approval_list == "all" else args.approval_list
        rows = queue.list(status=status)
        result = {
            "status": args.approval_list,
            "count": len(rows),
            "requests": [_approval_request_dict(row) for row in rows],
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_approval_rows(rows)
        return 0

    if args.control_smoke:
        result = asyncio.run(_run_control_smoke(args.root))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Control Gateway Smoke")
            print(f"status: {result['status']}")
            print(f"policy: {result['policy']['action']} - {result['policy']['reason']}")
            print(f"guard: risk={result['guard']['risk_score']:.2f} {result['guard']['risk_level']}")
            print(f"audit_event_id: {result['audit_event_id']}")
        return 0

    if args.bypass_smoke:
        result = asyncio.run(_run_bypass_smoke(args.root))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Bypass Guard Smoke")
            print(f"direct_success: {result['direct_success']}")
            print(f"direct_message: {result['direct_message']}")
            print(f"runner_status: {result['runner']['status']}")
            print(f"raw_apply_count: {result['raw_apply_count']}")
        return 0

    if args.registry_smoke:
        result = asyncio.run(_run_registry_smoke(args.root))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Controlled Registry Smoke")
            print(f"registered: {result['registry']['persisted_patients']}")
            print(f"direct_success: {result['direct_success']}")
            print(f"runner_status: {result['runner']['status']}")
        return 0

    if args.runner_smoke:
        result = asyncio.run(_run_runner_smoke(args.root, observe_only=not args.execute))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("MEDIC Controlled Runner Smoke")
            print(f"status: {result['status']}")
            print(f"trace_id: {result['trace_id']}")
            print(f"gateway: {result['gateway']['status']}")
            if result.get("treatment"):
                print(f"treatment: success={result['treatment'].get('success')} {result['treatment'].get('message')}")
        return 0

    report = MedicSelfControlLayer(args.root).inspect()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render_text())
    return 0


async def _gateway_review_with_trace(
    root: str,
    gateway: ControlGateway,
    patient: object,
    prescription: Prescription,
    observe_only: bool,
    actor: str,
    message: str = "Gateway-only review",
) -> object:
    trace = _pipeline_trace(root)
    trace_id = trace.new_trace_id()
    patient_id = str(getattr(prescription, "patient_id", ""))
    prescription_id = str(getattr(prescription, "prescription_id", ""))
    trace.record(
        trace_id,
        "prescription_received",
        "ok",
        f"{message}: prescription submitted",
        patient_id=patient_id,
        prescription_id=prescription_id,
        context={"treatment_type": _treatment_type(prescription)},
    )
    result = await gateway.review(
        patient=patient,
        prescription=prescription,
        observe_only=observe_only,
        actor=actor,
        trace_id=trace_id,
    )
    trace.record(
        trace_id,
        "control_gateway",
        result.status,
        f"{message}: gateway review completed",
        patient_id=patient_id,
        prescription_id=prescription_id,
        context=result.to_dict(),
    )
    if result.approval_request_id:
        trace.record(
            trace_id,
            "approval_queue",
            "queued",
            f"{message}: prescription queued for approval",
            patient_id=patient_id,
            prescription_id=prescription_id,
            context={"request_id": result.approval_request_id},
        )
    trace.record(
        trace_id,
        "treatment_execution",
        "skipped",
        f"{message}: gateway-only path does not execute treatment",
        patient_id=patient_id,
        prescription_id=prescription_id,
    )
    trace.record(
        trace_id,
        "runner_complete",
        result.status,
        f"{message}: completed before patient execution",
        patient_id=patient_id,
        prescription_id=prescription_id,
    )
    return result


async def _run_control_smoke(root: str) -> dict:
    gateway = ControlGateway(root)
    rx = Prescription(
        patient_id="medic-self",
        treatment_type=TreatmentType.PROMPT_PATCH,
        payload={
            "system_prompt": "Observe MEDIC control behavior and do not execute treatments.",
            "dry_run": True,
        },
        issued_by="medic.control_smoke",
        confidence=0.82,
        risk_level="LOW",
    )
    result = await _gateway_review_with_trace(
        root,
        gateway,
        patient=_ControlSmokePatient(),
        prescription=rx,
        observe_only=True,
        actor="medic_control.py",
        message="Control smoke",
    )
    return result.to_dict()


async def _run_runner_smoke(root: str, observe_only: bool = True) -> dict:
    runner = ControlledTreatmentRunner(root)
    patient = runner.protect_patient(_ControlSmokePatient())
    rx = Prescription(
        patient_id=patient.patient_id,
        treatment_type=TreatmentType.MONITOR,
        payload={"reason": "controlled runner smoke"},
        issued_by="medic.runner_smoke",
        confidence=0.95,
        risk_level="LOW",
    )
    result = await runner.run(
        patient=patient,
        prescription=rx,
        observe_only=observe_only,
        actor="medic_control.py",
    )
    return result.to_dict()


async def _run_diagnostic_smoke(root: str, observe_only: bool = True) -> dict:
    diagnostic = ControlledDiagnosticRunner(root)
    patient = diagnostic.protect_patient(_ControlSmokePatient())
    result = await diagnostic.run(
        patient=patient,
        observe_only=observe_only,
        actor="medic_control.py",
    )
    return result.to_dict()


async def _run_diagnostic_harness(root: str) -> dict:
    return await DiagnosticHarnessRunner(root).run()


async def _run_second_opinion_smoke(root: str) -> dict:
    gateway = ControlGateway(root)
    patient = _ControlSmokePatient()

    safe_patch = Prescription(
        patient_id=patient.patient_id,
        treatment_type=TreatmentType.PATCH_CODE,
        payload={
            "file_path": "safe_smoke.py",
            "source_code": "def answer():\n    return 41\n",
            "diff_patch": "--- a/safe_smoke.py\n+++ b/safe_smoke.py\n@@\n+def extra_check():\n+    return True\n",
            "dry_run": True,
            "report_only": True,
        },
        issued_by="medic.second_opinion_smoke",
        confidence=0.90,
        risk_level="HIGH",
    )
    dangerous_patch = Prescription(
        patient_id=patient.patient_id,
        treatment_type=TreatmentType.PATCH_CODE,
        payload={
            "file_path": "danger_smoke.py",
            "source_code": "def answer():\n    return 41\n",
            "diff_patch": "--- a/danger_smoke.py\n+++ b/danger_smoke.py\n@@\n+value = eval('40 + 2')\n",
            "dry_run": True,
        },
        issued_by="medic.second_opinion_smoke",
        confidence=0.90,
        risk_level="HIGH",
    )

    safe = await _gateway_review_with_trace(
        root,
        gateway,
        patient=patient,
        prescription=safe_patch,
        observe_only=False,
        actor="medic_control.py",
        message="Second-opinion safe patch smoke",
    )
    dangerous = await _gateway_review_with_trace(
        root,
        gateway,
        patient=patient,
        prescription=dangerous_patch,
        observe_only=False,
        actor="medic_control.py",
        message="Second-opinion dangerous patch smoke",
    )
    safe_decision = {}
    if safe.approval_request_id:
        safe_decision = _decide_approval(
            root,
            safe.approval_request_id,
            "rejected",
            decided_by="second_opinion_smoke",
            note="safe patch smoke request closed without execution",
        )
    return {
        "cases": {
            "safe_patch": safe.to_dict(),
            "dangerous_patch": dangerous.to_dict(),
        },
        "safe_patch_cleanup": safe_decision,
        "expectations": {
            "safe_patch": "SecondOpinionGate APPROVE, then PolicyEngine queues for approval",
            "dangerous_patch": "SecondOpinionGate REJECT, then PolicyEngine blocks",
        },
        "queue": _approval_queue(root).stats(),
    }


async def _run_second_opinion_harness(root: str) -> dict:
    return await SecondOpinionHarnessRunner(root).run()


async def _run_control_soak(root: str, iterations: int) -> dict:
    return await ControlSoakRunner(root).run(iterations=iterations)


async def _run_benchmark_suite(args: argparse.Namespace) -> dict:
    return await MedicBenchmarkSuiteRunner(args.root).run(
        external_cases_path=args.benchmark_external_cases,
        attack_cases_path=args.benchmark_attack_cases,
        control_iterations=args.benchmark_control_iterations,
        observe_cycles=args.benchmark_observe_cycles,
        observe_interval=args.benchmark_observe_interval,
    )


async def _run_observe_soak(
    root: str,
    config_path: str,
    cycles: int,
    interval_seconds: float,
    stop_on_blocked: bool,
) -> dict:
    return await ObserveSoakRunner(root).run(
        config_path=config_path,
        cycles=cycles,
        interval_seconds=interval_seconds,
        stop_on_blocked=stop_on_blocked,
    )


async def _run_python_service_smoke(root: str) -> dict:
    return await PythonServiceSmokeRunner(root).run()


async def _run_observe_daemon(
    root: str,
    config_path: str,
    interval_seconds: float | None,
    max_cycles: int | None,
    stop_on_blocked: bool | None,
) -> dict:
    return await ObserveDaemonRunner(root).run(
        config_path=config_path,
        interval_seconds=interval_seconds,
        max_cycles=max_cycles,
        stop_on_blocked=stop_on_blocked,
    )


async def _run_observe_supervisor(
    root: str,
    config_path: str,
    cycles: int,
    cycle_interval_seconds: float,
) -> dict:
    return await ObserveSupervisorRunner(root).run(
        config_path=config_path,
        cycles=cycles,
        cycle_interval_seconds=cycle_interval_seconds,
    )


async def _run_observe_loop(
    root: str,
    target: str,
    iterations: int,
    interval_seconds: float,
    patient_id: str = "",
    service_url: str = "",
    source_root: str = "",
    health_path: str = "/health",
    pid: int | None = None,
    watch_processes: str = "",
    disk_path: str = "",
) -> dict:
    runner = ObserveLoopRunner(root)
    patient = build_observe_patient(
        target=target,
        root=root,
        patient_id=patient_id,
        service_url=service_url,
        source_root=source_root,
        health_path=health_path,
        pid=pid,
        watch_processes=watch_processes,
        disk_path=disk_path,
    )
    registry = ControlledPatientRegistry(root, audit_log=runner.audit_log)
    registered = registry.register(patient, replace=True)
    result = await runner.run(
        patient=registered,
        iterations=iterations,
        interval_seconds=interval_seconds,
        actor="medic_control.py",
    )
    result["observe_target"] = target
    result["registered_patient"] = registry.stats()
    return result


async def _run_bypass_smoke(root: str) -> dict:
    runner = ControlledTreatmentRunner(root)
    raw_patient = _ControlSmokePatient()
    patient = ControlledPatientProxy(raw_patient, audit_log=runner.audit_log)
    rx = Prescription(
        patient_id=patient.patient_id,
        treatment_type=TreatmentType.MONITOR,
        payload={"reason": "bypass guard smoke"},
        issued_by="medic.bypass_smoke",
        confidence=0.95,
        risk_level="LOW",
    )

    direct_apply = getattr(patient, "apply_treatment")
    direct = await direct_apply(rx)
    runner_result = await runner.run(
        patient=patient,
        prescription=rx,
        observe_only=False,
        actor="medic_control.py",
    )
    return {
        "direct_success": direct.success,
        "direct_message": direct.message,
        "runner": runner_result.to_dict(),
        "raw_apply_count": raw_patient.applied,
    }


async def _run_registry_smoke(root: str) -> dict:
    runner = ControlledTreatmentRunner(root)
    registry = ControlledPatientRegistry(root, audit_log=runner.audit_log)
    raw_patient = _ControlSmokePatient()
    patient = registry.register(raw_patient, replace=True)
    rx = Prescription(
        patient_id=patient.patient_id,
        treatment_type=TreatmentType.MONITOR,
        payload={"reason": "controlled registry smoke"},
        issued_by="medic.registry_smoke",
        confidence=0.95,
        risk_level="LOW",
    )

    direct_apply = getattr(patient, "apply_treatment")
    direct = await direct_apply(rx)
    runner_result = await runner.run(
        patient=registry.get(patient.patient_id),
        prescription=rx,
        observe_only=False,
        actor="medic_control.py",
    )
    return {
        "registry": registry.stats(),
        "direct_success": direct.success,
        "direct_message": direct.message,
        "runner": runner_result.to_dict(),
        "raw_apply_count": raw_patient.applied,
    }


async def _run_approval_smoke(root: str) -> dict:
    gateway = ControlGateway(root)
    rx = Prescription(
        patient_id="medic-self",
        treatment_type=TreatmentType.PATCH_CODE,
        payload={
            "diff_patch": "--- a/smoke.py\n+++ b/smoke.py\n@@\n+print('approval smoke')",
            "dry_run": True,
            "report_only": True,
        },
        issued_by="medic.approval_smoke",
        confidence=0.85,
        risk_level="HIGH",
    )
    gateway_result = await _gateway_review_with_trace(
        root,
        gateway,
        patient=_ControlSmokePatient(),
        prescription=rx,
        observe_only=False,
        actor="medic_control.py",
        message="Approval smoke",
    )
    decision = {}
    if gateway_result.approval_request_id:
        decision = _decide_approval(
            root,
            gateway_result.approval_request_id,
            "rejected",
            decided_by="approval_smoke",
            note="smoke request closed without execution",
        )
    return {
        "gateway": gateway_result.to_dict(),
        "decision": decision,
        "queue": _approval_queue(root).stats(),
    }


async def _run_approval_execute_smoke(root: str) -> dict:
    runner = ControlledTreatmentRunner(root)
    registry = ControlledPatientRegistry(root, audit_log=runner.audit_log)
    patient = registry.register(_ControlSmokePatient(), replace=True)
    gateway = ControlGateway(root)
    rx = Prescription(
        patient_id=patient.patient_id,
        treatment_type=TreatmentType.MONITOR,
        payload={"reason": "approved execution smoke"},
        issued_by="medic.approval_execute_smoke",
        confidence=0.95,
        risk_level="HIGH",
    )

    queued = await _gateway_review_with_trace(
        root,
        gateway,
        patient=patient,
        prescription=rx,
        observe_only=False,
        actor="medic_control.py",
        message="Approval execute smoke",
    )
    approved = _decide_approval(
        root,
        queued.approval_request_id,
        "approved",
        decided_by="approval_execute_smoke",
        note="smoke request approved for controlled execution",
    )
    executor = ApprovedTreatmentExecutor(
        root,
        registry=registry,
        runner=runner,
        audit_log=runner.audit_log,
    )
    executed = await executor.execute(
        queued.approval_request_id,
        actor="medic_control.py",
    )
    return {
        "queued": queued.to_dict(),
        "approved": approved,
        "executed": executed.to_dict(),
        "queue": _approval_queue(root).stats(),
    }


async def _execute_approved_request(root: str, request_id: str) -> dict:
    executor = ApprovedTreatmentExecutor(root)
    try:
        result = await executor.execute(request_id, actor="medic_control.py")
        return result.to_dict()
    except KeyError as exc:
        return {
            "status": "error",
            "message": (
                f"runtime patient not available for {exc}. "
                "Run this inside the MEDIC process that registered the patient."
            ),
            "request_id": request_id,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc), "request_id": request_id}


def _approval_queue(root: str) -> ApprovalQueue:
    return ApprovalQueue(Path(root) / "control_state" / "approval_queue.jsonl")


def _incident_queue(root: str) -> IncidentQueue:
    return IncidentQueue(Path(root) / "control_state" / "incident_cases.jsonl")


def _audit_log(root: str) -> AuditLog:
    return AuditLog(Path(root) / "control_state" / "audit.jsonl")


def _pipeline_trace(root: str) -> PipelineTrace:
    return PipelineTrace(Path(root) / "control_state" / "pipeline_trace.jsonl")


def _build_daily_check(args: argparse.Namespace) -> dict:
    brief = OperatorBriefBuilder(args.root).build(
        observe_daemon_config=args.observe_daemon_config,
        incident_stale_after_seconds=args.incident_stale_after,
        daemon_stale_after_seconds=args.brief_daemon_stale_after,
        incident_limit=args.brief_incident_limit,
        approval_limit=args.brief_approval_limit,
        alert_limit=args.brief_alert_limit,
    ).to_dict()

    self_control = dict(brief.get("self_control", {}) or {})
    role_contract = dict(brief.get("role_contract", {}) or {})
    incident = dict(brief.get("incident", {}) or {})
    approval = dict(brief.get("approval", {}) or {})
    daemon = dict(brief.get("observe_daemon", {}) or {})
    daemon_process = dict(daemon.get("process", {}) or {})
    storage = dict(brief.get("storage", {}) or {})
    causal = dict(brief.get("causal", {}) or {})

    checks = [
        _daily_check_row(
            "self_control",
            self_control.get("status", "unknown"),
            self_control.get("status") == "healthy",
            "MEDIC internal consistency",
        ),
        _daily_check_row(
            "role_contract",
            role_contract.get("status", "unknown"),
            role_contract.get("status") == "healthy",
            "behavior limits and approval rules",
        ),
        _daily_check_row(
            "daemon_heartbeat",
            "fresh" if not daemon.get("is_stale") else "stale",
            not bool(daemon.get("is_stale")),
            f"updated={daemon.get('updated_at', '') or 'none'}",
        ),
        _daily_check_row(
            "daemon_process",
            daemon_process.get("status", "unknown"),
            bool(daemon_process.get("alive", False)),
            f"count={daemon_process.get('count', 0)}",
        ),
        _daily_check_row(
            "incidents",
            incident.get("status", "unknown"),
            int(incident.get("active", 0) or 0) == 0,
            f"active={incident.get('active', 0)} stale={incident.get('stale_active', 0)}",
        ),
        _daily_check_row(
            "approvals",
            "clear" if int(approval.get("pending", 0) or 0) == 0 else "pending",
            int(approval.get("pending", 0) or 0) == 0,
            f"pending={approval.get('pending', 0)} approved={approval.get('approved', 0)}",
        ),
        _daily_check_row(
            "storage",
            storage.get("status", "unknown"),
            storage.get("status") == "healthy",
            f"invalid={storage.get('invalid_recent_lines', 0)}",
        ),
        _daily_check_row(
            "causal",
            causal.get("status", "unknown"),
            causal.get("status") == "healthy",
            "harness and trace consistency",
        ),
    ]

    commands = dict(brief.get("commands", {}) or {})
    medic_cli = _medic_cli_command()
    commands.update({
        "daily_check": f"{medic_cli} --daily-check",
        "refresh_daemon_once": (
            f"{medic_cli} --observe-daemon --daemon-max-cycles 1 "
            "--daemon-interval 0 --observe-daemon-config MEDIC\\config\\observe_daemon.example.json"
        ),
        "start_daemon_hidden": ".\\MEDIC\\scripts\\start_observe_daemon_hidden.ps1",
        "install_user_startup": ".\\MEDIC\\scripts\\install_user_startup.ps1 -Apply",
        "benchmark_suite": ".\\MEDIC\\scripts\\run_benchmark_suite.ps1",
        "observe_soak": ".\\MEDIC\\scripts\\run_observe_soak.ps1 -Cycles 3 -Interval 1",
        "refresh_daemon_once_script": ".\\MEDIC\\scripts\\run_observe_daemon.ps1 -MaxCycles 1 -Interval 0",
    })

    open_items = list(brief.get("open_items", []) or [])
    daemon_process_item = "observe daemon process is not running"
    if not daemon_process.get("alive", False) and daemon_process_item not in open_items:
        open_items.append(daemon_process_item)

    next_actions = _daily_next_actions(brief)
    status = str(brief.get("status", "unknown"))
    if status == "clear" and any(not bool(item.get("ok")) for item in checks):
        status = "attention_required"
    top_action = str(brief.get("top_action", ""))
    if not daemon_process.get("alive", False):
        top_action = "Start the continuous observe daemon or install user startup."
    return {
        "kind": "daily_check",
        "status": status,
        "generated_at": brief.get("generated_at", ""),
        "root": brief.get("root", ""),
        "summary": brief.get("summary", ""),
        "top_action": top_action,
        "checks": checks,
        "open_items": open_items,
        "next_actions": next_actions,
        "commands": commands,
    }


def _daily_check_row(name: str, status: str, ok: bool, detail: str) -> dict:
    return {
        "name": name,
        "status": status,
        "ok": ok,
        "detail": detail,
    }


def _daily_next_actions(brief: dict) -> list[str]:
    actions: list[str] = []
    daemon = dict(brief.get("observe_daemon", {}) or {})
    daemon_process = dict(daemon.get("process", {}) or {})
    incident = dict(brief.get("incident", {}) or {})
    approval = dict(brief.get("approval", {}) or {})

    if not daemon_process.get("alive", False):
        actions.append("Start the continuous observe daemon or register user startup.")
    if brief.get("status") == "clear" and not actions:
        return [str(brief.get("top_action", ""))]
    if daemon.get("is_stale"):
        actions.append("Refresh daemon heartbeat with one observe cycle.")
    if int(incident.get("active", 0) or 0) > 0:
        actions.append("Review active incidents and ack, resolve, or reject them.")
    if int(approval.get("pending", 0) or 0) > 0:
        actions.append("Review pending approval requests before any execution.")

    for item in list(brief.get("open_items", []) or []):
        if (
            item == "observe daemon process is not running"
            and any(action.startswith("Start the continuous observe daemon") for action in actions)
        ):
            continue
        if item not in actions:
            actions.append(str(item))
    return actions or [str(brief.get("top_action", ""))]


def _print_daily_check(result: dict) -> None:
    print(f"MEDIC Daily Check ({result.get('generated_at', '')})")
    print(f"root: {result.get('root', '')}")
    print(f"status: {result.get('status', 'unknown')}")
    print(f"top action: {result.get('top_action', '')}")
    print("")
    print("Checks:")
    for item in list(result.get("checks", []) or []):
        marker = "OK" if item.get("ok") else "ATTN"
        print(
            f"  [{marker}] {item.get('name', '')}: "
            f"{item.get('status', '')} - {item.get('detail', '')}"
        )

    print("")
    print("Open Items:")
    open_items = list(result.get("open_items", []) or [])
    if not open_items:
        print("  none")
    for item in open_items:
        print(f"  - {item}")

    print("")
    print("Next Actions:")
    for item in list(result.get("next_actions", []) or []):
        print(f"  - {item}")

    commands = dict(result.get("commands", {}) or {})
    print("")
    print("Useful Commands:")
    for key in [
        "daily_check",
        "refresh_daemon_once",
        "start_daemon_hidden",
        "install_user_startup",
        "benchmark_suite",
        "observe_soak",
        "refresh_daemon_once_script",
        "incident_triage",
        "daemon_status",
        "approval_pending",
        "causal_report",
        "storage_health",
    ]:
        value = commands.get(key)
        if value:
            print(f"  {key}: {value}")


def _medic_cli_command() -> str:
    return f"{_quote_cli_arg(sys.executable or 'python')} MEDIC\\medic_control.py"


def _quote_cli_arg(value: str) -> str:
    if any(char.isspace() for char in value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _treatment_type(prescription: object) -> str:
    tx = getattr(prescription, "treatment_type", "")
    return str(getattr(tx, "value", tx) or "")


def _approval_request_dict(req: ApprovalRequest) -> dict:
    return req.to_dict()


def _incident_case_dict(case: IncidentCase) -> dict:
    return case.to_dict()


def _print_or_json(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if "request" in result:
        req = result["request"]
        print(f"{req['request_id']}  {req['status']}  {req['treatment_type']}  {req['patient_id']}")
        print(f"reason: {req['reason']}")
        if req.get("decision_note"):
            print(f"note: {req['decision_note']}")
        return
    if "incident" in result:
        item = result["incident"]
        print(
            f"{item['incident_id']}  {item['status']}  "
            f"{item['severity']}  {item['target_name']}"
        )
        print(f"message: {item['message']}")
        if item.get("decision_note"):
            print(f"note: {item['decision_note']}")
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _print_approval_rows(rows: list[ApprovalRequest]) -> None:
    if not rows:
        print("No approval requests.")
        return
    for row in rows:
        print(
            f"{row.request_id}  {row.status:<8}  "
            f"{row.risk_level:<7}  {row.treatment_type:<16}  {row.patient_id}"
        )
        print(f"  reason: {row.reason}")


def _print_incident_rows(rows: list[IncidentCase]) -> None:
    if not rows:
        print("No incident cases.")
        return
    for row in rows:
        print(
            f"{row.incident_id}  {row.status:<12}  "
            f"{row.severity:<8}  seen={row.seen_count:<3}  {row.target_name}"
        )
        print(f"  message: {row.message}")
        if row.trace_ids:
            print(f"  traces: {', '.join(row.trace_ids[:3])}")


def _print_incident_triage(result: dict) -> None:
    print("MEDIC Incident Triage")
    print(f"status: {result.get('status', 'unknown')}")
    print(f"active: {result.get('active', 0)}")
    print(f"active critical: {result.get('active_critical', 0)}")
    print(f"stale active: {result.get('stale_active', 0)}")
    print(f"highest priority: {result.get('highest_priority', '') or 'none'}")
    print(f"next: {result.get('next_action', '') or 'none'}")
    cases = list(result.get("top_active_cases", []) or [])
    if not cases:
        print("No active incident cases.")
        return
    for item in cases:
        print(
            f"{item.get('incident_id', '')}  {item.get('priority', ''):<2}  "
            f"{item.get('status', ''):<12}  {item.get('severity', ''):<8}  "
            f"age={int(float(item.get('age_seconds', 0) or 0))}s  "
            f"{item.get('target_name', '')}"
        )
        print(f"  reason: {item.get('reason', '')}")
        print(f"  message: {item.get('message', '')}")


def _decide_approval(
    root: str,
    request_id: str,
    status: str,
    decided_by: str,
    note: str,
) -> dict:
    queue = _approval_queue(root)
    item = queue.decide(request_id, status, decided_by=decided_by, note=note)
    _audit_log(root).record(
        event_type=f"approval_{status}",
        actor=decided_by,
        patient_id=item.patient_id,
        message=f"approval request {status}",
        context={
            "trace_id": item.trace_id,
            "request_id": item.request_id,
            "prescription_id": item.prescription_id,
            "treatment_type": item.treatment_type,
            "note": note,
        },
    )
    if item.trace_id:
        _pipeline_trace(root).record(
            item.trace_id,
            "approval_decision",
            status,
            "Approval request decided",
            patient_id=item.patient_id,
            prescription_id=item.prescription_id,
            context={
                "request_id": item.request_id,
                "decided_by": decided_by,
                "note": note,
            },
        )
    return {"request": item.to_dict(), "queue": queue.stats()}


def _decide_incident(
    root: str,
    incident_id: str,
    status: str,
    decided_by: str,
    note: str,
) -> dict:
    queue = _incident_queue(root)
    item = queue.transition(incident_id, status, decided_by=decided_by, note=note)
    _audit_log(root).record(
        event_type=f"incident_{status}",
        actor=decided_by,
        patient_id=item.target_name or item.target,
        message=f"incident {status}",
        context={
            "incident_id": item.incident_id,
            "severity": item.severity,
            "target": item.target,
            "message": item.message,
            "note": note,
            "trace_ids": item.trace_ids,
        },
    )
    for trace_id in item.trace_ids:
        _pipeline_trace(root).record(
            trace_id,
            "incident_triage",
            status,
            "Incident case triage decision recorded",
            patient_id=item.target_name or item.target,
            context={
                "incident_id": item.incident_id,
                "decided_by": decided_by,
                "note": note,
            },
        )
    return {"incident": item.to_dict(), "queue": queue.stats()}


if __name__ == "__main__":
    raise SystemExit(main())
