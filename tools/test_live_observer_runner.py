#!/usr/bin/env python3
"""
test_live_observer_runner.py — unit tests for Heart/tools/live_observer_runner.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = ROOT / "Heart" / "tools" / "live_observer_runner.py"


_load_count = 0


def _load(name: str | None = None) -> object:
    global _load_count
    _load_count += 1
    mod_name = name or f"live_observer_runner_{_load_count}"
    spec = importlib.util.spec_from_file_location(mod_name, str(TOOL_PATH))
    mod = importlib.util.module_from_spec(spec)
    for _p in (str(TOOL_PATH.parent), str(ROOT / "Brain" / "src")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    spec.loader.exec_module(mod)
    return mod


class TestDiscoverOrgs(unittest.TestCase):
    maxDiff = 2048

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="liverunner-test-"))
        self._root = self.tmp / "repos"
        self._brain = self.tmp / "brain"
        self._root.mkdir(parents=True, exist_ok=True)
        self._brain.mkdir(parents=True, exist_ok=True)
        (self._brain / "_entities").mkdir(parents=True, exist_ok=True)
        os.environ["BRAIN_PATH"] = str(self._brain)
        os.environ["NEOHIRO_REPO_ROOT"] = str(self._root)
        os.environ["NEOHIRO_SHARED_ROOT"] = str(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_entity(self, name: str, github_org: str, repos: list[str]) -> None:
        brain_ents = self._brain / "_entities"
        content = f"""---
github_org: {github_org}
repos: {json.dumps(repos)}
---
# body
"""
        (brain_ents / name).with_suffix(".md").write_text(content, encoding="utf-8")

    def _mk_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "README.md").write_text("# test", encoding="utf-8")

    def test_discovers_single_org_repos(self):
        self._write_entity("org-neohiro", "neohiro", ["LLM", "Heart"])
        self._mk_repo(self._root / "LLM")
        self._mk_repo(self._root / "Heart")
        mod = _load()
        roots = mod._discover_orgs()
        scopes = sorted(roots.keys())
        self.assertEqual(scopes, ["neohiro-Heart", "neohiro-LLM"])
        self.assertEqual(roots["neohiro-LLM"].name, "LLM")
        self.assertEqual(roots["neohiro-Heart"].name, "Heart")

    def test_discovers_multiple_org_entities(self):
        self._write_entity("org-fpm", "frenzypenguin", ["website", "docs"])
        self._write_entity("org-osi", "openstageisland", ["api"])
        self._mk_repo(self._root / "website")
        self._mk_repo(self._root / "docs")
        self._mk_repo(self._root / "api")
        mod = _load()
        roots = mod._discover_orgs()
        scopes = sorted(roots.keys())
        self.assertEqual(scopes, ["frenzypenguin-docs", "frenzypenguin-website", "openstageisland-api"])

    def test_skips_missing_local_repos(self):
        self._write_entity("org-neohiro", "neohiro", ["LLM", "GhostRepo"])
        self._mk_repo(self._root / "LLM")
        mod = _load()
        roots = mod._discover_orgs()
        self.assertEqual(list(roots.keys()), ["neohiro-LLM"])

    def test_empty_ents_dir(self):
        mod = _load()
        roots = mod._discover_orgs()
        self.assertEqual(roots, {})

    def test_skips_non_org_md_files(self):
        self._write_entity("user-wout", "neohiro", [])
        (self._brain / "_entities" / "README.md").write_text("# README\n", encoding="utf-8")
        mod = _load()
        roots = mod._discover_orgs()
        self.assertEqual(roots, {})


class TestBuildRootsArg(unittest.TestCase):
    def test_single_root(self):
        mod = _load()
        arg = mod._build_roots_arg({"neohiro-LLM": Path("/repos/LLM")})
        self.assertEqual(arg, f"neohiro-LLM:{Path('/repos/LLM')}")

    def test_multiple_sorted(self):
        mod = _load()
        arg = mod._build_roots_arg({
            "neohiro-LLM": Path("/a/LLM"),
            "neohiro-Heart": Path("/a/Heart"),
        })
        expected = ",".join([
            f"neohiro-Heart:{Path('/a/Heart')}",
            f"neohiro-LLM:{Path('/a/LLM')}",
        ])
        self.assertEqual(arg, expected)


class TestSentinelWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="liverunner-sentinel-"))
        self._watch = self.tmp / "watch"
        self._watch.mkdir(parents=True, exist_ok=True)
        os.environ["NEOHIRO_WATCH_DIR"] = str(self._watch)
        os.environ["NEOHIRO_SHARED_ROOT"] = str(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_valid_sentinel(self):
        mod = _load()
        mod.WATCH_DIR = self._watch
        mod.SENTINEL_PATH = self._watch / "observer.sentinel.json"
        mod._sentinel_write(12345, {"neohiro-LLM": "/repos/LLM"}, ok=True)
        data = json.loads(mod.SENTINEL_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], 12345)
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["roots"], {"neohiro-LLM": "/repos/LLM"})
        self.assertIn("started_at", data)

    def test_sentinel_remove(self):
        mod = _load()
        mod.WATCH_DIR = self._watch
        mod.SENTINEL_PATH = self._watch / "observer.sentinel.json"
        mod.SENTINEL_PATH.write_text("{}", encoding="utf-8")
        mod._sentinel_remove()
        self.assertFalse(mod.SENTINEL_PATH.exists())


class TestSigtermDrain(unittest.TestCase):
    """Tests for SIGTERM handler cleanup and sentinel lifecycle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="liverunner-sigterm-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sigterm_removes_sentinel_and_no_temp_files(self):
        """SIGTERM handler removes sentinel and leaves no temp files."""
        mod = _load()
        mod.WATCH_DIR = self.tmp
        mod.SENTINEL_PATH = self.tmp / "observer.sentinel.json"
        mod._sentinel_write(12345, {"neohiro-LLM": "/repos/LLM"}, ok=True)

        self.assertTrue(mod.SENTINEL_PATH.exists())

        mod._sigterm_handler(signal.SIGTERM, None)

        self.assertFalse(mod.SENTINEL_PATH.exists())
        # mkstemp uses a random suffix (e.g. ".atomic.XXXXXX.tmp") so we glob
        # the directory for any leftover temp files instead of checking a
        # specific name.
        leftover_tmps = list(self.tmp.glob("*.tmp"))
        self.assertEqual(leftover_tmps, [],
                         f"SIGTERM leaked temp files: {leftover_tmps}")

        remaining = list(self.tmp.iterdir())
        self.assertEqual(remaining, [])

    def test_sigterm_idempotent_when_no_sentinel(self):
        """_sigterm_handler is safe to call even if sentinel does not exist."""
        mod = _load()
        mod.WATCH_DIR = self.tmp
        mod.SENTINEL_PATH = self.tmp / "observer.sentinel.json"

        mod._sigterm_handler(signal.SIGTERM, None)

        remaining = list(self.tmp.iterdir())
        self.assertEqual(remaining, [])

    def test_running_flag_cleared_on_sigterm(self):
        """SIGTERM sets _running to False so the watch loop exits."""
        mod = _load()
        mod.WATCH_DIR = self.tmp
        mod.SENTINEL_PATH = self.tmp / "observer.sentinel.json"
        mod._running = True

        mod._sigterm_handler(signal.SIGTERM, None)

        self.assertFalse(mod._running)


if __name__ == "__main__":
    unittest.main()
