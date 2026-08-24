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
**`sentinel phone` is the cross-platform packaging of the same pair** (the owner's machine is
Windows, where a .sh is not a phone mode): it prompts for a password if none is set and refuses one
under 8 characters, starts the dashboard with `--tunnel` on loopback, waits for `/_stcore/health`
before opening the tunnel (origin before tunnel — the ordering that can never publish a 502), opens
a cloudflared quick tunnel and prints the `trycloudflare.com` URL, then supervises both: either
process dying stops the other, and Ctrl+C stops both, via process-group kill (setsid/killpg on
POSIX, CREATE_NEW_PROCESS_GROUP on Windows) so the streamlit grandchild cannot be left serving.

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

- **Live vendor calls are only partly verified.** EODHD prices and FMP fundamentals have now been
  called for real (`sentinel ingest --universe ai`), which is how the FMP port below was forced;
  Finnhub news is still `UNVERIFIED`. The adapters' *parsing* is unit-tested against recorded
  payload shapes and that is the half that harbours bugs — but only a live call proves an endpoint
  path, and `sentinel health` is where that is found out.
  **FMP is on `/stable`, not `/api/v3`.** FMP retired v3 for accounts created after 2025-08-31: it
  answers `403` with `"Legacy Endpoint"` and nothing else, on every request. `/stable` takes the
  symbol as a **query parameter** rather than a path segment and renamed four fields
  (`filingDate`, `epsDiluted`, `marketCap`, `priceToEarningsRatioTTM`). The adapter accepts both
  spellings and `FMP_API_BASE` can point back at v3, so a pre-cutoff account still works.
  **Free tiers gate ROWS, not endpoints.** On `/stable`, FMP's free plan answered 9 of the 25 AI
  tickers and returned `402 Payment Required — Special Endpoint: This value set for 'symbol' is
  not available under your current subscription` for the other 16. Nothing is wrong with the
  adapter in that case and no retry helps. `data.fundamentals_provider` therefore accepts a
  comma-separated **fallback chain** (`"fmp,eodhd"`): each vendor is tried per ticker, the first
  answer wins, and the `source` column records the vendor that actually answered rather than the
  chain. Only when every vendor refuses the same ticker is it a failure, and then the error
  carries each vendor's own reason. **Full coverage of a universe is still a paid decision** —
  the chain widens two partial free tiers, it does not make either complete.
- **Vendor history caps are detected, not assumed.** One run wrote 568 bars per ticker and the next
  wrote exactly 250, with nothing in the report saying why: `check_history_depth` compares against a
  fixed floor of 250, so a series a third of the requested length cleared it exactly. `run_all` now
  takes the `requested_start` and `check_history_span` reports any series materially shorter than the
  window asked for (INFO — a young listing is legitimately short). `check_history_cap` turns many of
  those into one run-level WARN **only** when the short series share a start date: independent
  listings do not, so a common floor is the vendor's boundary rather than the market's. The requested
  window and every series' first bar are recorded in the `INGEST_COMPLETED` audit payload, so two
  runs of different depth can be compared after the fact instead of guessed at.
- **No live LLM call has been made.** The client is exercised end to end against a stub SDK
  (including the repair turn and the no-`temperature` behaviour), but there is no
  `ANTHROPIC_API_KEY` in this environment.
- **The dashboard is read-only, with ONE deliberate write seam (2026-08-24 owner decision):
  Record a trade.** The owner asked for position entry on the web page, which supersedes the
  earlier CLI-only stance. The rules live in `portfolio.manual` — one implementation under both
  `sentinel paper buy`/`sell` and the Portfolio page's form, so they cannot drift: never an order
  anywhere (no broker connection); a risk-limit breach WARNS but records, because the book must
  match the broker especially when the broker's book breaks the rules; input that can only be a
  mistake is refused (a long's stop at/above entry, and the SAME fill twice — the duplicate guard
  is what makes a double-submitted web form deduct cash once). The form renders only when
  `manual.allowed_in` says so: a LOCAL session (`SENTINEL_DASHBOARD_LOCAL`, which the hosted
  deploy pops) on a NON-demo database (a real fill inside fabricated history would be the one
  real-looking number on a page that promises there are none). The page's own connection stays
  read-only; each recording opens its own write connection for one transaction. The Data health
  page can also START the two long jobs (2026-08-24 owner decision, superseding the line that
  ingest stays CLI-only): `dashboard.jobs` runs `sentinel ingest` / `sentinel weekly` as a
  DETACHED subprocess (it survives the tab and the dashboard; output to a log file next to the
  database) under a pid lock file (atomic O_EXCL create; one job at a time; a lock whose pid is
  dead is stale and self-clears). Liveness must consult the Popen handle, not just
  `os.kill(pid, 0)` — an exited child is a zombie until its parent reaps it, and the pid probe
  calls a zombie alive, so a finished ingest would otherwise read as "running" until the
  dashboard restarted. Only names in `jobs.COMMANDS` can run: the page hands over a choice,
  never a command line. Same `ctx.writable` gate as Record-a-trade (local + non-demo).
  While a job holds the lock, every page that reports it renders a LIVE progress panel
  (`_running_job_panel`): both ingest and the brief log one `[n/m]` line per ticker,
  `jobs.progress` parses the last marker plus the last log line out of the job log, and the
  panel draws them as a progress bar that refreshes itself every few seconds via `st.fragment`
  (a Refresh button on Streamlit without fragments).
  The Search page plots golden/death crosses on the price chart (deterministic trend events, not
  advice — the verdict banner stays the system's opinion) and shows the stored news headlines the
  sentiment module scored, which were previously captured and shown to nobody. The stock detail's
  price section is a two-way toggle: daily candlesticks (~6 months of raw OHLC prints, direction
  coloured with the validated diverging pair rather than green/red, 20/50-day averages overlaid,
  volume in the tooltip only — never a second axis) or the adjusted-close trend view with the
  50/200-day averages and cross markers. The Today page leads with a top-five leaderboard of
  accepted ideas (`score_leaders`): same rules-AND-risk gate as the Conviction board via
  `top_ideas_frame`, one hue on a full 0–100 scale, with a reference rule at the digest's
  notable-70 bar. Search accepts company names as well as tickers: known stocks are
  offered as "TICKER — Company Name" labels (one substring filter finds both spellings), and an
  unknown name goes through `data/lookup.py` — EODHD's symbol-search endpoint, one request per
  submitted query (cached per session, never per keystroke), each match a Fetch button into the
  normal ingest job. Under a BUY verdict the candle chart also draws the trade plan on the price
  axis — entry (muted), stop (solid critical), 1R/2R targets (dashed good), every rule text-labelled
  because a status colour never carries meaning alone; an AVOID/HOLD chart carries no levels, same
  policy as the plan tiles. Both price charts title themselves with the stock's name and ticker, so
  a screenshot or scroll position that has lost the page header still names its subject.
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
