"""Stat tiles, status badges and the theme shell.

Two rules from the chart method carry over to these non-chart pieces:

* **A single headline number is a stat tile, not a one-bar bar chart.** Most of
  what a portfolio dashboard leads with is a current value plus a delta, which
  is exactly the case where the number *is* the chart.
* **A status colour never carries meaning alone.** Every badge here ships an
  icon and a word, so "over the sector limit" survives a colourblind reader, a
  greyscale print and a forced-colors setting.

Hero and tile values use proportional figures; ``tabular-nums`` is reserved for
columns that must align vertically, because equal-width digits make a large
standalone number look loose.
"""

from __future__ import annotations

import html
from decimal import Decimal

from . import palette as pal

STATUS_ICONS = {"good": "●", "warning": "▲", "serious": "▲", "critical": "■", "neutral": "—"}
STATUS_WORDS = {"good": "OK", "warning": "Watch", "serious": "Serious", "critical": "Breach"}


def shell_css(mode: str) -> str:
    """Page chrome for a mode.

    Dark is a *selected* set of surfaces, not an inversion — the same reason the
    chart palette has its own dark steps rather than flipping the light ones.
    """
    p = pal.get(mode)
    return f"""
<style>
  .stApp {{ background: {p.plane}; }}
  /* The Deploy button and the running-man widget are noise on a personal
     read-only dashboard. The toolbar itself STAYS: it holds Streamlit's theme
     setting, which is now the single control for light/dark. */
  [data-testid="stAppDeployButton"], [data-testid="stDecoration"],
  [data-testid="stStatusWidget"] {{ display: none; }}
  /* Streamlit's header keeps its own background; unthemed it left a white band
     across the top of every dark-mode page. */
  [data-testid="stHeader"] {{ background: transparent; }}
  .block-container {{ padding-top: 2.4rem; }}
  section[data-testid="stSidebar"] {{ background: {p.surface};
      border-right: 1px solid {p.border}; }}
  /* Colour broadly, but set the family on the ROOT ONLY and let it inherit.
     A blanket `.stApp span {{ font-family }}` also hits Streamlit's Material
     Symbols icon spans, and once the icon loses its font the ligature cannot
     resolve — every expander chevron rendered as the literal text
     "arrow_right" on top of its own label. Caught in a screenshot. */
  .stApp {{ font-family: {pal.FONT}; }}
  .stApp p, .stApp li, .stApp label,
  [data-testid="stMarkdownContainer"] {{ color: {p.ink_secondary}; }}
  [data-testid="stIconMaterial"], .material-symbols-rounded,
  span[class*="material"] {{ font-family: "Material Symbols Rounded" !important; }}
  .stApp h1, .stApp h2, .stApp h3, .stApp h4 {{ color: {p.ink}; font-family: {pal.FONT};
      letter-spacing: -0.01em; }}
  [data-testid="stMetricValue"] {{ color: {p.ink}; }}
  .sx-card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px;
      padding: 14px 16px; }}
  .sx-tile {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px;
      padding: 12px 14px 10px; height: 100%; }}
  .sx-tile-label {{ font-size: 11px; color: {p.ink_muted}; text-transform: none;
      margin: 0 0 2px; }}
  .sx-tile-value {{ font-size: 26px; line-height: 1.15; font-weight: 600; color: {p.ink};
      margin: 0; font-variant-numeric: proportional-nums; }}
  .sx-hero {{ font-size: 48px; line-height: 1.05; font-weight: 600; color: {p.ink};
      margin: 0; font-variant-numeric: proportional-nums; }}
  .sx-tile-delta {{ font-size: 12px; margin: 3px 0 0; }}
  .sx-badge {{ display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
      padding: 3px 9px; border-radius: 999px; border: 1px solid {p.border};
      background: {p.surface}; color: {p.ink_secondary}; }}
  .sx-badge-icon {{ font-size: 10px; line-height: 1; }}
  .sx-note {{ font-size: 12px; color: {p.ink_muted}; margin: 6px 0 0; }}
  .sx-disclaimer {{ font-size: 12px; color: {p.ink_muted}; border-left: 3px solid {p.axis};
      padding: 8px 12px; background: {p.surface}; border-radius: 0 6px 6px 0; }}
  .stDataFrame {{ font-variant-numeric: tabular-nums; }}
  div[data-testid="stMetric"] {{ background: {p.surface}; border: 1px solid {p.border};
      border-radius: 10px; padding: 10px 14px; }}
</style>
"""


def badge(status: str, label: str | None = None) -> str:
    """A status pill: icon + word + colour, in that order of importance."""
    p_status = pal.LIGHT.status.get(status)
    icon = STATUS_ICONS.get(status, STATUS_ICONS["neutral"])
    word = label or STATUS_WORDS.get(status, status)
    colour = p_status or "currentColor"
    return (
        f'<span class="sx-badge"><span class="sx-badge-icon" style="color:{colour}">{icon}</span>'
        f"{html.escape(word)}</span>"
    )


def tile(label: str, value: str, *, delta: str | None = None,
         delta_status: str | None = None, mode: str = "light", hero: bool = False) -> str:
    """A stat tile. ``hero`` promotes the value to the dashboard's lead figure."""
    p = pal.get(mode)
    value_class = "sx-hero" if hero else "sx-tile-value"
    delta_html = ""
    if delta:
        colour = p.status.get(delta_status or "", p.ink_muted)
        delta_html = (
            f'<p class="sx-tile-delta" style="color:{colour}">{html.escape(delta)}</p>'
        )
    return (
        f'<div class="sx-tile"><p class="sx-tile-label">{html.escape(label)}</p>'
        f'<p class="{value_class}">{html.escape(value)}</p>{delta_html}</div>'
    )


def money(value: Decimal | float, *, symbol: str = "£") -> str:
    return f"{symbol}{float(value):,.2f}"


def percent(value: Decimal | float, *, signed: bool = False) -> str:
    fmt = "{:+.1%}" if signed else "{:.1%}"
    return fmt.format(float(value))


def status_for_drawdown(drawdown: float, limit: float) -> str:
    if drawdown >= limit:
        return "critical"
    if drawdown >= limit * 0.6:
        return "warning"
    return "good"


def status_for_age(age_days: int) -> str:
    if age_days <= 1:
        return "good"
    return "warning" if age_days <= 4 else "critical"
