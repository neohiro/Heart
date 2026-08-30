#!/usr/bin/env python3
"""
heartctl — control interface for the Heart cadence engine.

All tools in one file, flag-driven. Designed to be copied into a GitHub
Actions workflow, run locally, or used in CI.

Usage:
    python heartctl.py <command> [flags]

Commands:
    status              — show current heartbeat state (mode, last run, health, repos)
    mode                — get or set the current cadence mode
    repos               — list all known repos from Brain/_entities + repos.yaml
    audit               — show recent audit entries
    health              — show latest health metrics
    phase               — run a single phase and print JSON result
    trigger             — trigger a single Heart cycle (calls heart.py --once)
    watch               — tail the audit log in real time
    doctor              — run neohiro-doctor checks and print report
    env-check           — verify all required environment variables are set
    visitor-counters    — run a single visitor_counter_scraper cycle
    social-counters     — run a single social_counter_poll cycle

Environment:
    BRAIN_PATH              Root of /Brain (default: /brain)
    GH_TOKEN                GitHub PAT
    HEART_LOG_LEVEL         debug|info|warn|error
    NEWS_PATH               Root of neohiro/news (default: /news)
    CC_PATH                 Root of frenzypenguin-media/Content-Creator (default: /content-creator)
    NEOHIRO_SHARED_ROOT     Root of /shared (default: /shared)
    NEOHIRO_LINKS_SECRET    Path to links-secret YAML (default: /links-secret/<file>)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        log_msg = f"yaml parse error in {path}"
        print(f"warning: {log_msg}", file=sys.stderr)
        return {}
    except OSError as e:
        print(f"warning: read error on {path}: {e}", file=sys.stderr)
        return {}


def cmd_status(_args: argparse.Namespace) -> int:
    mode_file = BRAIN_PATH / "heartbeat" / "mode.yaml"
    last_run = BRAIN_PATH / "heartbeat" / "last_run.yaml"
    health = BRAIN_PATH / "heartbeat" / "health.yaml"
    repo_summary = BRAIN_PATH / "heartbeat" / "repo_summary.json"

    print("=== Heart Status ===")
    print(f"BRAIN_PATH : {BRAIN_PATH}")
    print(f"mode       : {_read_yaml(mode_file).get('mode', 'unknown')}")
    print(f"last_run   : {_read_yaml(last_run)}")
    print(f"health     : {_read_yaml(health)}")
    if repo_summary.is_file():
        summary = json.loads(repo_summary.read_text())
        print(f"cycle      : {summary.get('cycle', '?')}")
        print(f"repos      : {len(summary.get('repos', []))}")
        print(f"entities   : {summary.get('entities', [])}")
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    mode_file = BRAIN_PATH / "heartbeat" / "mode.yaml"
    mode_file.parent.mkdir(parents=True, exist_ok=True)
    if args.mode_value:
        mode_file.write_text(f"mode: {args.mode_value}\n")
        print(f"mode set to: {args.mode_value}")
        return 0
    current = _read_yaml(mode_file).get("mode", "normal")
    print(current)
    return 0


def cmd_repos(_args: argparse.Namespace) -> int:
    from heart import _discover_orgs_from_entities, _load_repos_yaml

    from_entities = _discover_orgs_from_entities()
    from_yaml = _load_repos_yaml()
    seen: set[tuple[str, str]] = {(r.org, r.repo) for r in from_entities}
    for r in from_yaml:
        if (r.org, r.repo) not in seen:
            from_entities.append(r)
    print(f"{'ORG':<12} {'REPO':<40} {'ENTITY'}")
    print("-" * 80)
    for r in sorted(from_entities, key=lambda x: (x.org, x.repo)):
        print(f"{r.org:<12} {r.repo:<40} {r.entity}")
    print(f"\nTotal: {len(from_entities)} repos across {len(set(r.org for r in from_entities))} orgs")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    audit_file = BRAIN_PATH / "audit" / "heartbeat.yaml"
    if not audit_file.is_file():
        print("no audit entries")
        return 0
    raw = audit_file.read_text()
    if not raw.strip():
        print("no audit entries")
        return 0
    # Audit entries are separated by blank lines. Group by entry, walk
    # backwards from the end, return the last N complete entries. This is
    # immune to the varying number of lines per entry (error/repos_touched
    # are optional).
    entries = [e for e in raw.split("\n\n") if e.strip()]
    n = args.lines or 20
    tail = entries[-n:]
    print("\n\n".join(tail))
    if not tail:
        return 0
    return 0


def cmd_health(_args: argparse.Namespace) -> int:
    health_file = BRAIN_PATH / "heartbeat" / "health.yaml"
    data = _read_yaml(health_file)
    if not data:
        print("no health data")
        return 1
    print(json.dumps(data, indent=2))
    return 0


def cmd_phase(args: argparse.Namespace) -> int:
    import traceback
    import heart as _heart_module
    _heart_module.BRAIN_PATH = BRAIN_PATH

    state = _heart_module.CycleState()
    state.repos = _heart_module._discover_orgs_from_entities()
    state.repos.extend(_heart_module._load_repos_yaml())

    phase_map = {
        "discover_repos": _heart_module._phase_discover_repos,
        "fetch_repos": _heart_module._phase_fetch_repos,
        "fetch_issues": _heart_module._phase_fetch_issues,
        "fetch_prs": _heart_module._phase_fetch_prs,
        "fetch_actions": _heart_module._phase_fetch_actions,
        "ingest_news": _heart_module._phase_ingest_news,
        "ingest_content": _heart_module._phase_ingest_content,
        "ingest_osint": _heart_module._phase_ingest_osint,
        "osint_userdata": _heart_module._phase_osint_userdata,
        "compute_health": _heart_module._phase_compute_health,
        "write_brain": _heart_module._phase_write_brain,
        "fire_reminders": _heart_module._phase_fire_reminders,
        "prune_stale": _heart_module._phase_prune_stale,
        "self_heal": _heart_module._phase_self_heal,
        "audit": _heart_module._phase_audit,
    }

    if args.phase_name not in phase_map:
        print(f"unknown phase: {args.phase_name}")
        print(f"available: {', '.join(sorted(phase_map.keys()))}")
        return 1
    try:
        result = phase_map[args.phase_name](state)
        print(json.dumps(
            {"phase": result.name, "ok": result.ok, "elapsed_ms": result.elapsed_ms,
             "error": result.error, "repos_touched": result.repos_touched},
            indent=2))
    except Exception as e:
        print(json.dumps(
            {"phase": args.phase_name, "ok": False,
             "error": f"{type(e).__name__}: {e}",
             "traceback": traceback.format_exc()}))
        return 1
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    script = Path(__file__).parent / "heart.py"
    cmd = [sys.executable, str(script), "--once"]
    if args.brain_path:
        cmd.extend(["--brain-path", args.brain_path])
    if args.dry_run:
        cmd.append("--dry-run")
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, env={**os.environ, "BRAIN_PATH": args.brain_path or str(BRAIN_PATH)})
    return result.returncode


def cmd_watch(args: argparse.Namespace) -> int:
    audit_file = BRAIN_PATH / "audit" / "heartbeat.yaml"
    if not audit_file.is_file():
        print("audit file not found, waiting...")
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        audit_file.touch()

    with open(audit_file) as f:
        f.seek(0, 2)
        print(f"Watching {audit_file} — Ctrl+C to stop")
        while True:
            line = f.readline()
            if not line:
                time.sleep(1)
                continue
            print(line, end="")


def cmd_doctor(_args: argparse.Namespace) -> int:
    print("=== neohiro-doctor checks ===")
    checks = [
        ("brain_path_exists", lambda: BRAIN_PATH.exists()),
        ("heartbeat_dir", lambda: (BRAIN_PATH / "heartbeat").is_dir()),
        ("entities_dir", lambda: (BRAIN_PATH / "_entities").is_dir()),
        ("audit_dir", lambda: (BRAIN_PATH / "audit").is_dir()),
        ("mode_yaml", lambda: (BRAIN_PATH / "heartbeat" / "mode.yaml").is_file()),
        ("health_yaml", lambda: (BRAIN_PATH / "heartbeat" / "health.yaml").is_file()),
    ]
    all_ok = True
    for name, fn in checks:
        try:
            ok = fn()
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {name}")
            if not ok:
                all_ok = False
        except Exception as e:
            print(f"  [ERROR] {name}: {e}")
            all_ok = False
    print(f"\n{'All checks passed' if all_ok else 'Some checks failed'}")
    return 0 if all_ok else 1


def cmd_env_check(_args: argparse.Namespace) -> int:
    required = ["BRAIN_PATH"]
    optional = ["GH_TOKEN", "HEART_LOG_LEVEL", "NEWS_PATH", "CC_PATH"]
    print("=== Environment Check ===")
    all_ok = True
    for k in required:
        v = os.environ.get(k)
        status = "OK" if v else "MISSING"
        print(f"  [{status}] {k}={v or '(not set)'}")
        if not v:
            all_ok = False
    print("\n  [INFO] Optional vars:")
    for k in optional:
        v = os.environ.get(k)
        print(f"  [{('OK' if v else ' unset')}] {k}={v or '(not set)'}")
    return 0 if all_ok else 1


def _run_scopecmd(scope: str) -> int:
    """Run a single populator-script cycle and propagate its return code."""
    script_dir = Path(__file__).parent
    candidates = {
        "visitor-counters": script_dir / "visitor_counter_scraper.py",
        "social-counters":  script_dir / "social_counter_poll.py",
    }
    if scope not in candidates:
        print(f"unknown scope: {scope}", file=sys.stderr)
        return 2
    script = candidates[scope]
    if not script.is_file():
        print(f"script not found: {script}", file=sys.stderr)
        return 3
    cmd = [sys.executable, str(script), "--quiet", "--once"]
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, env=os.environ.copy())
    return result.returncode


def cmd_visitor_counters(_args: argparse.Namespace) -> int:
    """Run one visitor_counter_scraper.py cycle and print the result."""
    print("=== Heart visitor-counter scope ===")
    return _run_scopecmd("visitor-counters")


def cmd_social_counters(_args: argparse.Namespace) -> int:
    """Run one social_counter_poll.py cycle and print the result."""
    print("=== Heart social-counter scope ===")
    return _run_scopecmd("social-counters")


def main() -> int:
    parser = argparse.ArgumentParser(description="heartctl — Heart cadence engine control interface")
    parser.add_argument("--brain-path", default=os.environ.get("BRAIN_PATH", "/brain"), help="Root of /Brain")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="show heartbeat state")

    m = sub.add_parser("mode", help="get or set cadence mode")
    m.add_argument("mode_value", nargs="?", choices=["dormant", "normal", "active", "sports"], help="mode to set")

    sub.add_parser("repos", help="list all known repos")

    a = sub.add_parser("audit", help="show recent audit entries")
    a.add_argument("--lines", type=int, default=20)

    sub.add_parser("health", help="show latest health metrics")

    ph = sub.add_parser("phase", help="run a single phase and print JSON result")
    ph.add_argument("phase_name", help="phase to run (e.g. discover_repos)")

    t = sub.add_parser("trigger", help="trigger a single Heart cycle")
    t.add_argument("--dry-run", action="store_true")

    sub.add_parser("watch", help="tail the audit log in real time")

    sub.add_parser("doctor", help="run neohiro-doctor checks")

    sub.add_parser("env-check", help="verify environment variables")

    sub.add_parser(
        "visitor-counters",
        help="run one visitor_counter_scraper.py cycle (see Heart/schedules/REGISTRY.yaml)",
    )
    sub.add_parser(
        "social-counters",
        help="run one social_counter_poll.py cycle (see Heart/schedules/REGISTRY.yaml)",
    )

    args = parser.parse_args()
    global BRAIN_PATH
    BRAIN_PATH = Path(args.brain_path)

    import heart as _heart_module
    _heart_module.BRAIN_PATH = BRAIN_PATH

    commands = {
        "status": cmd_status,
        "mode": cmd_mode,
        "repos": cmd_repos,
        "audit": cmd_audit,
        "health": cmd_health,
        "phase": cmd_phase,
        "trigger": cmd_trigger,
        "watch": cmd_watch,
        "doctor": cmd_doctor,
        "env-check": cmd_env_check,
        "visitor-counters": cmd_visitor_counters,
        "social-counters": cmd_social_counters,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
