"""Rendering a brief to markdown and to HTML email.

The disclaimer is emitted here, at the top of every rendered brief, by both
renderers. Putting it in the renderer rather than the generator means there is
no code path that produces a readable brief without it — rule 6 of the build
instructions is satisfied structurally rather than by remembering.

The stale-data banner sits *above* the ideas, not in a footnote. §5.4 requires a
loud warning when a brief is built on data over 24 hours old, and a warning
below the thing it is warning about is decoration.
"""

from __future__ import annotations

import datetime as dt
import html
from decimal import Decimal
from typing import Sequence

from .. import DISCLAIMER
from ..domain.enums import Severity
from ..domain.models import Brief, Idea, RiskVerdict

RENDER_VERSION = "render-v1"


def _idea_block(idea: Idea, verdict: RiskVerdict | None = None) -> list[str]:
    lines = [f"### {idea.ticker} — {idea.conviction.value} conviction, {idea.idea_class.value}"]
    lines.append(f"*Composite {idea.composite_score}/100*")
    lines.append("")
    if idea.memo:
        lines += [
            f"**Thesis.** {idea.memo.thesis}", "",
            f"**Bull case.** {idea.memo.bull_case}", "",
            f"**Bear case.** {idea.memo.bear_case}", "",
            f"**This is wrong if:** {idea.memo.invalidation}", "",
            f"**Horizon.** {idea.memo.horizon_days} days", "",
        ]
    if idea.catalyst:
        lines.append(
            f"**Catalyst.** {idea.catalyst.catalyst_type.value}, materiality "
            f"{idea.catalyst.materiality}/5 over {idea.catalyst.horizon_days} days — "
            f"{idea.catalyst.summary}"
        )
        lines.append("")
    if idea.sentiment and idea.sentiment.herding_risk:
        lines.append(
            "**Crowding.** This name is flagged as crowded, so positive sentiment on it has "
            "been scored down rather than up."
        )
        lines.append("")

    if verdict and verdict.plan:
        plan = verdict.plan
        lines += [
            f"**Position.** {plan.shares} shares at {plan.entry} "
            f"= £{plan.gbp_exposure:,.2f} ({plan.fraction_of_satellite:.1%} of satellite). "
            f"Stop {plan.stop}, so £{plan.gbp_risk:,.2f} at risk.",
            "",
        ]
    lines.append("**Evidence.**")
    for signal in idea.signals:
        lines.append(
            f"- *{signal.module.value}* ({signal.score}/100, confidence {signal.confidence}): "
            + "; ".join(f"{e.key} — {e.value}" for e in signal.evidence)
        )
    lines.append("")
    return lines


def to_markdown(
    brief: Brief, *, verdicts: Sequence[tuple[Idea, RiskVerdict]] = (), title: str = "Daily brief"
) -> str:
    by_id = {idea.id: verdict for idea, verdict in verdicts}
    out: list[str] = [
        f"# Sentinel — {title}",
        f"*{brief.as_of.isoformat()} · generated {brief.generated_at:%Y-%m-%d %H:%M UTC}*",
        "",
        f"> {DISCLAIMER}",
        "",
    ]

    if brief.stale:
        critical = [i for i in brief.data_issues if i.severity is Severity.CRITICAL]
        out += [
            "## ⚠️ STALE OR BAD DATA",
            "",
            "Some tickers were **not scored** because their data failed a quality check. "
            "Treat everything below as incomplete:",
            "",
        ]
        out += [f"- **{i.ticker or 'universe'}** — {i.check}: {i.detail}" for i in critical]
        out.append("")

    if brief.kill_switch_active:
        out += [
            "## 🛑 DRAWDOWN KILL SWITCH ACTIVE",
            "",
            "No new short-term ideas will be issued until the portfolio is reviewed and "
            "de-risked. Long-term ideas continue.",
            "",
        ]

    if brief.triggered:
        out += ["## Action needed", ""]
        out += [f"- {line}" for line in brief.triggered]
        out.append("")

    out += ["## Portfolio", ""]
    out += [f"- {line}" for line in brief.portfolio_lines]
    out += ["", "## Risk", ""]
    out += [f"- {line}" for line in brief.risk_lines]
    out.append("")

    out += ["## Candidate ideas", ""]
    if brief.ideas:
        for idea in brief.ideas:
            out += _idea_block(idea, by_id.get(idea.id))
    else:
        out += [
            "None today. Nothing cleared both the rules layer and the risk layer, which is "
            "the expected outcome on most days.",
            "",
        ]

    if brief.what_we_got_wrong:
        out += ["## What this run got wrong", ""]
        out += [f"- {line}" for line in brief.what_we_got_wrong]
        out.append("")

    warnings = [i for i in brief.data_issues if i.severity is Severity.WARN]
    if warnings:
        out += ["## Data warnings", ""]
        out += [f"- {i.ticker or 'universe'} — {i.check}: {i.detail}" for i in warnings[:15]]
        out.append("")

    out += ["---", "", DISCLAIMER, ""]
    return "\n".join(out)


def weekly_review(review, *, subject_prefix: str = "Sentinel") -> str:
    """Render a WeeklyReview to markdown.

    Takes the review object rather than a Brief: the previous signature accepted
    a Brief purely to read one date off it, which made the two documents look
    related when they are assembled from different things.

    The "what the system got wrong" section is mandatory. If it ever arrives
    empty that is a reporting bug, not a clean week, and the renderer says so
    rather than omitting the heading.
    """
    out = [
        f"# {subject_prefix} — weekly review, week ending {review.as_of.isoformat()}",
        f"*{review.period_start.isoformat()} to {review.as_of.isoformat()} · "
        f"{review.ideas_generated} idea(s) generated · {review.closed_trades} trade(s) closed*",
        "",
        f"> {DISCLAIMER}",
        "",
        "## Performance",
        "",
        review.performance,
        "",
        "## Versus the benchmarks",
        "",
    ]
    out += [f"- {line}" for line in review.benchmark_lines]
    out += ["", "## Evals", ""]
    out += [f"- {line}" for line in review.eval_lines]

    if review.kill_criteria:
        out += ["", "## Kill criteria", ""]
        out += [f"- {line}" for line in review.kill_criteria]

    out += ["", "## What the system got wrong this week", ""]
    out += [f"- {line}" for line in review.wrong] or [
        "- (nothing recorded — this section must never be empty; an empty one is a "
        "reporting bug, not a clean week)"
    ]
    out += ["", "---", "", DISCLAIMER, ""]
    return "\n".join(out)


def weekly_subject(review) -> str:
    """Derived from what the week held, so it is worth opening."""
    if any("KILL CRITERION MET" in line for line in review.kill_criteria):
        return f"Sentinel weekly {review.as_of:%d %b} — a kill criterion has been met"
    if review.closed_trades:
        return (f"Sentinel weekly {review.as_of:%d %b} — "
                f"{review.closed_trades} trade(s) closed")
    return f"Sentinel weekly {review.as_of:%d %b} — no trades closed"


_CSS = """
body{font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
color:#1a1a1a;background:#fff;margin:0;padding:24px;max-width:720px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:28px 0 8px;
border-bottom:1px solid #e5e7eb;padding-bottom:4px} h3{font-size:15px;margin:20px 0 4px}
.meta{color:#6b7280;font-size:13px;margin-bottom:16px}
.disclaimer{background:#f8fafc;border-left:3px solid #94a3b8;padding:10px 14px;
color:#475569;font-size:13px;margin:16px 0}
.alarm{background:#fef2f2;border-left:3px solid #dc2626;padding:12px 14px;margin:16px 0}
.alarm h2{border:0;margin:0 0 6px;color:#991b1b}
ul{padding-left:20px;margin:6px 0} li{margin:3px 0}
code{background:#f1f5f9;padding:1px 4px;border-radius:3px;font-size:13px}
@media(prefers-color-scheme:dark){body{background:#0f172a;color:#e2e8f0}
h2{border-color:#334155}.meta{color:#94a3b8}
.disclaimer{background:#1e293b;border-color:#475569;color:#cbd5e1}
.alarm{background:#3f1d1d;border-color:#f87171}.alarm h2{color:#fca5a5}
code{background:#1e293b}}
"""


def to_html(brief: Brief, markdown: str, *, title: str = "Sentinel daily brief") -> str:
    """A deliberately small markdown-to-HTML pass for the email.

    Not a general markdown renderer: it handles exactly the constructs
    ``to_markdown`` emits. A dependency here would be a supply-chain risk for a
    document that is generated from our own template and read by one person.
    """
    body: list[str] = []
    in_list = False

    def close_list() -> None:
        # Closing the list BEFORE appending the next element, rather than
        # splicing `</ul>` in afterwards. The splice version put the closing tag
        # before the final `<li>` whenever a line produced no output at all —
        # which every blank line does.
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline(line[2:])}</li>")
            continue

        if line.startswith("# "):
            close_list()
            body.append(f"<h1>{_inline(line[2:])}</h1>")
        elif line.startswith("## "):
            close_list()
            klass = ' class="alarm"' if ("⚠️" in line or "🛑" in line) else ""
            body.append(f"<h2{klass}>{_inline(line[3:])}</h2>")
        elif line.startswith("### "):
            close_list()
            body.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("> "):
            close_list()
            body.append(f'<p class="disclaimer">{_inline(line[2:])}</p>')
        elif line.startswith("*") and line.endswith("*") and len(line) > 2:
            close_list()
            body.append(f'<p class="meta">{_inline(line[1:-1])}</p>')
        elif line == "---":
            close_list()
            body.append("<hr>")
        elif line:
            close_list()
            body.append(f"<p>{_inline(line)}</p>")
        # A blank line neither closes the list nor emits anything: markdown
        # lists survive a blank line between items, and so must this.
    close_list()
    return (
        f"<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{''.join(body)}</body></html>"
    )


def _inline(text: str) -> str:
    escaped = html.escape(text)
    for marker, tag in (("**", "strong"), ("*", "em")):
        parts = escaped.split(marker)
        if len(parts) > 2:
            rebuilt = parts[0]
            for index, part in enumerate(parts[1:], start=1):
                rebuilt += (f"<{tag}>{part}</{tag}>" if index % 2 else part)
            escaped = rebuilt
    return escaped


def subject_line(brief: Brief) -> str:
    """Derived from what the brief holds, not templated.

    A subject that says the same thing every day trains you to stop reading it,
    which defeats the one channel that is allowed to arrive unprompted.
    """
    if brief.triggered:
        return f"Sentinel {brief.as_of:%d %b} — action needed on {len(brief.triggered)} position(s)"
    if brief.kill_switch_active:
        return f"Sentinel {brief.as_of:%d %b} — drawdown kill switch active"
    if brief.stale:
        return f"Sentinel {brief.as_of:%d %b} — incomplete, data quality failure"
    if brief.ideas:
        names = ", ".join(i.ticker for i in brief.ideas)
        return f"Sentinel {brief.as_of:%d %b} — {len(brief.ideas)} candidate(s): {names}"
    return f"Sentinel {brief.as_of:%d %b} — no candidates, portfolio unchanged"


def summary_line(brief: Brief) -> str:
    return (
        f"{len(brief.ideas)} candidate(s), {len(brief.rejected)} rejected, "
        f"{len(brief.triggered)} action(s), "
        f"{len([i for i in brief.data_issues if i.severity is Severity.CRITICAL])} blocked ticker(s)"
    )
