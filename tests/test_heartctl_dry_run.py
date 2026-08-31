"""
test_heartctl_dry_run.py — Integration test for `heartctl phase --dry-run`.

Locks in the dual-gate contract: when --dry-run is passed, both the module-level
DRY_RUN flag and the HEART_DRY_RUN environment variable must be set so that
all writers (heart.py _atomic_write_yaml, heart_shared_prune._is_dry_run)
correctly skip mutation. Also verifies that a write-side phase run with
--dry-run does NOT modify the underlying files.

Run: python -m pytest Heart/tests/test_heartctl_dry_run.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
HEARTCTL = ROOT / "Heart" / "tools" / "heartctl.py"
HEART_PY = ROOT / "Heart" / "tools" / "heart.py"


def _clear_heart_modules():
    for k in list(sys.modules.keys()):
        if k == "heart" or k.startswith("heart.") or k.startswith("heart_"):
            del sys.modules[k]


@pytest.fixture
def fresh_imports():
    """Force a clean import of heart + heartctl so DRY_RUN state from other
    tests doesn't leak into this one."""
    _clear_heart_modules()
    yield
    _clear_heart_modules()


def test_cmd_phase_dry_run_sets_module_flag_and_env(monkeypatch, tmp_path, fresh_imports):
    """`cmd_phase --dry-run` must set both _heart_module.DRY_RUN=True AND
    os.environ['HEART_DRY_RUN']='1' (and restore the previous DRY_RUN value
    on the module after the call, to avoid leaking state into other phases)."""
    monkeypatch.syspath_prepend(str(ROOT / "Heart" / "tools"))
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "brain"))
    monkeypatch.setenv("HEART_DRY_RUN", "")

    import argparse
    import heart as _heart_module
    import heartctl

    previous_dry_run = _heart_module.DRY_RUN
    args = argparse.Namespace(
        phase_name="compute_health",
        dry_run=True,
    )
    rc = heartctl.cmd_phase(args)
    assert rc == 0, f"cmd_phase returned {rc}"
    assert os.environ.get("HEART_DRY_RUN") == "1", (
        "HEART_DRY_RUN env var was not set by --dry-run; heart_shared_prune._is_dry_run "
        "will treat this as live mode and may still mutate files"
    )
    assert _heart_module.DRY_RUN is True, "module-level DRY_RUN flag was not set"
    # Restore previous state so subsequent tests aren't affected.
    _heart_module.DRY_RUN = previous_dry_run


def test_cmd_phase_dry_run_does_not_write_brain_files(monkeypatch, tmp_path, fresh_imports):
    """Run a write-side phase (`write_brain`) with --dry-run and assert the
    intended output file was NOT created. This is the end-to-end guarantee
    that the dry-run contract is honoured across the heartctl → heart → atomic
    write path."""
    monkeypatch.syspath_prepend(str(ROOT / "Heart" / "tools"))
    brain = tmp_path / "brain"
    heartbeat = brain / "heartbeat"
    heartbeat.mkdir(parents=True)
    monkeypatch.setenv("BRAIN_PATH", str(brain))
    monkeypatch.setenv("HEART_DRY_RUN", "")

    # Pre-seed a minimal repo_summary.json so write_brain has something to consume.
    (brain / "heartbeat" / "repo_summary.json").write_text(
        '{"repos": [], "generated_at": "2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    target = brain / "heartbeat" / "last_run.yaml"
    assert not target.exists(), "precondition: last_run.yaml should not exist yet"

    import argparse
    import heart as _heart_module
    import heartctl

    args = argparse.Namespace(
        phase_name="write_brain",
        dry_run=True,
    )
    rc = heartctl.cmd_phase(args)
    assert rc == 0
    assert not target.exists(), (
        f"write_brain with --dry-run still wrote {target}; DRY_RUN gate is broken"
    )

    # Reset module state
    _heart_module.DRY_RUN = False
    monkeypatch.delenv("HEART_DRY_RUN", raising=False)


def test_cmd_phase_without_dry_run_does_not_set_env(monkeypatch, tmp_path, fresh_imports):
    """Sanity: when --dry-run is NOT passed, HEART_DRY_RUN must not be
    set to '1' and module DRY_RUN must remain its prior value."""
    monkeypatch.syspath_prepend(str(ROOT / "Heart" / "tools"))
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "brain"))
    monkeypatch.setenv("HEART_DRY_RUN", "")
    monkeypatch.delenv("HEART_DRY_RUN", raising=False)

    import argparse
    import heart as _heart_module
    import heartctl

    previous_dry_run = _heart_module.DRY_RUN
    args = argparse.Namespace(
        phase_name="compute_health",
        dry_run=False,
    )
    rc = heartctl.cmd_phase(args)
    assert rc == 0
    assert os.environ.get("HEART_DRY_RUN") != "1", (
        "HEART_DRY_RUN was set to '1' even though --dry-run was not passed; "
        "this is a regression in the gate logic"
    )
    assert _heart_module.DRY_RUN == previous_dry_run, (
        "module-level DRY_RUN was modified by a non-dry-run invocation"
    )
