# Heart/tools/visitor_counter_scraper.py
#
# Phase: scrape (per AGENTS.md Rule 5 — every error must include a phase).
#
# Polls freevisitorcounters.com's authenticated stats endpoint for every
# counter registered in links-secret/visitor-counters.yaml, then writes:
#   1. /shared/worldmap/datalayers/visitors.json    — country-level aggregates
#   2. /shared/dashboard/counters/counters.json    — per-counter live totals
#   3. /shared/worldmap/feeds/visitor-events.ndjson — append-only audit log
#
# Schedule: every 5 minutes (see Heart/schedules/REGISTRY.yaml -> visitor-counter).
# Failure handling: see failure_policy block in REGISTRY.yaml; demote after 5,
# pause after 20 consecutive failures.
#
# Secrets: the scrape IDs live in /links-secret/visitor-counters.yaml — this
# script NEVER touches a public repo or committed YAML.

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import structlog

LOG = structlog.get_logger("heart.visitor_counter_scraper")

# Configure structlog to route through stdlib logging so caplog + log aggregators
# see the events. Idempotent: repeated imports are no-ops via already_configured.
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.KeyValueRenderer(
            key_order=["event", "phase", "counter_id", "error"],
        ),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

# Phase names — every emitted log line + error must include one of these.
PHASE_SCRAPE = "scrape"
PHASE_PUBLISH = "scrape.publish"
PHASE_DEGRADED = "scrape.degraded"

DEFAULT_VENDOR_AUTH = "https://www.freevisitorcounters.com/auth.php"

SHARED_ROOT = Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared"))
WORLDMAP_DATALAYER = SHARED_ROOT / "worldmap" / "datalayers" / "visitors.json"
DASHBOARD_COUNTERS = SHARED_ROOT / "dashboard" / "counters" / "counters.json"
WORLDMAP_FEED = SHARED_ROOT / "worldmap" / "feeds" / "visitor-events.ndjson"
FAIL_COUNTER = SHARED_ROOT / "brain" / "heart" / "visitor-counter.fails"

LINKS_SECRET = Path(
    os.environ.get("NEOHIRO_LINKS_SECRET", "/links-secret/visitor-counters.yaml")
)

REQUEST_TIMEOUT = 8.0
# Per-counter wall-clock budget. Two attempts (each â‰¤REQUEST_TIMEOUT) plus a
# small overhead must fit inside this, or we abandon the counter and move on.
# Worst case for 12 counters then becomes 12 Ã— PER_COUNTER_DEADLINE, not
# 12 Ã— MAX_RETRIES Ã— REQUEST_TIMEOUT, so a single degraded vendor can't
# stall the next 5-min cycle.
PER_COUNTER_DEADLINE = 4.0
MAX_RETRIES = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_session() -> requests.Session:
    """One Session per process; connection-pooled, single User-Agent."""
    s = requests.Session()
    s.headers["User-Agent"] = "neohiro-Heart/1.0 (+https://neohiro.github.io)"
    return s


def load_registry() -> list[dict[str, Any]]:
    """Read the scrape registry from /links-secret/visitor-counters.yaml.

    Parsed with yaml.safe_load when available; falls back to a minimal line-based
    parser if yaml is not installed or parsing fails. Both paths return only
    entries that have both auth_id and display_id.

    Expected YAML schema:
        - id: neohiro.profile
          display_id: "1631162"
          auth_id: "8ce833a4b2722ea505cd7fff9a983daa572877b8"
          label: "neohiro/profile README"
    """
    if not LINKS_SECRET.exists():
        LOG.error("registry_missing", path=str(LINKS_SECRET))
        return []

    text = LINKS_SECRET.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        if isinstance(data, list):
            return [d for d in data if d.get("auth_id") and d.get("display_id")]
    except Exception:  # noqa: BLE001 — fall through to line parser
        pass

    # Line-based fallback: handles registries without pyyaml or malformed YAML.
    out: list[dict[str, Any]] = []
    pending: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- id:") or line.startswith("id:"):
            if pending.get("auth_id") and pending.get("display_id"):
                out.append(pending)
            elif pending.get("id"):
                LOG.warning("incomplete_entry_dropped", phase=PHASE_SCRAPE, counter_id=pending.get("id"))
            val = line.split(":", 1)[1].strip()
            pending = {"id": val.strip('"')}
            continue
        if line.startswith("display_id:"):
            pending["display_id"] = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("auth_id:"):
            pending["auth_id"] = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("label:"):
            pending["label"] = line.split(":", 1)[1].strip().strip('"')
    if pending.get("auth_id") and pending.get("display_id"):
        out.append(pending)
    elif pending.get("id"):
        LOG.warning("incomplete_entry_dropped", phase=PHASE_SCRAPE, counter_id=pending.get("id"))
    return [c for c in out if c.get("auth_id") and c.get("display_id")]


def fetch_one(
    counter: dict[str, Any],
    session: requests.Session,
    deadline_s: float = PER_COUNTER_DEADLINE,
) -> dict[str, Any] | None:
    """Hit the vendor's auth endpoint for a single counter.

    Returns the parsed payload or None on failure. Errors include the phase
    name and counter ID so /doctor can route them.

    The full retry budget is bounded by `deadline_s` so a single counter
    can never consume more wall-clock than that, regardless of how many
    individual timeouts occur.
    """
    import time as _time
    auth_id = counter["auth_id"]
    url = f"{DEFAULT_VENDOR_AUTH}?id={auth_id}"
    deadline = _time.monotonic() + deadline_s
    last_exc: str = ""
    for attempt in range(1, MAX_RETRIES + 1):
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            LOG.warning(
                "counter_deadline_exhausted",
                phase=PHASE_SCRAPE, counter_id=counter["id"],
                deadline_s=deadline_s, last_error=last_exc,
            )
            return None
        try:
            r = session.get(url, timeout=(min(remaining, REQUEST_TIMEOUT), REQUEST_TIMEOUT))
            r.raise_for_status()
            payload = r.json()
            payload["_id"] = counter["id"]
            payload["_label"] = counter.get("label", counter["id"])
            payload["_fetched_at"] = _now_iso()
            return payload
        except requests.RequestException as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
            LOG.warning(
                "counter_attempt_failed",
                phase=PHASE_SCRAPE, counter_id=counter["id"],
                attempt=attempt, max_retries=MAX_RETRIES, error=str(exc),
            )
        except ValueError as exc:
            last_exc = f"{type(exc).__name__}: {exc}"
            LOG.warning(
                "counter_non_json_response",
                phase=PHASE_SCRAPE, counter_id=counter["id"],
                attempt=attempt, error=str(exc),
            )
    return None


def aggregate_countries(
    per_counter: list[dict[str, Any]],
    now: str | None = None,
) -> tuple[dict[str, int], dict[str, str], str]:
    """Pure aggregation: roll up per-counter country hits into totals.

    Returns (country_totals, last_seen, timestamp) where:
      country_totals — {iso: total_hits}
      last_seen    — {iso: last_seen_ts}  (all set to `now`)
      timestamp    — the `now` value used (ISO or generated)

    No I/O, no filesystem access. Fully testable in isolation.
    """
    ts = now or _now_iso()
    country_totals: dict[str, int] = {}
    last_seen: dict[str, str] = {}
    for c in per_counter:
        for country in c.get("countries", []):
            iso = country.get("iso")
            if not iso:
                continue
            country_totals[iso] = country_totals.get(iso, 0) + int(country.get("hits") or 0)
            last_seen[iso] = ts
    return country_totals, last_seen, ts


def write_datalayer(per_counter: list[dict[str, Any]]) -> None:
    """Roll up country-level hits into the worldmap datalayer format.

    Schema per `neohiro-worldmap/SPEC_ADDENDUM.md Â§ 1`:
        {
          "layer": "visitors",
          "updated_at": "<iso>",
          "countries": [
            {"iso": "US", "hits_24h": 1234, "last_seen_ts": "..."},
            ...
          ]
        }
    """
    country_totals, last_seen, now = aggregate_countries(per_counter)
    payload = {
        "layer": "visitors",
        "updated_at": now,
        "source": "freevisitorcounters",
        "countries": [
            {"iso": iso, "hits_24h": hits, "last_seen_ts": last_seen[iso]}
            for iso, hits in sorted(country_totals.items(), key=lambda kv: -kv[1])
        ],
    }
    WORLDMAP_DATALAYER.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp + rename. Survives partial writes if the process
    # crashes mid-write — readers always see either the old or new file.
    tmp = WORLDMAP_DATALAYER.with_suffix(WORLDMAP_DATALAYER.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(WORLDMAP_DATALAYER)
    LOG.info("wrote_datalayer", phase=PHASE_PUBLISH, countries=len(payload["countries"]), path=str(WORLDMAP_DATALAYER))


def write_counters(per_counter: list[dict[str, Any]]) -> None:
    """Write per-counter totals for the dashboard /api/visitor-counter endpoint."""
    payload = {
        "updated_at": _now_iso(),
        "counters": [
            {
                "id": c["_id"],
                "label": c.get("_label"),
                "hits": c.get("hits"),
                "unique_24h": c.get("unique_24h"),
                "online": c.get("online"),
            }
            for c in per_counter
        ],
    }
    DASHBOARD_COUNTERS.parent.mkdir(parents=True, exist_ok=True)
    tmp = DASHBOARD_COUNTERS.with_suffix(DASHBOARD_COUNTERS.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(DASHBOARD_COUNTERS)


def append_feed(per_counter: list[dict[str, Any]]) -> None:
    """Append per-cycle totals to the NDJSON audit feed (rolled hourly)."""
    WORLDMAP_FEED.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    written = 0
    with WORLDMAP_FEED.open("a", encoding="utf-8") as f:
        for c in per_counter:
            row = {
                "ts": ts,
                "id": c["_id"],
                "hits": c.get("hits"),
                "unique_24h": c.get("unique_24h"),
            }
            f.write(json.dumps(row) + "\n")
            written += 1
    LOG.info("appended_feed_rows", phase=PHASE_PUBLISH, rows=written, path=str(WORLDMAP_FEED))


def bump_fail_counter(delta: int) -> int:
    FAIL_COUNTER.parent.mkdir(parents=True, exist_ok=True)
    cur = 0
    if FAIL_COUNTER.exists():
        try:
            cur = int(FAIL_COUNTER.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            cur = 0
    if delta == 0 and cur > 0:
        # Explicit reset on success.
        FAIL_COUNTER.write_text("0", encoding="utf-8")
        return 0
    new_val = max(0, cur + delta)
    if new_val != cur:
        FAIL_COUNTER.write_text(str(new_val), encoding="utf-8")
    return new_val


FAIL_EVENTS = SHARED_ROOT / "brain" / "heart" / "visitor-counter.events.ndjson"
FAIL_WINDOW_SECONDS = 30 * 60


def record_cycle_event(ok: bool) -> None:
    """Append a per-cycle outcome to the rolling audit log.

    Each line is `{"ts": "<iso>", "ok": true|false, "fails": N}`. Doctor H-08
    reads this with a 30-min window so it can surface rate-based findings
    ("3 fails in 7 min") rather than the current-snapshot counter.
    """
    FAIL_EVENTS.parent.mkdir(parents=True, exist_ok=True)
    # Read the current counter so the log row carries the streak at that
    # point. Defensive: a missing or corrupted counter is treated as 0.
    fails_value = 0
    if FAIL_COUNTER.exists():
        try:
            fails_value = int(FAIL_COUNTER.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            fails_value = 0
    row = {
        "ts": _now_iso(),
        "ok": bool(ok),
        "fails": fails_value,
    }
    with FAIL_EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def read_fail_window(window_s: int = FAIL_WINDOW_SECONDS) -> list[dict[str, Any]]:
    """Read cycle events from the last `window_s` seconds.

    Returns a list of {ts, ok, fails} dicts in chronological order.
    Drops malformed lines silently — they're audit data, not user input.
    Returns an empty list if the log file does not yet exist.
    """
    if not FAIL_EVENTS.exists():
        return []
    try:
        text = FAIL_EVENTS.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("event_log_read_failed", phase=PHASE_SCRAPE, error=str(exc))
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - window_s
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "ts" not in row or "ok" not in row:
            # Malformed line: required fields missing.
            continue
        try:
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts.timestamp() < cutoff:
            continue
        out.append(row)
    return out


def run_once(args: argparse.Namespace) -> int:
    registry = load_registry()
    if not registry:
        LOG.error("registry_empty_abort", phase=PHASE_SCRAPE)
        bump_fail_counter(1)
        record_cycle_event(ok=False)
        return 2

    session = _build_session()
    try:
        results: list[dict[str, Any]] = []
        failures = 0
        for counter in registry:
            payload = fetch_one(counter, session, PER_COUNTER_DEADLINE)
            if payload is None:
                failures += 1
                continue
            results.append(payload)
    finally:
        session.close()

    if not results:
        LOG.error("all_counters_failed", phase=PHASE_DEGRADED, failures=failures)
        bump_fail_counter(1)
        record_cycle_event(ok=False)
        return 3

    try:
        write_datalayer(results)
        write_counters(results)
        append_feed(results)
    except OSError as exc:
        LOG.error("publish_failed", phase=PHASE_PUBLISH, error=str(exc))
        bump_fail_counter(1)
        record_cycle_event(ok=False)
        return 4

    # Successful cycle — reset fail counter.
    if failures:
        LOG.warning("partial_failure", phase=PHASE_DEGRADED, failures=failures, total=len(registry))
    bump_fail_counter(0)
    record_cycle_event(ok=True)
    LOG.info("cycle_complete", phase=PHASE_SCRAPE, succeeded=len(results), total=len(registry))
    return 0


def health_check() -> int:
    """Live smoke test: hit the vendor auth endpoint and exit 0 on HTTP 200.

    Use as a deploy verification step:
        python visitor_counter_scraper.py --health-check

    Returns 0 on success, 2 if the registry is empty, 3 on any network failure.
    Does not write to /shared — read-only.
    """
    registry = load_registry()
    if not registry:
        LOG.error("health_check_no_registry", phase=PHASE_SCRAPE)
        return 2
    counter = registry[0]
    session = _build_session()
    try:
        payload = fetch_one(counter, session, PER_COUNTER_DEADLINE)
    finally:
        session.close()
    if payload is None:
        LOG.error("health_check_fetch_failed", phase=PHASE_SCRAPE, counter_id=counter["id"])
        return 3
    if not isinstance(payload, dict) or not payload:
        LOG.error("health_check_empty_payload", phase=PHASE_SCRAPE, counter_id=counter["id"])
        return 3
    LOG.info("health_check_ok", phase=PHASE_SCRAPE, counter_id=counter["id"], keys=len(payload))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heart visitor-counter scraper")
    parser.add_argument("--once", action="store_true", help="Single cycle then exit (default)")
    parser.add_argument("--loop-seconds", type=int, default=0, help="If >0, loop with this delay")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--health-check", action="store_true",
        help="Live smoke test: hit vendor API for the first registry counter; exit 0 on HTTP 200",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.health_check:
        return health_check()
    if args.loop_seconds > 0:
        while True:
            run_once(args)
            time.sleep(args.loop_seconds)
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())