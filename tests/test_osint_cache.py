"""
test_osint_cache.py — Heart osint_cache.py tests

Covers:
  - READ: load existing cache; load missing cache; load corrupted cache
  - AMEND: new IP creates new entry; existing IP renews last_seen;
           country drift detected; VPN/Tor/Proxy changes flagged
  - WRITE: atomic write to .tmp + rename; survives concurrent reads
  - TTL: stale observations pruned; self-renewing on re-observation
  - Phase: run_phase integrates all three (READ → AMEND → WRITE + enqueue)
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def osint_mod():
    """Fresh import of osint_cache and userdata.ghosts per test to avoid state leakage."""
    for k in list(sys.modules.keys()):
        if "osint_cache" in k or k.startswith("userdata."):
            del sys.modules[k]
    import osint_cache
    return osint_cache


@pytest.fixture
def bp(tmp_path) -> Path:
    """Fresh Brain path for each test. Also remove any stale lock from a prior
    crashed run so the lock acquisition does not timeout waiting for a dead owner."""
    p = tmp_path / "Brain"
    p.mkdir()
    (p / "heartbeat").mkdir()
    # Stale lock can survive a Ctrl-C or process kill between tests.
    stale_lock = p / "heartbeat" / ".osint_run_phase.lock"
    try:
        stale_lock.rmdir()
    except OSError:
        pass
    return p


# ── Hashing ────────────────────────────────────────────────────────────────

class TestHashing:
    def test_hash_ip_deterministic(self, osint_mod):
        h1 = osint_mod._hash_ip("192.0.2.1")
        h2 = osint_mod._hash_ip("192.0.2.1")
        assert h1 == h2

    def test_hash_ip_format(self, osint_mod):
        h = osint_mod._hash_ip("192.0.2.1")
        assert h.startswith("ip-hash:")
        assert len(h) == len("ip-hash:") + 32

    def test_hash_ip_different_inputs_different_hashes(self, osint_mod):
        assert osint_mod._hash_ip("192.0.2.1") != osint_mod._hash_ip("192.0.2.2")

    def test_hash_ip_strips_whitespace(self, osint_mod):
        assert osint_mod._hash_ip("  192.0.2.1  ") == osint_mod._hash_ip("192.0.2.1")

    def test_ipv6_mapped_v4_normalizes_to_ipv4(self, osint_mod):
        raw = "192.0.2.1"
        mapped = "::ffff:192.0.2.1"
        assert osint_mod._hash_ip(raw) == osint_mod._hash_ip(mapped)

    def test_ipv6_mapped_v4_case_insensitive(self, osint_mod):
        raw = "192.0.2.1"
        mapped_lower = "::ffff:192.0.2.1"
        mapped_upper = "::FFFF:192.0.2.1"
        assert osint_mod._hash_ip(raw) == osint_mod._hash_ip(mapped_lower)
        assert osint_mod._hash_ip(raw) == osint_mod._hash_ip(mapped_upper)

    def test_ipv6_mapped_v4_prevents_correlation(self, osint_mod):
        ipv4 = "192.0.2.1"
        mapped = "::ffff:192.0.2.1"
        bare_ipv6 = "2001:db8::1"
        h_ipv4 = osint_mod._hash_ip(ipv4)
        h_mapped = osint_mod._hash_ip(mapped)
        h_bare = osint_mod._hash_ip(bare_ipv6)
        assert h_ipv4 == h_mapped
        assert h_ipv4 != h_bare


# ── Load (READ) ─────────────────────────────────────────────────────────────

class TestLoad:
    def test_load_missing_returns_empty(self, osint_mod, bp):
        cache = osint_mod.load(bp)
        assert cache["version"] == osint_mod.CACHE_VERSION
        assert cache["observations"] == {}
        assert cache["pruned"] == 0

    def test_load_corrupt_returns_empty(self, osint_mod, bp):
        cache_file = bp / "heartbeat" / osint_mod.CACHE_FILE
        cache_file.write_text("{ this is not json")
        cache = osint_mod.load(bp)
        assert cache["observations"] == {}
        backup = cache_file.with_suffix(".bak")
        assert backup.exists(), "corrupt cache should be backed up"

    def test_load_prunes_expired(self, osint_mod, bp):
        # Plant an observation that's older than TTL
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()
        cache = {
            "version": 1,
            "observations": {
                "ip-hash:old": {"ip_hash": "ip-hash:old", "last_seen": old_ts},
                "ip-hash:new": {"ip_hash": "ip-hash:new", "last_seen": new_ts},
            },
        }
        cache_file = bp / "heartbeat" / osint_mod.CACHE_FILE
        cache_file.write_text(json.dumps(cache))

        loaded = osint_mod.load(bp)
        assert "ip-hash:old" not in loaded["observations"]
        assert "ip-hash:new" in loaded["observations"]
        assert loaded["pruned"] == 1

    def test_load_keeps_observations_within_ttl(self, osint_mod, bp):
        # TTL default is 60 minutes — observation 30 min old should survive
        ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        cache = {
            "version": 1,
            "observations": {
                "ip-hash:abc": {"ip_hash": "ip-hash:abc", "last_seen": ts}
            },
        }
        cache_file = bp / "heartbeat" / osint_mod.CACHE_FILE
        cache_file.write_text(json.dumps(cache))
        loaded = osint_mod.load(bp)
        assert "ip-hash:abc" in loaded["observations"]


# ── Amend one observation ──────────────────────────────────────────────────

class TestAmendObservation:
    def test_new_ip_creates_entry(self, osint_mod):
        raw = {
            "ip": "192.0.2.1",
            "country": "Belgium",
            "country_code": "BE",
            "isp": "Proximus",
        }
        amended = osint_mod._amend_observation(None, raw)
        assert amended["country"] == "Belgium"
        assert amended["country_code"] == "BE"
        assert amended["isp"] == "Proximus"
        assert amended["first_seen"] == amended["last_seen"]
        assert amended["_signal"] == "new_ip"
        assert amended["ip_hash"].startswith("ip-hash:")

    def test_existing_ip_renews_last_seen(self, osint_mod):
        existing = {
            "ip_hash": osint_mod._hash_ip("192.0.2.1"),
            "country": "Belgium",
            "country_code": "BE",
            "isp": "Proximus",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "last_country": "BE", "last_country_code": "BE",
            "geo_drift_count": 0, "last_drift_at": None,
            "signals_enqueued": [], "tags": [],
        }
        time.sleep(0.01)  # ensure timestamps differ
        raw = {"ip": "192.0.2.1", "country": "Belgium", "country_code": "BE"}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["last_seen"] != "2026-01-01T00:00:00+00:00"
        assert amended["first_seen"] == "2026-01-01T00:00:00+00:00"
        assert "_signal" not in amended  # no change = no signal

    def test_country_drift_detected(self, osint_mod):
        existing = {
            "ip_hash": osint_mod._hash_ip("192.0.2.1"),
            "country": "Belgium", "country_code": "BE",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "last_country": "BE", "last_country_code": "BE",
            "geo_drift_count": 0, "last_drift_at": None,
            "signals_enqueued": [], "tags": [],
        }
        raw = {"ip": "192.0.2.1", "country": "France", "country_code": "FR"}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["_signal"] == "geo_drift"
        assert amended["geo_drift_count"] == 1
        assert amended["last_drift_at"] is not None

    def test_repeated_drift_increments(self, osint_mod):
        existing = {
            "ip_hash": osint_mod._hash_ip("192.0.2.1"),
            "country": "Belgium", "country_code": "BE",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "last_country": "FR", "last_country_code": "FR",
            "geo_drift_count": 1, "last_drift_at": "2026-01-01T00:00:00+00:00",
            "signals_enqueued": [], "tags": [],
        }
        raw = {"ip": "192.0.2.1", "country": "Germany", "country_code": "DE"}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["geo_drift_count"] == 2

    def test_vpn_flag_emergence_signals(self, osint_mod):
        existing = {
            "ip_hash": osint_mod._hash_ip("192.0.2.1"),
            "country": "Belgium", "country_code": "BE",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "last_country": "BE", "last_country_code": "BE",
            "geo_drift_count": 0, "last_drift_at": None,
            "signals_enqueued": [], "tags": [],
        }
        raw = {"ip": "192.0.2.1", "is_vpn": True}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["is_vpn"] is True
        assert amended["_signal"] == "is_vpn"

    def test_tor_flag_emergence_signals(self, osint_mod):
        existing = {
            "ip_hash": osint_mod._hash_ip("192.0.2.1"),
            "country": "BE", "country_code": "BE",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "last_country": "BE", "last_country_code": "BE",
            "geo_drift_count": 0, "last_drift_at": None,
            "signals_enqueued": [], "tags": [],
        }
        raw = {"ip": "192.0.2.1", "is_tor": True}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["is_tor"] is True
        assert amended["_signal"] == "is_tor"

    def test_enrichment_merged_with_prefix(self, osint_mod):
        raw = {
            "ip": "192.0.2.1", "country": "BE", "country_code": "BE",
            "enrichment": {"asn": "12345", "org": "Proximus", "is_datacenter": False}
        }
        amended = osint_mod._amend_observation(None, raw)
        assert amended["enrich_asn"] == "12345"
        assert amended["enrich_org"] == "Proximus"
        assert amended["enrich_is_datacenter"] is False

    def test_empty_country_does_not_overwrite(self, osint_mod):
        existing = {
            "ip_hash": osint_mod._hash_ip("192.0.2.1"),
            "country": "Belgium", "country_code": "BE", "isp": "Proximus",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-01T00:00:00+00:00",
            "last_country": "BE", "last_country_code": "BE",
            "geo_drift_count": 0, "last_drift_at": None,
            "signals_enqueued": [], "tags": [],
        }
        raw = {"ip": "192.0.2.1"}  # no country
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["country"] == "Belgium"
        assert amended["country_code"] == "BE"


# ── Save (WRITE) ───────────────────────────────────────────────────────────

class TestSave:
    def test_save_creates_file(self, osint_mod, bp):
        cache = osint_mod._empty_cache()
        cache["observations"]["ip-hash:test"] = {"ip_hash": "ip-hash:test"}
        osint_mod.save(bp, cache)
        assert (bp / "heartbeat" / osint_mod.CACHE_FILE).exists()

    def test_save_uses_atomic_rename(self, osint_mod, bp):
        """Verify no .tmp file left after save (atomic rename)."""
        cache = osint_mod._empty_cache()
        osint_mod.save(bp, cache)
        # No stale .tmp files
        assert not (bp / "heartbeat" / f"{osint_mod.CACHE_FILE}.tmp").exists() \
            if (bp / "heartbeat" / f"{osint_mod.CACHE_FILE}.tmp").exists() \
            else True
        # The main file exists
        assert (bp / "heartbeat" / osint_mod.CACHE_FILE).exists()

    def test_save_creates_parent_dirs(self, osint_mod, tmp_path):
        bp = tmp_path / "deep" / "nested" / "Brain"
        cache = osint_mod._empty_cache()
        osint_mod.save(bp, cache)
        assert (bp / "heartbeat" / osint_mod.CACHE_FILE).exists()

    def test_save_overwrites_existing(self, osint_mod, bp):
        cache_file = bp / "heartbeat" / osint_mod.CACHE_FILE
        cache_file.write_text('{"old": true}')
        cache = osint_mod._empty_cache()
        osint_mod.save(bp, cache)
        loaded = json.loads(cache_file.read_text())
        assert "old" not in loaded


# ── Enqueue signal ─────────────────────────────────────────────────────────

class TestEnqueueSignal:
    def test_enqueue_creates_signal_file(self, osint_mod, bp):
        osint_mod.enqueue_signal(
            ip_hash="ip-hash:abc",
            signal_type="ip_observed",
            fields={"ip": "ip-hash:abc", "country": "BE"},
            brain_path=bp,
        )
        inbox = bp / "heartbeat" / "abuse_signals"
        assert inbox.exists()
        files = list(inbox.glob("osint_*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data["source"] == "heartbeat_osint"
        assert data["entity_id"] == "ip-hash:abc"
        assert data["signal_type"] == "ip_observed"

    def test_enqueue_filename_safe(self, osint_mod, bp):
        """Filenames must not contain reserved characters."""
        osint_mod.enqueue_signal(
            ip_hash="ip-hash:abc/def",
            signal_type="ip_observed",
            fields={},
            brain_path=bp,
        )
        files = list((bp / "heartbeat" / "abuse_signals").glob("*.json"))
        assert len(files) == 1
        assert "/" not in files[0].name
        assert ":" not in files[0].name


# ── Load incoming raw observations ─────────────────────────────────────────

class TestLoadIncoming:
    def test_empty_inbox(self, osint_mod, bp):
        assert osint_mod._load_incoming(bp) == []

    def test_reads_and_clears_inbox(self, osint_mod, bp):
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({
            "ip": "192.0.2.1", "country": "BE", "country_code": "BE"
        }))
        (inbox / "obs2.json").write_text(json.dumps({
            "ip": "192.0.2.2", "country": "FR", "country_code": "FR"
        }))

        raw = osint_mod._load_incoming(bp)
        assert len(raw) == 2
        # Inbox should be cleared (raw files consumed)
        assert list(inbox.glob("*.json")) == []

    def test_skips_files_without_ip(self, osint_mod, bp):
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "no_ip.json").write_text(json.dumps({"country": "BE"}))
        assert osint_mod._load_incoming(bp) == []
        assert list(inbox.glob("*.json")) == []  # deleted

    def test_skips_corrupt_json(self, osint_mod, bp):
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "bad.json").write_text("{ not json")
        assert osint_mod._load_incoming(bp) == []
        assert list(inbox.glob("*.json")) == []  # deleted


# ── Top-level run_phase (READ → AMEND → WRITE + enqueue) ──────────────────

class TestRunPhase:
    def test_empty_cycle(self, osint_mod, bp):
        result = osint_mod.run_phase(bp)
        assert result["ok"] is True
        assert result["observations_seen"] == 0
        assert result["new_ips"] == 0
        assert result["signals_enqueued"] == 0
        assert (bp / "heartbeat" / osint_mod.CACHE_FILE).exists()

    def test_new_ip_enqueues_signal(self, osint_mod, bp):
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({
            "ip": "192.0.2.1", "country": "Belgium", "country_code": "BE"
        }))
        result = osint_mod.run_phase(bp)
        assert result["new_ips"] == 1
        assert result["signals_enqueued"] == 1
        assert result["cache_size"] == 1

        # Signal file should be created
        signals = list((bp / "heartbeat" / "abuse_signals").glob("osint_*.json"))
        assert len(signals) == 1

        # Cache should contain the observation
        cache = json.loads((bp / "heartbeat" / osint_mod.CACHE_FILE).read_text())
        assert cache["cache_size" if "cache_size" in cache else "observations"]

    def test_renewing_observation_no_signal(self, osint_mod, bp):
        # Cycle 1: new IP
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "BE"}))
        r1 = osint_mod.run_phase(bp)
        assert r1["new_ips"] == 1

        # Cycle 2: same IP — should NOT enqueue (renewal only)
        (inbox / "obs2.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "BE"}))
        r2 = osint_mod.run_phase(bp)
        assert r2["new_ips"] == 0
        assert r2["signals_enqueued"] == 0
        assert r2["cache_size"] == 1

    def test_geo_drift_enqueues_drift_signal(self, osint_mod, bp):
        # Cycle 1: BE
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "BE"}))
        osint_mod.run_phase(bp)
        # Clear signal inbox
        for f in (bp / "heartbeat" / "abuse_signals").glob("*.json"):
            f.unlink()

        # Cycle 2: same IP, FR — drift!
        (inbox / "obs2.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "FR"}))
        r2 = osint_mod.run_phase(bp)
        assert r2["geo_drifts"] == 1
        assert r2["signals_enqueued"] == 1

        signals = list((bp / "heartbeat" / "abuse_signals").glob("osint_*.json"))
        assert len(signals) == 1
        data = json.loads(signals[0].read_text())
        assert data["signal_type"] == "ip_drift"

    def test_prune_stale_observations(self, osint_mod, bp):
        # Pre-populate with a stale observation
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        cache = {
            "version": 1,
            "observations": {
                "ip-hash:stale": {
                    "ip_hash": "ip-hash:stale",
                    "last_seen": old_ts,
                    "country_code": "BE",
                }
            },
        }
        cache_file = bp / "heartbeat" / osint_mod.CACHE_FILE
        cache_file.write_text(json.dumps(cache))

        result = osint_mod.run_phase(bp)
        assert result["pruned"] == 1
        assert result["cache_size"] == 0

    def test_observation_renewed_by_re_observation(self, osint_mod, bp):
        """Self-evolution: live observation survives by being re-observed."""
        # Pre-populate with an observation 30 min old (within TTL)
        ts_30min_ago = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        cache = {
            "version": 1,
            "observations": {
                osint_mod._hash_ip("192.0.2.1"): {
                    "ip_hash": osint_mod._hash_ip("192.0.2.1"),
                    "last_seen": ts_30min_ago,
                    "country_code": "BE",
                    "is_vpn": False, "is_tor": False, "is_proxy": False,
                }
            },
        }
        cache_file = bp / "heartbeat" / osint_mod.CACHE_FILE
        cache_file.write_text(json.dumps(cache))

        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "BE"}))
        result = osint_mod.run_phase(bp)
        assert result["cache_size"] == 1
        assert result["pruned"] == 0

        loaded = json.loads(cache_file.read_text())
        last_seen = loaded["observations"][osint_mod._hash_ip("192.0.2.1")]["last_seen"]
        # last_seen should be very recent
        parsed = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        delta = (datetime.now(timezone.utc) - parsed).total_seconds()
        assert delta < 5, f"last_seen should be fresh, got delta={delta}s"

    def test_vpn_emergence_enqueues_signal(self, osint_mod, bp):
        # Cycle 1: not VPN
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "BE"}))
        osint_mod.run_phase(bp)
        for f in (bp / "heartbeat" / "abuse_signals").glob("*.json"):
            f.unlink()

        # Cycle 2: now VPN
        (inbox / "obs2.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "BE", "is_vpn": True}))
        r2 = osint_mod.run_phase(bp)
        assert r2["signals_enqueued"] == 1
        assert r2["cache_size"] == 1

    def test_multiple_ips_in_one_cycle(self, osint_mod, bp):
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (inbox / f"obs{i}.json").write_text(json.dumps({
                "ip": f"192.0.2.{i}",
                "country_code": "BE"
            }))
        result = osint_mod.run_phase(bp)
        assert result["new_ips"] == 5
        assert result["cache_size"] == 5
        assert result["signals_enqueued"] == 5

    def test_idempotent_under_replay(self, osint_mod, bp):
        """Running the same cycle twice with same data should produce stable state."""
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({"ip": "192.0.2.1", "country_code": "BE"}))
        r1 = osint_mod.run_phase(bp)

        # Replay same data (no new inbox files)
        r2 = osint_mod.run_phase(bp)
        assert r2["new_ips"] == 0
        assert r2["signals_enqueued"] == 0
        assert r2["cache_size"] == r1["cache_size"]


# ── Privacy ────────────────────────────────────────────────────────────────

class TestPrivacy:
    def test_no_raw_ip_in_cache(self, osint_mod, bp):
        """Cache must never contain raw IP — only ip-hash:<sha256>."""
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs.json").write_text(json.dumps({
            "ip": "192.0.2.42", "country_code": "BE"
        }))
        osint_mod.run_phase(bp)
        cache_text = (bp / "heartbeat" / osint_mod.CACHE_FILE).read_text()
        assert "192.0.2.42" not in cache_text
        assert "ip-hash:" in cache_text

    def test_no_raw_ip_in_enqueued_signal(self, osint_mod, bp):
        """Signal files must use ip-hash, not raw IP."""
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs.json").write_text(json.dumps({
            "ip": "192.0.2.42", "country_code": "BE"
        }))
        osint_mod.run_phase(bp)
        for f in (bp / "heartbeat" / "abuse_signals").glob("osint_*.json"):
            content = f.read_text()
            assert "192.0.2.42" not in content
            assert "ip-hash:" in content


# ── Safe filename ──────────────────────────────────────────────────────────

class TestSafeFilename:
    def test_colon_replaced(self, osint_mod):
        assert ":" not in osint_mod._safe_filename("ip-hash:abc")

    def test_slash_replaced(self, osint_mod):
        assert "/" not in osint_mod._safe_filename("path/with/slashes")
        assert "\\" not in osint_mod._safe_filename("path\\with\\backslashes")

    def test_empty(self, osint_mod):
        assert osint_mod._safe_filename("") == "x"

    def test_reserved_chars_replaced(self, osint_mod):
        out = osint_mod._safe_filename("foo;rm -rf /")
        assert ";" not in out


# ── Dedup fingerprint ──────────────────────────────────────────────────────

class TestFingerprint:
    def test_fingerprint_deterministic(self, osint_mod):
        obs = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        assert osint_mod._fingerprint(obs) == osint_mod._fingerprint(obs)

    def test_fingerprint_changes_on_country(self, osint_mod):
        base = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        changed = {"country_code": "FR", "is_vpn": False, "is_tor": False, "is_proxy": False}
        assert osint_mod._fingerprint(base) != osint_mod._fingerprint(changed)

    def test_fingerprint_changes_on_vpn(self, osint_mod):
        base = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        changed = {"country_code": "BE", "is_vpn": True, "is_tor": False, "is_proxy": False}
        assert osint_mod._fingerprint(base) != osint_mod._fingerprint(changed)

    def test_fingerprint_ignores_volatile_fields(self, osint_mod):
        base = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False,
                "last_seen": "2026-01-01T00:00:00Z", "geo_drift_count": 3}
        bare = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        assert osint_mod._fingerprint(base) == osint_mod._fingerprint(bare)

    def test_fingerprint_12_chars(self, osint_mod):
        obs = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        fp = osint_mod._fingerprint(obs)
        assert len(fp) == 12
        assert fp.isalnum()

    def test_is_unchanged_true_when_fp_matches(self, osint_mod):
        existing_fp = osint_mod._fingerprint({"country_code": "BE", "is_vpn": False,
                                              "is_tor": False, "is_proxy": False})
        existing = {"ip_hash": "ip-hash:abc", "_fp": existing_fp}
        raw = {"country_code": "BE", "is_vpn": False}
        assert osint_mod.is_unchanged(existing, raw) is True

    def test_is_unchanged_false_when_fp_differs(self, osint_mod):
        existing_fp = osint_mod._fingerprint({"country_code": "BE", "is_vpn": False,
                                              "is_tor": False, "is_proxy": False})
        existing = {"ip_hash": "ip-hash:abc", "_fp": existing_fp}
        raw = {"country_code": "FR", "is_vpn": False}  # country changed
        assert osint_mod.is_unchanged(existing, raw) is False

    def test_is_unchanged_false_when_no_existing(self, osint_mod):
        raw = {"country_code": "BE", "is_vpn": False}
        assert osint_mod.is_unchanged(None, raw) is False

# ── /userdata write path ───────────────────────────────────────────────────

class TestWriteToUserdata:
    def test_write_creates_ghostprofile_file(self, osint_mod, bp, monkeypatch):
        """write_to_userdata delegates to userdata.ghosts.upsert; on-disk format
        is a canonical GhostProfile (JSON, GhostProfile.to_dict() output)."""
        ud_dir = bp / "userdata"
        monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
        obs = {
            "ip_hash": "ip-hash:canon1",
            "country_code": "BE",
            "is_vpn": False,
            "is_tor": False,
            "is_proxy": False,
            "geo_drift_count": 0,
            "last_drift_at": None,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
            "_fp": "canon_fp_abc",
        }
        written, reason = osint_mod.write_to_userdata(obs, ud_dir)
        assert written is True
        assert reason == "ok"
        # Canonical path: {USERDATA_DIR}/ghosts/ip-hash_canon1.json
        ghost_file = ud_dir / "ghosts" / "ip-hash_canon1.json"
        assert ghost_file.exists()
        content = json.loads(ghost_file.read_text())
        assert content["ghost_id"] == "ip-hash:canon1"
        assert content["country_code"] == "BE"
        assert content["_fp"] == "canon_fp_abc"
        assert "192.0.2" not in json.dumps(content)  # raw IP never written

    def test_dedup_skips_unchanged(self, osint_mod, bp, monkeypatch):
        """Two writes with the same _fp: second is a no-op (on-disk fp matches)."""
        ud_dir = bp / "userdata"
        monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
        fp = osint_mod._fingerprint({
            "country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False
        })
        obs = {
            "ip_hash": "ip-hash:dedup1",
            "country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00Z", "last_seen": "2026-01-01T00:00:00Z",
            "_fp": fp,
        }
        w1, r1 = osint_mod.write_to_userdata(obs, ud_dir)
        assert w1 is True
        w2, r2 = osint_mod.write_to_userdata(obs, ud_dir)
        assert w2 is False
        assert "unchanged" in r2

    def test_denies_raw_ip(self, osint_mod, bp, monkeypatch):
        """Raw 'ip' field is in the deny-list and must not appear on disk."""
        ud_dir = bp / "userdata"
        monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
        obs = {
            "ip_hash": "ip-hash:ipstriptest",
            "country_code": "BE",
            "ip": "192.0.2.42",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
            "_fp": "abc123",
        }
        written, reason = osint_mod.write_to_userdata(obs, ud_dir)
        assert written is True
        on_disk = json.loads(
            (ud_dir / "ghosts" / "ip-hash_ipstriptest.json").read_text()
        )
        assert "192.0.2.42" not in json.dumps(on_disk)
        assert "ip" not in on_disk  # no 'ip' key in GhostProfile

    def test_raw_ip_value_in_ip_hash_key_is_allowed(self, osint_mod, bp, monkeypatch):
        """ip_hash is allowlisted; ip-hash: prefix is safe."""
        ud_dir = bp / "userdata"
        monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
        obs = {
            "ip_hash": "ip-hash:abc1234567890abcdef1234567890ab",
            "country_code": "BE",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
            "_fp": "abc",
        }
        written, reason = osint_mod.write_to_userdata(obs, ud_dir)
        assert written is True

    def test_denies_country_field(self, osint_mod, bp, monkeypatch):
        """The deny-list strips 'country'; verify it never appears on disk."""
        ud_dir = bp / "userdata"
        monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
        obs = {
            "ip_hash": "ip-hash:denytest",
            "country_code": "BE",
            "country": "Belgium",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
            "_fp": osint_mod._fingerprint({
                "country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False
            }),
        }
        written, reason = osint_mod.write_to_userdata(obs, ud_dir)
        assert written is True
        on_disk = json.loads(
            (ud_dir / "ghosts" / "ip-hash_denytest.json").read_text()
        )
        assert "country" not in on_disk  # GhostProfile has country_code, not country

    def test_write_and_read_via_ghosts(self, osint_mod, bp, monkeypatch):
        """Write via osint_cache, read back via userdata.ghosts.get()."""
        ud_dir = bp / "userdata"
        monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
        fp = osint_mod._fingerprint({
            "country_code": "FR", "is_vpn": True, "is_tor": False, "is_proxy": False
        })
        obs = {
            "ip_hash": "ip-hash:round1",
            "country_code": "FR",
            "is_vpn": True,
            "is_tor": False,
            "is_proxy": False,
            "geo_drift_count": 1,
            "last_drift_at": "2026-07-01T00:00:00Z",
            "first_seen": "2026-06-01T00:00:00Z",
            "last_seen": "2026-08-30T00:00:00Z",
            "_fp": fp,
        }
        osint_mod.write_to_userdata(obs, ud_dir)
        # Read back using the canonical ghosts.get() — confirms the schema contract
        ghosts = osint_mod._ghosts()
        gp = ghosts.get("ip-hash:round1")
        assert gp is not None
        assert gp.country_code == "FR"
        assert gp.is_vpn is True
        assert gp.geo_drift_count == 1
        assert gp._fp == fp

    def test_corrupt_fp_not_present(self, osint_mod, bp, monkeypatch):
        """A GhostProfile written without _fp: dedup check has nothing to compare,
        so it proceeds with a write (safe behavior — overwrites stale record)."""
        ud_dir = bp / "userdata"
        monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
        ip_hash = "ip-hash:no_fp"
        ghost_file = ud_dir / "ghosts" / "ip-hash_no_fp.json"
        ghost_file.parent.mkdir(parents=True, exist_ok=True)
        # Write a canonical GhostProfile WITHOUT _fp
        ghost_file.write_text(json.dumps({
            "ghost_id": ip_hash,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
            "signals": {"type": "ip_observed", "source": "heartbeat_osint"},
            "trust_score": 0, "tags": ["mentioned"], "mentions": 0,
            "role": "none", "schema_version": 1,
            "country_code": "BE",
        }))
        # _fp in on-disk is None → _userdata_ghost_fingerprint_via_ghosts returns None
        # → no on_disk_fp → dedup check passes → write proceeds
        obs = {
            "ip_hash": ip_hash,
            "country_code": "FR",
            "is_vpn": False, "is_tor": False, "is_proxy": False,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
            "_fp": "new_fp",
        }
        written, reason = osint_mod.write_to_userdata(obs, ud_dir)
        assert written is True  # no on-disk fp to compare → write proceeds


# ── run_phase integration ──────────────────────────────────────────────────

class TestRunPhaseUserdata:
    def test_run_phase_returns_userdata_counts(self, osint_mod, bp, monkeypatch):
        monkeypatch.setenv("USERDATA_DIR", str(bp / "userdata"))
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs.json").write_text(json.dumps({
            "ip": "192.0.2.99", "country_code": "BE"
        }))
        result = osint_mod.run_phase(bp)
        assert "userdata_writes" in result
        assert "userdata_skipped" in result
        assert "userdata_pending_drained" in result
        assert result["userdata_writes"] >= 1
        assert result["userdata_skipped"] >= 0

    def test_run_phase_dedup_skips_userdata_on_renewal(self, osint_mod, bp, monkeypatch):
        """When a canonical GhostProfile with matching _fp is already on disk,
        run_phase must skip the write (userdata_writes=0, userdata_skipped>=1)."""
        monkeypatch.setenv("USERDATA_DIR", str(bp / "userdata"))
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)

        ud_dir = bp / "userdata"
        ud_dir.mkdir(parents=True, exist_ok=True)
        (ud_dir / "ghosts").mkdir(parents=True, exist_ok=True)

        ip_hash = osint_mod._hash_ip("192.0.2.77")
        fp = osint_mod._fingerprint({
            "country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False
        })
        # Pre-write a canonical GhostProfile with matching _fp
        pre_written = {
            "ghost_id": ip_hash,
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-01T00:00:00Z",
            "signals": {
                "type": "ip_observed", "source": "heartbeat_osint",
                "country_code": "BE", "is_vpn": False,
            },
            "trust_score": 0, "tags": ["mentioned"], "mentions": 0,
            "role": "none", "schema_version": 1,
            "country_code": "BE",
            "_fp": fp,
        }
        ghost_file = ud_dir / "ghosts" / f"{ip_hash.replace(':', '_')}.json"
        ghost_file.write_text(json.dumps(pre_written))

        (inbox / "obs1.json").write_text(json.dumps({
            "ip": "192.0.2.77", "country_code": "BE"
        }))
        r1 = osint_mod.run_phase(bp)
        assert r1["userdata_writes"] == 0, f"expected dedup skip, got {r1}"
        assert r1["userdata_skipped"] >= 1, f"expected skip counted, got {r1}"

    def test_run_phase_writes_on_country_change(self, osint_mod, bp, monkeypatch):
        monkeypatch.setenv("USERDATA_DIR", str(bp / "userdata"))
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({
            "ip": "192.0.2.55", "country_code": "BE"
        }))
        osint_mod.run_phase(bp)

        (inbox / "obs2.json").write_text(json.dumps({
            "ip": "192.0.2.55", "country_code": "FR"
        }))
        r2 = osint_mod.run_phase(bp)
        assert r2["userdata_writes"] >= 1

    def test_run_phase_rate_cap_enqueues_overflow(self, osint_mod, bp, monkeypatch):
        """When USERDATA_MAX_WRITES_PER_CYCLE=2, the 3rd write is enqueued."""
        monkeypatch.setenv("USERDATA_DIR", str(bp / "userdata"))
        monkeypatch.setenv("USERDATA_MAX_WRITES_PER_CYCLE", "2")
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        # Three distinct IPs — three _signal events, but only 2 writes allowed
        for i, ip in enumerate(("10.0.0.1", "10.0.0.2", "10.0.0.3")):
            (inbox / f"obs{i}.json").write_text(json.dumps({
                "ip": ip, "country_code": "BE"
            }))
        r = osint_mod.run_phase(bp)
        assert r["userdata_writes"] == 2
        assert r["userdata_skipped"] == 1
        assert r["userdata_pending_drained"] == 0
        # Pending file must exist for the 3rd IP
        pending_dir = bp / "heartbeat" / osint_mod.USERDATA_PENDING_DIR
        pending_files = list(pending_dir.glob("userdata_pending_*.json")) if pending_dir.exists() else []
        assert len(pending_files) == 1, f"expected 1 pending file, got {len(pending_files)}"

    def test_run_phase_drains_pending_next_cycle(self, osint_mod, bp, monkeypatch):
        """A pending write from the previous cycle is drained and replayed."""
        monkeypatch.setenv("USERDATA_DIR", str(bp / "userdata"))
        monkeypatch.setenv("USERDATA_MAX_WRITES_PER_CYCLE", "10")
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        # Pre-create a pending file
        pending_dir = bp / "heartbeat" / osint_mod.USERDATA_PENDING_DIR
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_file = pending_dir / "userdata_pending_ip-hash_pending1_2026-08-30T00-00-00-000000.json"
        pending_file.write_text(json.dumps({
            "ip_hash": "ip-hash:pending1",
            "payload": {
                "type": "ip_observed", "source": "heartbeat_osint",
                "country_code": "BE", "is_vpn": False,
                "first_seen": "2026-01-01T00:00:00Z",
                "last_seen": "2026-01-01T00:00:00Z",
                "_fp": "pending_fp",
            },
        }))
        # No new observations in this cycle
        r = osint_mod.run_phase(bp)
        assert r["userdata_pending_drained"] == 1
        assert not pending_file.exists(), "pending file should be consumed"
        ghosts = osint_mod._ghosts()
        gp = ghosts.get("ip-hash:pending1")
        assert gp is not None
        assert gp.country_code == "BE"

