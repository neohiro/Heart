"""
test_heart_phases.py — Heart phase list integrity and phase function smoke tests.

Run: python -m pytest Heart/tests/test_heart_phases.py -v

These tests verify that the phase list is consistent with the PHASE_DOC
docstring and that each phase function has the correct signature and
returns the expected fields. No GH_TOKEN or network access is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "Heart" / "tools"))


@pytest.fixture
def heart_mod(tmp_path, monkeypatch):
    """Import heart with a clean temporary workspace."""
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "brain"))
    monkeypatch.setenv("HEART_LOG_LEVEL", "error")
    monkeypatch.setenv("USERDATA_DIR", str(tmp_path / "userdata"))
    (tmp_path / "brain").mkdir(parents=True)
    (tmp_path / "userdata").mkdir(parents=True)
    import importlib
    for k in list(sys.modules.keys()):
        if "heart" in k or "osint_" in k:
            del sys.modules[k]
    import Heart.tools.heart as h
    importlib.reload(h)
    return h


class TestPhaseListIntegrity:
    def test_osint_userdata_in_cycle_phases(self, heart_mod):
        """osint_userdata must be in the phases list between ingest_osint and compute_health."""
        import inspect
        src = inspect.getsource(heart_mod.run_cycle)
        lines = src.splitlines()
        phase_lines = [
            l.strip() for l in lines
            if l.strip().startswith('("') and "_phase_" in l
        ]
        phase_names = [l.split('"')[1] for l in phase_lines if '"' in l]

        assert "osint_userdata" in phase_names, (
            f"osint_userdata not in run_cycle phases: {phase_names}"
        )
        idx_osint_userdata = phase_names.index("osint_userdata")
        idx_ingest = phase_names.index("ingest_osint")
        idx_compute = phase_names.index("compute_health")
        assert idx_ingest < idx_osint_userdata < idx_compute, (
            f"osint_userdata must be between ingest_osint and compute_health; got {phase_names}"
        )

    def test_heartctl_phase_map_has_osint_userdata(self, monkeypatch, tmp_path):
        """heartctl phase_map must include osint_userdata and ingest_osint."""
        monkeypatch.setenv("BRAIN_PATH", str(tmp_path))
        monkeypatch.setenv("HEART_LOG_LEVEL", "error")
        import sys
        for k in list(sys.modules.keys()):
            if "heart" in k or "osint_" in k:
                del sys.modules[k]
        import Heart.tools.heartctl as hc
        import inspect
        src = inspect.getsource(hc)
        # phase_map keys appear in the source as "key_name": pattern
        import re
        matches = re.findall(r'"([a-z_]+)"\s*:', src)
        assert "osint_userdata" in matches, f"osint_userdata not in heartctl phase_map; found: {matches}"
        assert "ingest_osint" in matches


class TestPhaseFunctionSignatures:
    def test_osint_userdata_phase_signature(self, heart_mod):
        """_phase_osint_userdata must accept a state param and return PhaseResult."""
        import inspect
        from Heart.tools.heart import CycleState
        sig = inspect.signature(heart_mod._phase_osint_userdata)
        assert "state" in sig.parameters
        state = CycleState()
        result = heart_mod._phase_osint_userdata(state)
        assert hasattr(result, "ok")
        assert hasattr(result, "elapsed_ms")
        assert isinstance(result.elapsed_ms, int)

    def test_osint_userdata_phase_is_idempotent(self, heart_mod, tmp_path, monkeypatch):
        """Running the phase twice must not raise or corrupt state."""
        from Heart.tools.heart import CycleState
        state = CycleState()
        result1 = heart_mod._phase_osint_userdata(state)
        assert result1.ok is True
        result2 = heart_mod._phase_osint_userdata(state)
        assert result2.ok is True
        assert result2.elapsed_ms >= 0

    def test_osint_userdata_phase_skips_import_error(self, heart_mod, monkeypatch):
        """When osint_userdata cannot be imported, phase must still return ok=True."""
        import sys
        for k in list(sys.modules.keys()):
            if "osint_userdata" in k:
                del sys.modules[k]
        monkeypatch.setitem(sys.modules, "osint_userdata", None)
        # Restore real module
        import importlib
        import Heart.tools.osint_userdata as ou
        importlib.reload(ou)
        from Heart.tools.heart import CycleState
        state = CycleState()
        result = heart_mod._phase_osint_userdata(state)
        assert result.ok is True


class TestPruneStaleAndSelfHeal:
    """Verify the implementations of prune_stale and self_heal that replaced the stubs."""

    def test_prune_stale_returns_phase_result(self, heart_mod, tmp_path):
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 1
        result = heart_mod._phase_prune_stale(state)
        assert result.name == "prune_stale"
        assert isinstance(result.ok, bool)
        assert isinstance(result.elapsed_ms, int)

    def test_prune_stale_appends_stale_yaml(self, heart_mod, tmp_path):
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 1
        heart_mod._phase_prune_stale(state)
        stale_file = heart_mod.BRAIN_PATH / "heartbeat" / "stale.yaml"
        assert stale_file.exists()
        content = stale_file.read_text(encoding="utf-8")
        assert "ts:" in content
        assert "cycle: 1" in content
        assert "pruned:" in content

    def test_prune_stale_idempotent(self, heart_mod):
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 2
        r1 = heart_mod._phase_prune_stale(state)
        r2 = heart_mod._phase_prune_stale(state)
        assert r1.ok is True
        assert r2.ok is True

    def test_self_heal_returns_phase_result(self, heart_mod):
        from Heart.tools.heart import CycleState
        result = heart_mod._phase_self_heal(CycleState())
        assert result.name == "self_heal"
        assert isinstance(result.ok, bool)

    def test_self_heal_records_action_on_high_staleness(self, heart_mod, monkeypatch):
        """When health.yaml has staleness > threshold, self_heal must append to audit."""
        import yaml
        from Heart.tools.heart import CycleState

        health_file = heart_mod.BRAIN_PATH / "heartbeat" / "health.yaml"
        health_file.parent.mkdir(parents=True, exist_ok=True)
        health_file.write_text(yaml.dump({"staleness": 0.9, "error_rate": 0.01}), encoding="utf-8")
        monkeypatch.setenv("HEART_HEAL_STALENESS_MAX", "0.5")

        result = heart_mod._phase_self_heal(CycleState())
        assert result.ok is True
        audit = heart_mod.BRAIN_PATH / "audit" / "self_heal.yaml"
        assert audit.exists()
        assert "staleness=" in audit.read_text(encoding="utf-8")

    def test_self_heal_no_action_on_healthy_state(self, heart_mod, monkeypatch):
        import yaml
        from Heart.tools.heart import CycleState

        health_file = heart_mod.BRAIN_PATH / "heartbeat" / "health.yaml"
        health_file.parent.mkdir(parents=True, exist_ok=True)
        health_file.write_text(yaml.dump({"staleness": 0.1, "error_rate": 0.0}), encoding="utf-8")

        heart_mod._phase_self_heal(CycleState())
        audit = heart_mod.BRAIN_PATH / "audit" / "self_heal.yaml"
        assert not audit.exists()
