"""
tools-populate — Heart dispatcher: refresh the PA tools catalog from public-apis.

Sources:
    public-apis/public-apis (fetched raw README.md)
    private-assistant/tools/tools.yaml (current catalog — diffed)

Outputs (written to /shared/brain/tools/):
    catalog.json        — full 200-entry catalog
    diff.json          — changes vs last run
    changelog.jsonl     — append-only change log
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
from pathlib import Path

import structlog
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "_lib"))
from heart_dispatch import (
    atomic_write_json,
    atomic_write_text,
    http_get,
    run_scope,
    utcnow_iso,
    write_last_good,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PUBLIC_APIS_RAW = "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"

CAT_SLUG = {
    "animals": "animals", "anime": "anime",
    "anti-malware": "osint", "art & design": "art",
    "authentication & authorization": "auth", "blockchain": "blockchain",
    "books": "books", "business": "business",
    "calendar": "calendar", "cloud storage & file sharing": "storage",
    "continuous integration": "dev-tools", "cryptocurrency": "finance",
    "currency exchange": "finance", "data validation": "data-validation",
    "development": "dev-tools", "dictionaries": "data",
    "documents & productivity": "productivity", "email": "comms",
    "entertainment": "entertainment", "environment": "environment",
    "events": "events", "finance": "finance",
    "food & drink": "food", "games & comics": "games",
    "geocoding": "geography", "government": "government",
    "health": "health", "jobs": "jobs",
    "machine learning": "ai", "music": "music",
    "news": "news", "open data": "open-data",
    "open source projects": "open-data", "patent": "legal",
    "personality": "ai", "phone": "comms",
    "photography": "media", "programming": "dev-tools",
    "science & math": "science", "security": "osint",
    "shopping": "shopping", "social": "social",
    "sports & fitness": "sports", "test data": "test-data",
    "text analysis": "ai", "tracking": "osint",
    "transportation": "transport", "url shorteners": "dev-tools",
    "vehicle": "transport", "video": "media",
    "weather": "weather",
}
GEOIP_CATS = {"geography", "geocoding"}
SELECTION_TARGET = 200


def _parse_readme(body: str) -> list[dict]:
    sections = re.split(r"\n(?=### )", body)
    all_entries = []
    for section in sections[1:]:
        lines = section.strip().split("\n")
        m = re.match(r"^###\s+(.+)$", lines[0])
        if not m:
            continue
        cat_name = m.group(1).strip().lower()
        slug = CAT_SLUG.get(cat_name)
        if slug is None:
            continue
        table_started = False
        for line in lines[1:]:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.split("|")]
            # Strip leading/trailing empty cells (trailing pipe creates empty cell)
            while cells and cells[0] == "":
                cells.pop(0)
            while cells and cells[-1] == "":
                cells.pop()
            if len(cells) < 4:
                continue
            if not table_started and cells[0].strip().lower() in ("api", "apis"):
                # Header row: first cell is "API" exactly
                table_started = True
                continue
            api_cell = cells[0]
            desc_cell = cells[1]
            auth_cell = cells[2] if len(cells) > 2 else ""
            https_cell = cells[3] if len(cells) > 3 else ""
            cors_cell = cells[4] if len(cells) > 4 else "Unknown"

            url_m = re.search(r"\((https?://[^\)]+)\)", api_cell)
            name_m = re.search(r"\[([^\]]+)\]", api_cell)
            url = url_m.group(1) if url_m else None
            name = name_m.group(1) if name_m else None
            if not url or url.startswith("#") or https_cell.strip().lower() == "no":
                continue
            if not name:
                continue
            desc = re.sub(r"\s+\|.*", "", desc_cell).strip()
            desc = re.sub(r"\s+", " ", desc)
            name = re.sub(r"\s+", " ", name).strip()
            if not desc:
                continue
            all_entries.append({
                "slug": slug,
                "name": name,
                "url": url,
                "auth": auth_cell.strip().lower(),
                "cors": cors_cell.strip().lower(),
                "desc": desc,
            })
    return all_entries


def _dedupe(entries: list[dict]) -> list[dict]:
    seen_urls, seen_names = set(), set()
    out = []
    for e in entries:
        if e["url"] in seen_urls or e["name"] in seen_names:
            continue
        seen_urls.add(e["url"])
        seen_names.add(e["name"])
        out.append(e)
    return out


def _quality(e: dict) -> int:
    s = 0
    a = str(e.get("auth", "")).lower()
    if a in ("", "no", "none"):
        s += 3
    elif "apikey" in a:
        s += 1
    if e.get("cors", "").lower() == "yes":
        s += 2
    return -s


def _select(entries: list[dict], target: int = SELECTION_TARGET) -> list[dict]:
    by_cat: dict = {}
    for e in entries:
        by_cat.setdefault(e["slug"], []).append(e)
    for cat in by_cat:
        by_cat[cat].sort(key=_quality)
    selected = []
    for cat in sorted(by_cat.keys()):
        take = min(5, len(by_cat[cat]))
        selected.extend(by_cat[cat][:take])
        by_cat[cat] = by_cat[cat][take:]
    for cat in sorted(by_cat.keys()):
        for e in by_cat[cat]:
            if len(selected) >= target:
                break
            selected.append(e)
            by_cat[cat].remove(e)
            if len(selected) >= target:
                break
    return selected


def _auth_block(e: dict) -> tuple[dict, str | None]:
    a = str(e.get("auth", "")).lower()
    if a in ("", "no", "none"):
        return {"kind": "none", "key_location": None, "key_format": None}, None
    if "apikey" in a:
        return {"kind": "apiKey", "key_location": "query", "key_format": "apiKey"}, "X-API-Key"
    if "oauth" in a:
        return {"kind": "oauth", "key_location": "header", "key_format": "Bearer"}, "Authorization"
    return {"kind": "none", "key_location": None, "key_format": None}, None


def _emit_tool(entry: dict, idx: int) -> dict:
    auth, header = _auth_block(entry)
    return {
        "id": f"tool-{idx:05d}",
        "name": entry["name"],
        "description": entry["desc"],
        "topic": [entry["slug"]],
        "nr_prefix": "50" if entry["slug"] in ("osint", "geography") else "10",
        "geoip": entry["slug"] in GEOIP_CATS,
        "endpoint": {
            "url": entry["url"],
            "method": "GET",
            "path_params": [],
            "query_params": {},
            "body": None,
        },
        "auth": auth,
        "auth_header": header,
        "cors": entry["cors"] == "yes",
        "rate_limit": {"per_minute": 60, "note": "fair use"},
        "cache_seconds": 86400 if entry["slug"] in ("open-data", "government", "books", "data-validation") else 3600,
        "sources": ["public-apis"],
    }


def handler(log: structlog.stdlib.BoundLogger, config: dict) -> int:
    flags = config.get("flags", {})
    if flags.get("dry_run"):
        log.info("tools.dry_run", scope="tools-populate")
        return 0

    output_root = Path(os.environ.get("NEOHIRO_TOOLS_OUTPUT", "/shared/brain/tools"))
    output_root.mkdir(parents=True, exist_ok=True)
    ts = utcnow_iso()

    # Load current catalog for diff
    current_catalog_path = SCRIPT_DIR.parent.parent / "tools" / "tools.yaml"
    current_tools = []
    current_ids: set = set()
    current_urls: set = set()
    if current_catalog_path.is_file():
        try:
            data = yaml.safe_load(current_catalog_path.read_text(encoding="utf-8"))
            for t in data.get("tools", []):
                current_tools.append(t)
                current_ids.add(t["id"])
                current_urls.add(t.get("endpoint", {}).get("url", ""))
        except Exception as e:
            log.warning("tools.load_current_failed", path=str(current_catalog_path), error=str(e))

    # Fetch public-apis README
    log.info("tools.fetch_readme", url=PUBLIC_APIS_RAW)
    r = http_get(PUBLIC_APIS_RAW, timeout=30)
    if not r.ok:
        log.error("tools.fetch_failed", url=PUBLIC_APIS_RAW, error=r.error)
        return 1

    raw = _parse_readme(r.body)
    log.info("tools.parsed", raw=len(raw))
    deduped = _dedupe(raw)
    log.info("tools.deduped", count=len(deduped))
    selected = _select(deduped)
    log.info("tools.selected", count=len(selected))

    # Emit new tools
    new_tools = [_emit_tool(e, i + 1) for i, e in enumerate(selected)]
    new_catalog = {
        "schema_version": 1,
        "total_tools": len(new_tools),
        "last_updated": ts,
        "sources": ["public-apis/public-apis (MIT)", "n0shake/Public-APIs (MIT)"],
        "tools": new_tools,
    }

    # Compute diff
    new_urls = {t.get("endpoint", {}).get("url", "") for t in new_tools}
    added_urls = new_urls - current_urls
    removed_urls = current_urls - new_urls
    diff = {
        "added": len(added_urls),
        "removed": len(removed_urls),
        "total_new": len(new_tools),
        "total_current": len(current_tools),
    }

    # Write outputs
    atomic_write_json(output_root / "catalog.json", new_catalog)
    atomic_write_json(output_root / "diff.json", {"ts": ts, **diff})
    changelog_path = output_root / "changelog.jsonl"
    changelog_entry = {"ts": ts, "total": len(new_tools), "diff": diff}
    with changelog_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(changelog_entry) + "\n")
    atomic_write_text(output_root / "last_updated", ts + "\n")

    # Write to repo (idempotent — only if different)
    if new_catalog["tools"] != current_tools:
        try:
            repo_path = SCRIPT_DIR.parent.parent / "tools" / "tools.yaml"
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(repo_path, yaml.safe_dump(new_catalog, sort_keys=False, allow_unicode=True, width=4096))
            log.info("tools.wrote_repo", path=str(repo_path))
        except Exception as e:
            log.warning("tools.write_repo_failed", path=str(current_catalog_path), error=str(e))

    write_last_good("tools-populate", "all", {"ts": ts, **diff})
    log.info("tools.end", total=len(new_tools), added=diff["added"], removed=diff["removed"], ts=ts)
    return 0


if __name__ == "__main__":
    sys.exit(run_scope("tools-populate", handler))
