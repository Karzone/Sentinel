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


#: Makes the page installable as a phone app ("Add to Home Screen" launches it
#: full-screen with its own icon). Streamlit gives a script no way to edit <head>,
#: so this adds the tags from JavaScript — idempotently, because every rerun
#: re-executes it. The files it points at live in dashboard/static and are served
#: by `--server.enableStaticServing`; that directory is PUBLIC (a manifest is
#: fetched without the session cookie), so it holds icons and a manifest and
#: nothing else — tests/test_pwa.py enforces that.
_PWA_HEAD = """
<script>
(function () {
  var d = window.document;
  if (d.getElementById("sx-pwa")) return;
  function add(tag, attrs) {
    var el = d.createElement(tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    d.head.appendChild(el);
    return el;
  }
  add("meta", {id: "sx-pwa", name: "sx-pwa", content: "1"});
  add("link", {rel: "manifest", href: "/app/static/manifest.json"});
  add("link", {rel: "apple-touch-icon", href: "/app/static/apple-touch-icon.png"});
  add("meta", {name: "theme-color", content: "#2a78d6"});
  add("meta", {name: "mobile-web-app-capable", content: "yes"});
  add("meta", {name: "apple-mobile-web-app-capable", content: "yes"});
  add("meta", {name: "apple-mobile-web-app-title", content: "Sentinel"});
})();
</script>
"""


def main() -> None:
    st.set_page_config(
        page_title="Sentinel", page_icon="◐", layout="wide",
        initial_sidebar_state="expanded",
    )

    # Before the password gate on purpose: the sign-in page is what gets
    # installed, and it carries no data.
    st.html(_PWA_HEAD, unsafe_allow_javascript=True)

    mode = _mode()
    pal.enable(mode)
    st.markdown(ui.shell_css(mode), unsafe_allow_html=True)
    # The brand sits ABOVE the nav, where st.navigation itself cannot put
    # content — `with st.sidebar:` blocks always land below it. One SVG serves
    # both themes: the mark is the series blue, the word the mode-shared muted
    # ink, both validated against either surface.
    st.logo(str(Path(__file__).parent / "assets" / "wordmark.svg"), size="large")

    decision = auth.gate(st)
    if not decision.may_render:
        st.stop()

    config = load_config(os.environ.get("SENTINEL_CONFIG"))
    db_path = Path(os.environ.get("SENTINEL_DB") or config.paths.db)


    try:
        conn = queries_connect(db_path)
    except FileNotFoundError as exc:
        st.error(str(exc), icon="🗄️")
        st.stop()
        return

    from . import queries
    from ..portfolio import manual

    ctx = views.Context(
        conn=conn, config=config, mode=mode, db_path=db_path,
        writable=manual.allowed_in(
            dashboard_local=auth.is_local_session(),
            demo=queries.is_demo_database(conn),
        ),
        settings_writable=not queries.is_demo_database(conn),
    )

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
    by_title = dict(views.PAGES)
    grouped: dict[str, list] = {}
    first = True
    for group, titles in views.NAV_GROUPS.items():
        entries = []
        for title in titles:
            bound = _bind(by_title[title], ctx)
            if first:
                entries.append(st.Page(bound, title=title, default=True))
                first = False
            else:
                entries.append(st.Page(bound, title=title,
                                       url_path=title.lower().replace(" ", "-")))
        grouped[group] = entries
    selected = st.navigation(grouped, position="sidebar")

    with st.sidebar:
        notice = st.session_state.get("_auth_notice")
        if notice:
            st.warning(notice, icon="🔓")
        st.caption("Research dashboard — read-only except Settings and trades")
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
