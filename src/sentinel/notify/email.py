"""Email — the scheduled digest channel.

Transport selection is three-way and falls through: Resend if an API key is
present, then SMTP if a host is, then nothing. An environment with neither
simply sends no mail rather than failing at start-up, which is what lets the
whole pipeline run on a laptop with an empty ``.env``.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import httpx

from ..config import NotifyConfig, api_key
from ..logging_setup import get_logger
from .base import NotificationResult

log = get_logger("notify.email")


class ResendEmailNotifier:
    channel = "email:resend"

    def __init__(self, config: NotifyConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client
        self._key = api_key("RESEND_API_KEY")

    def available(self) -> bool:
        return bool(self._key and self.config.email_to and self.config.email_from)

    def send(self, *, subject: str, body: str, html: str | None = None,
             priority: str = "default", tags: str = "") -> NotificationResult:
        if not self.available():
            return NotificationResult(self.channel, False, False, "RESEND_API_KEY or address unset")
        client = self._client or httpx.Client(timeout=20)
        try:
            response = client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "from": self.config.email_from, "to": [self.config.email_to],
                    "subject": subject, "text": body,
                    **({"html": html} if html else {}),
                },
            )
            response.raise_for_status()
            return NotificationResult(self.channel, True, True, "sent")
        except httpx.HTTPError as exc:
            log.warning("resend delivery failed: %s", exc)
            return NotificationResult(self.channel, False, True, str(exc))
        finally:
            if self._client is None:
                client.close()


class SmtpEmailNotifier:
    channel = "email:smtp"

    def __init__(self, config: NotifyConfig, *, transport: object = None) -> None:
        self.config = config
        self._transport = transport
        self.host = os.environ.get("SENTINEL_SMTP_HOST")
        self.port = int(os.environ.get("SENTINEL_SMTP_PORT", "587"))
        self.user = os.environ.get("SENTINEL_SMTP_USER")
        self.password = os.environ.get("SENTINEL_SMTP_PASSWORD")

    def available(self) -> bool:
        return bool(self.host and self.config.email_to and self.config.email_from)

    def send(self, *, subject: str, body: str, html: str | None = None,
             priority: str = "default", tags: str = "") -> NotificationResult:
        if not self.available():
            return NotificationResult(self.channel, False, False, "SMTP host or address unset")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.email_from
        message["To"] = self.config.email_to
        message.set_content(body)
        if html:
            message.add_alternative(html, subtype="html")
        try:
            if self._transport is not None:
                self._transport.send_message(message)  # type: ignore[attr-defined]
            else:
                with smtplib.SMTP(self.host, self.port, timeout=20) as server:
                    server.starttls()
                    if self.user and self.password:
                        server.login(self.user, self.password)
                    server.send_message(message)
            return NotificationResult(self.channel, True, True, "sent")
        except (OSError, smtplib.SMTPException) as exc:
            log.warning("smtp delivery failed: %s", exc)
            return NotificationResult(self.channel, False, True, str(exc))


def build_email_notifier(config: NotifyConfig, **kwargs: object):
    """Resend, then SMTP, then nothing."""
    resend = ResendEmailNotifier(config, client=kwargs.get("http_client"))  # type: ignore[arg-type]
    if resend.available():
        return resend
    smtp = SmtpEmailNotifier(config, transport=kwargs.get("smtp_transport"))
    if smtp.available():
        return smtp
    from .base import NullNotifier

    return NullNotifier()
