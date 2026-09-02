"""
Unified dispatcher runner — pulls from a feed registry, extracts, deduplicates,
and writes atomic cache files.

Usage:
    python -m Heart.scripts._lib.runner --scope osint-populate
    python -m Heart.scripts._lib.runner --scope apis-populate --once

Reads: Heart/schedules/REGISTRY.yaml + links/feeds/<topic>.yaml
Writes: /shared/<scope>/cache.json, /shared/<scope>/seen.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import structlog

_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_root / "Heart" / "tools"))
sys.path.insert(0, str(_root))

os.environ.setdefault("NEOHIRO_SHARED_ROOT", "/shared")
os.environ.setdefault("NEOHIRO_LINKS_ROOT", str(_root / "links"))
os.environ.setdefault("NEOHIRO_LINKS_SECRET", str(_root / "links-secret"))

import yaml

from .extract.csv_parser import parse_csv
from .extract.geojson import parse_geojson
from .extract.html_selectolax import extract_html
from .extract.json_api import parse_json_api
from .extract.rss import parse_rss
from .extract.sitemap import parse_sitemap
from .pull.httpx_async import AsyncPuller, PullConfig
from .schema.document import Document, Seen

log = logging.getLogger("heart.runner")

SHARED = Path(os.environ["NEOHIRO_SHARED_ROOT"])
LINKS = Path(os.environ["NEOHIRO_LINKS_ROOT"])

EXTRACTORS = {
    "rss": parse_rss,
    "atom": parse_rss,
    "api": parse_json_api,
    "json": parse_json_api,
    "geojson": parse_geojson,
    "csv": parse_csv,
    "html": extract_html,
    "sitemap": parse_sitemap,
}

FEED_YAML_MAP = {
    "osint-populate": LINKS / "feeds" / "osint.yaml",
    "news-populate": LINKS / "feeds" / "news.yaml",
    "worldmap-populate": LINKS / "feeds" / "geo.yaml",
    "status-populate": LINKS / "feeds" / "status.yaml",
}


def _atomic_write(path: Path, data: list) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.rename(path)


def run_scope(scope: str, feeds: list[dict], output_dir: Path, *, quiet: bool = False) -> dict:
    seen_path = output_dir / "seen.jsonl"
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "cache.json"
    seen = Seen(str(seen_path))
    start = time.monotonic()

    configs = []
    for f in feeds:
        configs.append(
            PullConfig(
                id=f["id"],
                url=f["url"],
                kind=f.get("kind", "api"),
                cadence=f.get("cadence", "every_15_minutes"),
                auth=f.get("auth", "none"),
            )
        )

    results = []
    if asyncio.get_event_loop().is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            for cfg in configs:
                r = _fetch_sync(cfg)
                results.append(r)
    else:
        results = asyncio.run(_run_async(configs))

    docs: list[dict] = []
    errors = []
    for r in results:
        if r.error:
            errors.append({"id": r.id, "error": r.error})
            continue
        if r.status == 304 and r.cached:
            continue
        body = r.body or ""
        extractor = EXTRACTORS.get(r.kind) or parse_json_api
        try:
            extracted = extractor(body, source_id=r.id, url=r.url)
        except Exception as e:
            log.warning("extractor failed id=%s err=%s", r.id, e)
            errors.append({"id": r.id, "error": str(e)})
            continue
        for doc in extracted:
            if seen.add(doc.id):
                docs.append(doc.to_dict())

    _atomic_write(cache_path, docs)
    elapsed = time.monotonic() - start
    log.info("scope=%s docs=%d errors=%d elapsed=%.2fs", scope, len(docs), len(errors), elapsed)
    return {"scope": scope, "docs": len(docs), "errors": len(errors), "elapsed_ms": int(elapsed * 1000)}


def _fetch_sync(cfg: PullConfig):
    import urllib.request
    from datetime import datetime, timezone
    try:
        req = urllib.request.Request(cfg.url, headers={"User-Agent": "neohiro-heart/1.0"})
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            body = resp.read(5 * 1024 * 1024).decode("utf-8", errors="replace")
            return type("R", (), {"id": cfg.id, "url": cfg.url, "kind": cfg.kind,
                                   "status": resp.status, "body": body, "cached": False,
                                   "fetched_at": datetime.now(timezone.utc).isoformat(),
                                   "error": None})()
    except Exception as e:
        from datetime import datetime, timezone
        return type("R", (), {"id": cfg.id, "url": cfg.url, "kind": cfg.kind,
                               "status": 0, "body": None, "cached": False,
                               "fetched_at": datetime.now(timezone.utc).isoformat(),
                               "error": str(e)})()


async def _run_async(configs: list[PullConfig]) -> list:
    async with AsyncPuller(max_concurrency=50) as puller:
        return await puller.fetch_all(configs)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default="osint-populate")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

    feed_file = FEED_YAML_MAP.get(args.scope)
    if not feed_file or not feed_file.exists():
        print(f"Unknown scope or feed file missing: {args.scope}")
        sys.exit(1)

    with open(feed_file) as f:
        registry = yaml.safe_load(f)

    feeds = registry.get("feeds", [])
    output_dir = SHARED / args.scope.replace("-populate", "").replace("_populate", "")
    result = run_scope(args.scope, feeds, output_dir, quiet=args.quiet)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
