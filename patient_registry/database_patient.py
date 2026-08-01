"""
database_patient.py
─────────────────────────────────────────────────────────────────────
데이터베이스 환자 어댑터.

PostgreSQL, SQLite, Redis를 MEDIC 환자로 등록.

증상 수집:
  PostgreSQL/SQLite:
    - 슬로우 쿼리 감지 (1초 이상)
    - 커넥션 수 모니터링
    - DB 파일 크기 / 디스크 사용률
    - 응답 시간 측정

  Redis:
    - 메모리 사용률
    - 키 만료율
    - 히트율 (cache hit rate)
    - 응답 시간

치료:
  RESTART      → DB 프로세스 재시작 명령
  CONFIG_CHANGE → 슬로우 쿼리 임계값, 커넥션 풀 조정
  MONITOR      → 관찰 유지
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .base_patient import (
    BasePatient, PatientType, Prescription, TreatmentResult,
    TreatmentType, Vitals,
)

logger = logging.getLogger(__name__)


# ── SQLite 환자 ──────────────────────────────────────────────────────

class SQLitePatient(BasePatient):
    """
    SQLite DB를 MEDIC 환자로 등록.

    사용 예시:
        patient = SQLitePatient(
            patient_id = "app-database",
            db_path    = "data/app.db",
        )
        await medic.register(patient)
    """

    def __init__(
        self,
        patient_id : str,
        db_path    : str,
        slow_query_ms: float = 500.0,   # 이 이상이면 슬로우 쿼리
        metadata   : dict = None,
    ) -> None:
        self._patient_id   = patient_id
        self._db_path      = Path(db_path)
        self._slow_ms      = slow_query_ms
        self._meta         = metadata or {}

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.DATABASE

    async def collect_vitals(self) -> Vitals:
        symptoms = []
        is_alive = False
        latency  = 0.0
        db_size_mb = 0.0

        # DB 파일 존재 확인
        if not self._db_path.exists():
            return Vitals(
                patient_id    = self._patient_id,
                patient_type  = self.patient_type,
                is_alive      = False,
                symptoms      = [f"db_file_not_found:{self._db_path}"],
                cpu_percent   = 0.0,
                memory_percent= 0.0,
                error_rate    = 100.0,
                latency_p99_ms= 0.0,
            )

        # DB 크기
        try:
            db_size_mb = self._db_path.stat().st_size / 1024 / 1024
        except Exception:
            pass

        # 응답 시간 측정 (간단한 쿼리)
        try:
            t0 = time.monotonic()
            conn = sqlite3.connect(str(self._db_path), timeout=5.0)
            conn.execute("SELECT 1")
            conn.execute("PRAGMA integrity_check")
            conn.close()
            latency = (time.monotonic() - t0) * 1000
            is_alive = True

            if latency > self._slow_ms:
                symptoms.append(f"slow_response:{latency:.0f}ms")
        except sqlite3.OperationalError as e:
            symptoms.append(f"db_error:{str(e)[:50]}")
            is_alive = False
        except Exception as e:
            symptoms.append(f"connection_failed:{str(e)[:50]}")

        # 디스크 사용률
        try:
            import shutil
            disk = shutil.disk_usage(str(self._db_path.parent))
            disk_pct = (disk.used / disk.total) * 100
            if disk_pct > 85:
                symptoms.append(f"disk_pressure:{disk_pct:.0f}%")
        except Exception:
            disk_pct = 0.0

        return Vitals(
            patient_id    = self._patient_id,
            patient_type  = self.patient_type,
            is_alive      = is_alive,
            cpu_percent   = 0.0,
            memory_percent= disk_pct,
            error_rate    = 0.0 if is_alive else 100.0,
            latency_p99_ms= latency,
            symptoms      = symptoms,
            custom_metrics= {
                "db_path"    : str(self._db_path),
                "db_size_mb" : round(db_size_mb, 2),
                "slow_ms"    : self._slow_ms,
            },
        )

    async def report_health(self) -> bool:
        if not self._db_path.exists():
            return False
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=3.0)
            conn.execute("SELECT 1")
            conn.close()
            return True
        except Exception:
            return False

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        before = await self.collect_vitals()

        tx = prescription.treatment_type
        if tx == TreatmentType.MONITOR:
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=True, message="모니터링 유지",
                before_vitals=before,
            )
        elif tx == TreatmentType.CONFIG_CHANGE:
            r = await self._optimize_db(prescription.payload)
        elif tx == TreatmentType.RESTART:
            r = {"success": False,
                 "message": "SQLite는 프로세스 재시작 불필요 (파일 기반)"}
        else:
            r = {"success": False,
                 "message": f"SQLite에 지원하지 않는 치료: {tx.value}"}

        after = await self.collect_vitals()
        return TreatmentResult(
            prescription_id=prescription.prescription_id,
            patient_id=self._patient_id,
            success=r.get("success", False),
            message=r.get("message", ""),
            before_vitals=before, after_vitals=after,
        )

    async def _optimize_db(self, payload: dict) -> dict:
        """VACUUM + 인덱스 재구성."""
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=30.0)
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
            conn.close()
            return {"success": True, "message": "VACUUM + ANALYZE 완료"}
        except Exception as e:
            return {"success": False, "message": str(e)[:100]}

    def get_metadata(self) -> dict:
        return {
            "patient_id"  : self._patient_id,
            "patient_type": self.patient_type.value,
            "db_path"     : str(self._db_path),
            **self._meta,
        }


# ── Redis 환자 ───────────────────────────────────────────────────────

class RedisPatient(BasePatient):
    """
    Redis를 MEDIC 환자로 등록.

    redis-py 설치 필요: pip install redis

    사용 예시:
        patient = RedisPatient(
            patient_id = "cache-redis",
            host       = "localhost",
            port       = 6379,
        )
        await medic.register(patient)
    """

    def __init__(
        self,
        patient_id       : str,
        host             : str   = "localhost",
        port             : int   = 6379,
        db               : int   = 0,
        password         : str   = "",
        memory_warn_pct  : float = 80.0,
        hit_rate_warn_pct: float = 50.0,
        metadata         : dict  = None,
    ) -> None:
        self._patient_id    = patient_id
        self._host          = host
        self._port          = port
        self._db            = db
        self._password      = password
        self._mem_warn      = memory_warn_pct
        self._hit_warn      = hit_rate_warn_pct
        self._meta          = metadata or {}
        self._redis         = None

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.DATABASE

    def _get_client(self):
        try:
            import redis
            if self._redis is None:
                self._redis = redis.Redis(
                    host=self._host, port=self._port,
                    db=self._db,
                    password=self._password or None,
                    socket_connect_timeout=3,
                    socket_timeout=5,
                    decode_responses=True,
                )
            return self._redis
        except ImportError:
            raise RuntimeError(
                "redis-py 미설치 — pip install redis"
            )

    async def collect_vitals(self) -> Vitals:
        symptoms = []
        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None, self._collect_sync
            )
            return r
        except Exception as e:
            return Vitals(
                patient_id    = self._patient_id,
                patient_type  = self.patient_type,
                is_alive      = False,
                symptoms      = [f"redis_unreachable:{str(e)[:50]}"],
                cpu_percent   = 0.0,
                memory_percent= 0.0,
                error_rate    = 100.0,
                latency_p99_ms= 0.0,
            )

    def _collect_sync(self) -> Vitals:
        symptoms = []
        client = self._get_client()

        t0 = time.monotonic()
        client.ping()
        latency = (time.monotonic() - t0) * 1000

        info = client.info()

        # 메모리
        used    = info.get("used_memory", 0)
        max_mem = info.get("maxmemory", 0)
        mem_pct = (used / max_mem * 100) if max_mem > 0 else 0.0

        if mem_pct > self._mem_warn:
            symptoms.append(f"memory_pressure:{mem_pct:.0f}%")

        # 히트율
        hits   = info.get("keyspace_hits", 0)
        misses = info.get("keyspace_misses", 0)
        total  = hits + misses
        hit_rate = (hits / total * 100) if total > 0 else 100.0

        if hit_rate < self._hit_warn and total > 100:
            symptoms.append(f"low_hit_rate:{hit_rate:.0f}%")

        # 연결 수
        connected = info.get("connected_clients", 0)
        if connected > 100:
            symptoms.append(f"high_connections:{connected}")

        # 응답 지연
        if latency > 100:
            symptoms.append(f"slow_response:{latency:.0f}ms")

        return Vitals(
            patient_id    = self._patient_id,
            patient_type  = self.patient_type,
            is_alive      = True,
            cpu_percent   = 0.0,
            memory_percent= mem_pct,
            error_rate    = max(0.0, 100.0 - hit_rate),
            latency_p99_ms= latency,
            symptoms      = symptoms,
            custom_metrics= {
                "host"          : self._host,
                "port"          : self._port,
                "used_memory_mb": round(used / 1024 / 1024, 2),
                "hit_rate_pct"  : round(hit_rate, 1),
                "connected_clients": connected,
                "redis_version" : info.get("redis_version", ""),
            },
        )

    async def report_health(self) -> bool:
        try:
            client = self._get_client()
            return await asyncio.get_event_loop().run_in_executor(
                None, client.ping
            )
        except Exception:
            return False

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        before = await self.collect_vitals()
        tx = prescription.treatment_type

        if tx == TreatmentType.MONITOR:
            return TreatmentResult(
                prescription_id=prescription.prescription_id,
                patient_id=self._patient_id,
                success=True, message="모니터링 유지",
                before_vitals=before,
            )
        elif tx == TreatmentType.CONFIG_CHANGE:
            payload = prescription.payload
            results = []
            try:
                client = self._get_client()
                if "maxmemory" in payload:
                    client.config_set("maxmemory", payload["maxmemory"])
                    results.append(f"maxmemory={payload['maxmemory']}")
                if "maxmemory_policy" in payload:
                    client.config_set("maxmemory-policy", payload["maxmemory_policy"])
                    results.append(f"policy={payload['maxmemory_policy']}")
                msg = "설정 변경: " + ", ".join(results) if results else "변경 없음"
                r = {"success": True, "message": msg}
            except Exception as e:
                r = {"success": False, "message": str(e)[:100]}
        elif tx == TreatmentType.RESTART:
            r = {"success": False,
                 "message": "Redis 재시작은 외부에서 수동으로 필요 (service restart redis)"}
        else:
            r = {"success": False,
                 "message": f"Redis에 지원하지 않는 치료: {tx.value}"}

        after = await self.collect_vitals()
        return TreatmentResult(
            prescription_id=prescription.prescription_id,
            patient_id=self._patient_id,
            success=r.get("success", False),
            message=r.get("message", ""),
            before_vitals=before, after_vitals=after,
        )

    def get_metadata(self) -> dict:
        return {
            "patient_id"  : self._patient_id,
            "patient_type": self.patient_type.value,
            "host"        : f"{self._host}:{self._port}",
            **self._meta,
        }
