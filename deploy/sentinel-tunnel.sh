#!/usr/bin/env bash
#
# The dashboard, published through a Cloudflare tunnel.
#
# Runs two processes that must live and die together: the Streamlit server on
# loopback, and cloudflared republishing that origin on a public hostname.
#
# THE FAILURE THIS SCRIPT EXISTS TO PREVENT is a tunnel that outlives its
# origin, or starts without one. cloudflared happily serves 502s from a public
# URL forever, and — worse — a tunnel pointed at a `sentinel dashboard` started
# WITHOUT --tunnel would publish the portfolio with no password at all, because
# the gate reads a loopback bind as "local session, nobody else can reach this".
# So: the password is checked before anything starts, --tunnel is not optional,
# and the two processes share a fate.
#
# Exit codes:
#   0  clean shutdown
#   1  refused to start, or a process died
#
set -uo pipefail
export PATH="${PATH:+$PATH:}/usr/local/bin:/usr/bin:/bin"

PROJECT_ROOT="${SENTINEL_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SENTINEL_HOME="${SENTINEL_HOME:-$PROJECT_ROOT}"
LOG_DIR="${SENTINEL_LOG_DIR:-$SENTINEL_HOME/logs}"
PORT="${SENTINEL_DASHBOARD_PORT:-8501}"
THEME="${SENTINEL_DASHBOARD_THEME:-light}"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/tunnel-$(date -u +%Y-%m-%d).log"
log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }

# A password is the ONLY thing between the public URL and the portfolio. Check
# it here as well as in the CLI: this script is what a systemd unit runs, and a
# unit that fails at the second process has already opened the first.
if [ -z "${SENTINEL_DASHBOARD_PASSWORD:-}" ]; then
  log "FATAL: SENTINEL_DASHBOARD_PASSWORD is not set. A tunnel publishes the"
  log "       dashboard to anyone with the URL; refusing to start without it."
  exit 1
fi

UV="${UV_BIN:-$(command -v uv || true)}"
for candidate in "$HOME/.local/bin/uv" /usr/local/bin/uv /opt/homebrew/bin/uv; do
  [ -n "$UV" ] && break
  [ -x "$candidate" ] && UV="$candidate"
done
# -x, not -n: an explicitly-set UV_BIN/CLOUDFLARED_BIN pointing at a path that
# does not exist is non-empty and would sail past a presence check, so the
# dashboard would start and only THEN fail to open a tunnel — leaving a served
# origin behind. Both binaries are proven runnable before anything is served.
[ -n "$UV" ] && [ -x "$UV" ] || {
  log "FATAL: uv not found or not executable at '${UV:-<unset>}'. Set UV_BIN to its absolute path."
  exit 1
}

CLOUDFLARED="${CLOUDFLARED_BIN:-$(command -v cloudflared || true)}"
[ -n "$CLOUDFLARED" ] && [ -x "$CLOUDFLARED" ] || {
  log "FATAL: cloudflared not found or not executable at '${CLOUDFLARED:-<unset>}'."
  log "       Install it, or set CLOUDFLARED_BIN to its absolute path."
  exit 1
}

cd "$SENTINEL_HOME" || { log "FATAL: SENTINEL_HOME=$SENTINEL_HOME does not exist"; exit 1; }

DASHBOARD_PID=""
TUNNEL_PID=""
shutdown() {
  # Kill the TUNNEL first. Reversing this leaves a public URL answering for an
  # origin that is already gone, which looks like an outage rather than a stop.
  [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null
  [ -n "$DASHBOARD_PID" ] && kill "$DASHBOARD_PID" 2>/dev/null
  wait 2>/dev/null
  log "stopped"
}
trap shutdown EXIT INT TERM

# --tunnel keeps the loopback bind cloudflared needs while telling the gate not
# to treat it as a local session. Without this flag the password above is never
# demanded, which is the whole hazard.
log "=== starting dashboard on 127.0.0.1:$PORT (tunnelled) ==="
"$UV" run --project "$PROJECT_ROOT" sentinel dashboard \
  --port "$PORT" --address 127.0.0.1 --theme "$THEME" --tunnel >>"$LOG" 2>&1 &
DASHBOARD_PID=$!

# Wait for the origin before opening the tunnel, so the first visitor never
# meets a 502.
for _ in $(seq 1 "${SENTINEL_DASHBOARD_WAIT:-45}"); do
  if ! kill -0 "$DASHBOARD_PID" 2>/dev/null; then
    log "FATAL: the dashboard exited before it came up — see $LOG"
    exit 1
  fi
  curl -sf "http://127.0.0.1:$PORT/_stcore/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/_stcore/health" >/dev/null 2>&1 || {
  log "FATAL: the dashboard never became healthy on port $PORT"
  exit 1
}
log "dashboard healthy"

# Named tunnel when one is configured (stable hostname, and Cloudflare Access
# can sit in front of it); quick tunnel otherwise, which prints a random
# trycloudflare.com URL that changes on every restart.
if [ -n "${CLOUDFLARE_TUNNEL_NAME:-}" ]; then
  log "=== opening named tunnel $CLOUDFLARE_TUNNEL_NAME ==="
  "$CLOUDFLARED" tunnel run "$CLOUDFLARE_TUNNEL_NAME" >>"$LOG" 2>&1 &
else
  log "=== opening quick tunnel (random hostname; URL is in $LOG) ==="
  "$CLOUDFLARED" tunnel --no-autoupdate \
    --url "http://127.0.0.1:$PORT" >>"$LOG" 2>&1 &
fi
TUNNEL_PID=$!

# Shared fate: whichever dies first takes the other down. A dashboard with no
# tunnel is merely useless; a tunnel with no dashboard is a public 502, and a
# tunnel whose dashboard restarted unprotected would be worse than either.
while kill -0 "$DASHBOARD_PID" 2>/dev/null && kill -0 "$TUNNEL_PID" 2>/dev/null; do
  sleep 5
done

kill -0 "$DASHBOARD_PID" 2>/dev/null || log "the dashboard exited — taking the tunnel down"
kill -0 "$TUNNEL_PID" 2>/dev/null || log "cloudflared exited — taking the dashboard down"
exit 1
