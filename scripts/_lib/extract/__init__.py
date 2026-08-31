"""
Tier-3 Extract — parser registry.

Provides pluggable parsers for: rss, atom, json_api, geojson, csv,
sparql, sitemap, html (selectolax), html (scrapling).

Each parser returns list[Document] (from schema.document).
"""

from __future__ import annotations
