"""
monitor_shim.py — Heart's bridge to neohiro-doctor/monitor.sh

Wraps the monitor.sh script so Heart can:
  - Call it on a cadence (e.g. every 5 minutes)
  - Capture the metrics output (JSON) for inclusion in the abuse_digest
  - Emit an ASCII treeview of the docker/device/node hierarchy to
    /neohiro/network/dashboard/ (consumed by the dashboard refresh)

The monitor.sh script produces two things:
  1. JSON metrics at <output>/metrics.json (from monitor.sh --json)
  2. ASCII treeview on stdout (from monitor.sh --treeview)

Both are captured by this shim and:
  - metrics → /Brain/heartbeat/monitor/<device>_<ts>.json
  - treeview → /network/svg/dashboard/<device>.tree.txt

The treeview combines docker containers (heartbeat) and Tailscale nodes
(/network/metrics) into a single ASCII tree:

    device: <hostname>
    ├── role: <role>
    ├── tag: <acl-tags>
    ├── docker:
    │   ├── heart: <status>
    │   ├── brain: <status>
    │   ├── mouth: <status>
    │   └── ...
    ├── system:
    │   ├── cpu: <pct>%
    │   ├── mem: <pct>%
    │   └── disk: <pct>%
    └── tailscale:
        ├── peer: <node1>
        ├── peer: <node2>
        └── ...

Privacy: this shim never writes raw device hostnames to /network/dashboard
without going through the privacy rules engine. Local surface = OK.
Public surface = ANONYMISED (only the role + status is allowed).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))
NETWORK_PATH = Path(os.environ.get("NETWORK_PATH", "network"))
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MONITOR_SH = REPO_ROOT / "neohiro-doctor" / "monitor.sh"
MONITOR_OUTPUT_DIR = BRAIN_PATH / "heartbeat" / "monitor"
MONITOR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TREEVIEW_DIR = NETWORK_PATH / "svg" / "dashboard"
TREEVIEW_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_HOSTNAME = os.environ.get("HEART_HOSTNAME", "heart-local")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_monitor(
    *,
    hostname: str = DEFAULT_HOSTNAME,
    treeview: bool = True,
    metrics: bool = True,
    timeout: int = 30,
) -> dict:
    """
    Run monitor.sh, capture outputs, and return a phase result dict.
    """
    start = time.time()
    if not (MONITOR_SH.exists() and MONITOR_SH.is_file()):
        return {
            "ok": False,
            "reason": f"monitor.sh not found at {MONITOR_SH}",
            "metrics": None,
            "treeview": None,
        }

    out_dir = MONITOR_OUTPUT_DIR / hostname
    out_dir.mkdir(parents=True, exist_ok=True)

    metrics_file = None
    treeview_text = None

    try:
        # Phase 1: metrics
        if metrics:
            metrics_file = out_dir / f"metrics_{int(time.time())}.json"
            subprocess.run(
                ["bash", str(MONITOR_SH), "--once", "--json", str(metrics_file)],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            if not metrics_file.exists():
                metrics_file = None

        # Phase 2: treeview
        if treeview:
            r = subprocess.run(
                ["bash", str(MONITOR_SH), "--treeview"],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            treeview_text = r.stdout if r.returncode == 0 else None

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return {"ok": False, "reason": str(e), "metrics": None, "treeview": None}

    if treeview_text:
        tree_path = TREEVIEW_DIR / f"{hostname}.tree.txt"
        tree_path.write_text(treeview_text, encoding="utf-8")

    return {
        "ok": True,
        "hostname": hostname,
        "duration_ms": int((time.time() - start) * 1000),
        "metrics_file": str(metrics_file) if metrics_file else None,
        "metrics": _read_metrics(metrics_file) if metrics_file else None,
        "treeview": treeview_text,
        "treeview_path": str(TREEVIEW_DIR / f"{hostname}.tree.txt") if treeview_text else None,
        "ts": _now(),
    }


def _read_metrics(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None


def build_ascii_treeview(
    hostname: str,
    *,
    docker_state: Optional[dict] = None,
    system_metrics: Optional[dict] = None,
    tailscale_peers: Optional[list[str]] = None,
    role: str = "monitoring",
    tags: Optional[list[str]] = None,
) -> str:
    """
    Build an ASCII treeview of a device + its docker instances + tailscale peers.
    Used by the dashboard.

    Example output:
        device: socks
        ├── role: exit-node
        ├── tag: tag:exit-node
        ├── docker:
        │   ├── heart: up
        │   ├── brain: up
        │   ├── mouth: down
        │   └── doctor: up
        ├── system:
        │   ├── cpu: 23%
        │   ├── mem: 41%
        │   └── disk: 62%
        └── tailscale:
            ├── peer: exit-router
            ├── peer: dashboard
            └── peer: brain
    """
    lines = [f"device: {hostname}"]
    lines.append(f"├── role: {role}")
    if tags:
        lines.append(f"├── tag: {', '.join(tags)}")
    else:
        lines.append("├── tag: -")

    # Build the three optional subtrees in fixed order. The decision of
    # which subtree is the *last* in the overall tree is made here, so each
    # subtree only needs to know its own contents.
    has_docker = bool(docker_state)
    has_system = bool(system_metrics)
    has_tailscale = bool(tailscale_peers)

    subtrees = []
    if docker_state is not None:
        subtrees.append(("docker", list(docker_state.items())))
    else:
        subtrees.append(("docker", None))
    if system_metrics is not None:
        subtrees.append(("system", list(system_metrics.items())))
    else:
        subtrees.append(("system", None))
    subtrees.append(("tailscale", tailscale_peers or []))

    n = len(subtrees)
    for idx, (label, items) in enumerate(subtrees):
        is_last_subtree = idx == n - 1
        subtree_prefix = "└── " if is_last_subtree else "├── "
        child_prefix = "    " if is_last_subtree else "│   "

        if not items:
            lines.append(f"{subtree_prefix}{label}: (no {'peers' if label == 'tailscale' else label + ' info'})")
            continue
        lines.append(f"{subtree_prefix}{label}:")
        m = len(items)
        for j, item in enumerate(items):
            child_branch = "└── " if j == m - 1 else "├── "
            if label == "tailscale":
                lines.append(f"{child_prefix}{child_branch}peer: {item}")
            else:
                lines.append(f"{child_prefix}{child_branch}{item[0]}: {item[1]}")

    return "\n".join(lines)


def collect_tailscale_peers() -> list[str]:
    """Read /network/metrics for current tailscale peers."""
    metrics_dir = NETWORK_PATH / "metrics"
    if not metrics_dir.exists():
        return []
    peers: set[str] = set()
    for f in metrics_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("kind") == "device_health":
            for peer in data.get("peers", []) or []:
                peers.add(peer.get("hostname", peer.get("id", "unknown")))
    return sorted(peers)


def build_heart_phase_payload() -> dict:
    """Build the full payload for a Heart cycle."""
    return {
        "phase": "monitor",
        "ts": _now(),
        "trees": {},
        "monitor": None,
        "ts_outputs": [],
    }


def heart_phase(*, hostname: str = DEFAULT_HOSTNAME) -> dict:
    """Run monitor.sh, collect tree, return the phase result."""
    result = run_monitor(hostname=hostname)
    metrics = (result.get("metrics") if result.get("ok") else None) or {}

    # Build a treeview from the metrics (or defaults if monitor.sh unavailable)
    system = {}
    docker_state = {}
    for k, v in metrics.items():
        if k in ("cpu_percent", "mem_percent", "disk_percent", "load", "cpu_speed_mhz", "mem_total_mb"):
            system[k] = v
        elif k.startswith("docker_") or k in ("heart", "brain", "mouth", "doctor", "container_status"):
            docker_state[k] = v

    tree = build_ascii_treeview(
        hostname,
        docker_state=docker_state or None,
        system_metrics=system or None,
        tailscale_peers=collect_tailscale_peers() or None,
        role=os.environ.get("HEART_ROLE", "monitoring"),
        tags=os.environ.get("HEART_TAGS", "tag:monitoring").split(","),
    )

    tree_path = TREEVIEW_DIR / f"{hostname}.tree.txt"
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(tree, encoding="utf-8")

    return {
        "phase": "monitor",
        "ok": result.get("ok", False),
        "hostname": hostname,
        "reason": result.get("reason") if not result.get("ok") else None,
        "metrics_file": result.get("metrics_file"),
        "treeview_path": str(tree_path),
        "treeview": tree,
        "duration_ms": result.get("duration_ms", 0),
    }
