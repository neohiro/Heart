#!/usr/bin/env python3
"""
test_grounding.py — unit tests for Heart/tools/grounding.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = ROOT / 'Heart' / 'tools' / 'grounding.py'


def _load():
    spec = importlib.util.spec_from_file_location('grounding', str(TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOOLS_DIR = str(TOOL_PATH.parent)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)


class TestGrounding(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load()
        cls.tmp = Path(tempfile.mkdtemp(prefix='grounding-test-'))
        os.environ['NEOHIRO_SHARED_ROOT'] = str(cls.tmp)

        # Set up directory structure
        (cls.tmp / 'brain' / 'knowledge').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'brain' / 'audit').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'brain' / 'watch' / 'state').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'public' / 'health').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'heart' / 'audit' / 'instant').mkdir(parents=True, exist_ok=True)

        # Write sources.yaml
        sources_yaml = """---
schema_version: 1
sources:
  - id: repo:neohiro/LLM
    type: github
    repo: neohiro/LLM
    schedule: every_30_minutes
  - id: news:hn
    type: rss
    urls: ["https://hnrss.org/frontpage"]
    schedule: every_15_minutes
"""
        (cls.tmp / 'brain' / 'knowledge' / 'sources.yaml').write_text(sources_yaml, encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        if cls.tmp.is_dir():
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_load_sources(self):
        sources, ok = self.mod._load_sources()
        self.assertTrue(ok)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]['id'], 'repo:neohiro/LLM')
        self.assertEqual(sources[1]['type'], 'rss')

    def test_load_empty_sources(self):
        path = self.mod._sources_path()
        original = path.read_text(encoding='utf-8')
        path.write_text('', encoding='utf-8')
        try:
            sources, ok = self.mod._load_sources()
            self.assertTrue(ok)
            self.assertEqual(sources, [])
        finally:
            path.write_text(original, encoding='utf-8')

    def test_load_sources_parse_error(self):
        path = self.mod._sources_path()
        original = path.read_text(encoding='utf-8')
        path.write_text('invalid: yaml: content: [', encoding='utf-8')
        try:
            sources, ok = self.mod._load_sources()
            self.assertFalse(ok)
            self.assertEqual(sources, [])
        finally:
            path.write_text(original, encoding='utf-8')

    def test_build_samples_size(self):
        import random
        sources, _ = self.mod._load_sources()
        pairs = self.mod._build_samples(sources, 20, random.Random(42))
        # Min sample is 20, but we only have 2 sources
        # The implementation cycles through github_vars, so we get multiple entries
        # but only 2 distinct scopes. Total entries = 2 (one rss, one github)
        self.assertGreaterEqual(len(pairs), 2)
        for s, v in pairs:
            self.assertIn('id', s)
            self.assertIsInstance(v, str)

    def test_build_samples_min(self):
        import random
        sources, _ = self.mod._load_sources()
        # Even with low request, samples should be at least 1 per source
        pairs = self.mod._build_samples(sources, 5, random.Random(0))
        self.assertGreaterEqual(len(pairs), 1)

    def test_dry_run(self):
        # Verify dry-run path doesn't write
        audit_path = self.mod._audit_path()
        if audit_path.exists():
            audit_path.unlink()
        result = self._invoke_main(['--dry-run'])
        self.assertEqual(result, 0)
        self.assertFalse(audit_path.exists(), 'dry-run should not write audit log')

    def test_write_health(self):
        samples = [
            {'ts': '2026-01-01T00:00:00Z', 'scope': 'a', 'variable': 'v1', 'cached_value': '1', 'fetched_value': '1', 'matched': True, 'latency_ms': 100, 'fingerprint': 'a'},
            {'ts': '2026-01-01T00:00:00Z', 'scope': 'b', 'variable': 'v2', 'cached_value': '2', 'fetched_value': '3', 'matched': False, 'latency_ms': 200, 'fingerprint': 'b'},
        ]
        self.mod._write_health(5, samples)
        health_path = self.mod._health_path()
        self.assertTrue(health_path.exists())
        data = json.loads(health_path.read_text(encoding='utf-8'))
        self.assertEqual(data['matched'], 1)
        self.assertEqual(data['mismatched'], 1)
        self.assertEqual(data['grounding_rate'], 0.5)
        self.assertEqual(data['band'], 'red')
        self.assertEqual(len(data['mismatched_scopes']), 1)
        self.assertEqual(data['mismatched_scopes'][0]['scope'], 'b')

    def test_write_health_all_match(self):
        samples = [
            {'ts': '2026-01-01T00:00:00Z', 'scope': 'a', 'variable': 'v1', 'cached_value': '1', 'fetched_value': '1', 'matched': True, 'latency_ms': 100, 'fingerprint': 'a'},
            {'ts': '2026-01-01T00:00:00Z', 'scope': 'b', 'variable': 'v2', 'cached_value': '2', 'fetched_value': '2', 'matched': True, 'latency_ms': 200, 'fingerprint': 'b'},
        ]
        self.mod._write_health(5, samples)
        data = json.loads(self.mod._health_path().read_text(encoding='utf-8'))
        self.assertEqual(data['grounding_rate'], 1.0)
        self.assertEqual(data['band'], 'green')

    def test_append_audit(self):
        audit = self.mod._audit_path()
        if audit.exists():
            audit.unlink()
        self.mod._append_audit({'ts': 'now', 'matched': True})
        self.mod._append_audit({'ts': 'now', 'matched': False})
        lines = [l for l in audit.read_text(encoding='utf-8').strip().split('\n') if l]
        self.assertEqual(len(lines), 2)
        for l in lines:
            obj = json.loads(l)
            self.assertIn('ts', obj)
            self.assertIn('matched', obj)

    def test_emit_poke(self):
        poke_dir = self.tmp / 'heart' / 'audit' / 'instant'
        before = set(p.name for p in poke_dir.iterdir())
        self.mod._emit_poke(reason='test', priority='low', fingerprint='test-fp')
        after = set(p.name for p in poke_dir.iterdir())
        new_files = after - before
        self.assertEqual(len(new_files), 1)
        # Verify content
        content = (poke_dir / new_files.pop()).read_text(encoding='utf-8')
        self.assertIn('test-fp', content)
        self.assertIn('grounding_audit', content)

    def test_load_per_scope_state(self):
        safe = self.mod._sanitize_scope_for_path('repo:neohiro/LLM')
        scope_dir = self.tmp / 'brain' / 'watch' / 'state' / safe
        scope_dir.mkdir(parents=True, exist_ok=True)
        state_file = scope_dir / 'releases.json'
        state_file.write_text(json.dumps({'value': 'v1.2.3', 'fetched_at': '2026-01-01T00:00:00Z'}), encoding='utf-8')
        data = self.mod._load_per_scope_state('repo:neohiro/LLM', 'releases')
        self.assertIsNotNone(data)
        self.assertEqual(data['value'], 'v1.2.3')

    def test_load_per_scope_state_missing(self):
        data = self.mod._load_per_scope_state('nonexistent', 'var')
        self.assertIsNone(data)

    def test_iso_now(self):
        ts = self.mod._iso_now()
        self.assertRegex(ts, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')

    def test_no_sources_returns_1(self):
        path = self.mod._sources_path()
        original = path.read_text(encoding='utf-8')
        path.write_text('schema_version: 1\n', encoding='utf-8')
        try:
            result = self._invoke_main([])
            self.assertEqual(result, 1)
        finally:
            path.write_text(original, encoding='utf-8')


    def _invoke_main(self, argv: list[str]) -> int:
        """Invoke main() with the given argv list and return the exit code."""
        import sys
        old_argv = sys.argv
        sys.argv = ['grounding.py'] + argv
        try:
            return self.mod.main()
        except SystemExit as e:
            return int(e.code) if e.code is not None else 0
        finally:
            sys.argv = old_argv


def main_with_args_patch(mod, args):
    """Helper: invoke main with argv, return exit code."""
    import argparse
    return mod.main()


class TestReadLastCycleRate(unittest.TestCase):
    """Tests for _read_last_cycle_rate (now backed by grounding.last_rate.json sidecar)."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load()
        cls.tmp = Path(tempfile.mkdtemp(prefix='grounding-rate-test-'))
        os.environ['NEOHIRO_SHARED_ROOT'] = str(cls.tmp)
        (cls.tmp / 'brain' / 'audit').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'brain' / 'knowledge').mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_sidecar_file(self):
        sidecar = self.tmp / 'brain' / 'audit' / 'grounding.last_rate.json'
        if sidecar.exists():
            sidecar.unlink()
        rate, ts = self.mod._read_last_cycle_rate()
        self.assertIsNone(rate)
        self.assertIsNone(ts)

    def test_sidecar_with_rate(self):
        sidecar = self.tmp / 'brain' / 'audit' / 'grounding.last_rate.json'
        sidecar.write_text(json.dumps({'ts': '2026-08-30T00:00:00Z', 'grounding_rate': 0.85}), encoding='utf-8')
        rate, ts = self.mod._read_last_cycle_rate()
        self.assertEqual(rate, 0.85)
        self.assertEqual(ts, '2026-08-30T00:00:00Z')

    def test_sidecar_with_zero_rate(self):
        sidecar = self.tmp / 'brain' / 'audit' / 'grounding.last_rate.json'
        sidecar.write_text(json.dumps({'ts': '2026-08-30T00:00:00Z', 'grounding_rate': 0.0}), encoding='utf-8')
        rate, ts = self.mod._read_last_cycle_rate()
        self.assertEqual(rate, 0.0)
        self.assertIsNotNone(rate)

    def test_sidecar_missing_rate_key(self):
        sidecar = self.tmp / 'brain' / 'audit' / 'grounding.last_rate.json'
        sidecar.write_text(json.dumps({'ts': '2026-08-30T00:00:00Z', 'matched': 8}), encoding='utf-8')
        rate, ts = self.mod._read_last_cycle_rate()
        self.assertIsNone(rate)

    def test_sidecar_corrupt_json(self):
        sidecar = self.tmp / 'brain' / 'audit' / 'grounding.last_rate.json'
        sidecar.write_text('not valid json', encoding='utf-8')
        rate, ts = self.mod._read_last_cycle_rate()
        self.assertIsNone(rate)


class TestEmitPokeUniqueness(unittest.TestCase):
    """Tests for _emit_poke (pass-2 fix: ULID-based filename)."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load()
        cls.tmp = Path(tempfile.mkdtemp(prefix='grounding-poke-test-'))
        os.environ['NEOHIRO_SHARED_ROOT'] = str(cls.tmp)
        (cls.tmp / 'heart' / 'audit' / 'instant').mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_poke_filename_uses_ulid(self):
        self.mod._emit_poke('test 1', priority='high', fingerprint='x')
        files = list((self.tmp / 'heart' / 'audit' / 'instant').glob('grounding-*.yaml'))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r'^grounding-[0-9A-HJKMNP-TV-Z]{26}\.yaml$')


class TestPokePolicy(unittest.TestCase):
    """Regression test for the two-cycle poke policy.

    Pass-3 fix: _read_last_cycle_rate() was called AFTER appending the current
    aggregate, so it found the current cycle's rate instead of the previous one.
    The poke policy (fire only if TWO consecutive cycles < 0.90) was broken:
    it compared the current rate to itself.

    Fix: call _read_last_cycle_rate() BEFORE _append_audit(current_aggregate).
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load()
        cls.tmp = Path(tempfile.mkdtemp(prefix='grounding-poke-policy-'))
        os.environ['NEOHIRO_SHARED_ROOT'] = str(cls.tmp)
        os.environ.pop('GH_TOKEN', None)
        (cls.tmp / 'brain' / 'knowledge').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'brain' / 'audit').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'heart' / 'audit' / 'instant').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'brain' / 'watch' / 'state').mkdir(parents=True, exist_ok=True)
        (cls.tmp / 'public' / 'health').mkdir(parents=True, exist_ok=True)
        sources_yaml = 'schema_version: 1\nsources:\n  - id: repo:test/repo\n    type: github\n    repo: test/repo\n'
        (cls.tmp / 'brain' / 'knowledge' / 'sources.yaml').write_text(sources_yaml, encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _invoke_main(self, argv: list[str]) -> int:
        import sys
        old_argv = sys.argv
        sys.argv = ['grounding.py'] + argv
        try:
            return self.mod.main()
        except SystemExit as e:
            return int(e.code) if e.code is not None else 0
        finally:
            sys.argv = old_argv

    def _write_last_rate(self, rate: float) -> None:
        """Write the sidecar file that records the previous cycle's rate."""
        self.mod._last_rate_path().write_text(
            json.dumps({
                'ts': '2026-08-30T00:00:00Z',
                'grounding_rate': rate,
                'matched': 8,
                'total': 10,
                'band': 'yellow' if rate < 0.95 else 'green',
            }),
            encoding='utf-8',
        )

    def test_poke_fires_on_two_consecutive_low_rates(self):
        audit_path = self.mod._audit_path()
        if audit_path.exists():
            audit_path.unlink()
        last_rate_path = self.mod._last_rate_path()
        if last_rate_path.exists():
            last_rate_path.unlink()
        poke_dir = self.tmp / 'heart' / 'audit' / 'instant'
        existing_pokes = set(poke_dir.glob('grounding-*.yaml'))
        self._write_last_rate(0.80)
        self._invoke_main(['--dry-run'])
        poke_dir.mkdir(parents=True, exist_ok=True)
        new_pokes = set(poke_dir.glob('grounding-*.yaml')) - existing_pokes
        self.assertEqual(len(new_pokes), 0, 'dry-run should not emit poke')

        before_pokes = set(poke_dir.glob('grounding-*.yaml'))
        self._invoke_main([])
        after_pokes = set(poke_dir.glob('grounding-*.yaml')) - before_pokes
        self.assertEqual(len(after_pokes), 1, 'two consecutive low-rate cycles should emit poke')

    def test_no_poke_on_first_low_rate(self):
        audit_path = self.mod._audit_path()
        if audit_path.exists():
            audit_path.unlink()
        last_rate_path = self.mod._last_rate_path()
        if last_rate_path.exists():
            last_rate_path.unlink()
        before_pokes = set((self.tmp / 'heart' / 'audit' / 'instant').glob('grounding-*.yaml'))
        self._invoke_main([])
        after_pokes = set((self.tmp / 'heart' / 'audit' / 'instant').glob('grounding-*.yaml')) - before_pokes
        self.assertEqual(len(after_pokes), 0, 'first low-rate cycle (no history) should not emit poke')

    def test_no_poke_when_only_current_low(self):
        audit_path = self.mod._audit_path()
        if audit_path.exists():
            audit_path.unlink()
        last_rate_path = self.mod._last_rate_path()
        if last_rate_path.exists():
            last_rate_path.unlink()
        self._write_last_rate(0.95)
        before_pokes = set((self.tmp / 'heart' / 'audit' / 'instant').glob('grounding-*.yaml'))
        self._invoke_main([])
        after_pokes = set((self.tmp / 'heart' / 'audit' / 'instant').glob('grounding-*.yaml')) - before_pokes
        self.assertEqual(len(after_pokes), 0, 'only current low (previous was 0.95) should not emit poke')


if __name__ == '__main__':
    import unittest as _unittest
    _unittest.main(verbosity=2)
