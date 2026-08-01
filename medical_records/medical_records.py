"""
medical_records.py
─────────────────────────────────────────────────────────────────────
의무기록 시스템.

모든 환자의 진단 → 처방 → 치료 결과를 영구 저장한다.
재발 패턴 분석과 치료 효과 추적에 사용된다.

기록하는 것:
  - 언제, 어떤 증상이 발생했는가
  - 어떤 처방을 내렸는가
  - 2차 소견은 무엇이었는가
  - 치료가 효과적이었는가
  - 같은 문제가 재발하는가

이 기록이 쌓일수록 MEDIC 은 더 정확해진다:
  "이 환자는 매주 월요일 새벽에 메모리 누수가 반복된다" 같은
  예방적 인사이트를 생성할 수 있게 된다.
─────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class CaseRecord:
    """하나의 진단→치료 케이스 전체 기록."""
    case_id          : str
    patient_id       : str
    patient_type     : str
    severity         : str
    root_cause       : str
    symptoms         : str
    treatment_type   : str
    treatment_payload: str          # JSON
    second_opinion   : str          # APPROVE/REJECT/ESCALATE/SKIPPED
    treatment_success: Optional[bool] = None
    treatment_message: str = ""
    recurrence_count : int = 0      # 동일 root_cause 재발 횟수
    recorded_at      : str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved_at      : Optional[str] = None


class MedicalRecords:
    """
    SQLite 기반 의무기록 저장소.
    
    프로덕션에서는 PostgreSQL 로 교체 가능.
    인터페이스는 동일하게 유지.
    """

    def __init__(self, db_path: str = "medic_records.db") -> None:
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """DB 스키마 초기화."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cases (
                    case_id           TEXT PRIMARY KEY,
                    patient_id        TEXT NOT NULL,
                    patient_type      TEXT,
                    severity          TEXT,
                    root_cause        TEXT,
                    symptoms          TEXT,
                    treatment_type    TEXT,
                    treatment_payload TEXT,
                    second_opinion    TEXT,
                    treatment_success INTEGER,
                    treatment_message TEXT,
                    recurrence_count  INTEGER DEFAULT 0,
                    recorded_at       TEXT,
                    resolved_at       TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_patient "
                "ON cases(patient_id, recorded_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_root_cause "
                "ON cases(patient_id, root_cause)"
            )
            conn.commit()

    def save_case(self, record: CaseRecord) -> None:
        """케이스를 저장하고 재발 횟수를 업데이트한다."""
        # 동일 patient + root_cause 의 과거 케이스 수 조회
        recurrence = self._count_recurrences(record.patient_id, record.root_cause)
        record.recurrence_count = recurrence

        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO cases VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                record.case_id,
                record.patient_id,
                record.patient_type,
                record.severity,
                record.root_cause,
                record.symptoms,
                record.treatment_type,
                record.treatment_payload,
                record.second_opinion,
                1 if record.treatment_success else (0 if record.treatment_success is False else None),
                record.treatment_message,
                record.recurrence_count,
                record.recorded_at,
                record.resolved_at,
            ))
            conn.commit()

        if recurrence >= 3:
            logger.warning(
                f"[MedicalRecords] ⚠️  반복 재발 감지 | "
                f"patient={record.patient_id} "
                f"cause='{record.root_cause}' "
                f"count={recurrence}"
            )

    def get_patient_history(
        self, patient_id: str, limit: int = 50
    ) -> list[CaseRecord]:
        """환자의 최근 케이스 이력을 반환한다."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM cases
                WHERE patient_id = ?
                ORDER BY recorded_at DESC
                LIMIT ?
            """, (patient_id, limit)).fetchall()

        return [self._row_to_record(r) for r in rows]

    def get_recurrence_patterns(self, patient_id: str) -> list[dict]:
        """재발 패턴을 분석해 반환한다."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT 
                    root_cause,
                    COUNT(*) as total,
                    SUM(treatment_success) as successes,
                    MAX(recorded_at) as last_seen,
                    AVG(recurrence_count) as avg_recurrence
                FROM cases
                WHERE patient_id = ?
                GROUP BY root_cause
                HAVING COUNT(*) > 1
                ORDER BY total DESC
            """, (patient_id,)).fetchall()

        return [
            {
                "root_cause"    : r[0],
                "total_cases"   : r[1],
                "success_rate"  : (r[2] or 0) / r[1] if r[1] else 0,
                "last_seen"     : r[3],
                "avg_recurrence": r[4],
            }
            for r in rows
        ]

    def get_treatment_effectiveness(self) -> dict[str, float]:
        """치료 유형별 성공률을 반환한다."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT 
                    treatment_type,
                    COUNT(*) as total,
                    SUM(CASE WHEN treatment_success = 1 THEN 1 ELSE 0 END) as success
                FROM cases
                WHERE treatment_success IS NOT NULL
                GROUP BY treatment_type
            """).fetchall()

        return {
            r[0]: round(r[2] / r[1], 3) if r[1] else 0.0
            for r in rows
        }

    def _count_recurrences(self, patient_id: str, root_cause: str) -> int:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM cases WHERE patient_id=? AND root_cause=?",
                (patient_id, root_cause)
            ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row_to_record(row) -> CaseRecord:
        return CaseRecord(
            case_id          = row["case_id"],
            patient_id       = row["patient_id"],
            patient_type     = row["patient_type"] or "",
            severity         = row["severity"] or "",
            root_cause       = row["root_cause"] or "",
            symptoms         = row["symptoms"] or "",
            treatment_type   = row["treatment_type"] or "",
            treatment_payload= row["treatment_payload"] or "{}",
            second_opinion   = row["second_opinion"] or "",
            treatment_success= bool(row["treatment_success"]) if row["treatment_success"] is not None else None,
            treatment_message= row["treatment_message"] or "",
            recurrence_count = row["recurrence_count"] or 0,
            recorded_at      = row["recorded_at"] or "",
            resolved_at      = row["resolved_at"],
        )
