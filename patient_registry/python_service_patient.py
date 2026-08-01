"""
python_service_patient.py
─────────────────────────────────────────────────────────────────────
Python 서비스 환자 어댑터.

FastAPI, Flask, Django, 일반 Python 프로세스를 MEDIC 환자로 등록한다.

증상 수집:
  - psutil 로 CPU/메모리/디스크 수집
  - /health 엔드포인트 폴링
  - 로그 파일 tail
  - 예외 훅 자동 설치 (sys.excepthook)

치료 지원:
  - PATCH_CODE : unified diff 를 받아 파일에 적용
  - RESTART    : 프로세스 재시작 (supervisor / systemd 연동)
  - ROLLBACK   : git 기반 이전 커밋으로 롤백
  - QUARANTINE : iptables 또는 nginx upstream 제거로 트래픽 차단
─────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
    _HTTPX_OK = True
except ImportError:
    from infrastructure import httpx_mock as httpx
    _HTTPX_OK = False  # mock 사용 중

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


class PythonServicePatient(BasePatient):
    """
    Python 서비스를 MEDIC 환자로 등록하는 어댑터.

    사용 예시:
        patient = PythonServicePatient(
            patient_id   = "api-gateway-prod",
            service_url  = "http://localhost:8080",
            source_root  = "/app/src",
            log_file     = "/var/log/api-gateway/app.log",
            restart_cmd  = "supervisorctl restart api-gateway",
        )
        await medic.register(patient)
    """

    def __init__(
        self,
        patient_id   : str,
        service_url  : str,
        source_root  : str,
        log_file     : str  = "",
        restart_cmd  : str  = "",
        rollback_cmd : str  = "git checkout HEAD~1",
        health_path  : str  = "/health",
        pid          : Optional[int] = None,
        snapshot_root: str  = "",
        metadata     : dict[str, Any] = None,
    ) -> None:
        self._patient_id  = patient_id
        self._service_url = service_url.rstrip("/")
        self._source_root = Path(source_root)
        self._log_file    = log_file
        self._restart_cmd = restart_cmd
        self._rollback_cmd= rollback_cmd
        self._health_path = health_path
        self._pid         = pid
        self._snapshot_root = (
            Path(snapshot_root)
            if snapshot_root else
            Path(tempfile.gettempdir()) / "medic_snapshots" / patient_id
        )
        self._meta        = metadata or {}

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.PYTHON_SERVICE

    # ── 증상 수집 ──────────────────────────────────────────────────

    async def collect_vitals(self) -> Vitals:
        """CPU, 메모리, 응답 지연, 에러율을 수집한다."""
        symptoms = []

        # 프로세스 지표 (pid 가 있는 경우)
        cpu_pct = 0.0
        mem_pct = 0.0
        if self._pid and _PSUTIL_OK:
            try:
                proc = psutil.Process(self._pid)
                cpu_pct = proc.cpu_percent(interval=0.5)
                mem_pct = proc.memory_percent()
            except Exception:
                symptoms.append(f"process_not_found:pid={self._pid}")

        # HTTP 응답 지연 측정
        latency_ms = 0.0
        is_alive   = False
        if _HTTPX_OK:
            try:
                import time as _time
                async with httpx.AsyncClient(timeout=5.0) as client:
                    t0 = _time.monotonic()
                    resp = await client.get(f"{self._service_url}{self._health_path}")
                    latency_ms = (_time.monotonic() - t0) * 1000
                    is_alive   = resp.status_code < 500
                    if resp.status_code >= 400:
                        symptoms.append(f"health_check_degraded:status={resp.status_code}")
            except Exception as e:
                symptoms.append(f"health_check_failed:{type(e).__name__}")
                is_alive = False
        else:
            symptoms.append("httpx_not_installed")

        return Vitals(
            patient_id     = self._patient_id,
            patient_type   = self.patient_type,
            is_alive       = is_alive,
            cpu_percent    = cpu_pct,
            memory_percent = mem_pct,
            latency_p99_ms = latency_ms,
            symptoms       = symptoms,
            custom_metrics = {
                "service_url" : self._service_url,
                "source_root" : str(self._source_root),
            },
        )

    async def report_health(self) -> bool:
        """서비스가 정상 응답하는지 확인한다."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._service_url}{self._health_path}")
                return resp.status_code < 500
        except Exception:
            return False

    # ── 치료 적용 ──────────────────────────────────────────────────

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        """MEDIC 처방을 실행한다."""
        before = await self.collect_vitals()

        try:
            if prescription.treatment_type == TreatmentType.PATCH_CODE:
                success, msg = await self._apply_patch(prescription.payload)

            elif prescription.treatment_type == TreatmentType.RESTART:
                success, msg = await self._restart()

            elif prescription.treatment_type == TreatmentType.ROLLBACK:
                success, msg = await self._rollback(prescription.payload)

            elif prescription.treatment_type == TreatmentType.QUARANTINE:
                success, msg = await self._quarantine(prescription.payload)

            elif prescription.treatment_type == TreatmentType.CONFIG_CHANGE:
                success, msg = await self._apply_config(prescription.payload)

            else:
                return TreatmentResult(
                    prescription_id = prescription.prescription_id,
                    patient_id      = self._patient_id,
                    success         = False,
                    message         = f"지원하지 않는 치료 유형: {prescription.treatment_type}",
                    before_vitals   = before,
                )

        except Exception as exc:
            logger.error(f"[{self._patient_id}] 치료 실행 중 오류: {exc}")
            success, msg = False, str(exc)

        after = await self.collect_vitals()

        return TreatmentResult(
            prescription_id = prescription.prescription_id,
            patient_id      = self._patient_id,
            success         = success,
            message         = msg,
            before_vitals   = before,
            after_vitals    = after,
        )

    # ── 소스 코드 제공 ─────────────────────────────────────────────

    async def get_source_code(self, file_path: str) -> Optional[str]:
        """
        패치 생성을 위해 소스 파일을 반환한다.
        source_root 밖의 파일은 거부한다 (경로 탈출 방지).
        """
        if not file_path:
            return self._build_source_inventory_summary()
        try:
            target = (self._source_root / file_path).resolve()
            if not str(target).startswith(str(self._source_root.resolve())):
                logger.warning(
                    f"[{self._patient_id}] 경로 탈출 시도 차단: {file_path}"
                )
                return None
            return target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning(f"[{self._patient_id}] 소스 코드 읽기 실패: {exc}")
            return None

    def _build_source_inventory_summary(self) -> Optional[str]:
        try:
            source_root = self._source_root.resolve()
            if not source_root.exists():
                return None
            candidates: list[Path] = []
            preferred = [
                "main.py", "app.py", "server.py", "run.py",
                "settings.py", "config.py", "requirements.txt",
                "pyproject.toml",
            ]
            for name in preferred:
                target = source_root / name
                if target.exists() and target.is_file():
                    candidates.append(target)
            for pattern in ("*.py", "*.yml", "*.yaml", "*.json", "*.toml"):
                for path in sorted(source_root.rglob(pattern)):
                    if path.is_file() and path not in candidates:
                        candidates.append(path)
                    if len(candidates) >= 8:
                        break
                if len(candidates) >= 8:
                    break
            if not candidates:
                return None

            lines = [
                "# source inventory",
                f"# patient_id={self._patient_id}",
                f"# source_root={source_root}",
            ]
            for path in candidates[:8]:
                rel = path.resolve().relative_to(source_root).as_posix()
                lines.append(f"\n## file: {rel}")
                try:
                    snippet = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:25]
                    lines.extend(snippet)
                except Exception as exc:
                    lines.append(f"# read_failed: {type(exc).__name__}")
            return "\n".join(lines)[:12000]
        except Exception as exc:
            logger.debug(f"[{self._patient_id}] source inventory build failed: {exc}")
            return None

    async def get_recent_logs(self, lines: int = 500) -> str:
        """로그 파일에서 최근 N 줄을 반환한다."""
        if not self._log_file:
            return ""
        try:
            cmd = self._build_log_tail_command(lines)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    shell=isinstance(cmd, str),
                )
            )
            return result.stdout
        except Exception as exc:
            logger.warning(f"[{self._patient_id}] 로그 수집 실패: {exc}")
            return ""

    def get_metadata(self) -> dict[str, Any]:
        return {
            "patient_id"  : self._patient_id,
            "patient_type": self.patient_type.value,
            "service_url" : self._service_url,
            "source_root" : str(self._source_root),
            "snapshot_root": str(self._snapshot_root),
            **self._meta,
        }

    # ── 내부 치료 구현 ─────────────────────────────────────────────

    async def _apply_patch(self, payload: dict) -> tuple[bool, str]:
        """unified diff 패치를 소스 디렉토리에 적용한다."""
        diff_patch = payload.get("diff_patch") or payload.get("patch", "")
        dry_run = bool(payload.get("dry_run"))
        report_only = bool(payload.get("report_only"))
        staged = payload.get("staged", True) is not False
        if not diff_patch:
            return False, "패치 내용이 비어있습니다"

        try:
            parsed_patch = self._parse_unified_diff(diff_patch)
        except ValueError as exc:
            return False, f"패치 파싱 실패: {exc}"

        file_count = len(parsed_patch)
        hunk_count = sum(len(file_patch["hunks"]) for file_patch in parsed_patch)
        target_files = ", ".join(file_patch["path"] for file_patch in parsed_patch[:3])
        if file_count > 3:
            target_files += ", ..."

        if report_only:
            mode = "dry-run 포함" if dry_run else "미적용"
            return True, (
                f"report-only: {mode}, files={file_count}, "
                f"hunks={hunk_count}, targets={target_files}"
            )

        snapshot_id = ""
        if payload.get("create_snapshot", True):
            snapshot_id = self._create_snapshot([
                self._resolve_patch_target(file_patch["path"])
                for file_patch in parsed_patch
            ])

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as f:
            f.write(diff_patch)
            patch_file = f.name

        try:
            if self._has_command("patch"):
                success, message = await self._apply_with_patch(patch_file, dry_run)
            elif self._has_command("git"):
                success, message = await self._apply_with_git(patch_file, dry_run)
            else:
                success, message = await self._apply_with_internal(
                    parsed_patch,
                    dry_run=dry_run,
                    staged=staged,
                )
            if snapshot_id and not dry_run and success:
                message = f"{message} | snapshot={snapshot_id}"
            return success, message
        except Exception:
            if snapshot_id and not dry_run:
                self._restore_snapshot(snapshot_id)
            raise
        finally:
            os.unlink(patch_file)

    async def _apply_with_patch(self, patch_file: str, dry_run: bool) -> tuple[bool, str]:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["patch", "-p1", "--dry-run", "-i", patch_file],
                cwd=str(self._source_root),
                capture_output=True, text=True, timeout=30
            )
        )
        if result.returncode != 0:
            return False, f"패치 dry-run 실패: {result.stderr[:300]}"
        if dry_run:
            return True, "patch dry-run 검사 통과"

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["patch", "-p1", "-i", patch_file],
                cwd=str(self._source_root),
                capture_output=True, text=True, timeout=30
            )
        )
        if result.returncode == 0:
            return True, "패치 적용 완료"
        return False, f"패치 적용 실패: {result.stderr[:300]}"

    async def _apply_with_git(self, patch_file: str, dry_run: bool) -> tuple[bool, str]:
        check = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["git", "apply", "--check", patch_file],
                cwd=str(self._source_root),
                capture_output=True, text=True, timeout=30
            )
        )
        if check.returncode != 0:
            stderr = (check.stderr or check.stdout or "")[:300]
            return False, f"git apply 검사 실패: {stderr}"
        if dry_run:
            return True, "git apply dry-run 검사 통과"

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["git", "apply", patch_file],
                cwd=str(self._source_root),
                capture_output=True, text=True, timeout=30
            )
        )
        if result.returncode == 0:
            return True, "git apply로 패치 적용 완료"
        stderr = (result.stderr or result.stdout or "")[:300]
        return False, f"git apply 실패: {stderr}"

    async def _apply_with_internal(
        self,
        parsed_patch: list[dict[str, Any]],
        dry_run: bool,
        staged: bool = True,
    ) -> tuple[bool, str]:
        try:
            staged_results: list[tuple[Path, list[str]]] = []
            for file_patch in parsed_patch:
                target = self._resolve_patch_target(file_patch["path"])
                original_lines = (
                    target.read_text(encoding="utf-8").splitlines(keepends=True)
                    if target.exists()
                    else []
                )
                updated_lines = self._apply_hunks_to_lines(
                    original_lines,
                    file_patch["hunks"],
                    target_path=file_patch["path"],
                )
                staged_results.append((target, updated_lines))
            if dry_run:
                return True, "internal dry-run 검사 통과"
            if staged:
                for target, updated_lines in staged_results:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp_target = target.with_suffix(target.suffix + ".medic-stage")
                    tmp_target.write_text("".join(updated_lines), encoding="utf-8")
                    os.replace(tmp_target, target)
                return True, "내부 Python fallback으로 staged 패치 적용 완료"
            for target, updated_lines in staged_results:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("".join(updated_lines), encoding="utf-8")
            return True, "내부 Python fallback으로 패치 적용 완료"
        except Exception as exc:
            return False, f"내부 패치 적용 실패: {exc}"

    def _resolve_patch_target(self, relative_path: str) -> Path:
        cleaned = relative_path.replace("\\", "/")
        cleaned = re.sub(r"^[ab]/", "", cleaned)
        target = (self._source_root / cleaned).resolve()
        if not str(target).startswith(str(self._source_root.resolve())):
            raise ValueError(f"경로 탈출이 감지되었습니다: {relative_path}")
        return target

    def _parse_unified_diff(self, diff_patch: str) -> list[dict[str, Any]]:
        lines = diff_patch.splitlines(keepends=True)
        files: list[dict[str, Any]] = []
        current_file: Optional[dict[str, Any]] = None
        current_hunk: Optional[dict[str, Any]] = None
        hunk_re = re.compile(
            r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
            r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
        )

        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("--- "):
                if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
                    raise ValueError("+++ 헤더가 없는 unified diff입니다")
                new_path = lines[i + 1][4:].strip()
                current_file = {"path": new_path, "hunks": []}
                files.append(current_file)
                current_hunk = None
                i += 2
                continue
            if line.startswith("@@ "):
                if current_file is None:
                    raise ValueError("파일 헤더 없이 hunk가 시작되었습니다")
                match = hunk_re.match(line)
                if not match:
                    raise ValueError(f"hunk 헤더 형식이 잘못되었습니다: {line.strip()}")
                current_hunk = {
                    "old_start": int(match.group("old_start")),
                    "old_count": int(match.group("old_count") or "1"),
                    "new_start": int(match.group("new_start")),
                    "new_count": int(match.group("new_count") or "1"),
                    "lines": [],
                }
                current_file["hunks"].append(current_hunk)
                i += 1
                continue
            if current_hunk is not None:
                if line.startswith((" ", "+", "-")):
                    current_hunk["lines"].append(line)
                    i += 1
                    continue
                if line.startswith("\\ No newline at end of file"):
                    i += 1
                    continue
            i += 1

        if not files:
            raise ValueError("파일 변경이 없는 unified diff입니다")
        for file_patch in files:
            if not file_patch["hunks"]:
                raise ValueError(f"hunk가 없는 파일 패치입니다: {file_patch['path']}")
        return files

    def _apply_hunks_to_lines(
        self,
        original_lines: list[str],
        hunks: list[dict[str, Any]],
        target_path: str,
    ) -> list[str]:
        updated_lines = list(original_lines)
        offset = 0
        for hunk in hunks:
            start_index = max(hunk["old_start"] - 1 + offset, 0)
            cursor = start_index
            for hunk_line in hunk["lines"]:
                marker = hunk_line[:1]
                content = hunk_line[1:]
                if marker == " ":
                    if cursor >= len(updated_lines) or updated_lines[cursor] != content:
                        raise ValueError(
                            f"context mismatch at {target_path}:{cursor + 1}"
                        )
                    cursor += 1
                elif marker == "-":
                    if cursor >= len(updated_lines) or updated_lines[cursor] != content:
                        raise ValueError(
                            f"remove mismatch at {target_path}:{cursor + 1}"
                        )
                    updated_lines.pop(cursor)
                    offset -= 1
                elif marker == "+":
                    updated_lines.insert(cursor, content)
                    cursor += 1
                    offset += 1
                else:
                    raise ValueError(f"지원하지 않는 hunk 라인입니다: {hunk_line!r}")
        return updated_lines

    async def _restart(self) -> tuple[bool, str]:
        """서비스를 재시작한다."""
        if not self._restart_cmd:
            return False, "재시작 명령이 설정되지 않았습니다"

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                self._restart_cmd, shell=True,
                capture_output=True, text=True, timeout=60
            )
        )
        if result.returncode == 0:
            # 재시작 후 health check 대기
            for _ in range(10):
                await asyncio.sleep(2)
                if await self.report_health():
                    return True, "재시작 후 health check 통과"
            return False, "재시작 후 health check 실패"
        return False, f"재시작 명령 실패: {result.stderr[:200]}"

    async def _rollback(self, payload: dict) -> tuple[bool, str]:
        """이전 버전으로 롤백한다."""
        snapshot_id = payload.get("snapshot_id", "")
        if snapshot_id:
            restored = self._restore_snapshot(snapshot_id)
            if restored:
                return True, f"snapshot rollback 완료: {snapshot_id}"
            return False, f"snapshot rollback 실패: {snapshot_id}"
        cmd = payload.get("rollback_cmd", self._rollback_cmd)
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                cmd, shell=True, cwd=str(self._source_root),
                capture_output=True, text=True, timeout=120
            )
        )
        success = result.returncode == 0
        return success, result.stdout[:300] if success else result.stderr[:300]

    async def _quarantine(self, payload: dict) -> tuple[bool, str]:
        """
        트래픽을 차단한다.
        실제 환경에서는 nginx reload 또는 iptables 룰 추가.
        여기서는 플래그 파일 생성으로 시뮬레이션.
        """
        import tempfile as _tmpmod
        flag_file = Path(_tmpmod.gettempdir()) / f"quarantine_{self._patient_id}.flag"
        flag_file.write_text("quarantined", encoding="utf-8")
        return True, f"격리 플래그 설정: {flag_file}"

    async def _apply_config(self, payload: dict) -> tuple[bool, str]:
        """설정 파일을 변경한다."""
        config_path = payload.get("config_path", "")
        config_data = payload.get("config_data", {})
        if not config_path or not config_data:
            return False, "config_path 또는 config_data 가 없습니다"

        try:
            import yaml
            serialized = yaml.dump(config_data)
        except ImportError:
            import json
            serialized = json.dumps(config_data, indent=2, ensure_ascii=False)
        target = (self._source_root / config_path).resolve()
        snapshot_id = ""
        if payload.get("create_snapshot", True) and target.exists():
            snapshot_id = self._create_snapshot([target])
        target.write_text(serialized, encoding="utf-8")
        if snapshot_id:
            return True, f"설정 파일 업데이트: {config_path} | snapshot={snapshot_id}"
        return True, f"설정 파일 업데이트: {config_path}"

    def _build_log_tail_command(self, lines: int):
        if os.name == "nt":
            escaped = self._log_file.replace("'", "''")
            return (
                "powershell -NoProfile -Command "
                f"\"Get-Content -Path '{escaped}' -Tail {int(lines)}\""
            )
        return ["tail", "-n", str(lines), self._log_file]

    @staticmethod
    def _has_command(name: str) -> bool:
        from shutil import which
        return which(name) is not None

    def _create_snapshot(self, targets: list[Path]) -> str:
        snapshot_id = str(uuid.uuid4())
        snapshot_dir = self._snapshot_root / snapshot_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        source_root = self._source_root.resolve()
        for target in targets:
            resolved = target.resolve()
            rel = resolved.relative_to(source_root)
            backup = snapshot_dir / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            exists = resolved.exists()
            if exists:
                backup.write_text(resolved.read_text(encoding="utf-8"), encoding="utf-8")
            files.append({"path": rel.as_posix(), "exists": exists})
        manifest = {
            "snapshot_id": snapshot_id,
            "patient_id": self._patient_id,
            "files": files,
        }
        (snapshot_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return snapshot_id

    def _restore_snapshot(self, snapshot_id: str) -> bool:
        snapshot_dir = self._snapshot_root / snapshot_id
        manifest_path = snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest.get("files", []):
            rel = item["path"]
            target = (self._source_root / rel).resolve()
            backup = snapshot_dir / rel
            if item.get("exists"):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
            elif target.exists():
                target.unlink()
        return True
