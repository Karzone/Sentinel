# CLAUDE.md — Sentinel

Project context for Claude Code. Read this before making changes. If anything here drifts from
the actual code, this file is the source of truth for *intent* — fix the code, or if intent
changed, update this file in the same commit.

## What this is

A personal investment research copilot for a UK retail investor. It aggregates prices,
fundamentals, news and sentiment; scores candidates through five modules; puts every resulting
idea through a hard-coded risk layer; and produces a daily brief of at most three candidates,
each with a thesis, an invalidation condition and a position size.

It manages the **satellite** 10–20% of a core/satellite portfolio. The passive core is
deliberately outside its scope, and every percentage limit is a fraction of satellite capital,
never of net worth — a limit that accidentally sized against the whole portfolio would be
5–10× too permissive.

`specs/sentinel.md` is the contract. Build against it; if reality diverges, update it in the
same commit.

## The one hard rule: no automated execution

Sentinel never places an order against real money, and never will. Paper trading through the
simulated ledger is the whole of its execution surface, and a mandatory six-month paper period
gates any real-money use of short-term signals. The final human click is a feature, not a
limitation. Do not add order execution, even if asked in passing — flag it instead.

## Rules that are not negotiable

These are enforced mechanically, not by convention. **A commit that changes what enforces one
of them updates `specs/sentinel.md` § 2 in the same commit** — a reviewer must never have to
run the code to learn what actually guards an invariant.

1. **Deterministic logic is never delegated to an LLM.** Fundamental scoring, technical
   scoring, position sizing, risk checks and backtest maths are pure Python. LLMs do exactly
   four things: news synthesis, sentiment scoring, memo writing, eval judging.
2. **The LLM proposes, the rules dispose.** `analysis/rules.py::vet` runs R1–R8 over every memo
   before it can become an idea. A rejected memo is still stored with its reasons — deleting
   rejections would make the rejection rate unmeasurable.
3. **`risk/` imports nothing from `analysis/` or `llm/`.** The engine reads an idea's ticker,
   class and invalidation text — never its score or conviction. Two tests exist purely to keep
   it that way.
4. **All money is `Decimal`, GBP base, explicit FX.** `money.Money` refuses cross-currency
   arithmetic. Every price column in SQLite is `TEXT`, never `REAL`.
5. **Ideas and the audit trail are append-only**, enforced by `RAISE(ABORT)` triggers.
6. **A signal from bad data is a Sev-1.** A `CRITICAL` quality issue means the ticker is not
   scored *at all* — not scored-and-flagged.
7. **Missing data is never scored as neutral.** Components with no inputs are dropped and
   confidence falls. "We could not see" must stay distinguishable from "we looked and it was
   average", or the calibration evals mean nothing.
8. **The daily brief never pushes.** Push is a closed five-event allow-list.
9. **The dashboard cannot write** — its connection is opened `mode=ro`.

## Verification gate

No significant change is done until: the full suite passes (`uv run pytest -q`); there is a
test that would have **failed before** the change; the risk layer still reports 100% branch
coverage; and anything touching a live path has been exercised end-to-end against a real
database, not just unit-tested. The live round-trip is not ceremony — it is what caught the
`config.satellite_capital` / `satellite_capital_gbp` mismatch that every unit test missed.

Report what changed, the failing-before test name, suite results as numbers, and what you
personally observed in the live run. A behaviour claim without that evidence line is not
acceptable; if verification is genuinely impossible, prefix the claim with `UNVERIFIED:`.

## Charts

Every chart goes through the data-viz method: pick the form before the colour, assign
categorical hues by entity in fixed order, and **run the palette validator rather than
eyeballing colourblind safety**. The validated slots live in `dashboard/palette.py` with their
results recorded in the docstring. One y-axis, ever. A legend for two or more series, endpoint
labels only, and a table twin for every chart.

## Honesty

This system's north star is that it will tell the owner when the benchmark is winning. That
shapes the code: evals return negative verdicts in plain English, `Sharpe` returns `None`
rather than `0.0` when it cannot be computed, hit rates always carry their interval, the
Monte Carlo carries an exposure caveat, and the brief's "what this run got wrong" section is
mandatory and must always find something. Do not soften any of it.
