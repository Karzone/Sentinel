# Running the daily brief on a schedule

The brief is two commands — `sentinel ingest` then `sentinel brief --send` — and
`sentinel-daily.sh` wraps them with the things a scheduled job needs and a
hand-written cron line usually forgets: a lock so two runs never write the same
SQLite file, exit codes that stay distinguishable, a push alert when the pipeline
dies, and logs that get cleaned up.

## Before you schedule anything

**A schedule is not the hard part; having real data is.** With no vendor keys the
fixture provider generates prices from a hash of the ticker, and with no
`ANTHROPIC_API_KEY` no memos are written — so nothing clears the risk layer's
invalidation check and the brief correctly reports no candidates, every day,
forever. Check these first:

```bash
cd ~/sentinel
uv run sentinel health          # every vendor should say "ready", not "dormant"
uv run sentinel brief           # should name real tickers, not DEMO1.LSE
```

`.env` needs `EODHD_API_KEY`, `FINNHUB_API_KEY` and `ANTHROPIC_API_KEY`, and
`sentinel.toml` needs a `[universes]` entry with real tickers.

**Configure a push channel, or failures are silent.** The runner reports a dead
pipeline through `sentinel notify failure`, which goes to ntfy. With no channel
configured there is nowhere to send it, so a broken morning looks exactly like a
quiet one. Set `ntfy_topic` in `sentinel.toml` — make it long and unguessable,
since the topic is the only thing protecting it — and confirm with:

```bash
uv run sentinel notify test
```

## systemd (recommended)

`Timezone=Europe/London` in the timer is the reason to prefer this over crontab:
the brief keeps arriving at 07:00 local through both clock changes with nobody
editing anything, and `Persistent=true` catches up if the machine was asleep.

```bash
# The unit is templated on your username, so install it as user@instance.
sudo cp sentinel-daily.service /etc/systemd/system/sentinel-daily@.service
sudo cp sentinel-daily.timer   /etc/systemd/system/sentinel-daily@.timer
sudo systemctl daemon-reload
sudo systemctl enable --now "sentinel-daily@$USER.timer"
```

Check it, then force one run without waiting for the morning:

```bash
systemctl list-timers 'sentinel-daily@*'        # NEXT should be the next weekday 07:00
sudo systemctl start "sentinel-daily@$USER"     # run it now
journalctl -u "sentinel-daily@$USER" -n 50      # what systemd saw
tail -n 50 ~/sentinel/logs/daily-*.log          # what the run itself logged
```

Edit `WorkingDirectory`, `SENTINEL_HOME` and `ReadWritePaths` in the unit if your
checkout is not at `/home/<user>/sentinel`, and uncomment `SENTINEL_UNIVERSE` to
name a universe from your `sentinel.toml`.

## crontab

For machines without systemd. It cannot catch up after downtime, and `CRON_TZ`
is not supported by every cron (notably not busybox).

```bash
crontab -e     # then paste from crontab.example, editing YOUR_USER
```

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `SENTINEL_HOME` | the checkout | Where `sentinel.toml` and `data/` live. Point it outside the repo to keep your portfolio database out of a git working tree. |
| `SENTINEL_PROJECT` | derived from the script's own path | The checkout, for `uv run --project`. |
| `SENTINEL_UNIVERSE` | unset | Universe name; unset falls back to your watchlist. |
| `SENTINEL_HISTORY_DAYS` | `800` | Days of price history per ingest. |
| `SENTINEL_LOG_DIR` | `$SENTINEL_HOME/logs` | Logs, pruned after 30 days. |
| `UV_BIN` | resolved from PATH | Absolute path to `uv`, if cron cannot find it. |

## Exit codes, and why the script does not flatten them

| Code | Meaning | What the runner does |
|---|---|---|
| 0 | Brief generated and sent | Nothing further |
| 1 | A real failure | Pushes a `PIPELINE_FAILURE` alert, exits 1 |
| 2 | Generated, but a quality check blocked a ticker | Pushes an alert, **exits 2** |

Exit 2 is deliberately not reported as success. The brief did go out and carries
its own stale-data banner, but a monitor that saw `0` would never know the run
was incomplete.

## Weekdays only

The timer runs Monday to Friday. EOD vendors publish nothing at weekends, so a
Saturday run re-scores Friday's data and mails you a brief you have already read.

## The weekly review

`sentinel-weekly.sh` runs `sentinel weekly --send` on **Sunday at 18:00
Europe/London** — late enough that the week is unambiguously over, early enough
that a met kill criterion is read before Monday's open rather than after it.

```bash
sudo cp sentinel-weekly.service /etc/systemd/system/sentinel-weekly@.service
sudo cp sentinel-weekly.timer   /etc/systemd/system/sentinel-weekly@.timer
sudo systemctl daemon-reload
sudo systemctl enable --now "sentinel-weekly@$USER.timer"
sudo systemctl start "sentinel-weekly@$USER"     # run one now
```

It reads rather than ingests — no vendor call, just the numbers already stored —
but it takes **the same lock as the daily runner**, because both write to the
same SQLite file and a Sunday review racing a catch-up daily run is the kind of
thing that only happens once.

Its exit codes differ from the daily runner's in one important way:

| Code | Meaning |
|---|---|
| 0 | Review generated and sent |
| 1 | A real failure |
| 2 | Generated, and **a kill criterion has been met** |

Exit 2 pushes an alert. A met kill criterion is the most consequential thing this
system can say — the pre-committed answer to "should this money just be indexed?"
— and it should not sit unread in an inbox until Monday.

`SENTINEL_REVIEW_WEEKS` (default `1`) widens the window if you want a monthly
retrospective from the same command.
