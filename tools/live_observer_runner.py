"""
live_observer_runner.py — Heart bridge: start live_observer as a managed daemon.

Discovers all org repos from Brain/_entities/org-*.md (the same source
as Heart's _discover_orgs_from_entities), builds scope:root pairs, and
launches Brain/src/live_observer.py as a subprocess.  Writes a sentinel
file so Heart and doctor can confirm the daemon is alive.

Run:
    python Heart/tools/live_observer_runner.py           # daemon mode
    python Heart/tools/live_observer_runner.py --once    # scan once, emit, exit
    python Heart/tools/live_observer_runner.py --roots neohiro:/neohiro   # override

Sentinel:
    /shared/brain/watch/observer.sentinel.json

Exit codes:
    0  — clean exit (SIGTERM, --once)
    1  — configuration error (no repos found, no roots given)
    2  — live_observer subprocess exited with non-zero
    3  — no observer module found
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PY = sys.executable
BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))
WATCH_DIR = Path(os.environ.get("NEOHIRO_WATCH_DIR", "/shared/brain/watch"))
SENTINEL_PATH = WATCH_DIR / "observer.sentinel.json"
REPO_ROOT = Path(os.environ.get("NEOHIRO_REPO_ROOT", str(BRAIN_PATH.parent)))

_observer_proc: subprocess.Popen | None = None
_running = True


def _sentinel_write(pid: int, roots: dict[str, str], *, ok: bool = True, error: str = "") -> None:
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    from atomic import write_json
    write_json(
        SENTINEL_PATH,
        {
            "pid": pid,
            "ok": ok,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "roots": roots,
            "error": error,
        },
        prefix=".sentinel.",
    )


def _sentinel_remove() -> None:
    with contextlib.suppress(OSError):
        SENTINEL_PATH.unlink(missing_ok=True)


def _discover_orgs() -> dict[str, Path]:
    ents_dir = BRAIN_PATH / "_entities"
    roots: dict[str, Path] = {}
    if not ents_dir.is_dir():
        return roots
    for path in ents_dir.iterdir():
        if path.is_dir() or path.suffix != ".md" or not path.name.startswith("org-"):
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            continue
        front, _, _ = raw.partition("\n---")
        try:
            import yaml
            fm = yaml.safe_load(front) or {}
        except Exception:
            continue
        org: str = str(fm.get("github_org", ""))
        raw_repos = fm.get("repos", [])
        repo_list: list[str] = []
        if isinstance(raw_repos, list):
            repo_list = [str(r) for r in raw_repos if r]
        elif isinstance(raw_repos, str):
            repo_list = [r.strip() for r in raw_repos.split(",") if r.strip()]
        if not org:
            continue
        for r in repo_list:
            local = REPO_ROOT / r
            if local.is_dir():
                roots[f"{org}/{r}"] = local
            elif (REPO_ROOT / org / r).is_dir():
                roots[f"{org}/{r}"] = REPO_ROOT / org / r
    return roots


def _build_roots_arg(roots: dict[str, Path]) -> str:
    return ",".join(f"{scope}:{root}" for scope, root in sorted(roots.items()))


def _launch_observer(roots: dict[str, Path], once: bool = False) -> subprocess.Popen:
    roots_arg = _build_roots_arg(roots)
    observer_mod = BRAIN_PATH / "src" / "live_observer.py"
    if not observer_mod.is_file():
        raise FileNotFoundError(f"observer module not found: {observer_mod}")
    cmd = [PY, str(observer_mod), "--roots", roots_arg]
    if once:
        cmd.append("--once")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )


def _sigterm_handler(signum: int, frame) -> None:
    global _running
    _running = False
    print("[live_observer_runner] SIGTERM received, shutting down...")
    if _observer_proc is not None and _observer_proc.poll() is None:
        _observer_proc.terminate()
        try:
            _observer_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _observer_proc.kill()
    _sentinel_remove()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="live_observer_runner",
        description="Launch Brain/src/live_observer.py as a managed daemon.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="scan once (initial-scan) and exit instead of watching",
    )
    parser.add_argument(
        "--roots",
        default="",
        help="override roots (scope:path pairs, comma-sep); "
        "default: discover from Brain/_entities/",
    )
    parser.add_argument(
        "--sentinel-only",
        action="store_true",
        help="write sentinel and exit (checks if observer is already running)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    roots: dict[str, Path] = {}
    if args.roots:
        for entry in args.roots.split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            scope, path = entry.split(":", 1)
            if not re.match(r"^[A-Za-z0-9_/-]{1,128}$", scope):
                print(f"[live_observer_runner] invalid scope {scope!r}, skipping", file=sys.stderr)
                continue
            roots[scope] = Path(path).resolve()
    else:
        roots = _discover_orgs()

    if not roots:
        print("[live_observer_runner] no roots found; set NEOHIRO_REPO_ROOT or --roots", file=sys.stderr)
        return 1

    if args.sentinel_only:
        _sentinel_write(os.getpid(), {k: str(v) for k, v in roots.items()})
        print(f"[live_observer_runner] sentinel written: {SENTINEL_PATH}")
        return 0

    try:
        proc = _launch_observer(roots, once=args.once)
    except FileNotFoundError as e:
        print(f"[live_observer_runner] {e}", file=sys.stderr)
        return 3

    global _observer_proc
    _observer_proc = proc
    _sentinel_write(proc.pid, {k: str(v) for k, v in roots.items()})

    if args.once:
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        rc = proc.wait()
        _sentinel_remove()
        if rc != 0 and stderr:
            print(f"[live_observer_runner] observer stderr: {stderr}", file=sys.stderr)
        return rc

    print(f"[live_observer_runner] live_observer started (pid={proc.pid}), watching {len(roots)} roots")
    print(f"  sentinel: {SENTINEL_PATH}")
    for scope, root in sorted(roots.items()):
        print(f"  {scope} → {root}")

    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        print("[live_observer_runner] interrupted")
        proc.terminate()
        rc = proc.wait()
    finally:
        _sentinel_remove()

    if rc != 0:
        print(f"[live_observer_runner] observer exited with {rc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
