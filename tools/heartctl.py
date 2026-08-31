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
    doctor-deep         — cross-check /healthz, .heartbeat, and repo_summary (see test_doctor_deep.py)
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
import warnings
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
    if args.mode_value:
        # Reject whitespace-only mode strings before mkdir/write to avoid
        # creating an empty mode.yaml. argparse gives us a str here, so
        # .strip() is always safe.
        if not args.mode_value.strip():
            print("heartctl: error: mode_value is empty or whitespace", file=sys.stderr)
            return 1
        mode_file.parent.mkdir(parents=True, exist_ok=True)
        from atomic import write_text
        write_text(mode_file, f"mode: {args.mode_value}\n")
        print(f"mode set to: {args.mode_value}")
        return 0
    mode_file.parent.mkdir(parents=True, exist_ok=True)
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

    # Honour --dry-run: gate both the module-level DRY_RUN flag (used by
    # heart.py writers) AND the HEART_DRY_RUN env var (consumed by
    # heart_shared_prune._is_dry_run). Both must be set for the dry-run
    # contract to hold end-to-end.
    if getattr(args, "dry_run", False):
        _heart_module.DRY_RUN = True
        os.environ["HEART_DRY_RUN"] = "1"

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
        "grounding_audit": _heart_module._phase_grounding_audit,
        "prune_shared": _heart_module._phase_prune_shared,
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


# ─── doctor --deep (cross-checks /healthz + /shared/.heartbeat + repo_summary) ─
#
# Constants used by the deep doctor check.
HEARTBEAT_FILE = "/shared/.heartbeat"
_HEARTBEAT_MAX_AGE_DEFAULT = 90
CYCLE_DRIFT_THRESHOLD = 5
_SKEW_TOLERANCE_S = 2
# Maximum bytes to read from /healthz before aborting.  The Go sidecar
# emits a ~200-byte JSON object.  A 1 MB cap is 5000x the normal size and
# prevents a malicious or misconfigured server from exhausting doctor RAM.
_MAX_HEALTHZ_BYTES = 1 * 1024 * 1024
# Maximum bytes to read from repo_summary.json before rejecting.  A file
# larger than this is treated as missing/corrupt and returns None.  Tunable
# via this constant so an operator can adjust without changing the code.
_MAX_REPO_SUMMARY_BYTES = 10 * 1024 * 1024
# Maximum heartbeat file size to read for sentinel validation.
# The Go sidecar writes ~12 bytes.  Reject anything >> that to prevent a
# malicious or accidental large-file write from exhausting doctor RAM.
_MAX_HEARTBEAT_SENTINEL_BYTES = 10 * 1024
# Maximum diagnostic JSON files to keep in Brain's knowledge base.
# Older files are pruned after each run to prevent unbounded disk usage.
_MAX_KB_DOCTOR_DEEP_FILES = 100


def _resolve_healthz_max_age() -> int:
    """Resolve HEARTBEAT_MAX_AGE env, defaulting to 90s on bad/missing input.

    Re-read on every call (cheap), so an operator who tweaks the sidecar
    doesn't need a heartctl restart.  Wraps in try/except so a malformed
    env var (e.g. "abc", "", "0") never crashes the import path.
    """
    raw = os.environ.get("HEARTBEAT_MAX_AGE", str(_HEARTBEAT_MAX_AGE_DEFAULT))
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _HEARTBEAT_MAX_AGE_DEFAULT
    return v if v >= 1 else _HEARTBEAT_MAX_AGE_DEFAULT


def _resolve_skew_tolerance_s() -> int:
    """Resolve HEART_SKEW_TOLERANCE_S env, defaulting to 2s on bad/missing input.

    Allows operators on systems with poor clock discipline (virtualized hosts,
    suspended laptops, Raspberry Pi) to increase the tolerance without a code
    change.  Bad/missing env falls back to the module-level default.
    """
    raw = os.environ.get("HEART_SKEW_TOLERANCE_S", str(_SKEW_TOLERANCE_S))
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _SKEW_TOLERANCE_S
    return v if v >= 0 else _SKEW_TOLERANCE_S


def _fetch_healthz(port: int, timeout: float = 2.0) -> dict | None:
    """Return the parsed /healthz JSON, or None on any failure.

    Uses a raw socket (not urllib) for two reasons:
      1. urllib follows http_proxy/HTTPS_PROXY env vars by default. A
         127.0.0.1 healthcheck could be mis-routed to an external proxy
         if the operator has those vars set in their shell.
      2. urllib does DNS resolution; we want this call to be hermetic
         and never touch the network beyond the local interface.

    The response is capped at _MAX_HEALTHZ_BYTES to prevent a malicious or
    misconfigured server from exhausting the doctor process's RAM.
    """
    import re as _re
    import socket

    truncated = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n"
                      b"Connection: close\r\n\r\n")
            chunks: list[bytes] = []
            total = 0
            while total < _MAX_HEALTHZ_BYTES:
                buf = s.recv(4096)
                if not buf:
                    break
                chunks.append(buf)
                total += len(buf)
            else:
                truncated = True
    except OSError:
        return None

    if truncated:
        return None
    if not chunks:
        return None
    raw = b"".join(chunks)

    # Split headers from body at the first blank line.
    # Accept both RFC-compliant \r\n\r\n and lenient \n\n.
    sep_idx = raw.find(b"\r\n\r\n")
    if sep_idx >= 0:
        header_part = raw[:sep_idx]
        body = raw[sep_idx + 4:]
    else:
        sep_idx = raw.find(b"\n\n")
        if sep_idx >= 0:
            header_part = raw[:sep_idx]
            body = raw[sep_idx + 2:]
        else:
            return None

    # Parse the status line (the first line of the response) to extract the
    # HTTP status code.  The status line is the first line before any \n or \r.
    first_lines = header_part.split(b"\n")
    if not first_lines:
        return None
    status_line = first_lines[0]
    # RFC 7230 §3.1: status-line = HTTP-version SP status-code SP reason-phrase
    # e.g. "HTTP/1.1 200 OK".  Extract only the 3-digit status code.
    m = _re.fullmatch(rb"HTTP/1\.[01] (\d{3}) .*", status_line)
    if not m or m.group(1) != b"200":
        return None

    # Strip any chunked-transfer encoding trailer.
    if b"chunked" in header_part.lower():
        body = _decode_chunked(body)

    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _decode_chunked(body: bytes) -> bytes:
    """Decode HTTP chunked Transfer-Encoding for a single final chunk.

    Handles the Go sidecar's typical case: one non-zero-size chunk followed by
    the final 0\r\n\r\n terminator.  On receipt of the first size==0 chunk the
    decoder stops — any data after that point ( trailers, extra zero-chunks)
    is discarded.  This matches what the Go stdlib emits for a small JSON
    response: a single chunk that carries the entire body.
    """
    import re as _re
    result: list[bytes] = []
    remaining = body
    while remaining:
        # Each chunk: <size-in-hex> CRLF <data> CRLF  (or final: 0 CRLF)
        m = _re.match(rb"([0-9a-fA-F]+)\r\n", remaining)
        if not m:
            break
        size = int(m.group(1), 16)
        if size == 0:
            break
        data_start = m.end()
        data_end = data_start + size
        result.append(remaining[data_start:data_end])
        remaining = remaining[data_end:]
        if remaining.startswith(b"\r\n"):
            remaining = remaining[2:]
    return b"".join(result)


def _read_repo_summary() -> dict | None:
    """Return the on-disk repo_summary.json dict, or None if missing/corrupt.

    Capped at _MAX_REPO_SUMMARY_BYTES (10 MB by default) to prevent a hostile
    or accidentally-huge file from OOM'ing the doctor process.  10 MB is ~50x
    the current typical size (~200 KB) so a real-world OOM condition is the
    only thing this would ever reject.
    """
    path = BRAIN_PATH / "heartbeat" / "repo_summary.json"
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        if size > _MAX_REPO_SUMMARY_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


HEARTBEAT_SENTINEL_MARKER = b"heartbeat: OK\n"


def _check_heartbeat_content_is_sentinel(path: Path) -> bool:
    """Check if /shared/.heartbeat contains the expected sentinel.

    Returns True if content matches HEARTBEAT_SENTINEL_MARKER, False otherwise.
    Rejects files larger than _MAX_HEARTBEAT_SENTINEL_BYTES to prevent
    memory exhaustion from a malicious or accidental large-file write.
    """
    try:
        if not path.is_file():
            return False
        st = path.stat()
        if st.st_size > _MAX_HEARTBEAT_SENTINEL_BYTES:
            return False
        content = path.read_bytes()
        return content == HEARTBEAT_SENTINEL_MARKER
    except OSError:
        return False


def _check_healthz_vs_heartbeat() -> list[str]:
    """Compare /healthz JSON to /shared/.heartbeat mtime. Returns a list of
    drift messages; empty list means the two sources agree."""
    return _doctor_diagnose()["drift"]


def _doctor_diagnose(health_port: int | None = None) -> dict:
    """Return a structured diagnosis of /healthz + .heartbeat + repo_summary.

    Returns a dict suitable for both human rendering and machine parsing
    (consumed by cmd_doctor_deep --json and by the Brain self-improvement
    pass for richer inputs).

    Keys:
        ok (bool)              — True if all drift checks passed
        drift (list[str])      — human-readable drift messages (empty when ok)
        max_age_s (int)        — HEARTBEAT_MAX_AGE effective value
        skew_tolerance_s (int) — HEART_SKEW_TOLERANCE_S effective value
        mtime_age_s (int|None) — seconds since .heartbeat was touched, None if missing
        sentinel_valid (bool)  — True if .heartbeat contains the canonical sentinel
        healthz_reachable (bool) — True if /healthz responded with a parseable body
        healthz_cycle (int|None) — cycle value from /healthz, None if unreachable
        repo_cycle (int|None) — cycle value from repo_summary, None if missing/corrupt
        health_port (int|None) — port used for /healthz
        error (str|None)       — fatal error preventing further checks (e.g. bad port)
    """
    max_age = _resolve_healthz_max_age()
    skew_tolerance = _resolve_skew_tolerance_s()
    diagnosis: dict = {
        "ok": True,
        "drift": [],
        "max_age_s": max_age,
        "skew_tolerance_s": skew_tolerance,
        "mtime_age_s": None,
        "sentinel_valid": False,
        "healthz_reachable": False,
        "healthz_cycle": None,
        "repo_cycle": None,
        "health_port": None,
        "error": None,
    }

    hb_path = Path(HEARTBEAT_FILE)
    if not hb_path.is_file():
        diagnosis["drift"].append(
            "no .heartbeat file at /shared/.heartbeat — Heart may be down"
        )
        diagnosis["ok"] = False
        return diagnosis

    port_env = os.environ.get("HEART_HEALTH_PORT", "9090")
    if health_port is not None:
        # CLI flag overrides env var
        port = health_port
    else:
        try:
            port = int(port_env)
        except ValueError:
            diagnosis["error"] = f"HEART_HEALTH_PORT is not an integer: {port_env!r}"
            diagnosis["ok"] = False
            diagnosis["drift"].append(diagnosis["error"])
            return diagnosis
    if not (1 <= port <= 65535):
        diagnosis["error"] = f"HEART_HEALTH_PORT out of range: {port}"
        diagnosis["ok"] = False
        diagnosis["drift"].append(diagnosis["error"])
        return diagnosis
    diagnosis["health_port"] = port

    healthz = _fetch_healthz(port)
    if healthz is None:
        diagnosis["drift"].append(
            f"/healthz on 127.0.0.1:{port} unreachable — "
            f"Go binary may not be running, or port blocked"
        )
        diagnosis["ok"] = False
        return diagnosis
    diagnosis["healthz_reachable"] = True
    try:
        diagnosis["healthz_cycle"] = int(healthz.get("cycle", 0) or 0)
    except (TypeError, ValueError):
        diagnosis["healthz_reachable"] = False
        diagnosis["drift"].append(
            f"/healthz returned non-integer cycle: {healthz.get('cycle')!r}"
        )
        diagnosis["ok"] = False
        return diagnosis

    now = time.time()
    try:
        mt = hb_path.stat().st_mtime
    except OSError as e:
        diagnosis["error"] = f"cannot stat {HEARTBEAT_FILE}: {e}"
        diagnosis["ok"] = False
        diagnosis["drift"].append(diagnosis["error"])
        return diagnosis
    age = int(now - mt)
    diagnosis["mtime_age_s"] = age
    if age < -skew_tolerance:
        diagnosis["drift"].append(
            f"clock skew: .heartbeat mtime is {abs(age)}s in the future"
        )
        diagnosis["ok"] = False
    elif age > max_age:
        diagnosis["drift"].append(
            f"stale .heartbeat: age={age}s > max={max_age}s "
            f"(container may be wedged even though /healthz responds)"
        )
        diagnosis["ok"] = False

    if _check_heartbeat_content_is_sentinel(hb_path):
        diagnosis["sentinel_valid"] = True
    else:
        # Reason for rejection: oversized, missing, or content mismatch.
        # _check_heartbeat_content_is_sentinel returns False in all cases
        # without distinguishing them; the doctor surface only needs to
        # know the sentinel does not match.
        diagnosis["drift"].append(
            f".heartbeat file corrupted or missing sentinel: "
            f"{HEARTBEAT_SENTINEL_MARKER!r}"
        )
        diagnosis["ok"] = False

    repo = _read_repo_summary()
    if repo is not None:
        try:
            repo_cycle = int(repo.get("cycle") or 0)
        except (TypeError, ValueError):
            repo_cycle = None
            diagnosis["drift"].append(
                f"repo_summary returned non-integer cycle: {repo.get('cycle')!r}"
            )
            diagnosis["ok"] = False
        else:
            diagnosis["repo_cycle"] = repo_cycle
            if diagnosis["healthz_cycle"] is not None and diagnosis["healthz_cycle"] < repo_cycle:
                diagnosis["drift"].append(
                    f"cycle regression: /healthz.cycle={diagnosis['healthz_cycle']} < "
                    f"repo_summary.cycle={repo_cycle} (Go binary restarted?)"
                )
                diagnosis["ok"] = False
            elif (
                diagnosis["healthz_cycle"] is not None
                and diagnosis["healthz_cycle"] - repo_cycle > CYCLE_DRIFT_THRESHOLD
            ):
                diagnosis["drift"].append(
                    f"cycle drift: /healthz.cycle={diagnosis['healthz_cycle']} is "
                    f"{diagnosis['healthz_cycle'] - repo_cycle} ahead of repo_summary "
                    f"(writes are lagging; check disk)"
                )
                diagnosis["ok"] = False
    return diagnosis


def _doctor_self_heal(diagnosis: dict, hb_path: Path | None = None) -> dict:
    """Attempt to remediate fixable drift. Returns a dict of actions taken.

    Self-healable drift classes:
      - stale .heartbeat (mtime drift)        → touch the file
      - corrupted .heartbeat (sentinel drift) → write the canonical sentinel
    Non-self-healable:
      - /healthz unreachable                  → operator must restart Go binary
      - cycle regression / cycle drift        → operator must inspect the disk

    hb_path: override for HEARTBEAT_FILE (used by tests).  Defaults to the
    module-level constant.
    """
    actions = {
        "touched_heartbeat": False,
        "regenerated_heartbeat": False,
        "errors": [],
    }
    if diagnosis.get("ok"):
        return actions
    hb_path = hb_path if hb_path is not None else Path(HEARTBEAT_FILE)
    drift_msgs = diagnosis.get("drift", [])

    # Sentinel corruption: write the canonical content
    sentinel_corrupt = any("corrupted" in d for d in drift_msgs)
    if sentinel_corrupt and hb_path.is_file():
        tmp = hb_path.with_suffix(hb_path.suffix + ".heal")
        try:
            tmp.write_bytes(HEARTBEAT_SENTINEL_MARKER)
            tmp.replace(hb_path)
            os.utime(hb_path)
            actions["regenerated_heartbeat"] = True
        except OSError as e:
            actions["errors"].append(f"regenerate failed: {e}")
            # Clean up the orphan temp on any failure so /shared doesn't
            # accumulate stale .heal files after a failed write.
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        return actions

    # Stale mtime: touch the file to refresh
    stale = any("stale" in d for d in drift_msgs)
    if stale and hb_path.is_file():
        try:
            os.utime(hb_path)
            actions["touched_heartbeat"] = True
        except OSError as e:
            actions["errors"].append(f"touch failed: {e}")

    return actions


def cmd_doctor_deep(args: argparse.Namespace) -> int:
    """Deep doctor: cross-check /healthz, /shared/.heartbeat, and repo_summary.

    Use when an alert says "Heart unhealthy" and you need to know which
    invariant is broken.  Each drift is reported with a specific remediation
    hint.  With --json, emits machine-readable JSON to stdout.

    Self-healable drift classes (stale mtime, sentinel corruption) can be
    remediated automatically with --self-heal.  The diagnostic JSON is also
    written to Brain's knowledge base for the self-improvement pass.
    """
    d = _doctor_diagnose(health_port=getattr(args, "health_port", None))

    # Export diagnostic to Brain for self-improvement pass
    _doctor_export_diagnostic(d)

    do_self_heal = getattr(args, "self_heal", False) or getattr(args, "fix_heartbeat", False)
    if getattr(args, "fix_heartbeat", False):
        warnings.warn(
            "--fix-heartbeat is deprecated; use --self-heal instead",
            DeprecationWarning,
            stacklevel=2,
        )
    # JSON output — structured machine-readable format
    if getattr(args, "json", False):
        out: dict = {
            "ok": d["ok"],
            "drift": d["drift"],
            "sources": {
                "heartbeat_mtime_age_s": d["mtime_age_s"],
                "sentinel_valid": d["sentinel_valid"],
                "healthz_reachable": d["healthz_reachable"],
                "healthz_cycle": d["healthz_cycle"],
                "repo_cycle": d["repo_cycle"],
                "health_port": d["health_port"],
                "max_age_s": d["max_age_s"],
                "skew_tolerance_s": d["skew_tolerance_s"],
            },
            "error": d["error"],
            "self_heal": None,
        }
        if do_self_heal and not d["ok"]:
            actions = _doctor_self_heal(d)
            out["self_heal"] = actions
            if not actions["errors"] and (
                actions["touched_heartbeat"] or actions["regenerated_heartbeat"]
            ):
                d2 = _doctor_diagnose(health_port=getattr(args, "health_port", None))
                out["ok_after_heal"] = d2["ok"]
                out["drift_after_heal"] = d2["drift"]
                print(json.dumps(out, indent=2))
                return 0 if d2["ok"] else 1
        print(json.dumps(out, indent=2))
        return 0 if d["ok"] else 1

    # Human-readable output
    print("=== Heart deep doctor ===")
    print("(cross-checks /healthz, /shared/.heartbeat, repo_summary.json)")
    print()

    rc = 0
    if d["ok"]:
        print("  [OK] all three sources agree")
        print(f"        /shared/.heartbeat: mtime ≤ {d['max_age_s']}s ago")
        print(f"        sentinel: {'valid' if d['sentinel_valid'] else 'MISSING/CORRUPT'}")
        print(f"        /healthz: reachable (cycle={d['healthz_cycle']})")
        print(f"        repo_summary: cycle={d['repo_cycle']}")
    else:
        print(f"  [DRIFT] found {len(d['drift'])} mismatch(es):")
        for msg in d["drift"]:
            print(f"    - {msg}")
        rc = 1

    if do_self_heal and not d["ok"]:
        print()
        print("  [SELF-HEAL] attempting remediation...")
        actions = _doctor_self_heal(d)
        if actions["regenerated_heartbeat"]:
            print(f"    [OK] regenerated .heartbeat with canonical sentinel")
        if actions["touched_heartbeat"]:
            print(f"    [OK] touched .heartbeat to refresh mtime")
        if actions["errors"]:
            for err in actions["errors"]:
                print(f"    [ERROR] {err}")
        if not actions["regenerated_heartbeat"] and not actions["touched_heartbeat"]:
            print("    [SKIPPED] no self-healable drift found (unreachable /healthz, cycle regression, etc.)")
        elif not actions["errors"]:
            # Re-diagnose after self-heal so return code reflects current state
            d_after = _doctor_diagnose(health_port=getattr(args, "health_port", None))
            if d_after["ok"]:
                print()
                print("  [OK] all checks pass after self-heal")
                rc = 0
            else:
                rc = 1

    return rc


def _doctor_export_diagnostic(d: dict) -> None:
    """Write the doctor diagnostic to Brain's knowledge base.

    Writes /shared/brain/knowledge/doctor_deep/<ts>.json so the Heart
    self-improvement pass can read it and use the raw diagnostic fields
    (not just the drift messages) to make better scheduling decisions.
    """
    try:
        root = BRAIN_PATH
        if not root.is_dir():
            return
        kb_dir = root / "knowledge" / "doctor_deep"
        kb_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S")
        out = {
            "ts": ts,
            "ok": d["ok"],
            "drift": d["drift"],
            "sources": {
                "heartbeat_mtime_age_s": d["mtime_age_s"],
                "sentinel_valid": d["sentinel_valid"],
                "healthz_reachable": d["healthz_reachable"],
                "healthz_cycle": d["healthz_cycle"],
                "repo_cycle": d["repo_cycle"],
                "health_port": d["health_port"],
                "max_age_s": d["max_age_s"],
                "skew_tolerance_s": d["skew_tolerance_s"],
            },
            "error": d["error"],
        }
        path = kb_dir / f"{ts}.json"
        from atomic import write_json
        write_json(path, out, indent=2)
        # Prune oldest files beyond the cap.  Sort by name (timestamp prefix)
        # so lexicographic order matches chronological order.
        existing = sorted(kb_dir.glob("*.json"))
        if len(existing) > _MAX_KB_DOCTOR_DEEP_FILES:
            for old in existing[: len(existing) - _MAX_KB_DOCTOR_DEEP_FILES]:
                try:
                    old.unlink()
                except OSError:
                    pass
    except Exception:
        pass


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


def cmd_live_observer(args: argparse.Namespace) -> int:
    """Run one live_observer_runner.py cycle (scan + emit, no daemon)."""
    print("=== Heart live-observer scope (one-shot) ===")
    script = Path(__file__).parent / "live_observer_runner.py"
    if not script.is_file():
        print(f"script not found: {script}", file=sys.stderr)
        return 3
    cmd = [sys.executable, str(script), "--quiet", "--once"]
    if getattr(args, "roots", ""):
        cmd[1:1] = ["--roots", args.roots]
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, env=os.environ.copy(), check=False)
    return result.returncode


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
    ph.add_argument(
        "--dry-run",
        action="store_true",
        help="set HEART_DRY_RUN=1 + heart.DRY_RUN=True so writes are skipped",
    )

    t = sub.add_parser("trigger", help="trigger a single Heart cycle")
    t.add_argument("--dry-run", action="store_true")

    sub.add_parser("watch", help="tail the audit log in real time")

    sub.add_parser("doctor", help="run neohiro-doctor checks")

    dd = sub.add_parser("doctor-deep", help="cross-check /healthz, .heartbeat, and repo_summary (see test_doctor_deep.py)")
    dd.add_argument("--fix-heartbeat", action="store_true",
                    help="(deprecated) touch /shared/.heartbeat to refresh mtime; prefer --self-heal")
    dd.add_argument("--self-heal", action="store_true",
                    help="attempt to remediate fixable drift (stale mtime, sentinel corruption)")
    dd.add_argument("--json", action="store_true",
                    help="emit JSON diagnostic to stdout (machine-readable)")
    dd.add_argument("--health-port", type=int, default=None,
                    help="port to probe /healthz on (default: $HEART_HEALTH_PORT or 9090)")

    sub.add_parser("env-check", help="verify environment variables")

    sub.add_parser(
        "visitor-counters",
        help="run one visitor_counter_scraper.py cycle (see Heart/schedules/REGISTRY.yaml)",
    )
    sub.add_parser(
        "social-counters",
        help="run one social_counter_poll.py cycle (see Heart/schedules/REGISTRY.yaml)",
    )

    lo = sub.add_parser(
        "live-observer",
        help="run one live_observer_runner.py --once cycle (see Heart/schedules/REGISTRY.yaml)",
    )
    lo.add_argument(
        "--roots", default="",
        help="override roots (scope:path pairs, comma-sep); default: discover from Brain/_entities/",
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
        "doctor-deep": cmd_doctor_deep,
        "env-check": cmd_env_check,
        "visitor-counters": cmd_visitor_counters,
        "social-counters": cmd_social_counters,
        "live-observer": cmd_live_observer,
        "router": cmd_router,
        "delegate": cmd_delegate,
        "delegate-watch": cmd_watch_session,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
