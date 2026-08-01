"""MEDIC control layer.

This package contains meta-diagnostic tools that inspect MEDIC itself.
The first layer is observe-only: it reads existing harness, soak, guard,
and independence signals without applying treatments.
"""

from control.approval_queue import ApprovalQueue
from control.approval_executor import ApprovedTreatmentExecutor
from control.audit_log import AuditLog
from control.benchmark_suite import MedicBenchmarkSuiteRunner
from control.causal_report import CausalReportBuilder
from control.control_gateway import ControlGateway
from control.control_soak import ControlSoakRunner
from control.controlled_registry import ControlledPatientRegistry
from control.diagnostic_harness import DiagnosticHarnessRunner
from control.diagnostic_runner import ControlledDiagnosticRunner
from control.direct_call_detector import DirectTreatmentCallDetector
from control.incident_queue import IncidentCase, IncidentQueue
from control.observe_daemon import (
    ObserveDaemonRunner,
    ObserveDaemonConfig,
    read_observe_alerts,
    read_observe_daemon_status,
)
from control.observe_loop import ObserveLoopRunner
from control.observe_config import ObserveConfig, ObserveTargetSpec, load_observe_config
from control.observe_soak import ObserveSoakRunner
from control.observe_supervisor import ObserveSupervisorRunner
from control.observe_targets import MedicSelfPatient, build_observe_patient
from control.operator_brief import OperatorBrief, OperatorBriefBuilder
from control.patient_proxy import ControlledPatientProxy
from control.pipeline_trace import PipelineTrace
from control.policy_engine import PolicyEngine
from control.python_service_smoke import PythonServiceSmokeRunner
from control.role_contract import (
    inspect_role_contract,
    render_role_contract_text,
    write_role_contract_template,
)
from control.second_opinion_gate import SecondOpinionGate, SecondOpinionVerdict
from control.second_opinion_harness import SecondOpinionHarnessRunner
from control.self_control_layer import MedicSelfControlLayer
from control.storage_health import ControlStorageHealth
from control.treatment_runner import ControlledTreatmentRunner

__all__ = [
    "ApprovalQueue",
    "ApprovedTreatmentExecutor",
    "AuditLog",
    "MedicBenchmarkSuiteRunner",
    "CausalReportBuilder",
    "ControlGateway",
    "ControlSoakRunner",
    "ControlStorageHealth",
    "ControlledPatientRegistry",
    "ControlledDiagnosticRunner",
    "ControlledTreatmentRunner",
    "DiagnosticHarnessRunner",
    "ControlledPatientProxy",
    "DirectTreatmentCallDetector",
    "IncidentCase",
    "IncidentQueue",
    "MedicSelfControlLayer",
    "MedicSelfPatient",
    "ObserveDaemonConfig",
    "ObserveDaemonRunner",
    "read_observe_alerts",
    "read_observe_daemon_status",
    "ObserveConfig",
    "ObserveLoopRunner",
    "ObserveSoakRunner",
    "ObserveSupervisorRunner",
    "ObserveTargetSpec",
    "OperatorBrief",
    "OperatorBriefBuilder",
    "build_observe_patient",
    "load_observe_config",
    "PipelineTrace",
    "PolicyEngine",
    "PythonServiceSmokeRunner",
    "inspect_role_contract",
    "render_role_contract_text",
    "write_role_contract_template",
    "SecondOpinionGate",
    "SecondOpinionHarnessRunner",
    "SecondOpinionVerdict",
]
