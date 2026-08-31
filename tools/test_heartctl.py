#!/usr/bin/env python3
"""
test_heartctl.py — tests for the router subcommand added to heartctl.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HEARCTL_PATH = ROOT / 'Heart' / 'tools' / 'heartctl.py'


class TestHeartctlRouter(unittest.TestCase):

    def test_router_help(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'router', '--help'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--preset', r.stdout)

    def test_router_unknown_preset(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'router',
             '--preset', 'nonsense'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('invalid choice', r.stderr)

    def test_router_dry_run(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'router',
             '--preset', 'coding', '--dry-run'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('router:', r.stdout)
        self.assertIn('coding', r.stdout)

    def test_router_json(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'router',
             '--preset', 'reasoning', '--json'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        data = json.loads(r.stdout)
        self.assertIn('preset_id', data)
        self.assertIn('model_id', data)
        self.assertIn('confidence', data)
        self.assertIn('prefer_tier', data)

    def test_audit_uses_utf8_encoding(self):
        """Pass-4 fix: cmd_audit must read with encoding='utf-8' (not cp1252 on Windows)."""
        import tempfile, os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            tmp_brain = Path(tmp) / 'brain'
            audit = tmp_brain / 'audit' / 'heartbeat.yaml'
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text(
                "ts: 2026-01-01T00:00:00Z\nphase: test\nok: true\n\n"
                "ts: 2026-01-01T00:01:00Z\nphase: ascii-only\nok: true\n\n",
                encoding='utf-8',
            )
            env = os.environ.copy()
            env['BRAIN_PATH'] = str(tmp_brain)
            r = subprocess.run(
                [sys.executable, str(HEARCTL_PATH), 'audit', '--lines', '5'],
                capture_output=True, text=True, encoding='utf-8', errors='replace', env=env,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('ascii-only', r.stdout)


class TestHeartctlDelegate(unittest.TestCase):

    def test_delegate_help(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate', '--help'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('--objective', r.stdout)
        self.assertIn('--acceptance', r.stdout)
        self.assertIn('--auto-resume', r.stdout)

    def test_delegate_requires_objective(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--objective', r.stderr)

    def test_delegate_dry_run_local_fallback(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate',
             '--objective', 'Refactor the router cascade.',
             '--dry-run', '--target', 'local'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('delegate:', r.stdout)
        self.assertIn('local', r.stdout)
        self.assertIn('dry-run', r.stdout)

    def test_delegate_json_output(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate',
             '--objective', 'Add a new endpoint to the API.',
             '--scope', 'neohiro/LLM',
             '--org', 'neohiro',
             '--auto-resume',
             '--acceptance', 'endpoint returns 200',
             '--acceptance', 'tests pass',
             '--json', '--dry-run'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        data = json.loads(r.stdout)
        self.assertIn('route', data)
        self.assertIn('task_id', data)
        self.assertIn('cascade_model', data)
        self.assertEqual(data['cascade_model'], 'openrouter/free')
        self.assertIn('auto_resume', data)
        self.assertEqual(data['auto_resume'], True)
        self.assertEqual(data['route'], 'local')
        self.assertIn('reason', data)

    def test_delegate_unknown_org_rejected(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate',
             '--objective', 'Do something.',
             '--org', 'evilcorp'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('invalid choice', r.stderr)

    def test_delegate_objective_too_long(self):
        long_obj = 'x' * 1025
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate',
             '--objective', long_obj, '--dry-run'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('1024', r.stderr)

    def test_delegate_record_route_field(self):
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate',
             '--objective', 'Quick fix for the router.',
             '--dry-run', '--target', 'local',
             '--json'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        data = json.loads(r.stdout)
        self.assertEqual(data['route'], 'local')
        self.assertEqual(data['cascade_model'], 'openrouter/free')


    def test_delegate_json_on_rejection(self):
        """When --json is set, validation rejections emit JSON to stdout (not just stderr)."""
        long_obj = 'x' * 1025
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate',
             '--objective', long_obj, '--json', '--dry-run'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        import json
        data = json.loads(r.stdout)
        self.assertEqual(data['route'], 'rejected')
        self.assertIn('reason', data)
        self.assertEqual(data['cascade_model'], 'openrouter/free')

    def test_delegate_acceptance_with_dash_passes(self):
        """--acceptance text is freeform (not path-checked) and emits JSON."""
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate',
             '--objective', 'Do a thing.',
             '--acceptance', 'path traversal forbidden',
             '--json', '--dry-run'],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        import json
        data = json.loads(r.stdout)
        self.assertIn('route', data)

    def test_watch_session_missing_dir(self):
        """delegate-watch returns 1 when the session dir does not exist."""
        r = subprocess.run(
            [sys.executable, str(HEARCTL_PATH), 'delegate-watch',
             '--session-id', 'deadbeef-dead-beef-dead-beefdeadbeef',
             '--poll-interval', '5'],
            capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('session dir not found', r.stderr)

    def test_watch_session_shows_new_files(self):
        """delegate-watch prints a line when a new file appears in the session dir."""
        import tempfile, pathlib, time

        tmp = pathlib.Path(tempfile.mkdtemp(prefix='heartctl-watch-test-'))
        session_id = 'abc-123'
        # _shared_root() = NEOHIRO_SHARED_ROOT, so session dir is <shared>/brain/opencode/sessions/<id>.
        session_dir = tmp / 'brain' / 'opencode' / 'sessions' / session_id
        session_dir.mkdir(parents=True)
        (session_dir / 'brief.json').write_text('{}', encoding='utf-8')

        proc = subprocess.Popen(
            [sys.executable, '-u', str(HEARCTL_PATH), 'delegate-watch',
             '--session-id', session_id,
             '--poll-interval', '5'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, 'NEOHIRO_SHARED_ROOT': str(tmp)},
        )
        try:
            time.sleep(2)
            (session_dir / 'report.md').write_text('# Report\n', encoding='utf-8')
            time.sleep(10)  # ensure at least one poll cycle observes the new file
        finally:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
        self.assertIn('Watching', stdout)
        self.assertIn('brief.json', stdout)
        self.assertIn('report.md', stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestCmdModeValidation(unittest.TestCase):
    """heartctl.py cmd_mode must not write whitespace-only mode values."""

    def test_whitespace_only_mode_not_written(self):
        """A whitespace-only mode string must be rejected, not written as 'mode:  '."""
        import argparse, importlib.util, sys
        sys.path.insert(0, str(HEARCTL_PATH.parent))
        spec = importlib.util.spec_from_file_location("heartctl", HEARCTL_PATH)
        heartctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(heartctl)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain

            args = argparse.Namespace(mode_value="   ", brain_path=str(brain))
            rc = heartctl.cmd_mode(args)

            mode_file = brain / "heartbeat" / "mode.yaml"
            self.assertFalse(mode_file.exists(),
                "whitespace-only mode_value must not create mode.yaml")

    def test_valid_mode_written(self):
        """A non-empty mode string is written correctly."""
        import argparse, importlib.util, sys
        sys.path.insert(0, str(HEARCTL_PATH.parent))
        spec = importlib.util.spec_from_file_location("heartctl", HEARCTL_PATH)
        heartctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(heartctl)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain

            args = argparse.Namespace(mode_value="sports", brain_path=str(brain))
            rc = heartctl.cmd_mode(args)

            mode_file = brain / "heartbeat" / "mode.yaml"
            self.assertTrue(mode_file.exists())
            self.assertIn("sports", mode_file.read_text())

    def test_empty_mode_does_not_overwrite_existing(self):
        """An empty mode_value must not overwrite an existing mode.yaml."""
        import argparse, importlib.util, sys
        sys.path.insert(0, str(HEARCTL_PATH.parent))
        spec = importlib.util.spec_from_file_location("heartctl", HEARCTL_PATH)
        heartctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(heartctl)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain

            # Pre-seed with a real mode
            mode_file = brain / "heartbeat" / "mode.yaml"
            mode_file.parent.mkdir(parents=True, exist_ok=True)
            mode_file.write_text("mode: normal\n")

            # Empty mode_value must not overwrite
            args = argparse.Namespace(mode_value="", brain_path=str(brain))
            heartctl.cmd_mode(args)

            self.assertIn("normal", mode_file.read_text(),
                "empty mode_value must not overwrite existing mode.yaml")

    def test_atomic_mode_write_no_leftover_tmp(self):
        """cmd_mode must not leave a .tmp file behind after a successful write."""
        import argparse, importlib.util, sys
        sys.path.insert(0, str(HEARCTL_PATH.parent))
        spec = importlib.util.spec_from_file_location("heartctl", HEARCTL_PATH)
        heartctl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(heartctl)

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            args = argparse.Namespace(mode_value="turbo", brain_path=str(brain))
            heartctl.cmd_mode(args)

            mode_file = brain / "heartbeat" / "mode.yaml"
            self.assertTrue(mode_file.exists())
            self.assertIn("turbo", mode_file.read_text())
            # No leftover temp files
            leftover = list((brain / "heartbeat").glob(".mode.yaml.*.tmp"))
            self.assertEqual(leftover, [],
                f"atomic write must not leave temp files behind: {leftover}")
