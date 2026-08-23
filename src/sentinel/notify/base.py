"""Notification transports.

Every transport degrades to a no-op rather than raising when it is not
configured, and every send returns a result rather than throwing. A pipeline
that fails because an SMTP host is unreachable has turned a delivery problem
into a data problem.

Delivery failures are recorded to the audit trail and reported through the
fallback channel — which is why ``send`` returns a result object instead of a
bool: "not configured" and "configured and failed" need different responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NotificationResult:
    channel: str
    delivered: bool
    configured: bool
    detail: str = ""

    @property
    def failed(self) -> bool:
        """Configured but did not deliver — the only case worth alerting on."""
        return self.configured and not self.delivered


class Notifier(Protocol):
    channel: str

    def available(self) -> bool: ...

    def send(self, *, subject: str, body: str, html: str | None = None,
             priority: str = "default", tags: str = "") -> NotificationResult: ...


class NullNotifier:
    """The always-available fallback. Reports honestly that it delivered nothing."""

    channel = "null"

    def available(self) -> bool:
        return False

    def send(self, *, subject: str, body: str, html: str | None = None,
             priority: str = "default", tags: str = "") -> NotificationResult:
        return NotificationResult(self.channel, delivered=False, configured=False,
                                  detail="no transport configured")
