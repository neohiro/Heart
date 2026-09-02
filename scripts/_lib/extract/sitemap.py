"""
Tier-3 Extract — Sitemap XML parser (sitemap.xml / sitemap_index.xml).

Usage:
    from Heart.scripts._lib.extract.sitemap import parse_sitemap, discover_sitemaps

    urls = parse_sitemap(body)       # extract <url> entries
    sitemaps = discover_sitemaps(body)  # extract child <sitemap> entries
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..schema.document import Document, make_id

log = logging.getLogger("heart.extract.sitemap")


def _strip_cdata(text: str) -> str:
    text = re.sub(r"<!\[CDATA\[|\]\]>", "", text or "")
    return text.strip()


def parse_sitemap(body: str, source_id: str = "sitemap") -> list[Document]:
    if not body:
        return []

    urls = re.findall(r"<url>(.*?)</url>", body, re.DOTALL | re.IGNORECASE)
    out = []
    for u in urls:
        loc_m = re.search(r"<loc[^>]*>(.*?)</loc>", u, re.DOTALL)
        lastmod_m = re.search(r"<lastmod[^>]*>(.*?)</lastmod>", u, re.DOTALL)
        changefreq_m = re.search(r"<changefreq[^>]*>(.*?)</changefreq>", u, re.DOTALL)
        priority_m = re.search(r"<priority[^>]*>(.*?)</priority>", u, re.DOTALL)
        loc = _strip_cdata(loc_m.group(1)) if loc_m else ""
        if not loc:
            continue
        lastmod = _strip_cdata(lastmod_m.group(1)) if lastmod_m else None
        tags = []
        if changefreq_m:
            tags.append(f"changefreq:{_strip_cdata(changefreq_m.group(1))}")
        priority = priority_m.group(1).strip() if priority_m else None
        out.append(
            Document(
                id=make_id(source_id, loc),
                url=loc,
                source_id=source_id,
                kind="sitemap",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                title=loc.rsplit("/", 1)[-1][:200],
                tags=tags,
                published=lastmod[:64] if lastmod else None,
                raw={"priority": priority} if priority else {},
                _extractor="sitemap@1",
            )
        )
    return out


def discover_sitemaps(body: str) -> list[str]:
    if not body:
        return []
    sitemaps = re.findall(r"<sitemap>(.*?)</sitemap>", body, re.DOTALL | re.IGNORECASE)
    urls = []
    for s in sitemaps:
        loc_m = re.search(r"<loc[^>]*>(.*?)</loc>", s, re.DOTALL)
        if loc_m:
            urls.append(_strip_cdata(loc_m.group(1)))
    return urls
