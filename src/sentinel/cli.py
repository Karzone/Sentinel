"""The command line.

Commands are thin: each one loads config, opens the database, calls into the
library and prints. Nothing here contains logic that the library does not,
because a rule enforced only in the CLI is a rule that a cron job calling the
library can walk past.

Exit codes carry meaning, since these run under cron: 0 success, 1 a real
failure, 2 a data-quality block. A monitoring script should be able to tell
"the brief did not generate" from "the brief generated and said the data is
bad".
"""

from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import DISCLAIMER, __version__
from .config import Config, ENV_EXAMPLE, STARTER_CONFIG, api_key, load_config
from .logging_setup import configure, get_logger

app = typer.Typer(
    add_completion=False,
    help="Sentinel — personal investment research copilot. "
         "Research output, not financial advice.",
)
paper_app = typer.Typer(help="Paper-trading account.")
notify_app = typer.Typer(help="Notification channels.")
app.add_typer(paper_app, name="paper")
app.add_typer(notify_app, name="notify")

console = Console()
log = get_logger("cli")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_DATA_BLOCKED = 2


def _config(path: Optional[str] = None) -> Config:
    configure()
    return load_config(path)


def _db(config: Config, *, create: bool = False):
    from .storage import connect, migrate

    conn = connect(config.paths.db, create=create)
    if create:
        migrate(conn)
    return conn


def _llm(config: Config):
    from .llm.client import AnthropicClient

    return AnthropicClient(config.llm)


def _tickers(config: Config, universe: Optional[str], tickers: Optional[str]) -> list[str]:
    if tickers:
        return [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if universe:
        return list(config.universe(universe))
    if config.watchlist:
        return list(config.watchlist)
    if config.universes:
        return list(next(iter(config.universes.values())))
    raise typer.BadParameter("no universe, watchlist or --tickers given")


# ---------------------------------------------------------------- init


@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing sentinel.toml."),
) -> None:
    """Write a starter config and create the database."""
    config_path = Path("sentinel.toml")
    if config_path.exists() and not force:
        console.print("[yellow]sentinel.toml already exists — use --force to overwrite.[/]")
    else:
        config_path.write_text(STARTER_CONFIG, encoding="utf-8")
        console.print(f"[green]wrote[/] {config_path}")

    env_path = Path(".env.example")
    if not env_path.exists():
        env_path.write_text(ENV_EXAMPLE, encoding="utf-8")
        console.print(f"[green]wrote[/] {env_path}")

    config = load_config(config_path)
    conn = _db(config, create=True)
    conn.close()
    console.print(f"[green]created[/] {config.paths.db}")
    console.print(f"\n[dim]{DISCLAIMER}[/]")


# ---------------------------------------------------------------- ingest / health


@app.command()
def ingest(
    universe: Optional[str] = typer.Option(None, "--universe", "-u"),
    tickers: Optional[str] = typer.Option(None, "--tickers", "-t", help="Comma-separated."),
    history: int = typer.Option(800, "--history", help="Days of price history to fetch."),
    config_path: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Fetch prices, fundamentals and news, then run the quality checks."""
    config = _config(config_path)
    conn = _db(config, create=True)
    from .data import ingest as ingest_mod

    names = _tickers(config, universe, tickers)
    result = ingest_mod.ingest(conn, config, names, history_days=history)
    console.print(result.summary())
    for issue in result.report.critical:
        console.print(f"[red]CRITICAL[/] {issue.ticker}: {issue.detail}")
    for issue in result.report.warnings[:10]:
        console.print(f"[yellow]warn[/] {issue.ticker}: {issue.detail}")
    conn.close()
    raise typer.Exit(EXIT_DATA_BLOCKED if result.report.blocking else EXIT_OK)


@app.command()
def health(config_path: Optional[str] = typer.Option(None, "--config")) -> None:
    """Vendor configuration, data freshness and recent quality issues."""
    config = _config(config_path)
    from .data import registry
    from .storage import repo

    table = Table(title="Vendors")
    table.add_column("kind")
    table.add_column("provider")
    table.add_column("status")
    for row in registry.describe(config):
        status = "[green]ready[/]" if row["available"] else "[yellow]dormant (no key)[/]"
        table.add_row(str(row["kind"]), str(row["provider"]), status)
    console.print(table)

    console.print(
        f"LLM: {config.llm.model} — "
        + ("[green]ready[/]" if api_key('ANTHROPIC_API_KEY') else "[yellow]dormant (no key)[/]")
    )

    if not Path(config.paths.db).exists():
        console.print("[yellow]no database yet — run `sentinel init`[/]")
        raise typer.Exit(EXIT_OK)

    conn = _db(config)
    freshness = Table(title="Data freshness")
    freshness.add_column("ticker")
    freshness.add_column("last bar")
    freshness.add_column("fundamentals")
    today = dt.date.today()
    stale = 0
    for ticker in repo.tickers_with_bars(conn):
        last = repo.latest_bar_date(conn, ticker)
        age = (today - last).days if last else 999
        if age > 4:
            stale += 1
        marker = "[red]" if age > 4 else ""
        freshness.add_row(
            ticker, f"{marker}{last} ({age}d)" if last else "[red]none[/]",
            str(repo.latest_fundamentals_date(conn, ticker) or "—"),
        )
    console.print(freshness)

    issues = repo.get_quality_issues(conn, limit=20)
    if issues:
        console.print("\n[bold]Recent quality issues[/]")
        for issue in issues[:10]:
            colour = {"critical": "red", "warn": "yellow"}.get(issue.severity.value, "dim")
            console.print(f"[{colour}]{issue.severity.value}[/] {issue.ticker}: {issue.detail}")

    stats = repo.llm_schema_compliance(conn)
    if stats["calls"]:
        from .evals.signal_quality import schema_compliance_verdict

        console.print(f"\nLLM: {schema_compliance_verdict(stats)}")
    conn.close()
    raise typer.Exit(EXIT_DATA_BLOCKED if stale else EXIT_OK)


# ---------------------------------------------------------------- idea / brief


@app.command()
def idea(
    ticker: str = typer.Argument(..., help="e.g. VOD.LSE"),
    config_path: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Run every module for one ticker and print the memo and risk verdict."""
    config = _config(config_path)
    conn = _db(config)
    from . import pipeline
    from .brief.render import _idea_block

    as_of = dt.date.today()
    result = pipeline.score_ticker(conn, config, ticker.upper(), as_of, llm=_llm(config))
    if result.skipped:
        console.print(f"[red]not scored[/] — {result.skipped}")
        conn.close()
        raise typer.Exit(EXIT_DATA_BLOCKED)
    if result.idea is None:
        console.print("[yellow]no idea produced[/]")
        conn.close()
        raise typer.Exit(EXIT_OK)

    verdicts = pipeline.assess(conn, config, [result.idea], as_of=as_of)
    console.print("\n".join(_idea_block(result.idea, verdicts[0][1] if verdicts else None)))
    if result.idea.rejected_by_rules:
        console.print("[yellow]Rejected by the rules layer:[/]")
        for reason in result.idea.rejected_by_rules:
            console.print(f"  - {reason}")
    if verdicts and not verdicts[0][1].approved:
        console.print("[yellow]Rejected by the risk layer:[/]")
        for reason in verdicts[0][1].failure_reasons:
            console.print(f"  - {reason}")
    if result.llm_error:
        console.print(f"[red]LLM error:[/] {result.llm_error}")
    console.print(f"\n[dim]{DISCLAIMER}[/]")
    conn.close()


@app.command()
def brief(
    universe: Optional[str] = typer.Option(None, "--universe", "-u"),
    tickers: Optional[str] = typer.Option(None, "--tickers", "-t"),
    send: bool = typer.Option(False, "--send", help="Email the digest and push any events."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Also write to this path."),
    config_path: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Generate today's brief."""
    config = _config(config_path)
    conn = _db(config)
    from . import pipeline
    from .brief import build, subject_line, to_html, to_markdown
    from .risk import RiskEngine
    from .storage import repo

    as_of = dt.date.today()
    names = _tickers(config, universe, tickers)
    run = pipeline.run(conn, config, names, as_of=as_of, llm=_llm(config))
    state = pipeline.portfolio_state(conn, config, as_of=as_of)
    engine = RiskEngine(config.risk, sectors=config.sectors)
    blocked = run.report.blocked_tickers() if run.report else set()
    verdicts = pipeline.assess(conn, config, run.accepted, as_of=as_of, state=state, blocked=blocked)

    document = build(
        as_of=as_of, ideas=run.ideas, verdicts=verdicts, state=state, engine=engine,
        issues=run.report.issues if run.report else (),
    )
    markdown = to_markdown(document, verdicts=verdicts)
    repo.save_brief(conn, document, markdown)

    console.print(markdown)
    target = Path(output) if output else Path(config.paths.briefs) / f"{as_of.isoformat()}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    console.print(f"[green]written[/] {target}")

    if send:
        from .notify import build_router, events_from_brief

        router = build_router(config, conn=conn)
        result = router.send_digest(
            subject=subject_line(document), body=markdown,
            html=to_html(document, markdown),
        )
        console.print(f"digest → {result.channel}: {'sent' if result.delivered else result.detail}")
        for event, subject, body in events_from_brief(document, positions=state.open_positions):
            pushed = router.push_event(event, subject=subject, body=body)
            console.print(f"push {event.value} → {'sent' if pushed.delivered else pushed.detail}")

    conn.close()
    raise typer.Exit(EXIT_DATA_BLOCKED if document.stale else EXIT_OK)


# ---------------------------------------------------------------- paper


@paper_app.command("status")
def paper_status(config_path: Optional[str] = typer.Option(None, "--config")) -> None:
    """Open positions, distance to stop, and drawdown from the high-water mark."""
    config = _config(config_path)
    conn = _db(config)
    from . import pipeline
    from .risk import RiskEngine, sector_allocation

    state = pipeline.portfolio_state(conn, config, as_of=dt.date.today())
    engine = RiskEngine(config.risk, sectors=config.sectors)

    console.print(
        f"NAV £{state.nav:,.2f} · cash £{state.cash:,.2f} · "
        f"drawdown {state.drawdown():.1%} of a £{state.high_water_mark:,.2f} high-water mark"
    )
    if engine.kill_switch_active(state):
        console.print(f"[red]{engine.review_required(state)}[/]")

    table = Table(title="Open positions")
    for column in ("ticker", "class", "sector", "shares", "entry", "mark", "move", "to stop"):
        table.add_column(column)
    for position in state.open_positions:
        mark = state.marks.get(position.ticker, position.entry)
        move = (mark / position.entry - 1) if position.entry else Decimal("0")
        to_stop = (
            f"{((mark - position.stop) / mark):.1%}"
            if position.stop is not None and mark > 0 else "—"
        )
        table.add_row(
            position.ticker, position.idea_class.value, position.sector, str(position.shares),
            str(position.entry), str(mark), f"{move:+.1%}", to_stop,
        )
    console.print(table)

    allocation = sector_allocation(state)
    if allocation:
        cap = config.risk.max_sector_pct / Decimal("100")
        console.print("\n[bold]Sector allocation[/] (cap " + f"{cap:.0%})")
        for sector, weight in allocation.items():
            flag = " [red]← at the limit[/]" if weight >= cap else ""
            console.print(f"  {sector}: {weight:.1%}{flag}")
    conn.close()


# ---------------------------------------------------------------- backtest


@app.command()
def backtest(
    universe: Optional[str] = typer.Option(None, "--universe", "-u"),
    folds: int = typer.Option(3, "--folds"),
    monte_carlo: int = typer.Option(500, "--monte-carlo", help="Random portfolios for B4."),
    config_path: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Walk-forward backtest with UK costs, against B1-B4."""
    config = _config(config_path)
    conn = _db(config)
    from .analysis import technical
    from .backtest import (
        BacktestConfig, benchmarks, random_portfolios, round_trip_drag, run_walk_forward,
    )
    from .costs import CostModel
    from .evals.metrics import summarise
    from .storage import repo

    names = _tickers(config, universe, None)
    bars = {t: repo.get_bars(conn, t) for t in names}
    bars = {t: series for t, series in bars.items() if len(series) > 300}
    if not bars:
        console.print("[red]not enough price history — run `sentinel ingest` first[/]")
        conn.close()
        raise typer.Exit(EXIT_FAILURE)

    def factory(train_bars, start, end):
        def ranker(date, history):
            scored = []
            for ticker, series in history.items():
                try:
                    scored.append((ticker, technical.score(series).score))
                except technical.InsufficientHistory:
                    continue
            return sorted(scored, key=lambda pair: -pair[1])
        return ranker

    costs = CostModel()
    # Fit the folds to the history that actually exists rather than refusing.
    # A 3-year sample cannot support the 2-year-train/1-year-test default, and
    # "not enough history" is a less useful answer than a smaller, honestly
    # labelled test.
    available = max(len(series) for series in bars.values())
    per_fold = max(120, available // (folds + 1))
    train_periods = int(per_fold * 2 / 3)
    test_periods = per_fold - train_periods
    console.print(
        f"[dim]{available} bars available → {folds} fold(s) of {train_periods} train / "
        f"{test_periods} test[/]"
    )

    result = run_walk_forward(
        bars, factory, folds=folds, train_periods=train_periods, test_periods=test_periods,
        config=BacktestConfig(starting_cash=config.satellite_capital_gbp,
                              warmup_bars=min(250, train_periods - 1)),
        limits=config.risk, sectors=config.sectors, costs=costs,
    )
    if not result.returns:
        console.print(
            f"[red]not enough history: {available} bars cannot support {folds} walk-forward "
            f"fold(s). Ingest more history or ask for fewer folds.[/]"
        )
        conn.close()
        raise typer.Exit(EXIT_FAILURE)

    summary = summarise(result.returns, result.equity, trades=result.trades,
                        exposures=result.exposures)
    console.print(f"\n[bold]Out-of-sample across {result.completed_folds} folds[/]")
    console.print(summary.headline())
    console.print(
        f"trades {summary.trades.trades} · win rate "
        f"{(summary.trades.win_rate or 0):.0%} · average exposure "
        f"{(summary.average_exposure or 0):.0%}"
    )
    console.print(
        f"round-trip cost on £1,000: "
        f"{round_trip_drag(costs, ticker='X.LSE', notional_gbp=Decimal('1000')):.2%}"
    )

    strategy_return = Decimal(str(summary.total_return))
    console.print("\n[bold]Benchmarks[/] (all GBP, total return, net of the same costs)")

    for key, label in (("B1", "global index"), ("B2", "S&P 500")):
        symbol = config.benchmarks.get(key)
        if not symbol or symbol in ("CASH", "RANDOM"):
            continue
        series = repo.get_bars(conn, symbol)
        if not series:
            console.print(
                f"  {key} {symbol}: [yellow]not ingested — run "
                f"`sentinel ingest --tickers {symbol}` to enable the {label} comparison[/]"
            )
            continue
        held = benchmarks.buy_and_hold(series, name=key, label=symbol)
        verdict = "ahead" if strategy_return > held.total_return else "BEHIND"
        console.print(f"  {key} {symbol}: {held.total_return:+.1%} — strategy is {verdict}")

    cash_series = benchmarks.cash(len(result.returns) + 1)
    cash_verdict = "ahead" if strategy_return > cash_series.total_return else "BEHIND"
    console.print(f"  B3 cash: {cash_series.total_return:+.1%} — strategy is {cash_verdict}")

    # B4 needs a universe wider than one portfolio, or "random" means nothing.
    holdings = min(6, max(2, len(bars) // 2))
    if len(bars) < 4:
        console.print(
            f"  B4: [yellow]skipped — {len(bars)} tickers with usable history is too few to "
            f"draw random portfolios from. B4 needs at least 4.[/]"
        )
    else:
        mc = random_portfolios(bars, portfolios=monte_carlo, holdings=holdings, costs=costs)
        mc = benchmarks.place_strategy(mc, strategy_return,
                                       strategy_exposure=summary.average_exposure)
        console.print(f"  B4 ({holdings} of {len(bars)} names): {mc.verdict()}")
        console.print(f"     percentiles: "
                      + ", ".join(f"p{k} {v:+.1%}" for k, v in sorted(mc.percentiles.items())))
    console.print(f"\n[dim]{DISCLAIMER}[/]")
    conn.close()


# ---------------------------------------------------------------- evals


@app.command()
def evals(
    days: int = typer.Option(180, "--days"),
    as_json: bool = typer.Option(False, "--json"),
    config_path: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Signal-quality, calibration and schema-compliance evals."""
    config = _config(config_path)
    conn = _db(config)
    from .evals.signal_quality import schema_compliance_verdict
    from .storage import repo

    since = dt.date.today() - dt.timedelta(days=days)
    ideas = repo.get_ideas(conn, since=since, limit=2000)
    rejected = [i for i in ideas if i.rejected_by_rules]

    report: dict[str, object] = {
        "window_days": days,
        "ideas": len(ideas),
        "accepted": len(ideas) - len(rejected),
        "rejection_rate": (len(rejected) / len(ideas)) if ideas else None,
        "schema_compliance": schema_compliance_verdict(repo.llm_schema_compliance(conn)),
    }

    reasons: dict[str, int] = {}
    for item in rejected:
        for reason in item.rejected_by_rules:
            rule = reason.split(":")[0]
            reasons[rule] = reasons.get(rule, 0) + 1
    report["rejections_by_rule"] = reasons

    trades = repo.get_all_positions(conn)
    closed = [p for p in trades if not p.is_open and p.exit_price is not None]
    report["closed_positions"] = len(closed)
    if len(closed) < 20:
        report["verdict"] = (
            f"{len(closed)} closed positions. The kill criteria need 6 months of paper "
            f"trading and 100 catalyst samples before any verdict is meaningful; there is "
            f"not enough here to conclude anything, and saying so is the correct output."
        )

    if as_json:
        console.print_json(json.dumps(report, default=str))
    else:
        for key, value in report.items():
            console.print(f"[bold]{key}[/]: {value}")
    conn.close()


# ---------------------------------------------------------------- notify


@notify_app.command("test")
def notify_test(config_path: Optional[str] = typer.Option(None, "--config")) -> None:
    """Send one test message down each configured channel."""
    config = _config(config_path)
    from .domain.enums import NotifyEvent
    from .notify import build_router

    router = build_router(config)
    digest = router.send_digest(
        subject="Sentinel — test digest",
        body=f"This is a test of the digest channel.\n\n{DISCLAIMER}",
    )
    console.print(f"digest ({digest.channel}): "
                  + ("[green]sent[/]" if digest.delivered else f"[yellow]{digest.detail}[/]"))

    push = router.push_event(
        NotifyEvent.PIPELINE_FAILURE,
        subject="Sentinel — test push",
        body="This is a test of the event channel. Real pushes mean act or review now.",
    )
    console.print(f"push ({push.channel}): "
                  + ("[green]sent[/]" if push.delivered else f"[yellow]{push.detail}[/]"))
    if config.notify.ntfy_topic:
        console.print(
            f"[dim]ntfy topic is {config.notify.ntfy_topic!r} — anyone who knows it can "
            f"publish to it, so make it long and unguessable.[/]"
        )


@notify_app.command("failure")
def notify_failure(
    message: str = typer.Argument(..., help="What went wrong."),
    config_path: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Push a pipeline-failure alert.

    This exists for the scheduled runner. A cron job whose pipeline dies sends
    nothing, and a morning with no brief looks exactly like a quiet morning with
    no candidates — so the failure has to announce itself on the one channel
    that means "act or review now".

    It goes through the router rather than curling ntfy directly, so the alert is
    recorded in the audit trail and obeys the same allow-list as every other
    event.
    """
    config = _config(config_path)
    from .domain.enums import NotifyEvent
    from .notify import build_router

    router = build_router(config)
    result = router.push_event(
        NotifyEvent.PIPELINE_FAILURE,
        subject="Sentinel — pipeline failure",
        body=f"{message}\n\nNo brief was produced. Check the runner log.",
    )
    console.print(
        f"push ({result.channel}): "
        + ("[green]sent[/]" if result.delivered else f"[yellow]{result.detail}[/]")
    )
    # A failed alert must not mask the failure it was reporting, but it also
    # must not be silent: report it and exit non-zero so the runner logs both.
    raise typer.Exit(EXIT_OK if result.delivered or not result.configured else EXIT_FAILURE)


@app.command()
def dashboard(
    port: int = typer.Option(8501, "--port"),
    address: str = typer.Option("localhost", "--address",
                                help="Bind address. Anything non-loopback requires a password."),
    theme: str = typer.Option("light", "--theme", help="light | dark"),
    config_path: Optional[str] = typer.Option(None, "--config"),
) -> None:
    """Launch the read-only Streamlit dashboard.

    Binding to a loopback address marks the session local, which is the only way
    the dashboard will serve without SENTINEL_DASHBOARD_PASSWORD. Bind anywhere
    else without one and it refuses to render — an unprotected portfolio on a
    network is the failure that policy exists to prevent.
    """
    import subprocess
    import sys

    config = _config(config_path)
    if not Path(config.paths.db).exists():
        console.print("[red]no database yet — run `sentinel init` and `sentinel ingest` first[/]")
        raise typer.Exit(EXIT_FAILURE)

    try:
        import streamlit  # noqa: F401 - probing for the optional extra
    except ImportError:
        console.print(
            "[red]streamlit is not installed.[/] Install the dashboard extra:\n"
            "  uv sync --extra dashboard"
        )
        raise typer.Exit(EXIT_FAILURE) from None

    from .dashboard import auth as dash_auth

    is_local = address in ("localhost", "127.0.0.1", "::1")
    env = dict(os.environ)
    env["SENTINEL_DASHBOARD_THEME"] = theme
    # Streamlit's ProgressColumn and widgets paint with primaryColor, which
    # defaults to red. On "distance to stop" a long red bar reads as danger when
    # a long bar is in fact the safe case, so it takes the palette's slot 1.
    env.setdefault("STREAMLIT_THEME_PRIMARY_COLOR", "#2a78d6")
    env.setdefault("STREAMLIT_THEME_BASE", "dark" if theme == "dark" else "light")
    env["SENTINEL_DB"] = str(config.paths.db)
    if config.source_path:
        env["SENTINEL_CONFIG"] = str(config.source_path)
    if is_local:
        env[dash_auth.LOCAL_ENV] = "1"
    else:
        env.pop(dash_auth.LOCAL_ENV, None)
        if not env.get(dash_auth.PASSWORD_ENV):
            console.print(
                f"[red]refusing to bind {address} without a password.[/] "
                f"Set {dash_auth.PASSWORD_ENV} and try again."
            )
            raise typer.Exit(EXIT_FAILURE)

    script = Path(__file__).parent / "dashboard" / "run.py"
    console.print(f"[green]starting[/] http://{address}:{port}")
    if not env.get(dash_auth.PASSWORD_ENV):
        console.print(
            f"[yellow]no {dash_auth.PASSWORD_ENV} set — local session only.[/]"
        )
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(script),
         "--server.port", str(port), "--server.address", address,
         "--server.headless", "true", "--browser.gatherUsageStats", "false"],
        env=env, check=False,
    )


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"sentinel {__version__}")
    console.print(f"[dim]{DISCLAIMER}[/]")


if __name__ == "__main__":
    app()
