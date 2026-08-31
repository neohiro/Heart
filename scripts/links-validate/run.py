"""
links-validate — Heart dispatcher: validate all public + private links.

Sources:
    links/feeds/*.yaml       — public feed registry (links SSOT)
    links-secret/            — private links (never fetched here)
    links/audit/pending.yaml — lazy-update queue

Outputs (written to /shared/links/):
    validation.json          — per-link status + metadata
    broken.json              — links that returned non-2xx
    pending.json             — links queued for lazy-update
    last_updated             — ISO timestamp

Run:
    python run.py --once
    python run.py --quiet --dry-run
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "_lib"))
from heart_dispatch import (
    append_pending,
    atomic_write_json,
    atomic_write_text,
    http_get,
    links_root,
    links_secret_root,
    run_scope,
    utcnow_iso,
    write_last_good,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def collect_all_links(log: structlog.stdlib.BoundLogger) -> list[dict]:
    lr = links_root()
    lsr = links_secret_root()
    links: list[dict] = []

    # Public feeds
    feeds_dir = lr / "feeds"
    if feeds_dir.is_dir():
        for fpath in feeds_dir.glob("*.yaml"):
            if fpath.name == "pending.yaml":
                continue
            try:
                data = yaml.safe_load(fpath.read_text(encoding="utf-8"))
                # Each feed file has a `feeds:` or `layers:` list
                for key in ("feeds", "layers"):
                    items = data.get(key, [])
                    for item in items:
                        url = item.get("url")
                        if url:
                            item.setdefault("source", f"public:{fpath.name}")
                            links.append(item)
            except Exception as e:
                log.warning("links.skip_file", path=str(fpath), error=str(e))

    # Specs
    specs_path = feeds_dir / "specs.yaml"
    if specs_path.is_file():
        try:
            data = yaml.safe_load(specs_path.read_text(encoding="utf-8"))
            for spec in data.get("specs", []):
                url = spec.get("url")
                if url:
                    spec.setdefault("source", "public:specs.yaml")
                    links.append(spec)
        except Exception as e:
            log.warning("links.skip_file", path=str(specs_path), error=str(e))

    # Private per-user bookmarks (no fetch — just track)
    # SECURITY: do NOT follow symlinks. A symlinked bookmark.yaml pointing
    # at /etc/passwd would be opened; the real path may be world-readable
    # but the contents are still scraped. Use os.walk(follow_symlinks=False)
    # and skip any symlink encountered.
    bm_dir = lsr / "per-user"
    if bm_dir.is_dir():
        bm_paths: list[Path] = []
        for root, dirs, files in os.walk(bm_dir, followlinks=False):
            # Prune symlinked directories to avoid the same risk one level down
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for fname in files:
                fp = Path(root) / fname
                if fp.is_symlink():
                    log.warning("links.skip_symlink", path=str(fp))
                    continue
                if fname == "bookmark.yaml":
                    bm_paths.append(fp)
        for bm_path in bm_paths:
            try:
                data = yaml.safe_load(bm_path.read_text(encoding="utf-8"))
                # Bookmark entries have url field
                for bm in data.get("bookmarks", []):
                    url = bm.get("url")
                    if url:
                        bm.setdefault("source", f"private:{bm_path.relative_to(lsr)}")
                        links.append(bm)
            except Exception as e:
                log.warning("links.skip_file", path=str(bm_path), error=str(e))

    return links


def validate_link(link: dict, log: structlog.stdlib.BoundLogger) -> dict:
    url = link.get("url")
    if not url:
        return {"ok": False, "error": "no url", "link": link}

    feed_id = link.get("id", "")
    r = http_get(url, timeout=15)
    return {
        "ok": 200 <= r.status < 400,
        "status": r.status,
        "url": url,
        "feed_id": feed_id,
        "name": link.get("name", feed_id),
        "source": link.get("source", ""),
        "elapsed_ms": r.elapsed_ms,
        "error": r.error,
        "ts": utcnow_iso(),
    }


def handler(log: structlog.stdlib.BoundLogger, config: dict) -> int:
    flags = config.get("flags", {})
    if flags.get("dry_run"):
        log.info("links.dry_run", scope="links-validate")
        return 0

    output_root = Path(os.environ.get("NEOHIRO_LINKS_OUTPUT", "/shared/links"))
    output_root.mkdir(parents=True, exist_ok=True)
    ts = utcnow_iso()

    links = collect_all_links(log)
    log.info("links.start", total=len(links))

    results = []
    broken = []
    ok_count = 0
    fail_count = 0

    for link in links:
        fid = link.get("id", link.get("url", "?"))
        res = validate_link(link, log)
        results.append(res)

        if res["ok"]:
            ok_count += 1
        else:
            fail_count += 1
            broken.append(res)
            log.warning("links.broken", feed_id=fid, url=link.get("url", ""), status=res.get("status"), error=res.get("error"))
            append_pending(fid, "broken", f"HTTP {res.get('status', 0)} — {res.get('error', '')}", scope="links-validate")

    # Write results
    atomic_write_json(output_root / "validation.json", {"ts": ts, "total": len(results), "ok": ok_count, "failed": fail_count, "results": results})
    atomic_write_json(output_root / "broken.json", {"ts": ts, "count": len(broken), "links": broken})

    write_last_good("links-validate", "all", {"ts": ts, "ok": ok_count, "failed": fail_count})
    atomic_write_text(output_root / "last_updated", ts + "\n")

    log.info("links.end", ok=ok_count, failed=fail_count, ts=ts)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(run_scope("links-validate", handler))
