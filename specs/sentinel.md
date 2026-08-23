# Sentinel — personal investment research copilot

**Status:** Phases 0–6 implemented and tested.

> Research output, not financial advice. All decisions and risk are the owner's.

This spec is the contract, in the sense CLAUDE.md means: build against it, and if reality diverges,
update this file in the same commit. It records what was built, the decisions that shaped it, and —
at the end — exactly what was not.

## 1. What it is

A UK retail investor's research copilot for the **satellite** 10–20% of a core/satellite portfolio.
It aggregates prices, fundamentals, news and sentiment; scores each candidate through five modules;
puts every resulting idea through a hard-coded risk layer; and produces a daily brief of at most
three candidates, each with a thesis, an invalidation condition and a position size.

**It never places an order.** Paper trading through a simulated ledger is the whole of its execution
surface, and a mandatory six-month paper period gates any real-money use of short-term signals.

## 2. The rules that are not negotiable

These are enforced mechanically. Where a rule is enforced in exactly one place, that place is named,
and per CLAUDE.md's enforcement-mechanism rule, **a commit that moves it updates this section in the
same commit.**

| # | Rule | Enforced by |
|---|---|---|
| 1 | Deterministic logic is never delegated to an LLM | `analysis/fundamental.py`, `analysis/technical.py`, `risk/`, `backtest/` contain no LLM calls; `risk/engine.py` imports nothing from `analysis/` or `llm/` |
| 2 | The LLM proposes, the rules dispose | `analysis/rules.py::vet` — R1–R8, run on every memo before it can become an idea |
| 3 | Sentiment is never a primary buy reason | Twice: `analysis/sentiment.py::to_signal` (a crowded name's positive tone is scored *down*) and `rules.R1` (a memo with no deterministic module above neutral is rejected) |
| 4 | All money is `Decimal`, GBP base, explicit FX | `money.Money` refuses cross-currency arithmetic; every price column in SQLite is `TEXT`, never `REAL` |
| 5 | A signal from bad data is a Sev-1 | `data/quality.py` — a `CRITICAL` issue means the ticker is **not scored at all**, and `RiskCheckId.DATA_FRESHNESS` blocks it again at the risk layer |
| 6 | Every brief carries the disclaimer | `brief/render.py`, emitted by the renderer so no path produces a readable brief without it |
| 7 | Ideas are immutable | `storage/db.py` — `BEFORE UPDATE`/`BEFORE DELETE` triggers on `ideas` and `audit` that `RAISE(ABORT)` |
| 8 | The daily brief never pushes | `notify/router.py::push_event` — a closed five-event allow-list; anything else raises `PushNotAllowed` |
| 9 | Risk limits cannot be overridden by any signal | `risk/engine.py` never reads `conviction` or `composite_score`; two tests assert a low- and a high-conviction idea get identical share counts |

## 3. Risk limits (Phase 3)

All fractions are of **satellite capital**, never total net worth.

| Limit | Default | Notes |
|---|---|---|
| Max single position | 10% | Caps the risk-based size, so real risk taken can be *below* the 1% budget but never above |
| Max sector concentration | 30% | An unmapped ticker falls into one shared `unknown` bucket — it concentrates rather than escapes |
| Risk per trade | 1% | `shares = (satellite × 1%) / (entry − stop)`, **rounded down**, with FX applied to the stop distance |
| Short-term sub-allocation | 25% | Short-term ideas are capped well below long-term; that is where retail losses concentrate |
| Drawdown kill switch | 15% | From the **high-water mark**. Halts new short-term ideas only; a long-term thesis is not invalidated by the portfolio being down |
| Minimum position | £250 | Below this, flat commission eats the edge |
| Max open positions | 12 | |

M3's bar — 100% branch coverage of the risk layer — is enforced in CI (`.github/workflows/ci.yml`), not
merely reported.

## 4. Scoring

Every module emits **0–100, where 50 is neutral**, with `confidence` and traceable `Evidence`.
The composite is a confidence-weighted mean over fundamental 0.40 / technical 0.30 / news 0.20 /
sentiment 0.10, renormalised over whichever modules ran.

**Missing data is never scored as 50.** A component with no inputs is dropped and confidence falls to
the weight actually available. §5.3 asks whether high-conviction ideas outperform low-conviction
ones; that question is meaningless if "we could not see" reads identically to "we looked and it was
average". Piotroski reports its denominator for the same reason — 5 of 9 and 5 of 5 are different
claims.

Indicators are implemented in-house (`analysis/indicators.py`) rather than taken from `pandas-ta` or
`ta-lib`: those libraries disagree on smoothing conventions, and §5.2 requires *exact* expected
scores, which cannot be pinned to a convention a dependency may change in a point release. Every
convention is stated in its function's docstring and asserted by a hand-computed golden test.

## 5. Deviations from the original project spec

Three, each deliberate.

**`temperature: 0` is not sent to current models, because the API rejects it.** Sampling parameters
were removed from the Claude 4.6+ and 5-series; sending `temperature` returns a 400. Determinism
there comes from the constrained decode of `output_config.format` plus a low effort setting.
`llm/client.py::accepts_sampling` still sends `temperature=0` to models that accept it, so the rule
holds wherever the API allows. §5.2's inter-run consistency eval measures what the pipeline actually
produces rather than assuming a knob worked — which is the right way round in any case.

**`vectorbt`/`backtrader` were not used.** The backtester is ~200 lines in `backtest/engine.py`,
because it has to run the *live* `RiskEngine` and the *live* `Ledger` — a third-party engine with its
own position sizing would be backtesting a strategy we are not allowed to run, and a separate set of
books would make any backtest-vs-paper discrepancy ambiguous.

**A long-term idea's stop is a sizing input, not a hard exit.** The spec requires a stop on
short-term ideas and a written invalidation on long-term ones. Both are enforced
(`HAS_STOP`, `HAS_INVALIDATION`), but the sizing formula needs a stop distance for *any* position, so
a long-term idea is sized off an ATR-derived level while the invalidation condition is what actually
ends it.

## 6. Data sources

Every adapter is **dormant without its key** — no crash, and `sentinel health` says which are asleep.
The `fixture` provider generates every price, fundamental and news item from a hash of the ticker, so
the whole pipeline and the entire test suite run offline with an empty `.env`.

| Kind | Adapter | Notes |
|---|---|---|
| Prices | `eodhd` | Chosen for LSE coverage, the binding constraint for a UK investor |
| Fundamentals | `fmp`, `eodhd` | FMP's `as_of` is the **filing date**, not the period end — the period end is lookahead |
| News | `finnhub` | Company tagging is the point; an untagged headline scored against a ticker is a fabricated input |
| Everything | `fixture` | Deterministic, offline, and never a justification for a strategy |

## 6a. The dashboard (Phase 6)

Read-only, five pages, launched with `sentinel dashboard`. It reads the same SQLite database the
CLI writes and **cannot write to it**: the connection is opened through a `file:…?mode=ro` URI, so
a write is refused by the database engine rather than by anyone remembering. A test asserts the
refusal.

**Auth is fail-closed.** With no `SENTINEL_DASHBOARD_PASSWORD` the dashboard refuses to serve
unless something has explicitly marked the session local — `sentinel dashboard` sets that flag only
when it binds to a loopback address. A container, a VPS or Streamlit Community Cloud will not have
it. The alternative (default open, warn in the UI) fails in the one direction that matters: a
banner nobody reads is not an access control.

**A tunnel revokes the local inference (`cli.serves_as_local`).** The flag above is sound only
while loopback means "reachable from this machine and nowhere else", and cloudflared, tailscale
funnel and ngrok all connect to a loopback origin and republish it publicly. So
`cloudflared --url http://localhost:8501` in front of a default `sentinel dashboard` would serve
the entire portfolio, password-free, to anyone with the URL — under a notice reading "Running
locally". `sentinel dashboard --tunnel` is how the operator states the premise no longer holds: it
keeps the loopback bind a tunnel needs, but stops that bind implying safety, so a password becomes
mandatory and the launcher refuses without one. `deploy/sentinel-tunnel.sh` checks again before
starting either process, since a systemd unit that fails at the second has already opened the
first. Generalise: an access control that infers safety from a network fact is only as good as the
fact, and tunnels are built to falsify this one.

**"Accepted" means the rules layer AND the risk layer, and it is read from the audit trail.**
`score_universe` persists an idea BEFORE `assess()` runs, and `assess` returns its verdicts to the
caller without writing them back — ideas are append-only, so it could not update them anyway.
`Idea.risk` is therefore always `None` on anything read back from the database, which makes
`Idea.accepted` always False and unusable as "did this clear the risk layer". The surviving record
is the audit trail: `assess` writes RISK_APPROVED or RISK_CHECK_FAILED against the idea id for every
idea it evaluates, and `queries.risk_outcomes` reads that back. Before this, the Ideas table defined
accepted as `not rejected_by_rules` — on the demo pool that reported **21 of 28** ideas accepted when
only **14** had cleared both layers, overstating by 7 ideas the risk layer had refused. The layer the
spec says nothing may override was the one the table ignored.

**The search page reports decisions; it does not make them.** `queries.verdict_for` applies no
threshold of its own — BUY / HOLD / AVOID / NOT SCORED are each derived from a decision some other
layer already recorded. A ticker the pipeline has not scored returns NOT SCORED rather than a guess.
This is what stops the page becoming a second opinion that can disagree with the brief about the
same ticker, and it is why a low composite that cleared both layers still reads BUY.

**No statistics are re-derived in the dashboard.** Hit rates, calibration and Brier come from
`evals/` — the same code the CLI and the weekly review use. A dashboard that computed its own
version of a hit rate would eventually disagree with the eval that gates real money, and the wrong
one would be the one on screen.

### Colour

Every categorical slot and ordinal ramp in `dashboard/palette.py` was run through the data-viz
validator **before** any chart code was written, in both modes against the surface each actually
renders on. Results are recorded in that module's docstring. Rules that hold throughout:

* One y-axis, ever. Benchmarks share an axis by being indexed to a common starting capital.
* Hues follow the entity, not its rank — filtering a series out cannot repaint the survivors.
* Ordered categories (conviction, materiality) take the ordinal ramp, not categorical hues.
* Severity takes the reserved status palette, always with an icon and a word beside it.
* Endpoint labels only, a legend for two or more series, and a table twin for every chart —
  the table twin is also what discharges the light-mode contrast warning on two slots.

Light and dark are two *selected* palettes, not an inversion. The dashboard follows Streamlit's own
theme setting rather than running a second switcher beside it, so the charts, the page chrome and
Streamlit's own widgets always agree.

### Fabricated data is labelled as such

`scripts/seed_demo.py` writes a populated database for demonstrating the dashboard. Everything it
writes is invented. It stamps `schema_meta.demo_data = true` and the dashboard renders a banner on
any database carrying that stamp, because a fabricated track record mistaken for a real one is the
most damaging thing this repository could produce.

## 7. Not built yet

Recorded so the gaps are known rather than discovered.

- **Live vendor calls are unverified.** The adapters' *parsing* is unit-tested against recorded
  payload shapes, and that is the half that harbours bugs — but no request has been made to EODHD,
  FMP or Finnhub, so endpoint paths and auth are `UNVERIFIED`. `sentinel health` is where that is
  found out.
- **No live LLM call has been made.** The client is exercised end to end against a stub SDK
  (including the repair turn and the no-`temperature` behaviour), but there is no
  `ANTHROPIC_API_KEY` in this environment.
- **The dashboard is read-only and has no filters that persist.** Each page filters its own
  view in-session; nothing is saved.
- **Alpaca paper API.** The internal simulated ledger covers both UK and US names; the Alpaca
  integration the spec mentions for US names is not wired.
- **Reddit / StockTwits sentiment.** The sentiment module accepts arbitrary text via `extra_texts`,
  but only news headlines are currently fed to it.

## 7a. Scheduling

`deploy/` carries the scheduled runner: `sentinel-daily.sh` plus a systemd unit and timer, with a
crontab example for machines without systemd. It runs **weekdays at 07:00 Europe/London** —
weekends are excluded because EOD vendors publish nothing then, so a weekend run re-scores Friday
and mails a brief already read.

The script exists because the failure modes of a scheduled job are all *quiet* ones:

* **A dead pipeline sends nothing**, and a morning with no brief is indistinguishable from a quiet
  morning with no candidates — so a failure pushes a `PIPELINE_FAILURE` alert through the router
  (`sentinel notify failure`), which keeps it on the audit trail and inside the push allow-list
  rather than curling ntfy directly.
* **Two runs writing one SQLite file** corrupts the audit trail, so the script takes a
  non-blocking `flock`; a run that finds the lock held exits rather than queueing behind it.
* **Exit 2 is not flattened into success.** The brief went out and carries its own stale-data
  banner, but a monitor seeing `0` would never learn the run was incomplete.

`Timezone=Europe/London` on the timer is why systemd is preferred over crontab: 07:00 stays 07:00
through both clock changes with nothing to edit, and `Persistent=true` catches up after downtime.

The **weekly review** (`sentinel weekly`) runs Sunday 18:00 on the same lock, and reports
performance, the benchmark comparison, every eval that can return a verdict, the §5.5 kill criteria,
and a mandatory faults section. Its exit code 2 means *a kill criterion has been met*, which pushes
an alert rather than waiting to be read.

**A gap this closed, found by running it rather than by any test:** `KillCriteria.verdicts()` printed
*nothing at all* once the paper period had elapsed but the four comparison inputs were unavailable —
silence that reads exactly like "the gate passed" while meaning it was never evaluated. It now says
so explicitly and names the missing inputs, and the weekly review computes the strategy and benchmark
Sharpe itself so the gate can usually be evaluated for real.

The runners' exit-code routing, locking and PATH repair are covered by `tests/test_deploy.py`
against a fake `uv`, and both scripts have been run end-to-end against a real database.

## 8. Verification

- 337 tests, offline, no vendor key required.
- Risk layer at **100% statement and branch coverage**, enforced in CI.
- End-to-end run verified against a live SQLite database: `init` → `ingest` (2,571 bars over 10
  years) → `health` → `brief` → `paper status` → `backtest` → `evals` → `notify test`.
- That live run is what found the `config.satellite_capital` / `satellite_capital_gbp` mismatch in
  `pipeline.portfolio_state`, which every unit test had missed. `.github/workflows/ci.yml` now runs the same
  end-to-end sequence for that reason.
