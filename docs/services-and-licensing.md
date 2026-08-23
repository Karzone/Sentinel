# Services and licensing

The one place for every third-party service this project talks to, with its tier
and cost, and the licence of every dependency it ships. **Add a row in the same
commit as any new service or dependency.**

## The stack

| Layer | What | Version |
|---|---|---|
| Language | Python, managed with `uv` | 3.12 |
| CLI | Typer + Rich | 0.27 / 15.0 |
| Data | pandas + NumPy for analysis; `Decimal` for anything monetary | 3.0 / 2.5 |
| Storage | SQLite (upgrade path to Postgres; nothing uses a SQLite-only type) | stdlib |
| Models | pydantic v2 — the wire contract for the audit trail | 2.13 |
| HTTP | httpx, for the vendor adapters | 0.28 |
| Dashboard | Streamlit + Altair, read-only (optional extra) | 1.62 / 6.2 |
| LLM | Anthropic API, for news / sentiment / memo / judge only (optional extra) | — |

## Services

Every adapter is **dormant without its key**: no crash, and `sentinel health`
reports which are asleep. The fixture provider generates prices, fundamentals and
news from a hash of the ticker, so the whole pipeline and the entire test suite
run offline with an empty `.env`.

| Service | What it does | Tier | Limit | Cost |
|---|---|---|---|---|
| EODHD | EOD prices + fundamentals; good LSE coverage, which is the binding constraint for a UK investor | Paid | per plan | ~$20/mo — **not subscribed** |
| Financial Modeling Prep | Fundamentals, statements, TTM ratios | Paid | per plan | ~$20–30/mo — **not subscribed** |
| Finnhub | Company-tagged news. The tagging is the point: an untagged headline scored against a ticker is a fabricated input | **Free** | 60 calls/min | $0 — **no key set** |
| Anthropic API | News synthesis, sentiment scoring, memo writing, eval judging. Nothing else. | Prepaid credits | — | usage — **no key set** |
| Resend | Transactional mail for the daily digest | **Free** | 3,000/month, 100/day | $0 — **not configured** |
| ntfy.sh | Mobile push for stop / kill-switch / pipeline events | **Free** | no account; the topic is the only credential | $0 |
| GitHub Actions | CI | **Free** (private repo) | 2,000 min/month | $0 |
| Streamlit Community Cloud | Optional demonstration hosting of the dashboard. Fabricated data only — `streamlit_app.py` refuses to serve a database without the demo stamp | **Free** | 1 private app; sleeps when idle; no persistent disk | $0 — **not deployed** |
| Cloudflare Tunnel (`cloudflared`) | Publishes the loopback dashboard on an HTTPS hostname without opening a port. Optional; not required to run anything | **Free** | quick tunnels are rate-limited and get a random hostname; a named tunnel needs a free Cloudflare account + a domain | $0 |

Budget guidance from the spec: start at **£40–70/month** total (one
price+fundamentals vendor, Finnhub free, Anthropic usage). Do not buy more until
the Phase 4 evals justify it.

**The ntfy topic is the only credential protecting the push channel.** Anyone who
knows it can publish to it, so make it long and unguessable rather than
`sentinel`.

**A tunnel makes the dashboard password load-bearing, not advisory.** The gate
serves without one only when the launcher says the session is local, which it
infers from a loopback bind — and a tunnel republishes exactly that origin. So
`sentinel dashboard --tunnel` binds loopback as usual but refuses to start
without `SENTINEL_DASHBOARD_PASSWORD`, and `deploy/sentinel-tunnel.sh` checks
again before starting either process. `cloudflared` sees the connection, never
the database: no data is copied to Cloudflare. A named tunnel can additionally
sit behind Cloudflare Access, which is a second lock rather than a replacement
for the first.

## Open-source licences

Read from the installed packages on 2026-08-23 via `importlib.metadata`, not from
memory. Re-run the extraction after adding a dependency. Sentinel ships as source;
nothing here is redistributed as a binary.

| Package | Version | Licence |
|---|---|---|
| annotated-types | 0.8.0 | MIT |
| anyio | 4.14.2 | MIT |
| certifi | 2026.7.22 | **MPL-2.0** — see below |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| markdown-it-py | 4.2.0 | MIT |
| mdurl | 0.1.2 | MIT |
| numpy | 2.5.2 | BSD-3-Clause (with 0BSD / MIT / Zlib components) |
| pandas | 3.0.5 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic-core | 2.46.4 | MIT |
| pygments | 2.21.0 | BSD-2-Clause |
| python-dateutil | 2.9.0.post0 | Dual Apache-2.0 / BSD-3-Clause |
| python-dotenv | 1.2.3 | BSD-3-Clause |
| rich | 15.0.0 | MIT |
| shellingham | 1.5.4 | ISC |
| six | 1.17.0 | MIT |
| typer | 0.27.1 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |

Optional extras: `anthropic` (MIT, `--extra llm`); `streamlit` (Apache-2.0) and
`altair` (BSD-3-Clause) with their own dependency trees (`--extra dashboard`).

### ⚠ The one non-permissive dependency

**`certifi`** is **MPL-2.0** — *weak, file-level copyleft*, not the permissive
licence a blanket "everything is MIT/BSD" would claim. In practice it obliges
nothing here: MPL-2.0's reciprocity attaches to modified copies of the
MPL-licensed **files themselves**, and we neither modify certifi nor distribute
it — it is installed from PyPI on the machine that runs Sentinel. It matters only
if certifi's own source is ever patched and shipped onward, in which case those
files' source must travel with them. Recorded because a licensing claim that is
95% right is the kind that survives until it matters.

## Review triggers

Re-read this file when: adding a dependency or a vendor; before making the
repository public; if the licensing picture ever becomes load-bearing (due
diligence, an acquisition, shipping a binary anywhere other than your own
machine).
