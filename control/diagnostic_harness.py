"""
diagnostic_harness.py
─────────────────────────────────────────────────────────────────────
MEDIC 진단/처방 회귀 harness.

사라진 medic_harness.py 원본을 직접 복원하지는 못하므로, 현재 control
레이어가 읽을 수 있는 summary 형식으로 진단 시나리오를 새로 생성한다.
이 harness의 목적은 최소 30개 이상의 케이스로 다음을 반복 검증하는 것:

  vitals -> diagnose -> prescribe -> second_opinion -> gateway trace

치료 실행은 하지 않는다. 이 파일은 감시/판정 레이어의 인과성 테스트다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control.diagnostic_runner import ControlledDiagnosticRunner
from patient_registry.base_patient import PatientType, TreatmentResult, TreatmentType, Vitals


@dataclass
class DiagnosticScenario:
    """One deterministic diagnostic harness case."""
    scenario_id: str
    patient_type: PatientType
    severity_expected: str
    root_cause_expected: str
    treatment_expected: TreatmentType
    vitals: dict[str, Any] = field(default_factory=dict)
    symptoms: list[str] = field(default_factory=list)
    supported_treatments: list[TreatmentType] = field(default_factory=list)
    fail_collect_vitals: bool = False

    def patient_id(self, index: int) -> str:
        return f"harness-{index:02d}-{self.scenario_id}"


class HarnessPatient:
    """Small in-memory patient used only by the diagnostic harness."""

    def __init__(self, scenario: DiagnosticScenario, patient_id: str) -> None:
        self._scenario = scenario
        self._patient_id = patient_id

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return self._scenario.patient_type

    async def collect_vitals(self) -> Vitals:
        if self._scenario.fail_collect_vitals:
            raise RuntimeError("harness vitals collection failure")

        data = dict(self._scenario.vitals)
        return Vitals(
            patient_id=self.patient_id,
            patient_type=self.patient_type,
            is_alive=bool(data.get("is_alive", True)),
            cpu_percent=float(data.get("cpu_percent", 0.0) or 0.0),
            memory_percent=float(data.get("memory_percent", 0.0) or 0.0),
            error_rate=float(data.get("error_rate", 0.0) or 0.0),
            latency_p99_ms=float(data.get("latency_p99_ms", 0.0) or 0.0),
            symptoms=list(self._scenario.symptoms),
        )

    async def apply_treatment(self, prescription: Any) -> TreatmentResult:
        before = None
        try:
            before = await self.collect_vitals()
        except Exception:
            before = None
        return TreatmentResult(
            prescription_id=str(getattr(prescription, "prescription_id", "")),
            patient_id=self.patient_id,
            success=True,
            message="diagnostic harness should run in observe-only mode",
            before_vitals=before,
            after_vitals=before,
        )

    async def report_health(self) -> bool:
        return True


class DiagnosticHarnessRunner:
    """Run diagnostic scenarios and persist a CausalReport-compatible summary."""

    VARIANTS = [
        ("baseline", True, True),
        ("decode_off", False, True),
        ("uics_off", True, False),
        ("decode_uics_off", False, False),
    ]

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.harness_dir = self.root / "harness_runs"
        self.diagnostic = ControlledDiagnosticRunner(self.root)

    async def run(self) -> dict[str, Any]:
        scenarios = self.scenarios()
        reports = []
        variants = []
        baseline_match_rate = 0.0

        for variant, decode_enabled, uics_enabled in self.VARIANTS:
            report = await self._run_variant(
                scenarios=scenarios,
                variant=variant,
                decode_enabled=decode_enabled,
                uics_enabled=uics_enabled,
            )
            reports.append(report)
            if variant == "baseline":
                baseline_match_rate = float(report["match_rate"])
            variants.append({
                "variant": variant,
                "match_rate": report["match_rate"],
                "delta_vs_baseline": round(float(report["match_rate"]) - baseline_match_rate, 4),
                "matched_cases": report["matched_cases"],
                "total_cases": report["total_cases"],
                "bias_flags": report["bias_flags"],
                "mismatch_counts": report["mismatch_counts"],
            })

        summary = {
            "kind": "harness_matrix",
            "scenario_set": "diagnostic_control_v1",
            "language": "ko",
            "baseline_variant": "baseline",
            "baseline_match_rate": baseline_match_rate,
            "regressions": self._regressions(variants),
            "variants": variants,
            "reports": reports,
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    async def _run_variant(
        self,
        scenarios: list[DiagnosticScenario],
        variant: str,
        decode_enabled: bool,
        uics_enabled: bool,
    ) -> dict[str, Any]:
        results = []
        treatment_counts: dict[str, int] = {}
        mismatch_counts: dict[str, int] = {}

        for index, scenario in enumerate(scenarios, start=1):
            row = await self._run_scenario(index, scenario)
            results.append(row)
            treatment_counts[row["treatment_actual"]] = (
                treatment_counts.get(row["treatment_actual"], 0) + 1
            )
            if not row["matched"]:
                key = row["scenario_id"]
                mismatch_counts[key] = mismatch_counts.get(key, 0) + 1

        matched_cases = sum(1 for row in results if row["matched"])
        return {
            "scenario_set": "diagnostic_control_v1",
            "decode_enabled": decode_enabled,
            "uics_enabled": uics_enabled,
            "language": "ko",
            "total_cases": len(results),
            "matched_cases": matched_cases,
            "match_rate": self._rate(matched_cases, len(results)),
            "mismatch_counts": mismatch_counts,
            "treatment_counts": treatment_counts,
            "bias_flags": [],
            "results": results,
            "variant": variant,
        }

    async def _run_scenario(
        self,
        index: int,
        scenario: DiagnosticScenario,
    ) -> dict[str, Any]:
        patient = self.diagnostic.protect_patient(
            HarnessPatient(scenario, scenario.patient_id(index))
        )
        result = await self.diagnostic.run(
            patient=patient,
            observe_only=True,
            actor="medic.diagnostic_harness",
            verify_health=False,
        )
        data = result.to_dict()
        diagnosis = data["diagnosis"]
        prescription = data["prescription"]
        treatment_actual = str(prescription["treatment_type"])
        supported = [
            str(getattr(item, "value", item))
            for item in (scenario.supported_treatments or [scenario.treatment_expected])
        ]

        severity_ok = scenario.severity_expected == diagnosis["severity"]
        root_ok = scenario.root_cause_expected == diagnosis["root_cause"]
        treatment_strict_ok = scenario.treatment_expected.value == treatment_actual
        treatment_supported_ok = treatment_actual in supported
        matched = severity_ok and root_ok and treatment_supported_ok

        return {
            "scenario_id": scenario.scenario_id,
            "patient_id": patient.patient_id,
            "severity_expected": scenario.severity_expected,
            "severity_actual": diagnosis["severity"],
            "root_cause_expected": scenario.root_cause_expected,
            "root_cause_actual": diagnosis["root_cause"],
            "treatment_expected": scenario.treatment_expected.value,
            "treatment_actual": treatment_actual,
            "supported_treatments": supported,
            "strict_treatment_matched": treatment_strict_ok,
            "matched": matched,
            "trace_id": data["trace_id"],
            "notes": self._notes(
                scenario,
                diagnosis,
                treatment_actual,
                severity_ok,
                root_ok,
                treatment_strict_ok,
                treatment_supported_ok,
            ),
        }

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        path = self.harness_dir / f"harness_{stamp}_summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _regressions(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            variant for variant in variants
            if float(variant.get("delta_vs_baseline", 0.0) or 0.0) < 0
        ]

    @staticmethod
    def _notes(
        scenario: DiagnosticScenario,
        diagnosis: dict[str, Any],
        treatment_actual: str,
        severity_ok: bool,
        root_ok: bool,
        treatment_strict_ok: bool,
        treatment_supported_ok: bool,
    ) -> str:
        if severity_ok and root_ok and treatment_strict_ok:
            return (
                f"{treatment_actual} for {scenario.patient_type.value} because "
                f"{diagnosis['root_cause']}; risk={diagnosis['risk_level']}; strict"
            )
        if severity_ok and root_ok and treatment_supported_ok:
            return (
                f"{treatment_actual} is supported for {diagnosis['root_cause']}, "
                "but differs from strict expectation"
            )
        return (
            f"mismatch severity_ok={severity_ok} root_ok={root_ok} "
            f"treatment_supported_ok={treatment_supported_ok}"
        )

    @staticmethod
    def _rate(num: int | float, den: int | float) -> float:
        if not den:
            return 0.0
        return round(float(num) / float(den), 4)

    @staticmethod
    def scenarios() -> list[DiagnosticScenario]:
        P = PatientType
        T = TreatmentType
        return [
            DiagnosticScenario("python_service_dead", P.PYTHON_SERVICE, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, {"is_alive": False}),
            DiagnosticScenario("python_service_unreachable_symptom", P.PYTHON_SERVICE, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, symptoms=["health_check_failed:Timeout"]),
            DiagnosticScenario("python_service_process_missing", P.PYTHON_SERVICE, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, symptoms=["process_not_found:pid=4100"]),
            DiagnosticScenario("python_service_cpu_overload", P.PYTHON_SERVICE, "HIGH", "cpu_overload", T.RESTART, {"cpu_percent": 95.0}),
            DiagnosticScenario("python_service_cpu_symptom", P.PYTHON_SERVICE, "HIGH", "cpu_overload", T.RESTART, symptoms=["cpu_high:93%"]),
            DiagnosticScenario("python_service_memory_pressure", P.PYTHON_SERVICE, "HIGH", "memory_pressure", T.CONFIG_CHANGE, {"memory_percent": 94.0}),
            DiagnosticScenario("python_service_error_spike", P.PYTHON_SERVICE, "HIGH", "error_rate_spike", T.RESTART, {"error_rate": 62.0}),
            DiagnosticScenario("python_service_latency_spike", P.PYTHON_SERVICE, "HIGH", "latency_spike", T.RESTART, {"latency_p99_ms": 3600.0}),
            DiagnosticScenario("python_service_healthy", P.PYTHON_SERVICE, "LOW", "no_issue_detected", T.MONITOR, {"cpu_percent": 12.0, "memory_percent": 18.0, "latency_p99_ms": 120.0}),
            DiagnosticScenario("ai_hallucination_symptom", P.AI_MODEL, "HIGH", "ai_hallucination_spike_prompt_or_model_issue", T.PROMPT_PATCH, symptoms=["hallucination_rate:0.31"]),
            DiagnosticScenario("ai_hallucination_error_rate", P.AI_MODEL, "HIGH", "ai_hallucination_spike_prompt_or_model_issue", T.PROMPT_PATCH, {"error_rate": 25.0}),
            DiagnosticScenario("ai_output_drift_with_hallucination", P.AI_MODEL, "HIGH", "ai_hallucination_spike_prompt_or_model_issue", T.PROMPT_PATCH, {"error_rate": 22.0}, ["output_drift:0.41"]),
            DiagnosticScenario("ai_latency_baseline_drift", P.AI_MODEL, "HIGH", "severe_latency_baseline_drift", T.PROMPT_PATCH, {"latency_p99_ms": 6500.0}),
            DiagnosticScenario("ai_slow_inference", P.AI_MODEL, "HIGH", "severe_latency_baseline_drift", T.PROMPT_PATCH, {"latency_p99_ms": 5200.0}),
            DiagnosticScenario("ai_cpu_overload", P.AI_MODEL, "HIGH", "cpu_overload", T.RESTART, {"cpu_percent": 91.0}),
            DiagnosticScenario("ai_memory_pressure", P.AI_MODEL, "HIGH", "memory_pressure", T.CONFIG_CHANGE, {"memory_percent": 92.0}),
            DiagnosticScenario("ai_healthy", P.AI_MODEL, "LOW", "no_issue_detected", T.MONITOR, {"latency_p99_ms": 400.0}),
            DiagnosticScenario("database_unreachable", P.DATABASE, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, {"is_alive": False}, ["db_file_not_found:data.db"]),
            DiagnosticScenario("database_connection_failed", P.DATABASE, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, symptoms=["connection_failed:ECONNREFUSED"]),
            DiagnosticScenario("database_memory_pressure", P.DATABASE, "HIGH", "memory_pressure", T.CONFIG_CHANGE, {"memory_percent": 96.0}, ["memory_pressure:96%"]),
            DiagnosticScenario("database_latency_spike", P.DATABASE, "HIGH", "latency_spike", T.RESTART, {"latency_p99_ms": 4200.0}),
            DiagnosticScenario("database_error_spike", P.DATABASE, "HIGH", "error_rate_spike", T.RESTART, {"error_rate": 75.0}),
            DiagnosticScenario("database_healthy", P.DATABASE, "LOW", "no_issue_detected", T.MONITOR, {"memory_percent": 30.0}),
            DiagnosticScenario("k8s_degraded_replicas", P.K8S_WORKLOAD, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, symptoms=["degraded_replicas:0/3", "unreachable"]),
            DiagnosticScenario("k8s_cpu_throttling", P.K8S_WORKLOAD, "HIGH", "cpu_overload", T.K8S_HPA_ADJUST, {"cpu_percent": 93.0}, ["cpu_throttling:95%"]),
            DiagnosticScenario("k8s_memory_pressure", P.K8S_WORKLOAD, "HIGH", "memory_pressure", T.CONFIG_CHANGE, {"memory_percent": 94.0}, ["memory_pressure:94%"]),
            DiagnosticScenario("k8s_latency_spike", P.K8S_WORKLOAD, "HIGH", "latency_spike", T.RESTART, {"latency_p99_ms": 3500.0}),
            DiagnosticScenario("k8s_healthy", P.K8S_WORKLOAD, "LOW", "no_issue_detected", T.MONITOR, {"cpu_percent": 18.0, "memory_percent": 40.0}),
            DiagnosticScenario("system_cpu_high", P.GENERIC_PROCESS, "HIGH", "cpu_overload", T.RESTART, {"cpu_percent": 97.0}, ["cpu_high:97%"]),
            DiagnosticScenario("system_memory_high", P.GENERIC_PROCESS, "HIGH", "memory_pressure", T.CONFIG_CHANGE, {"memory_percent": 95.0}, ["memory_high:95%"]),
            DiagnosticScenario("system_process_missing", P.GENERIC_PROCESS, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, symptoms=["process_missing:worker"]),
            DiagnosticScenario("system_healthy", P.GENERIC_PROCESS, "LOW", "no_issue_detected", T.MONITOR, {"cpu_percent": 9.0, "memory_percent": 22.0}),
            DiagnosticScenario("router_all_candidates_down", P.GENERIC_PROCESS, "CRITICAL", "service_unreachable_or_process_dead", T.RESTART, symptoms=["all_candidates_down", "unreachable"]),
            DiagnosticScenario("router_majority_error_spike", P.GENERIC_PROCESS, "HIGH", "error_rate_spike", T.RESTART, {"error_rate": 67.0}, ["majority_candidates_down:3"]),
            DiagnosticScenario("remote_node_latency_spike", P.GENERIC_PROCESS, "HIGH", "latency_spike", T.RESTART, {"latency_p99_ms": 4800.0}, ["remote_status_failed:Timeout"]),
            DiagnosticScenario("vitals_collection_failure", P.GENERIC_PROCESS, "CRITICAL", "vitals_collection_failed", T.MANUAL_INTERVENTION, fail_collect_vitals=True),
        ]
