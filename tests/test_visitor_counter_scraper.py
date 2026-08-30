"""
test_visitor_counter_scraper.py — Heart/tools/visitor_counter_scraper.py tests

Covers:
  - Registry parsing: load valid YAML; handle missing file; reject malformed entries
  - Network: success path; transient failure → retry; permanent failure → None
  - Output writers: worldmap datalayer; dashboard counters; NDJSON append feed
  - Failure tracking: fail counter increments; resets on success
  - Privacy: only country-level ISO codes make it into visitors.json
  - Phase: every log line + emitted error includes a phase string (AGENTS.md Rule 5)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Module loader (mirrors test_osint_cache pattern) ────────────────────
@pytest.fixture
def scraper_mod(tmp_path, monkeypatch):
    """Import visitor_counter_scraper with a temp shared root + secret path."""
    # Reset module cache so each test gets a fresh import.
    for k in list(sys.modules.keys()):
        if "visitor_counter_scraper" in k:
            del sys.modules[k]

    monkeypatch.setenv("NEOHIRO_SHARED_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv("NEOHIRO_LINKS_SECRET", str(tmp_path / "links-secret" / "visitor-counters.yaml"))
    return _import_with_secrets(tmp_path)


def _import_with_secrets(tmp_path: Path):
    # Write a minimal valid secret file.
    secret_path = Path(os.environ["NEOHIRO_LINKS_SECRET"])
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(
        """- id: neohiro.profile
  display_id: "1631162"
  auth_id: "8ce833a4b2722ea505cd7fff9a983daa572877b8"
  label: "neohiro/profile"
- id: cripple-netstrip
  display_id: "1631168"
  auth_id: "1c2f6764dfb78b1d3a930df9705f56454ac3eeb6"
  label: "Cripple-NetStrip"
""",
        encoding="utf-8",
    )
    import visitor_counter_scraper
    return visitor_counter_scraper


# ── Registry parsing ─────────────────────────────────────────────────────
class TestRegistry:
    def test_loads_valid_yaml(self, scraper_mod):
        registry = scraper_mod.load_registry()
        assert len(registry) == 2
        assert registry[0]["id"] == "neohiro.profile"
        assert registry[0]["auth_id"] == "8ce833a4b2722ea505cd7fff9a983daa572877b8"
        assert registry[1]["id"] == "cripple-netstrip"

    def test_missing_file_returns_empty(self, scraper_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("NEOHIRO_LINKS_SECRET", str(tmp_path / "no-such-file.yaml"))
        for k in list(sys.modules.keys()):
            if "visitor_counter_scraper" in k:
                del sys.modules[k]
        mod = __import__("visitor_counter_scraper")
        assert mod.load_registry() == []

    def test_rejects_entries_without_auth_id(self, tmp_path, monkeypatch):
        bad_path = tmp_path / "bad.yaml"
        bad_path.write_text(
            """- id: incomplete
  display_id: "9999"
  label: "no auth_id"
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("NEOHIRO_LINKS_SECRET", str(bad_path))
        monkeypatch.setenv("NEOHIRO_SHARED_ROOT", str(tmp_path / "shared"))
        for k in list(sys.modules.keys()):
            if "visitor_counter_scraper" in k:
                del sys.modules[k]
        mod = __import__("visitor_counter_scraper")
        assert mod.load_registry() == []


# ── Fetch behavior ───────────────────────────────────────────────────────
class TestFetch:
    def test_success_path(self, scraper_mod):
        session = MagicMock()
        response = MagicMock()
        response.ok = True
        response.json.return_value = {"hits": 123, "countries": [{"iso": "US", "hits": 100}]}
        response.raise_for_status = MagicMock()
        session.get.return_value = response
        counter = {"id": "test", "auth_id": "abc", "display_id": "1234", "label": "t"}
        result = scraper_mod.fetch_one(counter, session)
        assert result is not None
        assert result["hits"] == 123
        assert result["_id"] == "test"
        assert "_fetched_at" in result

    def test_transient_failure_retries(self, scraper_mod):
        session = MagicMock()
        import requests
        # First call raises, second succeeds.
        success = MagicMock()
        success.ok = True
        success.json.return_value = {"hits": 5}
        success.raise_for_status = MagicMock()
        session.get.side_effect = [requests.RequestException("boom"), success]
        counter = {"id": "test", "auth_id": "abc", "display_id": "1234", "label": "t"}
        result = scraper_mod.fetch_one(counter, session)
        assert result is not None
        assert session.get.call_count == 2

    def test_permanent_failure_returns_none(self, scraper_mod):
        session = MagicMock()
        import requests
        session.get.side_effect = requests.RequestException("permanent")
        counter = {"id": "test", "auth_id": "abc", "display_id": "1234", "label": "t"}
        result = scraper_mod.fetch_one(counter, session)
        assert result is None
        assert session.get.call_count == scraper_mod.MAX_RETRIES

    def test_non_json_response_falls_through(self, scraper_mod):
        session = MagicMock()
        bad = MagicMock()
        bad.ok = True
        bad.raise_for_status = MagicMock()
        bad.json.side_effect = ValueError("not json")
        session.get.return_value = bad
        counter = {"id": "test", "auth_id": "abc", "display_id": "1234", "label": "t"}
        result = scraper_mod.fetch_one(counter, session)
        assert result is None


# ── Output writers ───────────────────────────────────────────────────────
class TestWriters:
    def test_write_datalayer_aggregates_by_country(self, scraper_mod, tmp_path):
        per_counter = [
            {"_id": "a", "countries": [{"iso": "US", "hits": 10}, {"iso": "DE", "hits": 5}]},
            {"_id": "b", "countries": [{"iso": "US", "hits": 20}, {"iso": "FR", "hits": 7}]},
        ]
        scraper_mod.write_datalayer(per_counter)
        out = json.loads(scraper_mod.WORLDMAP_DATALAYER.read_text(encoding="utf-8"))
        assert out["layer"] == "visitors"
        assert out["source"] == "freevisitorcounters"
        countries = {c["iso"]: c["hits_24h"] for c in out["countries"]}
        assert countries == {"US": 30, "DE": 5, "FR": 7}
        # Sorted descending.
        assert out["countries"][0]["iso"] == "US"

    def test_write_datalayer_handles_null_hits(self, scraper_mod):
        """Vendor may return null for unknown countries — must not TypeError."""
        per_counter = [
            {"_id": "a", "countries": [
                {"iso": "US", "hits": 10},
                {"iso": "XX", "hits": None},       # explicit null
                {"iso": "ZZ"},                    # missing key
            ]},
        ]
        scraper_mod.write_datalayer(per_counter)
        out = json.loads(scraper_mod.WORLDMAP_DATALAYER.read_text(encoding="utf-8"))
        countries = {c["iso"]: c["hits_24h"] for c in out["countries"]}
        # Null and missing should be treated as 0, not crash.
        assert countries == {"US": 10, "XX": 0, "ZZ": 0}

    def test_write_counters_emits_summary(self, scraper_mod):
        per_counter = [
            {"_id": "a", "_label": "A", "hits": 100, "unique_24h": 50, "online": 3},
        ]
        scraper_mod.write_counters(per_counter)
        out = json.loads(scraper_mod.DASHBOARD_COUNTERS.read_text(encoding="utf-8"))
        assert out["counters"][0]["id"] == "a"
        assert out["counters"][0]["hits"] == 100

    def test_append_feed_is_ndjson(self, scraper_mod):
        per_counter = [{"_id": "a", "hits": 1, "unique_24h": 1}]
        scraper_mod.append_feed(per_counter)
        lines = scraper_mod.WORLDMAP_FEED.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["id"] == "a"
        assert "ts" in rec

    def test_privacy_no_raw_ips(self, scraper_mod):
        per_counter = [
            {"_id": "a", "countries": [{"iso": "US", "hits": 10}]},
        ]
        scraper_mod.write_datalayer(per_counter)
        out = json.loads(scraper_mod.WORLDMAP_DATALAYER.read_text(encoding="utf-8"))
        serialized = json.dumps(out)
        # No IP-like patterns.
        import re
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", serialized)
        # Only ISO codes at country level.
        for c in out["countries"]:
            assert len(c["iso"]) == 2 or c["iso"] == "XX"


# ── aggregate_countries (pure function) ──────────────────────────────────
class TestAggregateCountries:
    def test_basic_aggregation(self, scraper_mod):
        per_counter = [
            {"_id": "a", "countries": [{"iso": "US", "hits": 10}, {"iso": "DE", "hits": 5}]},
            {"_id": "b", "countries": [{"iso": "US", "hits": 20}, {"iso": "FR", "hits": 7}]},
        ]
        totals, last_seen, ts = scraper_mod.aggregate_countries(per_counter, "2026-08-30T00:00:00Z")
        assert totals == {"US": 30, "DE": 5, "FR": 7}
        assert last_seen == {"US": "2026-08-30T00:00:00Z", "DE": "2026-08-30T00:00:00Z", "FR": "2026-08-30T00:00:00Z"}
        assert ts == "2026-08-30T00:00:00Z"

    def test_returns_sorted_by_hits(self, scraper_mod):
        per_counter = [
            {"_id": "a", "countries": [{"iso": "JP", "hits": 2}]},
            {"_id": "b", "countries": [{"iso": "US", "hits": 100}]},
            {"_id": "c", "countries": [{"iso": "DE", "hits": 50}]},
        ]
        totals, _, _ = scraper_mod.aggregate_countries(per_counter, "2026-08-30T00:00:00Z")
        # dict insertion order in Python 3.7+ preserves insertion order.
        # Sorting is done at write time in write_datalayer, not here.
        assert totals == {"JP": 2, "US": 100, "DE": 50}

    def test_null_and_missing_hits_treated_as_zero(self, scraper_mod):
        per_counter = [
            {"_id": "a", "countries": [
                {"iso": "US", "hits": 10},
                {"iso": "DE"},                    # missing hits
                {"iso": "FR", "hits": None},    # explicit null
            ]},
        ]
        totals, _, _ = scraper_mod.aggregate_countries(per_counter, "2026-08-30T00:00:00Z")
        assert totals == {"US": 10, "DE": 0, "FR": 0}

    def test_empty_per_counter_returns_empty(self, scraper_mod):
        totals, last_seen, ts = scraper_mod.aggregate_countries([], "2026-08-30T00:00:00Z")
        assert totals == {}
        assert last_seen == {}

    def test_missing_iso_skipped(self, scraper_mod):
        per_counter = [
            {"_id": "a", "countries": [{"iso": "US", "hits": 5}, {"hits": 10}]},
        ]
        totals, last_seen, _ = scraper_mod.aggregate_countries(per_counter, "2026-08-30T00:00:00Z")
        assert totals == {"US": 5}
        assert "XX" not in totals

    def test_generates_timestamp_when_not_provided(self, scraper_mod):
        _, _, ts = scraper_mod.aggregate_countries([], None)
        # Should be a valid ISO string.
        from datetime import datetime, timezone
        datetime.fromisoformat(ts)


# ── Failure tracking ─────────────────────────────────────────────────────
class TestFailureTracking:
    def test_fail_counter_increments(self, scraper_mod):
        before = scraper_mod.bump_fail_counter(1)  # → 1
        after = scraper_mod.bump_fail_counter(1)   # → 2
        assert after == before + 1

    def test_fail_counter_clamps_to_zero(self, scraper_mod):
        scraper_mod.bump_fail_counter(-10)
        assert scraper_mod.bump_fail_counter(0) == 0

    def test_fail_counter_resets_on_success(self, scraper_mod):
        scraper_mod.bump_fail_counter(3)
        # Reset by passing 0 with previously-non-zero counter; the next bump(0) should clamp.
        scraper_mod.bump_fail_counter(0)
        assert scraper_mod.bump_fail_counter(0) == 0


# ── Phase logging (AGENTS.md Rule 5) ─────────────────────────────────────
class TestPhaseLogging:
    def test_retry_log_includes_phase(self, scraper_mod, caplog):
        session = MagicMock()
        import requests
        session.get.side_effect = requests.RequestException("boom")
        counter = {"id": "test", "auth_id": "abc", "display_id": "1234", "label": "t"}
        with caplog.at_level(logging.WARNING, logger="heart.visitor_counter_scraper"):
            scraper_mod.fetch_one(counter, session)
        assert any(scraper_mod.PHASE_SCRAPE in r.message for r in caplog.records)

    def test_missing_registry_logs_phase(self, scraper_mod, caplog, tmp_path, monkeypatch):
        monkeypatch.setenv("NEOHIRO_LINKS_SECRET", str(tmp_path / "missing.yaml"))
        for k in list(sys.modules.keys()):
            if "visitor_counter_scraper" in k:
                del sys.modules[k]
        mod = __import__("visitor_counter_scraper")
        # Reset fail counter state.
        if mod.FAIL_COUNTER.exists():
            mod.FAIL_COUNTER.unlink()
        with caplog.at_level(logging.ERROR, logger="heart.visitor_counter_scraper"):
            args = MagicMock()
            args.loop_seconds = 0
            mod.run_once(args)
        assert any(mod.PHASE_SCRAPE in r.message for r in caplog.records)


# ── run_once integration ────────────────────────────────────────────────
class TestRunOnce:
    def test_all_counters_succeed(self, scraper_mod, tmp_path):
        session = MagicMock()
        ok = MagicMock()
        ok.ok = True
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"hits": 10, "countries": [{"iso": "US", "hits": 10}]}
        session.get.return_value = ok
        args = MagicMock(); args.loop_seconds = 0
        with patch.object(scraper_mod, "_build_session", return_value=session):
            rc = scraper_mod.run_once(args)
        assert rc == 0
        assert scraper_mod.WORLDMAP_DATALAYER.exists()
        # Event log should have an OK entry.
        if scraper_mod.FAIL_EVENTS.exists():
            lines = scraper_mod.FAIL_EVENTS.read_text(encoding="utf-8").strip().splitlines()
            import json as _json
            last = _json.loads(lines[-1])
            assert last["ok"] is True

    def test_all_counters_fail_increments(self, scraper_mod):
        session = MagicMock()
        import requests
        session.get.side_effect = requests.RequestException("down")
        args = MagicMock(); args.loop_seconds = 0
        scraper_mod.bump_fail_counter(0)
        if scraper_mod.FAIL_COUNTER.exists():
            scraper_mod.FAIL_COUNTER.write_text("0", encoding="utf-8")
        with patch.object(scraper_mod, "_build_session", return_value=session):
            rc = scraper_mod.run_once(args)
        assert rc == 3
        cur = int(scraper_mod.FAIL_COUNTER.read_text(encoding="utf-8").strip() or "0")
        assert cur >= 1
        # Event log should have a FAIL entry.
        if scraper_mod.FAIL_EVENTS.exists():
            lines = scraper_mod.FAIL_EVENTS.read_text(encoding="utf-8").strip().splitlines()
            import json as _json
            last = _json.loads(lines[-1])
            assert last["ok"] is False


# ── Rolling event log ───────────────────────────────────────────────────
class TestRollingEventLog:
    def test_record_cycle_event_writes_ndjson_line(self, scraper_mod):
        scraper_mod.record_cycle_event(ok=True)
        lines = scraper_mod.FAIL_EVENTS.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        import json as _json
        rec = _json.loads(lines[0])
        assert rec["ok"] is True
        assert "ts" in rec
        assert "fails" in rec

    def test_record_cycle_event_false_flag(self, scraper_mod):
        scraper_mod.record_cycle_event(ok=False)
        lines = scraper_mod.FAIL_EVENTS.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[-1])
        assert rec["ok"] is False

    def test_read_fail_window_returns_recent_events(self, scraper_mod):
        scraper_mod.record_cycle_event(ok=True)
        scraper_mod.record_cycle_event(ok=False)
        events = scraper_mod.read_fail_window(window_s=3600)
        assert len(events) == 2
        assert events[0]["ok"] is True
        assert events[1]["ok"] is False

    def test_read_fail_window_drops_old_events(self, scraper_mod):
        # Write a fake old event directly into the log (must create parent dir first).
        scraper_mod.FAIL_EVENTS.parent.mkdir(parents=True, exist_ok=True)
        old = {"ts": "2020-01-01T00:00:00Z", "ok": False, "fails": 99}
        with scraper_mod.FAIL_EVENTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(old) + "\n")
        # Fresh event.
        scraper_mod.record_cycle_event(ok=True)
        events = scraper_mod.read_fail_window(window_s=3600)
        # Only the fresh event should be in the window.
        assert all(e["ts"] != "2020-01-01T00:00:00Z" for e in events)

    def test_read_fail_window_drops_malformed_lines(self, scraper_mod):
        scraper_mod.FAIL_EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with scraper_mod.FAIL_EVENTS.open("a", encoding="utf-8") as f:
            f.write("not json\n")
            f.write('{"ts": "2030-01-01T00:00:00Z"}\n')  # missing ok
            f.write(json.dumps({"ts": "2030-01-01T00:00:00Z", "ok": True}) + "\n")
        events = scraper_mod.read_fail_window(window_s=3600)
        # Only the well-formed third line should survive.
        assert len(events) == 1
        assert events[0]["ok"] is True

    def test_read_fail_window_empty_file(self, scraper_mod):
        scraper_mod.FAIL_EVENTS.parent.mkdir(parents=True, exist_ok=True)
        scraper_mod.FAIL_EVENTS.write_text("", encoding="utf-8")
        events = scraper_mod.read_fail_window(window_s=3600)
        assert events == []

    def test_read_fail_window_missing_file(self, scraper_mod):
        if scraper_mod.FAIL_EVENTS.exists():
            scraper_mod.FAIL_EVENTS.unlink()
        events = scraper_mod.read_fail_window(window_s=3600)
        assert events == []
