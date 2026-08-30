#!/bin/sh
# Heart/cmd/heart/entrypoint.sh — start heart binary + heartbeat sidecar.
#
# Process topology:
#   This shell (PID 1 / init)
#     ├── heartbeat-sidecar.sh  (background)
#     └── /usr/local/bin/heart   (background, Go cadence engine)
#
# The Go binary reads its config from env vars (see loadConfig in main.go):
#   HEART_HEART_PATH, HEART_BRAIN_PATH, HEART_MOUTH_PATH, GH_TOKEN, HEART_BEAT_FILE
# Flags passed on the command line are accepted by the shell but ignored by Go.
set -u

log() { printf '[entrypoint] %s\n' "$*"; }

SHARED_ROOT="${NEOHIRO_HEART_SHARED_ROOT:-/shared/heart}"
mkdir -p "$SHARED_ROOT"

export NEOHIRO_DEVICE_ROLE="${NEOHIRO_DEVICE_ROLE:-heart}"

# ── Start sidecar ─────────────────────────────────────────────────────
log "starting heartbeat sidecar"
/usr/local/bin/heartbeat-sidecar.sh &
SIDECAR_PID=$!

# ── Start Heart (Go) ─────────────────────────────────────────────────
log "starting heart"
/usr/local/bin/heart &
HEART_PID=$!

# ── Cleanup on signal ─────────────────────────────────────────────────
# If a child is already dead, skip the wait — POSIX `wait` has no timeout.
cleanup() {
    log "teardown: stopping children"
    if kill -0 "$HEART_PID"    2>/dev/null; then
        kill -TERM "$HEART_PID"    2>/dev/null || true
        wait "$HEART_PID"    2>/dev/null
    fi
    if kill -0 "$SIDECAR_PID" 2>/dev/null; then
        kill -TERM "$SIDECAR_PID" 2>/dev/null || true
        wait "$SIDECAR_PID" 2>/dev/null
    fi
    log "teardown complete"
}
trap cleanup TERM INT HUP EXIT

# Reap the foreground child; when it exits, propagate its rc.
# The EXIT trap will run cleanup (idempotent: children already dead).
wait "$HEART_PID"; RC=$?
log "heart exited rc=$RC; stopping container"
exit $RC
