#!/usr/bin/env python3
"""Tests for Heart/tools/doctor_sync_heart.py — JSON envelope + arg forwarding."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HEART_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(HEART_ROOT, "tools", "doctor_sync_heart.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("doctor_sync_heart", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHeartDispatcher(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_script_path_default(self):
        # The default is the Docker container path
        self.assertTrue(self.mod.SCRIPT.endswith(
            os.path.join("tools", "sync_doctor_workflow.py")
        ))

    def test_script_path_env_override(self):
        with mock.patch.dict(os.environ, {"NEOHIRO_DOCTOR_ROOT": "/tmp/alt"}):
            mod = _load_module()
            self.assertTrue(mod.SCRIPT.startswith("/tmp/alt"))


class TestJsonEnvelope(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def _invoke(self, rc: int = 0) -> dict:
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh", "--json"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=rc)) as run, \
             redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.mod.main()
        envelope = json.loads(out.getvalue())
        run.assert_called_once()
        return envelope

    def test_envelope_keys(self):
        env = self._invoke()
        for k in (
            "tool", "version", "started_at", "ended_at",
            "duration_seconds", "returncode", "ok", "args", "doctor_root",
        ):
            self.assertIn(k, env, f"missing key: {k}")

    def test_envelope_ok(self):
        env = self._invoke(rc=0)
        self.assertEqual(env["returncode"], 0)
        self.assertTrue(env["ok"])

    def test_envelope_error(self):
        env = self._invoke(rc=2)
        self.assertEqual(env["returncode"], 2)
        self.assertFalse(env["ok"])

    def test_envelope_args_captured(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["dsh", "--json", "--org", "neohiro",
                                "--repo", "foo/bar", "--force", "--dry-run"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)), \
             redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.mod.main()
        env = json.loads(out.getvalue())
        a = env["args"]
        self.assertEqual(a["org"], "neohiro")
        self.assertEqual(a["repo"], "foo/bar")
        self.assertTrue(a["force"])
        self.assertTrue(a["dry_run"])

    def test_envelope_duration_nonnegative(self):
        env = self._invoke()
        self.assertGreaterEqual(env["duration_seconds"], 0)

    def test_iso_timestamps_parseable(self):
        env = self._invoke()
        from datetime import datetime
        # fromisoformat accepts the +00:00 suffix in 3.10+
        datetime.fromisoformat(env["started_at"])
        datetime.fromisoformat(env["ended_at"])


class TestArgForwarding(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_minimal_cmd(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run, \
             redirect_stdout(out):
            self.mod.main()
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "python")
        self.assertTrue(cmd[1].endswith("sync_doctor_workflow.py"))
        self.assertIn("--org", cmd)
        self.assertEqual(cmd[cmd.index("--org") + 1], "all")

    def test_force_flag_forwarded(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh", "--force"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run, \
             redirect_stdout(out):
            self.mod.main()
        self.assertIn("--force", run.call_args[0][0])

    def test_dry_run_flag_forwarded(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh", "--dry-run"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run, \
             redirect_stdout(out):
            self.mod.main()
        self.assertIn("--dry-run", run.call_args[0][0])

    def test_repo_flag_forwarded(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh", "--repo", "x/y"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run, \
             redirect_stdout(out):
            self.mod.main()
        cmd = run.call_args[0][0]
        self.assertIn("--repo", cmd)
        self.assertEqual(cmd[cmd.index("--repo") + 1], "x/y")


class TestGhTokenForward(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_gh_token_becomes_github_token(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch.dict(
                 os.environ,
                 {"GH_TOKEN": "pat123", "PATH": os.environ["PATH"]},
                 clear=True,
             ), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run, \
             redirect_stdout(out):
            self.mod.main()
        env_passed = run.call_args.kwargs.get("env") or run.call_args[1].get("env")
        self.assertEqual(env_passed["GITHUB_TOKEN"], "pat123")
        self.assertEqual(env_passed["GH_TOKEN"], "pat123")

    def test_existing_github_token_preserved(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh"]), \
             mock.patch("os.path.isfile", return_value=True), \
             mock.patch.dict(
                 os.environ,
                 {
                     "GH_TOKEN": "pat123",
                     "GITHUB_TOKEN": "existing",
                     "PATH": os.environ["PATH"],
                 },
                 clear=True,
             ), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0)) as run, \
             redirect_stdout(out):
            self.mod.main()
        env_passed = run.call_args.kwargs.get("env") or run.call_args[1].get("env")
        # Existing GITHUB_TOKEN wins
        self.assertEqual(env_passed["GITHUB_TOKEN"], "existing")


class TestMissingScript(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_missing_script_json_output(self):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh", "--json"]), \
             mock.patch("os.path.isfile", return_value=False), \
             redirect_stdout(out), redirect_stderr(err):
            rc = self.mod.main()
        self.assertEqual(rc, 1)
        env = json.loads(out.getvalue())
        self.assertFalse(env["ok"])
        self.assertIn("not found", env["error"])

    def test_missing_script_human_output(self):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["dsh"]), \
             mock.patch("os.path.isfile", return_value=False), \
             redirect_stdout(out), redirect_stderr(err):
            rc = self.mod.main()
        self.assertEqual(rc, 1)
        self.assertIn("ERROR", err.getvalue())


if __name__ == "__main__":
    unittest.main()
