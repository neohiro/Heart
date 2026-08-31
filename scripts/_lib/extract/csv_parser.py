"""
Tier-3 Extract — CSV parser.

Usage:
    from Heart.scripts._lib.extract.csv_parser import parse_csv

    docs = parse_csv(body, source_id="usgs-earthquakes", url="...",
                     columns={"title": "place", "lat": "latitude",
                              "lon": "longitude", "tags": "type"})
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

from ..schema.document import Document, make_id, parse_geo, parse_tags

log = logging.getLogger("heart.extract.csv")


def parse_csv(
    body: str,
    source_id: str,
    url: str,
    columns: dict | None = None,
    delimiter: str = ",",
    skip_header: bool = True,
) -> list[Document]:
    if not body:
        return []
    try:
        reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
        headers = reader.fieldnames or []
    except Exception:
        return []

    if not columns:
        columns = {}

    out = []
    for row in reader:
        title_col = columns.get("title") or next((h for h in headers if h.lower() in ("title", "name", "place", "subject")), None)
        title = str(row.get(title_col, "")).strip() if title_col else ""
        if not title:
            title = str(row.get(list(row.keys())[0], "")).strip()[:100]

        lat_key = columns.get("lat") or next((h for h in headers if "lat" in h.lower()), None)
        lon_key = columns.get("lon") or next((h for h in headers if h.lower() in ("lon", "lng", "long", "longitude")), None)
        lat_raw = row.get(lat_key) if lat_key else None
        lon_raw = row.get(lon_key) if lon_key else None
        geo = parse_geo(lat_raw, lon_raw)

        tags_col = columns.get("tags") or next((h for h in headers if h.lower() in ("type", "category", "status", "tag")), None)
        tags_raw = row.get(tags_col) if tags_col else ""
        tags = parse_tags(str(tags_raw)) if tags_raw else []

        url_col = columns.get("url") or next((h for h in headers if h.lower() in ("url", "link", "href")), None)
        doc_url = str(row.get(url_col, url)).strip()

        doc_id = make_id(source_id, doc_url, title)
        out.append(
            Document(
                id=doc_id,
                url=doc_url,
                source_id=source_id,
                kind="csv",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                title=title[:500],
                tags=tags,
                geo=geo,
                raw=row,
                _extractor="csv@1",
            )
        )
    return out
