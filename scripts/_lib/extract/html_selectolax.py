"""
Tier-3 Extract — fast HTML extraction using selectolax + CSS selectors.

Falls back to regex if selectolax is not installed.

Usage:
    from Heart.scripts._lib.extract.html_selectolax import extract_html

    docs = extract_html(
        body,
        source_id="awesome-sysadmin",
        url="https://github.com/.../README.md",
        selectors={"item": "li a[href]", "title": "h2, h3"},
    )
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from ..schema.document import Document, make_id

log = logging.getLogger("heart.extract.html_selectolax")

try:
    from selectolax.lexbor import LexborHTMLParser

    HAS_SELECTOLAX = True
except ImportError:
    HAS_SELECTOLAX = False


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _extract_with_selectolax(body: str, source_id: str, url: str, base_url: str, selectors: dict) -> list[Document]:
    tree = LexborHTMLParser(body)
    item_sel = selectors.get("item", "a[href]")
    title_sel = selectors.get("title", "h1, h2, h3")
    desc_sel = selectors.get("description", "p")

    out = []
    for item in tree.css(item_sel):
        href = item.attributes.get("href", "")
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if base_url and not href.startswith(("http://", "https://", "//")):
            href = urljoin(base_url, href)
        title = _strip(item.text() or "")[:200]
        parent = item.parent
        desc = ""
        if parent:
            d = parent.css_first(desc_sel)
            if d:
                desc = _strip(d.text() or "")[:500]
        if not title and desc:
            title = desc[:200]
        if not title and not desc:
            title = href
        doc_id = make_id(source_id, href, title)
        out.append(
            Document(
                id=doc_id,
                url=href,
                source_id=source_id,
                kind="html",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                title=title,
                summary=desc,
                body=desc,
                raw={"item_text": _strip(item.text() or "")[:500]},
                _extractor="html_selectolax@1",
            )
        )
    return out


def _extract_with_regex(body: str, source_id: str, url: str, base_url: str, selectors: dict) -> list[Document]:
    links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, re.DOTALL | re.IGNORECASE)
    out = []
    for href, text in links:
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if base_url and not href.startswith(("http://", "https://", "//")):
            href = urljoin(base_url, href)
        title = _strip(re.sub(r"<[^>]+>", "", text))[:200]
        doc_id = make_id(source_id, href, title)
        out.append(
            Document(
                id=doc_id,
                url=href,
                source_id=source_id,
                kind="html",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                title=title,
                _extractor="html_regex@1",
            )
        )
    return out


def extract_html(
    body: str,
    source_id: str,
    url: str,
    selectors: dict | None = None,
    base_url: str | None = None,
) -> list[Document]:
    if not body:
        return []
    base_url = base_url or url
    if HAS_SELECTOLAX:
        return _extract_with_selectolax(body, source_id, url, base_url, selectors or {})
    return _extract_with_regex(body, source_id, url, base_url, selectors or {})
