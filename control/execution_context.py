"""
execution_context.py
─────────────────────────────────────────────────────────────────────
치료 실행 허가 컨텍스트.

ControlledPatientProxy는 이 컨텍스트가 있을 때만 실제 환자의
apply_treatment() 호출을 통과시킨다.
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class ExecutionGrant:
    """ControlledTreatmentRunner가 발급하는 일회성 실행 허가."""
    trace_id: str
    actor: str
    patient_id: str
    prescription_id: str


_CURRENT_GRANT: ContextVar[Optional[ExecutionGrant]] = ContextVar(
    "medic_execution_grant",
    default=None,
)


@contextmanager
def allow_treatment_execution(
    trace_id: str,
    actor: str,
    patient_id: str,
    prescription_id: str,
) -> Iterator[ExecutionGrant]:
    """Temporarily allow a controlled treatment execution."""
    grant = ExecutionGrant(
        trace_id=trace_id,
        actor=actor,
        patient_id=patient_id,
        prescription_id=prescription_id,
    )
    token = _CURRENT_GRANT.set(grant)
    try:
        yield grant
    finally:
        _CURRENT_GRANT.reset(token)


def current_execution_grant() -> Optional[ExecutionGrant]:
    """Return the active execution grant, if any."""
    return _CURRENT_GRANT.get()
