#!/usr/bin/env bash
#
# The scheduled daily run: ingest, then brief.
#
# Everything interesting in this script is about the three ways a scheduled job
# fails quietly, because a morning with no brief looks exactly like a quiet
# morning with no candidates:
#
#   1. The pipeline dies and nothing is sent.        -> push a PIPELINE_FAILURE alert
#   2. Two runs overlap and corrupt the database.    -> flock, non-blocking
#   3. The brief goes out built on stale data.       -> exit code 2 is NOT success
#
# Exit codes from the CLI, which this script deliberately does not flatten:
#   0  brief generated and sent
#   1  a real failure
#   2  generated, but a data-quality check blocked at least one ticker
#
set -uo pipefail

# cron can hand this script a PATH of almost nothing — on some systems not
# even coreutils, so `date` and `tee` in log() below would fail before the
# script could report anything. Append the standard locations rather than
# replacing whatever the caller set.
export PATH="${PATH:+$PATH:}/usr/local/bin:/usr/bin:/bin"

# The checkout, found from this script's own location, so the runner works
# regardless of where it is invoked from and without hard-coding a path.
PROJECT_ROOT="${SENTINEL_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Where sentinel.toml and data/ live. Defaults to the checkout, but can point
# elsewhere so your portfolio database is not sitting inside a git working tree.
SENTINEL_HOME="${SENTINEL_HOME:-$PROJECT_ROOT}"
UNIVERSE="${SENTINEL_UNIVERSE:-}"
LOG_DIR="${SENTINEL_LOG_DIR:-$SENTINEL_HOME/logs}"
LOCK_FILE="${SENTINEL_LOCK:-/tmp/sentinel-daily.lock}"
HISTORY_DAYS="${SENTINEL_HISTORY_DAYS:-800}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily-$(date -u +%Y-%m-%d).log"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

# cron gives you almost no PATH. Resolve uv explicitly rather than hoping.
UV="${UV_BIN:-$(command -v uv || true)}"
for candidate in "$HOME/.local/bin/uv" /usr/local/bin/uv /opt/homebrew/bin/uv; do
  [ -n "$UV" ] && break
  [ -x "$candidate" ] && UV="$candidate"
done
if [ -z "$UV" ]; then
  log "FATAL: uv not found on PATH. Set UV_BIN to its absolute path."
  exit 1
fi

cd "$SENTINEL_HOME" || { log "FATAL: SENTINEL_HOME=$SENTINEL_HOME does not exist"; exit 1; }
[ -f sentinel.toml ] || log "WARNING: no sentinel.toml in $SENTINEL_HOME — using defaults"

# Non-blocking: if yesterday's run is somehow still going, do NOT queue up behind
# it and end up with two processes writing the same SQLite file.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another run holds $LOCK_FILE — exiting rather than running concurrently"
  exit 0
fi

alert() {
  # Best-effort. If the alert channel is down too, say so in the log and carry
  # on failing — never let the alerting mask the original fault.
  "$UV" run --project "$PROJECT_ROOT" sentinel notify failure "$1" >>"$LOG" 2>&1 \
    || log "WARNING: could not send the failure alert either"
}

universe_args=()
[ -n "$UNIVERSE" ] && universe_args=(--universe "$UNIVERSE")

log "=== ingest ==="
"$UV" run --project "$PROJECT_ROOT" sentinel ingest "${universe_args[@]}" \
  --history "$HISTORY_DAYS" >>"$LOG" 2>&1
ingest_rc=$?
case "$ingest_rc" in
  0) log "ingest clean" ;;
  2) log "ingest completed with CRITICAL data-quality issues" ;;
  *) log "FATAL: ingest failed (exit $ingest_rc)"
     alert "Ingest failed with exit code $ingest_rc."
     exit "$ingest_rc" ;;
esac

log "=== brief ==="
"$UV" run --project "$PROJECT_ROOT" sentinel brief "${universe_args[@]}" --send >>"$LOG" 2>&1
brief_rc=$?
case "$brief_rc" in
  0) log "brief sent" ;;
  2) log "brief sent, but it is flagged stale — some tickers were not scored"
     # Not a failure: the brief went out and carries its own banner. Still worth
     # a push, because a stale brief is a thing to look at rather than skim.
     alert "Today's brief went out flagged as incomplete: a data-quality check blocked at least one ticker." ;;
  *) log "FATAL: brief failed (exit $brief_rc)"
     alert "Brief generation failed with exit code $brief_rc."
     exit "$brief_rc" ;;
esac

# Keep 30 days of logs. Small, but unbounded log growth is how a Pi fills its SD card.
find "$LOG_DIR" -name 'daily-*.log' -type f -mtime +30 -delete 2>/dev/null || true

log "done (ingest=$ingest_rc brief=$brief_rc)"
exit "$brief_rc"
