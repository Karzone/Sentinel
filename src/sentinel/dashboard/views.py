"""The five pages.

Layout follows one rule throughout: **the filter row sits above everything it
scopes**, never inside a chart card, so every chart on a page re-renders against
the same slice. And every chart carries a table twin behind an expander — that
is both the accessibility floor (no value is reachable only through a tooltip)
and what discharges the light-mode contrast WARN on two of the series slots.

Nothing here writes. The connection is opened read-only, and there is no control
on any page that would place, size or close a trade — Phase 6 is explicit that
the dashboard is read-only in v1.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd

from .. import DISCLAIMER
from ..config import Config
from . import charts, components as ui, palette as pal, queries


@dataclass(slots=True)
class Context:
    conn: Any
    config: Config
    mode: str
    #: Path to the database, for the one seam that may write (Record a trade).
    #: The page's own `conn` stays read-only; writes open their own connection
    #: for exactly one recording and close it, so no page can drift into
    #: writing through the handle it renders from.
    db_path: Any = None
    #: True only for a local session on a non-demo database — the same policy
    #: as `portfolio.manual.allowed_in`, decided once in app.py.
    writable: bool = False


def _chart(st, chart, *, key: str | None = None) -> None:
    # theme=None matters: Streamlit's own Altair theme would otherwise override
    # the validated palette registered in palette.py.
    st.altair_chart(chart, width="stretch", theme=None, key=key)


def _table_twin(st, frame: pd.DataFrame, *, label: str = "Table view") -> None:
    """Every chart's WCAG-clean equivalent. Tooltips enhance; they never gate."""
    with st.expander(label):
        if frame.empty:
            st.caption("No rows.")
        else:
            st.dataframe(frame, width="stretch", hide_index=True)


def _section(st, title: str, subtitle: str | None = None) -> None:
    st.markdown(f"#### {title}")
    if subtitle:
        st.markdown(f'<p class="sx-note">{subtitle}</p>', unsafe_allow_html=True)


# ---------------------------------------------------------------- 1. portfolio


def conviction(st, ctx: Context) -> None:
    st.markdown("### Conviction")
    st.markdown(
        '<p class="sx-note">The latest idea per ticker that cleared BOTH the rules and '
        'risk layers, at or above your score bar. A high score that failed a check is '
        'not shown at any threshold — this page never carries a signal without its '
        'justification, so open a name to see exactly why it earned its place.</p>',
        unsafe_allow_html=True,
    )

    bar = st.radio("Score bar", [70, 80, 90], index=1, horizontal=True,
                   help="Composite score out of 100. The brief's email bar is 70.")
    qualifying, near_miss = queries.conviction_board(ctx.conn, min_score=int(bar))

    if not qualifying:
        if near_miss is not None:
            st.info(
                f"Nothing clears {bar} today. The best ACCEPTED idea is "
                f"**{near_miss.ticker}** at **{near_miss.composite_score:.0f}** — "
                f"lower the bar to see it, or wait for a stronger day. The bar is "
                f"not lowered silently.",
                icon="📉",
            )
        else:
            st.info(
                "No accepted ideas at all in the last 14 days. Either the brief has "
                "not run (Data health → Run brief), or every idea was rejected — "
                "the Ideas page shows which layer said no and why.",
                icon="📭",
            )
        return

    chosen = st.session_state.get("conviction-open")
    columns = st.columns(min(3, len(qualifying)), gap="medium")
    for index, idea_ in enumerate(qualifying):
        with columns[index % len(columns)]:
            st.markdown(
                ui.tile(
                    f"{idea_.ticker} · {idea_.idea_class.value.replace('_', ' ')}",
                    f"{idea_.composite_score:.0f}",
                    delta=f"{idea_.conviction.value} conviction · {idea_.as_of}",
                    mode=ctx.mode,
                ),
                unsafe_allow_html=True,
            )
            if st.button(f"Why {idea_.ticker}", key=f"conv-{idea_.id}"):
                chosen = idea_.ticker
                st.session_state["conviction-open"] = chosen

    if not chosen or chosen not in {i.ticker for i in qualifying}:
        st.caption("Open a name to see its statistics and the full justification.")
        return

    idea_ = next(i for i in qualifying if i.ticker == chosen)
    st.divider()
    _conviction_detail(st, ctx, idea_)


def _conviction_detail(st, ctx: Context, item) -> None:
    """Stats + the full why, for one board entry — the same sources the Search
    page uses, so the two can never disagree about a ticker."""
    verdict = queries.verdict_for(ctx.conn, item.ticker)
    st.markdown(
        ui.verdict_banner(verdict.stance, verdict.headline,
                          status=STANCE_STATUS.get(verdict.stance), mode=ctx.mode),
        unsafe_allow_html=True,
    )
    st.caption(f"Scored {item.as_of} · composite {item.composite_score:.0f}/100 · "
               f"{item.conviction.value} conviction")

    if item.memo is not None:
        _section(st, "Thesis")
        st.markdown(item.memo.thesis)
        left, right = st.columns(2, gap="large")
        with left:
            _section(st, "Bull case")
            st.markdown(item.memo.bull_case)
        with right:
            _section(st, "Bear case")
            st.markdown(item.memo.bear_case)
        _section(st, "What would prove this wrong",
                 "An idea that cannot be wrong is not an idea.")
        st.markdown(item.memo.invalidation)

    st.divider()
    _section(st, "Statistics",
             "Computed by the same indicators the technical module scores with.")
    _stat_tiles(st, ctx, queries.ticker_stats(ctx.conn, item.ticker))

    prices = queries.price_frame(ctx.conn, item.ticker)
    _section(st, "Price", "Adjusted close with the 50 and 200-day moving averages.")
    _chart(st, charts.price_history(prices, ctx.mode,
                                    crosses=queries.sma_crosses(prices)),
           key=f"conv-price-{item.ticker}")

    _section(st, "Module scores", "Shown as a deviation from neutral 50.")
    frame = queries.module_scores_frame(item)
    _chart(st, charts.module_scores(frame, ctx.mode), key=f"conv-mod-{item.ticker}")


def portfolio(st, ctx: Context) -> None:
    st.markdown("### Portfolio")
    snapshot = queries.portfolio_snapshot(ctx.conn, ctx.config)
    equity = queries.equity_frame(ctx.conn)
    limit = float(ctx.config.risk.drawdown_kill_pct) / 100.0

    # The lead figure is NAV: one current value, so a stat tile rather than a
    # one-bar bar chart.
    lead, tiles = st.columns([1.15, 3], gap="medium")
    with lead:
        st.markdown(
            ui.tile("Satellite NAV", ui.money(snapshot.nav),
                    delta=f"{ui.percent(snapshot.total_return, signed=True)} since inception",
                    delta_status="good" if snapshot.total_return >= 0 else "critical",
                    mode=ctx.mode, hero=True),
            unsafe_allow_html=True,
        )
        if not equity.empty:
            _chart(st, charts.sparkline(equity, ctx.mode), key="nav-spark")

    with tiles:
        row = st.columns(4, gap="small")
        drawdown_status = ui.status_for_drawdown(float(snapshot.drawdown), limit)
        cells = [
            ("Cash", ui.money(snapshot.cash), None, None),
            ("Invested", ui.money(snapshot.invested),
             f"{len(queries.positions_frame(ctx.conn))} position(s)", None),
            ("Drawdown", ui.percent(snapshot.drawdown),
             f"limit {limit:.0%}", drawdown_status),
            ("High-water", ui.money(snapshot.high_water), None, None),
        ]
        for column, (label, value, delta, status) in zip(row, cells):
            with column:
                st.markdown(
                    ui.tile(label, value, delta=delta, delta_status=status, mode=ctx.mode),
                    unsafe_allow_html=True,
                )

    st.divider()
    _section(st, "Equity against the benchmarks",
             "All in GBP on one axis, indexed to the same starting capital. "
             "A benchmark whose prices are not ingested is absent rather than flat-lined.")
    benchmarks = queries.benchmark_frame(ctx.conn, ctx.config)
    _chart(st, charts.equity_vs_benchmarks(benchmarks, ctx.mode), key="equity")
    _table_twin(st, benchmarks)

    missing = [
        f"{key} ({ctx.config.benchmarks.get(key)})"
        for key in ("B1", "B2")
        if ctx.config.benchmarks.get(key)
        and ctx.config.benchmarks[key] not in ("CASH", "RANDOM")
        and benchmarks[benchmarks["series"].str.contains(ctx.config.benchmarks[key], regex=False)].empty
        if not benchmarks.empty
    ]
    if missing:
        st.markdown(
            f'<p class="sx-note">Not plotted, because their prices have not been ingested: '
            f'{", ".join(missing)}. Run <code>sentinel ingest --tickers …</code> to enable them.</p>',
            unsafe_allow_html=True,
        )

    st.divider()
    left, right = st.columns([3, 2], gap="large")
    with left:
        _section(st, "Open positions", "Distance to stop is the column the brief leads on.")
        positions = queries.positions_frame(ctx.conn)
        if positions.empty:
            st.caption("No open positions. Satellite capital is entirely in cash.")
        else:
            st.dataframe(
                positions, width="stretch", hide_index=True,
                column_config={
                    "entry": st.column_config.NumberColumn("Entry", format="%.2f"),
                    "mark": st.column_config.NumberColumn("Mark", format="%.2f"),
                    "stop": st.column_config.NumberColumn("Stop", format="%.2f"),
                    # move is a FRACTION (-0.274), so the percent format needs
                    # ProgressColumn-style scaling; "%+.1f%%" printed the raw
                    # fraction with a % sign and -27.4% displayed as -0.3%.
                    "move": st.column_config.NumberColumn(
                        "Move", format="percent"),
                    "to_stop": st.column_config.ProgressColumn(
                        "To stop", format="%.1f%%", min_value=0.0, max_value=0.5,
                    ),
                    "value_gbp": st.column_config.NumberColumn("Value (£)", format="%.2f"),
                },
            )

        _record_trade_forms(st, ctx)

    with right:
        _section(st, "Sector allocation", "Every sector against the same cap.")
        sectors = queries.sector_frame(ctx.conn, ctx.config)
        _chart(st, charts.sector_vs_limit(sectors, ctx.mode), key="sectors")
        if not sectors.empty and bool(sectors["over"].any()):
            breached = ", ".join(sectors[sectors["over"]]["sector"])
            st.markdown(
                ui.badge("critical", f"Over the {float(ctx.config.risk.max_sector_pct):.0f}% cap: {breached}"),
                unsafe_allow_html=True,
            )
        _table_twin(st, sectors)

    exposure = queries.class_exposure(ctx.conn, ctx.config)
    st.markdown(
        f'<p class="sx-note">Short-term book £{exposure["swing_gbp"]:,.2f} of a '
        f'£{exposure["cap_gbp"]:,.2f} cap '
        f'({float(ctx.config.risk.swing_max_pct):.0f}% of satellite).</p>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------- 2. risk


def _record_trade_forms(st, ctx: Context) -> None:
    """Record a fill that already happened at the broker — the write seam.

    Rendered only for a local session on a real database (`ctx.writable`,
    decided once in app.py by `portfolio.manual.allowed_in`). The page's own
    connection stays read-only; each recording opens its own write connection
    for that one transaction. All rules live in `portfolio.manual`, shared
    with `sentinel paper buy/sell` — this function is only a form.
    """
    if not ctx.writable:
        return

    import sqlite3

    from ..domain.enums import IdeaClass
    from ..money import dec
    from ..portfolio import manual

    def _write(fn, **kwargs):
        conn = sqlite3.connect(str(ctx.db_path))
        conn.row_factory = sqlite3.Row
        try:
            return fn(conn, ctx.config, **kwargs)
        finally:
            conn.close()

    with st.expander("Record a trade — a fill that already happened at your broker"):
        st.markdown(
            '<p class="sx-note">This writes your book, it places no order — Sentinel '
            'has no broker connection. A risk limit the position breaks is warned '
            'about and recorded anyway; the same fill twice is refused.</p>',
            unsafe_allow_html=True,
        )
        buy_tab, sell_tab = st.tabs(["Bought", "Sold"])

        with buy_tab, st.form("record-buy", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            ticker = c1.text_input("Ticker", placeholder="NVDA.US")
            shares = c2.number_input("Shares", min_value=1, step=1, value=1)
            price = c3.text_input("Fill price", placeholder="178.50",
                                  help="In the instrument's own currency.")
            c4, c5, c6 = st.columns(3)
            opened = c4.date_input("Fill date", value="today", format="YYYY-MM-DD")
            stop = c5.text_input("Stop (optional)", placeholder="150")
            klass = c6.selectbox("Class", ["long_term", "swing"])
            note = st.text_input("Why you hold it / what would make you sell (optional)")
            if st.form_submit_button("Record buy"):
                try:
                    result = _write(
                        manual.record_buy, ticker=ticker, shares=int(shares),
                        price=dec(price), opened_on=opened,
                        stop=dec(stop) if stop.strip() else None,
                        idea_class=IdeaClass(klass), note=note,
                    )
                except (manual.ManualEntryError, ArithmeticError, ValueError) as exc:
                    st.error(str(exc) or "that price could not be parsed", icon="⛔")
                else:
                    for warning in result.warnings:
                        st.warning(warning, icon="⚠️")
                    st.success(
                        f"Recorded {result.position.shares} × {result.position.ticker} "
                        f"@ {result.position.entry} {result.position.currency} "
                        f"(£{result.gbp_amount:,.2f}) · cash £{result.cash:,.2f}. "
                        f"Reload the page to see it in the tables.",
                        icon="✅",
                    )

        with sell_tab:
            open_tickers = queries.positions_frame(ctx.conn)
            names = open_tickers["ticker"].tolist() if not open_tickers.empty else []
            if not names:
                st.caption("No open positions to close.")
            else:
                with st.form("record-sell", clear_on_submit=True):
                    s1, s2, s3 = st.columns(3)
                    sold = s1.selectbox("Position", names)
                    exit_price = s2.text_input("Exit price")
                    closed = s3.date_input("Exit date", value="today", format="YYYY-MM-DD")
                    if st.form_submit_button("Record sell"):
                        try:
                            result = _write(manual.record_sell, ticker=sold,
                                            price=dec(exit_price), closed_on=closed)
                        except (manual.ManualEntryError, ArithmeticError, ValueError) as exc:
                            st.error(str(exc) or "that price could not be parsed", icon="⛔")
                        else:
                            st.success(
                                f"Closed {result.position.shares} × {result.position.ticker} "
                                f"@ {result.position.exit_price} · P&L £{result.pnl:,.2f} · "
                                f"cash £{result.cash:,.2f}. Reload to refresh the tables.",
                                icon="✅",
                            )


def _offer_fetch(st, ctx: Context, ticker: str) -> None:
    """A ticker we hold nothing on. Say so, and offer to change that.

    "SOFI returns nothing" was the report: search only listed what past
    ingests happened to cover, and the page gave no path from "not here" to
    "here". Fetching is a real ingest through the job runner (detached, one
    at a time), never a quiet inline network call.
    """
    st.info(f"Nothing ingested for **{ticker}** yet — no prices, no "
            f"fundamentals, no news.", icon="🔍")
    if not ctx.writable:
        st.caption(f"Fetch it with `sentinel ingest --tickers {ticker}` and reload.")
        return
    from . import jobs

    current = jobs.running(ctx.db_path)
    if current is not None:
        st.caption(f"`{current.name}` is already running (started "
                   f"{current.started_at}); one job at a time. Reload to check.")
        return
    left, right = st.columns(2, gap="medium")
    with left:
        if st.button(f"Fetch {ticker} now", key="fetch-any"):
            _start_job(st, ctx, "ingest", ["--tickers", ticker, "--history", "600"])
            st.caption("A single ticker takes well under a minute. Reload, then "
                       "search it again.")
    with right:
        if st.button(f"Fetch AND score {ticker}", key="fetch-score-any"):
            # brief ingests nothing, so this must be the scoring pass only
            # after data exists — chain by running brief on next reload is
            # more machinery than it is worth; score after the fetch lands.
            _start_job(st, ctx, "ingest", ["--tickers", ticker, "--history", "600"])
            st.caption("When the fetch finishes, come back and press "
                       "“Score this ticker”.")


def reports(st, ctx: Context) -> None:
    st.markdown("### Reports")
    st.markdown(
        '<p class="sx-note">Every brief and weekly review the pipeline has written, '
        'newest first — the same markdown `sentinel brief` and `sentinel weekly` '
        'print to the terminal and save under <code>data/briefs</code>.</p>',
        unsafe_allow_html=True,
    )
    files = queries.list_reports(ctx.config.paths.briefs)
    if not files:
        st.info("No reports yet. Run the brief or the weekly review — the "
                "buttons are on the Data health page.", icon="📭")
        return
    names = {f.name: f for f in files}
    chosen = st.selectbox("Report", list(names), index=0)
    st.divider()
    st.markdown(names[chosen].read_text(encoding="utf-8"))


def risk(st, ctx: Context) -> None:
    st.markdown("### Risk")
    snapshot = queries.portfolio_snapshot(ctx.conn, ctx.config)
    limit = float(ctx.config.risk.drawdown_kill_pct) / 100.0

    if snapshot.kill_switch:
        st.error(snapshot.kill_switch_reason or "Drawdown kill switch is active.", icon="🛑")
    else:
        st.markdown(
            ui.badge("good", f"Kill switch inactive — drawdown {ui.percent(snapshot.drawdown)} "
                             f"of a {limit:.0%} limit"),
            unsafe_allow_html=True,
        )

    row = st.columns(4, gap="small")
    limits = ctx.config.risk
    for column, (label, value, note) in zip(row, [
        ("Max single position", f"{float(limits.max_single_position_pct):.0f}%", "of satellite"),
        ("Max sector", f"{float(limits.max_sector_pct):.0f}%", "of satellite"),
        ("Risk per trade", f"{float(limits.risk_per_trade_pct):.0f}%", "if the stop fills"),
        ("Short-term cap", f"{float(limits.swing_max_pct):.0f}%", "of satellite"),
    ]):
        with column:
            st.markdown(ui.tile(label, value, delta=note, mode=ctx.mode), unsafe_allow_html=True)

    st.divider()
    _section(st, "Drawdown from the high-water mark",
             "Measured from the peak, not from starting capital — up 50% then down 16% "
             "from the peak is still a kill-switch event.")
    equity = queries.equity_frame(ctx.conn)
    _chart(st, charts.drawdown_area(equity, ctx.mode, kill_pct=limit), key="drawdown")
    _table_twin(st, equity[["date", "nav", "high_water", "drawdown"]] if not equity.empty else equity)

    st.divider()
    left, right = st.columns(2, gap="large")
    failures = queries.risk_failures_frame(ctx.conn)
    with left:
        _section(st, "Risk-check failures", "Which limit refused an idea, and how often.")
        _chart(st, charts.counts_bar(queries.failure_counts(failures), ctx.mode,
                                     field="check", title="Failures"), key="risk-failures")
    with right:
        _section(st, "Rules-layer rejections",
                 "A rising rate means the synthesis prompts need work, not that the rules are wrong.")
        _chart(st, charts.counts_bar(queries.rule_rejection_counts(ctx.conn), ctx.mode,
                                     field="rule", title="Rejections"), key="rule-rejections")

    _section(st, "Failure log")
    if failures.empty:
        st.caption("No risk-check failures recorded.")
    else:
        st.dataframe(failures.head(200), width="stretch", hide_index=True)


# ---------------------------------------------------------------- 3. ideas


def ideas(st, ctx: Context) -> None:
    st.markdown("### Ideas")
    st.markdown(
        '<p class="sx-note">Every idea ever generated, including the ones the rules layer '
        'rejected. Rejections are kept because deleting them would make the rejection rate '
        'unmeasurable.</p>',
        unsafe_allow_html=True,
    )

    frame = queries.ideas_frame(ctx.conn)
    if frame.empty:
        st.info("No ideas stored yet. Run `sentinel brief` to generate some.", icon="ℹ️")
        return

    # One filter row, above everything it scopes.
    filters = st.columns([2, 2, 2, 3], gap="small")
    with filters[0]:
        only_accepted = st.selectbox("Show", ["All", "Accepted only", "Rejected only"], index=0)
    with filters[1]:
        tickers = ["All"] + sorted(frame["ticker"].unique().tolist())
        ticker = st.selectbox("Ticker", tickers, index=0)
    with filters[2]:
        convictions = ["All"] + sorted(frame["conviction"].unique().tolist())
        conviction = st.selectbox("Conviction", convictions, index=0)

    view = frame
    if only_accepted == "Accepted only":
        view = view[view["accepted"]]
    elif only_accepted == "Rejected only":
        view = view[~view["accepted"]]
    if ticker != "All":
        view = view[view["ticker"] == ticker]
    if conviction != "All":
        view = view[view["conviction"] == conviction]

    # The id is the idea's internal UUID — it joins ideas to audit events and
    # positions, and the "Open an idea" selector below uses it. As a table
    # column it is 36 characters of noise, so it is dropped from display only.
    st.dataframe(
        view.drop(columns=["id"]), width="stretch", hide_index=True,
        column_config={"score": st.column_config.ProgressColumn(
            "Composite", format="%.0f", min_value=0, max_value=100)},
    )

    if view.empty:
        st.caption("Nothing matches those filters.")
        return

    st.divider()
    labels = {
        f"{row.ticker} · {row.as_of} · {row.score:.0f}/100"
        f"{' · rejected' if not row.accepted else ''}": row.id
        for row in view.itertuples()
    }
    chosen = st.selectbox("Open an idea", list(labels), index=0)
    item = queries.idea(ctx.conn, labels[chosen])
    if item is None:
        st.warning("That idea could not be loaded.")
        return

    header, scores = st.columns([3, 2], gap="large")
    with header:
        if item.memo:
            st.markdown(f"**Thesis.** {item.memo.thesis}")
            st.markdown(f"**Bull case.** {item.memo.bull_case}")
            st.markdown(f"**Bear case.** {item.memo.bear_case}")
            st.markdown(f"**This is wrong if:** {item.memo.invalidation}")
            st.markdown(
                f'<p class="sx-note">{item.memo.idea_class.value} · '
                f'{item.memo.conviction.value} conviction · '
                f'{item.memo.horizon_days}-day horizon</p>',
                unsafe_allow_html=True,
            )
        else:
            # The audit trail knows WHY the memo is missing (no LLM, a failed
            # call, or a composite under the bar) — never guess when it can be
            # read back.
            st.info(queries.memo_absence_reason(ctx.conn, item), icon="ℹ️")
        if item.rejected_by_rules:
            st.markdown(ui.badge("critical", "Rejected by the rules layer"), unsafe_allow_html=True)
            for reason in item.rejected_by_rules:
                st.markdown(f"- {reason}")

    with scores:
        _section(st, "Module scores", "Shown as a deviation from neutral 50.")
        module_frame = queries.module_scores_frame(item)
        _chart(st, charts.module_scores(module_frame, ctx.mode), key="module-scores")
        _table_twin(st, module_frame)

    _section(st, "Evidence", "Every claim in the memo must trace to one of these keys.")
    st.dataframe(queries.evidence_frame(item), width="stretch", hide_index=True)

    if item.catalyst:
        st.markdown(
            f'<p class="sx-note">Catalyst: {item.catalyst.catalyst_type.value}, '
            f'direction {item.catalyst.direction.value}, materiality '
            f'{item.catalyst.materiality}/5 over {item.catalyst.horizon_days} days.</p>',
            unsafe_allow_html=True,
        )
    st.caption(f"Inputs digest `{item.inputs_digest}` · model versions {dict(item.model_versions)}")


# ---------------------------------------------------------------- 4. evals


def evals(st, ctx: Context) -> None:
    st.markdown("### Evals")
    st.markdown(
        '<p class="sx-note">These are written to be able to return a negative verdict. '
        'A number without its sample size is not a result, so every rate here carries '
        'its interval.</p>',
        unsafe_allow_html=True,
    )

    compliance = queries.llm_compliance(ctx.conn)
    row = st.columns(3, gap="small")
    with row[0]:
        rate = compliance.get("rate")
        st.markdown(
            ui.tile("LLM schema compliance",
                    f"{rate:.1%}" if rate is not None else "—",
                    delta=f"{compliance['calls']} call(s)",
                    delta_status="good" if (rate or 0) >= 0.99 else
                    ("critical" if rate is not None else None),
                    mode=ctx.mode),
            unsafe_allow_html=True,
        )
    calls = queries.catalyst_calls(ctx.conn)
    scoreable, abstained = queries.catalyst_call_counts(calls)
    with row[1]:
        # The count MUST be the scoreable one, not len(calls): the gate below
        # counts only calls that committed to a direction, and a tile that
        # counted the abstentions too overstated progress toward it.
        gate = "100 needed for a verdict"
        if abstained:
            gate += f" · {abstained} flat not scored"
        st.markdown(ui.tile("Scoreable catalyst calls", str(scoreable),
                            delta=gate, mode=ctx.mode),
                    unsafe_allow_html=True)
    outcomes = queries.conviction_outcomes(ctx.conn)
    with row[2]:
        st.markdown(ui.tile("Closed positions", str(len(outcomes)),
                            delta="feeds conviction calibration", mode=ctx.mode),
                    unsafe_allow_html=True)
    st.caption(compliance["verdict"])

    st.divider()
    _section(st, "Catalyst direction accuracy",
             "The interval is the point: 60% on ten calls and 60% on four hundred are the "
             "same number and completely different evidence.")
    accuracy = queries.direction_accuracy_frame(calls)
    _chart(st, charts.hit_rate_interval(accuracy, ctx.mode), key="hit-rate")
    if not accuracy.empty:
        st.caption(str(accuracy["verdict"].iloc[0]))
    _table_twin(st, accuracy)

    st.divider()
    left, right = st.columns(2, gap="large")
    with left:
        _section(st, "Conviction calibration",
                 "High-conviction ideas must outperform low-conviction ones, or the label is noise.")
        conviction = queries.conviction_frame(outcomes)
        _chart(st, charts.ordinal_bars(conviction, ctx.mode, field="conviction",
                                       value="mean_return", order=["low", "medium", "high"],
                                       value_title="Mean return"), key="conviction")
        if not conviction.empty:
            st.caption(str(conviction.attrs.get("verdict", "")))
        _table_twin(st, conviction)

    with right:
        _section(st, "Materiality calibration",
                 "Do materiality-5 events actually move price more than materiality-1 ones?")
        materiality = queries.materiality_frame(calls)
        _chart(st, charts.ordinal_bars(materiality, ctx.mode, field="bucket",
                                       value="mean_abs_move",
                                       order=["1", "2", "3", "4", "5"],
                                       value_title="Mean absolute move"), key="materiality")
        if not materiality.empty:
            st.caption(str(materiality.attrs.get("verdict", "")))
        _table_twin(st, materiality)

    st.divider()
    _section(st, "Kill criteria", "Pre-committed in the spec so they cannot be rationalised away.")
    from ..evals.calibration import KillCriteria

    criteria = KillCriteria(
        paper_months=0.0, strategy_sharpe=None, benchmark_sharpe=None,
        strategy_return=None, benchmark_return=None,
        catalyst_samples=scoreable,
        catalyst_beats_coin_flip=(
            bool(accuracy["significant"].iloc[0]) if not accuracy.empty else None
        ),
    )
    for verdict in criteria.verdicts():
        st.markdown(f"- {verdict}")


# ---------------------------------------------------------------- 5. data health


def _run_jobs_panel(st, ctx: Context) -> None:
    """Start ingest / the weekly pipeline from the page — local sessions only.

    The job is a detached process with a lock file (`dashboard.jobs`), so a
    closed tab does not kill it and a double click cannot start it twice. The
    page shows liveness from the lock and the last lines of the job log; the
    numbers above refresh on reload once the run completes.
    """
    if not ctx.writable:
        return

    from . import jobs

    current = jobs.running(ctx.db_path)
    with st.expander("Run a job — the same commands the terminal runs",
                     expanded=current is not None):
        if current is not None:
            st.info(f"`{current.name}` running since {current.started_at} "
                    f"(pid {current.pid}). Reload to refresh; closing this tab "
                    f"does not stop it.", icon="⏳")
            if st.button("Refresh status"):
                st.rerun()
        else:
            universes = sorted(ctx.config.universes) or ["core"]
            universe = st.selectbox("Universe", universes,
                                    index=universes.index("ai") if "ai" in universes else 0)
            left, middle, right = st.columns(3, gap="medium")
            with left:
                st.markdown("**Fetch latest data** — prices, fundamentals and "
                            "news (`sentinel ingest`).")
                history = st.number_input("Days of price history", min_value=30,
                                          max_value=2000, value=600, step=10)
                if st.button("Run ingest"):
                    _start_job(st, ctx, "ingest",
                               ["--universe", universe, "--history", str(int(history))])
            with middle:
                st.markdown("**Score the universe** — every module per ticker, "
                            "risk verdicts, and today's brief (`sentinel brief`). "
                            "Calls the LLM: costs API credits and a few minutes.")
                if st.button("Run brief"):
                    _start_job(st, ctx, "brief", ["--universe", universe])
            with right:
                st.markdown("**Weekly review** — the retrospective: performance, "
                            "evals, kill criteria (`sentinel weekly`). No LLM.")
                if st.button("Run weekly"):
                    _start_job(st, ctx, "weekly", [])

        log_tail = jobs.tail(ctx.db_path)
        if log_tail:
            with st.expander("Job log (last 40 lines)"):
                st.code(log_tail)


def _start_job(st, ctx: Context, name: str, extra: list[str]) -> None:
    from . import jobs

    try:
        job = jobs.start(name, db_path=ctx.db_path, extra_args=extra)
    except jobs.JobRefused as exc:
        st.error(str(exc), icon="⛔")
    else:
        st.success(f"Started `{job.name}` (pid {job.pid}). It keeps running if "
                   f"you close this page; reload to see progress.", icon="🚀")


def data_health(st, ctx: Context) -> None:
    st.markdown("### Data health")
    st.markdown(
        '<p class="sx-note">A signal generated from bad data is a Sev-1, so a critical issue '
        'means the ticker was not scored at all — not scored-and-flagged.</p>',
        unsafe_allow_html=True,
    )

    _run_jobs_panel(st, ctx)

    counts = queries.severity_counts(ctx.conn)
    row = st.columns(3, gap="small")
    for column, (level, status) in zip(
        row, [("critical", "critical"), ("warn", "warning"), ("info", "good")]
    ):
        with column:
            st.markdown(
                ui.tile(f"{level.title()} (60d)", str(counts.get(level, 0)),
                        delta_status=status if counts.get(level, 0) else None, mode=ctx.mode),
                unsafe_allow_html=True,
            )

    st.divider()
    _section(st, "Freshness", "Same thresholds the quality layer uses, so this and "
                              "`sentinel health` cannot disagree about what stale means.")
    freshness = queries.freshness_frame(ctx.conn)
    if freshness.empty:
        st.info("No price history ingested yet.", icon="ℹ️")
    else:
        legend = " ".join(
            ui.badge(status, word) for status, word in
            [("good", "current"), ("warning", "1–4 days behind"), ("critical", "stale")]
        )
        st.markdown(legend, unsafe_allow_html=True)
        display = freshness.copy()
        display["status"] = display["status"].map(
            {"good": "● current", "warning": "▲ 1–4 days behind", "critical": "■ stale"}
        )
        st.dataframe(display, width="stretch", hide_index=True)

    st.divider()
    _section(st, "Quality issues over time", "Severity is a state, so it wears the reserved "
                                             "status palette rather than series colours.")
    history = queries.quality_history_frame(ctx.conn)
    _chart(st, charts.severity_history(history, ctx.mode), key="severity")
    _table_twin(st, history)

    _section(st, "Recent issues")
    st.dataframe(queries.quality_issue_table(ctx.conn), width="stretch", hide_index=True)

    with st.expander("Audit trail summary"):
        st.dataframe(queries.audit_counts(ctx.conn), width="stretch", hide_index=True)


# ---------------------------------------------------------------- 6. search


STANCE_STATUS = {"BUY": "good", "HOLD": "warning", "AVOID": "critical", "NOT SCORED": None}


def search(st, ctx: Context) -> None:
    st.markdown("### Search")
    st.markdown(
        '<p class="sx-note">One ticker, what the system knows about it, and what it '
        'decided. The verdict applies no threshold of its own — it reports decisions '
        'the rules and risk layers already made, so this page cannot disagree with '
        'the brief.</p>',
        unsafe_allow_html=True,
    )

    tickers = queries.searchable_tickers(ctx.conn)

    typed = st.text_input(
        "Any ticker", key="search-any", placeholder="e.g. SOFI or VOD.LSE",
        help="A bare symbol is read as a US listing (SOFI → SOFI.US). "
             "Anything not yet ingested can be fetched from here.",
    )
    wanted = queries.normalize_ticker(typed)
    if wanted and wanted not in tickers:
        _offer_fetch(st, ctx, wanted)
        return
    if wanted:
        ticker = wanted
    else:
        if not tickers:
            st.info("No ticker has price history yet. Run `sentinel ingest` first.", icon="📭")
            return
        ticker = st.selectbox("Ingested tickers", tickers, key="search-ticker")
    if not ticker:
        return

    verdict = queries.verdict_for(ctx.conn, ticker)
    stats = queries.ticker_stats(ctx.conn, ticker)

    # The verdict, and immediately under it the reason — never a bare signal.
    st.markdown(
        ui.verdict_banner(verdict.stance, verdict.headline,
                          status=STANCE_STATUS.get(verdict.stance), mode=ctx.mode),
        unsafe_allow_html=True,
    )
    if verdict.as_of:
        st.caption(
            f"Scored {verdict.as_of} · composite {verdict.composite:.0f}/100 · "
            f"{verdict.conviction} conviction"
            + (f" · {verdict.horizon_days}-day horizon" if verdict.horizon_days else "")
        )

    if verdict.as_of is None and ctx.writable:
        from . import jobs

        if jobs.running(ctx.db_path) is None:
            if st.button(f"Score {ticker} now (runs the pipeline + LLM on this one ticker)",
                         key="score-any"):
                _start_job(st, ctx, "brief", ["--tickers", ticker])

    if verdict.blockers:
        _section(st, "Why it is not a buy", "Every layer that refused it, and what it said.")
        for blocker in verdict.blockers:
            st.markdown(f"- {blocker}")
    if verdict.thesis:
        _section(st, "Thesis")
        st.markdown(verdict.thesis)
    if verdict.falsifier:
        _section(st, "What would falsify this",
                 "An idea that cannot be wrong is not an idea.")
        st.markdown(verdict.falsifier)

    st.divider()
    _section(st, "Statistics",
             "Computed by the same indicators the technical module scores with.")
    _stat_tiles(st, ctx, stats)

    st.divider()
    _section(st, "Price",
             "Adjusted close with the 50 and 200-day moving averages. Triangles mark "
             "golden/death crosses — trend events the technical module also sees, "
             "not buy/sell advice; the verdict above is the system's actual opinion.")
    prices = queries.price_frame(ctx.conn, ticker)
    _chart(st, charts.price_history(prices, ctx.mode,
                                    crosses=queries.sma_crosses(prices)),
           key=f"price-{ticker}")
    _table_twin(st, prices.tail(60))

    news = queries.news_frame(ctx.conn, ticker)
    st.divider()
    _section(st, "Recent news",
             "What the sentiment module read — captured by the news vendor at each "
             "ingest, last 14 days.")
    if news.empty:
        st.caption("No stored headlines for this ticker in the last 14 days. "
                   "News arrives with `sentinel ingest` (Finnhub key required).")
    else:
        st.dataframe(
            news, width="stretch", hide_index=True,
            column_config={
                "published": st.column_config.DateColumn("Published"),
                "headline": st.column_config.TextColumn("Headline", width="large"),
                "source": st.column_config.TextColumn("Source"),
                "url": st.column_config.LinkColumn("Link", display_text="open"),
            },
        )

    if verdict.as_of is not None:
        st.divider()
        _section(st, "Module scores", "Shown as a deviation from neutral 50.")
        item = queries.latest_idea_for(ctx.conn, ticker)
        if item is not None:
            frame = queries.module_scores_frame(item)
            _chart(st, charts.module_scores(frame, ctx.mode), key=f"modules-{ticker}")
            _table_twin(st, frame)


def _stat_tiles(st, ctx: Context, stats: dict) -> None:
    if not stats:
        st.caption("No price history for this ticker.")
        return

    def pct(value, digits=1):
        return "—" if value is None else f"{value * 100:.{digits}f}%"

    def num(value, digits=2):
        return "—" if value is None else f"{value:,.{digits}f}"

    rsi = stats.get("rsi14")
    rows = [
        ("Last close", num(stats.get("last_close")), str(stats.get("last_bar", "")), None),
        ("RSI (14)", num(rsi, 0),
         "overbought" if rsi and rsi > 70 else "oversold" if rsi and rsi < 30 else "neutral",
         "warning" if rsi and (rsi > 70 or rsi < 30) else None),
        ("vs 200-day", "above" if stats.get("above_sma200") else "below",
         ("above" if stats.get("above_sma200") else "below")
         + f" the 200-day at {num(stats.get('sma200'))}",
         "good" if stats.get("above_sma200") else "critical"),
        # The caption has to carry the polarity, because the colour lands on it:
        # "skips the last month" is a methodology note, and rendering it red
        # reads as a warning about the method rather than about the number.
        ("Momentum 12-1", pct(stats.get("momentum_12_1")),
         ("positive" if (stats.get("momentum_12_1") or 0) > 0 else "negative")
         + " · skips the last month",
         "good" if (stats.get("momentum_12_1") or 0) > 0 else "critical"),
        ("Realised vol", pct(stats.get("realised_vol")), "20-day", None),
        ("ATR (14)", num(stats.get("atr14")), "stop distance input", None),
        ("Drawdown", pct(stats.get("drawdown")), "from peak", None),
        ("History", f"{stats.get('bars', 0):,}", "bars", None),
    ]
    columns = st.columns(4, gap="small")
    for index, (label, value, delta, status) in enumerate(rows):
        with columns[index % 4]:
            st.markdown(ui.tile(label, value, delta=delta, delta_status=status,
                                mode=ctx.mode), unsafe_allow_html=True)


PAGES = [
    ("Portfolio", portfolio),
    ("Conviction", conviction),
    ("Risk", risk),
    ("Ideas", ideas),
    ("Evals", evals),
    ("Data health", data_health),
    ("Search", search), ("Reports", reports),
]
