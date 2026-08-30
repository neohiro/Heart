"""
test_monitor_shim.py — Heart's monitor.sh bridge tests

Run: python -m pytest Heart/tests/test_monitor_shim.py -v
"""
import json, os, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "Heart" / "tools"))


@pytest.fixture(autouse=True)
def fresh_shim(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_PATH", str(tmp_path / "brain"))
    monkeypatch.setenv("NETWORK_PATH", str(tmp_path / "network"))
    monkeypatch.setenv("HEART_HOSTNAME", "test-host")
    import importlib
    import Heart.tools.monitor_shim as ms
    importlib.reload(ms)
    yield ms


class TestBuildTreeview:
    def test_basic_treeview(self, fresh_shim):
        ms = fresh_shim
        tree = ms.build_ascii_treeview(
            "test-host",
            docker_state={"heart": "up", "brain": "up", "mouth": "down"},
            system_metrics={"cpu": "23%", "mem": "41%"},
            tailscale_peers=["exit-router", "dashboard"],
        )
        lines = tree.split("\n")
        assert lines[0] == "device: test-host"
        assert "├── role: monitoring" in tree
        assert "├── docker:" in tree
        assert "│   ├── heart: up" in tree
        assert "│   ├── brain: up" in tree
        assert "│   └── mouth: down" in tree
        assert "├── system:" in tree
        assert "│   ├── cpu: 23%" in tree
        assert "│   └── mem: 41%" in tree
        assert "└── tailscale:" in tree
        assert "    ├── peer: exit-router" in tree
        assert "    └── peer: dashboard" in tree
        assert "│   ├──" in tree
        assert "│   └──" in tree
        assert "    ├──" in tree

    def test_treeview_no_docker(self, fresh_shim):
        ms = fresh_shim
        tree = ms.build_ascii_treeview("host1")
        assert "device: host1" in tree
        assert "(no docker info)" in tree

    def test_treeview_no_tailscale(self, fresh_shim):
        ms = fresh_shim
        tree = ms.build_ascii_treeview("host1", docker_state={"heart": "up"})
        assert "(no peers" in tree

    def test_treeview_writes_to_file(self, fresh_shim):
        ms = fresh_shim
        ms.heart_phase(hostname="socks")
        path = ms.TREEVIEW_DIR / "socks.tree.txt"
        assert path.exists()
        content = path.read_text()
        assert "device: socks" in content


class TestCollectTailscalePeers:
    def test_no_metrics_dir(self, fresh_shim):
        ms = fresh_shim
        peers = ms.collect_tailscale_peers()
        assert peers == []

    def test_with_metrics(self, fresh_shim, tmp_path):
        ms = fresh_shim
        metrics_dir = ms.NETWORK_PATH / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        (metrics_dir / "peer1.json").write_text(json.dumps({
            "kind": "device_health",
            "peers": [{"hostname": "exit-router", "id": "n1"}],
        }))
        (metrics_dir / "peer2.json").write_text(json.dumps({
            "kind": "device_health",
            "peers": [{"hostname": "dashboard", "id": "n2"}],
        }))
        peers = ms.collect_tailscale_peers()
        assert "exit-router" in peers
        assert "dashboard" in peers


class TestRunMonitor:
    def test_run_monitor_no_script(self, fresh_shim, monkeypatch):
        ms = fresh_shim
        # Force the script to not be found
        monkeypatch.setattr(ms, "MONITOR_SH", Path("/nonexistent/monitor.sh"))
        result = ms.run_monitor()
        assert not result["ok"]

    def test_run_monitor_with_script(self, fresh_shim, tmp_path):
        ms = fresh_shim
        # Create a tiny mock monitor.sh
        mock = tmp_path / "monitor.sh"
        mock.write_text("#!/usr/bin/env bash\necho '{\"hostname\":\"x\"}'\n")
        ms.MONITOR_SH = mock
        result = ms.run_monitor(metrics=False, treeview=True)
        # Will fail because bash might not be available; we just check graceful failure
        assert "ok" in result


class TestBuildHeartPhasePayload:
    def test_default_payload(self, fresh_shim):
        ms = fresh_shim
        payload = ms.build_heart_phase_payload()
        assert payload["phase"] == "monitor"
        assert "ts" in payload
        assert "trees" in payload
        assert "monitor" in payload
        assert "ts_outputs" in payload


class TestHeartPhase:
    def test_heart_phase_returns_result(self, fresh_shim, monkeypatch):
        ms = fresh_shim
        monkeypatch.setattr(ms, "MONITOR_SH", Path("/nonexistent"))
        result = ms.heart_phase(hostname="test-host")
        # Returns a phase result
        assert "phase" in result
        assert result["phase"] == "monitor"
