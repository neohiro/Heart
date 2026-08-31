"""
test_brain_device_agency_heart.py — Heart/tools/brain_device_agency_heart.py tests.

Run: python -m pytest Heart/tests/test_brain_device_agency_heart.py -v
(conftest.py already sets up sys.path for Heart.tools.* imports)
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import Heart.tools.brain_device_agency_heart as bda_h


@pytest.fixture(autouse=True)
def fresh_module(tmp_path, monkeypatch):
    for m in list(__import__("sys").modules.keys()):
        if "brain_device_agency" in m:
            del __import__("sys").modules[m]
    bda_h._MANIFEST = None
    bda_h._MANIFEST_LOADED_AT = 0.0
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "brain"))
    monkeypatch.setenv("NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR", str(tmp_path / "feedback"))
    monkeypatch.setenv("NEOHIRO_BRAIN_DEVICE_RETRO_DIR", str(tmp_path / "retro"))
    monkeypatch.setenv("NEOHIRO_BRAIN_DEVICE_TOOLSET_PATH", str(tmp_path / "toolset.yaml"))
    yield
    bda_h._MANIFEST = None
    bda_h._MANIFEST_LOADED_AT = 0.0


# ── helpers ───────────────────────────────────────────────────────────────────

def write_toolset(tmp_path):
    p = tmp_path / "toolset.yaml"
    p.write_text("""\
---
schema_version: 1
enabled: true
tools:
  - name: linux_exec
    description: shell
    transport: opencode_exec
    roles: [dev, admin, godadmin]
  - name: service_install
    description: install
    transport: opencode_sudo
    roles: [admin, godadmin]
""", encoding="utf-8")
    os.environ["NEOHIRO_BRAIN_DEVICE_TOOLSET_PATH"] = str(p)


def write_feedback(feedback_dir: Path, role: str, login: str,
                   action: str, trace: dict) -> Path:
    fb_id = f"2026-08-31T12-00-00-{login}"
    p = feedback_dir / f"{fb_id}.yaml"
    import yaml
    data = {
        "schema_version": 1,
        "ts": "2026-08-31T12:00:00Z",
        "role": role,
        "login": login,
        "action": action,
        "trace": trace,
        "manifest_origin": "test",
    }
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


# ── TestRunRetrospective ───────────────────────────────────────────────────────

class TestRunRetrospective:
    def test_install_service_success(self):
        fb = {
            "action": "install_service",
            "role": "admin",
            "login": "wout",
            "trace": {"tool": "service_install", "args": {"package": "fail2ban"}, "exit_code": 0},
        }
        retro = bda_h._run_retrospective(fb)
        assert any("fail2ban" in w and "successfully" in w for w in retro["what_went_well"])
        # evidence contains systemctl + actor context
        all_evidence = " ".join(retro["evidence"])
        assert "systemctl" in all_evidence
        assert "actor: wout" in all_evidence

    def test_install_service_failure_evidence_includes_journalctl(self):
        fb = {
            "action": "install_service",
            "role": "admin",
            "login": "wout",
            "trace": {"tool": "service_install", "args": {"package": "fail2ban"}, "exit_code": 1},
        }
        retro = bda_h._run_retrospective(fb)
        all_evidence = " ".join(retro["evidence"])
        assert "journalctl" in all_evidence

    def test_install_service_failure(self):
        fb = {
            "action": "install_service",
            "role": "dev",
            "login": "alice",
            "trace": {"tool": "service_install", "args": {"package": "nginx"}, "exit_code": 1},
        }
        retro = bda_h._run_retrospective(fb)
        assert any("failed" in d and "nginx" in d for d in retro["what_didnt"])

    def test_linux_exec_success(self):
        fb = {
            "action": "linux_exec",
            "role": "dev",
            "login": "wout",
            "trace": {"tool": "linux_exec", "args": {"command": "ls /shared"}, "exit_code": 0, "duration_ms": 150},
        }
        retro = bda_h._run_retrospective(fb)
        assert any("150ms" in w for w in retro["what_went_well"])

    def test_gh_query_failure(self):
        fb = {
            "action": "gh_query",
            "role": "dev",
            "login": "bob",
            "trace": {"tool": "gh_query", "args": {"query": "repos"}, "exit_code": 1},
        }
        retro = bda_h._run_retrospective(fb)
        assert any("gh_query failed" in d for d in retro["what_didnt"])

    def test_unknown_action_generic_went_well(self):
        fb = {"action": "unknown_action", "role": "godadmin", "login": "wout", "trace": {}}
        retro = bda_h._run_retrospective(fb)
        assert any("without errors" in w for w in retro["what_went_well"])


# ── TestRunIntrospective ──────────────────────────────────────────────────────

class TestRunIntrospective:
    def test_returns_structure(self, tmp_path):
        intro = bda_h._run_introspective()
        assert "self_aware" in intro
        assert "awareness_gaps" in intro
        assert isinstance(intro["awareness_gaps"], list)

    def test_missing_last_run_not_self_aware(self, tmp_path):
        intro = bda_h._run_introspective()
        assert intro["self_aware"] is False
        assert "last_run.yaml missing" in intro["awareness_gaps"]

    def test_grounding_rate_low_adds_gap(self, tmp_path, monkeypatch):
        brain = tmp_path / "brain"
        brain.mkdir(parents=True)
        # The introspective phase now reads from NEOHIRO_SHARED_ROOT
        # (same convention as Heart/tools/grounding.py).
        public = tmp_path / "public" / "health"
        public.mkdir(parents=True)
        (public / "grounding.json").write_text(
            json.dumps({"grounding_rate": 0.80}), encoding="utf-8")
        monkeypatch.setenv("BRAIN_PATH", str(brain))
        monkeypatch.setenv("NEOHIRO_SHARED_ROOT", str(tmp_path))
        intro = bda_h._run_introspective()
        assert intro["self_aware"] is False
        assert any("0.80" in g for g in intro["awareness_gaps"])


# ── TestCrossPrivilege ────────────────────────────────────────────────────────

class TestCrossPrivilege:
    def test_dev_service_install_is_cross(self):
        fb = {"role": "dev", "action": "install_service", "login": "alice"}
        assert bda_h._is_cross_privilege(fb) is True

    def test_dev_linux_exec_not_cross(self):
        fb = {"role": "dev", "action": "linux_exec", "login": "alice"}
        assert bda_h._is_cross_privilege(fb) is False

    def test_admin_service_install_not_cross(self):
        fb = {"role": "admin", "action": "install_service", "login": "wout"}
        assert bda_h._is_cross_privilege(fb) is False

    def test_godadmin_not_cross(self):
        fb = {"role": "godadmin", "action": "install_service", "login": "wout"}
        assert bda_h._is_cross_privilege(fb) is False


# ── TestRunOnce ───────────────────────────────────────────────────────────────

class TestRunOnce:
    def test_empty_feedback_dir_returns_ok(self, tmp_path):
        result = bda_h.run_once()
        assert result["ok"] is True
        assert result["processed"] == 0
        assert result["errors"] == 0

    def test_processes_feedback_file(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "dev", "wout", "linux_exec",
                       {"tool": "linux_exec", "args": {"command": "ls"}, "exit_code": 0})

        result = bda_h.run_once()

        assert result["ok"] is True
        assert result["processed"] == 1
        assert result["errors"] == 0

        retro_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_RETRO_DIR"])
        assert len(list(retro_dir.glob("*.yaml"))) == 1

        # Feedback file should now have a processed marker sibling.
        marker = feedback_dir / ".2026-08-31T12-00-00-wout.processed"
        assert marker.is_file()

    def test_marks_as_processed(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "godadmin", "wout", "gh_query",
                       {"tool": "gh_query", "exit_code": 0})

        bda_h.run_once()

        marker = feedback_dir / ".2026-08-31T12-00-00-wout.processed"
        assert marker.is_file()

    def test_second_run_skips_processed(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "dev", "alice", "linux_exec",
                       {"exit_code": 0})
        marker = feedback_dir / ".2026-08-31T12-00-00-alice.processed"
        marker.write_text("2026-08-31T12:05:00Z", encoding="utf-8")

        result = bda_h.run_once()

        assert result["processed"] == 0  # skipped already-processed

    def test_writes_self_improvement_audit(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        # service_install failure → triggers self_improvement_actions
        write_feedback(feedback_dir, "dev", "wout", "install_service",
                       {"exit_code": 1})

        result = bda_h.run_once()

        audit_file = Path(os.environ["BRAIN_PATH"]) / "audit" / "self_improvement.yaml"
        assert audit_file.is_file()
        lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[0])
        assert entry["source"] == "brain_device_agency_heart"
        assert entry["action"] == "install_service"
        assert len(entry["self_improvement_actions"]) > 0
        assert any("install_service" in a.lower() or "failed" in a.lower()
                   for a in entry["self_improvement_actions"])

    def test_cross_privilege_pokes_godadmin(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "dev", "alice", "install_service",
                       {"exit_code": 0})

        result = bda_h.run_once()

        assert result["processed"] == 1
        poke_queue = Path(os.environ["BRAIN_PATH"]) / "heartbeat" / "poke_queue"
        pokes = list(poke_queue.glob("godadmin-poke-*.yaml"))
        assert len(pokes) == 1

        import yaml
        poke = yaml.safe_load(pokes[0].read_text(encoding="utf-8"))
        assert poke["kind"] == "brain_device_agency"
        assert poke["actor_login"] == "alice"
        assert poke["actor_role"] == "dev"

    def test_service_install_calls_doctor(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "admin", "wout", "install_service",
                       {"exit_code": 0})

        # Monitor script doesn't exist in the test workspace; mock it.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = bda_h.run_once()

        assert result["processed"] == 1
        assert result["results"][0]["doctor_called"] is True
        mock_run.assert_called_once()

    def test_unknown_action_does_not_call_doctor(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "dev", "bob", "linux_exec",
                       {"exit_code": 0})

        with patch("subprocess.run") as mock_run:
            result = bda_h.run_once()

        assert result["results"][0]["doctor_called"] is False
        mock_run.assert_not_called()

    def test_retro_output_contains_all_fields(self, tmp_path):
        write_toolset(tmp_path)
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "godadmin", "wout", "gh_query",
                       {"exit_code": 0})

        bda_h.run_once()

        retro_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_RETRO_DIR"])
        retro_files = list(retro_dir.glob("*.yaml"))
        assert len(retro_files) == 1

        import yaml
        retro = yaml.safe_load(retro_files[0].read_text(encoding="utf-8"))
        assert retro["schema_version"] == 1
        assert "feedback_id" in retro
        assert "retrospective" in retro
        assert "introspective" in retro
        assert "doctor_called" in retro
        assert "godadmin_notified" in retro
        assert "self_improvement_actions" in retro

    def test_bad_yaml_skipped_error_counted(self, tmp_path):
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        bad = feedback_dir / "bad-feedback.yaml"
        bad.write_text("{{ invalid yaml {{{", encoding="utf-8")

        result = bda_h.run_once()

        assert result["errors"] == 1
        assert result["processed"] == 0

    def test_dotfile_skipped(self, tmp_path):
        feedback_dir = Path(os.environ["NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR"])
        feedback_dir.mkdir(parents=True)
        skip = feedback_dir / ".already-processed.yaml"
        skip.write_text("---\nts: 2026-08-31T12:00:00Z\n---\n", encoding="utf-8")

        result = bda_h.run_once()

        assert result["processed"] == 0  # dotfile skipped

    def test_unreadable_feedback_dir_returns_ok_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR", "/nonexistent/path/feedback")
        result = bda_h.run_once()
        assert result["ok"] is True
        assert result["processed"] == 0

    def test_batch_limit_respects_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEART_BRAIN_DEVICE_RETRO_BATCH", "2")
        monkeypatch.setenv("NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR", str(tmp_path / "feedback"))
        feedback_dir = tmp_path / "feedback"
        feedback_dir.mkdir(parents=True)
        # Wipe any leftover files so this test has a clean slate.
        for f in feedback_dir.glob("*.yaml"):
            f.unlink()
        for f in feedback_dir.glob(".processed"):
            f.unlink()
        # Write 5 unprocessed feedback files
        for i in range(5):
            write_feedback(feedback_dir, "dev", f"user{i}", "linux_exec", {"exit_code": 0})

        result = bda_h.run_once()

        # Only 2 should be processed; 3 should be skipped due to batch limit.
        assert result["processed"] == 2
        assert result["skipped_over_batch_limit"] == 3

    def test_batch_limit_default_50(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR", str(tmp_path / "feedback"))
        feedback_dir = tmp_path / "feedback"
        feedback_dir.mkdir(parents=True)
        for f in list(feedback_dir.glob("*.yaml")) + list(feedback_dir.glob(".processed")):
            f.unlink()
        # Write 60 unprocessed feedback files (over default batch of 50).
        for i in range(60):
            write_feedback(feedback_dir, "dev", f"user{i}", "linux_exec", {"exit_code": 0})

        result = bda_h.run_once()

        assert result["processed"] == 50
        assert result["skipped_over_batch_limit"] == 10

    def test_invalid_batch_env_falls_back_to_50(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEART_BRAIN_DEVICE_RETRO_BATCH", "not_a_number")
        monkeypatch.setenv("NEOHIRO_BRAIN_DEVICE_FEEDBACK_DIR", str(tmp_path / "feedback"))
        feedback_dir = tmp_path / "feedback"
        feedback_dir.mkdir(parents=True)
        write_feedback(feedback_dir, "dev", "alice", "linux_exec", {"exit_code": 0})

        result = bda_h.run_once()
        assert result["processed"] == 1
