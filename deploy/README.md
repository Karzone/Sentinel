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

---

# Reaching the dashboard from anywhere (Cloudflare tunnel)

`sentinel dashboard` binds loopback, so it is reachable only from the machine it
runs on. A tunnel republishes that origin on an HTTPS hostname without opening a
port, forwarding a port on your router, or copying the database anywhere. The
data stays on your machine; Cloudflare only carries the connection.

## Read this before you start it

**A loopback bind is what tells the password gate "nobody else can reach this",
and a tunnel is exactly the thing that makes that false.** Run
`cloudflared --url http://localhost:8501` in front of a plain `sentinel
dashboard` and the whole portfolio is served to anyone with the URL, with no
password, under a sidebar notice reading *"Running locally"*. That is not
hypothetical — it is what the default flags do.

So the dashboard has a `--tunnel` flag. It changes nothing about the bind — a
tunnel needs a loopback origin — only whether that origin is allowed to imply
safety. With it, `SENTINEL_DASHBOARD_PASSWORD` becomes mandatory and the
launcher refuses to start without one. **Always use it, or the script that does
it for you.**

```bash
sentinel dashboard --tunnel          # refuses: no password set
```

## Setup

```bash
# 1. cloudflared
#    macOS:  brew install cloudflared
#    Debian: see https://pkg.cloudflare.com — the repo, not a loose .deb

# 2. A password. It is the only thing between the URL and your positions.
sudo mkdir -p /etc/sentinel
printf 'SENTINEL_DASHBOARD_PASSWORD=%s\n' "$(openssl rand -base64 24)" \
  | sudo tee /etc/sentinel/dashboard.env >/dev/null
sudo chmod 600 /etc/sentinel/dashboard.env

# 3. Try it in the foreground first.
set -a; . /etc/sentinel/dashboard.env; set +a
./deploy/sentinel-tunnel.sh
```

The quick tunnel prints a `https://<random-words>.trycloudflare.com` URL into
`logs/tunnel-<date>.log`. Open it, and you should get the password prompt — not
the dashboard. If you get the dashboard, stop immediately: `--tunnel` was not
passed.

## Leaving it running

```bash
sudo cp deploy/sentinel-tunnel.service /etc/systemd/system/sentinel-tunnel@.service
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-tunnel@$USER
journalctl -u sentinel-tunnel@$USER -f
```

`Restart=on-failure` is deliberate here, unlike the daily and weekly units: a
dropped tunnel is a transient the script cannot fix from inside. A restart loop
cannot degrade into an open dashboard, because the script refuses to start
without a password at all.

## Quick tunnel vs named tunnel

The quick tunnel needs no Cloudflare account, but its hostname changes on every
restart and the only access control is the app password. For anything you intend
to keep:

```bash
cloudflared tunnel login
cloudflared tunnel create sentinel
# route it at a hostname on your domain, then:
sudo systemctl set-environment CLOUDFLARE_TUNNEL_NAME=sentinel
```

A named tunnel gives a stable hostname and lets **Cloudflare Access** sit in
front — email or SSO before a request ever reaches your machine. That is a
second, independent lock; the app password stays regardless, because a tunnel
misconfiguration should not be a single point of failure for your portfolio.

## What the script guarantees

- Nothing starts without a password — not the dashboard, not cloudflared.
- `uv` and `cloudflared` are proven **executable** before anything is served, so
  a wrong `CLOUDFLARED_BIN` cannot leave a served origin with no tunnel.
- The tunnel opens only after the origin answers `/_stcore/health`, so the first
  visitor never meets a 502.
- The two processes share a fate: whichever dies takes the other with it.

These are tested in `tests/test_tunnel.py` against fake binaries, offline.
