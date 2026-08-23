"""Fabricate a populated Sentinel database, for demonstrating the dashboard.

**Everything this writes is invented.** Prices come from the fixture provider (a
hash of the ticker), the LLM outputs are scripted, and the paper track record is
a simulation over that fake series. It is not a backtest, it is not a result, and
no number it produces says anything about any strategy.

Two guards, because a fabricated track record that gets mistaken for a real one
is the most damaging thing in this repository:

* the output path must be named explicitly — there is no default, so this cannot
  quietly overwrite the database you actually use;
* it stamps ``schema_meta.demo_data = true``, and the dashboard renders a banner
  on any database carrying that stamp.

    uv run python scripts/seed_demo.py --db /tmp/demo.sqlite
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import random
from decimal import Decimal
from pathlib import Path

from sentinel.analysis import synthesis, technical
from sentinel.costs import CostModel
from sentinel.config import Config, STARTER_CONFIG, _decimalise
from sentinel.data import ingest as ingest_mod
from sentinel.domain.enums import IdeaClass, PositionStatus
from sentinel.llm.fake import CallableClient
from sentinel.portfolio import Ledger
from sentinel.storage import init_db, repo

UNIVERSE = ["DEMO1.LSE", "DEMO2.LSE", "DEMO3.US", "DEMO4.US"]
BENCHMARKS = ["VWRP.LSE", "SPY.US"]

DIRECTIONS = ["long", "long", "flat", "avoid"]
CATALYSTS = ["earnings", "guidance", "product", "regulatory"]
CONVICTIONS = ["low", "medium", "medium", "high"]


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def scripted_llm() -> CallableClient:
    """Deterministic, varied-by-prompt LLM output, so the eval pages have spread."""

    def respond(module: str, prompt: str) -> dict:
        rng = random.Random(_seed(module, prompt[:200]))
        if module == "news":
            return {
                "catalyst_type": CATALYSTS[rng.randrange(len(CATALYSTS))],
                "direction": DIRECTIONS[rng.randrange(len(DIRECTIONS))],
                "materiality": rng.randint(1, 5),
                "horizon_days": rng.choice([14, 30, 60]),
                "summary": "Fabricated demonstration catalyst.",
                "headline_refs": ["Demo headline"],
            }
        if module == "sentiment":
            return {
                "sentiment": rng.randint(-2, 2),
                "conviction": round(rng.uniform(0.4, 0.95), 2),
                "herding_risk": rng.random() < 0.25,
                "rationale": "Fabricated demonstration tone.",
                "sample_size": rng.randint(4, 40),
            }
        conviction = CONVICTIONS[rng.randrange(len(CONVICTIONS))]
        return {
            "thesis": "Margins are recovering faster than the market has priced. "
                      "Cash generation covers the debt. This is demonstration data.",
            "bull_case": "Operating leverage as volumes return, with pricing power intact.",
            "bear_case": "Input costs spike again and the recovery stalls into next year.",
            "invalidation": f"Operating margin falls below {rng.randint(8, 15)}% "
                            f"in the FY26 interim results.",
            "idea_class": "long_term",
            "conviction": conviction,
            "horizon_days": rng.choice([270, 365, 540]),
            "claims": ["trend", "momentum"],
        }

    return CallableClient(respond)


def build_config(db: Path) -> Config:
    import tomllib

    data = _decimalise(tomllib.loads(STARTER_CONFIG))
    data["paths"] = {"db": str(db), "briefs": str(db.parent / "briefs")}
    data["sectors"] = {
        "DEMO1.LSE": "consumer", "DEMO2.LSE": "industrials",
        "DEMO3.US": "technology", "DEMO4.US": "healthcare",
    }
    return Config.model_validate(data)


def simulate_paper(conn, config: Config, *, days: int = 320) -> None:
    """A simulated paper account over the fixture series.

    Uses the real Ledger and the real cost model, so the equity curve is at least
    internally consistent — it is still a simulation over invented prices.
    """
    ledger = Ledger(config.satellite_capital_gbp, costs=CostModel())
    series = {t: repo.get_bars(conn, t) for t in UNIVERSE}
    series = {t: bars for t, bars in series.items() if len(bars) > days + 30}
    if not series:
        return

    dates = sorted({b.date for bars in series.values() for b in bars})[-days:]
    rng = random.Random(20260101)
    opened: dict[str, dt.date] = {}

    for index, date in enumerate(dates):
        marks = {
            t: next((b.adjusted_close for b in reversed(bars) if b.date <= date), None)
            for t, bars in series.items()
        }
        marks = {t: m for t, m in marks.items() if m is not None}

        # Exits first: stops fill at the stop level.
        for position in list(ledger.open_positions):
            mark = marks.get(position.ticker)
            if mark is None or position.stop is None:
                continue
            if mark <= position.stop:
                ledger.close(position.ticker, price=position.stop, date=date,
                             status=PositionStatus.CLOSED_STOP)
            elif (date - position.opened_on).days > 180 and rng.random() < 0.05:
                ledger.close(position.ticker, price=mark, date=date,
                             status=PositionStatus.CLOSED_TARGET)

        # Entries every ~30 sessions, sized by the real risk arithmetic.
        if index % 30 == 0 and len(ledger.open_positions) < 4:
            for ticker, mark in marks.items():
                if any(p.ticker == ticker for p in ledger.open_positions):
                    continue
                bars = [b for b in series[ticker] if b.date <= date]
                stop = technical.atr_stop(bars) or mark * Decimal("0.9")
                if stop >= mark:
                    continue
                risk_budget = config.satellite_capital_gbp * Decimal("0.01")
                shares = int(risk_budget / (mark - stop))
                cap = int(config.satellite_capital_gbp * Decimal("0.10") / mark)
                shares = max(0, min(shares, cap))
                cost = mark * shares
                if shares <= 0 or cost < Decimal("250") or cost + Decimal("20") > ledger.cash:
                    continue
                ideas = repo.get_ideas(conn, ticker=ticker, limit=1)
                ledger.open(
                    ticker=ticker, idea_id=ideas[0].id if ideas else f"demo-{ticker}",
                    idea_class=IdeaClass.LONG_TERM, sector=config.sector_of(ticker),
                    shares=shares, price=mark, date=date, stop=stop,
                    invalidation="Demonstration invalidation condition.",
                )
                opened[ticker] = date
                break

        point = ledger.mark_to_market(date, marks)
        repo.save_equity_point(conn, date, point.nav_gbp, point.cash_gbp, point.high_water_gbp)

    for position in ledger.positions.values():
        pid = repo.save_position(conn, position)
        for fill in [f for f in ledger.fills if f.ticker == position.ticker]:
            repo.save_fill(conn, pid, fill)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="Where to write the demo database. No default, on purpose.")
    parser.add_argument("--history", type=int, default=1200)
    args = parser.parse_args()

    db: Path = args.db
    config = build_config(db)
    conn = init_db(db)

    print(f"ingesting fixture data for {len(UNIVERSE) + len(BENCHMARKS)} tickers…")
    ingest_mod.ingest(conn, config, UNIVERSE + BENCHMARKS, history_days=args.history)

    llm = scripted_llm()
    from sentinel import pipeline

    today = dt.date.today()
    # Ideas dated far enough back that their catalyst horizons have elapsed,
    # so the direction-accuracy eval has something scoreable.
    for offset in (400, 330, 260, 190, 120, 60, 20, 3):
        as_of = today - dt.timedelta(days=offset)
        for ticker in UNIVERSE:
            result = pipeline.score_ticker(conn, config, ticker, as_of, llm=llm)
            if result.idea is not None and repo.get_idea(conn, result.idea.id) is None:
                repo.save_idea(conn, result.idea)
                verdicts = pipeline.assess(conn, config, [result.idea], as_of=as_of)
                del verdicts
    print(f"stored {len(repo.get_ideas(conn, limit=1000))} ideas")

    print("simulating a paper account…")
    simulate_paper(conn, config)

    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('demo_data','true') "
        "ON CONFLICT(key) DO UPDATE SET value='true'"
    )
    print(f"\nwrote {db}")
    print("EVERY NUMBER IN THIS DATABASE IS FABRICATED. It is stamped demo_data=true "
          "and the dashboard will say so.")
    conn.close()


if __name__ == "__main__":
    main()
