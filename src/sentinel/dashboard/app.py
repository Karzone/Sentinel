"""Streamlit entry point. Run it with ``sentinel dashboard``.

Thin by design, like the CLI: it authenticates, picks a theme, opens a read-only
connection and hands a ``Context`` to whichever page is selected. Every rule that
matters lives in the library beneath it, because a rule enforced only in the
Streamlit layer is one a future page can walk past.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from .. import DISCLAIMER, __version__
from ..config import load_config
from . import auth, components as ui, palette as pal, views

def _mode() -> str:
    """Follow Streamlit's own theme rather than running a second one beside it.

    An in-app radio themed everything this code controls — the page, the cards,
    the charts — and could not reach Streamlit's own widgets, so dataframes
    stayed light on a dark page. Streamlit's setting drives both, so there is
    one control and nothing disagrees.

    Dark remains a *selected* palette: `palette.DARK` holds steps validated
    against the dark surface, not an automatic inversion of the light ones. This
    only decides which validated set to use.
    """
    theme = getattr(st.context, "theme", None)
    detected = (getattr(theme, "type", None) or "").strip().lower()
    if detected in pal.PALETTES:
        return detected
    fallback = os.environ.get("SENTINEL_DASHBOARD_THEME", "light").strip().lower()
    return fallback if fallback in pal.PALETTES else "light"


def main() -> None:
    st.set_page_config(
        page_title="Sentinel", page_icon="◐", layout="wide",
        initial_sidebar_state="expanded",
    )

    mode = _mode()
    pal.enable(mode)
    st.markdown(ui.shell_css(mode), unsafe_allow_html=True)

    decision = auth.gate(st)
    if not decision.may_render:
        st.stop()

    config = load_config(os.environ.get("SENTINEL_CONFIG"))
    db_path = Path(os.environ.get("SENTINEL_DB") or config.paths.db)

    with st.sidebar:
        st.markdown("### Sentinel")
        st.caption("Read-only research dashboard")
        st.divider()

    try:
        conn = queries_connect(db_path)
    except FileNotFoundError as exc:
        st.error(str(exc), icon="🗄️")
        st.stop()
        return

    ctx = views.Context(conn=conn, config=config, mode=mode)

    from . import queries

    if queries.is_demo_database(conn):
        st.warning(
            "This database was written by `scripts/seed_demo.py`. **Every number on every "
            "page is fabricated** — the prices are generated from a hash of the ticker and "
            "the track record is a simulation over them. Nothing here is a result.",
            icon="🧪",
        )

    # The first page is the default and is served at "/". Giving it a url_path
    # as well makes that path a 404, and Streamlit's "page not found" modal then
    # sits over the whole app swallowing clicks.
    pages = []
    for index, (title, render) in enumerate(views.PAGES):
        bound = _bind(render, ctx)
        if index == 0:
            pages.append(st.Page(bound, title=title, default=True))
        else:
            pages.append(st.Page(bound, title=title,
                                 url_path=title.lower().replace(" ", "-")))
    selected = st.navigation(pages, position="sidebar")

    with st.sidebar:
        notice = st.session_state.get("_auth_notice")
        if notice:
            st.warning(notice, icon="🔓")
        st.caption(f"Database `{db_path}`")
        st.caption(f"Satellite capital £{float(config.satellite_capital_gbp):,.0f}")
        st.caption(f"sentinel {__version__}")
        st.markdown(
            f'<p class="sx-disclaimer">{DISCLAIMER}</p>', unsafe_allow_html=True
        )

    selected.run()

    st.divider()
    st.markdown(f'<p class="sx-disclaimer">{DISCLAIMER}</p>', unsafe_allow_html=True)


def queries_connect(path: Path):
    from . import queries

    return queries.read_only_connect(path)


def _bind(render, ctx: views.Context):
    def page() -> None:
        render(st, ctx)

    page.__name__ = render.__name__
    return page


if __name__ == "__main__":
    main()
