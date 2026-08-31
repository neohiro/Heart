"""
Tier-3 Extract — RSS / Atom parser using feedparser.

Usage:
    from Heart.scripts._lib.extract.rss import parse_rss

    docs = parse_rss(body, source_id="nist-nvd-cve", url="...")
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..schema.document import Document, Geo, Refs, make_id, parse_geo, parse_tags

log = logging.getLogger("heart.extract.rss")

try:
    import feedparser
except ImportError:
    feedparser = None
    log.warning("feedparser not installed; RSS parsing will use regex fallback")


def _entry_id(entry: dict, source_id: str) -> str:
    if entry.get("id"):
        return make_id(source_id, entry.id)
    if entry.get("link"):
        return make_id(source_id, entry.link)
    if entry.get("title"):
        return make_id(source_id, entry.title, str(datetime.now(timezone.utc)))
    return make_id(source_id, "anon", str(datetime.now(timezone.utc)))


def _entry_published(entry: dict) -> str | None:
    for key in ("published", "updated", "created"):
        val = entry.get(key)
        if val:
            return val
    return None


def _entry_geo(entry: dict) -> Geo | None:
    geo = entry.get("geo_lat") or entry.get("latitude")
    lon = entry.get("geo_long") or entry.get("longitude")
    if geo is not None and lon is not None:
        return parse_geo(geo, lon)
    return None


def _entry_refs(entry: dict) -> Refs | None:
    doi = entry.get("prism_doi") or entry.get("doi")
    isbn = entry.get("prism_isbn") or entry.get("isbn")
    issn = entry.get("prism_issn") or entry.get("issn")
    cve = None
    if doi and doi.lower().startswith("10.") and "cve" in doi.lower():
        cve = doi
    if not any([doi, isbn, issn, cve]):
        return None
    return Refs(doi=doi, isbn=isbn, issn=issn, cve=cve, url=entry.get("link"))


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def parse_rss(body: str, source_id: str, url: str) -> list[Document]:
    if not body:
        return []

    if feedparser is not None:
        return _parse_with_feedparser(body, source_id, url)

    return _parse_with_regex(body, source_id, url)


def _parse_with_feedparser(body: str, source_id: str, url: str) -> list[Document]:
    parsed = feedparser.parse(body)
    out: list[Document] = []
    for entry in parsed.entries:
        title = _strip_html(entry.get("title", ""))
        summary = _strip_html(entry.get("summary", ""))
        doc_id = _entry_id(entry, source_id)
        out.append(
            Document(
                id=doc_id,
                url=entry.get("link", url),
                source_id=source_id,
                kind="rss",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                title=title,
                summary=summary,
                body=summary,
                author=entry.get("author", ""),
                tags=parse_tags(title + " " + summary),
                geo=_entry_geo(entry),
                refs=_entry_refs(entry),
                published=_entry_published(entry),
                _extractor="feedparser_rss@1",
            )
        )
    return out


def _parse_with_regex(body: str, source_id: str, url: str) -> list[Document]:
    items = re.findall(r"<item[^>]*>(.*?)</item>", body, re.DOTALL | re.IGNORECASE)
    if not items:
        items = re.findall(r"<entry[^>]*>(.*?)</entry>", body, re.DOTALL | re.IGNORECASE)
    out: list[Document] = []
    for it in items:
        title_m = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.DOTALL)
        link_m = re.search(r"<link[^>]*href=[\"']([^\"']+)[\"']", it)
        if not link_m:
            link_m = re.search(r"<link[^>]*>(.*?)</link>", it, re.DOTALL)
        desc_m = re.search(r"<description[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", it, re.DOTALL)
        if not desc_m:
            desc_m = re.search(r"<summary[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</summary>", it, re.DOTALL)
        title = _strip_html(title_m.group(1)) if title_m else ""
        link = link_m.group(1) if link_m else url
        summary = _strip_html(desc_m.group(1)) if desc_m else ""
        doc_id = make_id(source_id, link, title)
        out.append(
            Document(
                id=doc_id,
                url=link,
                source_id=source_id,
                kind="rss",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                title=title,
                summary=summary,
                body=summary,
                tags=parse_tags(title + " " + summary),
                _extractor="regex_rss@1",
            )
        )
    return out
