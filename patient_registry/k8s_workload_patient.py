"""
k8s_workload_patient.py
─────────────────────────────────────────────────────────────────────
K8s 워크로드 환자 어댑터.

Deployment, StatefulSet, DaemonSet 등 K8s 워크로드를
MEDIC 환자로 등록한다.

구현 상태:
  ✅ 인터페이스 완성 (BasePatient 구현)
  ✅ Vitals 수집 구조 완성 (metrics-server / Prometheus 연동 포인트)
  ✅ 치료 유형 분기 완성
  ✅ 에스컬레이션 구조 완성
  🔲 실제 kubectl 명령어 연동 (stub — 나중에 채움)
  🔲 K8s API 서버 직접 연동 (stub)

stub 메서드는 NotImplementedError 대신 safe_stub() 를 반환한다.
→ 실제 K8s 없어도 MEDIC 파이프라인 전체가 동작한다.
→ kubectl 연동 코드만 채우면 즉시 활성화된다.

치료 매핑:
  RESTART           → kubectl rollout restart deployment/{name}
  ROLLBACK          → kubectl rollout undo deployment/{name}
  K8S_ROLLING_UPDATE→ kubectl set image deployment/{name} + rollout status
  K8S_HPA_ADJUST    → kubectl patch hpa/{name}
  QUARANTINE        → kubectl apply -f networkpolicy.yaml (인바운드 차단)
  SCALE_DOWN        → kubectl scale deployment/{name} --replicas=0
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .base_patient import (
    BasePatient, PatientType, Prescription, TreatmentResult,
    TreatmentType, Vitals,
)

logger = logging.getLogger(__name__)


# ── K8s 전용 메타데이터 ──────────────────────────────────────────────

@dataclass
class K8sWorkloadInfo:
    """K8s 워크로드 기본 정보."""
    namespace        : str = "default"
    workload_type    : str = "Deployment"   # Deployment / StatefulSet / DaemonSet
    replicas_desired : int = 0
    replicas_ready   : int = 0
    replicas_unavail : int = 0
    image            : str = ""
    resource_version : str = ""
    labels           : dict = field(default_factory=dict)


@dataclass
class K8sPodMetrics:
    """Pod 집계 지표."""
    cpu_millicores   : float = 0.0   # 전체 Pod CPU 합산 (m)
    memory_mib       : float = 0.0   # 전체 Pod 메모리 합산 (MiB)
    cpu_limit_pct    : float = 0.0   # limit 대비 사용률 (0~100)
    memory_limit_pct : float = 0.0
    restart_count    : int   = 0     # 전체 Pod restart 합산
    oom_killed       : int   = 0     # OOMKilled Pod 수


# ── 안전한 stub 반환값 ────────────────────────────────────────────────

def _safe_stub(method_name: str) -> dict:
    """
    kubectl 연동 미구현 메서드의 안전한 반환값.
    파이프라인이 중단되지 않도록 success=False + stub 메시지를 반환한다.
    """
    return {
        "success": False,
        "message": f"[STUB] {method_name} — kubectl 연동 미구현. patient_registry/k8s_workload_patient.py 참고.",
        "stub"   : True,
    }


# ── K8s 워크로드 환자 ────────────────────────────────────────────────

class K8sWorkloadPatient(BasePatient):
    """
    K8s 워크로드를 MEDIC 환자로 등록하는 어댑터.

    사용 예시 (kubectl 연동 완성 후):
        patient = K8sWorkloadPatient(
            patient_id     = "api-gateway-deployment",
            namespace      = "production",
            workload_name  = "api-gateway",
            workload_type  = "Deployment",
            kubeconfig     = "/home/.kube/config",      # 선택
            context        = "my-cluster-prod",         # 선택
        )
        await medic.register(patient)

    현재 (stub 상태):
        patient = K8sWorkloadPatient(
            patient_id    = "api-gateway-deployment",
            namespace     = "production",
            workload_name = "api-gateway",
        )
        # Vitals 수집은 stub, 치료도 stub
        # 파이프라인 구조 검증용으로 사용 가능
    """

    def __init__(
        self,
        patient_id   : str,
        workload_name: str,
        namespace    : str = "default",
        workload_type: str = "Deployment",
        kubeconfig   : Optional[str] = None,   # None = 기본 kubeconfig
        context      : Optional[str] = None,   # None = 현재 컨텍스트
        service_url  : Optional[str] = None,   # health check용 (없어도 됨)
        metadata     : dict = None,
    ) -> None:
        self._patient_id   = patient_id
        self._name         = workload_name
        self._namespace    = namespace
        self._wtype        = workload_type
        self._kubeconfig   = kubeconfig
        self._context      = context
        self._service_url  = service_url
        self._meta         = metadata or {}

    @property
    def patient_id(self) -> str:
        return self._patient_id

    @property
    def patient_type(self) -> PatientType:
        return PatientType.K8S_WORKLOAD

    # ── Vitals 수집 ──────────────────────────────────────────────────

    async def collect_vitals(self) -> Vitals:
        """
        K8s 워크로드의 생체 신호를 수집한다.

        실제 연동 시 채울 것:
          - metrics-server: kubectl top pods -n {namespace}
          - Prometheus: PromQL로 집계
          - K8s API: /apis/apps/v1/namespaces/{ns}/deployments/{name}

        현재: stub (기본값 반환)
        """
        symptoms = []
        info     = await self._get_workload_info()
        metrics  = await self._get_pod_metrics()

        is_alive = info.replicas_ready > 0

        # 이상 징후 탐지
        if info.replicas_unavail > 0:
            symptoms.append(
                f"replicas_unavailable:{info.replicas_unavail}"
            )
        if metrics.restart_count > 5:
            symptoms.append(f"high_restart_count:{metrics.restart_count}")
        if metrics.oom_killed > 0:
            symptoms.append(f"oom_killed:{metrics.oom_killed}")
        if metrics.cpu_limit_pct > 80:
            symptoms.append(f"cpu_throttling:{metrics.cpu_limit_pct:.0f}%")
        if metrics.memory_limit_pct > 85:
            symptoms.append(f"memory_pressure:{metrics.memory_limit_pct:.0f}%")
        if info.replicas_ready < info.replicas_desired:
            ratio = info.replicas_ready / max(info.replicas_desired, 1)
            if ratio < 0.5:
                symptoms.append(f"degraded_replicas:{info.replicas_ready}/{info.replicas_desired}")

        return Vitals(
            patient_id     = self._patient_id,
            patient_type   = self.patient_type,
            is_alive       = is_alive,
            cpu_percent    = metrics.cpu_limit_pct,
            memory_percent = metrics.memory_limit_pct,
            error_rate     = (info.replicas_unavail / max(info.replicas_desired, 1)) * 100,
            latency_p99_ms = 0.0,  # Prometheus 연동 시 채움
            symptoms       = symptoms,
            custom_metrics = {
                "namespace"      : self._namespace,
                "workload_name"  : self._name,
                "workload_type"  : self._wtype,
                "replicas"       : {
                    "desired"  : info.replicas_desired,
                    "ready"    : info.replicas_ready,
                    "unavailable": info.replicas_unavail,
                },
                "restart_count"  : metrics.restart_count,
                "oom_killed"     : metrics.oom_killed,
                "image"          : info.image,
            },
        )

    async def report_health(self) -> bool:
        info = await self._get_workload_info()
        return info.replicas_ready >= max(info.replicas_desired // 2, 1)

    # ── 치료 적용 ────────────────────────────────────────────────────

    async def apply_treatment(self, prescription: Prescription) -> TreatmentResult:
        """K8s 처방을 실행한다."""
        before = await self.collect_vitals()

        tx = prescription.treatment_type

        if tx == TreatmentType.RESTART:
            r = await self._rollout_restart()

        elif tx == TreatmentType.ROLLBACK:
            r = await self._rollout_undo(prescription.payload)

        elif tx == TreatmentType.K8S_ROLLING_UPDATE:
            r = await self._rolling_update(prescription.payload)

        elif tx == TreatmentType.K8S_HPA_ADJUST:
            r = await self._adjust_hpa(prescription.payload)

        elif tx == TreatmentType.QUARANTINE:
            r = await self._apply_network_policy(prescription.payload)

        elif tx == TreatmentType.SCALE_DOWN:
            r = await self._scale(replicas=0)

        elif tx == TreatmentType.MONITOR:
            return TreatmentResult(
                prescription_id = prescription.prescription_id,
                patient_id      = self._patient_id,
                success         = True,
                message         = "모니터링 유지",
                before_vitals   = before,
            )
        else:
            r = {"success": False,
                 "message": f"K8s 환자에 지원하지 않는 치료: {tx.value}"}

        after = await self.collect_vitals()
        return TreatmentResult(
            prescription_id = prescription.prescription_id,
            patient_id      = self._patient_id,
            success         = r.get("success", False),
            message         = r.get("message", ""),
            before_vitals   = before,
            after_vitals    = after,
            side_effects    = ["stub"] if r.get("stub") else [],
        )

    def get_treatment_blacklist(self):
        # K8s에 코드 패치 직접 적용은 금지 (이미지 빌드 후 롤링 업데이트로만)
        return [TreatmentType.PATCH_CODE]

    def get_metadata(self) -> dict:
        return {
            "patient_id"  : self._patient_id,
            "patient_type": self.patient_type.value,
            "namespace"   : self._namespace,
            "workload"    : f"{self._wtype}/{self._name}",
            **self._meta,
        }

    # ── K8s 정보 수집 (stub → 실제 kubectl/API로 교체) ───────────────

    async def _get_workload_info(self) -> K8sWorkloadInfo:
        """
        워크로드 상태 조회.

        실제 연동 시:
            result = await self._kubectl(
                ["get", self._wtype.lower(), self._name,
                 "-n", self._namespace, "-o", "json"]
            )
            return self._parse_workload_json(result)
        """
        # STUB: 기본값 반환 (정상 상태로 가정)
        return K8sWorkloadInfo(
            namespace        = self._namespace,
            workload_type    = self._wtype,
            replicas_desired = 2,
            replicas_ready   = 2,
            replicas_unavail = 0,
        )

    async def _get_pod_metrics(self) -> K8sPodMetrics:
        """
        Pod CPU/메모리 지표 수집.

        실제 연동 시:
            result = await self._kubectl(
                ["top", "pods", "-n", self._namespace,
                 "-l", f"app={self._name}", "--no-headers"]
            )
            return self._parse_top_output(result)
        """
        # STUB: 기본값 반환
        return K8sPodMetrics()

    # ── 치료 구현 (stub → 실제 kubectl로 교체) ───────────────────────

    async def _rollout_restart(self) -> dict:
        """
        kubectl rollout restart deployment/{name} -n {namespace}

        실제 연동 시:
            ok = await self._kubectl_check([
                "rollout", "restart",
                f"{self._wtype.lower()}/{self._name}",
                "-n", self._namespace,
            ])
            if ok:
                await self._wait_rollout()
            return {"success": ok, "message": "롤링 재시작 완료"}
        """
        try:
            await self._kubectl([
                "rollout", "restart",
                f"{self._wtype.lower()}/{self._name}",
                "-n", self._namespace,
            ])
            ok = await self._wait_rollout(timeout_sec=120)
            return {"success": ok,
                    "message": "롤링 재시작 완료" if ok else "재시작 타임아웃"}
        except Exception as e:
            return {"success": False, "message": str(e)[:120]}

    async def _rollout_undo(self, payload: dict) -> dict:
        """
        kubectl rollout undo deployment/{name} -n {namespace}
        payload: {"revision": int}  # 특정 버전으로 롤백 (기본 직전 버전)
        """
        revision = payload.get("revision", "")
        _rev_flag = f" --to-revision={revision}" if revision else ""
        logger.info(
            f"[K8s] rollout undo | "
            f"{self._namespace}/{self._name}{_rev_flag}"
        )
        try:
            args = ["rollout", "undo",
                    f"{self._wtype.lower()}/{self._name}",
                    "-n", self._namespace]
            if revision:
                args += [f"--to-revision={revision}"]
            await self._kubectl(args)
            ok = await self._wait_rollout(timeout_sec=120)
            return {"success": ok, "message": f"롤백 완료 (revision={revision or '직전'})"}
        except Exception as e:
            return {"success": False, "message": str(e)[:120]}

    async def _rolling_update(self, payload: dict) -> dict:
        """
        kubectl set image deployment/{name} {container}={image} -n {namespace}
        payload: {"container": str, "image": str}
        """
        container = payload.get("container", self._name)
        image     = payload.get("image", "")
        if not image:
            return {"success": False, "message": "이미지 미지정"}
        logger.info(
            f"[K8s] rolling update | "
            f"{self._namespace}/{self._name} → {image}"
        )
        try:
            await self._kubectl([
                "set", "image",
                f"{self._wtype.lower()}/{self._name}",
                f"{container}={image}",
                "-n", self._namespace,
            ])
            ok = await self._wait_rollout(timeout_sec=180)
            return {"success": ok,
                    "message": f"이미지 업데이트 완료: {image}" if ok else "업데이트 타임아웃"}
        except Exception as e:
            return {"success": False, "message": str(e)[:120]}

    async def _adjust_hpa(self, payload: dict) -> dict:
        """
        kubectl patch hpa/{name} -n {namespace}
        payload: {"min_replicas": int, "max_replicas": int, "target_cpu_pct": int}
        """
        logger.info(
            f"[K8s] HPA 조정 | "
            f"{self._namespace}/{self._name} payload={payload}"
        )
        try:
            patch_parts = []
            if "min_replicas" in payload:
                patch_parts.append(f'"minReplicas":{payload["min_replicas"]}')
            if "max_replicas" in payload:
                patch_parts.append(f'"maxReplicas":{payload["max_replicas"]}')
            if not patch_parts:
                return {"success": False, "message": "min_replicas 또는 max_replicas 필요"}
            patch = '{"spec":{" + ",".join(patch_parts) + "}}'
            await self._kubectl([
                "patch", "hpa", self._name,
                "-n", self._namespace,
                "--type=merge", "-p", patch,
            ])
            return {"success": True, "message": f"HPA 조정: {payload}"}
        except Exception as e:
            return {"success": False, "message": str(e)[:120]}

    async def _apply_network_policy(self, payload: dict) -> dict:
        """
        NetworkPolicy 적용으로 인바운드 트래픽 차단.
        payload: {"block_ingress": bool, "allow_namespaces": list}
        """
        logger.info(
            f"[K8s] NetworkPolicy 적용 | "
            f"{self._namespace}/{self._name}"
        )
        try:
            block = payload.get("block_ingress", True)
            policy_name = f"medic-quarantine-{self._name}"
            if block:
                # 인바운드 트래픽 차단 NetworkPolicy
                policy = (
                    f'{{"apiVersion":"networking.k8s.io/v1",'
                    f'"kind":"NetworkPolicy",'
                    f'"metadata":{{"name":"{policy_name}","namespace":"{self._namespace}"}},'
                    f'"spec":{{"podSelector":{{"matchLabels":{{"app":"{self._name}"}}}},'
                    f'"policyTypes":["Ingress"]}}}}'
                )
                import tempfile, os
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False
                ) as f:
                    f.write(policy)
                    tmp = f.name
                await self._kubectl(["apply", "-f", tmp])
                os.unlink(tmp)
                return {"success": True, "message": f"NetworkPolicy 적용: {policy_name}"}
            else:
                await self._kubectl(["delete", "networkpolicy", policy_name,
                                     "-n", self._namespace, "--ignore-not-found"])
                return {"success": True, "message": f"NetworkPolicy 해제: {policy_name}"}
        except Exception as e:
            return {"success": False, "message": str(e)[:120]}

    async def _scale(self, replicas: int) -> dict:
        """
        kubectl scale deployment/{name} --replicas={n} -n {namespace}
        """
        logger.info(
            f"[K8s] scale | "
            f"{self._namespace}/{self._name} → replicas={replicas}"
        )
        try:
            await self._kubectl([
                "scale", f"{self._wtype.lower()}/{self._name}",
                f"--replicas={replicas}",
                "-n", self._namespace,
            ])
            return {"success": True, "message": f"replicas={replicas}으로 조정 완료"}
        except Exception as e:
            return {"success": False, "message": str(e)[:120]}

    # ── kubectl 실행 헬퍼 (실제 연동 시 구현) ────────────────────────

    async def _kubectl(self, args: list[str]) -> dict:
        """kubectl 명령어를 비동기로 실행하고 JSON 결과를 반환한다."""
        import json as _json
        cmd = ["kubectl"] + args
        if self._kubeconfig:
            cmd += ["--kubeconfig", self._kubeconfig]
        if self._context:
            cmd += ["--context", self._context]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30.0
            )
            if proc.returncode != 0:
                raise RuntimeError(stderr.decode(errors="replace").strip())
            text = stdout.decode(errors="replace").strip()
            return _json.loads(text) if text else {}
        except FileNotFoundError:
            raise RuntimeError(
                "kubectl 없음 — kubectl 설치 또는 PATH 확인 필요"
            )
        except asyncio.TimeoutError:
            raise RuntimeError("kubectl 명령 시간 초과 (30초)")

    async def _wait_rollout(self, timeout_sec: int = 300) -> bool:
        """kubectl rollout status 로 완료 대기."""
        try:
            cmd = [
                "kubectl", "rollout", "status",
                f"{self._wtype.lower()}/{self._name}",
                "-n", self._namespace,
                "--timeout", f"{timeout_sec}s",
            ]
            if self._kubeconfig:
                cmd += ["--kubeconfig", self._kubeconfig]
            if self._context:
                cmd += ["--context", self._context]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, _ = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout_sec) + 5
            )
            return proc.returncode == 0
        except Exception as e:
            logger.warning(f"[K8s] rollout status 실패: {e}")
            return False

    @staticmethod
    def _parse_workload_json(raw: dict) -> K8sWorkloadInfo:
        """K8s API JSON → K8sWorkloadInfo."""
        spec   = raw.get("spec", {})
        status = raw.get("status", {})
        return K8sWorkloadInfo(
            replicas_desired = spec.get("replicas", 0),
            replicas_ready   = status.get("readyReplicas", 0),
            replicas_unavail = status.get("unavailableReplicas", 0),
            image            = (
                spec.get("template", {})
                    .get("spec", {})
                    .get("containers", [{}])[0]
                    .get("image", "")
            ),
        )
