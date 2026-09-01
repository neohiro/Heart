#!/usr/bin/env python3
"""
test_banctl.py — unit tests for Heart/tools/banctl.py

Run:  python Heart/tools/test_banctl.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = ROOT / 'Heart' / 'tools' / 'banctl.py'


def _load_banctl():
    """Load banctl.py as a module and return it."""
    spec = importlib.util.spec_from_file_location('banctl', str(TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestValidation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_banctl()
        cls.tmp = Path(tempfile.mkdtemp(prefix='banctl-test-'))
        os.environ['NEOHIRO_SHARED_ROOT'] = str(cls.tmp)

        ban_path = cls.tmp / 'brain' / 'audit'
        audit_path = cls.tmp / 'heart' / 'audit'
        ban_path.mkdir(parents=True, exist_ok=True)
        audit_path.mkdir(parents=True, exist_ok=True)

        def _ban_list_path():
            return ban_path / 'ban_list.yaml'

        def _audit_log_path():
            return audit_path / 'ban_enforcement.jsonl'

        def _instant_error_path(ban_id):
            p = cls.tmp / 'heart' / 'audit' / 'instant' / f'ban-{ban_id}.yaml'
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        cls.mod._ban_list_path = staticmethod(_ban_list_path)
        cls.mod._audit_log_path = staticmethod(_audit_log_path)
        cls.mod._instant_error_path = staticmethod(_instant_error_path)

    @classmethod
    def tearDownClass(cls):
        if cls.tmp.is_dir():
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ── Identifier validation ────────────────────────────────────────────

    def test_github_login_valid(self):
        self.mod.validate_identifier('github_login', 'octocat')
        self.mod.validate_identifier('github_login', 'a-b-c-1')
        self.mod.validate_identifier('github_login', 'x' * 39)

    def test_github_login_rejects(self):
        for bad in ['../etc', '-leading-dash', 'trailing-', '', 'has space', 'x' * 40]:
            with self.assertRaises(SystemExit, msg=f'should reject {bad!r}'):
                self.mod.validate_identifier('github_login', bad)

    def test_sha256_valid(self):
        h = '5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8'
        for t in ('email_hash', 'phone_hash', 'ip_hash', 'cookie_token'):
            self.mod.validate_identifier(t, h)

    def test_sha256_rejects_short(self):
        for t in ('email_hash', 'phone_hash', 'ip_hash', 'cookie_token'):
            with self.assertRaises(SystemExit, msg=f'should reject short {t}'):
                self.mod.validate_identifier(t, 'abc')

    def test_sha256_rejects_nonhex(self):
        for t in ('email_hash', 'phone_hash', 'ip_hash', 'cookie_token'):
            with self.assertRaises(SystemExit, msg=f'should reject non-hex {t}'):
                self.mod.validate_identifier(t, 'Z' * 64)

    def test_pgp_fingerprint(self):
        self.mod.validate_identifier('pgp_fingerprint', 'A' * 40)
        self.mod.validate_identifier('pgp_fingerprint', 'a' * 40)
        with self.assertRaises(SystemExit):
            self.mod.validate_identifier('pgp_fingerprint', 'A' * 39)
        with self.assertRaises(SystemExit):
            self.mod.validate_identifier('pgp_fingerprint', 'Z' * 40)

    def test_userdata_id(self):
        self.mod.validate_identifier('userdata_id', self.mod._ulid_now())
        with self.assertRaises(SystemExit):
            self.mod.validate_identifier('userdata_id', 'not-a-ulid')

    def test_unknown_identifier(self):
        with self.assertRaises(SystemExit):
            self.mod.validate_identifier('nonsense', 'x')

    def test_list_none_value(self):
        # Pass-2 regression: cmd_list table formatting crashed on None value
        import argparse
        # Mock _parse_ban_list to return a ban with value=None
        orig = self.mod._parse_ban_list
        def fake_parse(raw):
            return {
                'schema_version': 1,
                'last_updated': '2026-08-30T00:00:00Z',
                'bans': [{
                    'id': 'ban-2026-08-30-001',
                    'identifier': 'github_login',
                    'value': None,  # the regression case
                    'reason': 'abuse',
                    'expires_at': 'never',
                    'scope': 'all',
                }]
            }
        self.mod._parse_ban_list = staticmethod(fake_parse)
        try:
            ns = argparse.Namespace(format='text')
            # Should not raise TypeError: 'NoneType' object is not subscriptable
            ret = self.mod.cmd_list(ns)
            self.assertEqual(ret, 0)
        finally:
            self.mod._parse_ban_list = staticmethod(orig)

    # ── Scope validation ─────────────────────────────────────────────────

    def test_scope_valid(self):
        for s in [
            'all',
            'dashboard_only',
            'chat_only',
            'org:neohiro',
            'org:transhumanists',
            'org:FrenzyPenguin',
            'repo:neohiro/LLM',
            'repo:transhumanists/site',
        ]:
            self.mod.validate_scope(s)

    def test_scope_invalid(self):
        for s in ['everything', 'org:unknown', 'repo:bad', 'NEVER']:
            with self.assertRaises(SystemExit, msg=f'should reject scope {s!r}'):
                self.mod.validate_scope(s)

    # ── Ban-ID format ────────────────────────────────────────────────────

    def test_ban_id_valid(self):
        self.mod.validate_ban_id('ban-2026-08-30-001')
        self.mod.validate_ban_id('ban-2026-01-01-999')

    def test_ban_id_invalid(self):
        for bid in ['foo', 'ban-2026-08-30', 'ban-2026-08-30-1', 'ban-26-08-30-001', 'ban-2026-08-30-1000']:
            with self.assertRaises(SystemExit, msg=f'should reject ban_id {bid!r}'):
                self.mod.validate_ban_id(bid)

    # ── YAML round-trip ──────────────────────────────────────────────────

    def test_yaml_empty(self):
        out = self.mod._parse_ban_list('')
        self.assertEqual(out.get('bans'), [])

    def test_yaml_round_trip(self):
        raw = self.mod._read_yaml_raw(self.mod._ban_list_path())
        data = self.mod._parse_ban_list(raw)
        self.assertIn('bans', data)
        self.assertIn('schema_version', data)

    # ── Audit emission ───────────────────────────────────────────────────

    def test_audit_emits_valid_jsonl(self):
        import json
        path = self.mod._audit_log_path()
        if path.exists():
            path.unlink()
        self.mod._emit_audit('github_api', 'added', 'github_login', 'spamuser', 'ban-test-001', 200)
        content = path.read_text(encoding='utf-8')
        lines = [l for l in content.strip().split('\n') if l]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry['ban_id'], 'ban-test-001')
        self.assertEqual(entry['action'], 'added')

    # ── Idempotent add (without gh) ─────────────────────────────────────

    def test_add_then_list_roundtrip(self):
        import argparse
        # Add a ban with a fake identifier type that doesn't touch GitHub
        args = argparse.Namespace(
            identifier='email_hash',
            value='5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8',
            reason='abuse',
            scope='all',
            expires=None,
            force=False,
            seq=1,
        )
        ret = self.mod.cmd_add(args)
        self.assertEqual(ret, 0)

        # Idempotency: same add should return 0 with 'already banned' message
        args2 = argparse.Namespace(
            identifier='email_hash',
            value='5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8',
            reason='abuse',
            scope='all',
            expires=None,
            force=False,
            seq=1,
        )
        ret = self.mod.cmd_add(args2)
        self.assertEqual(ret, 0)

        # List
        list_args = argparse.Namespace(format='text')
        ret = self.mod.cmd_list(list_args)
        self.assertEqual(ret, 0)

        # Remove — use the dynamically generated ban ID (today's date)
        rem_args = argparse.Namespace(ban_id='ban-2026-09-01-001')
        ret = self.mod.cmd_remove(rem_args)
        self.assertEqual(ret, 0)

        # List (should be empty)
        self.mod.cmd_list(list_args)

    # ── gh API error handling (no gh installed) ─────────────────────────

    def test_gh_api_handles_no_gh(self):
        original_path = os.environ.get('PATH', '')
        os.environ['PATH'] = str(Path(sys.executable).parent)

        def no_gh():
            return shutil.which('gh', path=os.environ['PATH']) is None

        if no_gh():
            status, _ = self.mod._gh_api('GET', '/user')
            self.assertEqual(status, -1)
        else:
            status, body = self.mod._gh_api('GET', '/user')
            self.assertEqual(status, 200)

    def test_remove_from_github_org_handles_404(self):
        # Mock gh: a 404 status from _gh_api means the user is not a member
        # (a "skip, not a real error" condition, not a failure).
        original = self.mod._gh_api
        def mock_gh(method, path, body=None):
            return 404, 'HTTP/1.1 404 Not Found'
        self.mod._gh_api = mock_gh
        try:
            result = self.mod._remove_from_github_org('spamuser', 'neohiro')
            self.assertTrue(result, 'should return True on 404 (not a real error)')
        finally:
            self.mod._gh_api = original


class TestStructuredHttpParsing(unittest.TestCase):
    """Regression tests for Follow-up #3: structured HTTP status parsing."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_banctl()

    def test_parse_http_status_extracts_404(self):
        text = 'HTTP/1.1 404 Not Found\nContent-Type: application/json\n{}'
        self.assertEqual(self.mod._parse_http_status(text), 404)

    def test_parse_http_status_extracts_204(self):
        text = 'HTTP/1.1 204 No Content\n'
        self.assertEqual(self.mod._parse_http_status(text), 204)

    def test_parse_http_status_none_on_empty(self):
        self.assertIsNone(self.mod._parse_http_status(''))
        self.assertIsNone(self.mod._parse_http_status('no headers here'))

    def test_parse_http_status_none_on_invalid(self):
        self.assertIsNone(self.mod._parse_http_status('HTTP/1.1 BAD_STATUS'))

    def test_is_404_returns_true_for_404(self):
        self.assertTrue(self.mod._is_404(404))

    def test_is_404_returns_false_for_non_404(self):
        self.assertFalse(self.mod._is_404(403))
        self.assertFalse(self.mod._is_404(500))
        self.assertFalse(self.mod._is_404(0))

    def test_remove_from_github_org_404_via_structured_status(self):
        # Simulate gh api returning a structured 404 response.
        # The old _is_404(body) would look for '404 ' substring in body.
        # The new _is_404(status) checks status == 404.
        # This test verifies the integer status path works.
        original = self.mod._gh_api
        def mock_gh(method, path, body=None):
            return 404, 'HTTP/1.1 404 Not Found\n\n{"message": "Not Found"}'
        self.mod._gh_api = mock_gh
        try:
            result = self.mod._remove_from_github_org('ghostuser', 'neohiro')
            self.assertTrue(result)
        finally:
            self.mod._gh_api = original


class TestBanctlIntegration(unittest.TestCase):
    """Integration tests for cmd_add with a mock gh CLI.

    These tests verify the cross-component contract between:
      1. _determine_seq → atomic write (ban_list.yaml)
      2. _gh_api → org removal → audit emission
      3. _emit_audit → JSONL audit log

    They run with a real temp filesystem and a patched _gh_api, without
    requiring a live GitHub token.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_banctl()
        cls.tmp = Path(tempfile.mkdtemp(prefix='banctl-integration-'))
        os.environ['NEOHIRO_SHARED_ROOT'] = str(cls.tmp)

        ban_path = cls.tmp / 'brain' / 'audit'
        audit_path = cls.tmp / 'heart' / 'audit'
        ban_path.mkdir(parents=True, exist_ok=True)
        audit_path.mkdir(parents=True, exist_ok=True)

        def _ban_list_path():
            return ban_path / 'ban_list.yaml'

        def _audit_log_path():
            return audit_path / 'ban_enforcement.jsonl'

        def _instant_error_path(ban_id):
            p = cls.tmp / 'heart' / 'audit' / 'instant' / f'ban-{ban_id}.yaml'
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        cls.mod._ban_list_path = staticmethod(_ban_list_path)
        cls.mod._audit_log_path = staticmethod(_audit_log_path)
        cls.mod._instant_error_path = staticmethod(_instant_error_path)

    @classmethod
    def tearDownClass(cls):
        if cls.tmp.is_dir():
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_add_github_login_emits_audit_on_404(self):
        """Mock gh returning 404 (user not a member): ban is still written, audit emitted."""
        import argparse, json

        original = self.mod._gh_api
        def mock_gh(method, path, body=None):
            return 404, 'HTTP/1.1 404 Not Found\n\n{"message": "Not Found"}'
        self.mod._gh_api = mock_gh

        try:
            args = argparse.Namespace(
                identifier='github_login',
                value='notamember',
                reason='abuse',
                scope='all',
                expires=None,
                force=False,
                seq=1,
            )
            ret = self.mod.cmd_add(args)
            self.assertEqual(ret, 0)

            ban_path = self.mod._ban_list_path()
            self.assertTrue(ban_path.exists(), 'ban_list.yaml should be written')

            audit_path = self.mod._audit_log_path()
            self.assertTrue(audit_path.exists(), 'audit log should be written')
            audit_lines = [l for l in audit_path.read_text(encoding='utf-8').strip().split('\n') if l]
            self.assertGreaterEqual(len(audit_lines), 1, 'at least one audit entry')
            entries = [json.loads(line) for line in audit_lines if line.strip()]
            actions = {e['action'] for e in entries}
            self.assertIn('added', actions, 'ban_list added action must be in audit')
            instant_dir = self.tmp / 'heart' / 'audit' / 'instant'
            self.assertFalse(
                list(instant_dir.glob('ban-*.yaml')),
                'no instant error should be written when gh returns 404 (not a member is not an error)'
            )
        finally:
            self.mod._gh_api = original

    def test_add_github_login_fails_gh_produces_instant_error(self):
        """Mock gh returning 500: ban is written, instant error emitted, audit logged."""
        import argparse, json

        original = self.mod._gh_api
        def mock_gh(method, path, body=None):
            return 500, 'HTTP/1.1 500 Server Error'
        self.mod._gh_api = mock_gh

        try:
            args = argparse.Namespace(
                identifier='github_login',
                value='alwaysfails',
                reason='abuse',
                scope='all',
                expires=None,
                force=False,
                seq=2,
            )
            ret = self.mod.cmd_add(args)
            self.assertEqual(ret, 1, 'cmd_add should return 1 when gh fails')

            ban_path = self.mod._ban_list_path()
            self.assertTrue(ban_path.exists(), 'ban_list.yaml must be written even on gh failure')

            instant_dir = self.tmp / 'heart' / 'audit' / 'instant'
            error_files = list(instant_dir.glob('ban-*.yaml'))
            self.assertEqual(len(error_files), 1, 'one instant error file should be written')
        finally:
            self.mod._gh_api = original


class TestBanctlRobustness(unittest.TestCase):
    """Regression tests for self-improvement pass 1/2.

    Verifies:
      - cmd_add/list/remove die() with a clear message on BanListError
        (previously: unhandled exception crashed after disk write).
      - _emit_audit / _emit_instant_error are best-effort: a write
        failure is logged to stderr but does NOT propagate.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_banctl()
        cls.tmp = Path(tempfile.mkdtemp(prefix='banctl-robust-'))
        os.environ['NEOHIRO_SHARED_ROOT'] = str(cls.tmp)
        cls.ban_path = cls.tmp / 'brain' / 'audit' / 'ban_list.yaml'
        cls.ban_path.parent.mkdir(parents=True, exist_ok=True)

        def _ban_list_path():
            return cls.ban_path

        cls.mod._ban_list_path = staticmethod(_ban_list_path)

    @classmethod
    def tearDownClass(cls):
        if cls.tmp.is_dir():
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cmd_add_dies_cleanly_on_malformed_yaml(self):
        """cmd_add must die() with a clear message, not crash with
        an uncaught BanListError, when ban_list.yaml is malformed."""
        self.ban_path.write_text('not: valid: yaml: [unterminated', encoding='utf-8')
        import argparse
        args = argparse.Namespace(
            identifier='email_hash',
            value='5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8',
            reason='abuse',
            scope='all',
            expires=None,
            force=False,
            seq=1,
        )
        with self.assertRaises(SystemExit) as cm:
            self.mod.cmd_add(args)
        self.assertEqual(cm.exception.code, 1)

    def test_cmd_list_dies_cleanly_on_malformed_yaml(self):
        self.ban_path.write_text('not: valid: yaml: [unterminated', encoding='utf-8')
        import argparse
        args = argparse.Namespace(format='text')
        with self.assertRaises(SystemExit) as cm:
            self.mod.cmd_list(args)
        self.assertEqual(cm.exception.code, 1)

    def test_cmd_remove_dies_cleanly_on_malformed_yaml(self):
        self.ban_path.write_text('not: valid: yaml: [unterminated', encoding='utf-8')
        import argparse
        args = argparse.Namespace(ban_id='ban-2026-08-30-001')
        with self.assertRaises(SystemExit) as cm:
            self.mod.cmd_remove(args)
        self.assertEqual(cm.exception.code, 1)

    def test_emit_audit_swallows_oserror(self):
        """A read-only audit log directory should NOT crash cmd_add."""
        import argparse

        # Reset ban_list to a valid empty state — the previous robustness
        # test left a malformed YAML that would die in _parse_ban_list.
        self.ban_path.write_text(
            "schema_version: 1\nlast_updated: 2026-08-30T00:00:00Z\nbans: []\n",
            encoding='utf-8',
        )

        blocked_audit = self.tmp / 'blocked_audit_path'
        blocked_audit.write_text('i am a file, not a dir', encoding='utf-8')

        def _blocked_audit_path():
            return blocked_audit / 'ban_enforcement.jsonl'

        orig_audit = self.mod._audit_log_path
        self.mod._audit_log_path = staticmethod(_blocked_audit_path)
        orig_seq = self.mod._determine_seq
        self.mod._determine_seq = lambda: 42

        try:
            args = argparse.Namespace(
                identifier='email_hash',
                value='5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8',
                reason='abuse',
                scope='all',
                expires=None,
                force=False,
                seq=42,
            )
            ret = self.mod.cmd_add(args)
            self.assertEqual(ret, 0, 'cmd_add must succeed even when audit log is unwritable')
            self.assertTrue(self.ban_path.exists(), 'ban_list.yaml must be written')
        finally:
            self.mod._audit_log_path = orig_audit
            self.mod._determine_seq = orig_seq


if __name__ == '__main__':
    unittest.main(verbosity=2)
