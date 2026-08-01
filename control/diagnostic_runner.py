"""
diagnostic_runner.py
─────────────────────────────────────────────────────────────────────
진단 파이프라인 러너.

이 모듈은 환자 상태 수집부터 처방 생성, 2차 소견 단계 표시, 통제된
치료 러너 연결까지 하나의 trace_id로 묶는다. 아직 자동 수정
컨트롤러가 아니라, 왜 그런 처방이 나왔는지 감시/판정 가능한 발자국을
남기는 레이어다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from control.pipeline_trace import PipelineTrace
from control.treatment_runner import ControlledTreatmentRunner
from patient_registry.base_patient import PatientType, Prescription, TreatmentType


@dataclass
class Diagnosis:
    """환자 vitals에서 도출한 진단."""
    severity: str
    root_cause: str
    confidence: float
    risk_level: str
    symptoms: list[str] = field(default_factory=list)
    reasoning: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "root_cause": self.root_cause,
            "confidence": self.confidence,
            "risk_level": self.risk_level,
            "symptoms": self.symptoms,
            "reasoning": self.reasoning,
            "evidence": self.evidence,
        }


@dataclass
class DiagnosticPipelineResult:
    """진단 파이프라인 실행 결과."""
    status: str
    trace_id: str
    diagnosis: dict[str, Any]
    prescription: dict[str, Any]
    vitals: dict[str, Any]
    second_opinion: dict[str, Any]
    runner: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "trace_id": self.trace_id,
            "diagnosis": self.diagnosis,
            "prescription": self.prescription,
            "vitals": self.vitals,
            "second_opinion": self.second_opinion,
            "runner": self.runner,
            "notes": self.notes,
        }


class ControlledDiagnosticRunner:
    """Collect vitals, diagnose, prescribe, and submit to the control runner."""

    SECOND_OPINION_TREATMENTS = {
        "patch_code",
        "fine_tune_trigger",
        "quarantine",
    }

    def __init__(
        self,
        root: str | Path,
        trace: Optional[PipelineTrace] = None,
        runner: Optional[ControlledTreatmentRunner] = None,
    ) -> None:
        self.root = Path(root)
        self.trace = trace or PipelineTrace(
            self.root / "control_state" / "pipeline_trace.jsonl"
        )
        self.runner = runner or ControlledTreatmentRunner(self.root, trace=self.trace)

    def protect_patient(self, patient: Any) -> Any:
        """Return a direct-treatment-blocking proxy for this patient."""
        return self.runner.protect_patient(patient)

    async def run(
        self,
        patient: Any,
        observe_only: bool = True,
        actor: str = "medic.diagnostic_runner",
        verify_health: bool = True,
    ) -> DiagnosticPipelineResult:
        trace_id = self.trace.new_trace_id()
        patient_id = str(getattr(patient, "patient_id", ""))

        started = time.monotonic()
        try:
            vitals = await patient.collect_vitals()
            vitals_dict = self._vitals_dict(vitals)
            self.trace.record(
                trace_id,
                "collect_vitals",
                "ok",
                "Patient vitals collected",
                patient_id=patient_id,
                started_at=started,
                context=vitals_dict,
            )
        except Exception as exc:
            vitals = None
            vitals_dict = {"patient_id": patient_id, "error": str(exc)}
            self.trace.record(
                trace_id,
                "collect_vitals",
                "failed",
                "Patient vitals collection failed",
                patient_id=patient_id,
                started_at=started,
                context=vitals_dict,
            )

        diagnosis = self._diagnose(patient, vitals)
        self.trace.record(
            trace_id,
            "diagnose",
            "ok" if vitals is not None else "failed",
            "Diagnostic rules evaluated",
            patient_id=patient_id,
            context=diagnosis.to_dict(),
        )

        prescription = self._prescribe(patient, diagnosis, vitals_dict)
        prescription_id = str(getattr(prescription, "prescription_id", ""))
        self.trace.record(
            trace_id,
            "prescribe",
            "ok",
            "Prescription generated from diagnosis",
            patient_id=patient_id,
            prescription_id=prescription_id,
            context=self._prescription_dict(prescription),
        )

        second_opinion = self._second_opinion_status(prescription)
        self.trace.record(
            trace_id,
            "second_opinion",
            second_opinion["status"],
            second_opinion["message"],
            patient_id=patient_id,
            prescription_id=prescription_id,
            context=second_opinion,
        )

        runner_result = await self.runner.run(
            patient=patient,
            prescription=prescription,
            observe_only=observe_only,
            actor=actor,
            verify_health=verify_health,
            trace_id=trace_id,
        )

        notes = []
        if observe_only:
            notes.append("observe_only: diagnosis and gateway review only")
        if second_opinion.get("required"):
            notes.append("second opinion is marked required before high-risk execution")

        return DiagnosticPipelineResult(
            status=runner_result.status,
            trace_id=trace_id,
            diagnosis=diagnosis.to_dict(),
            prescription=self._prescription_dict(prescription),
            vitals=vitals_dict,
            second_opinion=second_opinion,
            runner=runner_result.to_dict(),
            notes=notes,
        )

    def _diagnose(self, patient: Any, vitals: Any) -> Diagnosis:
        if vitals is None:
            return Diagnosis(
                severity="CRITICAL",
                root_cause="vitals_collection_failed",
                confidence=0.35,
                risk_level="HIGH",
                reasoning="MEDIC could not read patient vitals.",
            )

        patient_type = self._patient_type(patient, vitals)
        symptoms = [str(item) for item in getattr(vitals, "symptoms", []) or []]
        symptom_text = " ".join(symptoms).lower()
        cpu = float(getattr(vitals, "cpu_percent", 0.0) or 0.0)
        memory = float(getattr(vitals, "memory_percent", 0.0) or 0.0)
        error_rate = float(getattr(vitals, "error_rate", 0.0) or 0.0)
        latency = float(getattr(vitals, "latency_p99_ms", 0.0) or 0.0)
        is_alive = bool(getattr(vitals, "is_alive", True))

        evidence = {
            "patient_type": patient_type,
            "is_alive": is_alive,
            "cpu_percent": cpu,
            "memory_percent": memory,
            "error_rate": error_rate,
            "latency_p99_ms": latency,
            "symptoms": symptoms,
        }

        if not is_alive or self._has_any(
            symptom_text,
            [
                "unreachable",
                "process_not_found",
                "process_missing",
                "connection_failed",
                "health_check_failed",
                "all_candidates_down",
                "not_found",
            ],
        ):
            return Diagnosis(
                severity="CRITICAL",
                root_cause="service_unreachable_or_process_dead",
                confidence=0.95,
                risk_level="HIGH",
                symptoms=symptoms,
                reasoning="Patient is not alive or reports an unreachable service.",
                evidence=evidence,
            )

        if patient_type == PatientType.AI_MODEL.value and (
            "hallucination" in symptom_text or error_rate >= 20.0
        ):
            return Diagnosis(
                severity="HIGH",
                root_cause="ai_hallucination_spike_prompt_or_model_issue",
                confidence=0.88,
                risk_level="MEDIUM",
                symptoms=symptoms,
                reasoning="AI-model error rate is treated as hallucination pressure.",
                evidence=evidence,
            )

        if patient_type == PatientType.AI_MODEL.value and latency >= 5000.0:
            return Diagnosis(
                severity="HIGH",
                root_cause="severe_latency_baseline_drift",
                confidence=0.82,
                risk_level="MEDIUM",
                symptoms=symptoms,
                reasoning="AI-model p99 latency is far above the safe baseline.",
                evidence=evidence,
            )

        if cpu >= 90.0 or self._has_any(symptom_text, ["cpu_overload", "cpu_high", "cpu_throttling"]):
            return Diagnosis(
                severity="HIGH",
                root_cause="cpu_overload",
                confidence=0.86,
                risk_level="MEDIUM",
                symptoms=symptoms,
                reasoning="CPU pressure crossed the high-risk threshold.",
                evidence=evidence,
            )

        if memory >= 90.0 or self._has_any(symptom_text, ["memory_pressure", "memory_high", "oom_killed"]):
            return Diagnosis(
                severity="HIGH",
                root_cause="memory_pressure",
                confidence=0.84,
                risk_level="MEDIUM",
                symptoms=symptoms,
                reasoning="Memory pressure crossed the high-risk threshold.",
                evidence=evidence,
            )

        if error_rate >= 50.0:
            return Diagnosis(
                severity="HIGH",
                root_cause="error_rate_spike",
                confidence=0.82,
                risk_level="MEDIUM",
                symptoms=symptoms,
                reasoning="Observed error rate is high enough to require intervention.",
                evidence=evidence,
            )

        if latency >= 3000.0 or "latency_spike" in symptom_text:
            return Diagnosis(
                severity="HIGH",
                root_cause="latency_spike",
                confidence=0.78,
                risk_level="MEDIUM",
                symptoms=symptoms,
                reasoning="p99 latency crossed the service degradation threshold.",
                evidence=evidence,
            )

        return Diagnosis(
            severity="LOW",
            root_cause="no_issue_detected",
            confidence=0.96,
            risk_level="LOW",
            symptoms=symptoms,
            reasoning="Vitals are within the safe observation band.",
            evidence=evidence,
        )

    def _prescribe(
        self,
        patient: Any,
        diagnosis: Diagnosis,
        vitals: dict[str, Any],
    ) -> Prescription:
        patient_type = str(vitals.get("patient_type") or getattr(patient, "patient_type", ""))
        root_cause = diagnosis.root_cause
        treatment = TreatmentType.MONITOR

        if root_cause == "service_unreachable_or_process_dead":
            treatment = TreatmentType.RESTART
        elif root_cause == "cpu_overload":
            treatment = (
                TreatmentType.K8S_HPA_ADJUST
                if patient_type == PatientType.K8S_WORKLOAD.value
                else TreatmentType.RESTART
            )
        elif root_cause in {
            "ai_hallucination_spike_prompt_or_model_issue",
            "severe_latency_baseline_drift",
        }:
            treatment = TreatmentType.PROMPT_PATCH
        elif root_cause == "memory_pressure":
            treatment = TreatmentType.CONFIG_CHANGE
        elif root_cause in {"error_rate_spike", "latency_spike"}:
            treatment = TreatmentType.RESTART
        elif root_cause == "vitals_collection_failed":
            treatment = TreatmentType.MANUAL_INTERVENTION

        return Prescription(
            patient_id=str(getattr(patient, "patient_id", "")),
            treatment_type=treatment,
            payload={
                "root_cause": root_cause,
                "severity": diagnosis.severity,
                "reasoning": diagnosis.reasoning,
                "symptoms": diagnosis.symptoms,
                "vitals": vitals,
            },
            issued_by="medic.diagnostic_runner",
            confidence=diagnosis.confidence,
            risk_level=diagnosis.risk_level,
        )

    def _second_opinion_status(self, prescription: Prescription) -> dict[str, Any]:
        tx = self._treatment_type(prescription)
        risk = str(getattr(prescription, "risk_level", "") or "").upper()
        required = risk == "HIGH" or tx in self.SECOND_OPINION_TREATMENTS
        if not required:
            return {
                "required": False,
                "status": "skipped",
                "message": "Second opinion not required for low/medium policy risk.",
                "verdict": "not_required",
            }
        return {
            "required": True,
            "status": "required",
            "message": "Second opinion is required before high-risk execution.",
            "verdict": "not_configured",
        }

    @staticmethod
    def _has_any(text: str, needles: list[str]) -> bool:
        return any(needle in text for needle in needles)

    @staticmethod
    def _patient_type(patient: Any, vitals: Any) -> str:
        patient_type = getattr(vitals, "patient_type", None) or getattr(patient, "patient_type", "")
        return str(getattr(patient_type, "value", patient_type) or "")

    @staticmethod
    def _treatment_type(prescription: Any) -> str:
        tx = getattr(prescription, "treatment_type", "")
        return str(getattr(tx, "value", tx) or "")

    @staticmethod
    def _vitals_dict(vitals: Any) -> dict[str, Any]:
        if vitals is None:
            return {}
        patient_type = getattr(vitals, "patient_type", "")
        return {
            "patient_id": str(getattr(vitals, "patient_id", "")),
            "patient_type": str(getattr(patient_type, "value", patient_type) or ""),
            "is_alive": bool(getattr(vitals, "is_alive", False)),
            "cpu_percent": float(getattr(vitals, "cpu_percent", 0.0) or 0.0),
            "memory_percent": float(getattr(vitals, "memory_percent", 0.0) or 0.0),
            "error_rate": float(getattr(vitals, "error_rate", 0.0) or 0.0),
            "latency_p99_ms": float(getattr(vitals, "latency_p99_ms", 0.0) or 0.0),
            "symptoms": list(getattr(vitals, "symptoms", []) or []),
            "treatment_blacklist": list(getattr(vitals, "treatment_blacklist", []) or []),
        }

    @staticmethod
    def _prescription_dict(prescription: Prescription) -> dict[str, Any]:
        return {
            "prescription_id": prescription.prescription_id,
            "patient_id": prescription.patient_id,
            "treatment_type": ControlledDiagnosticRunner._treatment_type(prescription),
            "payload": dict(prescription.payload or {}),
            "issued_by": prescription.issued_by,
            "confidence": prescription.confidence,
            "second_opinion": prescription.second_opinion,
            "risk_level": prescription.risk_level,
            "issued_at": prescription.issued_at,
            "expires_at": prescription.expires_at,
        }
