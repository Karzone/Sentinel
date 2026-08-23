"""Altair chart builders.

Every chart in here obeys the same short list, and the list is why they read as
one system rather than nine separate charts:

* **One y-axis. Ever.** Two measures of different scale become two charts or are
  indexed to a common base — never a second scale, which lets a chart invent a
  correlation that is not in the data.
* **Colour follows the entity, not its rank.** Series scales carry an explicit
  ``domain``/``range`` pair, so filtering a series out cannot repaint the
  survivors. A reader who learned "cash is yellow" keeps that.
* **Labels are selective.** Endpoints, extremes, and the one series the story is
  about. Never a number on every point.
* **Text wears ink tokens, never the series colour.** Identity comes from the
  coloured mark beside the text.
* **A hover layer by default**, because these render as SVG and a static chart
  throws away the channel that carries the values we deliberately did not label.
* **A table twin for every chart** (the pages render it behind an expander),
  which is also what discharges the light-mode contrast WARN on the aqua and
  yellow slots.
"""

from __future__ import annotations

from typing import Sequence

import altair as alt
import pandas as pd

from . import palette as pal

CHARTS_VERSION = "dashboard-charts-v1"

#: A chart's own height. The container is sized to include the x-axis band, so
#: a card never grows a tiny nested scrollbar.
DEFAULT_HEIGHT = 260
COMPACT_HEIGHT = 180


def _p(mode: str) -> pal.Palette:
    return pal.get(mode)


def _row_height(rows: int) -> int:
    """Height of a horizontal bar chart: one row of air per row of data.

    A fixed minimum stretched a single bar to fill its band — the mark spec
    caps bars at 24px and lets the leftover band be air, so the container has
    to follow the row count instead of the other way round. The constant
    covers the x-axis band, so the card never grows a nested scrollbar.
    """
    return max(96, 34 * max(rows, 1) + 46)


def _empty(message: str, mode: str, *, height: int = COMPACT_HEIGHT) -> alt.Chart:
    """An honest empty state.

    A chart with no data renders the reason, not an empty grid — an empty grid
    looks like "the values are all zero", which is a different claim from "we
    have not measured this yet".
    """
    p = _p(mode)
    return (
        alt.Chart(pd.DataFrame([{"message": message}]))
        .mark_text(align="center", baseline="middle", color=p.ink_muted, fontSize=12,
                   font=pal.FONT, lineBreak="\n")
        .encode(text="message:N")
        .properties(height=height, width="container")
    )


def _series_scale(names: Sequence[str], mode: str, *, strategy: str | None = None) -> alt.Scale:
    """Fixed hue per entity.

    The strategy always takes slot 1 and each benchmark keeps the slot it was
    first assigned, in a stable order — so adding a benchmark later does not
    recolour the ones already on screen.
    """
    p = _p(mode)
    ordered = ([strategy] if strategy and strategy in names else []) + [
        n for n in names if n != strategy
    ]
    return alt.Scale(
        domain=ordered,
        range=[p.slot(i % len(p.series)) for i in range(len(ordered))],
    )


# ---------------------------------------------------------------- time series


def equity_vs_benchmarks(
    frame: pd.DataFrame, mode: str, *, strategy: str = "Sentinel (paper)",
    height: int = DEFAULT_HEIGHT,
) -> alt.LayerChart | alt.Chart:
    """Paper equity against B1–B3, all in GBP on one axis.

    Emphasis rather than plain categorical: the strategy is the subject and the
    benchmarks are context, so it carries the heavier stroke and the endpoint
    dot. The benchmarks still get their own hues because "which one am I behind"
    is a real question the reader has.
    """
    if frame.empty:
        return _empty("No paper equity curve yet.\nRun the pipeline to start one.", mode)

    p = _p(mode)
    names = list(dict.fromkeys(frame["series"]))
    scale = _series_scale(names, mode, strategy=strategy)
    is_strategy = alt.datum.series == strategy

    base = alt.Chart(frame).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %y", labelAngle=0)),
        y=alt.Y("value:Q", title="GBP",
                scale=alt.Scale(zero=False, nice=True),
                axis=alt.Axis(format=",.0f")),
        color=alt.Color("series:N", scale=scale,
                        legend=alt.Legend(title=None, symbolType="stroke")),
    )

    lines = base.mark_line(interpolate="monotone").encode(
        strokeWidth=alt.condition(is_strategy, alt.value(pal.EMPHASIS_LINE_WIDTH),
                                  alt.value(pal.LINE_WIDTH)),
        opacity=alt.condition(is_strategy, alt.value(1.0), alt.value(0.85)),
    )

    # Selective direct labels: the last point of each series only. This is also
    # what discharges the light-mode contrast WARN on the aqua and yellow slots.
    last = frame.sort_values("date").groupby("series", as_index=False).tail(1)
    endpoints = (
        alt.Chart(last)
        .mark_point(size=pal.MARKER_SIZE, filled=True, stroke=p.surface,
                    strokeWidth=pal.SURFACE_GAP)
        .encode(x="date:T", y="value:Q", color=alt.Color("series:N", scale=scale, legend=None))
    )
    labels = (
        alt.Chart(last)
        .mark_text(align="left", dx=8, dy=0, fontSize=11, font=pal.FONT,
                   color=p.ink_secondary)
        .encode(x="date:T", y="value:Q",
                text=alt.Text("value:Q", format=",.0f"))
    )

    hover = alt.selection_point(fields=["date"], nearest=True, on="pointerover",
                                empty=False, clear="pointerout")
    crosshair = (
        alt.Chart(frame).mark_rule(color=p.axis, strokeWidth=1)
        .encode(x="date:T", opacity=alt.condition(hover, alt.value(0.7), alt.value(0)),
                tooltip=[alt.Tooltip("date:T", title="Date", format="%d %b %Y"),
                         alt.Tooltip("series:N", title="Series"),
                         alt.Tooltip("value:Q", title="GBP", format=",.2f")])
        .add_params(hover)
    )
    return (
        alt.layer(lines, endpoints, labels, crosshair)
        .properties(height=height, width="container")
        .resolve_scale(color="shared")
    )


def drawdown_area(
    frame: pd.DataFrame, mode: str, *, kill_pct: float = 0.15, height: int = COMPACT_HEIGHT
) -> alt.LayerChart | alt.Chart:
    """Drawdown from the running high-water mark, with the kill-switch line.

    One series, so no legend box — the title says what is plotted. The threshold
    is a solid hairline rule with a label, not a dashed grid line: dashing reads
    as "projection" when it is in fact a hard limit.
    """
    if frame.empty or "drawdown" not in frame:
        return _empty("No equity history yet, so no drawdown to show.", mode)

    p = _p(mode)
    area = (
        alt.Chart(frame)
        .mark_area(interpolate="monotone", line={"color": p.slot(0), "strokeWidth": pal.LINE_WIDTH},
                   color=p.slot(0), opacity=pal.AREA_OPACITY)
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %y", labelAngle=0)),
            y=alt.Y("drawdown:Q", title="Drawdown",
                    axis=alt.Axis(format="+.0%"), scale=alt.Scale(nice=True)),
            tooltip=[alt.Tooltip("date:T", title="Date", format="%d %b %Y"),
                     alt.Tooltip("drawdown:Q", title="Drawdown", format="+.2%"),
                     alt.Tooltip("nav:Q", title="NAV", format=",.2f")],
        )
    )
    threshold = pd.DataFrame([{"limit": -abs(kill_pct)}])
    rule = (
        alt.Chart(threshold)
        .mark_rule(color=p.status["critical"], strokeWidth=1)
        .encode(y="limit:Q")
    )
    rule_label = (
        alt.Chart(threshold)
        .mark_text(align="left", baseline="bottom", dx=4, dy=-3, fontSize=11,
                   font=pal.FONT, color=p.ink_secondary)
        .encode(y="limit:Q", text=alt.value(f"kill switch −{abs(kill_pct):.0%}"))
    )
    return alt.layer(area, rule, rule_label).properties(height=height, width="container")


# ---------------------------------------------------------------- magnitude


def sector_vs_limit(
    frame: pd.DataFrame, mode: str, *, height: int = COMPACT_HEIGHT
) -> alt.LayerChart | alt.Chart:
    """Sector weights against the one shared concentration cap.

    Horizontal because sector names are long. Every sector is measured against
    the *same* limit, so the cap is one rule across the chart rather than an
    annotation repeated on each bar. A sector over the limit takes the reserved
    critical status colour — and the page prints an icon and a label beside it,
    because a status colour never carries meaning alone.
    """
    if frame.empty:
        return _empty("No open positions, so no sector exposure.", mode)

    p = _p(mode)
    cap = float(frame["cap"].iloc[0])
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=pal.BAR_CORNER_RADIUS, height=pal.BAR_MAX_WIDTH)
        .encode(
            y=alt.Y("sector:N", title=None, sort="-x"),
            x=alt.X("weight:Q", title="Share of satellite capital",
                    axis=alt.Axis(format=".0%"),
                    scale=alt.Scale(domain=[0, max(cap * 1.35, float(frame["weight"].max()) * 1.2)])),
            # Status, not a value ramp: the colour means "over the limit",
            # it does not re-encode the bar's own length.
            color=alt.condition(alt.datum.over, alt.value(p.status["critical"]),
                                alt.value(p.slot(0))),
            tooltip=[alt.Tooltip("sector:N", title="Sector"),
                     alt.Tooltip("weight:Q", title="Weight", format=".1%"),
                     alt.Tooltip("cap:Q", title="Cap", format=".0%")],
        )
    )
    labels = (
        alt.Chart(frame)
        .mark_text(align="left", dx=6, fontSize=11, font=pal.FONT, color=p.ink_secondary)
        .encode(y=alt.Y("sector:N", sort="-x"), x="weight:Q",
                text=alt.Text("weight:Q", format=".1%"))
    )
    limit = pd.DataFrame([{"cap": cap}])
    rule = alt.Chart(limit).mark_rule(color=p.status["critical"], strokeWidth=1).encode(x="cap:Q")
    rule_label = (
        alt.Chart(limit)
        .mark_text(align="left", baseline="top", dx=4, dy=4, fontSize=11, font=pal.FONT,
                   color=p.ink_secondary)
        .encode(x="cap:Q", text=alt.value(f"cap {cap:.0%}"))
    )
    return alt.layer(bars, labels, rule, rule_label).properties(
        height=_row_height(len(frame)), width="container"
    )


def counts_bar(
    frame: pd.DataFrame, mode: str, *, field: str, count: str = "count",
    title: str | None = None, height: int = COMPACT_HEIGHT,
) -> alt.LayerChart | alt.Chart:
    """A plain magnitude bar: one series, one hue, values at the tip.

    Deliberately *not* darker-where-bigger. These categories (risk checks, rule
    ids) have no natural order, and a value ramp on nominal categories
    double-encodes bar length as hue and burns the only free channel.
    """
    if frame.empty:
        return _empty("Nothing recorded.", mode)

    p = _p(mode)
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=pal.BAR_CORNER_RADIUS, height=pal.BAR_MAX_WIDTH, color=p.slot(0))
        .encode(
            y=alt.Y(f"{field}:N", title=None, sort="-x"),
            x=alt.X(f"{count}:Q", title=title, axis=alt.Axis(format="d", tickMinStep=1)),
            tooltip=[alt.Tooltip(f"{field}:N"), alt.Tooltip(f"{count}:Q", format="d")],
        )
    )
    labels = (
        alt.Chart(frame)
        .mark_text(align="left", dx=6, fontSize=11, font=pal.FONT, color=p.ink_secondary)
        .encode(y=alt.Y(f"{field}:N", sort="-x"), x=f"{count}:Q",
                text=alt.Text(f"{count}:Q", format="d"))
    )
    return alt.layer(bars, labels).properties(
        height=_row_height(len(frame)), width="container"
    )


def module_scores(
    frame: pd.DataFrame, mode: str, *, height: int = COMPACT_HEIGHT
) -> alt.LayerChart | alt.Chart:
    """Module scores as a deviation from neutral 50 — a diverging form.

    The reader's question is "which modules are for this idea and which are
    against it", which is polarity. On a plain 0–100 bar chart 51 and 92 sit at
    the same end and look like neighbours.
    """
    if frame.empty:
        return _empty("This idea carries no module signals.", mode)

    p = _p(mode)
    positive, _mid, negative = p.diverging
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=pal.BAR_CORNER_RADIUS, height=pal.BAR_MAX_WIDTH)
        .encode(
            y=alt.Y("module:N", title=None, sort="-x"),
            x=alt.X("delta:Q", title="Score relative to neutral (50)",
                    scale=alt.Scale(domain=[-50, 50]),
                    axis=alt.Axis(values=[-50, -25, 0, 25, 50], format="+d")),
            color=alt.condition(alt.datum.delta >= 0, alt.value(positive), alt.value(negative)),
            tooltip=[alt.Tooltip("module:N", title="Module"),
                     alt.Tooltip("score:Q", title="Score", format=".1f"),
                     alt.Tooltip("confidence:Q", title="Confidence", format=".0%"),
                     alt.Tooltip("version:N", title="Version")],
        )
    )
    midpoint = (
        alt.Chart(pd.DataFrame([{"zero": 0}]))
        .mark_rule(color=p.axis, strokeWidth=1).encode(x="zero:Q")
    )
    # Two label layers rather than one with a conditional alignment: `align` and
    # `dx` are mark properties, not encoding channels, so they cannot be driven
    # by alt.condition. Splitting on the sign keeps every label outside its bar
    # end on both arms, so a short bar never clips its own value.
    positive_labels = (
        alt.Chart(frame.loc[frame["delta"] >= 0])
        .mark_text(fontSize=11, font=pal.FONT, color=p.ink_secondary, align="left", dx=6)
        .encode(y=alt.Y("module:N", sort="-x"), x="delta:Q",
                text=alt.Text("score:Q", format=".0f"))
    )
    negative_labels = (
        alt.Chart(frame.loc[frame["delta"] < 0])
        .mark_text(fontSize=11, font=pal.FONT, color=p.ink_secondary, align="right", dx=-6)
        .encode(y=alt.Y("module:N", sort="-x"), x="delta:Q",
                text=alt.Text("score:Q", format=".0f"))
    )
    return alt.layer(bars, midpoint, positive_labels, negative_labels).properties(
        height=_row_height(len(frame)), width="container"
    )


# ---------------------------------------------------------------- evals


def hit_rate_interval(
    frame: pd.DataFrame, mode: str, *, height: int = 120
) -> alt.LayerChart | alt.Chart:
    """Hit rate with its Wilson interval against the coin-flip baseline.

    The interval *is* the chart. A bare bar at 60% would be the misleading form
    here, because 60% on ten calls and 60% on four hundred are the same number
    and completely different evidence.
    """
    if frame.empty:
        return _empty("No catalyst calls have completed their horizon yet.", mode)

    p = _p(mode)
    significant = bool(frame["significant"].iloc[0]) if "significant" in frame else False
    colour = p.slot(0) if significant else p.ink_muted

    interval = (
        alt.Chart(frame).mark_rule(strokeWidth=3, color=colour, strokeCap="round")
        .encode(y=alt.Y("label:N", title=None),
                x=alt.X("low:Q", title="Hit rate",
                        axis=alt.Axis(format=".0%", values=[0, 0.25, 0.5, 0.75, 1.0]),
                        scale=alt.Scale(domain=[0, 1])),
                x2="high:Q",
                tooltip=[alt.Tooltip("rate:Q", title="Hit rate", format=".1%"),
                         alt.Tooltip("low:Q", title="95% low", format=".1%"),
                         alt.Tooltip("high:Q", title="95% high", format=".1%"),
                         alt.Tooltip("n:Q", title="Scoreable calls", format="d")])
    )
    point = (
        alt.Chart(frame)
        .mark_point(size=pal.MARKER_SIZE, filled=True, color=colour,
                    stroke=p.surface, strokeWidth=pal.SURFACE_GAP)
        .encode(y="label:N", x="rate:Q")
    )
    label = (
        alt.Chart(frame)
        .mark_text(align="left", dx=10, dy=-14, fontSize=11, font=pal.FONT, color=p.ink_secondary)
        .encode(y="label:N", x="high:Q", text=alt.Text("rate:Q", format=".0%"))
    )
    baseline = pd.DataFrame([{"coin": 0.5}])
    rule = alt.Chart(baseline).mark_rule(color=p.axis, strokeWidth=1).encode(x="coin:Q")
    rule_label = (
        alt.Chart(baseline)
        # Above the rule, inside the plot: at dy=26 it landed on the axis
        # labels and collided with the 50% tick.
        .mark_text(align="center", baseline="bottom", dy=-6, fontSize=11, font=pal.FONT,
                   color=p.ink_muted)
        .encode(x="coin:Q", y=alt.value(10), text=alt.value("coin flip"))
    )
    return alt.layer(rule, rule_label, interval, point, label).properties(
        height=height, width="container"
    )


def ordinal_bars(
    frame: pd.DataFrame, mode: str, *, field: str, value: str, order: Sequence[str],
    value_title: str, value_format: str = ".1%", height: int = COMPACT_HEIGHT,
) -> alt.LayerChart | alt.Chart:
    """Bars over an **ordered** category — conviction low→high, materiality 1→5.

    These take the ordinal ramp rather than categorical hues, because the
    categories have a real order and the ramp says so. Its light end is stepped
    to clear the surface, which is why it starts at step 250 on light rather
    than at the palest step.
    """
    if frame.empty:
        return _empty("Not enough closed outcomes to calibrate yet.", mode)

    p = _p(mode)
    present = [name for name in order if name in set(frame[field])]
    ramp = alt.Scale(
        domain=present,
        range=[p.ordinal[min(i, len(p.ordinal) - 1)] for i in range(len(present))],
    )
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=pal.BAR_CORNER_RADIUS, size=pal.BAR_MAX_WIDTH)
        .encode(
            x=alt.X(f"{field}:N", title=None, sort=list(present),
                    axis=alt.Axis(labelAngle=0)),
            y=alt.Y(f"{value}:Q", title=value_title, axis=alt.Axis(format=value_format)),
            color=alt.Color(f"{field}:N", scale=ramp, legend=None, sort=list(present)),
            tooltip=[alt.Tooltip(f"{field}:N"),
                     alt.Tooltip(f"{value}:Q", format=value_format),
                     alt.Tooltip("samples:Q", title="Samples", format="d")],
        )
    )
    labels = (
        alt.Chart(frame)
        .mark_text(dy=-8, fontSize=11, font=pal.FONT, color=p.ink_secondary, baseline="bottom")
        .encode(x=alt.X(f"{field}:N", sort=list(present)), y=f"{value}:Q",
                text=alt.Text(f"{value}:Q", format=value_format))
    )
    zero = (
        alt.Chart(pd.DataFrame([{"zero": 0}]))
        .mark_rule(color=p.axis, strokeWidth=1).encode(y="zero:Q")
    )
    return alt.layer(zero, bars, labels).properties(height=height, width="container")


def severity_history(
    frame: pd.DataFrame, mode: str, *, height: int = COMPACT_HEIGHT
) -> alt.Chart:
    """Quality issues per day by severity.

    Severity is a *state*, so it wears the reserved status palette rather than
    categorical hues — and the page prints the icon-and-label key beside it,
    because a status colour never carries meaning alone. The 2px surface gap
    between stacked segments is what separates them; no borders are drawn.
    """
    if frame.empty:
        return _empty("No quality issues recorded in this window.", mode)

    p = _p(mode)
    order = ["info", "warn", "critical"]
    present = [s for s in order if s in set(frame["severity"])]
    scale = alt.Scale(
        domain=present,
        range=[{"info": p.slot(0), "warn": p.status["warning"],
                "critical": p.status["critical"]}[s] for s in present],
    )
    return (
        alt.Chart(frame)
        .mark_bar(cornerRadiusEnd=pal.BAR_CORNER_RADIUS, stroke=p.surface,
                  strokeWidth=pal.SURFACE_GAP)
        .encode(
            x=alt.X("as_of:T", title=None, axis=alt.Axis(format="%d %b", labelAngle=0)),
            y=alt.Y("count:Q", title="Issues", stack=True,
                    axis=alt.Axis(format="d", tickMinStep=1)),
            color=alt.Color("severity:N", scale=scale, sort=present,
                            legend=alt.Legend(title=None)),
            order=alt.Order("severity:N", sort="descending"),
            tooltip=[alt.Tooltip("as_of:T", title="Date", format="%d %b %Y"),
                     alt.Tooltip("severity:N", title="Severity"),
                     alt.Tooltip("count:Q", title="Issues", format="d")],
        )
        .properties(height=height, width="container")
    )


def sparkline(frame: pd.DataFrame, mode: str, *, y: str = "nav", height: int = 44) -> alt.Chart:
    """The trend line inside a stat tile. No axes, no grid — it is a shape, not
    a plot, and the tile's value carries the number."""
    if frame.empty or len(frame) < 2:
        return _empty("", mode, height=height)
    p = _p(mode)
    return (
        alt.Chart(frame)
        .mark_line(strokeWidth=pal.LINE_WIDTH, color=p.slot(0), interpolate="monotone")
        .encode(
            x=alt.X("date:T", axis=None),
            y=alt.Y(f"{y}:Q", axis=None, scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("date:T", title="Date", format="%d %b %Y"),
                     alt.Tooltip(f"{y}:Q", title="NAV", format=",.2f")],
        )
        .properties(height=height, width="container")
    )

def price_history(
    frame: pd.DataFrame, mode: str, *, height: int = DEFAULT_HEIGHT
) -> alt.LayerChart | alt.Chart:
    """Adjusted close with its 50 and 200-day moving averages.

    One y-axis, and the averages are derived from the same series rather than a
    second measure — so this stays a single-scale chart even though it carries
    three lines. Volume is deliberately absent: it belongs to a different scale,
    and a second axis is the one thing this codebase never draws.
    """
    if frame.empty:
        return _empty("No price history for this ticker.", mode)

    p = _p(mode)
    data = frame.copy()
    data["SMA 50"] = data["close"].rolling(50).mean()
    data["SMA 200"] = data["close"].rolling(200).mean()
    long = data.melt(id_vars="date", value_vars=["close", "SMA 50", "SMA 200"],
                     var_name="series", value_name="value").dropna(subset=["value"])
    long["series"] = long["series"].replace({"close": "Close"})

    scale = alt.Scale(domain=["Close", "SMA 50", "SMA 200"],
                      range=[p.series[0], p.series[3], p.ink_muted])
    lines = (
        alt.Chart(long)
        .mark_line(strokeWidth=1.6, interpolate="monotone")
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %y", labelAngle=0)),
            y=alt.Y("value:Q", title=None, scale=alt.Scale(zero=False, nice=True)),
            color=alt.Color("series:N", scale=scale,
                            legend=alt.Legend(title=None, orient="bottom",
                                              symbolType="stroke")),
            strokeWidth=alt.condition(alt.datum.series == "Close",
                                      alt.value(2.0), alt.value(1.3)),
            tooltip=[alt.Tooltip("date:T", title="Date"),
                     alt.Tooltip("series:N", title=""),
                     alt.Tooltip("value:Q", title="Value", format=",.2f")],
        )
    )
    return lines.properties(height=height, width="container")

