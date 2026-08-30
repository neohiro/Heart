"""
test_osint_userdata.py — Heart osint_userdata.py tests.

Tests that require os.statvfs (POSIX-only) are skipped on Windows.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRAPLING_DIR = Path(__file__).resolve().parent.parent.parent / "Brain" / "docker" / "scrapling"
sys.path.insert(0, str(SCRAPLING_DIR))


# ── Helpers ────────────────────────────────────────────────────────────────

@pytest.fixture
def osint_mod(monkeypatch, tmp_path):
    """Fresh import with patched directories."""
    for k in list(sys.modules.keys()):
        if "osint" in k or "osint_userdata" in k:
            del sys.modules[k]
    ud_dir = tmp_path / "userdata"
    brain_dir = tmp_path / "Brain"
    heart_data = brain_dir / "data"
    ud_dir.mkdir(parents=True)
    brain_dir.mkdir(parents=True)
    heart_data.mkdir(parents=True)
    monkeypatch.setenv("USERDATA_DIR", str(ud_dir))
    monkeypatch.setenv("BRAIN_PATH", str(brain_dir))
    monkeypatch.setenv("HEART_DATA", str(heart_data))
    import Heart.tools.osint_userdata as m
    m.USERDATA_DIR = ud_dir
    m.BRAIN_PATH = brain_dir
    m.HEART_DATA = heart_data
    return m


def _mock_st(mb: int):
    """Build a fake statvfs result for `mb` megabytes free."""
    class S:
        f_bavail = (mb * 1024 * 1024) // 4096
        f_frsize = 4096
    return S()


# ── Health assessment ─────────────────────────────────────────────────

class TestAssessHeartHealth:
    @pytest.mark.skipif(sys.platform == "win32", reason="statvfs not available on Windows")
    def test_missing_last_run_is_stalled(self, osint_mod, monkeypatch):
        monkeypatch.setattr(os, "statvfs", lambda p: _mock_st(10**6))
        result = osint_mod.assess_heart_health()
        assert result["healthy"] is False
        assert "heart-stalled" in result["organ_failures"]

    @pytest.mark.skipif(sys.platform == "win32", reason="statvfs not available on Windows")
    def test_healthy_returns_bidirectional_false(self, osint_mod, monkeypatch):
        last_run = osint_mod.BRAIN_PATH / "heartbeat" / "last_run.yaml"
        last_run.parent.mkdir(parents=True)
        last_run.write_text("ts: '2026-08-29T20:00:00+00:00'\n")
        monkeypatch.setattr(os, "statvfs", lambda p: _mock_st(10**6))
        result = osint_mod.assess_heart_health()
        assert result["healthy"] is True
        assert result["bidirectional_ok"] is False

    @pytest.mark.skipif(sys.platform == "win32", reason="statvfs not available on Windows")
    def test_authorised_but_healthy_keeps_bidirectional_off(self, osint_mod, monkeypatch):
        monkeypatch.setenv("USERDATA_BIDIRECTIONAL_OK", "1")
        last_run = osint_mod.BRAIN_PATH / "heartbeat" / "last_run.yaml"
        last_run.parent.mkdir(parents=True)
        last_run.write_text("ts: '2026-08-29T20:00:00+00:00'\n")
        monkeypatch.setattr(os, "statvfs", lambda p: _mock_st(10**6))
        result = osint_mod.assess_heart_health()
        assert result["healthy"] is True
        assert result["bidirectional_ok"] is False

    @pytest.mark.skipif(sys.platform == "win32", reason="statvfs not available on Windows")
    def test_organ_failure_plus_auth_enables_bidirectional(self, osint_mod, monkeypatch):
        monkeypatch.setenv("USERDATA_BIDIRECTIONAL_OK", "1")
        monkeypatch.setattr(os, "statvfs", lambda p: _mock_st(100))
        result = osint_mod.assess_heart_health()
        assert result["healthy"] is False
        assert result["bidirectional_ok"] is True
        assert "heart-disk" in result["organ_failures"]


# ── Read summaries ───────────────────────────────────────────────────

class TestReadUserdataSummaries:
    def test_pii_stripped(self, osint_mod):
        (osint_mod.USERDATA_DIR / "strangers.json").write_text(json.dumps({
            "gh:abc": {
                "profile_id": "gh:abc",
                "role": "stranger",
                "email": "alice@example.com",
                "ip_history": ["192.0.2.1"],
                "last_seen": "2026-08-29T20:00:00+00:00",
                "session_count": 3,
            }
        }))
        result = osint_mod.read_userdata_summaries()
        p = result["strangers"][0]
        assert "email" not in p
        assert "ip_history" not in p
        assert p["role"] == "stranger"
        assert p["session_count"] == 3

    def test_godadmins_only_count_not_content(self, osint_mod):
        (osint_mod.USERDATA_DIR / "godadmins.json").write_text(json.dumps({
            "gda:alice": {"github_username": "alice", "role": "godadmin", "email": "alice@example.com"},
            "gda:bob":   {"github_username": "bob",   "role": "godadmin", "email": "bob@example.com"},
        }))
        result = osint_mod.read_userdata_summaries()
        assert result["godadmins"] == [{"profile_id": "gda:alice"}, {"profile_id": "gda:bob"}]
        for g in result["godadmins"]:
            assert "email" not in g
            assert "github_username" not in g

    def test_missing_files_no_crash(self, osint_mod):
        result = osint_mod.read_userdata_summaries()
        assert "strangers" in result
        assert "users" in result

    def test_corrupt_json_ignored(self, osint_mod):
        (osint_mod.USERDATA_DIR / "users.json").write_text("{ invalid")
        result = osint_mod.read_userdata_summaries()
        assert result["users"] == []


# ── Role identification ───────────────────────────────────────────────

class TestIdentifyVisitorRole:
    def test_canonical_mapping(self, osint_mod):
        cases = [
            ("stranger", "stranger"),
            ("user", "user"),
            ("admin", "admin"),
            ("godadmin", "godadmin"),
            ("unknown", "ghost"),
            ("", "ghost"),
        ]
        for role, expected in cases:
            r = osint_mod.identify_visitor_role("id:1", role, "2026-08-29T20:00:00+00:00")
            assert r["role"] == expected, f"{role!r} → expected {expected}"


# ── Resurrection detection ────────────────────────────────────────────

class TestResurrectionCandidates:
    def test_no_pii_in_output(self, osint_mod):
        heart_ghosts = {"observations": {
            "ip-hash:abc": {"geo_drift_count": 0, "first_seen": "2026-08-01T00:00:00+00:00", "last_seen": "2026-08-29T20:00:00+00:00"}
        }}
        summaries = {"users": [{"profile_id": "ip-hash:abc", "role": "user", "email": "BAD"}]}
        result = osint_mod.find_resurrection_candidates(heart_ghosts, summaries)
        assert len(result) == 1
        assert "email" not in str(result)
        assert "BAD" not in str(result)

    def test_no_match(self, osint_mod):
        result = osint_mod.find_resurrection_candidates(
            {"observations": {"ip-hash:abc": {}}},
            {"users": [{"profile_id": "ip-hash:xyz", "role": "user"}]},
        )
        assert result == []


# ── Triage flags ────────────────────────────────────────────────────

class TestWriteTriageFlags:
    def test_only_allow_fields(self, osint_mod):
        osint_mod.write_triage_flags([{
            "ghost_id": "gh:abc",
            "email": "SHOULD NOT BE WRITTEN",
            "raw_ip": "192.0.2.1",
            "ts": "2026-08-29T20:00:00+00:00",
            "drift_count": 2,
            "first_seen": "2026-08-01T00:00:00+00:00",
            "last_seen": "2026-08-29T20:00:00+00:00",
        }])
        flag = json.loads((osint_mod.USERDATA_DIR / "triage_flags" / "gh:abc.json").read_text())
        assert "email" not in flag
        assert "raw_ip" not in flag
        assert flag["ghost_id"] == "gh:abc"
        assert flag["drift_count"] == 2

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not effective on Windows")
    def test_file_mode_0600(self, osint_mod):
        osint_mod.write_triage_flags([{"ghost_id": "gh:abc", "ts": "2026-08-29T20:00:00+00:00"}])
        f = osint_mod.USERDATA_DIR / "triage_flags" / "gh:abc.json"
        mode = f.stat().st_mode & 0o777
        assert mode == 0o600


# ── Command allowlist ────────────────────────────────────────────────

class TestRunCommand:
    def test_empty_rejected(self, osint_mod):
        assert osint_mod.run_command([])["ok"] is False

    def test_unknown_binary_rejected(self, osint_mod):
        assert osint_mod.run_command(["rm", "-rf", "/"])["ok"] is False

    def test_unknown_subcommand_rejected(self, osint_mod):
        assert osint_mod.run_command(["heartctl", "eviscerate"])["ok"] is False

    def test_known_subcommand_in_allowlist(self, osint_mod):
        r = osint_mod.run_command(["heartctl", "status"])
        assert "ok" in r  # allowlisted even if binary missing


# ── run_phase ───────────────────────────────────────────────────────

class TestRunPhase:
    @pytest.mark.skipif(sys.platform == "win32", reason="statvfs not available on Windows")
    def test_phase_returns_digest(self, osint_mod, monkeypatch):
        last_run = osint_mod.BRAIN_PATH / "heartbeat" / "last_run.yaml"
        last_run.parent.mkdir(parents=True)
        last_run.write_text("ts: '2026-08-29T20:00:00+00:00'\n")
        monkeypatch.setattr(os, "statvfs", lambda p: _mock_st(10**6))
        result = osint_mod.run_phase(osint_mod.BRAIN_PATH)
        assert result["ok"] is True
        assert "heart_health" in result
        digest_path = osint_mod.BRAIN_PATH / "heartbeat" / "userdata_osint_digest.json"
        assert digest_path.exists()
