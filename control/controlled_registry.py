"""
controlled_registry.py
─────────────────────────────────────────────────────────────────────
통제 환자 등록소.

MEDIC에 들어오는 환자는 이 등록소를 통해 ControlledPatientProxy로
감싸진다. 운영 코드가 등록소에서 환자를 꺼내 쓰면 apply_treatment()
직접 실행 우회가 기본적으로 차단된다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from control.audit_log import AuditLog
from control.file_store import read_text_locked, write_text_locked
from control.patient_proxy import ControlledPatientProxy


@dataclass
class RegisteredPatient:
    """등록된 환자 메타데이터."""
    patient_id: str
    patient_type: str
    protected: bool = True
    registered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patient_id": self.patient_id,
            "patient_type": self.patient_type,
            "protected": self.protected,
            "registered_at": self.registered_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegisteredPatient":
        return cls(
            patient_id=str(data.get("patient_id", "")),
            patient_type=str(data.get("patient_type", "")),
            protected=bool(data.get("protected", False)),
            registered_at=str(data.get("registered_at", "")),
            metadata=dict(data.get("metadata", {}) or {}),
        )


class ControlledPatientRegistry:
    """Register patients and always return protected proxies."""

    def __init__(
        self,
        root: str | Path,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        self.root = Path(root)
        self.audit_log = audit_log or AuditLog(
            self.root / "control_state" / "audit.jsonl"
        )
        self.path = self.root / "control_state" / "patient_registry.json"
        self._patients: dict[str, ControlledPatientProxy] = {}
        self._records: dict[str, RegisteredPatient] = self._load()

    def register(
        self,
        patient: Any,
        replace: bool = False,
    ) -> ControlledPatientProxy:
        """Register a patient and return its protected proxy."""
        proxy = self._protect(patient)
        patient_id = proxy.patient_id
        if not patient_id:
            raise ValueError("patient_id is required")
        if patient_id in self._patients and not replace:
            raise ValueError(f"patient already registered: {patient_id}")

        record = RegisteredPatient(
            patient_id=patient_id,
            patient_type=self._patient_type(proxy),
            protected=True,
            metadata=self._safe_metadata(proxy),
        )
        self._patients[patient_id] = proxy
        self._records[patient_id] = record
        self._save()
        self.audit_log.record(
            event_type="patient_registered",
            actor="medic.controlled_registry",
            patient_id=patient_id,
            message="patient registered with ControlledPatientProxy",
            context=record.to_dict(),
        )
        return proxy

    def unregister(self, patient_id: str) -> bool:
        """Remove a patient from runtime and persisted metadata."""
        existed = patient_id in self._patients or patient_id in self._records
        self._patients.pop(patient_id, None)
        self._records.pop(patient_id, None)
        if existed:
            self._save()
            self.audit_log.record(
                event_type="patient_unregistered",
                actor="medic.controlled_registry",
                patient_id=patient_id,
                message="patient removed from registry",
            )
        return existed

    def get(self, patient_id: str) -> ControlledPatientProxy:
        """Return a runtime protected patient proxy."""
        if patient_id not in self._patients:
            raise KeyError(patient_id)
        return self._patients[patient_id]

    def list(self) -> list[RegisteredPatient]:
        return list(self._records.values())

    def stats(self) -> dict[str, Any]:
        protected_persisted = sum(1 for r in self._records.values() if r.protected)
        return {
            "path": str(self.path),
            "runtime_patients": len(self._patients),
            "persisted_patients": len(self._records),
            "protected_persisted": protected_persisted,
            "unprotected_persisted": len(self._records) - protected_persisted,
            "patient_ids": sorted(self._records.keys()),
        }

    def _protect(self, patient: Any) -> ControlledPatientProxy:
        if isinstance(patient, ControlledPatientProxy):
            return patient
        return ControlledPatientProxy(patient, audit_log=self.audit_log)

    @staticmethod
    def _patient_type(patient: Any) -> str:
        patient_type = getattr(patient, "patient_type", "")
        return str(getattr(patient_type, "value", patient_type) or "")

    @staticmethod
    def _safe_metadata(patient: Any) -> dict[str, Any]:
        metadata = {}
        if hasattr(patient, "get_metadata"):
            try:
                metadata = dict(patient.get_metadata() or {})
            except Exception:
                metadata = {}
        safe = {}
        for key, value in metadata.items():
            try:
                json.dumps(value, ensure_ascii=False)
                safe[str(key)] = value
            except TypeError:
                safe[str(key)] = str(value)
        safe.setdefault("protected_by", "ControlledPatientProxy")
        return safe

    def _load(self) -> dict[str, RegisteredPatient]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(read_text_locked(self.path))
            records = {}
            for item in raw:
                rec = RegisteredPatient.from_dict(item)
                if rec.patient_id:
                    records[rec.patient_id] = rec
            return records
        except Exception:
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = [record.to_dict() for record in self._records.values()]
        write_text_locked(
            self.path,
            json.dumps(data, ensure_ascii=False, indent=2),
        )
