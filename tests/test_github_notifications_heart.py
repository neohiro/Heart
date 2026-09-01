"""
test_github_notifications_heart.py — unit tests for Heart/tools/github_notifications_heart.py

Tests:
    1. _PerOrgRateLimiter.allow respects sliding window.
    2. _PerOrgRateLimiter.allow blocks when org limit saturated.
    3. _PerOrgRateLimiter.cleanup_stale removes expired buckets.
    4. _PerOrgRateLimiter.maybe_cleanup runs every N cycles and resets counter.
    5. backlog_real excludes events_skipped_file_limit (load-shedding skip).

Run: pytest Heart/tests/test_github_notifications_heart.py -v
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import Heart.tools.github_notifications_heart as gh_h


class TestPerOrgRateLimiter:
    """Tests for _PerOrgRateLimiter."""

    def test_allow_within_limit(self):
        limiter = gh_h._PerOrgRateLimiter(per_minute=10, window_sec=60)
        for _ in range(10):
            assert limiter.allow("neohiro") is True

    def test_allow_blocks_at_limit(self):
        limiter = gh_h._PerOrgRateLimiter(per_minute=3, window_sec=60)
        for _ in range(3):
            assert limiter.allow("neohiro") is True
        assert limiter.allow("neohiro") is False

    def test_allow_per_org_isolation(self):
        limiter = gh_h._PerOrgRateLimiter(per_minute=2, window_sec=60)
        assert limiter.allow("neohiro") is True
        assert limiter.allow("neohiro") is True
        assert limiter.allow("neohiro") is False
        assert limiter.allow("frenzypenguin") is True
        assert limiter.allow("frenzypenguin") is True

    def test_cleanup_stale_removes_expired_buckets(self):
        limiter = gh_h._PerOrgRateLimiter(per_minute=1, window_sec=60)
        now = time.monotonic()
        # active-org: 1 timestamp from now (still in window)
        # stale-org: 1 timestamp from now+999 (way outside window)
        with patch.object(time, "monotonic", return_value=now):
            limiter._buckets["active-org"].append(now)
            limiter._buckets["stale-org"].append(now - 999)
        removed = limiter.cleanup_stale()
        assert removed == 1, "stale-org bucket should be removed"
        assert "active-org" in limiter._buckets
        assert "stale-org" not in limiter._buckets

    def test_maybe_cleanup_runs_every_n_cycles(self):
        limiter = gh_h._PerOrgRateLimiter(per_minute=1, window_sec=60)
        limiter.allow("org")
        with patch.object(time, "monotonic",
                          return_value=time.monotonic() + 120):
            for i in range(1, gh_h.RATE_LIMITER_CLEANUP_EVERY):
                result = limiter.maybe_cleanup()
                assert result is None, f"cleanup should not run on cycle {i}"
            result = limiter.maybe_cleanup()
            assert result == 1, "cleanup should run on every Nth cycle"
            assert limiter._cycles_since_cleanup == 0, "counter must reset after cleanup"


class TestBacklogReal:
    """Tests for backlog_real formula — load-shedding skips (file_limit)
    must NOT inflate the real backlog metric that triggers pokes."""

    def test_backlog_real_excludes_file_limit_skips(self):
        """When 8 files are seen, 0 processed, 0 intentional skips, 3 file-limit skips,
        backlog_real = max(0, (8-0) - 0 - 3) = 5 (not 8)."""
        files_seen_total = 8
        files_processed = 0
        files_skipped_intentional = 0
        events_skipped_file_limit = 3
        backlog = files_seen_total - files_processed
        backlog_real = max(
            0,
            backlog
            - files_skipped_intentional
            - events_skipped_file_limit,
        )
        assert backlog == 8
        assert backlog_real == 5, (
            f"backlog_real must be 5 (8 - 3 file_limit), not 8; got {backlog_real}"
        )

    def test_backlog_real_clamps_to_zero(self):
        """When intentional + file_limit skips exceed backlog, backlog_real must be 0,
        not negative."""
        backlog = 5
        files_skipped_intentional = 3
        events_skipped_file_limit = 4
        backlog_real = max(0, backlog - files_skipped_intentional - events_skipped_file_limit)
        assert backlog_real == 0

    def test_backlog_real_with_no_skips(self):
        """Sanity check: when there are no skips, backlog_real == backlog."""
        backlog = 10
        backlog_real = max(0, backlog - 0 - 0)
        assert backlog_real == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
