"""
Tier-3 Extract — GeoJSON parser.

Usage:
    from Heart.scripts._lib.extract.geojson import parse_geojson

    docs = parse_geojson(body, source_id="gdelt-geojson", url="...")
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..schema.document import Document, Geo, make_id, parse_geo

log = logging.getLogger("heart.extract.geojson")


def _get_prop(props: dict, *keys) -> Any:
    for k in keys:
        for key in (k, k.lower(), k.replace("_", "").lower()):
            if key in props:
                return props[key]
    return None


def _extract_feature(feat: dict, source_id: str) -> Document | None:
    geom = feat.get("geometry")
    props = feat.get("properties") or {}
    title = str(_get_prop(props, "title", "name", "headline", "event", "item") or "")
    summary = str(_get_prop(props, "summary", "description", "body", "text") or "")
    url = str(_get_prop(props, "url", "link", "uri") or "")

    lat = lon = None
    if geom:
        coords = geom.get("coordinates", [])
        if geom["type"] == "Point" and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
        elif geom["type"] in ("Polygon", "MultiPolygon") and coords:
            poly = coords[0] if geom["type"] == "Polygon" else coords[0][0]
            if poly:
                lon = sum(c[0] for c in poly) / len(poly)
                lat = sum(c[1] for c in poly) / len(poly)
        elif geom["type"] == "LineString" and coords:
            mid = coords[len(coords) // 2]
            lon, lat = mid[0], mid[1]

    geo = parse_geo(lat, lon) if lat and lon else None
    tags = []
    for k in ("category", "categories", "theme", "themes", "tags", "label"):
        v = _get_prop(props, k)
        if v:
            if isinstance(v, list):
                tags.extend(str(x)[:30] for x in v)
            else:
                tags.append(str(v)[:30])
    tags = list(dict.fromkeys(tags))[:20]

    if not title and not summary:
        title = str(props)[:100]

    doc_id = make_id(source_id, url, title, str(lat), str(lon))
    return Document(
        id=doc_id,
        url=url,
        source_id=source_id,
        kind="geojson",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        title=title[:500],
        summary=summary[:2000],
        tags=tags,
        geo=geo,
        raw=feat,
        _extractor="geojson@1",
    )


def parse_geojson(body: str, source_id: str, url: str) -> list[Document]:
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("geojson: invalid JSON for %s", source_id)
        return []
    features = data.get("features") or data.get("geometries") or []
    out = []
    for f in features:
        if not isinstance(f, dict):
            continue
        if "properties" not in f and "geometry" not in f:
            if "type" in f and "coordinates" in f:
                f = {"type": "Feature", "geometry": f, "properties": {}}
            else:
                continue
        doc = _extract_feature(f, source_id)
        if doc:
            out.append(doc)
    return out
