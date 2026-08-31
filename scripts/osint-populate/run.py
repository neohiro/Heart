"""
osint-populate — Heart dispatcher: refresh OSINT cache.

Sources:
    links/feeds/osint.yaml  — feed registry
    links/feeds/worldmap.yaml — worldmap layer feeds

Outputs (written to /shared/brain/osint/):
    <feed_id>.json     — one file per feed
    cache.json         — merged cache with TTL annotations
    abuse_signals.json — deduplicated abuse signals
    last_updated       — ISO timestamp

Run:
    python run.py --once
    python run.py --quiet --dry-run
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import structlog

# Max concurrent OSINT feed fetches. Most public feeds have no auth and
# tolerate 4-8 concurrent connections from a single IP. Set to 1 for
# deterministic ordering during debugging.
MAX_CONCURRENT_FEEDS = int(os.environ.get("NEOHIRO_OSINT_MAX_CONCURRENT", "6"))
# Substring in feed body that strongly suggests an API error rather than data
ERROR_BODY_MARKERS = ("\"error\":", "\"errors\":", "rate limit", "too many requests", "forbidden")

# Pre-compile the IPv4 regex; reused across every feed.
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
)

sys.path.insert(0, str(Path(__file__).parent.parent / "_lib"))
from heart_dispatch import (  # noqa: E402 — sys.path.insert above must precede
    append_pending,
    atomic_write_json,
    atomic_write_text,
    http_get,
    links_root,
    run_scope,
    utcnow_iso,
    write_last_good,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def load_osint_feeds() -> list[dict]:
    lr = links_root()
    feeds_path = lr / "feeds" / "osint.yaml"
    if not feeds_path.is_file():
        return []
    import yaml
    data = yaml.safe_load(feeds_path.read_text(encoding="utf-8"))
    return data.get("feeds", [])


def load_worldmap_feeds() -> list[dict]:
    lr = links_root()
    feeds_path = lr / "feeds" / "worldmap.yaml"
    if not feeds_path.is_file():
        return []
    import yaml
    data = yaml.safe_load(feeds_path.read_text(encoding="utf-8"))
    return data.get("layers", [])


def _fetch_feed(feed: dict, log: structlog.stdlib.BoundLogger) -> dict:
    """Fetch a single feed and return a result dict.

    The response body is preserved so the Brain can re-process the raw
    payload later. http_get enforces a 5 MiB cap (DEFAULT_MAX_BODY_BYTES)
    so no additional truncation is needed here — trust the library cap.
    """
    url = feed.get("url")
    if not url:
        return {"ok": False, "error": "no url", "feed_id": feed.get("id")}
    feed_id = feed.get("id", "unknown")
    r = http_get(url, timeout=30)
    result = r.to_dict()
    result["feed_id"] = feed_id
    result["name"] = feed.get("name", feed_id)
    result["body"] = r.body  # preserved for Brain re-processing
    # http_get already set body_truncated on the result when the server sent >5 MiB.
    if r.body:
        body_lower = r.body.lower()
        for marker in ERROR_BODY_MARKERS:
            if marker in body_lower:
                result["body_marks_error"] = marker
                break
    return result


def _merge_abuse_signals(feeds: list[dict], results: list[dict]) -> list[dict]:
    """Extract IP addresses from feed responses and emit them as raw signals.

    The Brain is responsible for geo-enrichment, VPN detection, and ASN
    resolution. This dispatcher only extracts and deduplicates the raw IPs
    so the Brain can enrich them in the next phase.

    Currently handles:
    - crt.sh: certificate transparency logs with name_value fields
    - GDELT GeoJSON: IP addresses embedded in document metadata
    - RDAP responses: ip_address fields

    Feeds with keys (VirusTotal, Shodan, AbuseIPDB) return bodies that
    the Brain will parse in a later phase.

    Note: `feeds` is retained for future per-feed extraction hints; currently
    all extraction is feed_id-based via the results.
    """
    signals: dict[str, dict] = {}
    for res in results:
        if not res.get("ok") or not res.get("body"):
            continue
        feed_id = res.get("feed_id", "")
        body: str = res.get("body", "")
        ips_found: set[str] = set()
        if "crt" in feed_id.lower():
            try:
                for cert in json.loads(body)[:200]:
                    name_val = cert.get("name_value", "")
                    for ip in _IPV4_RE.findall(name_val):
                        ips_found.add(ip)
            except (json.JSONDecodeError, ValueError):
                pass
        elif "gdelt" in feed_id.lower() or "geojson" in feed_id.lower():
            for ip in _IPV4_RE.findall(body):
                ips_found.add(ip)
        elif "rdap" in feed_id.lower():
            try:
                data = json.loads(body)
                for entry in (data if isinstance(data, list) else [data]):
                    ip = entry.get("ip_address") or entry.get("handle", "")
                    if ip:
                        ips_found.add(ip)
            except (json.JSONDecodeError, ValueError):
                pass
        for ip in ips_found:
            if ip not in signals:
                signals[ip] = {
                    "ip": ip,
                    "sources": [feed_id],
                    "ts": res.get("ts", ""),
                    "feed_id": feed_id,
                    "url": res.get("url", ""),
                }
            else:
                signals[ip]["sources"].append(feed_id)
                # ISO 8601 Z strings sort lexicographically equal to chronologically
                signals[ip]["ts"] = max(signals[ip]["ts"], res.get("ts", ""))
    return list(signals.values())


def handler(log: structlog.stdlib.BoundLogger, config: dict) -> int:
    flags = config.get("flags", {})
    if flags.get("dry_run"):
        log.info("osint.dry_run", scope="osint-populate")
        return 0

    output_root = Path(os.environ.get("NEOHIRO_OSINT_OUTPUT", "/shared/brain/osint"))
    output_root.mkdir(parents=True, exist_ok=True)
    ts = utcnow_iso()

    osint_feeds = load_osint_feeds()
    worldmap_feeds = load_worldmap_feeds()
    all_feeds = osint_feeds + worldmap_feeds

    log.info("osint.start", osint_feeds=len(osint_feeds), worldmap_feeds=len(worldmap_feeds))

    results: list[dict] = []
    ok_count = 0
    fail_count = 0

    def _fetch_one(feed: dict) -> dict:
        fid = feed.get("id", "?")
        res = _fetch_feed(feed, log)
        res["ts"] = ts  # propagate timestamp so _merge_abuse_signals can use it
        atomic_write_json(output_root / f"{fid}.json", {"ts": ts, **res})
        return res

    # Parallel fetch for throughput (6 workers; feeds are independent GETs)
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FEEDS) as pool:
        futures = {pool.submit(_fetch_one, f): f for f in all_feeds}
        for future in as_completed(futures):
            feed = futures[future]
            fid = feed.get("id", "?")
            try:
                res = future.result()
            except Exception as exc:
                res = {"ok": False, "error": str(exc), "feed_id": fid}
                log.error("osint.feed_exception", feed_id=fid, error=str(exc))
            results.append(res)
            if res.get("ok"):
                ok_count += 1
                log.info("osint.fetched", feed_id=fid, status=res.get("status"), elapsed_ms=res.get("elapsed_ms"))
            else:
                fail_count += 1
                log.warning("osint.fetch_failed", feed_id=fid, error=res.get("error") or f"status={res.get('status')}")
                append_pending(fid, "unreachable", res.get("error") or f"HTTP {res.get('status')}", scope="osint-populate")

    # Write merged cache
    cache = {
        "ts": ts,
        "feeds": len(all_feeds),
        "ok": ok_count,
        "failed": fail_count,
        "results": results,
    }
    atomic_write_json(output_root / "cache.json", cache)

    # Extract and emit abuse signals (IP extraction from raw feed bodies)
    signals = _merge_abuse_signals(all_feeds, results)
    atomic_write_json(output_root / "abuse_signals.json", {"ts": ts, "count": len(signals), "signals": signals})

    write_last_good("osint-populate", "all", {"ts": ts, "ok": ok_count, "failed": fail_count})
    atomic_write_text(output_root / "last_updated", ts + "\n")

    log.info("osint.end", ok=ok_count, failed=fail_count, ts=ts)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(run_scope("osint-populate", handler))
