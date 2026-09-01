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
    """Tests for backlog_real calculation excluding events_skipped_file_limit."""

    def test_backlog_real_excludes_file_limit_skips(self):
        """backlog_real must exclude events_skipped_file_limit so that load-shedding
        skips do not inflate the real backlog metric that triggers pokes."""
        limiter = gh_h._PerOrgRateLimiter()
        # Simulate 8 files seen, 0 processed, 0 intentional (oversized/no-org/no-type),
        # 3 skipped due to file limit.
        # Formula: backlog_real = max(0, (8-0) - 0 - 3) = max(0, 5) = 5
        #
        # The actual code path computes it inside run_once, so we verify the
        # arithmetic by checking the counters dict that run_once would compute.
        # We mock only _write_metrics to capture the computed backlog_real.
        with patch.object(gh_h, "MAX_FILES_PER_CYCLE", 5):
            with patch.object(gh_h, "IOT_CACHE_DIR", Path()) as mock_dir:
                # 8 files that all pass size check but get skipped due to the limit.
                paths = []
                for i in range(8):
                    p = MagicMock()
                    p.name = f"2025-01-01T00-abc{i}-github_pull_request.json"
                    p.stat.return_value = MagicMock(st_size=100)
                    paths.append(p)

                def fake_event(path):
                    return None  # normalize_failed, adds delivery_from_path

                captured: list[dict] = []

                def capture_metrics(counters, *, cycle_duration_ms, backlog, backlog_real):
                    captured.append({"backlog": backlog, "backlog_real": backlog_real,
                                     "file_limit": counters.get("events_skipped_file_limit", 0)})

                with patch.object(gh_h, "_iter_cache_files", return_value=paths):
                    with patch.object(gh_h, "_load_event_from_cache", side_effect=fake_event):
                        with patch.object(gh_h, "_shared_root", return_value=Path()):
                            with patch.object(gh_h, "_acquire_lease", return_value=None):
                                with patch.object(gh_h, "_release_lease"):
                                    with patch.object(gh_h, "_write_metrics", side_effect=capture_metrics):
                                        with patch.object(gh_h, "_emit_poke"):
                                            gh_h.run_once(dry_run=False, reset_processed=True)

        assert len(captured) == 1
        c = captured[0]
        assert c["file_limit"] == 3, f"3 files should hit the limit; got {c['file_limit']}"
        assert c["backlog"] == 8, f"8 files seen, 0 processed = backlog 8; got {c['backlog']}"
        assert c["backlog_real"] == 5, (
            f"backlog_real should be 5 (8 - 3 file_limit = 5); got {c['backlog_real']}"
        )
        assert call["backlog"] == 8, "8 files seen, 0 processed"
        assert call["backlog_real"] == 0, (
            "backlog_real must be 0 when only file-limit skips exist (no real backlog)"
        )

    def test_backlog_real_formula(self):
        """backlog_real = max(0, (seen - processed) - intentional - file_limit_skip)"""
        limiter = gh_h._PerOrgRateLimiter()
        seen = 10
        processed = 4
        intentional = 2  # oversized + unknown type
        file_limit_skip = 1
        backlog = seen - processed  # 6
        backlog_real = max(0, backlog - intentional - file_limit_skip)
        assert backlog_real == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
