"""Role contract for MEDIC as an external control agent."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MANDATORY_CONTROLS = [
    "ControlGateway",
    "ApprovalQueue",
    "AuditLog",
    "PipelineTrace",
    "IncidentQueue",
    "SecondOpinionGate",
    "ControlledTreatmentRunner",
    "ControlledPatientRegistry",
]

MANDATORY_EVIDENCE = [
    "control_state/approval_queue.jsonl",
    "control_state/audit.jsonl",
    "control_state/pipeline_trace.jsonl",
    "control_state/incident_cases.jsonl",
]

MANDATORY_APPROVAL_TREATMENTS = [
    "patch_code",
    "restart",
    "rollback",
    "config_change",
    "quarantine",
    "fine_tune_trigger",
    "manual_intervention",
]

MANDATORY_FORBIDDEN_ACTIONS = [
    "delete_evidence_logs_without_explicit_human_approval",
    "compact_evidence_logs_without_archive_and_explicit_human_approval",
    "execute_patient_treatment_outside_control_gateway",
    "enable_medium_or_high_risk_auto_execution_without_contract_change",
    "modify_policy_or_contract_without_audit",
]


def default_role_contract() -> dict[str, Any]:
    """Return MEDIC's default role contract."""
    return {
        "version": 1,
        "agent_name": "MEDIC",
        "agent_kind": "external_control_agent",
        "operating_mode": "observe_judge_approve",
        "default_execution_mode": "observe_only",
        "auto_execute_enabled": False,
        "requires_human_approval_for_contract_changes": True,
        "mission": [
            "observe configured patients and services",
            "diagnose symptoms and propose prescriptions",
            "route risky actions through policy, approval, audit, and trace",
            "preserve evidence for causal debugging and self-control review",
        ],
        "allowed_without_approval": [
            "collect_vitals",
            "diagnose",
            "prescribe_recommendation",
            "policy_review",
            "second_opinion_review",
            "write_audit_event",
            "write_pipeline_trace",
            "write_observe_summary",
            "write_alert",
            "write_incident_case",
            "run_harness",
            "run_observe_daemon",
            "read_control_state",
        ],
        "approval_required_treatments": MANDATORY_APPROVAL_TREATMENTS,
        "forbidden_without_explicit_human_approval": MANDATORY_FORBIDDEN_ACTIONS,
        "required_controls": MANDATORY_CONTROLS,
        "evidence_must_preserve": MANDATORY_EVIDENCE,
        "success_criteria": [
            "all medium and high risk treatments pass through ControlGateway",
            "approval queue has no stale pending request before execution",
            "audit and trace records link decisions to prescriptions",
            "observe daemon alerts are linked to incident cases",
            "stale active incidents are triaged before execution is enabled",
            "post-execution verification records recovery or failure",
        ],
        "operator_notes": [
            "MEDIC is an agent, but not an unrestricted autonomous repair agent.",
            "Automatic repair requires an explicit contract change and fresh harness baseline.",
        ],
    }


def load_role_contract(root: str | Path, path: str | Path = "") -> tuple[dict[str, Any], str]:
    """Load the role contract from config, or return the default contract."""
    root_path = Path(root)
    if path:
        contract_path = _resolve_path(path, root_path=root_path, base_dir=Path.cwd())
        return _load_json(contract_path), str(contract_path)

    default_path = root_path / "config" / "medic_role_contract.json"
    if default_path.exists():
        return _load_json(default_path), str(default_path)
    return default_role_contract(), "default"


def inspect_role_contract(root: str | Path, path: str | Path = "") -> dict[str, Any]:
    """Validate MEDIC's role contract and return a self-control friendly report."""
    contract, source = load_role_contract(root, path)
    violations: list[dict[str, str]] = []

    _require_equal(
        contract,
        "agent_kind",
        "external_control_agent",
        violations,
        "HIGH",
        "MEDIC must remain an external control agent.",
    )
    _require_equal(
        contract,
        "default_execution_mode",
        "observe_only",
        violations,
        "HIGH",
        "MEDIC must default to observe-only execution.",
    )
    if bool(contract.get("auto_execute_enabled", False)):
        violations.append(_violation(
            "HIGH",
            "auto_execute_enabled",
            "auto_execute_enabled is true.",
            "Keep auto execution disabled until approval and recovery gates are production-ready.",
        ))
    if not bool(contract.get("requires_human_approval_for_contract_changes", False)):
        violations.append(_violation(
            "HIGH",
            "contract_change_approval",
            "contract changes do not require human approval.",
            "Contract changes must be explicit, audited, and human-approved.",
        ))

    _require_contains_all(
        contract,
        "required_controls",
        MANDATORY_CONTROLS,
        violations,
        "HIGH",
        "Missing required control component.",
    )
    _require_contains_all(
        contract,
        "evidence_must_preserve",
        MANDATORY_EVIDENCE,
        violations,
        "HIGH",
        "Missing protected evidence path.",
    )
    _require_contains_all(
        contract,
        "approval_required_treatments",
        MANDATORY_APPROVAL_TREATMENTS,
        violations,
        "HIGH",
        "Missing approval-required treatment.",
    )
    _require_contains_all(
        contract,
        "forbidden_without_explicit_human_approval",
        MANDATORY_FORBIDDEN_ACTIONS,
        violations,
        "HIGH",
        "Missing forbidden action rule.",
    )

    status = "healthy"
    severities = {item["severity"] for item in violations}
    if "HIGH" in severities:
        status = "blocked"
    elif violations:
        status = "warning"

    return {
        "kind": "medic_role_contract",
        "status": status,
        "source": source,
        "version": contract.get("version", 0),
        "agent_name": contract.get("agent_name", "MEDIC"),
        "agent_kind": contract.get("agent_kind", ""),
        "operating_mode": contract.get("operating_mode", ""),
        "default_execution_mode": contract.get("default_execution_mode", ""),
        "auto_execute_enabled": bool(contract.get("auto_execute_enabled", False)),
        "requires_human_approval_for_contract_changes": bool(
            contract.get("requires_human_approval_for_contract_changes", False)
        ),
        "allowed_without_approval": list(contract.get("allowed_without_approval", []) or []),
        "approval_required_treatments": list(contract.get("approval_required_treatments", []) or []),
        "forbidden_without_explicit_human_approval": list(
            contract.get("forbidden_without_explicit_human_approval", []) or []
        ),
        "required_controls": list(contract.get("required_controls", []) or []),
        "evidence_must_preserve": list(contract.get("evidence_must_preserve", []) or []),
        "success_criteria": list(contract.get("success_criteria", []) or []),
        "violations": violations,
    }


def render_role_contract_text(report: dict[str, Any]) -> str:
    """Render a compact text view of a role contract report."""
    lines = [
        "MEDIC Role Contract",
        f"status: {report.get('status', 'unknown')}",
        f"source: {report.get('source', '')}",
        f"agent: {report.get('agent_name', 'MEDIC')} ({report.get('agent_kind', '')})",
        f"mode: {report.get('operating_mode', '')}",
        f"default execution: {report.get('default_execution_mode', '')}",
        f"auto execute: {report.get('auto_execute_enabled', False)}",
        f"approval-required treatments: {len(report.get('approval_required_treatments', []))}",
        f"protected evidence paths: {len(report.get('evidence_must_preserve', []))}",
        "",
        "Violations:",
    ]
    violations = list(report.get("violations", []) or [])
    if not violations:
        lines.append("  none")
    for item in violations:
        lines.append(f"  [{item.get('severity', '')}] {item.get('area', '')}: {item.get('message', '')}")
        if item.get("suggestion"):
            lines.append(f"       -> {item['suggestion']}")
    return "\n".join(lines)


def write_role_contract_template(path: str | Path, root: str | Path) -> dict[str, Any]:
    """Write a default role contract template."""
    root_path = Path(root)
    target = _resolve_output_path(path, root_path=root_path, base_dir=Path.cwd())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(default_role_contract(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "status": "written",
        "path": str(target),
    }


def _require_equal(
    contract: dict[str, Any],
    key: str,
    expected: str,
    violations: list[dict[str, str]],
    severity: str,
    message: str,
) -> None:
    actual = str(contract.get(key, ""))
    if actual != expected:
        violations.append(_violation(
            severity,
            key,
            f"{message} expected={expected}, actual={actual or 'missing'}.",
            "Restore the role contract before enabling execution paths.",
        ))


def _require_contains_all(
    contract: dict[str, Any],
    key: str,
    expected: list[str],
    violations: list[dict[str, str]],
    severity: str,
    message: str,
) -> None:
    actual = {str(item) for item in list(contract.get(key, []) or [])}
    for item in expected:
        if item not in actual:
            violations.append(_violation(
                severity,
                key,
                f"{message} missing={item}.",
                "Restore the missing item in medic_role_contract.json.",
            ))


def _violation(
    severity: str,
    area: str,
    message: str,
    suggestion: str,
) -> dict[str, str]:
    return {
        "severity": severity,
        "area": area,
        "message": message,
        "suggestion": suggestion,
    }


def _load_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("role contract must be a JSON object")
    return parsed


def _resolve_path(path: str | Path, root_path: Path, base_dir: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    options = [
        candidate,
        base_dir / candidate,
        root_path / candidate,
    ]
    for option in options:
        if option.exists():
            return option.resolve()
    return (root_path / candidate).resolve()


def _resolve_output_path(path: str | Path, root_path: Path, base_dir: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    parent = candidate.parent if str(candidate.parent) != "." else Path(".")
    if parent.exists():
        return candidate.resolve()
    if (base_dir / parent).exists():
        return (base_dir / candidate).resolve()
    return (root_path / candidate).resolve()
