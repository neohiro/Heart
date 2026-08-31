"""
Tests for Heart crawler engine (Tier-2/3).
Run: python -m pytest Heart/scripts/_lib/tests/ -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_root))

from Heart.scripts._lib.extract.csv_parser import parse_csv
from Heart.scripts._lib.extract.geojson import parse_geojson
from Heart.scripts._lib.extract.html_selectolax import extract_html
from Heart.scripts._lib.extract.json_api import parse_json_api
from Heart.scripts._lib.extract.rss import parse_rss
from Heart.scripts._lib.extract.sitemap import discover_sitemaps, parse_sitemap
from Heart.scripts._lib.pull.httpx_async import DiskCache, PullConfig
from Heart.scripts._lib.schema.document import Document, Geo, Refs, Seen, make_id, parse_geo, parse_tags


class TestSchema:
    def test_make_id_stable(self):
        a = make_id("x", "y", "z")
        b = make_id("x", "y", "z")
        assert a == b
        assert len(a) == 16

    def test_parse_tags(self):
        assert parse_tags("hello world test") == ["hello", "world", "test"]

    def test_parse_geo_valid(self):
        g = parse_geo(50.85, 4.35)
        assert g is not None
        assert g.lat == 50.85
        assert g.accuracy == "point"

    def test_parse_geo_invalid(self):
        assert parse_geo(999, 0) is None
        assert parse_geo("foo", "bar") is None

    def test_seen_dedup(self, tmp_path):
        s = Seen(str(tmp_path / "seen.jsonl"))
        assert s.add("a") is True
        assert s.add("a") is False
        assert "a" in s


class TestRSS:
    def test_parse_with_feedparser(self):
        body = """<?xml version="1.0"?>
        <rss><channel>
            <title>Test</title>
            <item>
                <title>Item 1</title>
                <link>https://x.com/1</link>
                <description>Hello world</description>
            </item>
            <item>
                <title>Item 2</title>
                <link>https://x.com/2</link>
                <description>Another item</description>
            </item>
        </channel></rss>"""
        docs = parse_rss(body, "test", "https://x.com/feed")
        assert len(docs) >= 1
        if docs:
            assert docs[0].kind == "rss"
            assert docs[0].title != ""

    def test_parse_with_regex_fallback(self):
        body = """<?xml version="1.0"?>
        <rss><channel>
            <item>
                <title>Item 1</title>
                <link>https://x.com/1</link>
                <description>Hello world</description>
            </item>
        </channel></rss>"""
        docs = parse_rss(body, "test", "https://x.com/feed")
        assert len(docs) >= 1


class TestJSONAPI:
    def test_list_root(self):
        body = json.dumps([
            {"title": "T1", "url": "https://x.com/1", "description": "D1"},
            {"title": "T2", "url": "https://x.com/2", "description": "D2"},
        ])
        docs = parse_json_api(body, "s1", "https://x.com/api")
        assert len(docs) == 2
        assert docs[0].title == "T1"
        assert docs[0]._extractor == "json_api@1"

    def test_data_wrapper(self):
        body = json.dumps({
            "results": [
                {"name": "A", "link": "https://a.com"},
                {"name": "B", "link": "https://b.com"},
            ]
        })
        docs = parse_json_api(body, "s1", "https://x.com/api")
        assert len(docs) == 2

    def test_with_geo(self):
        body = json.dumps([{
            "title": "Event", "url": "https://x.com/1",
            "lat": 50.85, "lon": 4.35,
        }])
        docs = parse_json_api(body, "s1", "https://x.com/api")
        assert docs[0].geo is not None
        assert docs[0].geo.lat == 50.85

    def test_cve_extraction(self):
        body = json.dumps([{
            "title": "CVE-2024-3094", "cve": "CVE-2024-3094",
            "url": "https://nvd.nist.gov/cve/2024/3094",
        }])
        docs = parse_json_api(body, "s1", "https://x.com/api")
        assert docs[0].refs is not None
        assert docs[0].refs.cve == "CVE-2024-3094"

    def test_invalid_json(self):
        docs = parse_json_api("not json", "s1", "https://x.com/api")
        assert docs == []


class TestGeoJSON:
    def test_point_feature(self):
        body = json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [4.35, 50.85]},
                "properties": {"title": "Brussels", "category": "city"},
            }]
        })
        docs = parse_geojson(body, "gdelt", "https://x.com/geo")
        assert len(docs) == 1
        assert docs[0].geo is not None
        assert docs[0].tags == ["city"]

    def test_polygon_feature(self):
        body = json.dumps({
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]},
                "properties": {"title": "Box"},
            }]
        })
        docs = parse_geojson(body, "x", "https://x.com/geo")
        assert len(docs) == 1
        assert docs[0].geo is not None


class TestCSV:
    def test_basic(self):
        body = "place,latitude,longitude,type\nBrussels,50.85,4.35,city\nAntwerp,51.22,4.40,city\n"
        docs = parse_csv(body, "usgs", "https://x.com/csv", columns={"title": "place"})
        assert len(docs) == 2
        assert docs[0].title == "Brussels"
        assert docs[0].geo is not None
        assert "city" in docs[0].tags


class TestSitemap:
    def test_parse_urls(self):
        body = """<?xml version="1.0"?>
        <urlset>
            <url><loc>https://x.com/a</loc><lastmod>2026-08-30</lastmod></url>
            <url><loc>https://x.com/b</loc><priority>0.8</priority></url>
        </urlset>"""
        docs = parse_sitemap(body, "x.com")
        assert len(docs) == 2
        assert docs[0].url == "https://x.com/a"
        assert docs[0].published == "2026-08-30"

    def test_discover_child_sitemaps(self):
        body = """<?xml version="1.0"?>
        <sitemapindex>
            <sitemap><loc>https://x.com/s1.xml</loc></sitemap>
            <sitemap><loc>https://x.com/s2.xml</loc></sitemap>
        </sitemapindex>"""
        urls = discover_sitemaps(body)
        assert urls == ["https://x.com/s1.xml", "https://x.com/s2.xml"]


class TestHTML:
    def test_extract_links(self):
        body = """
        <html><body>
            <h1>Awesome List</h1>
            <ul>
                <li><a href="https://a.com">Site A</a></li>
                <li><a href="https://b.com">Site B</a></li>
            </ul>
        </body></html>
        """
        docs = extract_html(body, "list", "https://x.com/list", base_url="https://x.com")
        assert len(docs) >= 1
        urls = {d.url for d in docs}
        assert "https://a.com" in urls
        assert "https://b.com" in urls


class TestCache:
    def test_set_get(self, tmp_path):
        cache = DiskCache(tmp_path, ttl=60)
        cache.set("https://x.com", {"body": "hello"})
        data = cache.get("https://x.com")
        assert data is not None
        assert data["body"] == "hello"

    def test_miss(self, tmp_path):
        cache = DiskCache(tmp_path, ttl=60)
        assert cache.get("https://nope.com") is None


class TestPullConfig:
    def test_defaults(self):
        c = PullConfig(id="x", url="https://x.com", kind="api")
        assert c.cadence == "every_15_minutes"
        assert c.auth == "none"
        assert c.timeout == 12.0
        assert c.max_retries == 3


class TestDocument:
    def test_round_trip(self):
        d = Document(
            id="abc",
            url="https://x.com",
            source_id="s",
            kind="api",
            fetched_at="2026-08-31T00:00:00Z",
            title="T",
            tags=["a", "b"],
            geo=Geo(lat=1.0, lon=2.0),
            refs=Refs(cve="CVE-1"),
        )
        d2 = Document.from_dict(d.to_dict())
        assert d2.id == "abc"
        assert d2.geo.lat == 1.0
        assert d2.refs.cve == "CVE-1"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
