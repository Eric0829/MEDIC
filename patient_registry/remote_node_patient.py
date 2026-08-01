from __future__ import annotations

import logging
import time
from typing import Any, Optional

try:
    import httpx
    _HTTPX_OK = True
except ImportError:
    from infrastructure import httpx_mock as httpx
    _HTTPX_OK = False

from .base_patient import (
    BasePatient,
    PatientType,
    Prescription,
    TreatmentResult,
    TreatmentType,
    Vitals,
)

logger = logging.getLogger(__name__)


class RemoteNodePatient(BasePatient):
    """
    원격 노드/서비스를 HTTP 엔드포인트 기반 환자로 다루는 어댑터.

    기대 엔드포인트:
      GET  /health    -> 상태 확인
      GET  /status    -> 선택적 상세 지표
      POST /treatment -> 선택적 원격 조치 위임
    """

    def __init__(
        self,
        patient_id: str,
        base_url: str,
        node_role: str = "remote-node",
        health_path: str = "/health",
        status_path: str = "/status",
        treatment_path: str = "/treatment",
        source_hint: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self._patient_id = patient_id
        self._base_url = base_url.rstrip("/")
        self._node_role = node_role
        self._health_path = health_path
        self._status_path = status_path
        self._treatment_path = treatment_path
        self._source_hint = source_hint
        self._meta = metadata or {}

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.GENERIC_PROCESS

    async def collect_vitals(self) -> Vitals:
        latency_ms = 0.0
        symptoms: list[str] = []
        is_alive = False
        health_payload: dict[str, Any] = {}
        status_payload: dict[str, Any] = {}

        if not _HTTPX_OK:
            symptoms.append("httpx_not_installed")
            return Vitals(
                patient_id=self._patient_id,
                patient_type=self.patient_type,
                is_alive=False,
                symptoms=symptoms,
                custom_metrics={"base_url": self._base_url, "node_role": self._node_role},
            )

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                t0 = time.monotonic()
                health_resp = await client.get(f"{self._base_url}{self._health_path}")
                latency_ms = (time.monotonic() - t0) * 1000
                is_alive = health_resp.status_code < 500
                health_payload = self._safe_json(health_resp)
                if health_resp.status_code >= 400:
                    symptoms.append(f"remote_health_status:{health_resp.status_code}")
                try:
                    status_resp = await client.get(f"{self._base_url}{self._status_path}")
                    status_payload = self._safe_json(status_resp)
                except Exception as exc:
                    symptoms.append(f"remote_status_failed:{type(exc).__name__}")
        except Exception as exc:
            symptoms.append(f"remote_health_failed:{type(exc).__name__}")
            logger.debug("[RemoteNodePatient] health check failed for %s: %s", self._patient_id, exc)

        merged = {**status_payload, **health_payload}
        remote_symptoms = merged.get("symptoms", [])
        if isinstance(remote_symptoms, list):
            symptoms.extend(str(item) for item in remote_symptoms[:10])

        return Vitals(
            patient_id=self._patient_id,
            patient_type=self.patient_type,
            is_alive=is_alive,
            cpu_percent=float(merged.get("cpu_percent", 0.0) or 0.0),
            memory_percent=float(merged.get("memory_percent", 0.0) or 0.0),
            error_rate=float(merged.get("error_rate", 0.0) or 0.0),
            latency_p99_ms=float(merged.get("latency_p99_ms", latency_ms) or latency_ms),
            symptoms=symptoms,
            custom_metrics={
                "base_url": self._base_url,
                "node_role": self._node_role,
                "remote_status": merged,
            },
        )

    async def report_health(self) -> bool:
        if not _HTTPX_OK:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._base_url}{self._health_path}")
                return resp.status_code < 500
        except Exception:
            return False

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        before = await self.collect_vitals()
        if prescription.treatment_type == TreatmentType.MONITOR:
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=True,
                message="remote monitor only",
                before_vitals=before,
            )

        success, message = await self._proxy_treatment(prescription)
        after = await self.collect_vitals()
        return TreatmentResult(
            prescription_id=prescription.prescription_id,
            patient_id=self._patient_id,
            success=success,
            message=message,
            before_vitals=before,
            after_vitals=after,
        )

    async def get_source_code(self, file_path: str) -> Optional[str]:
        if not self._source_hint:
            return None
        return f"# remote node source hint\n# {self._source_hint}\n"

    def get_metadata(self) -> dict[str, Any]:
        return {
            "patient_id": self._patient_id,
            "patient_type": self.patient_type.value,
            "base_url": self._base_url,
            "node_role": self._node_role,
            "source_hint": self._source_hint,
            **self._meta,
        }

    async def _proxy_treatment(self, prescription: Prescription) -> tuple[bool, str]:
        if not _HTTPX_OK:
            return False, "httpx_not_installed"
        payload = {
            "patient_id": self._patient_id,
            "treatment_type": prescription.treatment_type.value,
            "payload": prescription.payload,
            "issued_by": prescription.issued_by,
            "risk_level": prescription.risk_level,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{self._base_url}{self._treatment_path}", json=payload)
            if resp.status_code >= 400:
                return False, f"remote_treatment_failed:{resp.status_code}"
            data = self._safe_json(resp)
            if isinstance(data, dict):
                ok = bool(data.get("success", True))
                msg = str(data.get("message", "remote treatment applied"))
                return ok, msg
            return True, "remote treatment applied"
        except Exception as exc:
            return False, f"remote_treatment_error:{type(exc).__name__}"

    @staticmethod
    def _safe_json(resp: Any) -> dict[str, Any]:
        try:
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
