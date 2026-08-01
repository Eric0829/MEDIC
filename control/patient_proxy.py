"""
patient_proxy.py
─────────────────────────────────────────────────────────────────────
MEDIC 환자 보호 프록시.

환자 객체를 이 프록시로 감싸면 direct apply_treatment() 호출이 차단된다.
ControlledTreatmentRunner가 발급한 ExecutionGrant가 있을 때만 실제
환자의 apply_treatment()를 호출한다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from typing import Any, Optional

from control.audit_log import AuditLog
from control.execution_context import current_execution_grant
from patient_registry.base_patient import Prescription, TreatmentResult


class ControlledPatientProxy:
    """Wrap a patient and block treatment execution outside the runner."""

    def __init__(
        self,
        patient: Any,
        audit_log: Optional[AuditLog] = None,
        actor: str = "medic.patient_proxy",
    ) -> None:
        self._patient = patient
        self._audit_log = audit_log
        self._actor = actor

    @property
    def wrapped_patient(self) -> Any:
        return self._patient

    @property
    def patient_id(self) -> str:
        return str(getattr(self._patient, "patient_id", ""))

    @property
    def patient_type(self) -> Any:
        return getattr(self._patient, "patient_type", "")

    async def collect_vitals(self) -> Any:
        return await self._patient.collect_vitals()

    async def report_health(self) -> bool:
        return bool(await self._patient.report_health())

    async def get_source_code(self, file_path: str) -> Optional[str]:
        if hasattr(self._patient, "get_source_code"):
            return await self._patient.get_source_code(file_path)
        return None

    async def get_recent_logs(self, lines: int = 500) -> str:
        if hasattr(self._patient, "get_recent_logs"):
            return await self._patient.get_recent_logs(lines)
        return ""

    def get_treatment_blacklist(self) -> list[Any]:
        if hasattr(self._patient, "get_treatment_blacklist"):
            return list(self._patient.get_treatment_blacklist() or [])
        return []

    def get_metadata(self) -> dict[str, Any]:
        if hasattr(self._patient, "get_metadata"):
            return dict(self._patient.get_metadata() or {})
        return {
            "patient_id": self.patient_id,
            "patient_type": str(getattr(self.patient_type, "value", self.patient_type)),
            "protected_by": "ControlledPatientProxy",
        }

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        grant = current_execution_grant()
        prescription_id = str(getattr(prescription, "prescription_id", ""))
        patient_id = str(getattr(prescription, "patient_id", self.patient_id))

        if not self._grant_matches(grant, patient_id, prescription_id):
            return await self._blocked_result(prescription, grant)

        return await self._patient.apply_treatment(prescription)

    def __getattr__(self, name: str) -> Any:
        if name == "apply_treatment":
            raise AttributeError(name)
        return getattr(self._patient, name)

    @staticmethod
    def _grant_matches(grant: Any, patient_id: str, prescription_id: str) -> bool:
        if grant is None:
            return False
        return (
            str(getattr(grant, "patient_id", "")) == patient_id
            and str(getattr(grant, "prescription_id", "")) == prescription_id
        )

    async def _blocked_result(
        self,
        prescription: Prescription,
        grant: Any,
    ) -> TreatmentResult:
        before = None
        try:
            before = await self.collect_vitals()
        except Exception:
            before = None

        patient_id = str(getattr(prescription, "patient_id", self.patient_id))
        prescription_id = str(getattr(prescription, "prescription_id", ""))
        message = "direct apply_treatment blocked; use ControlledTreatmentRunner"

        if self._audit_log:
            self._audit_log.record(
                event_type="direct_treatment_blocked",
                actor=self._actor,
                patient_id=patient_id,
                message=message,
                context={
                    "prescription_id": prescription_id,
                    "grant_present": grant is not None,
                },
            )

        return TreatmentResult(
            prescription_id=prescription_id,
            patient_id=patient_id,
            success=False,
            message=message,
            before_vitals=before,
            side_effects=["blocked_by_control_proxy"],
        )
