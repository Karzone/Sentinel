"""Mobile push via ntfy — the event channel.

Free, no account, and the topic is the only credential. That last part is why
``notify test`` prints the topic back: anyone who knows it can publish to it, so
it should be long and unguessable rather than "sentinel".
"""

from __future__ import annotations

import httpx

from ..config import NotifyConfig
from ..logging_setup import get_logger
from .base import NotificationResult

log = get_logger("notify.push")


class NtfyNotifier:
    channel = "push:ntfy"

    def __init__(self, config: NotifyConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client

    def available(self) -> bool:
        return bool(self.config.ntfy_topic)

    def send(self, *, subject: str, body: str, html: str | None = None,
             priority: str = "default", tags: str = "") -> NotificationResult:
        if not self.available():
            return NotificationResult(self.channel, False, False, "no ntfy topic configured")
        client = self._client or httpx.Client(timeout=15)
        try:
            headers = {"Title": subject, "Priority": priority}
            if tags:
                headers["Tags"] = tags
            response = client.post(
                f"{self.config.ntfy_server.rstrip('/')}/{self.config.ntfy_topic}",
                content=body.encode("utf-8"), headers=headers,
            )
            response.raise_for_status()
            return NotificationResult(self.channel, True, True, "sent")
        except httpx.HTTPError as exc:
            log.warning("ntfy delivery failed: %s", exc)
            return NotificationResult(self.channel, False, True, str(exc))
        finally:
            if self._client is None:
                client.close()
