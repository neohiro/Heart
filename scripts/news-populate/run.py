"""
news-populate — Heart dispatcher: refresh news feed cache.

Sources:
    news/FEEDS.md (canonical feed registry)

Outputs (written to /shared/news/):
    hackernews.json     — HN top stories (30 items)
    mastodon.json      — Mastodon public timeline (last 20)
    bluesky.json       — Bluesky timeline (last 20)
    crtsh.json         — crt.sh cert transparency scan (top 50 certs)
    statusfeeds.json   — aggregated status page RSS (github, cloudflare, etc.)
    google-news.json   — Google News RSS per topic (10 topics)
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import structlog

# HN timeline is fetched then 20 individual items are fetched in parallel.
# HN's firebase endpoint tolerates 4-8 concurrent connections.
HN_ITEM_WORKERS = int(os.environ.get("NEOHIRO_HN_ITEM_WORKERS", "8"))
HN_ITEM_COUNT = 20
HN_IDS_TO_FETCH = 30  # Fetch this many IDs then take the first HN_ITEM_COUNT that succeed
MASTODON_LIMIT = 20
BLSKY_LIMIT = 20

sys.path.insert(0, str(Path(__file__).parent.parent / "_lib"))
from heart_dispatch import (  # noqa: E402 — sys.path.insert above must precede
    atomic_write_json,
    atomic_write_text,
    http_get,
    run_scope,
    utcnow_iso,
    write_last_good,
)

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_FEEDS = SCRIPT_DIR.parent.parent.parent / "news" / "FEEDS.md"


# Pre-compile the RSS / Atom patterns. Building these per-feed is wasteful
# and shows up in flame-graphs on air-gapped nodes with hundreds of feeds.
_RSS_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.DOTALL | re.IGNORECASE)
_RSS_TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.DOTALL)
_RSS_LINK_RE = re.compile(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", re.DOTALL)
_RSS_DATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.DOTALL)
_RSS_DESC_RE = re.compile(r"<description>(.*?)</description>", re.DOTALL)
_ATOM_ENTRY_RE = re.compile(r"<entry\b[^>]*>(.*?)</entry>", re.DOTALL | re.IGNORECASE)
_ATOM_TITLE_RE = re.compile(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.DOTALL)
_ATOM_LINK_RE = re.compile(r'<link[^>]*href=["\'](https?://[^"\']+)["\']')
_ATOM_UPDATED_RE = re.compile(r"<updated>(.*?)</updated>", re.DOTALL)
_ATOM_PUBLISHED_RE = re.compile(r"<published>(.*?)</published>", re.DOTALL)
_ATOM_SUMMARY_RE = re.compile(r"<summary[^>]*>(.*?)</summary>", re.DOTALL)
_ATOM_CONTENT_RE = re.compile(r"<content[^>]*>(.*?)</content>", re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _resolve_feeds() -> dict:
    """Parse FEEDS.md into a structured dict."""
    text = REPO_FEEDS.read_text(encoding="utf-8")
    sections: dict = {}
    current = None
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.startswith("| ") and "---" not in line:
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 2 and cells[0] != "Name":
                sections[current].append(cells)
    return sections


def _fetch_hackernews(log: structlog.stdlib.BoundLogger) -> dict:
    r = http_get("https://hacker-news.firebaseio.com/v0/topstories.json")
    if not r.ok:
        return {"ok": False, "error": r.error or f"status={r.status}"}
    ids = json.loads(r.body)[:HN_IDS_TO_FETCH]

    def _fetch_item(bid: int) -> dict | None:
        ir = http_get(f"https://hacker-news.firebaseio.com/v0/item/{bid}.json", timeout=10)
        if ir.ok and ir.body:
            try:
                return json.loads(ir.body)
            except json.JSONDecodeError:
                pass
        return None

    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=HN_ITEM_WORKERS) as pool:
        for result in pool.map(_fetch_item, ids):
            if result is not None:
                items.append(result)
                if len(items) >= HN_ITEM_COUNT:
                    break
    return {"ok": True, "count": len(items), "items": items[:HN_ITEM_COUNT]}


def _fetch_mastodon(log: structlog.stdlib.BoundLogger) -> dict:
    # Mastodon API requires lowercase header names (RFC 7540 §8.1.2)
    r = http_get(
        "https://mastodon.social/api/v1/timelines/public",
        headers={"limit": str(MASTODON_LIMIT)},
        timeout=20,
    )
    if not r.ok:
        return {"ok": False, "error": r.error or f"status={r.status}"}
    try:
        items = json.loads(r.body)
        return {"ok": True, "count": len(items), "items": items[:MASTODON_LIMIT]}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"parse error: {e}"}


def _fetch_bluesky(log: structlog.stdlib.BoundLogger) -> dict:
    r = http_get(
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getTimeline",
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if not r.ok:
        return {"ok": False, "error": r.error or f"status={r.status}"}
    try:
        data = json.loads(r.body)
        feed = data.get("feed", [])
        items = [f.get("post", {}) for f in feed[:BLSKY_LIMIT]]
        return {"ok": True, "count": len(items), "items": items}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"parse error: {e}"}


def _fetch_crtsh(log: structlog.stdlib.BoundLogger) -> dict:
    # Generic scan of .com to see what certs are issued (non-specific)
    r = http_get(
        "https://crt.sh/?q=%.com&output=json",
        timeout=30,
    )
    if not r.ok:
        return {"ok": False, "error": r.error or f"status={r.status}"}
    try:
        items = json.loads(r.body)[:50]
        return {
            "ok": True,
            "count": len(items),
            "items": [{"name": i.get("name_value", ""), "issuer": i.get("issuer_ca_id", ""), "not_before": i.get("not_before", "")} for i in items],
        }
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"parse error: {e}"}


def _strip(html: str) -> str:
    """Strip CDATA wrappers and HTML tags from descriptions."""
    cdata = _CDATA_RE.search(html)
    if cdata:
        html = cdata.group(1)
    html = _TAG_STRIP_RE.sub("", html)
    # Decode common HTML entities. Full unescape via html.unescape would be
    # more thorough, but stdlib import is enough for the most common cases
    # seen in Statuspage feeds.
    html = (html
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&apos;", "'"))
    return html.strip()[:200]


def _parse_rss(body: str | None, max_items: int = 5) -> list[dict]:
    """Extract a compact summary of recent items from an RSS/Atom feed.

    Returns at most `max_items` items, each with `title`, `link`, `pubDate`,
    and `description` (truncated to 200 chars). This is a best-effort parser
    that handles both RSS 2.0 <item> elements and Atom <entry> elements.
    Errors are silently absorbed (returns []), since the caller already
    tracks HTTP-level failures.
    """
    if not body:
        return []
    items: list[dict] = []

    # RSS 2.0: <item><title>...</title><link>...</link><pubDate>...</pubDate><description>...</description></item>
    for m in _RSS_ITEM_RE.finditer(body):
        block = m.group(1)
        title_m = _RSS_TITLE_RE.search(block)
        link_m = _RSS_LINK_RE.search(block)
        date_m = _RSS_DATE_RE.search(block)
        desc_m = _RSS_DESC_RE.search(block)
        items.append({
            "title": (title_m.group(1) if title_m else "").strip(),
            "link": (link_m.group(1) if link_m else "").strip(),
            "pubDate": (date_m.group(1) if date_m else "").strip(),
            "description": _strip(desc_m.group(1)) if desc_m else "",
        })
        if len(items) >= max_items:
            return items

    # Atom: <entry><title>...</title><link href="..."/></entry>
    for m in _ATOM_ENTRY_RE.finditer(body):
        block = m.group(1)
        title_m = _ATOM_TITLE_RE.search(block)
        link_m = _ATOM_LINK_RE.search(block)
        date_m = _ATOM_UPDATED_RE.search(block) or _ATOM_PUBLISHED_RE.search(block)
        desc_m = _ATOM_SUMMARY_RE.search(block) or _ATOM_CONTENT_RE.search(block)
        items.append({
            "title": (title_m.group(1) if title_m else "").strip(),
            "link": (link_m.group(1) if link_m else "").strip(),
            "pubDate": (date_m.group(1) if date_m else "").strip(),
            "description": _strip(desc_m.group(1)) if desc_m else "",
        })
        if len(items) >= max_items:
            break

    return items


def _fetch_status_feeds(log: structlog.stdlib.BoundLogger) -> dict:
    # Real RSS feed endpoints (verified 2026-08-30). Each provider publishes
    # an Atom/RSS feed at a stable URL; Statuspage-hosted providers share the
    # pattern https://<status-domain>/history.rss and the JSON feed at /api/v2/...
    status_pages = [
        ("github",      "https://www.githubstatus.com/history.rss"),
        ("cloudflare",  "https://www.cloudflarestatus.com/history.rss"),
        ("aws",         "https://status.aws.amazon.com/rss/all.rss"),
        ("tailscale",   "https://status.tailscale.com/history.rss"),
        ("openai",      "https://status.openai.com/history.rss"),
        ("google_ws",   "https://status.cloud.google.com/incidents.rss"),
        ("do",          "https://status.digitalocean.com/history.rss"),
        ("hf",          "https://status.huggingface.co/history.rss"),
    ]
    results = {}

    def _check(name: str, url: str) -> tuple[str, dict]:
        r = http_get(url, timeout=15)
        info: dict = {
            "ok": r.ok,
            "status": r.status,
            "elapsed_ms": r.elapsed_ms,
            "url": r.url,
        }
        if r.ok and r.body:
            try:
                parsed = _parse_rss(r.body)
                info["items"] = parsed
                info["item_count"] = len(parsed)
            except Exception as e:
                # Parse failure must not flip ok=False (HTTP succeeded)
                info["parse_error"] = str(e)
        return name, info

    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, info in pool.map(lambda p: _check(*p), status_pages):
            results[name] = info
    return results


def handler(log: structlog.stdlib.BoundLogger, config: dict) -> int:
    flags = config.get("flags", {})
    if flags.get("dry_run"):
        log.info("news.dry_run", scope="news-populate")
        return 0

    output_dir = Path(os.environ.get("NEOHIRO_NEWS_OUTPUT", "/shared/news"))
    output_dir.mkdir(parents=True, exist_ok=True)

    log.info("news.start", feeds=str(REPO_FEEDS))
    ts = utcnow_iso()
    ok_count = 0
    fail_count = 0

    # All 5 fetchers are independent — run them in parallel.
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_fetch_hackernews, log): "hackernews",
            pool.submit(_fetch_mastodon, log): "mastodon",
            pool.submit(_fetch_bluesky, log): "bluesky",
            pool.submit(_fetch_crtsh, log): "crtsh",
            pool.submit(_fetch_status_feeds, log): "statusfeeds",
        }
        for future in futures:
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}

            atomic_write_json(output_dir / f"{source}.json", {"ts": ts, **result})
            if source == "statusfeeds":
                # statusfeeds is a dict of provider -> info; each provider
                # counts as one feed in the ok/fail tally.
                ok_count += sum(1 for v in result.values() if v.get("ok"))
                fail_count += sum(1 for v in result.values() if not v.get("ok"))
            elif result.get("ok"):
                ok_count += 1
                log.info("news.fetched", source=source, count=result.get("count"))
            else:
                fail_count += 1
                log.warning("news.fetch_failed", source=source, error=result.get("error"))

    write_last_good("news-populate", "all", {"ts": ts, "ok_count": ok_count, "fail_count": fail_count})
    atomic_write_text(output_dir / "last_updated", ts + "\n")

    log.info("news.end", ok=ok_count, failed=fail_count, ts=ts)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(run_scope("news-populate", handler))
