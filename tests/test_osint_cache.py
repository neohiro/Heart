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
def shared_prune_mod():
    """Fresh import of heart_shared_prune per test to avoid state leakage."""
    for k in list(sys.modules.keys()):
        if "heart_shared_prune" in k:
            del sys.modules[k]
    from Heart.tools import heart_shared_prune
    return heart_shared_prune


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

    def test_reads_and_returns_paths(self, osint_mod, bp):
        """Files are NOT deleted by _load_incoming; they are deleted in run_phase after successful save."""
        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({
            "ip": "192.0.2.1", "country": "BE", "country_code": "BE"
        }))
        (inbox / "obs2.json").write_text(json.dumps({
            "ip": "192.0.2.2", "country": "FR", "country_code": "FR"
        }))

        raw_with_paths = osint_mod._load_incoming(bp)
        assert len(raw_with_paths) == 2
        # Files remain in inbox until run_phase saves cache successfully
        assert len(list(inbox.glob("*.json"))) == 2
        # Verify return structure: list of (raw_dict, Path) tuples
        assert all(isinstance(item, tuple) and len(item) == 2 for item in raw_with_paths)
        assert all(isinstance(item[0], dict) and isinstance(item[1], Path) for item in raw_with_paths)
        # Verify IP content
        ips = {item[0].get("ip") for item in raw_with_paths}
        assert ips == {"192.0.2.1", "192.0.2.2"}

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


# ── Stale lock detection ──────────────────────────────────────────────────────

class TestStaleLock:
    """Tests for stale lock detection in _acquire_lock.

    A lock directory older than 2x timeout_s (default 60s) is considered
    abandoned and is automatically removed so the next process can proceed.
    """

    def test_stale_lock_is_removed_and_acquired(self, osint_mod, bp, monkeypatch):
        """A lock directory older than 60s is stale and gets auto-removed."""
        import os, time
        lock_path = bp / "heartbeat" / ".osint_run_phase.lock"
        lock_path.mkdir(parents=True, exist_ok=True)
        # Set mtime to 70 seconds ago (stale threshold is 2*30s = 60s)
        old_time = time.time() - 70
        os.utime(lock_path, (old_time, old_time))

        # Should acquire immediately (stale lock removed, new one created)
        acquired = osint_mod._acquire_lock(lock_path, timeout_s=30)
        from atomic import FileLock
        assert isinstance(acquired, FileLock), f"expected FileLock, got {type(acquired).__name__}"
        assert acquired.path == lock_path
        assert lock_path.exists()  # new lock exists

    def test_fresh_lock_blocks_acquisition(self, osint_mod, bp, monkeypatch):
        """A fresh lock blocks acquisition until timeout."""
        import pytest
        import sys
        lock_path = bp / "heartbeat" / ".osint_run_phase.lock"
        lock_path.mkdir(parents=True, exist_ok=True)

        # msvcrt.locking does not share locks across separate file handles on Windows —
        # this test cannot reliably test blocking behavior on win32.
        if sys.platform == "win32":
            pytest.skip("msvcrt locking is not shared across lock-file instances on Windows")

        # Should timeout quickly with 1s timeout
        with pytest.raises(TimeoutError):
            osint_mod._acquire_lock(lock_path, timeout_s=1)

    def test_lock_path_returned_on_success(self, osint_mod, bp):
        """_acquire_lock returns a FileLock object with .path equal to the input path."""
        lock_path = bp / "heartbeat" / ".osint_run_phase.lock"
        acquired = osint_mod._acquire_lock(lock_path, timeout_s=30)
        from atomic import FileLock
        assert isinstance(acquired, FileLock), f"expected FileLock, got {type(acquired).__name__}"
        assert acquired.path == lock_path, f"FileLock.path should be {lock_path}, got {acquired.path}"


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


# ── prune_largest_first helper (unit tests) ──────────────────────────────────

class TestPruneLargestFirst:
    """Tests for the standalone prune_largest_first helper.

    This is a pure function: caller supplies the directory list and budget.
    Heart._phase_prune_shared uses it; Mouth and iot can reuse it without
    re-implementing the largest-first sort and budget bookkeeping.
    """

    def test_returns_zero_zero_on_empty_dir(self, shared_prune_mod, tmp_path):
        d = tmp_path / "obs"
        d.mkdir()
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=1_000_000)
        assert (n, b) == (0, 0)

    def test_returns_zero_zero_on_nonexistent_dir(self, shared_prune_mod, tmp_path):
        d = tmp_path / "does_not_exist"
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=1_000_000)
        assert (n, b) == (0, 0)

    def test_largest_first(self, shared_prune_mod, tmp_path):
        d = tmp_path / "obs"
        d.mkdir()
        small = d / "small.bin"
        large = d / "large.bin"
        tiny = d / "tiny.bin"
        small.write_bytes(b"x" * 1_000)
        large.write_bytes(b"x" * 100_000)
        tiny.write_bytes(b"x" * 10)
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=1_000_000)
        assert n == 3
        assert b == 101_010
        assert not small.exists()
        assert not large.exists()
        assert not tiny.exists()

    def test_stops_at_budget(self, shared_prune_mod, tmp_path):
        d = tmp_path / "obs"
        d.mkdir()
        (d / "a.bin").write_bytes(b"x" * 5_000)
        (d / "b.bin").write_bytes(b"x" * 5_000)
        (d / "c.bin").write_bytes(b"x" * 5_000)
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=7_000)
        assert n == 2
        assert b == 10_000
        leftover = list(d.iterdir())
        assert len(leftover) == 1

    def test_walks_subdirectories(self, shared_prune_mod, tmp_path):
        d = tmp_path / "obs"
        sub = d / "sub"
        d.mkdir()
        sub.mkdir(parents=True)
        (d / "top.bin").write_bytes(b"x" * 100)
        (sub / "deep.bin").write_bytes(b"x" * 200)
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=1_000_000)
        assert n == 2
        assert b == 300

    def test_aggregates_multiple_directories(self, shared_prune_mod, tmp_path):
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.bin").write_bytes(b"x" * 100)
        (d2 / "b.bin").write_bytes(b"x" * 200)
        n, b = shared_prune_mod.prune_largest_first([d1, d2], budget_bytes=1_000_000)
        assert n == 2
        assert b == 300

    def test_accepts_string_paths(self, shared_prune_mod, tmp_path):
        d = tmp_path / "obs"
        d.mkdir()
        (d / "a.bin").write_bytes(b"x" * 100)
        n, b = shared_prune_mod.prune_largest_first([str(d)], budget_bytes=1_000_000)
        assert n == 1


class TestIsDryRun:
    """Unit tests for _is_dry_run (dry-run gate)."""

    def test_false_when_no_env_and_no_sentinel(self, shared_prune_mod, monkeypatch, tmp_path):
        monkeypatch.delenv("HEART_DRY_RUN", raising=False)
        sentinel = tmp_path / "heartbeat" / ".dry_run"
        sentinel.parent.mkdir(parents=True)
        assert not shared_prune_mod._is_dry_run(tmp_path)

    def test_true_when_env_is_1(self, shared_prune_mod, monkeypatch, tmp_path):
        monkeypatch.setenv("HEART_DRY_RUN", "1")
        assert shared_prune_mod._is_dry_run(tmp_path)

    def test_true_when_env_is_any_nonempty_string(self, shared_prune_mod, monkeypatch, tmp_path):
        for val in ("yes", "true", "on", "1", "any-garbage"):
            monkeypatch.setenv("HEART_DRY_RUN", val)
            assert shared_prune_mod._is_dry_run(tmp_path), f"HEART_DRY_RUN={val!r} should be True"

    def test_false_when_env_is_0(self, shared_prune_mod, monkeypatch, tmp_path):
        monkeypatch.setenv("HEART_DRY_RUN", "0")
        assert not shared_prune_mod._is_dry_run(tmp_path)

    def test_true_when_sentinel_file_exists(self, shared_prune_mod, monkeypatch, tmp_path):
        monkeypatch.delenv("HEART_DRY_RUN", raising=False)
        sentinel = tmp_path / "heartbeat" / ".dry_run"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()
        assert shared_prune_mod._is_dry_run(tmp_path)

    def test_sentinel_overrides_env_zero(self, shared_prune_mod, monkeypatch, tmp_path):
        monkeypatch.setenv("HEART_DRY_RUN", "0")
        sentinel = tmp_path / "heartbeat" / ".dry_run"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()
        assert shared_prune_mod._is_dry_run(tmp_path), "sentinel must override HEART_DRY_RUN=0"

    def test_true_when_heart_module_dry_run_flag_is_set(self, shared_prune_mod, monkeypatch, tmp_path):
        """When heartctl.cmd_phase sets heart.DRY_RUN=True and restores it to
        False after, the HEART_DRY_RUN env var may still be '1' but should NOT
        count as dry-run once the flag is cleared. Conversely, the module flag
        itself must be authoritative: if it's True we are in dry-run regardless
        of env."""
        monkeypatch.delenv("HEART_DRY_RUN", raising=False)
        import Heart.tools.heart as _heart_module
        previous = _heart_module.DRY_RUN
        try:
            _heart_module.DRY_RUN = True
            assert shared_prune_mod._is_dry_run(tmp_path)
        finally:
            _heart_module.DRY_RUN = previous

    def test_false_for_nonexistent_brain_path(self, shared_prune_mod, monkeypatch, tmp_path):
        monkeypatch.delenv("HEART_DRY_RUN", raising=False)
        absent = tmp_path / "does_not_exist"
        assert not shared_prune_mod._is_dry_run(absent)


class TestCollectFiles:
    """Unit tests for _collect_files (recursive file collection)."""

    def test_empty_dir_returns_empty_list(self, shared_prune_mod, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert shared_prune_mod._collect_files(d) == []

    def test_nonexistent_dir_returns_empty_list(self, shared_prune_mod, tmp_path):
        d = tmp_path / "no_such_dir"
        assert shared_prune_mod._collect_files(d) == []

    def test_single_file(self, shared_prune_mod, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        f = d / "file.txt"
        f.write_bytes(b"hello world")
        result = shared_prune_mod._collect_files(d)
        assert len(result) == 1
        size, path = result[0]
        assert size == 11
        assert path == f

    def test_nested_subdirs_collected(self, shared_prune_mod, tmp_path):
        d = tmp_path / "root"
        d.mkdir()
        (d / "top.txt").write_bytes(b"top")
        sub1 = d / "sub1"
        sub1.mkdir()
        (sub1 / "mid.txt").write_bytes(b"mid")
        sub2 = sub1 / "sub2"
        sub2.mkdir()
        (sub2 / "deep.txt").write_bytes(b"deep")
        result = shared_prune_mod._collect_files(d)
        sizes = {p.name for _, p in result}
        assert sizes == {"top.txt", "mid.txt", "deep.txt"}

    def test_symlink_dir_with_external_target_is_excluded(self, shared_prune_mod, tmp_path):
        """A symlink-to-directory whose target is OUTSIDE the safe root must not be recursed.

        The realpath containment check (is_relative_to on resolved paths) drops
        the symlink entry before any recursion. This is the realistic attack
        vector the check is designed to prevent: a writable signals_incoming dir
        containing a symlink pointing elsewhere must not be followed.
        """
        d = tmp_path / "root"
        d.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (external / "outside.txt").write_bytes(b"secret")
        (d / "inside.txt").write_bytes(b"safe")
        try:
            (d / "link_to_external").symlink_to(external)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available")
        result = shared_prune_mod._collect_files(d)
        names = {p.name for _, p in result}
        assert "inside.txt" in names, "real file inside root must be collected"
        assert "outside.txt" not in names, (
            "symlink-to-dir whose target is outside root must not be recursed; "
            f"collected: {names}"
        )

    def test_symlink_outside_safe_root_not_followed(self, shared_prune_mod, tmp_path):
        """A symlink-to-file pointing outside the safe root must NOT be collected.
        _is_path_under checks the resolved path against the declared root, so
        a symlink that crosses the boundary is excluded before any stat()/unlink()."""
        d = tmp_path / "safe"
        d.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "protected.txt").write_bytes(b"must not collect")
        try:
            (d / "escape_link.txt").symlink_to(outside / "protected.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available")
        result = shared_prune_mod._collect_files(d)
        names = {p.name for _, p in result}
        assert "escape_link.txt" not in names, "symlink-to-file outside root must be excluded"
        assert "protected.txt" not in [p.name for _, p in result]

    def test_broken_symlink_to_file_ignored(self, shared_prune_mod, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        try:
            (d / "broken.lnk").symlink_to(tmp_path / "does_not_exist")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available")
        result = shared_prune_mod._collect_files(d)
        assert result == [], "broken symlink-to-nonexistent is ignored"


# ── Cross-process lock (subprocess on Windows spawn) ──────────────────────

class TestRunPhaseCrossProcess:
    def test_run_phase_blocks_concurrent_runner_until_lock_released(self, osint_mod, bp):
        """Pass-2/2 proposal 2: Two concurrent run_phase processes must not
        race. The lock ensures the second process waits for the first to
        complete before draining its cycle — preventing duplicate writes to
        userdata and ensuring each process observes a consistent cache state.

        Uses subprocess (not multiprocessing) so the worker is a top-level
        script string — picklable on Windows spawn. Proves:
        1. Both processes succeed (neither hits the 30s lock timeout)
        2. Exactly 1 new_ip total across both runs (no double-count)
        """
        import json
        import subprocess
        import threading
        import time as _t
        errors: list[str] = []

        inbox = bp / "heartbeat" / osint_mod.SIGNALS_INCOMING_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "obs1.json").write_text(json.dumps({
            "ip": "192.0.2.200", "country_code": "BE"
        }))

        bp_str = str(bp.resolve())
        userdata_dir = str((bp / "userdata").resolve())

        # Create empty marker file (truncate to clear any prior content)
        Path(bp_str + ".worker_result").write_text("", encoding="utf-8")

        def launch_and_wait(delay: float) -> None:
            # Build the worker as a self-contained script. Paths are passed
            # via env vars (no shell escaping concerns) and the result is
            # written to a known file. JSON-quoted in source but embedded
            # as Python repr (so the runtime gets proper string escaping).
            bp_esc = bp_str.replace("\\", "\\\\").replace("'", "\\'")
            userdata_esc = userdata_dir.replace("\\", "\\\\").replace("'", "\\'")
            result_esc = (bp_str + ".worker_result").replace("\\", "\\\\").replace("'", "\\'")
            code = (
                "import sys, json, time, os\n"
                f"sys.path.insert(0, {json.dumps(str(bp.parent.parent.parent))!r})\n"
                "for k in list(sys.modules.keys()):\n"
                "    if 'osint_cache' in k or k.startswith('userdata.'):\n"
                "        del sys.modules[k]\n"
                "from Heart.tools import osint_cache as m\n"
                f"os.environ['USERDATA_DIR'] = r'{userdata_esc}'\n"
                f"time.sleep({delay!r})\n"
                f"result = m.run_phase(r'{bp_esc}')\n"
                f"with open(r'{result_esc}', 'a', encoding='utf-8') as f:\n"
                "    f.write(json.dumps({'ok': result['ok'], 'new_ips': result.get('new_ips', -1)}) + '\\n')\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=90,
            )
            if proc.returncode != 0:
                errors.append(proc.stderr)

        t1 = threading.Thread(target=launch_and_wait, args=(0.0,))
        t2 = threading.Thread(target=launch_and_wait, args=(0.05,))
        result_file = Path(bp_str + ".worker_result")
        try:
            t1.start()
            t2.start()
            t1.join(timeout=60)
            t2.join(timeout=60)

            for _ in range(10):
                try:
                    lines = result_file.read_text(encoding="utf-8").strip().splitlines()
                    if lines:
                        results = [json.loads(line) for line in lines if line.strip()]
                        if len(results) >= 2:
                            break
                except (FileNotFoundError, ValueError):
                    pass
                _t.sleep(0.5)

            assert not t1.is_alive(), "worker-1 did not complete within 60s"
            assert not t2.is_alive(), "worker-2 did not complete within 60s"
            assert not errors, f"subprocess errors: {errors}"
            assert len(results) >= 2, f"expected 2 result lines, got {len(results)}: {results}"
            assert all(r["ok"] for r in results), f"some run_phase failed: {results}"
            new_ips_total = sum(r["new_ips"] for r in results)
            assert new_ips_total == 1, (
                f"expected exactly 1 new_ip total (no double-count), got {new_ips_total}: {results}"
            )
        finally:
            try:
                result_file.unlink()
            except OSError:
                pass


class TestRunPhaseLockContract:
    """Regression tests for the _release_lock(lock) contract.

    Bug (pre-session): osint_cache.run_phase finally-block called
    _release_lock(lock_path) (a WindowsPath) instead of _release_lock(lock)
    (the FileLock object returned by _acquire_lock). This raised
    AttributeError because Path has no .release(). The fix: pass the FileLock
    object so .release() is called and _locked is set to False.
    """

    def test_release_lock_accepts_filelock_and_sets_locked_false(self, osint_mod, bp):
        """Calling _release_lock with a FileLock must call .release() and set
        _locked=False. If this fails, the finally-block is passing the wrong
        variable (a Path instead of a FileLock object)."""
        lock_path = bp / "heartbeat" / ".contract_test.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock = osint_mod._acquire_lock(lock_path, timeout_s=5)
            assert lock._locked is True, "precondition: lock should be acquired"
            osint_mod._release_lock(lock)
            assert lock._locked is False, (
                "lock._locked must be False after _release_lock; "
                "if this fails, _release_lock received a Path instead of a FileLock"
            )
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass

    def test_release_lock_rejects_path_with_attributerror(self, osint_mod, tmp_path):
        """Passing a Path to _release_lock raises AttributeError — not silently
        succeeding. This documents the bug: a Path has no .release() method."""
        import pytest
        with pytest.raises(AttributeError, match="'WindowsPath'"):
            osint_mod._release_lock(tmp_path / "fake.lock")
