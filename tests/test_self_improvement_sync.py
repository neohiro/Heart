"""
test_self_improvement_sync.py — Heart self-improvement sync tests

Run: python -m pytest Heart/tests/test_self_improvement_sync.py -v
(conftest.py already sets up sys.path for Heart.tools.* imports)
"""
import json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

import Heart.tools.self_improvement_sync as sis


@pytest.fixture(autouse=True)
def fresh_module(tmp_path):
    import sys
    # Remove any cached module state
    mods = [k for k in sys.modules if "self_improvement" in k]
    for m in mods:
        del sys.modules[m]
    # Point BRAIN_PATH to isolated temp dir
    sis.BRAIN_PATH = tmp_path / "brain"
    sis.BRAIN_PATH.mkdir(parents=True, exist_ok=True)
    yield
    for m in mods:
        sys.modules.pop(m, None)


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with a stale initial commit (outside the 15-min window)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "neohiro@users.noreply.github.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "neohiro"], check=True)
    (repo / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    # Backdate the initial commit so it's outside the 15-min window
    past_date = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = past_date
    env["GIT_COMMITTER_DATE"] = past_date
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True, env=env)
    return repo


class TestDecideSync:
    def test_no_activity_commits(self, tmp_path):
        # Non-git path — _repo_root returns None, no recent commits found → safe to commit
        d = sis.decide_sync(str(tmp_path / "nowhere"))
        assert d.action == "commit"

    def test_user_identities_returns_global_set(self):
        ids = sis._user_identities()
        assert isinstance(ids, set)
        # We know the global identity from conftest/setup
        assert len(ids) > 0

    def test_manual_force_overrides_cooldown(self, git_repo):
        sis._set_cooldown(str(git_repo), 30, "test")
        d = sis.decide_sync(str(git_repo), manual_request=True)
        assert d.action == "manual_force"

    def test_active_session_blocks(self, git_repo, tmp_path):
        sess_dir = sis.BRAIN_PATH / "heartbeat" / "sessions"
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "active.yaml").write_text(json.dumps({
            "sessions": [{"last_input_at": datetime.now(timezone.utc).isoformat()}]
        }))
        d = sis.decide_sync(str(git_repo))
        assert d.action == "cooldown"
        assert d.active_session is True
        assert "active session" in d.reason.lower()

    def test_recent_user_push_blocks(self, git_repo):
        # Identity already matches _user_identities() (neohiro) — no re-config needed
        (git_repo / "x.md").write_text("x")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "user edit"], check=True)
        d = sis.decide_sync(str(git_repo))
        assert d.action == "cooldown"
        assert d.last_user_push is not None

    def test_other_coder_push_triggers_short_cooldown(self, git_repo):
        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = "Another Coder"
        env["GIT_AUTHOR_EMAIL"] = "other@external.com"
        env["GIT_COMMITTER_NAME"] = "Another Coder"
        env["GIT_COMMITTER_EMAIL"] = "other@external.com"
        (git_repo / "y.md").write_text("y")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "other edit"], check=True, env=env)
        d = sis.decide_sync(str(git_repo))
        assert d.action == "cooldown"
        assert d.last_other_push is not None
        assert "another coder" in d.reason.lower()

    def test_user_push_sets_lock(self, git_repo):
        (git_repo / "z.md").write_text("z")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "user z"], check=True)
        sis.decide_sync(str(git_repo))
        lock = sis._read_lock(str(git_repo))
        assert lock is not None
        assert lock["cooldown_until"] is not None

    def test_manual_false_cooldown_removed(self, git_repo):
        (git_repo / "w.md").write_text("w")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "user w"], check=True)
        sis.decide_sync(str(git_repo), manual_request=False)
        lock = sis._read_lock(str(git_repo))
        assert lock is not None

    def test_cooldown_in_progress_yields_skip(self, git_repo):
        sis._set_cooldown(str(git_repo), 30, "test")
        d = sis.decide_sync(str(git_repo))
        assert d.action == "skip"
        assert d.cooldown_until is not None

    def test_run_phase_emits_audit(self, tmp_path):
        # Use non-git path so there's no recent commit to block the phase
        result = sis.run_phase([str(tmp_path / "non_git_repo")], manual=False)
        assert "decisions" in result
        assert "committable" in result
        assert "blocked" in result
        assert str(tmp_path / "non_git_repo") in result["committable"]

    def test_no_session_file_returns_false(self):
        assert sis.detect_active_session() is False

    def test_nonexistent_path_commits(self):
        d = sis.decide_sync("/nonexistent/path/here")
        assert d.action == "commit"

    def test_user_identities_returns_set(self):
        ids = sis._user_identities()
        assert isinstance(ids, set)

    def test_manual_false_cooldown_removed(self, git_repo):
        subprocess.run(["git", "-C", str(git_repo), "config", "user.name", "wout"], check=True)
        subprocess.run(["git", "-C", str(git_repo), "config", "user.email", "wout@local"], check=True)
        (git_repo / "w.md").write_text("w")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "user w"], check=True)
        sis.decide_sync(str(git_repo), manual_request=False)
        lock = sis._read_lock(str(git_repo))
        assert lock is not None


class TestCooldownHelpers:
    def test_set_and_read_lock(self, tmp_path):
        sis._set_cooldown("my/repo", 5, "testing")
        lock = sis._read_lock("my/repo")
        assert lock is not None
        assert lock["reason"] == "testing"
        assert lock["cooldown_until"] is not None

    def test_is_in_cooldown_false_for_missing(self):
        assert sis._is_in_cooldown("nonexistent") is False

    def test_is_in_cooldown_true_within_window(self, tmp_path):
        from datetime import timedelta
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        sis._write_lock("cooldown_test", {
            "repo": "cooldown_test",
            "cooldown_until": future.isoformat(),
            "reason": "test",
            "set_at": datetime.now(timezone.utc).isoformat(),
        })
        assert sis._is_in_cooldown("cooldown_test") is True

    def test_is_in_cooldown_false_past_window(self, tmp_path):
        from datetime import timedelta
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        sis._write_lock("past_test", {
            "repo": "past_test",
            "cooldown_until": past.isoformat(),
            "reason": "test",
            "set_at": datetime.now(timezone.utc).isoformat(),
        })
        assert sis._is_in_cooldown("past_test") is False


class TestGitHelpers:
    def test_recent_commits_none_for_empty_repo(self, git_repo):
        commits = sis.recent_commits(git_repo, "2020-01-01T00:00:00Z")
        assert isinstance(commits, list)

    def test_recent_commits_finds_user_commit(self, git_repo):
        since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        (git_repo / "a.md").write_text("a")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "user a"], check=True)
        commits = sis.recent_commits(git_repo, since)
        assert len(commits) >= 1
        assert any("user a" in c["subject"] for c in commits)
