"""Which channel gets which message — and the rule that keeps push meaningful.

**The daily brief never pushes.** It goes by email, on a schedule, and that is
the whole design: a push notification is a claim that something needs attention
*now*, and a system that pushes a routine digest every morning teaches you to
dismiss its pushes without reading them. By the time it has something urgent to
say, you have already trained yourself not to look.

So the push allow-list is closed and small — five events, all of them meaning
"act or review now" — and ``push_event`` refuses anything not on it rather than
letting a caller pass a plausible-looking string. That refusal is tested.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from ..domain.enums import NotifyEvent
from ..domain.models import Brief, Position
from ..storage import repo
from .base import NotificationResult, Notifier, NullNotifier

ROUTER_VERSION = "router-v1"

#: Priority and tags per event, so the phone's own notification UI can
#: distinguish "a stop fired" from "the data pipeline broke".
_EVENT_STYLE = {
    NotifyEvent.STOP_TRIGGERED: ("high", "rotating_light"),
    NotifyEvent.INVALIDATION_HIT: ("high", "warning"),
    NotifyEvent.KILL_SWITCH: ("urgent", "octagonal_sign"),
    NotifyEvent.PIPELINE_FAILURE: ("high", "wrench"),
    NotifyEvent.EARNINGS_IMMINENT: ("default", "calendar"),
}


class PushNotAllowed(ValueError):
    """Raised when something tries to push a message that is not an event."""


@dataclass(slots=True)
class Router:
    digest: Notifier
    push: Notifier
    conn: object | None = None

    def _record(self, result: NotificationResult, event: str, subject: str) -> None:
        if self.conn is not None:
            repo.record_notification(
                self.conn, channel=result.channel, event=event, subject=subject,
                delivered=result.delivered, error=None if result.delivered else result.detail,
            )

    # ---------------------------------------------------------------- digest
    def send_digest(self, *, subject: str, body: str, html: str | None = None) -> NotificationResult:
        result = self.digest.send(subject=subject, body=body, html=html)
        self._record(result, "digest", subject)
        if result.failed:
            # The fallback channel: a delivery failure is itself an event, and
            # it is the one case where the brief's channel and the push channel
            # legitimately meet.
            fallback = self.push.send(
                subject="Sentinel digest failed to send",
                body=f"The daily digest could not be delivered: {result.detail}",
                priority="high", tags="wrench",
            )
            self._record(fallback, NotifyEvent.PIPELINE_FAILURE.value, "digest delivery failure")
        return result

    # ---------------------------------------------------------------- push
    def push_event(self, event: NotifyEvent, *, subject: str, body: str) -> NotificationResult:
        if event not in _EVENT_STYLE:
            raise PushNotAllowed(
                f"{event!r} is not on the push allow-list. Push is reserved for events that "
                f"mean 'act or review now'; anything else belongs in the digest."
            )
        priority, tags = _EVENT_STYLE[event]
        result = self.push.send(subject=subject, body=body, priority=priority, tags=tags)
        self._record(result, event.value, subject)
        return result


def events_from_brief(
    brief: Brief, *, positions: Sequence[Position] = (), as_of: dt.date | None = None,
    earnings_within_hours: int = 48, earnings_dates: dict[str, dt.date] | None = None,
) -> list[tuple[NotifyEvent, str, str]]:
    """The events a brief implies. The brief itself is not one of them.

    Returns (event, subject, body). Deliberately derived from the brief's own
    contents rather than passed in, so a caller cannot smuggle a routine message
    onto the push channel by labelling it an event.
    """
    as_of = as_of or brief.as_of
    out: list[tuple[NotifyEvent, str, str]] = []

    for line in brief.triggered:
        if line.startswith("STOP HIT"):
            out.append((NotifyEvent.STOP_TRIGGERED, "Stop triggered", line))

    if brief.kill_switch_active:
        out.append((
            NotifyEvent.KILL_SWITCH,
            "Drawdown kill switch active",
            "Satellite drawdown has passed the limit. No new short-term ideas will be "
            "issued. Review open positions against their invalidation conditions.",
        ))

    if brief.stale:
        blocked = [i.ticker for i in brief.data_issues if i.ticker and i.severity.value == "critical"]
        out.append((
            NotifyEvent.PIPELINE_FAILURE,
            "Data quality failure",
            f"{len(blocked)} ticker(s) could not be scored: {', '.join(blocked[:8])}. "
            f"Today's brief is incomplete.",
        ))

    for position in positions:
        if not position.is_open:
            continue
        earnings = (earnings_dates or {}).get(position.ticker)
        if earnings is None:
            continue
        hours = (dt.datetime.combine(earnings, dt.time.min)
                 - dt.datetime.combine(as_of, dt.time.min)).total_seconds() / 3600
        if 0 <= hours <= earnings_within_hours:
            out.append((
                NotifyEvent.EARNINGS_IMMINENT,
                f"{position.ticker} reports in {int(hours)}h",
                f"You hold {position.shares} shares of {position.ticker}. Results are due "
                f"{earnings.isoformat()}. Decide before the print, not after it.",
            ))
    return out


def build_router(config, *, conn: object | None = None, **kwargs: object) -> Router:
    from .email import build_email_notifier
    from .push import NtfyNotifier

    push = NtfyNotifier(config.notify, client=kwargs.get("http_client"))  # type: ignore[arg-type]
    return Router(
        digest=build_email_notifier(config.notify, **kwargs),
        push=push if push.available() else NullNotifier(),
        conn=conn,
    )
