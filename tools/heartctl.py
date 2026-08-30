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
    router              — route a user request to a model via preset
    delegate            — delegate a coding task to the brain node (BRAIN_NODE_OPENCODE_ROUTING.md § 5)

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
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

import yaml

BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding='utf-8')) or {}
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
        try:
            summary = json.loads(repo_summary.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"heartctl: warning: cannot read repo_summary: {e}", file=sys.stderr)
            return 1
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
    print(f"\nTotal: {len(from_entities)} repos across {len({r.org for r in from_entities})} orgs")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    audit_file = BRAIN_PATH / "audit" / "heartbeat.yaml"
    if not audit_file.is_file():
        print("no audit entries")
        return 0
    try:
        raw = audit_file.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as e:
        print(f"heartctl: warning: cannot read audit file {audit_file}: {e}", file=sys.stderr)
        return 1
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
        "self_reflexive_check": _heart_module._phase_self_reflexive_check,
        "intuition_deliberate": _heart_module._phase_intuition_deliberate,
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
    if not script.is_file():
        print(f"heartctl: error: {script} not found", file=sys.stderr)
        return 127
    cmd = [sys.executable, str(script), "--once"]
    if args.brain_path:
        cmd.extend(["--brain-path", args.brain_path])
    if args.dry_run:
        cmd.append("--dry-run")
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        env={**os.environ, "BRAIN_PATH": args.brain_path or str(BRAIN_PATH)},
        check=False,
    )
    return result.returncode


def cmd_watch(args: argparse.Namespace) -> int:
    audit_file = BRAIN_PATH / "audit" / "heartbeat.yaml"
    if not audit_file.is_file():
        print("audit file not found, waiting...")
        audit_file.parent.mkdir(parents=True, exist_ok=True)
        audit_file.touch()

    with open(audit_file, encoding='utf-8') as f:
        f.seek(0, 2)
        print(f"Watching {audit_file} — Ctrl+C to stop")
        try:
            while True:
                line = f.readline()
                if not line:
                    time.sleep(1)
                    continue
                print(line, end="")
        except KeyboardInterrupt:
            return 0


def _session_dir(args: argparse.Namespace) -> Path:
    if not args.session_id:
        die("delegate-watch requires --session-id")
    return _shared_root() / f"brain/opencode/sessions/{args.session_id}"


def cmd_watch_session(args: argparse.Namespace) -> int:
    """Poll a brain-node session directory and print new/changed files.

    Usage: heartctl delegate-watch --session-id <uuid> [--poll-interval 30]
    """
    session_dir = _session_dir(args)
    poll_interval = max(5, getattr(args, 'poll_interval', 30))
    if not session_dir.is_dir():
        print(f"heartctl: error: session dir not found: {session_dir}", file=sys.stderr)
        return 1
    known = {p.name for p in session_dir.iterdir() if p.is_file()}
    print(f"Watching {session_dir} — Ctrl+C to stop", flush=True)
    print(f"Known files: {sorted(known)}", flush=True)
    while True:
        time.sleep(poll_interval)
        try:
            current = {p.name for p in session_dir.iterdir() if p.is_file()}
        except OSError as e:
            print(f"heartctl: warning: {e}", file=sys.stderr)
            continue
        new_files = current - known
        if new_files:
            for name in sorted(new_files):
                mtime = session_dir / name
                try:
                    mt = os.stat(mtime).st_mtime
                    mt_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mt))
                except OSError:
                    mt_str = '?'
                print(f"  + {name}  {mt_str}", flush=True)
            known |= new_files
        known = current


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
    result = subprocess.run(cmd, env=os.environ.copy(), check=False)
    return result.returncode


def cmd_visitor_counters(_args: argparse.Namespace) -> int:
    """Run one visitor_counter_scraper.py cycle and print the result."""
    print("=== Heart visitor-counter scope ===")
    return _run_scopecmd("visitor-counters")


def cmd_social_counters(_args: argparse.Namespace) -> int:
    """Run one social_counter_poll.py cycle and print the result."""
    print("=== Heart social-counter scope ===")
    return _run_scopecmd("social-counters")


# ─── Router (per LLM_ROUTER_CASCADE.md § 2) ────────────────────────────────

VALID_PRESETS = ('coding', 'reasoning', 'fast', 'multimodal', 'tools')


def _repo_root() -> Path:
    """Best-effort repo root detection.

    Tries (in order):
      1. NEOHIRO_REPO_ROOT env var
      2. The parent of the parent of this script's parent (works when the
         layout is <root>/Heart/tools/heartctl.py)
      3. The current working directory if it has LLM/data/presets/
      4. Path('.') as a last resort
    """
    env_root = os.environ.get('NEOHIRO_REPO_ROOT', '').strip()
    if env_root and Path(env_root).is_dir():
        return Path(env_root)
    script_root = Path(__file__).resolve().parent
    # /Heart/tools/heartctl.py → /Heart (1) → / (2) → /Heart (3)
    for ancestor in [script_root.parent.parent, script_root.parent, script_root]:
        if (ancestor / 'LLM' / 'data' / 'presets').is_dir():
            return ancestor
    cwd = Path.cwd()
    if (cwd / 'LLM' / 'data' / 'presets').is_dir():
        return cwd
    return Path('.')


def _presets_dir() -> Path:
    return _repo_root() / 'LLM' / 'data' / 'presets'


def _router_context_dir() -> Path:
    return Path(os.environ.get('NEOHIRO_LLM_ROUTER_DIR', '/shared/heart/heartbeat/router'))


def _load_preset(preset_id: str) -> dict:
    if preset_id not in VALID_PRESETS:
        die(f'unknown preset: {preset_id!r} (valid: {", ".join(VALID_PRESETS)})')
    p = _presets_dir() / f'{preset_id}.yaml'
    if not p.is_file():
        die(f'preset file not found: {p}')
    try:
        return yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    except (yaml.YAMLError, OSError) as e:
        die(f'preset parse error in {p}: {e}')


def _load_golden_model(preset_id: str) -> tuple[str, float]:
    """Pick the best model for this preset from golden_free.yaml.
    Returns (model_id, confidence)."""
    market_root = Path(os.environ.get('NEOHIRO_LLM_MARKET_ROOT', '/shared/brain/knowledge/llm_market'))
    golden_path = market_root / 'golden_free.yaml'
    if not golden_path.is_file():
        return '', 0.0
    try:
        data = yaml.safe_load(golden_path.read_text(encoding='utf-8')) or {}
    except (yaml.YAMLError, OSError):
        return '', 0.0

    preset_caps = set()
    preset = _load_preset(preset_id)
    preset_caps.update(preset.get('capabilities', []))
    preset_caps.update(preset.get('tags', []))

    best = ('', 0.0)
    for src in data.get('sources', []):
        confidence = float(src.get('confidence', 0))
        caps = set(src.get('capability_match', []))
        # If preset needs tool_use and model matches → boost
        if 'tool_use' in preset_caps and 'tool_use' in caps:
            confidence = min(1.0, confidence * 1.1)
        if 'reasoning' in preset_caps and 'reasoning' in caps:
            confidence = min(1.0, confidence * 1.1)
        if confidence > best[1]:
            best = (src.get('model_id', ''), confidence)
    return best


def cmd_router(args: argparse.Namespace) -> int:
    """Select a model for a given preset and write a router context record.

    Per LLM_ROUTER_CASCADE.md § 2.2: writes /shared/heart/heartbeat/router/<ts>.json
    """
    preset = _load_preset(args.preset)
    model_id, confidence = _load_golden_model(args.preset)
    if not model_id:
        prefer_tier = preset.get("prefer_tier", "free-first")
        fallback_tiers = preset.get("fallback_tiers", [])
        model_id = fallback_tiers[0] if fallback_tiers else f"auto:{prefer_tier}"
        confidence = 0.0

    reasoning = (
        f"preset={args.preset} capabilities={preset.get('capabilities', [])} "
        f"tags={preset.get('tags', [])}; selected via golden_free.yaml"
    )

    record = {
        "ts": _iso_now(),
        "preset_id": args.preset,
        "use_case": args.use_case or "",
        "model_id": model_id,
        "confidence": round(confidence, 3),
        "reasoning": reasoning,
        "prefer_tier": preset.get("prefer_tier", "free-first"),
        "fallback_tiers": preset.get("fallback_tiers", []),
    }

    if args.json:
        print(json.dumps(record, indent=2))
        return 0

    if not args.dry_run:
        out_dir = _router_context_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        ts_slug = record["ts"].replace(":", "").replace("-", "")
        out_path = out_dir / f"{ts_slug}.json"
        stage_path = out_path.with_suffix(".json.stage")
        try:
            with open(stage_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(record, indent=2))
                f.flush()
                os.fsync(f.fileno())
            stage_path.replace(out_path)
        except OSError as e:
            print(f"heartctl: warning: cannot write router context {out_path}: {e}", file=sys.stderr)
            return 1

    print(f"router: preset={args.preset} model={model_id} confidence={record['confidence']}")
    if args.use_case:
        print(f"  use-case: {args.use_case}")
    if not args.dry_run:
        print(f"  context record: {out_path}")
    return 0


# ─── Delegate (per BRAIN_NODE_OPENCODE_ROUTING.md § 5) ───────────────────────

def _brain_node_ip() -> str | None:
    """Resolve brain node Tailscale IP via `tailscale status --json`.

    Returns the first peer with "brain-node" in its DNSName, or None if unavailable.
    Falls back to NEOHIRO_BRAIN_NODE_HOST env var.
    """
    env_host = os.environ.get("NEOHIRO_BRAIN_NODE_HOST", "").strip()
    if env_host:
        return env_host
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        status = json.loads(result.stdout)
        for peer in status.get("Peer", []):
            dns = peer.get("DNSName", "")
            if "brain-node" in dns:
                ips = peer.get("TailscaleIPs", [])
                if ips:
                    return ips[0]
    except Exception:
        pass
    return None


def _shared_root() -> Path:
    r"""Canonical /shared/ root, overridable for non-Linux test environments.

    On the device this is /shared (LUKS-mounted, see DOCKER_ARCHITECTURE.md).
    On Windows/macOS dev hosts, set NEOHIRO_SHARED_ROOT to a writable
    directory; the literal /shared would resolve to C:\shared which is
    almost never writable in a test sandbox.
    """
    return Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared"))


def _delegate_record(
    brief: dict,
    route: str,
    reason: str = "",
    session_id: str = "",
) -> dict:
    """Build the delegation record written to /shared/heart/heartbeat/delegations/."""
    rec = {
        "ts": _iso_now(),
        "task_id": brief.get("task_id", ""),
        "route": route,
        "reason": reason,
        "cascade_model": brief.get("cascade_model", "openrouter/free"),
        "auto_resume": brief.get("auto_resume", False),
    }
    if session_id:
        rec["session_id"] = session_id
    if route == "local":
        rec["warning"] = "brain_node_offline"
    return rec


def _write_delegation_record(record: dict) -> Path:
    """Atomically write delegation record to the shared heartbeat dir."""
    out_dir = _shared_root() / "heart/heartbeat/delegations"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_slug = record["ts"].replace(":", "").replace("-", "")
    out_path = out_dir / f"{ts_slug}.json"
    stage_path = out_path.with_suffix(".json.stage")
    with open(stage_path, "w", encoding="utf-8") as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    stage_path.replace(out_path)
    return out_path


def _build_brief(args: argparse.Namespace) -> dict:
    """Build a brain_node_brief dict from command-line arguments."""
    task_id = str(uuid.uuid4())
    scope_repo = args.scope or os.environ.get("NEOHIRO_DELEGATE_SCOPE", "")
    brief = {
        "session_type": "brain_node_task",
        "task_id": task_id,
        "idempotency_key": hashlib.sha256(f"{task_id}:{args.objective}".encode()).hexdigest(),
        "created_by": "operator",
        "scope": {
            "repo": scope_repo,
            "org": args.org,
            "entity": None,
        },
        "objective": args.objective,
        "acceptance_criteria": args.acceptance_criteria or [],
        "cascade_model": "openrouter/free",
        "auto_resume": args.auto_resume,
        "created_at": _iso_now(),
    }
    return brief  # noqa: RET504 — brief holds task_id for idempotency_key above


def cmd_delegate(args: argparse.Namespace) -> int:
    """Delegate a coding task to the brain node.

    Per BRAIN_NODE_OPENCODE_ROUTING.md § 5:
      1. Build brief
      2. Validate (length, no injection chars)
      3. Health check (2 s timeout)
      4. Write brief atomically
      5. Call brainctl or fall back to Python urllib3
      6. Write delegation record
    """
    brief = _build_brief(args)

    # Step 2: Validate before I/O. Rejections always write an audit record and
    # honor --json for machine-readable output.
    validation_error: str | None = None
    if len(brief["objective"]) > 1024:
        validation_error = f"objective_too_long:{len(brief['objective'])}"
        print(
            f"heartctl: error: objective is {len(brief['objective'])} chars "
            "(max 1024). Split multi-sentence objectives into separate briefs.",
            file=sys.stderr,
        )
    else:
        for i, entry in enumerate(brief.get("relevant_files") or []):
            for bad in ("..", "$", "|", ";"):
                if bad in entry:
                    validation_error = f"injection_char:{bad}"
                    print(
                        f"heartctl: error: relevant_files[{i}] contains rejected "
                        f"token {bad!r}: {entry!r}",
                        file=sys.stderr,
                    )
                    break
            if validation_error:
                break

    if validation_error:
        rec = _delegate_record(brief, route="rejected", reason=validation_error)
        if args.json:
            print(json.dumps(rec, indent=2))
        if not args.dry_run:
            _write_delegation_record(rec)
        return 1

    if args.target == "local" or args.dry_run:
        record = _delegate_record(brief, route="local", reason="dry_run" if args.dry_run else "user_requested")
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print(f"delegate: route=local task_id={brief['task_id']}", end="")
            if args.dry_run:
                print(" (dry-run)")
            else:
                print()
        return 0

    # Step 3: Health check via a direct HTTP probe (urllib stdlib, no curl dep).
    brain_ip = _brain_node_ip()
    health_ok = False
    health_msg = ""
    if brain_ip:
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://{brain_ip}:4096/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                health_ok = resp.status == 200
                health_msg = f"status={resp.status}"
        except Exception as e:
            health_msg = f"{type(e).__name__}: {e}"
    else:
        health_msg = "brain_node_ip_unresolved (tailscale or NEOHIRO_BRAIN_NODE_HOST)"

    if not health_ok:
        record = _delegate_record(brief, route="local", reason="brain_node_offline")
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print(
                f"delegate: route=local task_id={brief['task_id']} "
                f"reason=brain_node_offline ({health_msg})",
                file=sys.stderr,
            )
        if not args.dry_run:
            _write_delegation_record(record)
        return 0

    # Step 4: Write brief atomically
    task_dir = _shared_root() / f"brain/opencode/sessions/{brief['task_id']}"
    brief_path = task_dir / "brief.json"
    if not args.dry_run:
        task_dir.mkdir(parents=True, exist_ok=True)
        stage_path = task_dir / "brief.json.tmp"
        with open(stage_path, "w", encoding="utf-8") as f:
            json.dump(brief, f)
            f.flush()
            os.fsync(f.fileno())
        stage_path.replace(brief_path)

    # Step 5: Call brainctl
    brainctl_path = Path(__file__).parent / "brainctl"
    session_id = ""
    if brainctl_path.exists():
        try:
            result = subprocess.run(
                [str(brainctl_path), "delegate"],
                input=json.dumps(brief).encode(),
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                try:
                    out = json.loads(result.stdout)
                except (json.JSONDecodeError, ValueError) as e:
                    print(
                        f"heartctl: warning: brainctl returned non-JSON "
                        f"({type(e).__name__}: {e}); treating as session_create_failed",
                        file=sys.stderr,
                    )
                    out = {}
                session_id = out.get("session_id", "") if isinstance(out, dict) else ""
        except Exception as e:
            print(f"heartctl: warning: brainctl call failed: {e}", file=sys.stderr)

    # Step 6: Write delegation record
    route = "brain_node" if session_id else "pending_retry"
    reason = "" if session_id else "session_create_failed"
    record = _delegate_record(brief, route=route, reason=reason, session_id=session_id)
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(f"delegate: route={route} task_id={brief['task_id']}", end="")
        if session_id:
            print(f" session_id={session_id}")
        else:
            print(" reason=session_create_failed")
    if not args.dry_run:
        _write_delegation_record(record)
    return 0


def die(msg: str) -> NoReturn:
    """Local die: error to stderr + exit 1."""
    print(f"heartctl: error: {msg}", file=sys.stderr)
    sys.exit(1)


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

    r = sub.add_parser(
        "router",
        help="route a user request to a model via preset (per LLM_ROUTER_CASCADE.md § 2)",
    )
    r.add_argument(
        "--preset",
        required=True,
        choices=list(VALID_PRESETS),
        help="use-case preset (coding | reasoning | fast | multimodal | tools)",
    )
    r.add_argument(
        "--use-case",
        help="optional use-case tag (e.g. quick-fix, essay, chat)",
    )
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="print plan but do not write router context file",
    )
    r.add_argument(
        "--json",
        action="store_true",
        help="emit JSON only",
    )

    # delegate subparser (before args = parser.parse_args())
    d = sub.add_parser(
        "delegate",
        help="delegate a coding task to the brain node (per BRAIN_NODE_OPENCODE_ROUTING.md § 5)",
    )
    d.add_argument(
        "--target", choices=["brain-node", "local"], default="brain-node",
        help="delegation target (default: brain-node)",
    )
    d.add_argument(
        "--scope", metavar="OWNER/REPO",
        help="scope in owner/repo form (e.g. neohiro/LLM)",
    )
    d.add_argument(
        "--org",
        choices=["neohiro", "fpm", "osi", "hplus"], default="neohiro",
        help="org name (default: neohiro)",
    )
    d.add_argument(
        "--objective", required=True,
        help="brief objective text (max 1024 chars; multi-sentence objectives must be split)",
    )
    d.add_argument(
        "--acceptance", action="append", dest="acceptance_criteria", default=[],
        help="acceptance criterion (may be given multiple times)",
    )
    d.add_argument(
        "--auto-resume", action="store_true",
        help="enable auto-resume plugin (always set for High complexity tasks)",
    )
    d.add_argument(
        "--dry-run", action="store_true",
        help="print plan but do not write the brief or call brainctl",
    )
    d.add_argument(
        "--json", action="store_true",
        help="emit the delegation record as JSON to stdout",
    )

    w = sub.add_parser("delegate-watch", help="tail a brain-node session dir for new files")
    w.add_argument("--session-id", required=True, help="session id (uuid)")
    w.add_argument("--poll-interval", type=int, default=30, help="poll interval in seconds (min 5)")

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
        "router": cmd_router,
        "delegate": cmd_delegate,
        "delegate-watch": cmd_watch_session,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
