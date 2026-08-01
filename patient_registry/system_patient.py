"""
system_patient.py
─────────────────────────────────────────────────────────────────────
PC/서버 시스템 자체를 MEDIC 환자로 등록.

증상 수집:
  - CPU 사용률 (전체 + 코어별)
  - 메모리 사용률
  - 디스크 사용률 + I/O
  - 특정 프로세스 감시 (예: ollama, python 등)
  - 온도 (psutil 지원 시)

치료:
  - CONFIG_CHANGE : 불필요한 프로세스 종료 권고 알림
  - MONITOR       : 관찰 유지
  - SCALE_DOWN    : 특정 프로세스 우선순위 낮춤

사용 예시:
    patient = SystemPatient(
        patient_id     = "my-pc",
        watch_processes= ["ollama", "python"],  # 감시할 프로세스명
        cpu_warn       = 80.0,   # CPU 이 이상이면 경고
        mem_warn       = 85.0,   # 메모리 이 이상이면 경고
        disk_warn      = 90.0,   # 디스크 이 이상이면 경고
    )
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    psutil = None
    _PSUTIL_OK = False

from .base_patient import (
    BasePatient, PatientType, Prescription, TreatmentResult,
    TreatmentType, Vitals,
)

logger = logging.getLogger(__name__)


class SystemPatient(BasePatient):
    """
    PC/서버 시스템 전체를 MEDIC 환자로 등록.

    psutil 필요: pip install psutil
    (이미 설치돼 있을 가능성 높음)
    """

    def __init__(
        self,
        patient_id     : str = "local-system",
        watch_processes: list[str] = None,  # 감시할 프로세스 이름 목록
        cpu_warn       : float = 80.0,      # CPU % 경고 임계값
        mem_warn       : float = 85.0,      # 메모리 % 경고 임계값
        disk_warn      : float = 90.0,      # 디스크 % 경고 임계값
        disk_path      : str = "/",         # 감시할 디스크 경로 (Windows: C:\\)
        metadata       : dict = None,
    ) -> None:
        self._patient_id  = patient_id
        self._watch_procs = [p.lower() for p in (watch_processes or [])]
        self._cpu_warn    = cpu_warn
        self._mem_warn    = mem_warn
        self._disk_warn   = disk_warn
        self._disk_path   = disk_path
        self._meta        = metadata or {}

        # Windows 기본 디스크 경로 자동 설정
        import sys
        if sys.platform == "win32" and disk_path == "/":
            self._disk_path = "C:\\"

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.GENERIC_PROCESS

    async def collect_vitals(self) -> Vitals:
        if not _PSUTIL_OK:
            return Vitals(
                patient_id    = self._patient_id,
                patient_type  = self.patient_type,
                is_alive      = True,
                symptoms      = ["psutil_not_installed"],
                cpu_percent   = 0.0,
                memory_percent= 0.0,
                error_rate    = 0.0,
                latency_p99_ms= 0.0,
            )

        symptoms = []

        # ── CPU ──────────────────────────────────────────────────
        cpu_pct = psutil.cpu_percent(interval=1.0)
        cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        cpu_freq = psutil.cpu_freq()

        if cpu_pct > self._cpu_warn:
            symptoms.append(f"cpu_high:{cpu_pct:.0f}%")

        # 특정 코어만 100%인 경우 감지
        maxed_cores = sum(1 for c in cpu_per_core if c >= 95)
        if maxed_cores > 0:
            symptoms.append(f"cpu_cores_maxed:{maxed_cores}개")

        # ── 메모리 ──────────────────────────────────────────────
        mem = psutil.virtual_memory()
        mem_pct = mem.percent

        if mem_pct > self._mem_warn:
            symptoms.append(f"memory_high:{mem_pct:.0f}%")

        # 스왑 사용률
        swap = psutil.swap_memory()
        if swap.total > 0 and swap.percent > 50:
            symptoms.append(f"swap_high:{swap.percent:.0f}%")

        # ── 디스크 ──────────────────────────────────────────────
        try:
            disk = psutil.disk_usage(self._disk_path)
            disk_pct = disk.percent
            if disk_pct > self._disk_warn:
                symptoms.append(f"disk_high:{disk_pct:.0f}%")
        except Exception:
            disk_pct = 0.0

        # ── 감시 프로세스 ─────────────────────────────────────────
        watched_stats = {}
        if self._watch_procs:
            for proc in psutil.process_iter(
                ["name", "cpu_percent", "memory_percent", "status"]
            ):
                try:
                    name = proc.info["name"].lower()
                    for watch in self._watch_procs:
                        if watch in name:
                            if watch not in watched_stats:
                                watched_stats[watch] = {
                                    "cpu": 0.0, "mem": 0.0, "count": 0
                                }
                            raw_cpu = proc.info["cpu_percent"] or 0
                            # 멀티코어 정규화: psutil은 코어 합산으로 반환
                            cpu_count = psutil.cpu_count() or 1
                            normalized_cpu = raw_cpu / cpu_count
                            watched_stats[watch]["cpu"] += normalized_cpu
                            watched_stats[watch]["mem"] += proc.info["memory_percent"] or 0
                            watched_stats[watch]["count"] += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # 감시 프로세스가 없으면 증상 추가
            for watch in self._watch_procs:
                if watch not in watched_stats:
                    symptoms.append(f"process_missing:{watch}")
                elif watched_stats[watch]["cpu"] > self._cpu_warn:
                    symptoms.append(
                        f"process_cpu_high:{watch}:{watched_stats[watch]['cpu']:.0f}%"
                    )

        # ── 커스텀 지표 구성 ──────────────────────────────────────
        custom = {
            "cpu_percent"    : round(cpu_pct, 1),
            "cpu_per_core"   : [round(c, 1) for c in cpu_per_core],
            "cpu_freq_mhz"   : round(cpu_freq.current, 0) if cpu_freq else 0,
            "memory_total_gb": round(mem.total / 1e9, 1),
            "memory_used_gb" : round(mem.used / 1e9, 1),
            "memory_percent" : round(mem_pct, 1),
            "swap_percent"   : round(swap.percent, 1),
            "disk_percent"   : round(disk_pct, 1),
            "watched_procs"  : watched_stats,
        }

        # 온도 (지원하는 시스템만)
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                all_temps = [
                    t.current
                    for readings in temps.values()
                    for t in readings
                ]
                if all_temps:
                    max_temp = max(all_temps)
                    custom["max_temp_c"] = round(max_temp, 1)
                    if max_temp > 85:
                        symptoms.append(f"temperature_high:{max_temp:.0f}C")
        except (AttributeError, Exception):
            pass

        return Vitals(
            patient_id    = self._patient_id,
            patient_type  = self.patient_type,
            is_alive      = True,
            cpu_percent   = cpu_pct,
            memory_percent= mem_pct,
            error_rate    = 0.0,
            latency_p99_ms= 0.0,
            symptoms      = symptoms,
            custom_metrics= custom,
        )

    async def report_health(self) -> bool:
        return _PSUTIL_OK

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        before = await self.collect_vitals()
        tx = prescription.treatment_type

        if tx == TreatmentType.MONITOR:
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=True, message="시스템 모니터링 유지",
                before_vitals=before,
            )

        elif tx == TreatmentType.CONFIG_CHANGE:
            payload = prescription.payload
            msg_parts = []

            # Ollama 메모리 압박 → 모델 언로드 시도
            if payload.get("ollama_unload") and _PSUTIL_OK:
                unloaded = 0
                for proc in psutil.process_iter(["name", "pid", "memory_percent"]):
                    try:
                        if "ollama" in proc.info["name"].lower():
                            mem = proc.info["memory_percent"] or 0
                            if mem > 10:  # 10% 이상 쓰는 ollama 프로세스
                                logger.warning(
                                    f"[SystemPatient] Ollama 메모리 압박 감지 "
                                    f"(PID:{proc.info['pid']} MEM:{mem:.1f}%) "
                                    f"→ 수동으로 'ollama stop' 권고"
                                )
                                unloaded += 1
                    except Exception:
                        pass
                if unloaded:
                    msg_parts.append(
                        f"Ollama {unloaded}개 프로세스 메모리 과다 — "
                        f"터미널에서 'ollama stop' 실행 권고"
                    )

            if payload.get("suggest_kill"):
                procs = payload["suggest_kill"]
                msg_parts.append(f"종료 권고: {', '.join(procs)}")
                logger.warning(f"[SystemPatient] 종료 권고: {procs}")

            if not msg_parts:
                msg_parts.append("시스템 설정 최적화 권고")

            after = await self.collect_vitals()
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=True,
                message=" | ".join(msg_parts),
                before_vitals=before,
                after_vitals=after,
            )

        elif tx == TreatmentType.SCALE_DOWN:
            # 감시 프로세스 우선순위 낮춤
            results = []
            if _PSUTIL_OK:
                for proc in psutil.process_iter(["name", "pid"]):
                    try:
                        name = proc.info["name"].lower()
                        for watch in self._watch_procs:
                            if watch in name:
                                proc.nice(10)  # 낮은 우선순위
                                results.append(
                                    f"{proc.info['name']}(PID:{proc.info['pid']})"
                                )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            after = await self.collect_vitals()
            msg = f"우선순위 낮춤: {', '.join(results)}" if results else "대상 없음"
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=bool(results),
                message=msg,
                before_vitals=before,
                after_vitals=after,
            )

        else:
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=False,
                message=f"시스템 환자에 지원하지 않는 치료: {tx.value}",
                before_vitals=before,
            )

    def get_metadata(self) -> dict:
        info = {}
        if _PSUTIL_OK:
            try:
                info = {
                    "cpu_count"     : psutil.cpu_count(),
                    "total_memory_gb": round(psutil.virtual_memory().total / 1e9, 1),
                }
            except Exception:
                pass
        return {
            "patient_id"  : self._patient_id,
            "patient_type": self.patient_type.value,
            "watch_procs" : self._watch_procs,
            **info,
            **self._meta,
        }
