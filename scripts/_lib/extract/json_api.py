"""
Tier-3 Extract — JSON API parser (generic).

Usage:
    from Heart.scripts._lib.extract.json_api import parse_json_api

    docs = parse_json_api(body, source_id="openalex", url="...", field_map=...)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..schema.document import Document, Geo, Refs, make_id, parse_geo, parse_tags

log = logging.getLogger("heart.extract.json_api")

DEFAULT_FIELD_MAP = {
    "title": ["title", "name", "headline", "subject"],
    "summary": ["summary", "description", "abstract", "snippet"],
    "body": ["body", "content", "text"],
    "author": ["author", "creator", "user", "by", "submitter"],
    "url": ["url", "link", "uri", "href"],
    "published": ["published", "date", "created_at", "created", "timestamp"],
    "tags": ["tags", "categories", "keywords", "topics"],
    "lat": ["lat", "latitude", "geo_lat"],
    "lon": ["lon", "lng", "longitude", "geo_long"],
    "doi": ["doi", "prism_doi"],
    "cve": ["cve", "cve_id"],
    "isbn": ["isbn", "prism_isbn"],
    "issn": ["issn", "prism_issn"],
    "score": ["score", "points", "votes", "rank"],
    "comments": ["comments", "num_comments", "reply_count"],
    "language": ["language", "lang"],
}


def _get(d: dict, keys: list[str]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
        for k2 in (k.lower(), k.replace("_", "").lower()):
            if k2 in d and d[k2] not in (None, "", []):
                return d[k2]
    return None


def _extract_one(item: dict, source_id: str, url: str, field_map: dict) -> Document | None:
    if not isinstance(item, dict):
        return None

    title = str(_get(item, field_map.get("title", DEFAULT_FIELD_MAP["title"])) or "")
    summary = str(_get(item, field_map.get("summary", DEFAULT_FIELD_MAP["summary"])) or "")
    body = str(_get(item, field_map.get("body", DEFAULT_FIELD_MAP["body"])) or "")
    author = str(_get(item, field_map.get("author", DEFAULT_FIELD_MAP["author"])) or "")
    item_url = str(_get(item, field_map.get("url", DEFAULT_FIELD_MAP["url"])) or url)
    published = _get(item, field_map.get("published", DEFAULT_FIELD_MAP["published"]))

    tags_raw = _get(item, field_map.get("tags", DEFAULT_FIELD_MAP["tags"])) or []
    if isinstance(tags_raw, str):
        tags = parse_tags(tags_raw)
    elif isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw if t]
    else:
        tags = []

    lat = _get(item, field_map.get("lat", DEFAULT_FIELD_MAP["lat"]))
    lon = _get(item, field_map.get("lon", DEFAULT_FIELD_MAP["lon"]))
    geo = parse_geo(lat, lon)

    doi = _get(item, field_map.get("doi", DEFAULT_FIELD_MAP["doi"]))
    cve = _get(item, field_map.get("cve", DEFAULT_FIELD_MAP["cve"]))
    isbn = _get(item, field_map.get("isbn", DEFAULT_FIELD_MAP["isbn"]))
    issn = _get(item, field_map.get("issn", DEFAULT_FIELD_MAP["issn"]))
    refs = None
    if any([doi, cve, isbn, issn]):
        refs = Refs(doi=doi, cve=cve, isbn=isbn, issn=issn, url=item_url)

    score = _get(item, field_map.get("score", DEFAULT_FIELD_MAP["score"]))
    try:
        score_f = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_f = None
    comments = _get(item, field_map.get("comments", DEFAULT_FIELD_MAP["comments"]))
    try:
        comments_i = int(comments) if comments is not None else None
    except (TypeError, ValueError):
        comments_i = None

    language = _get(item, field_map.get("language", DEFAULT_FIELD_MAP["language"]))

    if not title and not summary and not body:
        return None

    doc_id = make_id(source_id, item_url, title, summary)
    return Document(
        id=doc_id,
        url=item_url,
        source_id=source_id,
        kind="api",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        title=title[:500],
        summary=summary[:2000],
        body=body[:10000],
        author=author[:200],
        tags=tags,
        geo=geo,
        refs=refs,
        published=str(published)[:64] if published else None,
        score=score_f,
        comments=comments_i,
        language=str(language)[:8] if language else None,
        raw=item,
        _extractor="json_api@1",
    )


def _walk_to_list(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [o for o in obj if isinstance(o, dict)]
    if isinstance(obj, dict):
        for key in ("results", "items", "data", "entries", "articles", "papers", "records", "vulnerabilities", "cves", "events", "rows"):
            v = obj.get(key)
            if isinstance(v, list):
                return [o for o in v if isinstance(o, dict)]
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def parse_json_api(
    body: str,
    source_id: str,
    url: str,
    field_map: dict | None = None,
    items_path: str | None = None,
) -> list[Document]:
    if not body:
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        log.warning("json_api: invalid JSON for %s", source_id)
        return []

    if items_path:
        for key in items_path.split("."):
            if isinstance(data, dict):
                data = data.get(key, {})
            else:
                data = []
                break

    items = _walk_to_list(data)
    return [d for d in (_extract_one(i, source_id, url, field_map or {}) for i in items) if d]
