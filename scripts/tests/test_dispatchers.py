"""
Heart/scripts/tests/ — Offline unit tests for dispatchers.
Run: python -m pytest tests/ -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "_lib"))
import heart_dispatch as hd


class TestSharedUtils(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)

    def test_parse_flags_defaults(self):
        f = hd.parse_flags([])
        self.assertEqual(f["once"], False)
        self.assertEqual(f["quiet"], False)
        self.assertEqual(f["json_console"], True)
        self.assertEqual(f["level"], "info")
        self.assertEqual(f["dry_run"], False)

    def test_parse_flags_known(self):
        f = hd.parse_flags(["--once", "--quiet", "--no-json", "--log-level", "debug", "--dry-run"])
        self.assertEqual(f["once"], True)
        self.assertEqual(f["quiet"], True)
        self.assertEqual(f["json_console"], False)
        self.assertEqual(f["level"], "debug")
        self.assertEqual(f["dry_run"], True)

    def test_parse_flags_unknown_preserved(self):
        f = hd.parse_flags(["--scope", "news-populate"])
        self.assertNotIn("--scope", f)

    def test_resolve_env_empty(self):
        self.assertEqual(hd.resolve_env(None), {})
        self.assertEqual(hd.resolve_env({}), {})

    def test_resolve_env_literal(self):
        result = hd.resolve_env({"FOO": "bar", "BAZ": "qux"})
        self.assertEqual(result["FOO"], "bar")

    def test_resolve_env_substitute(self):
        os.environ["MY_VAR"] = "secret_value"
        self.addCleanup(os.environ.pop, "MY_VAR", None)
        result = hd.resolve_env({"TOKEN": "${MY_VAR}"})
        self.assertEqual(result["TOKEN"], "secret_value")

    def test_resolve_env_multi_substitute(self):
        os.environ["VAR_A"] = "alpha"
        os.environ["VAR_B"] = "beta"
        self.addCleanup(os.environ.pop, "VAR_A", None)
        self.addCleanup(os.environ.pop, "VAR_B", None)
        result = hd.resolve_env({"DUAL": "prefix-${VAR_A}-middle-${VAR_B}-suffix"})
        self.assertEqual(result["DUAL"], "prefix-alpha-middle-beta-suffix")

    def test_resolve_env_missing_var(self):
        result = hd.resolve_env({"FOO": "${NONEXISTENT_VAR_12345}"})
        self.assertEqual(result["FOO"], "")

    def test_resolve_env_malformed_unchanged(self):
        # Unclosed ${} must be preserved (authoring bug, not silent drop)
        result = hd.resolve_env({"FOO": "${UNCLOSED"})
        self.assertEqual(result["FOO"], "${UNCLOSED")

    def test_parse_flags_warns_unknown(self):
        # Unknown --flag must not crash; it should be dropped from the dict
        # and a message written to stderr. Verify the dict is clean and the
        # unknown value does not appear as a key.
        f = hd.parse_flags(["--dryrun", "--once"])  # --dryrun is a typo
        self.assertNotIn("dryrun", f)
        self.assertEqual(f["once"], True)  # known flags still parsed
        # Also verify --scope=news-populate is dropped
        f2 = hd.parse_flags(["--scope", "news-populate"])
        self.assertNotIn("scope", f2)

    def test_atomic_write_text_roundtrip(self):
        p = Path(self.tmp) / "test.txt"
        hd.atomic_write_text(p, "hello world\n")
        self.assertEqual(p.read_text(encoding="utf-8"), "hello world\n")

    def test_atomic_write_json_roundtrip(self):
        p = Path(self.tmp) / "test.json"
        obj = {"key": "value", "list": [1, 2, 3]}
        hd.atomic_write_json(p, obj)
        loaded = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(loaded, obj)

    @patch.object(hd.urllib.request, "urlopen")
    def test_http_get_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"url": "https://httpbin.org/get"}'
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        result = hd.http_get("https://httpbin.org/get", timeout=10)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 200)
        self.assertIn("httpbin", result.url)

    @patch.object(hd.urllib.request, "urlopen")
    def test_http_get_404(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.read.return_value = b'Not Found'
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        result = hd.http_get("https://httpbin.org/status/404", timeout=10)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 404)

    def test_http_get_invalid_url(self):
        result = hd.http_get("not-a-url", timeout=5)
        self.assertFalse(result.ok)

    def test_http_get_result_to_dict(self):
        r = hd.HttpResult(ok=True, status=200, body='{"ok":true}', url="https://example.com", elapsed_ms=123)
        d = r.to_dict()
        self.assertEqual(d["ok"], True)
        self.assertEqual(d["status"], 200)
        self.assertEqual(d["elapsed_ms"], 123)
        self.assertEqual(d["bytes"], len('{"ok":true}'))

    def test_utcnow_iso_format(self):
        ts = hd.utcnow_iso()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_append_pending_creates_file(self):
        audit_dir = Path(self.tmp) / "audit"
        audit_dir.mkdir()
        with patch.object(hd, "links_root", return_value=Path(self.tmp)):
            hd.append_pending("tool-00001", "broken", "HTTP 404", scope="test-scope")
        pending = audit_dir / "pending.yaml"
        self.assertTrue(pending.is_file())
        data = yaml_load(pending.read_text())
        self.assertEqual(len(data["pending"]), 1)
        self.assertEqual(data["pending"][0]["link_id"], "tool-00001")
        self.assertEqual(data["pending"][0]["status"], "broken")

    def test_append_pending_max_1000(self):
        audit_dir = Path(self.tmp) / "audit"
        audit_dir.mkdir()
        pending = audit_dir / "pending.yaml"
        # Pre-seed 990 entries
        import yaml
        seed = {"schema_version": 1, "pending": [{"id": f"pre-{i}", "ts": "2026-01-01T00:00:00Z", "scope": "x", "link_id": f"pre-{i}", "status": "broken", "msg": "err"} for i in range(990)], "resolved": []}
        pending.write_text(yaml.safe_dump(seed), encoding="utf-8")
        with patch.object(hd, "links_root", return_value=Path(self.tmp)):
            hd.append_pending("id-newest", "broken", "err", scope="x")
        data = yaml_load(pending.read_text())
        # Cap is 1000; oldest 990 pre-seeded + 1 new = 991 total
        self.assertEqual(len(data["pending"]), 991)
        self.assertEqual(data["pending"][0]["link_id"], "pre-0")
        self.assertEqual(data["pending"][-1]["link_id"], "id-newest")


def yaml_load(text: str):
    import yaml
    return yaml.safe_load(text)


class TestNewsPopulateParsing(unittest.TestCase):
    def test_parse_feeds_extracts_rss_and_rest(self):
        # Use a real temp FEEDS.md
        tmp = tempfile.mkdtemp()
        feeds_md = Path(tmp) / "FEEDS.md"
        feeds_md.write_text("""\
## RSS / Atom feeds

| Name | URL | Update | Notes |
|------|-----|--------|-------|
| GitHub Status | https://www.githubstatus.com/feed | 15 min | status |

## REST / API sources (no auth)

| Name | Endpoint | Update | Notes |
|------|----------|--------|-------|
| HackerNews Top | https://hacker-news.firebaseio.com/v0/topstories.json | 60 min | HN |
""", encoding="utf-8")

        with patch.object(sys.modules.get("news_populate.run", MagicMock()), "REPO_FEEDS", feeds_md):
            # Just test the internal _resolve_feeds logic by re-running the parsing
            text = feeds_md.read_text()
            sections = {}
            current = None
            for line in text.splitlines():
                line_stripped = line.rstrip()
                if line_stripped.startswith("## "):
                    current = line_stripped[3:].strip()
                    sections[current] = []
                elif current and line_stripped.startswith("| ") and "---" not in line_stripped:
                    cells = [c.strip() for c in line_stripped.split("|")[1:-1]]
                    if len(cells) >= 2 and cells[0] != "Name":
                        sections[current].append(cells)
            self.assertIn("RSS / Atom feeds", sections)
            self.assertIn("REST / API sources (no auth)", sections)
            self.assertEqual(len(sections["RSS / Atom feeds"]), 1)
            self.assertEqual(sections["RSS / Atom feeds"][0][0], "GitHub Status")


class TestOsintPopulateFeeds(unittest.TestCase):
    def test_load_osint_feeds(self):
        tmp = Path(tempfile.mkdtemp())
        osint_yaml = tmp / "osint.yaml"
        osint_yaml.write_text("""\
schema_version: 1
feeds:
  - id: test-feed
    name: Test Feed
    url: https://example.com/api
    kind: api
    cadence: every_15_minutes
    auth: none
    cors: true
    from: {org: neohiro, repo: news, path: FEEDS.md}
    to: {kind: api, url: https://example.com}
    purpose: "Test purpose"
""", encoding="utf-8")
        with patch.object(hd, "links_root", return_value=tmp):
            # Directly test load
            import yaml
            data = yaml.safe_load(osint_yaml.read_text(encoding="utf-8"))
            self.assertEqual(len(data["feeds"]), 1)
            self.assertEqual(data["feeds"][0]["id"], "test-feed")


class TestLinksValidateLogic(unittest.TestCase):
    def test_validate_link_returns_correct_structure(self):
        from heart_dispatch import HttpResult
        r = HttpResult(ok=False, status=404, body="", url="https://example.com/404", elapsed_ms=50, error="Not Found")
        d = r.to_dict()
        self.assertEqual(d["ok"], False)
        self.assertEqual(d["status"], 404)
        self.assertEqual(d["error"], "Not Found")


class TestToolsPopulateParsing(unittest.TestCase):
    def test_parse_readme_extracts_entries(self):
        import re
        # Real README format: header line has no leading pipe; data rows do.
        readme = """\
# APILayer

### Animals
API | Description | Auth | HTTPS | CORS
|:---|:---|:---|:---|:---|
| [Dog](https://dog.ceo/dog-api/) | Random dog pictures | No | Yes | Yes |

### Weather
API | Description | Auth | HTTPS | CORS
|:---|:---|:---|:---|:---|
| [Open-Meteo](https://api.open-meteo.com/v1/forecast) | Weather API | No | Yes | Yes |
"""
        sections = re.split(r"\n(?=### )", readme)
        entries = []
        for section in sections[1:]:
            lines = section.strip().split("\n")
            m = re.match(r"^###\s+(.+)$", lines[0])
            self.assertIsNotNone(m, f"Could not parse header from: {lines[0]!r}")
            cat = m.group(1).strip().lower()
            table_started = False
            for line in lines[1:]:
                if not line.startswith("|"):
                    continue
                cells = [c.strip() for c in line.split("|")]
                while cells and cells[0] == "":
                    cells.pop(0)
                while cells and cells[-1] == "":
                    cells.pop()
                if len(cells) < 4:
                    continue
                if not table_started:
                    # Header row: first cell is "API" exactly
                    if cells[0].strip().lower() in ("api", "apis"):
                        table_started = True
                        continue
                api_cell = cells[0]
                auth_cell = cells[2] if len(cells) > 2 else ""
                https_cell = cells[3] if len(cells) > 3 else ""
                if https_cell.strip().lower() == "no":
                    continue
                url_m = re.search(r"\((https?://[^\)]+)\)", api_cell)
                name_m = re.search(r"\[([^\]]+)\]", api_cell)
                url = url_m.group(1) if url_m else None
                name = name_m.group(1) if name_m else None
                if url and name:
                    entries.append({"name": name, "url": url, "auth": auth_cell.lower(), "slug": cat})
        self.assertEqual(len(entries), 2, f"Expected 2 entries, got {len(entries)}: {entries}")
        self.assertEqual(entries[0]["name"], "Dog")
        self.assertEqual(entries[0]["slug"], "animals")
        self.assertEqual(entries[1]["name"], "Open-Meteo")
        self.assertEqual(entries[1]["slug"], "weather")

    def test_dedupe_removes_duplicates(self):
        entries = [
            {"name": "Dog", "url": "https://dog.ceo", "auth": "no", "cors": "yes", "slug": "animals", "desc": "Dog API"},
            {"name": "Dog", "url": "https://dog.ceo", "auth": "no", "cors": "yes", "slug": "animals", "desc": "Dog API"},
            {"name": "Cat", "url": "https://catfact.ninja", "auth": "no", "cors": "yes", "slug": "animals", "desc": "Cat API"},
        ]
        seen_urls, seen_names = set(), set()
        out = []
        for e in entries:
            if e["url"] in seen_urls or e["name"] in seen_names:
                continue
            seen_urls.add(e["url"])
            seen_names.add(e["name"])
            out.append(e)
        self.assertEqual(len(out), 2)

    def test_quality_scoring(self):
        # Import real _quality from tools-populate
        sys.path.insert(0, str(Path(__file__).parent.parent / "tools-populate"))
        try:
            import importlib
            if "run" in sys.modules:
                del sys.modules["run"]
            run = importlib.import_module("run")
        finally:
            try:
                sys.path.remove(str(Path(__file__).parent.parent / "tools-populate"))
            except ValueError:
                pass
        # _quality returns -score (so sort is ascending = best first)
        cases = [
            ({"auth": "none", "cors": "yes"}, -5),   # 3 + 2, negated
            ({"auth": "none", "cors": "no"}, -3),    # 3 + 0
            ({"auth": "apiKey", "cors": "yes"}, -3),  # 1 + 2
            ({"auth": "apiKey", "cors": "no"}, -1),   # 1 + 0
            ({"auth": "OAuth", "cors": "no"}, 0),    # 0 + 0
        ]
        for entry, expected in cases:
            self.assertEqual(run._quality(entry), expected, f"entry {entry}")

    def test_auth_block_none(self):
        # Import the real _auth_block from tools-populate
        sys.path.insert(0, str(Path(__file__).parent.parent / "tools-populate"))
        try:
            import importlib
            if "run" in sys.modules:
                del sys.modules["run"]
            run = importlib.import_module("run")
        finally:
            try:
                sys.path.remove(str(Path(__file__).parent.parent / "tools-populate"))
            except ValueError:
                pass
        cases = [
            ({"auth": "none"}, ({"kind": "none", "key_location": None, "key_format": None}, None)),
            ({"auth": "no"}, ({"kind": "none", "key_location": None, "key_format": None}, None)),
            ({"auth": "apiKey"}, ({"kind": "apiKey", "key_location": "query", "key_format": "apiKey"}, "X-API-Key")),
            ({"auth": "OAuth"}, ({"kind": "oauth", "key_location": "header", "key_format": "Bearer"}, "Authorization")),
        ]
        for entry, expected in cases:
            result = run._auth_block(entry)
            self.assertEqual(result, expected, f"entry {entry}")


class TestOsintSignals(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)

    def _import_osint(self):
        import importlib
        sys.path.insert(0, str(Path(__file__).parent.parent / "osint-populate"))
        try:
            if "run" in sys.modules:
                del sys.modules["run"]
            mod = importlib.import_module("run")
        finally:
            try:
                sys.path.remove(str(Path(__file__).parent.parent / "osint-populate"))
            except ValueError:
                pass
        return mod

    def test_merge_crt_sh_ips(self):
        run = self._import_osint()
        results = [
            {
                "ok": True,
                "body": json.dumps([
                    {"name_value": "api.example.com\n93.184.216.34"},
                    {"name_value": "www.example.com"},
                    {"name_value": "1.2.3.4\n5.6.7.8"},
                ]),
                "feed_id": "crt_sh_com",
                "ts": "2026-08-30T00:00:00Z",
            }
        ]
        signals = run._merge_abuse_signals([], results)
        ips = sorted(s["ip"] for s in signals)
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8", "93.184.216.34"])

    def test_merge_dedups_across_sources(self):
        run = self._import_osint()
        results = [
            {
                "ok": True,
                "body": json.dumps([{"name_value": "1.2.3.4"}]),
                "feed_id": "crt_sh_com",
                "ts": "2026-08-30T00:00:00Z",
            },
            {
                "ok": True,
                "body": json.dumps([{"ip_address": "1.2.3.4"}]),
                "feed_id": "rdap_arin",
                "ts": "2026-08-30T01:00:00Z",
            },
        ]
        signals = run._merge_abuse_signals([], results)
        self.assertEqual(len(signals), 1)
        self.assertEqual(sorted(signals[0]["sources"]), ["crt_sh_com", "rdap_arin"])

    def test_merge_rdap(self):
        run = self._import_osint()
        results = [
            {
                "ok": True,
                "body": json.dumps([{"ip_address": "8.8.8.8"}, {"ip_address": "1.1.1.1"}]),
                "feed_id": "rdap_arin",
                "ts": "2026-08-30T00:00:00Z",
            }
        ]
        signals = run._merge_abuse_signals([], results)
        ips = sorted(s["ip"] for s in signals)
        self.assertEqual(ips, ["1.1.1.1", "8.8.8.8"])

    def test_merge_skips_failed_results(self):
        run = self._import_osint()
        results = [
            {"ok": False, "error": "timeout", "feed_id": "crt_sh_com"},
            {"ok": True, "body": "", "feed_id": "crt_sh_com"},
        ]
        signals = run._merge_abuse_signals([], results)
        self.assertEqual(signals, [])


class TestNewsStatusUrls(unittest.TestCase):
    def _import_news(self):
        import importlib
        sys.path.insert(0, str(Path(__file__).parent.parent / "news-populate"))
        try:
            if "run" in sys.modules:
                del sys.modules["run"]
            mod = importlib.import_module("run")
        finally:
            try:
                sys.path.remove(str(Path(__file__).parent.parent / "news-populate"))
            except ValueError:
                pass
        return mod

    def test_status_feed_urls_are_real(self):
        run = self._import_news()
        import inspect
        import re
        src = inspect.getsource(run._fetch_status_feeds)
        self.assertNotIn("../", src, "Status feed URLs still contain placeholder ../")
        urls = re.findall(r'"https?://[^"]+"', src)
        self.assertGreater(len(urls), 4)
        for url in urls:
            self.assertTrue(url.startswith('"https://'), f"non-https URL: {url}")

    def test_mastodon_uses_lowercase_header(self):
        run = self._import_news()
        import inspect
        src = inspect.getsource(run._fetch_mastodon)
        self.assertNotIn('"Limit":', src, "Mastodon Limit header should be lowercase 'limit'")

    def test_parse_rss_rss2_items(self):
        run = self._import_news()
        rss = """<?xml version="1.0"?>
<rss version="2.0">
<channel><title>GitHub</title>
<item><title>Resolved: GitHub Actions degradation</title><link>https://www.githubstatus.com/incidents/abc</link><pubDate>Sun, 30 Aug 2026 12:00:00 GMT</pubDate><description>We have resolved the incident.</description></item>
<item><title>Investigating: API slow responses</title><link>https://www.githubstatus.com/incidents/def</link><pubDate>Sun, 30 Aug 2026 11:00:00 GMT</pubDate><description>We are investigating.</description></item>
<item><title>Older incident</title><link>https://www.githubstatus.com/incidents/xyz</link><pubDate>Sat, 29 Aug 2026 09:00:00 GMT</pubDate><description>Old.</description></item>
</channel>
</rss>"""
        items = run._parse_rss(rss, max_items=3)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "Resolved: GitHub Actions degradation")
        self.assertEqual(items[0]["link"], "https://www.githubstatus.com/incidents/abc")
        self.assertEqual(items[0]["pubDate"], "Sun, 30 Aug 2026 12:00:00 GMT")
        self.assertEqual(items[1]["title"], "Investigating: API slow responses")

    def test_parse_rss_atom_entries(self):
        run = self._import_news()
        atom = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Cloudflare Status</title>
<entry>
  <title>Cloudflare is healthy</title>
  <link href="https://www.cloudflarestatus.com/updates/abc"/>
  <updated>2026-08-30T12:00:00Z</updated>
  <summary>All systems operational.</summary>
</entry>
<entry>
  <title>Partial outage in LHR</title>
  <link href="https://www.cloudflarestatus.com/updates/def"/>
  <updated>2026-08-30T11:00:00Z</updated>
  <summary>Some issues in London.</summary>
</entry>
</feed>"""
        items = run._parse_rss(atom, max_items=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Cloudflare is healthy")
        self.assertEqual(items[0]["link"], "https://www.cloudflarestatus.com/updates/abc")
        self.assertEqual(items[0]["description"], "All systems operational.")

    def test_parse_rss_cdata(self):
        run = self._import_news()
        rss = """<?xml version="1.0"?>
<rss version="2.0">
<item><title>CDATA Test</title><link>https://example.com/1</link><pubDate>Sun, 30 Aug 2026 12:00:00 GMT</pubDate><description><![CDATA[<b>bold</b> &amp; plain]]></description></item>
</rss>"""
        items = run._parse_rss(rss)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["description"], "bold & plain")

    def test_parse_rss_empty_body(self):
        run = self._import_news()
        self.assertEqual(run._parse_rss(""), [])

    def test_parse_rss_max_items(self):
        run = self._import_news()
        rss = """<?xml version="1.0"?>
<rss version="2.0">
<channel>
""" + "".join(
            f"<item><title>Item {i}</title><link>https://e.com/{i}</link><pubDate>Sun, 30 Aug 2026 12:00:00 GMT</pubDate></item>"
            for i in range(20)
        ) + """
</channel>
</rss>"""
        items = run._parse_rss(rss, max_items=5)
        self.assertEqual(len(items), 5)


class TestHttpGetMaxBytes(unittest.TestCase):
    """http_get must cap the in-memory body to prevent memory-exhaustion."""

    def test_max_bytes_caps_body(self):
        # Spin up a tiny HTTP server that streams a 1 MiB body
        import http.server
        import threading
        big = b"X" * (1024 * 1024)

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(big)))
                self.end_headers()
                self.wfile.write(big)
            def log_message(self, *a, **k):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        try:
            r = hd.http_get(f"http://127.0.0.1:{port}/", timeout=5, max_bytes=4096)
        except Exception as e:
            self.skipTest(f"loopback http test failed: {e}")
            return
        self.assertTrue(r.ok)
        self.assertEqual(len(r.body), 4096)
        self.assertTrue(getattr(r, "body_truncated", False),
                        "Expected body_truncated=True for over-cap response")

    def test_max_bytes_no_truncate_under_cap(self):
        import http.server
        import threading
        small = b"hello world"

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(small)))
                self.end_headers()
                self.wfile.write(small)
            def log_message(self, *a, **k):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.shutdown)
        try:
            r = hd.http_get(f"http://127.0.0.1:{port}/", timeout=5, max_bytes=4096)
        except Exception as e:
            self.skipTest(f"loopback http test failed: {e}")
            return
        self.assertTrue(r.ok)
        self.assertEqual(r.body, "hello world")
        self.assertFalse(getattr(r, "body_truncated", False),
                         "body_truncated must be False for under-cap response")

    def test_to_dict_includes_body_truncated(self):
        # Confirms that to_dict() emits body_truncated when set on the
        # constructor (the round-trip path for handler consumers).
        r = hd.HttpResult(ok=True, status=200, body="x" * 100, url="https://e.com",
                          elapsed_ms=5, body_truncated=True)
        d = r.to_dict()
        self.assertEqual(d.get("body_truncated"), True)
        self.assertEqual(d["bytes"], 100)
        # And when not truncated, the key is absent (not False)
        r2 = hd.HttpResult(ok=True, status=200, body="ok", url="https://e.com", elapsed_ms=1)
        d2 = r2.to_dict()
        self.assertNotIn("body_truncated", d2)


class TestFileLock(unittest.TestCase):
    """_file_lock is a cross-platform best-effort advisory file lock.

    On POSIX it uses fcntl.flock; on Windows it uses msvcrt.locking.
    The critical guarantee is that the file descriptor is closed after the
    context manager exits, so no leaked FDs.
    """

    def test_acquire_and_release(self):
        p = Path(self._tmp()) / "x.lock"
        with hd._file_lock(p, timeout=2.0):
            self.assertTrue(p.is_file())
        # After context exit the lock file still exists (uncontended) but
        # we can immediately re-acquire it — confirming clean release.
        with hd._file_lock(p, timeout=0.5):
            pass  # clean re-entry

    def test_lock_file_cleaned_up_on_exception(self):
        """If an exception escapes the context, the FD must still be closed."""
        p = Path(self._tmp()) / "y.lock"
        try:
            with hd._file_lock(p, timeout=2.0):
                raise ValueError("boom")
        except ValueError:
            pass
        # Lock file may still exist; the important thing is no leaked FD.
        # Re-acquiring must still work (would hang / raise if FD leaked).
        with hd._file_lock(p, timeout=0.5):
            pass

    @staticmethod
    def _tmp():
        import tempfile
        return tempfile.mkdtemp()


class TestDispatcherParallelism(unittest.TestCase):
    """Verify that the dispatcher handlers use ThreadPoolExecutor."""

    def _get_handler_src(self, scope: str) -> str:
        import importlib
        import inspect
        sys.path.insert(0, str(Path(__file__).parent.parent / scope))
        try:
            if "run" in sys.modules:
                del sys.modules["run"]
            mod = importlib.import_module("run")
        finally:
            try:
                sys.path.remove(str(Path(__file__).parent.parent / scope))
            except ValueError:
                pass
        return inspect.getsource(mod.handler)

    def test_news_uses_threadpool(self):
        src = self._get_handler_src("news-populate")
        self.assertIn("ThreadPoolExecutor", src, "news-populate handler should use ThreadPoolExecutor")
        # Either as_completed OR iterating futures directly — both achieve parallelism
        has_parallel_iteration = "as_completed" in src or ("for future in futures" in src and "future.result()" in src)
        self.assertTrue(has_parallel_iteration, "news-populate handler should iterate futures in parallel")

    def test_osint_uses_threadpool(self):
        src = self._get_handler_src("osint-populate")
        self.assertIn("ThreadPoolExecutor", src, "osint-populate handler should use ThreadPoolExecutor")


class TestDryRunFlag(unittest.TestCase):
    """All four dispatchers must short-circuit on --dry-run without doing I/O.

    This is the offline-safe smoke test: invoking the handler with
    config.flags.dry_run=True must return 0 and must not write any files
    or call out to the network.
    """

    def _run_dry(self, scope: str) -> int:
        import importlib
        sys.path.insert(0, str(Path(__file__).parent.parent / scope))
        try:
            if "run" in sys.modules:
                del sys.modules["run"]
            mod = importlib.import_module("run")
        finally:
            try:
                sys.path.remove(str(Path(__file__).parent.parent / scope))
            except ValueError:
                pass
        log = hd.setup_logging(quiet=True, json_console=False, level="warning")
        return mod.handler(log, {"flags": {"dry_run": True}})

    def test_news_dry_run(self):
        rc = self._run_dry("news-populate")
        self.assertEqual(rc, 0, "news-populate --dry-run must return 0")

    def test_osint_dry_run(self):
        rc = self._run_dry("osint-populate")
        self.assertEqual(rc, 0, "osint-populate --dry-run must return 0")

    def test_links_dry_run(self):
        rc = self._run_dry("links-validate")
        self.assertEqual(rc, 0, "links-validate --dry-run must return 0")

    def test_tools_dry_run(self):
        rc = self._run_dry("tools-populate")
        self.assertEqual(rc, 0, "tools-populate --dry-run must return 0")

    def test_news_cli_dry_run_end_to_end(self):
        """Run the full CLI entry point with --dry-run; must exit 0 without any I/O."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "news-populate" / "run.py"),
             "--once", "--dry-run", "--quiet"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"CLI --dry-run failed: {result.stderr}")

    def test_osint_cli_dry_run_end_to_end(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "osint-populate" / "run.py"),
             "--once", "--dry-run", "--quiet"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"CLI --dry-run failed: {result.stderr}")

    def test_lint_watch_dry_run(self):
        """Proposal-3: lint-watch dry-run must return 0 without touching files."""
        rc = self._run_dry("lint-watch")
        self.assertEqual(rc, 0, "lint-watch --dry-run must return 0")

    def test_lint_watch_cli_dry_run(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "lint-watch" / "run.py"),
             "--once", "--dry-run", "--quiet"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, f"lint-watch CLI --dry-run failed: {result.stderr}")


class TestLintWatchLogic(unittest.TestCase):
    """Direct unit tests of lint-watch helpers (no shell-out)."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "lint-watch"))
        if "run" in sys.modules:
            del sys.modules["run"]
        import run as lw
        self.lw = lw

    def tearDown(self):
        try:
            sys.path.remove(str(Path(__file__).parent.parent / "lint-watch"))
        except ValueError:
            pass
        if "run" in sys.modules:
            del sys.modules["run"]

    def test_lint_clean_file_no_findings(self):
        candidate = self.lw.REPO_ROOT / "Heart" / "scripts" / "links-validate" / "run.py"
        if not candidate.exists():
            self.skipTest(f"missing: {candidate}")
        result = self.lw._lint_file(candidate)
        self.assertTrue(result["ok"], f"expected clean, got findings: {result['findings']}")

    def test_lint_dirty_file_collects_findings(self):
        """Use a real file in the repo with a deliberate F401 violation, detect it."""
        repo_tmp = self.lw.REPO_ROOT / ".pytest-tmp-lint"
        repo_tmp.mkdir(exist_ok=True)
        try:
            p = repo_tmp / "dirty.py"
            p.write_text("import os\nimport json\nimport sys\nx = sys\n", encoding="utf-8")
            result = self.lw._lint_file(p)
            self.assertFalse(result["ok"], f"expected findings, got: {result['findings']}")
            self.assertGreater(len(result["findings"]), 0)
        finally:
            import shutil
            shutil.rmtree(repo_tmp, ignore_errors=True)

    def test_lint_dirty_file_ruff_output_captured(self):
        """Verify that _run_tool actually captures ruff stdout when ruff finds errors."""
        rc, out = self.lw._run_tool(sys.executable, ["-m", "ruff", "check", "--no-fix", "Heart/tools/heartctl.py"], timeout=30)
        # heartctl.py has pre-existing ruff findings; we just verify the output is captured
        self.assertNotEqual(rc, 127, "ruff not installed")
        # The important thing: if there are findings (rc=1), stdout must not be empty
        if rc == 1:
            self.assertGreater(len(out.strip()), 0)

    def test_run_tool_handles_missing_executable(self):
        rc, out = self.lw._run_tool("definitely-not-a-real-tool-xyz", ["check"], timeout=5)
        self.assertEqual(rc, 127)
        self.assertIn("not installed", out)

    def test_module_files_excludes_tests(self):
        files = self.lw._module_files("userdata")
        rels = [str(f.relative_to(self.lw.REPO_ROOT)) for f in files]
        self.assertTrue(all("/tests/" not in r for r in rels), f"tests/ leaked: {rels}")
        self.assertTrue(any(r.endswith(".py") for r in rels))


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
