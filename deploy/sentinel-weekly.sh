#!/usr/bin/env bash
#
# The scheduled weekly review.
#
# Simpler than the daily runner because it reads rather than ingests: no vendor
# call, no new data, just the numbers already in the database. It shares the
# daily runner's lock anyway, because both write to the same SQLite file and a
# Sunday review racing a catch-up daily run is exactly the kind of thing that
# only happens once and corrupts the audit trail when it does.
#
# Exit codes from `sentinel weekly`:
#   0  review generated and sent
#   1  a real failure
#   2  generated, and A KILL CRITERION HAS BEEN MET
#
set -uo pipefail
export PATH="${PATH:+$PATH:}/usr/local/bin:/usr/bin:/bin"

PROJECT_ROOT="${SENTINEL_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SENTINEL_HOME="${SENTINEL_HOME:-$PROJECT_ROOT}"
LOG_DIR="${SENTINEL_LOG_DIR:-$SENTINEL_HOME/logs}"
LOCK_FILE="${SENTINEL_LOCK:-/tmp/sentinel-daily.lock}"
WEEKS="${SENTINEL_REVIEW_WEEKS:-1}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/weekly-$(date -u +%Y-%m-%d).log"
log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

UV="${UV_BIN:-$(command -v uv || true)}"
for candidate in "$HOME/.local/bin/uv" /usr/local/bin/uv /opt/homebrew/bin/uv; do
  [ -n "$UV" ] && break
  [ -x "$candidate" ] && UV="$candidate"
done
[ -n "$UV" ] || { log "FATAL: uv not found on PATH. Set UV_BIN to its absolute path."; exit 1; }

cd "$SENTINEL_HOME" || { log "FATAL: SENTINEL_HOME=$SENTINEL_HOME does not exist"; exit 1; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "the daily runner holds $LOCK_FILE — exiting rather than running concurrently"
  exit 0
fi

alert() {
  "$UV" run --project "$PROJECT_ROOT" sentinel notify failure "$1" >>"$LOG" 2>&1 \
    || log "WARNING: could not send the failure alert either"
}

log "=== weekly review (${WEEKS}w) ==="
"$UV" run --project "$PROJECT_ROOT" sentinel weekly --weeks "$WEEKS" --send >>"$LOG" 2>&1
rc=$?
case "$rc" in
  0) log "review sent" ;;
  2) log "review sent — A KILL CRITERION HAS BEEN MET"
     # This is the single most consequential thing the system can say, and it
     # would otherwise sit unread in an email until Monday. It is a
     # PIPELINE_FAILURE push only because that is the event channel available;
     # the message says what it actually is.
     alert "A kill criterion has been met. Read this week's review before placing any trade." ;;
  *) log "FATAL: weekly review failed (exit $rc)"
     alert "Weekly review failed with exit code $rc." ;;
esac

find "$LOG_DIR" -name 'weekly-*.log' -type f -mtime +90 -delete 2>/dev/null || true
log "done (rc=$rc)"
exit "$rc"
