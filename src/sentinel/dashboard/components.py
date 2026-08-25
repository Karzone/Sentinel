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

#: Display face for page chrome — titles, section headings, tile values.
#: Loaded from Google Fonts with a real system fallback, so an offline
#: session degrades to the body stack instead of a blank glyph box. CHARTS
#: DELIBERATELY STAY on ``pal.FONT`` (the recorded rule in palette.py: no
#: display face on a chart) — this constant must never reach theme_config.
DISPLAY_FONT = '"Instrument Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'


def shell_css(mode: str) -> str:
    """Page chrome for a mode.

    Dark is a *selected* set of surfaces, not an inversion — the same reason the
    chart palette has its own dark steps rather than flipping the light ones.
    """
    p = pal.get(mode)
    return f"""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap');
  .stApp {{ background: {p.plane}; }}
  /* A reading measure: full-bleed text lines on a wide monitor are the
     cheapest tell of an unstyled page. Charts still stretch to this width. */
  .block-container, [data-testid="stMainBlockContainer"] {{
      max-width: 1180px; margin-inline: auto; }}
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
  /* Sidebar nav: group labels as quiet sentence-case captions, links as soft
     rows, and the current page marked by weight + an accent bar — never by
     colour alone. */
  [data-testid="stNavSectionHeader"] {{ font-size: 11.5px; font-weight: 600;
      color: {p.ink_muted}; letter-spacing: 0.01em; }}
  [data-testid="stSidebarNavLink"] {{ border-radius: 8px;
      padding: 2px 10px; margin: 1px 0; }}
  /* The link text is a <p> nested inside the span — a span-level rule never
     reaches it (checked against the live 1.62 DOM). */
  [data-testid="stSidebarNavLink"] p {{ font-size: 13.5px;
      color: {p.ink_secondary}; }}
  [data-testid="stSidebarNavLink"]:hover {{ background: {p.hover}; }}
  [data-testid="stSidebarNavLink"][aria-current="page"] {{
      background: {p.hover}; box-shadow: inset 3px 0 0 {p.series[0]}; }}
  [data-testid="stSidebarNavLink"][aria-current="page"] p {{
      color: {p.ink}; font-weight: 600; }}
  /* The display face carries the WHOLE page, not just headings — with only
     the headers switched, the app still read as stock Streamlit (owner
     feedback, 2026-08-25). Charts are unaffected: Vega sets its fonts from
     theme_config, which stays on pal.FONT by the recorded palette rule.
     Set the family on the ROOT ONLY and let it inherit. A blanket
     `.stApp span {{ font-family }}` also hits Streamlit's Material Symbols
     icon spans, and once the icon loses its font the ligature cannot
     resolve — every expander chevron rendered as the literal text
     "arrow_right" on top of its own label. Caught in a screenshot. */
  .stApp {{ font-family: {DISPLAY_FONT}; }}
  .stApp p, .stApp li, .stApp label,
  [data-testid="stMarkdownContainer"] {{ color: {p.ink_secondary}; }}
  [data-testid="stIconMaterial"], .material-symbols-rounded,
  span[class*="material"] {{ font-family: "Material Symbols Rounded" !important; }}
  .stApp h1, .stApp h2, .stApp h3, .stApp h4 {{ color: {p.ink};
      font-family: {DISPLAY_FONT}; letter-spacing: -0.01em; }}
  /* Page + section anatomy. Hierarchy comes from weight and ink, sentence
     case throughout — never tracked uppercase. */
  /* Header classes carry .stApp + element in the selector: Streamlit styles
     markdown h3/h4 through its own container rules, and a bare class loses
     that specificity fight — the section titles rendered at Streamlit's h4
     size. Caught in a screenshot. */
  .stApp h3.sx-page-title {{ font-family: {DISPLAY_FONT}; font-size: 27px;
      font-weight: 650; color: {p.ink}; letter-spacing: -0.015em; margin: 0;
      line-height: 1.15; padding: 0; }}
  .sx-page-sub {{ font-size: 13px; color: {p.ink_muted}; margin: 4px 0 0;
      max-width: 72ch; line-height: 1.5; }}
  .sx-section {{ margin: 2px 0 4px; }}
  .sx-eyebrow {{ font-size: 11px; font-weight: 500; color: {p.ink_muted};
      letter-spacing: 0.02em; margin: 0 0 2px; }}
  .stApp h4.sx-sec-title {{ font-family: {DISPLAY_FONT}; font-size: 17px;
      font-weight: 600; color: {p.ink}; letter-spacing: -0.01em; margin: 0;
      line-height: 1.3; padding: 0; }}
  .sx-sec-note {{ font-size: 12.5px; color: {p.ink_muted}; margin: 4px 0 0;
      max-width: 72ch; line-height: 1.5; }}
  /* One entity per card: name, then a quiet meta line where the numbers are
     tabular so scores align down a list. */
  .sx-entity {{ background: {p.surface}; border: 1px solid {p.border};
      border-radius: 10px; padding: 10px 13px; margin: 0 0 8px; }}
  .sx-entity:hover {{ background: {p.hover}; }}
  .sx-entity-name {{ font-size: 14px; font-weight: 600; color: {p.ink};
      margin: 0; line-height: 1.3; }}
  .sx-entity-meta {{ display: flex; align-items: baseline; gap: 10px;
      flex-wrap: wrap; font-size: 12px; color: {p.ink_muted}; margin: 3px 0 0;
      font-variant-numeric: tabular-nums; }}
  .sx-entity-score {{ font-weight: 650; font-size: 13px; color: {p.ink}; }}
  .sx-chip {{ font-size: 10.5px; padding: 1px 8px; border-radius: 999px;
      border: 1px solid {p.border}; color: {p.ink_secondary}; }}
  .stButton button {{ border-radius: 8px; font-weight: 500; }}
  @media (prefers-reduced-motion: no-preference) {{
    .sx-entity {{ transition: background 0.12s ease; }}
    .stButton button {{ transition: background 0.12s ease, border-color 0.12s ease; }}
    [data-testid="stSidebarNavLink"] {{ transition: background 0.12s ease; }}
  }}
  [data-testid="stMetricValue"] {{ color: {p.ink}; }}
  .sx-card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px;
      padding: 14px 16px; }}
  .sx-tile {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px;
      padding: 12px 14px 10px; height: 100%; }}
  .sx-tile-label {{ font-size: 11px; color: {p.ink_muted}; text-transform: none;
      margin: 0 0 2px; }}
  .sx-tile-value {{ font-size: 26px; line-height: 1.15; font-weight: 600; color: {p.ink};
      margin: 0; font-variant-numeric: proportional-nums; font-family: {DISPLAY_FONT}; }}
  .sx-hero {{ font-size: 48px; line-height: 1.05; font-weight: 600; color: {p.ink};
      margin: 0; font-variant-numeric: proportional-nums; font-family: {DISPLAY_FONT}; }}
  .sx-tile-delta {{ font-size: 12px; margin: 3px 0 0; }}
  .sx-verdict {{ border-left: 4px solid {p.border}; background: {p.surface};
      border-radius: 0 8px 8px 0; padding: 12px 16px; margin: 4px 0 6px; }}
  .sx-verdict-stance {{ font-size: 22px; font-weight: 700; margin: 0;
      letter-spacing: -0.01em; }}
  .sx-verdict-why {{ font-size: 13px; color: {p.ink_muted}; margin: 2px 0 0; }}
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

def page_header(title: str, subtitle: str | None = None) -> str:
    """The page's name and its one-sentence promise, as one block."""
    sub = (f'<p class="sx-page-sub">{html.escape(subtitle)}</p>' if subtitle else "")
    return (f'<div class="sx-section"><h3 class="sx-page-title">'
            f"{html.escape(title)}</h3>{sub}</div>")


def section(title: str, note: str | None = None, *, eyebrow: str | None = None) -> str:
    """Section anatomy: optional eyebrow, title, then a measured note.

    The note is part of the component, not a separate caption, for the same
    reason verdict_banner fuses stance and reason: a later layout change
    must not strand a heading from the sentence that qualifies it.
    """
    brow = (f'<p class="sx-eyebrow">{html.escape(eyebrow)}</p>' if eyebrow else "")
    body = (f'<p class="sx-sec-note">{html.escape(note)}</p>' if note else "")
    return (f'<div class="sx-section">{brow}'
            f'<h4 class="sx-sec-title">{html.escape(title)}</h4>{body}</div>')


def entity_card(name: str, *, meta: tuple[str, ...] = (), score: float | None = None,
                chip: str | None = None) -> str:
    """One stock in a list: name, quiet tabular meta, the score emphasised,
    conviction (or any qualifier) as a chip. Replaces the ad-hoc
    markdown-with-opacity rows the idea lists used to draw."""
    parts = [f"<span>{html.escape(item)}</span>" for item in meta]
    if score is not None:
        parts.append(f'<span class="sx-entity-score">{score:.0f}'
                     f"<span style='font-weight:400'>/100</span></span>")
    if chip:
        parts.append(f'<span class="sx-chip">{html.escape(chip)}</span>')
    meta_html = (f'<p class="sx-entity-meta">{"".join(parts)}</p>' if parts else "")
    return (f'<div class="sx-entity"><p class="sx-entity-name">'
            f"{html.escape(name)}</p>{meta_html}</div>")


def verdict_banner(stance: str, headline: str, *, status: str | None = None,
                   mode: str = "light") -> str:
    """The search page's answer, with its reason attached in the same block.

    Deliberately one component rather than a badge plus a caption: the spec's
    rule is that a signal never travels without its reasoning, and two separate
    elements can be separated by a later layout change. Here the headline cannot
    be rendered without the sentence that qualifies it.
    """
    p = pal.get(mode)
    colour = p.status.get(status or "", p.ink_muted)
    return (
        f'<div class="sx-verdict" style="border-left-color:{colour}">'
        f'<p class="sx-verdict-stance" style="color:{colour}">{html.escape(stance)}</p>'
        f'<p class="sx-verdict-why">{html.escape(headline)}</p></div>'
    )

