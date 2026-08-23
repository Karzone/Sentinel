# Sentinel — personal investment research copilot

> **Research output, not financial advice. All decisions and risk are the owner's.**

Sentinel aggregates market data, fundamentals, technicals, news and sentiment into a daily
research brief, proposes a small number of candidate ideas *with explicit reasoning, an
invalidation level and a confidence label*, and puts every one of them through a hard-coded
risk layer before it is allowed to reach you.

It does **not** place orders. It never will — see [the one hard rule](#the-one-hard-rule).

## Quick start

```bash
uv sync                       # Python 3.12, deps pinned in uv.lock
cp .env.example .env          # vendor keys; every one is optional
uv run sentinel init          # creates sentinel.toml + the SQLite db
uv run sentinel ingest --universe demo
uv run sentinel brief
```

With no vendor keys at all the fixture provider drives the whole pipeline offline, which is
how the test suite runs and how you should explore it first.

## The one hard rule

No automated order execution against real money. Sentinel produces research; a human reads it
and places every trade. Paper trading through an API is fine and is the point of Phase 4 — a
mandatory 6-month paper period gates any real-money use of short-term signals.

## Commands

| Command | What it does |
|---|---|
| `sentinel init` | Write a starter config and create the database |
| `sentinel ingest` | Pull EOD prices / fundamentals / news for a universe, run quality checks |
| `sentinel health` | Data-freshness and quality report; non-zero exit on a Sev-1 |
| `sentinel idea <TICKER>` | Run every module for one ticker and print the memo + risk verdict |
| `sentinel brief` | Generate today's brief (markdown) and store it |
| `sentinel paper status` | Open paper positions, distance to stop, drawdown vs high-water mark |
| `sentinel backtest` | Walk-forward backtest with UK costs, vs B1–B4 |
| `sentinel evals` | Signal-quality, calibration and performance evals |
| `sentinel notify test` | Send one test message down each notification channel |
| `sentinel dashboard` | Launch the read-only Streamlit dashboard |

## Layout

```
src/sentinel/
  config.py      TOML config + .env, all limits in one place
  money.py       Decimal money, currency-checked arithmetic, explicit FX
  domain/        pydantic models — the wire contract for the audit trail
  storage/       SQLite schema, repositories, immutable audit log
  data/          vendor adapters behind one protocol + quality checks
  analysis/      fundamental & technical (deterministic), news/sentiment/synthesis (LLM)
  risk/          the hard-coded risk layer. No LLM reaches in here.
  portfolio/     paper ledger in Decimal
  backtest/      walk-forward engine, UK cost model, B1–B4 benchmarks
  evals/         performance, calibration and signal-quality evals
  brief/         daily brief + weekly review renderers
  notify/        scheduled digest vs event-driven push (kept strictly apart)
  dashboard/     read-only Streamlit dashboard (validated palette, no writes)
```

## Design rules that are not negotiable

1. **Deterministic logic is never delegated to an LLM.** Fundamental scoring, technical
   scoring, position sizing, risk checks and backtest maths are pure Python. LLMs are used
   for exactly four things: news synthesis, sentiment scoring, memo writing and eval judging.
2. **The LLM proposes, the rules dispose.** Every LLM output passes a deterministic vetting
   layer (`analysis/rules.py`) before it can become an idea.
3. **All money is `Decimal`, GBP base, explicit FX.** `money.Money` refuses cross-currency
   arithmetic, so a USD price cannot silently become a GBP position size.
4. **Sentiment is never a primary buy reason.** Encoded as a rule, not a guideline.
5. **A signal generated from bad data is a Sev-1.** The quality layer can block a brief.
6. **Every idea is stored immutably** with its inputs digest and model versions. That store
   is the eval dataset.

## Status

Phases 0–6 of `specs/sentinel.md` are implemented and tested. See the spec's
"Not built yet" section for the gaps that remain — chiefly that no live vendor or
LLM call has been made from this code yet.
