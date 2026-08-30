#!/usr/bin/env bash
#
# Heart/docker/monitor.sh — disk + system watchdog for the Heart container.
# ─────────────────────────────────────────────────────────────────────────
# This is the **outermost guard** of the neohiro infrastructure: the Skin
# of the organism. When it escalates, the rest of the immune system kicks
# in (heart -> doctor -> scrapling lab -> GitHub Actions fallback).
#
# Medical-analogy diagnostic terminology is used throughout. When a metric
# goes outside the safe range, we call it an **organ failure** of the
# corresponding system part. Recovery is **regeneration**. The escalation
# chain is the **sepsis protocol**.
#
# Configuration (env vars):
#   HEART_PATH              Root of /Heart (default: /heart)
#   HEART_DATA_PATH         Data directory (default: $HEART_PATH/data)
#   HEART_METRICS_DIR       Where to look for KPI files (default: $HEART_DATA_PATH/metrics)
#   WARN_THRESHOLD_MB       Free-space warning threshold (default: 2048)
#   CRITICAL_THRESHOLD_MB   Free-space critical threshold (default: 500)
#   MOUTH_WEBHOOK           URL to POST alerts to (default: http://localhost:4096/heartbeat/alert)
#   GH_ACTIONS_TOKEN        GitHub PAT with `workflow` scope (for fallback trigger)
#   GH_REPOSITORY           e.g. "neohiro/wingman-hub" (for fallback trigger)
#   HEART_FALLBACK_WORKFLOW Filename of fallback workflow (default: heart-remote-fallback.yml)
#   HEART_BPM               Expected heartbeats per minute (default: 1)
#   HEART_STALL_THRESHOLD_S If last heartbeat is older than this, treat as heart-stalled
#                           (default: 120 — i.e. 2 BPM is a stalling heart)
#
# Exit codes (organ-failure severity):
#   0 — Healthy (vital signs in range)
#   1 — Warning (one or more vitals elevated; logged + admin+developer notified)
#   2 — Critical (organ failure; fallback GitHub Actions triggered)
#
# Cron entry:
#   * * * * * /heart/docker/monitor.sh >> /var/log/heart-monitor.log 2>&1
#

set -euo pipefail

HEART_PATH="${HEART_PATH:-/heart}"
HEART_DATA_PATH="${HEART_DATA_PATH:-$HEART_PATH/data}"
HEART_METRICS_DIR="${HEART_METRICS_DIR:-$HEART_DATA_PATH/metrics}"
WARN_THRESHOLD_MB="${WARN_THRESHOLD_MB:-2048}"
CRITICAL_THRESHOLD_MB="${CRITICAL_THRESHOLD_MB:-500}"
MOUTH_WEBHOOK="${MOUTH_WEBHOOK:-http://localhost:4096/heartbeat/alert}"
GH_ACTIONS_TOKEN="${GH_ACTIONS_TOKEN:-}"
GH_REPOSITORY="${GH_REPOSITORY:-neohiro/wingman-hub}"
HEART_FALLBACK_WORKFLOW="${HEART_FALLBACK_WORKFLOW:-heart-remote-fallback.yml}"
HEART_BPM="${HEART_BPM:-1}"
HEART_STALL_THRESHOLD_S="${HEART_STALL_THRESHOLD_S:-120}"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] $*"; }
warn() { log "WARN: $*"; }
critical() { log "CRITICAL: $*"; }

# Prevent concurrent runs via PID file (portable, no flock required).
LOCKFILE="${HEART_DATA_PATH}/.monitor.lock"
if [ -f "$LOCKFILE" ]; then
    OLD_PID=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        log "LOCKED — pid $OLD_PID still running; exiting"
        exit 0
    fi
fi
echo "$$" > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

ALERTS_FILE="$HEART_DATA_PATH/alerts.yaml"
mkdir -p "$HEART_DATA_PATH" "$HEART_METRICS_DIR"

# ── Vital sign 1: skin (disk space) ────────────────────────────────────
# The outer guard: when disk runs low, the whole organism overheats.
if command -v df >/dev/null 2>&1; then
    FREE_KB=$(df -Pk "$HEART_DATA_PATH" 2>/dev/null | awk 'NR==2 {print $4}')
    if [ -z "$FREE_KB" ]; then
        FREE_KB=$(df -k "$HEART_DATA_PATH" 2>/dev/null | awk 'NR==2 {print $4}')
    fi
    FREE_MB=${FREE_KB:-0}
    FREE_MB=$((FREE_MB / 1024))
else
    critical "df not found — cannot measure skin (disk)"
    exit 1
fi

log "skin (disk_free_mb)=$FREE_MB warn_threshold=$WARN_THRESHOLD_MB critical_threshold=$CRITICAL_THRESHOLD_MB"

# ── Vital sign 2: heart rate (last heartbeat age) ──────────────────────
# If the cadence engine hasn't beaten in 2 minutes, the heart is stalling.
LAST_RUN_FILE="${HEART_PATH}/heartbeat/last_run.yaml"
HEART_AGE_S=-1
if [ -f "$LAST_RUN_FILE" ]; then
    LAST_RUN_TS=$(grep -m1 '^ts:' "$LAST_RUN_FILE" 2>/dev/null | awk '{print $2}' | tr -d '"' || echo "")
    if [ -n "$LAST_RUN_TS" ]; then
        LAST_EPOCH=$(date -d "$LAST_RUN_TS" +%s 2>/dev/null || echo 0)
        if [ "$LAST_EPOCH" -gt 0 ]; then
            NOW_EPOCH=$(date -u +%s)
            HEART_AGE_S=$((NOW_EPOCH - LAST_EPOCH))
        fi
    fi
fi
log "heart_rate_age_s=$HEART_AGE_S stall_threshold_s=$HEART_STALL_THRESHOLD_S"

# ── Vital sign 3: discovered KPIs ──────────────────────────────────────
# monitor.sh grows with the project. Any *.kpi.yaml file dropped in
# HEART_METRICS_DIR is picked up and evaluated against its warning /
# critical thresholds. The schema is simple:
#
#   metric: my_custom_kpi
#   value: 42
#   warn: 80
#   critical: 95
#   organ: brain   # which organ does this belong to?
#   direction: above  # warn/critical when value >= threshold
#   unit: percent
#
# This is the **dynamic growth contract**: any new KPI you want to track,
# drop a file. monitor.sh will read it on the next tick.
DISCOVERED_KPIS=""
KPI_WARNINGS=""
KPI_CRITICALS=""
if [ -d "$HEART_METRICS_DIR" ]; then
    for f in "$HEART_METRICS_DIR"/*.kpi.yaml; do
        [ -f "$f" ] || continue
        # Tiny YAML reader: handles the simple flat key: value shape above.
        metric=$(awk '/^metric:/{$1=""; print substr($0,2)}' "$f" | sed 's/^ //; s/^"//; s/"$//')
        value=$(awk '/^value:/{print $2}' "$f")
        warn_th=$(awk '/^warn:/{print $2}' "$f")
        crit_th=$(awk '/^critical:/{print $2}' "$f")
        organ=$(awk '/^organ:/{$1=""; print substr($0,2)}' "$f" | sed 's/^ //')
        direction=$(awk '/^direction:/{$1=""; print substr($0,2)}' "$f" | sed 's/^ //')
        unit=$(awk '/^unit:/{$1=""; print substr($0,2)}' "$f" | sed 's/^ //')
        unit=${unit:-count}
        direction=${direction:-above}

        if [ -z "$metric" ] || [ -z "$value" ]; then
            warn "skipping $(basename "$f"): missing metric/value"
            continue
        fi

        # Evaluate threshold
        is_warn=0; is_crit=0
        if [ "$direction" = "above" ]; then
            [ -n "$warn_th" ] && [ "$value" -ge "$warn_th" ] 2>/dev/null && is_warn=1
            [ -n "$crit_th" ] && [ "$value" -ge "$crit_th" ] 2>/dev/null && is_crit=1
        else
            [ -n "$warn_th" ] && [ "$value" -lt "$warn_th" ] 2>/dev/null && is_warn=1
            [ -n "$crit_th" ] && [ "$value" -lt "$crit_th" ] 2>/dev/null && is_crit=1
        fi

        if [ "$is_crit" = "1" ]; then
            KPI_CRITICALS="$KPI_CRITICALS $organ:$metric=$value$unit"
        elif [ "$is_warn" = "1" ]; then
            KPI_WARNINGS="$KPI_WARNINGS $organ:$metric=$value$unit"
        fi
    done
fi
[ -n "$KPI_WARNINGS" ]   && log "kpi_warnings:$KPI_WARNINGS"
[ -n "$KPI_CRITICALS" ] && log "kpi_criticals:$KPI_CRITICALS"

# ── Triage the vitals ──────────────────────────────────────────────────
# Aggregate the worst of: disk, heart rate, KPI warnings, KPI criticals.
LEVEL="ok"
ESCALATE=0
if [ "$FREE_MB" -lt "$CRITICAL_THRESHOLD_MB" ]; then
    LEVEL="critical"
    ESCALATE=2
    REASONS="organ_failure:skin(disk=$FREE_MB MB)"
elif [ "$FREE_MB" -lt "$WARN_THRESHOLD_MB" ]; then
    LEVEL="warning"
    ESCALATE=1
    REASONS="warning:skin(disk=$FREE_MB MB)"
fi

if [ "$HEART_AGE_S" -ge 0 ] && [ "$HEART_AGE_S" -gt "$HEART_STALL_THRESHOLD_S" ]; then
    LEVEL="critical"
    ESCALATE=2
    REASONS="${REASONS:+$REASONS; }organ_failure:heart(stalled_for=${HEART_AGE_S}s)"
fi

if [ -n "$KPI_CRITICALS" ]; then
    LEVEL="critical"
    ESCALATE=2
    if [ -n "$REASONS" ]; then
        REASONS="${REASONS}; organ_failure:KPIs($KPI_CRITICALS)"
    else
        REASONS="organ_failure:KPIs($KPI_CRITICALS)"
    fi
elif [ -n "$KPI_WARNINGS" ] && [ "$ESCALATE" = "0" ]; then
    LEVEL="warning"
    ESCALATE=1
    if [ -n "$REASONS" ]; then
        REASONS="${REASONS}; warning:KPIs($KPI_WARNINGS)"
    else
        REASONS="warning:KPIs($KPI_WARNINGS)"
    fi
fi

if [ "$LEVEL" = "ok" ]; then
    log "OK"
    exit 0
fi

# Append a structured alert (single yaml doc per alert; entries are blank-line separated).
cat >> "$ALERTS_FILE" <<EOF
- ts: $(ts)
  level: $LEVEL
  reason: "${REASONS:-unknown}"
  metrics:
    skin_disk_free_mb: $FREE_MB
    heart_age_s: $HEART_AGE_S
    kpi_warnings: "${KPI_WARNINGS:-}"
    kpi_criticals: "${KPI_CRITICALS:-}"
  warn_threshold_mb: $WARN_THRESHOLD_MB
  critical_threshold_mb: $CRITICAL_THRESHOLD_MB
  notify: [admin, developer]
  organ_failure: true

EOF
log "alert logged: $LEVEL :: $REASONS"

# Notify Mouth webhook (admin + developer only)
if [ -n "$MOUTH_WEBHOOK" ] && command -v curl >/dev/null 2>&1; then
    if curl -sS --fail --max-time 10 -X POST \
        -H "Content-Type: application/json" \
        -d "{\"level\":\"$LEVEL\",\"reason\":\"$REASONS\",\"recipients\":[\"admin\",\"developer\"]}" \
        "$MOUTH_WEBHOOK" >/dev/null 2>&1; then
        log "webhook delivered to mouth"
    else
        warn "webhook post failed (non-fatal)"
    fi
fi

# Critical: trigger GitHub Actions fallback workflow (sepsis protocol)
if [ "$ESCALATE" -eq 2 ]; then
    critical "triggering sepsis protocol: GitHub Actions fallback"
    if [ -n "$GH_ACTIONS_TOKEN" ] && command -v curl >/dev/null 2>&1; then
        # Pass enough state to the fallback for context
        INPUTS_JSON=$(printf '{"ref":"main","inputs":{"reason":"%s","free_mb":"%d","heart_age_s":"%d","organ_failure":"true"}}' \
                      "$REASONS" "$FREE_MB" "$HEART_AGE_S")
        GH_HTTP=$(curl -sS --max-time 10 \
            -o /dev/null -w "%{http_code}" \
            -X POST \
            -H "Authorization: token $GH_ACTIONS_TOKEN" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/$GH_REPOSITORY/actions/workflows/$HEART_FALLBACK_WORKFLOW/dispatches" \
            -d "$INPUTS_JSON" \
            2>/dev/null)
        GH_HTTP=${GH_HTTP:-NETERR}
        if [ "$GH_HTTP" = "204" ]; then
            log "sepsis protocol dispatched (HTTP $GH_HTTP)"
        else
            warn "sepsis protocol dispatch failed (HTTP $GH_HTTP, expected 204)"
        fi
    else
        warn "GH_ACTIONS_TOKEN unset or curl missing — sepsis protocol not dispatched"
    fi
fi

exit "$ESCALATE"
