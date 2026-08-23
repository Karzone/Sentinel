"""The dashboard's colour system — validated, not chosen by eye.

Every categorical slot and ordinal ramp below was run through the data-viz
validator before a line of chart code was written, in **both** modes against the
surface each actually renders on:

    4 categorical slots, light  → all checks PASS
      (worst adjacent CVD ΔE 9.1 protan, normal-vision 22.9; contrast WARN on
       aqua 2.74:1 and yellow 2.11:1 against the light surface)
    4 categorical slots, dark   → all checks PASS, all ≥ 3:1
    5-step ordinal ramp, both   → monotone lightness, ≥0.06 ΔL steps, single hue
                                  (five steps because materiality has five buckets;
                                   three would clamp the top three together)

**The light-mode contrast WARN is not dismissable and is discharged in code.**
Aqua and yellow sit below 3:1 on the light surface, which obliges "visible
labels or a table view". Both ship: every multi-series chart direct-labels its
endpoints, and every chart has a table twin behind an expander. If a future
chart drops those, it must change these slots instead.

Dark is a *selected* palette — the same eight hues re-stepped for the dark
surface — not an automatic inversion of the light one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["light", "dark"]


@dataclass(frozen=True, slots=True)
class Palette:
    mode: Mode

    #: Categorical slots, in fixed order. Assigned by entity, never by rank —
    #: filtering a series out must not repaint the survivors.
    series: tuple[str, ...]

    #: One hue, light→dark, for magnitude.
    sequential: tuple[str, ...]
    #: The ordinal subset: discrete ordered marks (conviction, materiality).
    #: Its light end must clear 2:1 against the surface, which is why it starts
    #: at step 250 on light and stops at step 600 on dark.
    ordinal: tuple[str, ...]
    #: Two hues that read as opposite, with a neutral — never a hue — midpoint.
    diverging: tuple[str, str, str]

    surface: str
    plane: str
    ink: str
    ink_secondary: str
    ink_muted: str
    grid: str
    axis: str
    border: str

    #: Reserved. Never used for "series 5", and never carried alone — every
    #: status colour ships with an icon and a label.
    status: dict[str, str] = field(default_factory=dict)

    def slot(self, index: int) -> str:
        """Categorical hue by slot. Raises past the token ceiling rather than
        generating a ninth hue, which would be indistinguishable under CVD."""
        if not 0 <= index < len(self.series):
            raise IndexError(
                f"categorical slot {index} is past the {len(self.series)}-slot ceiling; "
                "fold the tail into 'Other' or facet into small multiples instead of "
                "generating a hue"
            )
        return self.series[index]


#: Reserved status steps, identical in both modes. Deliberately distinct from
#: the categorical slots so a status colour never impersonates a series.
_STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

LIGHT = Palette(
    mode="light",
    series=("#2a78d6", "#eb6834", "#1baf7a", "#eda100"),
    sequential=("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#256abf", "#1c5cab"),
    ordinal=("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281"),
    diverging=("#2a78d6", "#f0efec", "#d03b3b"),
    surface="#fcfcfb",
    plane="#f9f9f7",
    ink="#0b0b0b",
    ink_secondary="#52514e",
    ink_muted="#898781",
    grid="#e1e0d9",
    axis="#c3c2b7",
    border="rgba(11,11,11,0.10)",
    status=_STATUS,
)

DARK = Palette(
    mode="dark",
    series=("#3987e5", "#d95926", "#199e70", "#c98500"),
    sequential=("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#256abf", "#184f95"),
    ordinal=("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#184f95"),
    diverging=("#3987e5", "#383835", "#d03b3b"),
    surface="#1a1a19",
    plane="#0d0d0d",
    ink="#ffffff",
    ink_secondary="#c3c2b7",
    ink_muted="#898781",
    grid="#2c2c2a",
    axis="#383835",
    border="rgba(255,255,255,0.10)",
    status=_STATUS,
)

PALETTES: dict[str, Palette] = {"light": LIGHT, "dark": DARK}

#: Everything stays in the system sans — including hero figures. No display or
#: serif face anywhere on a chart.
FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

# Mark specs, fixed across every chart in the dashboard.
LINE_WIDTH = 2
EMPHASIS_LINE_WIDTH = 2.75
MARKER_SIZE = 64          # Vega `size` is area; 64 → ~8px diameter.
BAR_MAX_WIDTH = 24
BAR_CORNER_RADIUS = 4
AREA_OPACITY = 0.10
SURFACE_GAP = 2


def get(mode: str) -> Palette:
    try:
        return PALETTES[mode]
    except KeyError as exc:
        raise ValueError(f"unknown mode {mode!r}; expected 'light' or 'dark'") from exc


def theme_config(mode: str) -> dict:
    """The Altair theme for a mode.

    Chrome is recessive by construction: hairline solid gridlines one step off
    the surface, no domain rule on the y-axis, no tick marks, and every piece of
    text in an ink token rather than a series colour.
    """
    p = get(mode)
    return {
        "config": {
            "background": p.surface,
            "font": FONT,
            "view": {"stroke": "transparent", "continuousWidth": 640, "continuousHeight": 300},
            "axis": {
                "labelFont": FONT, "titleFont": FONT,
                "labelColor": p.ink_muted, "titleColor": p.ink_secondary,
                "labelFontSize": 11, "titleFontSize": 11, "titleFontWeight": "normal",
                "titlePadding": 8, "labelPadding": 6,
                # Solid hairlines only — a dashed grid reads as "threshold"
                # when it is just a grid.
                "gridColor": p.grid, "gridWidth": 1, "gridDash": [],
                "domainColor": p.axis, "domainWidth": 1,
                "tickColor": p.axis, "tickSize": 0,
            },
            # Horizontal y-axis title, parked above the plot's left edge, with
            # the legend moved to the bottom so nothing shares that corner.
            "axisY": {"domain": False, "titleAngle": 0, "titleAlign": "left",
                      "titleX": 0, "titleY": -12},
            "axisX": {"grid": False},
            "legend": {
                "labelFont": FONT, "titleFont": FONT,
                "labelColor": p.ink_secondary, "titleColor": p.ink_muted,
                "labelFontSize": 11, "titleFontSize": 11, "titleFontWeight": "normal",
                "symbolType": "stroke", "symbolStrokeWidth": 3, "symbolSize": 120,
                # Bottom, not top: the y-axis title occupies the top-left and the
                # endpoint labels occupy the right edge, so a top legend collides
                # with one or the other. Both collisions were caught in screenshots.
                "orient": "bottom", "direction": "horizontal", "offset": 8,
                "columnPadding": 14,
            },
            "title": {
                "font": FONT, "fontSize": 13, "fontWeight": 600,
                "color": p.ink, "subtitleColor": p.ink_muted, "subtitleFontSize": 11,
                "anchor": "start", "offset": 12,
            },
            "range": {
                "category": list(p.series),
                "ordinal": list(p.ordinal),
                "heatmap": list(p.sequential),
                "ramp": list(p.sequential),
                "diverging": list(p.diverging),
            },
            "line": {"strokeWidth": LINE_WIDTH, "strokeCap": "round", "strokeJoin": "round"},
            "bar": {"cornerRadiusEnd": BAR_CORNER_RADIUS, "maxBandSize": BAR_MAX_WIDTH},
            "point": {"size": MARKER_SIZE, "filled": True, "strokeWidth": SURFACE_GAP,
                      "stroke": p.surface},
            "area": {"opacity": AREA_OPACITY, "line": True},
            "rule": {"strokeWidth": 1},
            "text": {"font": FONT, "fontSize": 11, "color": p.ink_secondary},
        }
    }


def register_themes() -> None:
    """Register both themes with Altair. Idempotent."""
    import altair as alt

    for mode in PALETTES:
        alt.theme.register(f"sentinel-{mode}", enable=False)(
            lambda mode=mode: theme_config(mode)
        )


def enable(mode: str) -> None:
    import altair as alt

    register_themes()
    alt.theme.enable(f"sentinel-{get(mode).mode}")
