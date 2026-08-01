"""Smoke runner for observing a real HTTP Python service target.

This module starts a tiny local /health endpoint, observes it through the
PythonServicePatient adapter, then observes an unused port to prove degraded
service symptoms still travel through the same observe-only pipeline.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from control.controlled_registry import ControlledPatientRegistry
from control.observe_loop import ObserveLoopRunner
from control.observe_targets import build_observe_patient


@dataclass
class PythonServiceSmokeCase:
    """One observed service case."""
    case_id: str
    expected_patient_status: str
    expected_root_cause: str
    result: dict[str, Any] = field(default_factory=dict)
    matched: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "expected_patient_status": self.expected_patient_status,
            "expected_root_cause": self.expected_root_cause,
            "matched": self.matched,
            "notes": self.notes,
            "result": self.result,
        }


class PythonServiceSmokeRunner:
    """Run observe-only checks against healthy and unavailable HTTP services."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.run_dir = self.root / "observe_runs"

    async def run(self) -> dict[str, Any]:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc)

        healthy_case = await self._run_healthy_case()
        unavailable_case = await self._run_unavailable_case()
        cases = [healthy_case, unavailable_case]
        matched = sum(1 for case in cases if case.matched)
        failures = [case.case_id for case in cases if not case.matched]

        summary = {
            "kind": "python_service_smoke",
            "observe_only": True,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "status": "healthy" if not failures else "warning",
            "total_cases": len(cases),
            "matched_cases": matched,
            "match_rate": self._rate(matched, len(cases)),
            "failures": failures,
            "cases": [case.to_dict() for case in cases],
        }
        summary["summary_file"] = str(self._write_summary(summary))
        return summary

    async def _run_healthy_case(self) -> PythonServiceSmokeCase:
        with _LocalHealthServer(status_code=200) as server:
            result = await self._observe_service(
                patient_id="python-service-smoke-healthy",
                service_url=server.url,
                iterations=2,
            )
        return self._case_result(
            case_id="healthy_python_service",
            result=result,
            expected_patient_status="healthy",
            expected_root_cause="no_issue_detected",
        )

    async def _run_unavailable_case(self) -> PythonServiceSmokeCase:
        service_url = f"http://127.0.0.1:{_unused_port()}"
        result = await self._observe_service(
            patient_id="python-service-smoke-unavailable",
            service_url=service_url,
            iterations=1,
        )
        return self._case_result(
            case_id="unavailable_python_service",
            result=result,
            expected_patient_status="critical",
            expected_root_cause="service_unreachable_or_process_dead",
        )

    async def _observe_service(
        self,
        patient_id: str,
        service_url: str,
        iterations: int,
    ) -> dict[str, Any]:
        runner = ObserveLoopRunner(self.root)
        patient = build_observe_patient(
            target="python-service",
            root=self.root,
            patient_id=patient_id,
            service_url=service_url,
            source_root=str(self.root),
            health_path="/health",
        )
        registry = ControlledPatientRegistry(self.root, audit_log=runner.audit_log)
        registered = registry.register(patient, replace=True)
        try:
            result = await runner.run(
                patient=registered,
                iterations=iterations,
                interval_seconds=0.0,
                actor="medic.python_service_smoke",
            )
            result["observe_target"] = "python-service"
            result["service_url"] = service_url
            result["registered_patient"] = registry.stats()
        finally:
            cleanup_done = registry.unregister(patient_id)
        result["cleanup_registry"] = registry.stats()
        result["cleanup_unregistered"] = cleanup_done
        return result

    def _case_result(
        self,
        case_id: str,
        result: dict[str, Any],
        expected_patient_status: str,
        expected_root_cause: str,
    ) -> PythonServiceSmokeCase:
        root_counts = dict(result.get("root_cause_counts", {}) or {})
        patient_status = str(result.get("patient_status", ""))
        matched = (
            str(result.get("status", "")) == "healthy"
            and int(result.get("pending_approval_final", 0) or 0) == 0
            and patient_status == expected_patient_status
            and root_counts.get(expected_root_cause, 0) > 0
        )
        notes = []
        if patient_status != expected_patient_status:
            notes.append(f"patient_status={patient_status}")
        if root_counts.get(expected_root_cause, 0) <= 0:
            notes.append(f"root_cause_counts={root_counts}")
        return PythonServiceSmokeCase(
            case_id=case_id,
            expected_patient_status=expected_patient_status,
            expected_root_cause=expected_root_cause,
            result=result,
            matched=matched,
            notes=notes,
        )

    def _write_summary(self, summary: dict[str, Any]) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.run_dir / f"python_service_smoke_{stamp}_summary.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _rate(num: int | float, den: int | float) -> float:
        if not den:
            return 0.0
        return round(float(num) / float(den), 4)


class _HealthHandler(BaseHTTPRequestHandler):
    status_code = 200

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/health":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _LocalHealthServer:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> "_LocalHealthServer":
        handler_cls = type(
            "_SmokeHealthHandler",
            (_HealthHandler,),
            {"status_code": self.status_code},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2.0)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
