"""
base_patient.py
─────────────────────────────────────────────────────────────────────
범용 환자 인터페이스.

MEDIC 의 가장 중요한 설계 원칙:
  "환자는 자신을 고치지 않는다."

모든 환자(Python 서버, AI 모델, K8s 클러스터, 다른 에이전트)는
이 인터페이스를 구현하여 MEDIC 에 등록된다.

환자의 역할:
  1. 증상을 수집해서 보고한다  (collect_vitals)
  2. MEDIC 이 처방한 치료를 수동적으로 받는다  (apply_treatment)
  3. 치료 후 상태를 보고한다  (report_health)

환자가 절대 하지 않는 것:
  - 자신의 코드를 직접 수정하는 것
  - 치료 방향을 결정하는 것
  - 다른 환자의 상태를 보는 것

편향 방지 원칙:
  자가 수정 시스템은 자신의 오류 패턴을 오류로 인식하지 못한다.
  MEDIC 은 환자와 완전히 분리된 독립 개체다.
  환자는 MEDIC 의 내부 로직에 접근할 수 없다.
─────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ── 환자 종류 ────────────────────────────────────────────────────────

class PatientType(str, Enum):
    PYTHON_SERVICE  = "python_service"   # FastAPI, Flask, Django 등
    AI_MODEL        = "ai_model"         # LLM, 에이전트, 추론 서버
    K8S_WORKLOAD    = "k8s_workload"     # Deployment, StatefulSet 등
    JS_SERVICE      = "js_service"       # Node.js, Deno 등
    DATABASE        = "database"         # Postgres, Redis, MongoDB 등
    GENERIC_PROCESS = "generic_process"  # 기타 모든 프로세스


# ── 생체 지표 (환자 종류에 따라 의미가 다름) ──────────────────────────

@dataclass
class Vitals:
    """
    환자의 현재 상태 스냅샷.
    
    모든 환자가 공통으로 보고하는 지표.
    환자 종류에 따라 custom_metrics 에 추가 지표를 담는다.
    """
    patient_id      : str
    patient_type    : PatientType
    timestamp       : str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # 공통 지표
    is_alive        : bool  = True    # 프로세스/서비스가 응답하는가
    cpu_percent     : float = 0.0
    memory_percent  : float = 0.0
    error_rate      : float = 0.0     # 최근 1분 에러율 (%)
    latency_p99_ms  : float = 0.0     # 99th percentile 응답 지연

    # 환자 종류별 추가 지표
    custom_metrics  : dict[str, Any] = field(default_factory=dict)

    # 이상 징후 목록 (환자가 스스로 탐지한 것)
    symptoms        : list[str] = field(default_factory=list)

    # 치료 거부 여부 (환자가 특정 치료를 거부할 수 있음)
    treatment_blacklist: list[str] = field(default_factory=list)


# ── 치료 처방전 ──────────────────────────────────────────────────────

class TreatmentType(str, Enum):
    PATCH_CODE          = "patch_code"
    RESTART             = "restart"
    ROLLBACK            = "rollback"
    SCALE_DOWN          = "scale_down"
    QUARANTINE          = "quarantine"
    CONFIG_CHANGE       = "config_change"
    MONITOR             = "monitor"             # 이상 없음 — 관찰 유지
    PROMPT_PATCH        = "prompt_patch"        # AI 전용
    WEIGHT_ROLLBACK     = "weight_rollback"     # AI 전용
    FINE_TUNE_TRIGGER   = "fine_tune_trigger"   # AI 전용
    K8S_ROLLING_UPDATE  = "k8s_rolling_update"  # K8s 전용
    K8S_HPA_ADJUST      = "k8s_hpa_adjust"     # K8s 전용
    MANUAL_INTERVENTION = "manual_intervention" # 사람 호출


@dataclass
class Prescription:
    """
    MEDIC 이 환자에게 내리는 처방전.
    
    환자는 이 처방을 받아 apply_treatment() 로 실행한다.
    환자는 처방 내용을 수정할 수 없다.
    (거부권은 있다 — treatment_blacklist)
    """
    prescription_id   : str = field(default_factory=lambda: str(uuid.uuid4()))
    patient_id        : str = ""
    treatment_type    : TreatmentType = TreatmentType.MANUAL_INTERVENTION
    payload           : dict[str, Any] = field(default_factory=dict)
    
    # 처방 메타데이터
    issued_by         : str = "medic.core"        # 처방을 내린 모듈
    confidence        : float = 0.0               # 처방 신뢰도 (0~1)
    second_opinion    : bool = False              # 2차 검토 통과 여부
    risk_level        : str = "LOW"               # LOW / MEDIUM / HIGH
    
    issued_at         : str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    expires_at        : Optional[str] = None      # 처방 유효기간


@dataclass
class TreatmentResult:
    """환자가 치료 후 MEDIC 에 반환하는 결과 보고서."""
    prescription_id : str
    patient_id      : str
    success         : bool
    message         : str = ""
    before_vitals   : Optional[Vitals] = None
    after_vitals    : Optional[Vitals] = None
    applied_at      : str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    side_effects    : list[str] = field(default_factory=list)


# ── 환자 기본 클래스 ─────────────────────────────────────────────────

class BasePatient(ABC):
    """
    모든 환자가 구현해야 하는 인터페이스.
    
    구현 예시:
        class MyPythonServer(BasePatient):
            @property
            def patient_id(self): return "api-gateway-prod"
            
            @property  
            def patient_type(self): return PatientType.PYTHON_SERVICE
            
            async def collect_vitals(self) -> Vitals: ...
            async def apply_treatment(self, rx: Prescription) -> TreatmentResult: ...
            async def report_health(self) -> bool: ...
    """

    # ── 필수 구현 메서드 ────────────────────────────────────────────

    @property
    @abstractmethod
    def patient_id(self) -> str:
        """이 환자의 고유 식별자. 변경 불가."""
        ...

    @property
    @abstractmethod
    def patient_type(self) -> PatientType:
        """이 환자의 종류."""
        ...

    @abstractmethod
    async def collect_vitals(self) -> Vitals:
        """
        현재 상태를 수집해서 반환한다.
        
        MEDIC 은 이 메서드를 주기적으로 호출해 환자를 모니터링한다.
        환자는 최대한 정확한 정보를 제공해야 한다.
        정보를 숨기거나 가공하면 안 된다.
        """
        ...

    @abstractmethod
    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        """
        MEDIC 이 처방한 치료를 적용한다.
        
        환자는 처방 내용을 수정할 수 없다.
        treatment_blacklist 에 있는 치료 유형은 거부할 수 있다.
        거부 시 TreatmentResult(success=False, message="blacklisted") 반환.
        """
        ...

    @abstractmethod
    async def report_health(self) -> bool:
        """
        현재 정상 동작 여부를 반환한다.
        True = 정상, False = 비정상.
        
        이 메서드는 치료 후 회복 확인에 사용된다.
        """
        ...

    # ── 선택적 구현 메서드 (기본 구현 제공) ─────────────────────────

    async def get_source_code(self, file_path: str) -> Optional[str]:
        """
        패치 생성을 위해 소스 코드를 제공한다.
        
        보안상 이유로 특정 파일은 제공을 거부할 수 있다.
        기본값: None (소스 코드 미제공)
        """
        return None

    async def get_recent_logs(self, lines: int = 500) -> str:
        """
        최근 로그를 반환한다.
        기본값: 빈 문자열
        """
        return ""

    def get_treatment_blacklist(self) -> list[TreatmentType]:
        """
        이 환자에게 적용 불가능한 치료 유형 목록.
        예: AI 모델은 RESTART 는 허용하지만 PATCH_CODE 는 거부할 수 있다.
        """
        return []

    def get_metadata(self) -> dict[str, Any]:
        """
        환자에 대한 부가 정보.
        예: 버전, 담당팀, 중요도, 의존 서비스 목록 등
        """
        return {
            "patient_id"  : self.patient_id,
            "patient_type": self.patient_type.value,
        }
