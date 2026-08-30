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

LOG = logging.getLogger("heart.visitor_counter_scraper")

# Phase names — every emitted log line + error must include one of these.
PHASE_SCRAPE = "scrape"
PHASE_PUBLISH = "scrape.publish"
PHASE_DEGRADED = "scrape.degraded"

DEFAULT_VENDOR_AUTH = "https://www.freevisitorcounters.com/auth.php"
DEFAULT_STATS_BASE = "https://www.freevisitorcounters.com/en/home/counter"

SHARED_ROOT = Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared"))
WORLDMAP_DATALAYER = SHARED_ROOT / "worldmap" / "datalayers" / "visitors.json"
DASHBOARD_COUNTERS = SHARED_ROOT / "dashboard" / "counters" / "counters.json"
WORLDMAP_FEED = SHARED_ROOT / "worldmap" / "feeds" / "visitor-events.ndjson"
FAIL_COUNTER = SHARED_ROOT / "brain" / "heart" / "visitor-counter.fails"

LINKS_SECRET = Path(
    os.environ.get("NEOHIRO_LINKS_SECRET", "/links-secret/visitor-counters.yaml")
)

REQUEST_TIMEOUT = 8.0
MAX_RETRIES = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_registry() -> list[dict[str, Any]]:
    """Read the scrape registry from /links-secret/visitor-counters.yaml.

    The YAML is intentionally not parsed here — the format is trivial and we
    avoid pulling pyyaml as a hard dependency. Expected schema:

        - id: neohiro.profile
          display_id: "1631162"
          auth_id: "8ce833a4b2722ea505cd7fff9a983daa572877b8"
          label: "neohiro/profile README"

    Lines beginning with `#` and blank lines are ignored.
    """
    if not LINKS_SECRET.exists():
        LOG.error("%s missing — no scrape IDs available", LINKS_SECRET)
        return []
    out: list[dict[str, Any]] = []
    with LINKS_SECRET.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                # crude; we read the whole block under each '- id:' below
                continue
            if line.startswith("id:"):
                # reset accumulator
                out.append({"id": line.split(":", 1)[1].strip()})
                continue
            if line.startswith("display_id:"):
                out[-1]["display_id"] = line.split(":", 1)[1].strip().strip('"')
            elif line.startswith("auth_id:"):
                out[-1]["auth_id"] = line.split(":", 1)[1].strip().strip('"')
            elif line.startswith("label:"):
                out[-1]["label"] = line.split(":", 1)[1].strip().strip('"')
    # Parse as proper YAML instead — fall back to yaml.safe_load for robustness.
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(LINKS_SECRET.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [d for d in data if d.get("auth_id") and d.get("display_id")]
    except Exception as exc:  # noqa: BLE001 — log and continue with the cheap parse
        LOG.warning("%s: yaml fallback failed (%s); using line parse", PHASE_SCRAPE, exc)
    return [c for c in out if c.get("auth_id") and c.get("display_id")]


def fetch_one(counter: dict[str, Any], session: requests.Session) -> dict[str, Any] | None:
    """Hit the vendor's auth endpoint for a single counter.

    Returns the parsed payload or None on failure. Errors include the phase
    name and counter ID so /doctor can route them.
    """
    auth_id = counter["auth_id"]
    url = f"{DEFAULT_VENDOR_AUTH}?id={auth_id}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            payload["_id"] = counter["id"]
            payload["_label"] = counter.get("label", counter["id"])
            payload["_fetched_at"] = _now_iso()
            return payload
        except requests.RequestException as exc:
            LOG.warning(
                "%s: counter %s attempt %d/%d failed: %s",
                PHASE_SCRAPE, counter["id"], attempt, MAX_RETRIES, exc,
            )
        except ValueError as exc:
            LOG.warning(
                "%s: counter %s returned non-JSON (attempt %d): %s",
                PHASE_SCRAPE, counter["id"], attempt, exc,
            )
    return None


def write_datalayer(per_counter: list[dict[str, Any]]) -> None:
    """Roll up country-level hits into the worldmap datalayer format.

    Schema per `neohiro-worldmap/SPEC_ADDENDUM.md § 1`:
        {
          "layer": "visitors",
          "updated_at": "<iso>",
          "countries": [
            {"iso": "US", "hits_24h": 1234, "last_seen_ts": "..."},
            ...
          ]
        }
    """
    country_totals: dict[str, int] = {}
    last_seen: dict[str, str] = {}
    now = _now_iso()

    for c in per_counter:
        for country in c.get("countries", []):
            iso = country.get("iso")
            if not iso:
                continue
            country_totals[iso] = country_totals.get(iso, 0) + int(country.get("hits", 0))
            last_seen[iso] = now

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
    WORLDMAP_DATALAYER.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOG.info("%s: wrote %d countries to %s", PHASE_PUBLISH, len(payload["countries"]), WORLDMAP_DATALAYER)


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
    DASHBOARD_COUNTERS.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_feed(per_counter: list[dict[str, Any]]) -> None:
    """Append per-cycle totals to the NDJSON audit feed (rolled hourly)."""
    WORLDMAP_FEED.parent.mkdir(parents=True, exist_ok=True)
    ts = _now_iso()
    with WORLDMAP_FEED.open("a", encoding="utf-8") as f:
        for c in per_counter:
            row = {
                "ts": ts,
                "id": c["_id"],
                "hits": c.get("hits"),
                "unique_24h": c.get("unique_24h"),
            }
            f.write(json.dumps(row) + "\n")


def bump_fail_counter(delta: int) -> int:
    FAIL_COUNTER.parent.mkdir(parents=True, exist_ok=True)
    cur = 0
    if FAIL_COUNTER.exists():
        try:
            cur = int(FAIL_COUNTER.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            cur = 0
    cur = max(0, cur + delta)
    FAIL_COUNTER.write_text(str(cur), encoding="utf-8")
    return cur


def run_once(args: argparse.Namespace) -> int:
    registry = load_registry()
    if not registry:
        LOG.error("%s: registry empty or missing — aborting cycle", PHASE_SCRAPE)
        bump_fail_counter(1)
        return 2

    session = requests.Session()
    results: list[dict[str, Any]] = []
    failures = 0
    for counter in registry:
        payload = fetch_one(counter, session)
        if payload is None:
            failures += 1
            continue
        results.append(payload)

    if not results:
        LOG.error("%s: every counter failed (failures=%d)", PHASE_DEGRADED, failures)
        bump_fail_counter(1)
        return 3

    try:
        write_datalayer(results)
        write_counters(results)
        append_feed(results)
    except OSError as exc:
        LOG.error("%s: publish failed: %s", PHASE_PUBLISH, exc)
        bump_fail_counter(1)
        return 4

    # Successful cycle — reset fail counter.
    if failures:
        LOG.warning("%s: %d/%d counters failed but at least one succeeded", PHASE_DEGRADED, failures, len(registry))
    bump_fail_counter(0)
    LOG.info("%s: cycle complete — %d/%d counters", PHASE_SCRAPE, len(results), len(registry))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heart visitor-counter scraper")
    parser.add_argument("--once", action="store_true", help="Single cycle then exit (default)")
    parser.add_argument("--loop-seconds", type=int, default=0, help="If >0, loop with this delay")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.loop_seconds > 0:
        while True:
            run_once(args)
            time.sleep(args.loop_seconds)
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())