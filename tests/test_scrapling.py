"""
test_scrapling.py — Brain/docker/scrapling tests.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

# Add scrapling container source to sys.path
SCRAPLING_DIR = Path(__file__).resolve().parent.parent.parent / "Brain" / "docker" / "scrapling"
sys.path.insert(0, str(SCRAPLING_DIR))


# ── token_check ─────────────────────────────────────────────────────────

class TestTokenCheck:
    def test_valid_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BRAIN_TOKEN_SECRET", "test-secret")
        # Path that does not exist — falls back to env
        monkeypatch.setenv("BRAIN_TOKEN_SECRET_PATH", str(tmp_path / "nonexistent"))
        # Reload module to pick up env
        for k in list(sys.modules.keys()):
            if k in ("token_check",):
                del sys.modules[k]
        import token_check
        token = token_check.make_token("user:alice")
        ok, reason = token_check.verify_token("user:alice", token)
        assert ok is True
        assert reason == ""

    def test_invalid_signature(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BRAIN_TOKEN_SECRET", "test-secret")
        monkeypatch.setenv("BRAIN_TOKEN_SECRET_PATH", str(tmp_path / "nonexistent"))
        for k in list(sys.modules.keys()):
            if k in ("token_check",):
                del sys.modules[k]
        import token_check
        token = token_check.make_token("user:alice")
        parts = token.split(".")
        tampered = ".".join([parts[0], parts[1], "0" * 64])
        ok, _ = token_check.verify_token("user:alice", tampered)
        assert ok is False

    def test_expired_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BRAIN_TOKEN_SECRET", "test-secret")
        monkeypatch.setenv("BRAIN_TOKEN_SECRET_PATH", str(tmp_path / "nonexistent"))
        for k in list(sys.modules.keys()):
            if k in ("token_check",):
                del sys.modules[k]
        import hmac, hashlib
        import token_check
        ts = str(int(time.time()) - 1000)
        nonce = "abcdef1234"
        body = f"user:alice|{ts}|{nonce}"
        sig = hmac.new(b"test-secret", body.encode(), hashlib.sha256).hexdigest()
        token = f"{ts}.{nonce}.{sig}"
        ok, reason = token_check.verify_token("user:alice", token)
        assert ok is False
        assert "expired" in reason

    def test_replay_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BRAIN_TOKEN_SECRET", "test-secret")
        monkeypatch.setenv("BRAIN_TOKEN_SECRET_PATH", str(tmp_path / "nonexistent"))
        for k in list(sys.modules.keys()):
            if k in ("token_check",):
                del sys.modules[k]
        import token_check
        token = token_check.make_token("user:alice")
        ok1, _ = token_check.verify_token("user:alice", token)
        assert ok1 is True
        ok2, reason = token_check.verify_token("user:alice", token)
        assert ok2 is False
        assert "replay" in reason

    def test_malformed_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BRAIN_TOKEN_SECRET", "test-secret")
        monkeypatch.setenv("BRAIN_TOKEN_SECRET_PATH", str(tmp_path / "nonexistent"))
        for k in list(sys.modules.keys()):
            if k in ("token_check",):
                del sys.modules[k]
        import token_check
        for bad in ["", "a.b", "a.b.c.d", "abc.def.ghi"]:
            ok, _ = token_check.verify_token("user:alice", bad)
            assert ok is False


# ── ratelimit ───────────────────────────────────────────────────────────

class TestRateLimit:
    def test_ghost_always_blocked(self):
        import ratelimit
        assert ratelimit.rate_limit("user:x", "ghost") is False

    def test_stranger_always_blocked(self):
        import ratelimit
        assert ratelimit.rate_limit("user:x", "stranger") is False

    def test_user_limit_enforced(self):
        import ratelimit
        ratelimit._counter.clear()
        for i in range(10):
            assert ratelimit.rate_limit("user:alice", "user") is True
        assert ratelimit.rate_limit("user:alice", "user") is False

    def test_admin_higher_limit(self):
        import ratelimit
        ratelimit._counter.clear()
        for i in range(11):
            assert ratelimit.rate_limit("user:bob", "admin") is True

    def test_different_users_independent(self):
        import ratelimit
        ratelimit._counter.clear()
        for i in range(10):
            assert ratelimit.rate_limit("user:alice", "user") is True
        # Bob should not be affected
        assert ratelimit.rate_limit("user:bob", "user") is True


# ── audit ───────────────────────────────────────────────────────────────

class TestAudit:
    def test_audit_appends_jsonl(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
        for k in list(sys.modules.keys()):
            if k in ("audit",):
                del sys.modules[k]
        import audit
        audit.audit_request("user:alice", "user", "https://example.com", "success", "")
        audit.audit_request("user:bob", "admin", "https://example.org", "denied", "expired")
        files = list((tmp_path / "audit").glob("audit-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().splitlines()
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            assert "ts" in entry
            assert "user_id" in entry
            assert "url" in entry

    def test_audit_thread_safe(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUDIT_DIR", str(tmp_path / "audit"))
        for k in list(sys.modules.keys()):
            if k in ("audit",):
                del sys.modules[k]
        import audit

        def worker(n):
            for i in range(10):
                audit.audit_request(f"user:{n}", "user", f"https://x/{n}/{i}", "success", "")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        files = list((tmp_path / "audit").glob("audit-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().splitlines()
        assert len(lines) == 80
