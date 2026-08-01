"""
alerting.py
─────────────────────────────────────────────────────────────────────
MEDIC 알림 시스템.

에스컬레이션, 반복 치료 실패, 메모리 압박 등
사람 개입이 필요한 상황에서 실제로 알림을 보낸다.

지원 채널:
  - Slack  (webhook URL)
  - Email  (SMTP)
  - 콘솔   (항상 출력, fallback)
  - 파일   (알림 로그 저장)

사용:
    alerter = MedicAlerter(
        slack_webhook = \"https://hooks.slack.com/services/...\",
        email_to      = \"admin@example.com\",
        email_from    = \"medic@example.com\",
        smtp_host     = \"smtp.gmail.com\",
        smtp_port     = 587,
        smtp_password = \"...\",
        log_path      = \"medic_alerts.log\",
    )

    await alerter.send(
        level   = \"CRITICAL\",
        patient = \"ollama-eeve\",
        cause   = \"응답 지연 급등\",
        message = \"자동 치료 5회 실패 — 수동 조치 필요\",
        context = {\"latency\": 45000, \"baseline\": 2500},
    )
─────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    """알림 하나."""
    level      : str   # CRITICAL / WARNING / INFO
    patient_id : str
    cause      : str
    message    : str
    context    : dict  = field(default_factory=dict)
    timestamp  : float = field(default_factory=time.time)

    def to_text(self) -> str:
        icon = {"CRITICAL": "[CRITICAL]", "WARNING": "[WARNING]", "INFO": "[INFO]"}.get(self.level, "[NOTICE]")
        ctx  = ""
        if self.context:
            ctx = "\n  " + "  ".join(f"{k}={v}" for k, v in self.context.items())
        return (
            f"{icon} MEDIC [{self.level}] {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.timestamp))}\n"
            f"  patient: {self.patient_id}\n"
            f"  cause: {self.cause}\n"
            f"  message: {self.message}"
            f"{ctx}"
        )

    def to_slack_payload(self) -> dict:
        color = {"CRITICAL": "#ff0000", "WARNING": "#ffa500", "INFO": "#36a64f"}.get(self.level, "#cccccc")
        icon  = {"CRITICAL": "[CRITICAL]", "WARNING": "[WARNING]", "INFO": "[INFO]"}.get(self.level, "[NOTICE]")
        fields = [
            {"title": "Patient", "value": self.patient_id, "short": True},
            {"title": "Cause", "value": self.cause,      "short": True},
        ]
        for k, v in self.context.items():
            fields.append({"title": k, "value": str(v), "short": True})
        return {
            "attachments": [{
                "color"    : color,
                "title"    : f"{icon} MEDIC {self.level}",
                "text"     : self.message,
                "fields"   : fields,
                "footer"   : "MEDIC Auto-Diagnosis System",
                "ts"       : int(self.timestamp),
            }]
        }


class MedicAlerter:
    """
    MEDIC 알림 발송기.

    채널 우선순위: Slack → Email → 콘솔 + 파일 로그
    """

    def __init__(
        self,
        slack_webhook : Optional[str] = None,
        email_to      : Optional[str] = None,
        email_from    : Optional[str] = None,
        smtp_host     : Optional[str] = None,
        smtp_port     : int           = 587,
        smtp_password : Optional[str] = None,
        log_path      : Optional[str] = None,
        min_level     : str           = "WARNING",  # 이 레벨 이상만 발송
    ) -> None:
        self._slack    = slack_webhook
        self._email_to = email_to
        self._email_from = email_from
        self._smtp_host  = smtp_host
        self._smtp_port  = smtp_port
        self._smtp_pw    = smtp_password
        self._log_path   = Path(log_path) if log_path else None
        self._levels     = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
        self._min_level  = self._levels.get(min_level, 1)

        # 중복 알림 방지 (같은 내용 5분 내 재발송 금지)
        self._sent_cache : dict[str, float] = {}
        self._dedup_sec  : int = 300

    async def send(
        self,
        level    : str,
        patient  : str,
        cause    : str,
        message  : str,
        context  : dict = None,
    ) -> bool:
        """알림을 발송한다. 성공 여부 반환."""
        if self._levels.get(level, 0) < self._min_level:
            return False

        alert = Alert(
            level      = level,
            patient_id = patient,
            cause      = cause,
            message    = message,
            context    = context or {},
        )

        # 중복 체크
        cache_key = f"{level}:{patient}:{cause}"
        last_sent = self._sent_cache.get(cache_key, 0)
        if time.time() - last_sent < self._dedup_sec:
            logger.debug(f"[Alerter] 중복 알림 억제: {cache_key}")
            return False
        self._sent_cache[cache_key] = time.time()

        # 발송
        sent_anywhere = False

        if self._slack:
            ok = await self._send_slack(alert)
            sent_anywhere = sent_anywhere or ok

        if self._email_to and self._smtp_host:
            ok = await self._send_email(alert)
            sent_anywhere = sent_anywhere or ok

        # 콘솔 + 파일은 항상
        self._log_console(alert)
        self._log_file(alert)

        return True

    async def _send_slack(self, alert: Alert) -> bool:
        try:
            # httpx 또는 urllib 사용
            payload = json.dumps(alert.to_slack_payload()).encode("utf-8")
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.post(
                        self._slack,
                        content=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    ok = r.status_code == 200
            except ImportError:
                import urllib.request
                req = urllib.request.Request(
                    self._slack, data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    ok = resp.status == 200

            if ok:
                logger.info(f"[Alerter] Slack sent: {alert.level} {alert.patient_id}")
            else:
                logger.warning("[Alerter] Slack send failed")
            return ok
        except Exception as e:
            logger.warning(f"[Alerter] Slack error: {e}")
            return False

    async def _send_email(self, alert: Alert) -> bool:
        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(alert.to_text(), "plain", "utf-8")
            msg["Subject"] = f"[MEDIC {alert.level}] {alert.patient_id} - {alert.cause}"
            msg["From"]    = self._email_from
            msg["To"]      = self._email_to

            def _smtp():
                with smtplib.SMTP(self._smtp_host, self._smtp_port) as s:
                    s.starttls()
                    if self._smtp_pw:
                        s.login(self._email_from, self._smtp_pw)
                    s.send_message(msg)

            await asyncio.get_event_loop().run_in_executor(None, _smtp)
            logger.info(f"[Alerter] Email sent -> {self._email_to}")
            return True
        except Exception as e:
            logger.warning(f"[Alerter] Email error: {e}")
            return False

    def _log_console(self, alert: Alert) -> None:
        text = alert.to_text()
        if alert.level == "CRITICAL":
            logger.critical(f"\n{text}")
        else:
            logger.warning(f"\n{text}")

    def _log_file(self, alert: Alert) -> None:
        if not self._log_path:
            return
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(alert.to_text() + "\n" + "-" * 50 + "\n")
        except Exception as e:
            logger.warning(f"[Alerter] File log failed: {e}")

    @property
    def is_configured(self) -> bool:
        return bool(self._slack or (self._email_to and self._smtp_host))
