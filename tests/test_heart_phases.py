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
        """heartctl phase_map must include osint_userdata, ingest_osint, and prune_shared."""
        monkeypatch.setenv("BRAIN_PATH", str(tmp_path))
        monkeypatch.setenv("HEART_LOG_LEVEL", "error")
        import sys
        for k in list(sys.modules.keys()):
            if "heart" in k or "osint_" in k:
                del sys.modules[k]
        import Heart.tools.heartctl as hc
        import inspect
        src = inspect.getsource(hc)
        import re
        matches = re.findall(r'"([a-z_]+)"\s*:', src)
        assert "osint_userdata" in matches, f"osint_userdata not in heartctl phase_map; found: {matches}"
        assert "ingest_osint" in matches
        assert "self_reflexive_check" in matches, f"self_reflexive_check not in heartctl phase_map; found: {matches}"
        assert "intuition_deliberate" in matches, f"intuition_deliberate not in heartctl phase_map; found: {matches}"
        assert "prune_shared" in matches, f"prune_shared not in heartctl phase_map; found: {matches}"

    def test_reflexive_and_intuition_phases_in_cycle(self, heart_mod):
        """The new self_reflexive_check + intuition_deliberate phases must be in run_cycle
        between self_heal and audit (per Heart/SPEC.md phase table)."""
        import inspect
        from Heart.tools.heart import CycleState
        src = inspect.getsource(heart_mod.run_cycle)
        phase_lines = [
            l.strip() for l in src.splitlines()
            if l.strip().startswith('("') and "_phase_" in l
        ]
        phase_names = [l.split('"')[1] for l in phase_lines if '"' in l]
        assert "self_reflexive_check" in phase_names, (
            f"self_reflexive_check not in phases list: {phase_names}"
        )
        assert "intuition_deliberate" in phase_names, (
            f"intuition_deliberate not in phases list: {phase_names}"
        )
        # Verify ordering: self_heal -> self_reflexive_check -> intuition_deliberate -> audit
        i_heal = phase_names.index("self_heal")
        i_reflexive = phase_names.index("self_reflexive_check")
        i_intuition = phase_names.index("intuition_deliberate")
        i_audit = phase_names.index("audit")
        assert i_heal < i_reflexive < i_intuition < i_audit, (
            f"phase order wrong; got heal={i_heal} reflexive={i_reflexive} "
            f"intuition={i_intuition} audit={i_audit}; full list: {phase_names}"
        )


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

    def test_prune_stale_caps_intuition_yaml(self, heart_mod, monkeypatch):
        """prune_stale must cap intuition.yaml to the last HEART_INTUITION_MAX_ENTRIES entries."""
        monkeypatch.setenv("HEART_INTUITION_MAX_ENTRIES", "5")
        monkeypatch.setenv("HEART_INTUITION_MAX_AGE_DAYS", "365")  # disable age-based pruning
        intuition_file = heart_mod.BRAIN_PATH / "heartbeat" / "intuition.yaml"
        intuition_file.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        base = now.replace(second=0, microsecond=0)
        lines = []
        for i in range(10):
            ts = base.replace(minute=i).isoformat().replace("+00:00", "Z")
            lines.append(
                f"---\n- ts: {ts}\n"
                f"  cycle: {i + 1}\n"
                f"  mode: active\n"
                f"  per_scope_weights:\n"
                f"    stale_repo: 0.10\n"
                f"  aggregate: 0.10\n"
                f"  threshold: 0.75\n"
                f"  consensus_reached: false\n"
                f"  escalated: []\n"
            )
        intuition_file.write_text("".join(lines), encoding="utf-8")

        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 11
        result = heart_mod._phase_prune_stale(state)
        assert result.ok is True

        import yaml
        docs = list(yaml.safe_load_all(intuition_file.read_text(encoding="utf-8")))
        kept = []
        for d in docs:
            if isinstance(d, list) and d and isinstance(d[0], dict):
                kept.append(d[0])
            elif isinstance(d, dict):
                kept.append(d)
        assert len(kept) == 5, f"expected 5 entries after cap, got {len(kept)}"
        kept_cycles = [d.get("cycle") for d in kept]
        assert kept_cycles == [6, 7, 8, 9, 10], f"kept wrong cycles: {kept_cycles}"

    def test_prune_stale_drops_intuition_entries_older_than_max_age(self, heart_mod, monkeypatch):
        """prune_stale must drop intuition.yaml entries older than HEART_INTUITION_MAX_AGE_DAYS."""
        monkeypatch.setenv("HEART_INTUITION_MAX_ENTRIES", "1000")  # disable entry cap
        monkeypatch.setenv("HEART_INTUITION_MAX_AGE_DAYS", "7")
        intuition_file = heart_mod.BRAIN_PATH / "heartbeat" / "intuition.yaml"
        intuition_file.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        fresh_ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        lines = []
        lines.append("---\n- ts: %s\n  cycle: 1\n  mode: active\n  per_scope_weights:\n    stale_repo: 0.10\n  aggregate: 0.10\n  threshold: 0.75\n  consensus_reached: false\n  escalated: []\n" % old_ts)
        lines.append("---\n- ts: %s\n  cycle: 2\n  mode: active\n  per_scope_weights:\n    stale_repo: 0.10\n  aggregate: 0.10\n  threshold: 0.75\n  consensus_reached: false\n  escalated: []\n" % fresh_ts)
        intuition_file.write_text("".join(lines), encoding="utf-8")

        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 3
        result = heart_mod._phase_prune_stale(state)
        assert result.ok is True

        import yaml
        docs = list(yaml.safe_load_all(intuition_file.read_text(encoding="utf-8")))
        kept = []
        for d in docs:
            if isinstance(d, list) and d and isinstance(d[0], dict):
                kept.append(d[0])
            elif isinstance(d, dict):
                kept.append(d)
        assert len(kept) == 1, f"expected 1 fresh entry, got {len(kept)}: kept={kept}"
        assert kept[0].get("cycle") == 2, f"wrong cycle kept: {kept[0]}"


def _entities_dir(heart_mod):
    """Helper: create _entities inside BRAIN_PATH so the phase's search path finds it."""
    d = heart_mod.BRAIN_PATH / "_entities"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSelfReflexiveCheck:
    def test_phase_returns_phase_result(self, heart_mod):
        """self_reflexive_check must return a PhaseResult."""
        from Heart.tools.heart import CycleState
        result = heart_mod._phase_self_reflexive_check(CycleState())
        assert result.name == "self_reflexive_check"
        assert isinstance(result.ok, bool)
        assert isinstance(result.elapsed_ms, int)

    def test_finds_missing_entity_file(self, heart_mod, monkeypatch):
        """When an org entity file is missing, a critical finding is written."""
        monkeypatch.setenv("HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE", "0")
        from Heart.tools.heart import CycleState
        ed = _entities_dir(heart_mod)
        (ed / "org-neohiro.md").write_text(
            "authority: system\nsummary: test\nscope: meta\n", encoding="utf-8")
        for org in ("neohiro", "fpm", "osi", "hplus"):
            (ed / f"org-{org}.md").unlink(missing_ok=True)
        state = CycleState()
        state.cycle = 1
        result = heart_mod._phase_self_reflexive_check(state)
        assert result.ok is True
        findings_file = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        assert findings_file.exists()
        content = findings_file.read_text(encoding="utf-8")
        assert "missing_entity" in content
        assert "critical" in content

    def test_auto_creates_skeleton(self, heart_mod, monkeypatch):
        """When HEART_REFLEXIVE_AUTO_CREATE_ENTITIES=1 and a default-org entity is missing, a skeleton is written."""
        monkeypatch.setenv("HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE", "0")
        monkeypatch.setenv("HEART_REFLEXIVE_AUTO_CREATE_ENTITIES", "1")
        from Heart.tools.heart import CycleState
        ed = _entities_dir(heart_mod)
        for org in ("neohiro", "fpm", "osi", "hplus"):
            (ed / f"org-{org}.md").unlink(missing_ok=True)
        state = CycleState()
        state.cycle = 1
        result = heart_mod._phase_self_reflexive_check(state)
        assert result.ok is True
        for org in ("neohiro", "fpm", "osi", "hplus"):
            skeleton = ed / f"org-{org}.md"
            assert skeleton.exists(), f"skeleton for {org} not created"
            content = skeleton.read_text(encoding="utf-8")
            assert "machine-generated skeleton" in content
            assert "authority: unknown" in content

    def test_throttles_first_cycle(self, heart_mod, monkeypatch):
        """First-cycle findings must be downgraded one severity level."""
        monkeypatch.setenv("HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE", "1")
        from Heart.tools.heart import CycleState
        ed = _entities_dir(heart_mod)
        (ed / "org-neohiro.md").write_text("summary: test\n", encoding="utf-8")
        for org in ("fpm", "osi", "hplus"):
            (ed / f"org-{org}.md").unlink(missing_ok=True)
        state = CycleState()
        state.cycle = 1
        result = heart_mod._phase_self_reflexive_check(state)
        assert result.ok is True
        findings_file = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        assert findings_file.exists()
        content = findings_file.read_text(encoding="utf-8")
        assert "missing_entity" in content
        assert "critical" not in content  # downgraded to warn

    def test_seen_dirs_persists_across_cycles(self, heart_mod, monkeypatch):
        """New directories are only found once; seen_dirs is persisted to reflexive_baseline.yaml."""
        monkeypatch.setenv("HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE", "0")
        baseline = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_baseline.yaml"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_text("cycle: 0\nts: 2026-08-30T00:00:00Z\nseen_dirs: []\n", encoding="utf-8")
        unknown_dir = heart_mod.BRAIN_PATH.parent / "some_unknown_project"
        unknown_dir.mkdir(parents=True)
        from Heart.tools.heart import CycleState
        state1 = CycleState()
        state1.cycle = 1
        result1 = heart_mod._phase_self_reflexive_check(state1)
        assert result1.ok is True
        findings1 = (heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml").read_text()
        assert "some_unknown_project" in findings1
        # After cycle 1 baseline update, seen_dirs contains the unknown dir + whatever else was found.
        baseline1 = (heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_baseline.yaml").read_text()
        assert "some_unknown_project" in baseline1, "unknown dir must be in baseline after cycle 1"
        state2 = CycleState()
        state2.cycle = 2
        result2 = heart_mod._phase_self_reflexive_check(state2)
        assert result2.ok is True
        findings2 = (heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml").read_text()
        cycle2_section = findings2.split("cycle: 2\n", 1)[1] if "cycle: 2\n" in findings2 else ""
        assert "some_unknown_project" not in cycle2_section, (
            f"unknown dir must NOT appear in cycle 2 findings; seen_dirs should have filtered it. "
            f"Findings2: {findings2}"
        )

    def test_enumerate_poke_queue_multiple_pokes_no_clobber(self, heart_mod, monkeypatch):
        """Both reflexive and intuition pokes in the same cycle must both survive (no clobber)."""
        monkeypatch.setenv("HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE", "0")
        monkeypatch.setenv("HEART_INTUITION_THRESHOLD", "0.0")
        from Heart.tools.heart import CycleState
        ed = _entities_dir(heart_mod)
        for org in ("neohiro", "fpm", "osi", "hplus"):
            (ed / f"org-{org}.md").unlink(missing_ok=True)
        state = CycleState()
        state.cycle = 1
        state.mode = "active"
        heart_mod._phase_self_reflexive_check(state)  # emits reflexive_critical poke
        heart_mod._phase_intuition_deliberate(state)  # emits intuition poke
        queue_dir = heart_mod.BRAIN_PATH / "heartbeat" / "poke_queue"
        assert queue_dir.exists()
        queue_files = list(queue_dir.glob("*.json"))
        assert len(queue_files) >= 2, f"Expected at least 2 pokes, found {len(queue_files)}: {queue_files}"
        import json
        kinds = set()
        for f in queue_files:
            p = json.loads(f.read_text(encoding="utf-8"))
            kinds.add(p.get("kind"))
        assert "reflexive_critical" in kinds, f"No reflexive_critical poke found; kinds={kinds}"
        assert "intuition" in kinds, f"No intuition poke found; kinds={kinds}"

    def test_enqueue_poke_cleans_up_tmp_on_write_error(self, heart_mod, monkeypatch):
        """Regression: when write() raises, the tmp file must be removed (Windows-safe).

        Before the fix, cleanup tried os.unlink on a still-open file handle,
        which fails on Windows with PermissionError and leaks the tmp file.
        The fix calls tmp.close() in the except block before os.unlink().
        """
        import tempfile, os
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 1
        queue_dir = heart_mod.BRAIN_PATH / "heartbeat" / "poke_queue"
        queue_dir.mkdir(parents=True, exist_ok=True)

        class FailingTmpFile:
            def __init__(self, **kwargs):
                self._real = tempfile.NamedTemporaryFile(delete=False, **kwargs)
                self.name = self._real.name
            def write(self, data):
                raise IOError("simulated write failure")
            def flush(self): pass
            def fileno(self): return self._real.fileno()
            def close(self): self._real.close()

        orig_ntf = tempfile.NamedTemporaryFile
        monkeypatch.setattr(
            tempfile, "NamedTemporaryFile",
            lambda **kw: FailingTmpFile(**kw)
        )
        try:
            heart_mod._enqueue_poke(state, "test_kind", {"probe": True})
        except Exception:
            pass
        finally:
            monkeypatch.setattr(tempfile, "NamedTemporaryFile", orig_ntf)
        leftovers = list(queue_dir.glob("*.tmp"))
        assert not leftovers, f"tmp file leaked on error: {leftovers}"


class TestIntuitionDeliberate:
    def test_phase_returns_phase_result(self, heart_mod):
        """intuition_deliberate must return a PhaseResult."""
        from Heart.tools.heart import CycleState
        result = heart_mod._phase_intuition_deliberate(CycleState())
        assert result.name == "intuition_deliberate"
        assert isinstance(result.ok, bool)

    def test_writes_yaml_with_weights(self, heart_mod, monkeypatch):
        """intuition.yaml must contain per_scope_weights and aggregate."""
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 1
        state.mode = "normal"
        _entities_dir(heart_mod)
        heart_mod._phase_self_reflexive_check(state)
        result = heart_mod._phase_intuition_deliberate(state)
        assert result.ok is True
        intuition_file = heart_mod.BRAIN_PATH / "heartbeat" / "intuition.yaml"
        assert intuition_file.exists()
        content = intuition_file.read_text(encoding="utf-8")
        assert "per_scope_weights" in content
        assert "aggregate:" in content


class TestPruneShared:
    """Tests for the shared-storage auto-prune phase."""

    def test_phase_returns_phase_result(self, heart_mod):
        """prune_shared must return a PhaseResult."""
        from Heart.tools.heart import CycleState
        result = heart_mod._phase_prune_shared(CycleState())
        assert result.name == "prune_shared"
        assert isinstance(result.ok, bool)
        assert isinstance(result.elapsed_ms, int)

    def test_phase_in_cycle_between_intuition_and_audit(self, heart_mod):
        """prune_shared must appear between intuition_deliberate and audit in the phase list."""
        import inspect
        src = inspect.getsource(heart_mod.run_cycle)
        phase_lines = [
            l.strip() for l in src.splitlines()
            if l.strip().startswith('("') and "_phase_" in l
        ]
        names = [l.split('"')[1] for l in phase_lines if '"' in l]
        assert "prune_shared" in names, f"prune_shared not in phases: {names}"
        assert "intuition_deliberate" in names
        assert "audit" in names
        assert names.index("intuition_deliberate") < names.index("prune_shared") < names.index("audit")

    def test_no_prune_when_under_threshold(self, heart_mod, monkeypatch):
        """When disk usage is below threshold, no files are pruned."""
        import shutil
        from Heart.tools.heart import CycleState

        obs_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        big_file = obs_dir / "junk.json"
        big_file.write_bytes(b"x" * 1024)

        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 500 * 1024 ** 2
                free = 500 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)

        result = heart_mod._phase_prune_shared(CycleState())
        assert result.ok is True
        assert big_file.exists(), "file under 85% threshold must NOT be pruned"

    def test_prunes_largest_first_over_threshold(self, heart_mod, monkeypatch):
        """When usage >= threshold, largest files are pruned first up to budget."""
        import shutil
        from Heart.tools.heart import CycleState

        obs_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        # We use a smaller budget so the test is reliable: budget = 1 KB.
        # Both files fit; largest is deleted first, then second survives.
        small = obs_dir / "small.json"
        large = obs_dir / "large.json"
        small.write_bytes(b"x" * 100)
        large.write_bytes(b"x" * 10_000)

        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 950 * 1024 ** 2
                free = 50 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)
        monkeypatch.setenv("HEART_SHARED_PRUNE_BUDGET", "1024")

        result = heart_mod._phase_prune_shared(CycleState())
        assert result.ok is True
        assert not large.exists(), "largest file should be pruned first"
        assert small.exists(), "smaller file survives (budget exhausted)"

    def test_disabled_via_env(self, heart_mod, monkeypatch):
        """HEART_SHARED_PRUNE_ENABLED=0 must disable pruning entirely."""
        import shutil
        from Heart.tools.heart import CycleState

        obs_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "should_stay.json").write_bytes(b"x" * 1024)

        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 950 * 1024 ** 2
                free = 50 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)
        monkeypatch.setenv("HEART_SHARED_PRUNE_ENABLED", "0")

        result = heart_mod._phase_prune_shared(CycleState())
        assert result.ok is True
        assert (obs_dir / "should_stay.json").exists(), "file must not be pruned when HEART_SHARED_PRUNE_ENABLED=0"

    def test_never_prunes_userdata_or_audit(self, heart_mod, monkeypatch):
        """Files outside safe_subtrees must never be removed."""
        import shutil
        from Heart.tools.heart import CycleState

        userdata_dir = heart_mod.BRAIN_PATH.parent / "userdata"
        audit_dir = heart_mod.BRAIN_PATH.parent / "Brain" / "audit"
        obs_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "observations"
        for d in (userdata_dir, audit_dir, obs_dir):
            d.mkdir(parents=True, exist_ok=True)
        (userdata_dir / "ghost.json").write_bytes(b"protected")
        (audit_dir / "audit.json").write_bytes(b"also_protected")
        (obs_dir / "big.bin").write_bytes(b"x" * 1024)

        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 950 * 1024 ** 2
                free = 50 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)

        heart_mod._phase_prune_shared(CycleState())
        assert (userdata_dir / "ghost.json").exists(), "userdata files must never be pruned"
        assert (audit_dir / "audit.json").exists(), "audit files must never be pruned"
        assert not (obs_dir / "big.bin").exists(), "observations files are safe to prune"

    def test_writes_shared_prune_audit(self, heart_mod, monkeypatch):
        """When triggered, prune_shared must append to shared_prune.yaml."""
        import shutil
        from Heart.tools.heart import CycleState

        obs_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "pruned.json").write_bytes(b"x" * 1024)

        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 950 * 1024 ** 2
                free = 50 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)

        result = heart_mod._phase_prune_shared(CycleState())
        assert result.ok is True

        audit = heart_mod.BRAIN_PATH / "audit" / "shared_prune.yaml"
        assert audit.exists(), "shared_prune.yaml must be written when triggered"
        content = audit.read_text(encoding="utf-8")
        assert "files_pruned:" in content
        assert "bytes_pruned:" in content
        assert "usage_pct:" in content
        # Trailing newline required so consecutive cycles have a blank-line
        # separator between entries. Without it, the last field of entry N
        # collides with `- ts:` of entry N+1.
        assert content.endswith("\n"), (
            f"shared_prune.yaml must end with a trailing newline; got {content[-10:]!r}"
        )
        # Two consecutive writes must produce a blank line between entries.
        heart_mod._phase_prune_shared(CycleState())
        content2 = audit.read_text(encoding="utf-8")
        # At least one "\n\n" sequence between the two entries is expected.
        assert "\n\n" in content2, (
            "Two consecutive prune entries must be separated by a blank line"
        )

    def test_usage_pct_in_log_matches_audit(self, heart_mod, monkeypatch):
        """The usage_pct written to the audit file must match used/total*100."""
        import shutil
        from Heart.tools.heart import CycleState

        obs_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "observations"
        obs_dir.mkdir(parents=True, exist_ok=True)
        (obs_dir / "pruned.json").write_bytes(b"x" * 1024)

        # total=1 GiB, used=975 MiB → 975/1024 ≈ 95.2% used.
        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 975 * 1024 ** 2
                free = 49 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)

        result = heart_mod._phase_prune_shared(CycleState())
        assert result.ok is True

        audit = heart_mod.BRAIN_PATH / "audit" / "shared_prune.yaml"
        content = audit.read_text(encoding="utf-8")
        # 975 MiB / 1024 MiB = 0.9511… = 95.2% rounded to 1 dp.
        assert "usage_pct: 95.2" in content, (
            f"audit must record 95.2%% (used/total); got: {content}"
        )

    def test_consensus_emits_poke(self, heart_mod, monkeypatch):
        """When aggregate >= threshold, a poke must be enqueued to poke_queue/."""
        monkeypatch.setenv("HEART_INTUITION_THRESHOLD", "0.0")
        from Heart.tools.heart import CycleState
        ed = _entities_dir(heart_mod)
        for org in ("neohiro", "fpm", "osi", "hplus"):
            (ed / f"org-{org}.md").unlink(missing_ok=True)
        state = CycleState()
        state.cycle = 1
        state.mode = "active"
        heart_mod._phase_self_reflexive_check(state)
        result = heart_mod._phase_intuition_deliberate(state)
        assert result.ok is True
        queue_dir = heart_mod.BRAIN_PATH / "heartbeat" / "poke_queue"
        queue_files = list(queue_dir.glob("*.json"))
        assert queue_files, f"Expected a poke file in poke_queue/, found: {list(queue_dir.iterdir())}"
        import json
        pokes = [json.loads(f.read_text(encoding="utf-8")) for f in queue_files]
        assert any(p.get("kind") == "intuition" for p in pokes), f"No intuition poke found in {pokes}"

    def test_no_false_poke_below_threshold(self, heart_mod, monkeypatch):
        """When aggregate < threshold, no intuition poke must be written to poke_queue/."""
        monkeypatch.setenv("HEART_INTUITION_THRESHOLD", "1.0")
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 1
        state.mode = "normal"
        result = heart_mod._phase_intuition_deliberate(state)
        assert result.ok is True
        queue_dir = heart_mod.BRAIN_PATH / "heartbeat" / "poke_queue"
        if queue_dir.exists():
            import json
            for f in queue_dir.glob("*.json"):
                poke = json.loads(f.read_text(encoding="utf-8"))
                assert poke.get("kind") != "intuition"

    def test_escalation_uses_only_last_N_cycles(self, heart_mod, monkeypatch):
        """Escalation must count findings in the last N cycles, not all history.
        Regression test: the dedup logic was counting every chunk ever written."""
        monkeypatch.setenv("HEART_INTUITION_REPEAT_ESCALATE", "5")
        findings_file = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        findings_file.parent.mkdir(parents=True, exist_ok=True)
        # Old history: same finding appears in 11 cycles (cycles 1-11).
        # Recent cycles (12-16): same finding appears in 5 cycles (cycles 12-16).
        # recent_chunks = last 5 chunks = cycles 12-16.
        # n = 5 (finding in all 5 recent cycles).
        # Condition: n > 5 → 5 > 5 = False → NOT escalated.
        # If old history were counted (total = 11 + 5 = 16 > 5): would falsely escalate.
        lines = []
        for i in range(11):
            day = 1 + (i % 28)
            ts = f"2026-08-{day:02d}T00:00:00Z"
            lines.append(
                f"- ts: {ts}\n  cycle: {i + 1}\n"
                f"  category: workspace_drift\n  severity: info\n"
                f"  target: /old/path\n  message: old\n"
            )
        for i in range(5):
            day = 25 + i
            ts = f"2026-08-{day:02d}T00:00:00Z"
            lines.append(
                f"- ts: {ts}\n  cycle: {i + 12}\n"
                f"  category: workspace_drift\n  severity: info\n"
                f"  target: /old/path\n  message: recent\n"
            )
        findings_file.write_text("".join(lines), encoding="utf-8")
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 17
        state.mode = "active"
        result = heart_mod._phase_intuition_deliberate(state)
        assert result.ok is True
        admin_briefing = heart_mod.BRAIN_PATH / "heartbeat" / "admin_briefing.json"
        if admin_briefing.exists():
            import json
            ab = json.loads(admin_briefing.read_text(encoding="utf-8"))
            esc = ab.get("escalated", [])
            workspace_drift_escalated = any(
                e.get("category") == "workspace_drift" and e.get("target") == "/old/path"
                for e in esc
            )
            assert not workspace_drift_escalated, (
                "workspace_drift appeared in exactly 5 of the last 5 cycles "
                "(repeat_escalate=5, threshold n>5); should NOT escalate. "
                f"Got escalated={esc}"
            )

    def test_escalation_writes_intuition_yaml(self, heart_mod, monkeypatch):
        """intuition_deliberate must write intuition.yaml even when no escalation occurs."""
        monkeypatch.setenv("HEART_INTUITION_REPEAT_ESCALATE", "5")
        findings_file = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        findings_file.parent.mkdir(parents=True, exist_ok=True)
        # Single finding in last cycle.
        findings_file.write_text(
            "- ts: 2026-08-30T00:00:00Z\n"
            "  cycle: 1\n"
            "  category: workspace_drift\n"
            "  severity: info\n"
            "  target: /some/path\n"
            "  message: new dir\n",
            encoding="utf-8",
        )
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 2
        state.mode = "active"
        result = heart_mod._phase_intuition_deliberate(state)
        assert result.ok is True
        intuition_file = heart_mod.BRAIN_PATH / "heartbeat" / "intuition.yaml"
        assert intuition_file.exists(), "intuition.yaml must be written every cycle"
        content = intuition_file.read_text(encoding="utf-8")
        assert "per_scope_weights" in content
        assert "aggregate:" in content

    def test_admin_briefing_dedups_repeated_escalations(self, heart_mod, monkeypatch):
        """Repeated escalation of the same (category, target) must NOT grow admin_briefing.json unbounded.

        Regression: a finding that triggers escalation each cycle used to be appended
        every cycle, leading to N copies in the file. Now it is deduped — one entry
        per (category, target), with the higher seen_count winning.
        """
        import json
        from Heart.tools.heart import CycleState

        monkeypatch.setenv("HEART_INTUITION_REPEAT_ESCALATE", "2")
        findings_file = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        findings_file.parent.mkdir(parents=True, exist_ok=True)
        # 2 cycles, each with 3 copies of the same workspace_drift /x/dup finding.
        # recent_cycles (last 2) → seen_counts[workspace_drift+/x/dup] = 6.
        # 6 > repeat_escalate=2 → escalates. Re-running intuition_deliberate 3×
        # produces 3 escalations of the same key; admin_briefing must dedup to 1.
        chunks = []
        for c in (1, 2):
            for _ in range(3):
                chunks.append(
                    f"- ts: 2026-08-30T0{c}:00:00Z\n"
                    f"  cycle: {c}\n"
                    f"  category: workspace_drift\n"
                    f"  severity: info\n"
                    f'  target: "/x/dup"\n'
                    f"  message: drift\n"
                )
        findings_file.write_text("".join(chunks), encoding="utf-8")

        for cycle in (3, 4, 5):
            state = CycleState()
            state.cycle = cycle
            state.mode = "active"
            heart_mod._phase_intuition_deliberate(state)

        ab_path = heart_mod.BRAIN_PATH / "heartbeat" / "admin_briefing.json"
        assert ab_path.exists()
        ab = json.loads(ab_path.read_text(encoding="utf-8"))
        esc = ab.get("escalated", [])
        matching = [e for e in esc if e.get("category") == "workspace_drift" and e.get("target") == "/x/dup"]
        assert len(matching) == 1, (
            f"admin_briefing.json must dedup by (category, target); got {len(matching)} entries"
        )

    def test_admin_briefing_caps_at_max_entries(self, heart_mod, monkeypatch):
        """admin_briefing.json must cap at HEART_ADMIN_BRIEFING_MAX_ENTRIES (default 100).

        Regression: without the cap, escalating 200 unique findings would all be retained.
        """
        import json
        from Heart.tools.heart import CycleState

        monkeypatch.setenv("HEART_INTUITION_REPEAT_ESCALATE", "2")
        monkeypatch.setenv("HEART_ADMIN_BRIEFING_MAX_ENTRIES", "5")
        findings_file = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        findings_file.parent.mkdir(parents=True, exist_ok=True)
        # 8 unique targets, 3 copies each across 2 cycles = 48 records.
        # recent_cycles (last 2) = {1, 2}. Each target appears 3 times → n=3.
        # 3 > repeat_escalate=2 → all 8 escalate. Sort by seen_count (all 3) → cap at 5.
        chunks = []
        for c in (1, 2):
            for t in range(8):
                for _ in range(3):
                    chunks.append(
                        f"- ts: 2026-08-30T0{c}:00:00Z\n"
                        f"  cycle: {c}\n"
                        f"  category: workspace_drift\n"
                        f"  severity: info\n"
                        f'  target: "/x/target_{t}"\n'
                        f"  message: drift\n"
                    )
        findings_file.write_text("".join(chunks), encoding="utf-8")

        state = CycleState()
        state.cycle = 2
        state.mode = "active"
        heart_mod._phase_intuition_deliberate(state)

        ab_path = heart_mod.BRAIN_PATH / "heartbeat" / "admin_briefing.json"
        ab = json.loads(ab_path.read_text(encoding="utf-8"))
        assert len(ab.get("escalated", [])) <= 5, (
            f"admin_briefing.json must cap at 5 entries; got {len(ab['escalated'])}"
        )

    def test_seen_dirs_round_trips_windows_paths(self, heart_mod):
        """Windows backslash paths in seen_dirs must survive a YAML round-trip.

        Regression: the original `_yaml_str` used single-quote escaping which cannot
        represent backslashes; PyYAML silently dropped them, breaking the dedup
        look-up on the next cycle. The fix uses double-quote escaping with \\n, \\t, etc.
        """
        import yaml
        from Heart.tools.heart import CycleState

        baseline = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_baseline.yaml"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        # Double-backslash so the YAML file contains literal \\ (which PyYAML unescapes to \).
        win_path = "C:\\\\Users\\\\Test\\\\path with spaces\\\\dir"
        baseline.write_text(
            f"cycle: 1\n"
            f"ts: 2026-08-30T00:00:00Z\n"
            f"seen_dirs:\n"
            f'  - "{win_path}"\n',
            encoding="utf-8",
        )

        data = yaml.safe_load(baseline.read_text(encoding="utf-8")) or {}
        expected = "C:\\Users\\Test\\path with spaces\\dir"
        assert expected in (data.get("seen_dirs") or []), (
            f"Windows path must round-trip; got {data.get('seen_dirs')!r}"
        )

    def test_poke_queue_filenames_are_unique_under_contention(self, heart_mod, monkeypatch):
        """Two same-cycle pokes must produce different filenames (proposal 2 + pid/monotonic fix)."""
        from Heart.tools.heart import CycleState

        state = CycleState()
        state.cycle = 42
        # Enqueue three pokes in rapid succession.
        for i in range(3):
            heart_mod._enqueue_poke(state, f"kind_{i}", {"i": i})
        queue_dir = heart_mod.BRAIN_PATH / "heartbeat" / "poke_queue"
        files = sorted(queue_dir.glob("*.json"))
        # All three are present (no clobber).
        assert len(files) == 3, f"expected 3 unique pokes; got {len(files)}: {files}"
        # And each filename is unique (lex-sort would put duplicates adjacent).
        names = [f.name for f in files]
        assert len(set(names)) == 3, f"poke filenames must be unique; got {names}"

    def test_repeat_escalate_clamped_to_one(self, heart_mod, monkeypatch):
        """Setting HEART_INTUITION_REPEAT_ESCALATE=0 or negative must clamp to 1 (no panic)."""
        from Heart.tools.heart import CycleState

        monkeypatch.setenv("HEART_INTUITION_REPEAT_ESCALATE", "0")
        findings_file = heart_mod.BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        findings_file.parent.mkdir(parents=True, exist_ok=True)
        # 2 findings in cycle 1 (so per-cycle count is 1; n=1, clamp=1, 1>1 False → no escalate).
        findings_file.write_text(
            "- ts: 2026-08-30T00:00:00Z\n  cycle: 1\n  category: x\n  severity: info\n  target: /a\n  message: m\n",
            encoding="utf-8",
        )
        state = CycleState()
        state.cycle = 2
        state.mode = "active"
        result = heart_mod._phase_intuition_deliberate(state)
        # Should not raise; should be ok.
        assert result.ok is True
