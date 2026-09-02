"""
Tier-3 Schema — Canonical Document model.

Every extractor emits this shape. Downstream tiers are type-checked against it.

Usage:
    from Heart.scripts._lib.schema.document import Document, Seen

    doc = Document(
        id="abc123",
        url="https://...",
        source_id="nist-nvd-cve",
        kind="api",
        title="CVE-2024-3094",
        tags=["cve", "libyaml"],
        geo=None,
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("heart.schema")


@dataclass
class Geo:
    lat: float | None = None
    lon: float | None = None
    accuracy: str = "unknown"
    country: str | None = None
    region: str | None = None


@dataclass
class Refs:
    doi: str | None = None
    isbn: str | None = None
    cve: str | None = None
    issn: str | None = None
    url: str | None = None


@dataclass
class Document:
    id: str
    url: str
    source_id: str
    kind: str
    fetched_at: str
    title: str = ""
    summary: str = ""
    body: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    geo: Geo | None = None
    refs: Refs | None = None
    license: str = "unknown"
    trust: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    _extractor: str = ""
    _tier: int = 3
    published: str | None = None
    score: float | None = None
    comments: int | None = None
    language: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.geo:
            d["geo"] = asdict(self.geo)
        if self.refs:
            d["refs"] = asdict(self.refs)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Document:
        if d.get("geo"):
            d = {**d, "geo": Geo(**d["geo"])}
        if d.get("refs"):
            d = {**d, "refs": Refs(**d["refs"])}
        return cls(**d)


def make_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def parse_tags(text: str) -> list[str]:
    text = re.sub(r"[^a-z0-9\-]", " ", text.lower())
    return [w.strip() for w in text.split() if len(w) > 1][:20]


def parse_geo(lat: float | str | None, lon: float | str | None) -> Geo | None:
    try:
        lat_f = float(lat) if lat is not None else None
        lon_f = float(lon) if lon is not None else None
        if lat_f is not None and lon_f is not None and -90 <= lat_f <= 90 and -180 <= lon_f <= 180:
            return Geo(lat=lat_f, lon=lon_f, accuracy="point")
    except (TypeError, ValueError):
        pass
    return None


class Seen:
    """
    Append-only SHA256 log for dedup.
    Writes to /shared/<scope>/seen.jsonl (one sha256 per line).
    """

    def __init__(self, path: str):
        self.path = path
        self._seen: set[str] = set()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        with open(self.path, "a"):
            pass
        with open(self.path, "r") as f:
            for line in f:
                self._seen.add(line.strip())

    def add(self, doc_id: str) -> bool:
        self._load()
        if doc_id in self._seen:
            return False
        self._seen.add(doc_id)
        with open(self.path, "a") as f:
            f.write(doc_id + "\n")
        return True

    def __contains__(self, doc_id: str) -> bool:
        self._load()
        return doc_id in self._seen
