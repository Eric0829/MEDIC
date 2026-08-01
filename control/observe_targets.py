"""Factory helpers for ObserveLoopRunner patient targets.

The default target is a small medic-self smoke patient. For more realistic
operation, the same observe-only loop can watch the local system or a Python
service with an HTTP health endpoint.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from patient_registry.base_patient import PatientType, TreatmentResult, Vitals
from patient_registry.python_service_patient import PythonServicePatient
from patient_registry.system_patient import SystemPatient


class MedicSelfPatient:
    """Small healthy target used when no external observe target is selected."""

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

    async def apply_treatment(self, prescription: Any) -> TreatmentResult:
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

    def get_metadata(self) -> dict[str, Any]:
        return {
            "patient_id": self.patient_id,
            "patient_type": self.patient_type.value,
            "observe_target": "medic-self",
        }


def build_observe_patient(
    target: str,
    root: str | Path,
    patient_id: str = "",
    service_url: str = "",
    source_root: str = "",
    health_path: str = "/health",
    pid: Optional[int] = None,
    watch_processes: str = "",
    disk_path: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> Any:
    """Build an observe-loop patient from CLI-friendly options."""
    target = (target or "medic-self").strip().lower()
    root_path = Path(root)
    extra_metadata = dict(metadata or {})

    if target == "medic-self":
        return MedicSelfPatient()

    if target == "system":
        return SystemPatient(
            patient_id=patient_id or "local-system",
            watch_processes=_parse_csv(watch_processes),
            disk_path=disk_path or "/",
            metadata={"observe_target": "system", **extra_metadata},
        )

    if target == "python-service":
        if not service_url:
            raise ValueError("--observe-service-url is required for python-service target")
        return PythonServicePatient(
            patient_id=patient_id or _default_service_patient_id(service_url),
            service_url=service_url,
            source_root=source_root or str(root_path),
            health_path=health_path or "/health",
            pid=pid,
            metadata={"observe_target": "python-service", **extra_metadata},
        )

    raise ValueError(f"unknown observe target: {target}")


def _parse_csv(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _default_service_patient_id(service_url: str) -> str:
    cleaned = (
        service_url
        .replace("https://", "")
        .replace("http://", "")
        .replace("/", "-")
        .replace(":", "-")
    )
    return f"python-service-{cleaned or 'target'}"
