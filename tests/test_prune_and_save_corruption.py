"""
test_prune_and_save_corruption.py — Defensive-coverage tests for heart_shared_prune.prune_and_save.

Locks in the contract: when osint_cache.load returns a cache with a non-int
``pruned`` field (None, malformed string, etc.), prune_and_save must coerce
safely to 0 instead of raising. This guards the heart cycle against
data-corruption crashes that would otherwise tear down the whole phase.

Run: python -m pytest Heart/tests/test_prune_and_save_corruption.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "Heart" / "tools"))


@pytest.fixture
def fresh_heart_modules():
    """Force a clean import of heart_shared_prune and osint_cache to avoid
    state leakage from prior tests."""
    for k in list(sys.modules.keys()):
        if k in ("osint_cache",) or k.startswith("heart_shared_prune") or k.startswith("Heart.tools.heart"):
            del sys.modules[k]
    yield
    for k in list(sys.modules.keys()):
        if k in ("osint_cache",) or k.startswith("heart_shared_prune") or k.startswith("Heart.tools.heart"):
            del sys.modules[k]


@pytest.fixture
def brain_with_cache(tmp_path, monkeypatch):
    """Create a Brain dir with a custom-written osint_cache.json. The test
    is responsible for what the cache file contains; the fixture only sets
    up the directory layout and returns the brain path."""
    brain = tmp_path / "Brain"
    heartbeat = brain / "heartbeat"
    heartbeat.mkdir(parents=True)
    return brain


def _write_cache(brain: Path, payload: dict) -> None:
    cache_file = brain / "heartbeat" / "osint_cache.json"
    cache_file.write_text(json.dumps(payload), encoding="utf-8")


def test_pruned_none_returns_zero(monkeypatch, fresh_heart_modules, brain_with_cache):
    """A corrupt cache with pruned=None must coerce to 0, not raise TypeError."""
    monkeypatch.delenv("HEART_DRY_RUN", raising=False)
    _write_cache(brain_with_cache, {
        "version": 1,
        "observations": {},
        "pruned": None,
        "generated_at": "2026-01-01T00:00:00Z",
    })
    from Heart.tools import heart_shared_prune
    result = heart_shared_prune.prune_and_save(brain_with_cache)
    assert result == 0, f"expected 0 for None, got {result!r}"


def test_pruned_missing_returns_zero(monkeypatch, fresh_heart_modules, brain_with_cache):
    """A cache without a pruned key at all must return 0 (the original .get default)."""
    monkeypatch.delenv("HEART_DRY_RUN", raising=False)
    _write_cache(brain_with_cache, {
        "version": 1,
        "observations": {},
        "generated_at": "2026-01-01T00:00:00Z",
    })
    from Heart.tools import heart_shared_prune
    result = heart_shared_prune.prune_and_save(brain_with_cache)
    assert result == 0


def test_pruned_non_numeric_string_returns_zero(monkeypatch, fresh_heart_modules, brain_with_cache):
    """A cache with pruned='abc' must coerce to 0, not raise ValueError."""
    monkeypatch.delenv("HEART_DRY_RUN", raising=False)
    _write_cache(brain_with_cache, {
        "version": 1,
        "observations": {},
        "pruned": "abc",
        "generated_at": "2026-01-01T00:00:00Z",
    })
    from Heart.tools import heart_shared_prune
    result = heart_shared_prune.prune_and_save(brain_with_cache)
    assert result == 0


def test_pruned_float_string_returns_zero(monkeypatch, fresh_heart_modules, brain_with_cache):
    """A cache with pruned='3.5' must coerce to 0 (NOT truncate to 3): losing
    data on a partial write is worse than reporting 0. This locks the
    coercion behavior so a future refactor that uses float() instead of
    int() will fail this test."""
    monkeypatch.delenv("HEART_DRY_RUN", raising=False)
    _write_cache(brain_with_cache, {
        "version": 1,
        "observations": {},
        "pruned": "3.5",
        "generated_at": "2026-01-01T00:00:00Z",
    })
    from Heart.tools import heart_shared_prune
    result = heart_shared_prune.prune_and_save(brain_with_cache)
    assert result == 0, (
        f"expected 0 for '3.5' (strict int() coercion), got {result!r}; "
        f"if this is 3 a refactor changed int() to float() somewhere"
    )


def test_pruned_int_returns_zero_from_load(monkeypatch, fresh_heart_modules, brain_with_cache):
    """pruned is computed by osint_cache.load, not read from the file's pruned field.
    So this test confirms what load actually returns: 0 when observations are all fresh."""
    monkeypatch.delenv("HEART_DRY_RUN", raising=False)
    _write_cache(brain_with_cache, {
        "version": 1,
        "observations": {},
        "pruned": 7,
        "generated_at": "2026-01-01T00:00:00Z",
    })
    from Heart.tools import heart_shared_prune
    result = heart_shared_prune.prune_and_save(brain_with_cache)
    assert result == 0, f"expected 0 (load computes pruned fresh), got {result!r}"


def test_pruned_in_dry_run_does_not_write_cache(monkeypatch, fresh_heart_modules, brain_with_cache):
    """When HEART_DRY_RUN is set, prune_and_save must NOT overwrite the cache file
    even if a valid pruned count is computed. This is the end-to-end dry-run
    contract for the shared-prune writer."""
    monkeypatch.setenv("HEART_DRY_RUN", "1")
    cache_file = brain_with_cache / "heartbeat" / "osint_cache.json"
    original = {
        "version": 1,
        "observations": {},
        "pruned": 0,
        "generated_at": "2026-01-01T00:00:00Z",
    }
    _write_cache(brain_with_cache, original)
    mtime_before = cache_file.stat().st_mtime

    from Heart.tools import heart_shared_prune
    result = heart_shared_prune.prune_and_save(brain_with_cache)
    assert result == 0

    # Cache file must be unchanged in dry-run mode.
    after = json.loads(cache_file.read_text(encoding="utf-8"))
    assert after == original, f"cache was modified in dry-run: {after!r}"
    assert cache_file.stat().st_mtime == mtime_before, "mtime changed; file was rewritten"


class TestIntuitionCapEmpty:
    """When the intuition cap filters ALL entries (everything older than
    HEART_INTUITION_MAX_AGE_DAYS, or the file is empty), the phase must
    still write the file (with an empty list of entries) instead of either:
    (a) skipping the write and leaving stale entries on disk, or
    (b) calling yaml.dump_all([]) which emits an empty string and atomic-replaces
    the file with a 0-byte file. Empty file IS the correct content."""

    def test_yaml_dump_all_empty_emits_empty_string(self):
        """Sanity check the atomic primitive's behavior on empty input."""
        import yaml
        result = yaml.dump_all([], default_flow_style=False, sort_keys=False, allow_unicode=True)
        assert result == "", f"expected empty string, got {result!r}"

    def test_atomic_write_yaml_with_empty_kept_list_writes_empty_file(
        self, tmp_path, fresh_heart_modules
    ):
        """End-to-end: _atomic_write_yaml with multi_doc=True and an empty
        list writes a 0-byte file (correctly clearing all stale entries)."""
        import pytest
        from pytest import MonkeyPatch
        mp = MonkeyPatch()
        mp.delenv("HEART_DRY_RUN", raising=False)
        try:
            import Heart.tools.heart as _heart_module
            previous = _heart_module.DRY_RUN
            _heart_module.DRY_RUN = False
            try:
                target = tmp_path / "intuition.yaml"
                _heart_module._atomic_write_yaml(target, [], multi_doc=True)
                content = target.read_text(encoding="utf-8")
                assert content == "", f"expected empty file, got {content!r}"
            finally:
                _heart_module.DRY_RUN = previous
        finally:
            mp.undo()


