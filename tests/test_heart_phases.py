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


def _reload_heart():
    """Re-import Heart.tools.heart with a clean module cache, return the fresh module.

    Used by tests that read heart's source (docstrings, run_cycle, etc.) so a
    prior test's monkeypatch of env vars doesn't leak into the new source.
    """
    import importlib
    for k in list(sys.modules.keys()):
        if "heart" in k:
            del sys.modules[k]
    import Heart.tools.heart as h
    importlib.reload(h)
    return h


def _resolve_heart_source() -> Path:
    """Return the Path to heart.py source via a fresh reload."""
    return Path(_reload_heart().__file__).resolve()


def _phase_names_from_run_cycle() -> list[str]:
    """Extract phase names from run_cycle() source via a fresh reload."""
    import inspect
    h = _reload_heart()
    src = inspect.getsource(h.run_cycle)
    lines = src.splitlines()
    phase_lines = [l.strip() for l in lines if l.strip().startswith('("') and "_phase_" in l]
    return [l.split('"')[1] for l in phase_lines if '"' in l]


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
    def test_module_docstring_phase_list_matches_run_cycle(self):
        """The 'Phases (in order):' block in heart.py's module docstring must list every
        phase in run_cycle (and only those phases), in the same order. Locks in
        docstring/code parity so a new phase added to run_cycle without updating the
        docstring (or vice versa) fails this test instead of being silently out of date.
        """
        import re
        text = _resolve_heart_source().read_text(encoding="utf-8")

        m = re.search(r"Phases \(in order\):\n(.*?)\n\s*\n", text, re.DOTALL)
        assert m, "module docstring has no 'Phases (in order):' block"
        block = m.group(1)
        doc_names = re.findall(r"^\s*([a-z_]+)\s*[\u2014\-]", block, re.MULTILINE)
        doc_names = [n for n in doc_names if n.replace("_", "").isalnum()]

        code_names = _phase_names_from_run_cycle()
        assert doc_names == code_names, (
            f"module docstring phase list must match run_cycle.\n"
            f"  doc:     {doc_names}\n"
            f"  run:     {code_names}\n"
            f"  only-doc: {set(doc_names) - set(code_names)}\n"
            f"  only-run: {set(code_names) - set(doc_names)}"
        )

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

    def test_prune_stale_does_not_mutate_cache_in_dry_run(self, heart_mod, monkeypatch):
        """prune_stale must not write osint_cache.json when HEART_DRY_RUN=1, but still count.

        Regression test: before the fix, prune_and_save always called save() regardless
        of dry-run mode, so --dry-run and sandboxed tests would silently mutate the cache.
        """
        import os
        from Heart.tools.heart import CycleState

        cache_file = heart_mod.BRAIN_PATH / "heartbeat" / "osint_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            '{"version": 1, "generated_at": "2025-01-01T00:00:00Z", '
            '"observations": {"dead": {"last_seen": "2020-01-01T00:00:00Z", "country": "XX"}}}',
            encoding="utf-8",
        )
        original_mtime = cache_file.stat().st_mtime

        monkeypatch.setenv("HEART_DRY_RUN", "1")
        state = CycleState()
        state.cycle = 1
        result = heart_mod._phase_prune_stale(state)

        assert result.ok is True
        assert cache_file.stat().st_mtime == original_mtime, (
            "osint_cache.json must not be modified in dry-run mode"
        )

        # Replace cache and record its mtime BEFORE prune_and_save runs.
        # prune_and_save uses _atomic_write (tmp+rename), which always updates mtime.
        # We assert the mtime advances past this baseline after the save.
        os.unlink(str(cache_file))
        cache_file.write_text(
            '{"version": 1, "generated_at": "2025-01-01T00:00:00Z", '
            '"observations": {"dead2": {"last_seen": "2019-01-01T00:00:00Z"}}, '
            '"pruned": 0}',
            encoding="utf-8",
        )
        before_save_mtime = cache_file.stat().st_mtime
        monkeypatch.setenv("HEART_DRY_RUN", "0")

        result2 = heart_mod._phase_prune_stale(state)
        assert result2.ok is True
        after_save_mtime = cache_file.stat().st_mtime
        assert after_save_mtime > before_save_mtime, (
            "osint_cache.json must be written (mtime advanced) when HEART_DRY_RUN=0; "
            f"before={before_save_mtime}, after={after_save_mtime}"
        )

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

        prune_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "signals_incoming"
        prune_dir.mkdir(parents=True, exist_ok=True)
        # We use a smaller budget so the test is reliable: budget = 1 KB.
        # Both files fit; largest is deleted first, then second survives.
        small = prune_dir / "small.json"
        large = prune_dir / "large.json"
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

    def test_prune_largest_first_caps_at_budget(self, heart_mod, monkeypatch):
        """When budget is exhausted, pruning stops even if more files remain.

        Largest-first: with files [100, 200, 300, 400, 500] and budget=250,
        sorted order is [500, 400, 300, 200, 100]. First file (500) exceeds
        budget immediately, so only 1 file is pruned and we stop.
        """
        import shutil
        from Heart.tools.heart import CycleState

        prune_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "signals_incoming"
        prune_dir.mkdir(parents=True, exist_ok=True)
        file_sizes = [100, 200, 300, 400, 500]
        files = []
        for i, sz in enumerate(file_sizes):
            f = prune_dir / f"file_{i}.bin"
            f.write_bytes(b"x" * sz)
            files.append(f)

        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 950 * 1024 ** 2
                free = 50 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)
        monkeypatch.setenv("HEART_SHARED_PRUNE_BUDGET", "250")

        result = heart_mod._phase_prune_shared(CycleState())
        assert result.ok is True

        deleted = [f for f in files if not f.exists()]
        surviving = [f for f in files if f.exists()]
        # Largest-first: 500 is pruned first, bytes_pruned=500 >= 250 -> stop
        assert len(deleted) == 1, f"budget=250: expected 1 deleted (500), got {len(deleted)}"
        assert len(surviving) == 4
        assert not files[4].exists(), "largest file (500) must be pruned"
        assert files[3].exists(), "second-largest (400) must survive (budget exhausted)"

    def test_disabled_via_env(self, heart_mod, monkeypatch):
        """HEART_SHARED_PRUNE_ENABLED=0 must disable pruning entirely."""
        import shutil
        from Heart.tools.heart import CycleState

        prune_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "signals_incoming"
        prune_dir.mkdir(parents=True, exist_ok=True)
        (prune_dir / "should_stay.json").write_bytes(b"x" * 1024)

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
        assert (prune_dir / "should_stay.json").exists(), "file must not be pruned when HEART_SHARED_PRUNE_ENABLED=0"

    def test_never_prunes_userdata_or_audit(self, heart_mod, monkeypatch):
        """Files outside safe_subtrees must never be removed."""
        import shutil
        from Heart.tools.heart import CycleState

        userdata_dir = heart_mod.BRAIN_PATH.parent / "userdata"
        audit_dir = heart_mod.BRAIN_PATH.parent / "Brain" / "audit"
        signals_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "signals_incoming"
        for d in (userdata_dir, audit_dir, signals_dir):
            d.mkdir(parents=True, exist_ok=True)
        (userdata_dir / "ghost.json").write_bytes(b"protected")
        (audit_dir / "audit.json").write_bytes(b"also_protected")
        (signals_dir / "big.bin").write_bytes(b"x" * 1024)

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
        assert not (signals_dir / "big.bin").exists(), "signals_incoming files are in safe_subtrees and must be pruned"

    def test_prune_largest_first_skips_symlinked_dirs_outside_subtree(self, heart_mod, monkeypatch):
        """Symlinks inside safe_subtrees that point outside must NOT be followed.

        Regression: if prune_largest_first used os.path.isdir without follow_symlinks=False,
        a symlink-to-dir would be recursed into, potentially deleting files outside the
        safe subtree. The fix uses os.scandir with follow_symlinks=False on both is_file
        and is_dir checks so symlinks are treated as files (size of the link target is not
        stat()'ed) and never followed.
        """
        import shutil
        from Heart.tools.heart import CycleState

        # Create a file outside the safe subtree that we must protect
        external_dir = heart_mod.BRAIN_PATH.parent / "external"
        external_dir.mkdir(parents=True)
        protected_file = external_dir / "must_not_delete.txt"
        protected_file.write_bytes(b"critical data")

        # Inside the safe subtree, create a symlink pointing to the external file's directory
        prune_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "signals_incoming"
        prune_dir.mkdir(parents=True)
        link_to_external = prune_dir / "link_to_external"
        try:
            link_to_external.symlink_to(external_dir)
        except (OSError, NotImplementedError):
            # On Windows without dev mode or admin, symlink creation fails; skip test
            return

        # Also create a regular file inside the safe subtree so budget is exceeded
        regular_file = prune_dir / "regular.json"
        regular_file.write_bytes(b"x" * 1024)

        def fake_usage(path):
            class _FU:
                total = 1 * 1024 ** 3
                used = 950 * 1024 ** 2
                free = 50 * 1024 ** 2
            return _FU()
        monkeypatch.setattr("shutil.disk_usage", fake_usage)
        monkeypatch.setenv("HEART_SHARED_PRUNE_BUDGET", "512")  # < 1024 so at least one file pruned

        result = heart_mod._phase_prune_shared(CycleState())
        assert result.ok is True

        # The symlink itself (as a file) may be pruned, but the target must be intact
        assert protected_file.exists(), "external file must not be deleted via symlink recursion"
        assert protected_file.read_bytes() == b"critical data", "external file must be unmodified"

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

    def test_safe_subtrees_excludes_userdata_and_audit(self, heart_mod):
        """The safe_subtrees list must not include userdata/ or audit/."""
        import inspect
        src = inspect.getsource(heart_mod._phase_prune_shared)
        # crude but robust: must not reference userdata/ or audit/ in the safe_subtrees literal.
        # The safe_subtrees list lives between `safe_subtrees = [` and `]`.
        import re
        m = re.search(r"safe_subtrees\s*=\s*\[(.*?)\]", src, re.DOTALL)
        assert m, "safe_subtrees list not found"
        body = m.group(1)
        assert "userdata" not in body, f"userdata must not be in safe_subtrees: {body!r}"
        assert "audit" not in body, f"audit must not be in safe_subtrees: {body!r}"
        # Must include the three known transient roots.
        for required in ("signals_incoming", "abuse_signals", "poke_queue"):
            assert required in body, f"safe_subtrees must include {required!r}; got {body!r}"

    def test_audit_written_even_when_no_files_pruned(self, heart_mod, monkeypatch):
        """When triggered, an audit entry is written even if 0 files were pruned.

        Without the fix, the audit only fired when `files_pruned > 0`, which
        lost historical context (e.g. when the safe subtrees are already empty
        but disk pressure is sustained).
        """
        import shutil
        from Heart.tools.heart import CycleState
        prune_dir = heart_mod.BRAIN_PATH.parent / "heartbeat" / "signals_incoming"
        prune_dir.mkdir(parents=True, exist_ok=True)
        # empty subtree — disk pressure but nothing to prune

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
        assert audit.exists(), "audit must be written when triggered even if no files pruned"
        content = audit.read_text(encoding="utf-8")
        assert "files_pruned: 0" in content

    def test_enqueue_poke_sanitizes_path_traversal_kind(self, heart_mod):
        """A kind with '/' or '..' must be sanitized so the file stays in queue_dir."""
        from Heart.tools.heart import CycleState
        state = CycleState()
        state.cycle = 1
        # Try to escape the queue_dir with a malicious kind.
        heart_mod._enqueue_poke(state, "../../etc/passwd", {"x": 1})
        heart_mod._enqueue_poke(state, "evil/../escape", {"x": 1})
        queue_dir = heart_mod.BRAIN_PATH / "heartbeat" / "poke_queue"
        for f in queue_dir.iterdir():
            assert ".." not in f.name, f"path traversal in poke filename: {f.name}"
            assert "/" not in f.name, f"slash in poke filename: {f.name}"
        # No file should be created outside queue_dir.
        outside = heart_mod.BRAIN_PATH / "etc" / "passwd"
        assert not outside.exists()


# ── Atomic write helpers ──────────────────────────────────────────────────────

class TestAtomicWriteHelpers:
    """Unit tests for _atomic_write_json, _atomic_write_yaml, _atomic_write_text."""

    def test_atomic_write_json_creates_file(self, heart_mod, tmp_path):
        path = tmp_path / "test.json"
        heart_mod._atomic_write_json(path, {"key": "value", "num": 42})
        assert path.exists()
        import json
        assert json.loads(path.read_text()) == {"key": "value", "num": 42}

    def test_atomic_write_json_overwrites_atomically(self, heart_mod, tmp_path):
        path = tmp_path / "test.json"
        heart_mod._atomic_write_json(path, {"v": 1})
        heart_mod._atomic_write_json(path, {"v": 2})
        import json
        assert json.loads(path.read_text()) == {"v": 2}

    def test_atomic_write_json_dry_run_skips(self, heart_mod, monkeypatch, tmp_path):
        monkeypatch.setattr(heart_mod, "DRY_RUN", True)
        path = tmp_path / "test.json"
        heart_mod._atomic_write_json(path, {"v": 1})
        assert not path.exists()

    def test_atomic_write_yaml_creates_file(self, heart_mod, tmp_path):
        path = tmp_path / "test.yaml"
        heart_mod._atomic_write_yaml(path, {"key": "value", "list": [1, 2, 3]})
        assert path.exists()
        import yaml
        assert yaml.safe_load(path.read_text()) == {"key": "value", "list": [1, 2, 3]}

    def test_atomic_write_yaml_dry_run_skips(self, heart_mod, monkeypatch, tmp_path):
        monkeypatch.setattr(heart_mod, "DRY_RUN", True)
        path = tmp_path / "test.yaml"
        heart_mod._atomic_write_yaml(path, {"v": 1})
        assert not path.exists()

    def test_atomic_write_text_creates_file(self, heart_mod, tmp_path):
        path = tmp_path / "test.txt"
        heart_mod._atomic_write_text(path, "hello world\n")
        assert path.exists()
        assert path.read_text() == "hello world\n"

    def test_atomic_write_text_dry_run_skips(self, heart_mod, monkeypatch, tmp_path):
        monkeypatch.setattr(heart_mod, "DRY_RUN", True)
        path = tmp_path / "test.txt"
        heart_mod._atomic_write_text(path, "hello")
        assert not path.exists()


# ── _amend_observation signal logic ───────────────────────────────────────────

class TestAmendObservationSignals:
    """Unit tests for _amend_observation signal generation logic.

    Ensures that:
    - new_ip signal fires on first observation
    - geo_drift signal fires on country change
    - is_vpn/is_tor/is_proxy signals fire on status change (0→1)
    - geo_drift takes priority over vpn/tor/proxy when both change
    - signal is per-cycle only (does not persist across cycles)
    """

    @pytest.fixture
    def osint_mod(self):
        """Fresh import of osint_cache per test."""
        import importlib
        import sys
        for k in list(sys.modules.keys()):
            if "osint_cache" in k:
                del sys.modules[k]
        import Heart.tools.osint_cache as m
        importlib.reload(m)
        return m

    def test_new_ip_signal(self, osint_mod):
        raw = {"ip": "192.0.2.1", "country_code": "BE", "is_vpn": False}
        amended = osint_mod._amend_observation(None, raw)
        assert amended["_signal"] == "new_ip"

    def test_geo_drift_signal(self, osint_mod):
        existing = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False,
                    "last_country_code": "BE", "geo_drift_count": 0, "last_drift_at": None}
        raw = {"ip": "192.0.2.1", "country_code": "FR", "is_vpn": False}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["_signal"] == "geo_drift"
        assert amended["geo_drift_count"] == 1
        assert amended["last_country_code"] == "FR"

    def test_vpn_signal_on_activation(self, osint_mod):
        existing = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        raw = {"ip": "192.0.2.1", "country_code": "BE", "is_vpn": True}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["_signal"] == "is_vpn"
        assert amended["is_vpn"] is True

    def test_tor_signal_on_activation(self, osint_mod):
        existing = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        raw = {"ip": "192.0.2.1", "country_code": "BE", "is_tor": True}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["_signal"] == "is_tor"
        assert amended["is_tor"] is True

    def test_proxy_signal_on_activation(self, osint_mod):
        existing = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False}
        raw = {"ip": "192.0.2.1", "country_code": "BE", "is_proxy": True}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["_signal"] == "is_proxy"
        assert amended["is_proxy"] is True

    def test_geo_drift_priority_over_vpn(self, osint_mod):
        """When both country drift AND VPN activation happen in same cycle,
        geo_drift takes priority (set earlier in the function)."""
        existing = {"country_code": "BE", "is_vpn": False, "is_tor": False, "is_proxy": False,
                    "last_country_code": "BE", "geo_drift_count": 0}
        raw = {"ip": "192.0.2.1", "country_code": "FR", "is_vpn": True}
        amended = osint_mod._amend_observation(existing, raw)
        assert amended["_signal"] == "geo_drift"
        assert amended["is_vpn"] is True  # still recorded, just not the signal

    def test_signal_does_not_persist_across_cycles(self, osint_mod):
        """Signal is per-cycle only; a subsequent observation without changes
        must NOT retain the previous cycle's signal."""
        existing = {"country_code": "BE", "is_vpn": True, "is_tor": False, "is_proxy": False,
                    "_signal": "is_vpn"}  # signal from previous cycle
        raw = {"ip": "192.0.2.1", "country_code": "BE", "is_vpn": True}  # no change
        amended = osint_mod._amend_observation(existing, raw)
        # No status change → no _signal set (previous signal consumed)
        assert "_signal" not in amended or amended.get("_signal") is None


# ── prune_largest_first budget boundary ───────────────────────────────────────

class TestPruneLargestFirstBudget:
    """Boundary tests for prune_largest_first budget enforcement."""

    @pytest.fixture
    def shared_prune_mod(self):
        """Fresh import of heart_shared_prune per test."""
        import importlib
        import sys
        for k in list(sys.modules.keys()):
            if "heart_shared_prune" in k:
                del sys.modules[k]
        import Heart.tools.heart_shared_prune as m
        importlib.reload(m)
        return m

    def test_single_file_exceeds_budget_still_pruned(self, shared_prune_mod, tmp_path):
        """A single file larger than budget must still be pruned (first file always
        checked against budget AFTER adding its size)."""
        d = tmp_path / "obs"
        d.mkdir()
        (d / "huge.bin").write_bytes(b"x" * 10_000)
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=1_000)
        assert n == 1
        assert b == 10_000
        assert not (d / "huge.bin").exists()

    def test_exact_budget_match_prunes_and_stops(self, shared_prune_mod, tmp_path):
        """When bytes_pruned == budget_bytes exactly, loop must stop."""
        d = tmp_path / "obs"
        d.mkdir()
        (d / "a.bin").write_bytes(b"x" * 5_000)
        (d / "b.bin").write_bytes(b"x" * 5_000)
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=10_000)
        assert n == 2
        assert b == 10_000
        assert not (d / "a.bin").exists()
        assert not (d / "b.bin").exists()

    def test_zero_budget_prunes_nothing(self, shared_prune_mod, tmp_path):
        d = tmp_path / "obs"
        d.mkdir()
        (d / "a.bin").write_bytes(b"x" * 100)
        n, b = shared_prune_mod.prune_largest_first([d], budget_bytes=0)
        assert n == 0
        assert b == 0
        assert (d / "a.bin").exists()
