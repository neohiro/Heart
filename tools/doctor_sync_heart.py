#!/usr/bin/env python3
"""
doctor_sync_heart.py -- Heart dispatcher
Runs neohiro-doctor/tools/sync_doctor_workflow.py to push doctor.yml
to every repo in the 4 orgs. Runs as a Heart cycle scope.

Args (forwarded to sync_doctor_workflow.py):
    --org {neohiro,frenzypenguin-media,openstageisland,transhumanists,all}
    --repo    (single 'owner/name')
    --force   (re-upload even if matches)
    --dry-run (preview only; default is --apply)

Dispatcher flags:
    --json    Emit a single JSON envelope on success instead of human log.
              Heart's conformance.py parses this for dashboard metrics.

Env:
    NEOHIRO_DOCTOR_ROOT    Path to neohiro-doctor checkout (default: /brain/neohiro-doctor)
    GH_TOKEN               PAT for cross-repo API calls
    GITHUB_ACTIONS=true    Auto-enables --json behavior in CI
    HEART_CYCLE            Auto-enables --json behavior in Heart cycles
    HEART_ORGS_FILE        Path to orgs.txt (for Heart dynamic orgs)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

DOCTOR_ROOT: str = os.environ.get("NEOHIRO_DOCTOR_ROOT", "/brain/neohiro-doctor")
SCRIPT: str = os.path.join(DOCTOR_ROOT, "tools", "sync_doctor_workflow.py")


def parse_args() -> argparse.Namespace:
    json_mode = bool(
        os.environ.get("GITHUB_ACTIONS") == "true"
        or os.environ.get("HEART_CYCLE")
    )
    p = argparse.ArgumentParser(description="Heart dispatcher for doctor-sync")
    p.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=json_mode,
        help="Emit structured JSON envelope (auto-on in GITHUB_ACTIONS / HEART_CYCLE)",
    )
    p.add_argument(
        "--org", default="all", help="Forwarded to sync_doctor_workflow.py"
    )
    p.add_argument("--repo", default=None, help="Single repo 'owner/name'")
    p.add_argument("--force", action="store_true", help="Force re-upload")
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.add_argument(
        "--orgs-file",
        default=os.environ.get("HEART_ORGS_FILE"),
        help="Path to orgs.txt (for Heart dynamic orgs)",
    )
    return p.parse_args()


def build_envelope(
    returncode: int, started: float, args: argparse.Namespace
) -> dict[str, Any]:
    return {
        "tool": "doctor_sync_heart",
        "version": "1.0",
        "started_at": datetime.fromtimestamp(started, tz=timezone.utc).isoformat(),
        "ended_at": datetime.now(tz=timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 3),
        "returncode": returncode,
        "ok": returncode == 0,
        "args": {
            "org": args.org,
            "repo": args.repo,
            "force": args.force,
            "dry_run": args.dry_run,
        },
        "doctor_root": DOCTOR_ROOT,
    }


def main() -> int:
    args = parse_args()

    if not os.path.isfile(SCRIPT):
        msg = f"{SCRIPT} not found"
        if args.json_output:
            print(json.dumps({"ok": False, "error": msg, "tool": "doctor_sync_heart"}))
        else:
            sys.stderr.write(f"ERROR: {msg}\n")
        return 1

    started = time.time()
    env = os.environ.copy()
    if "GH_TOKEN" in env and "GITHUB_TOKEN" not in env:
        env["GITHUB_TOKEN"] = env["GH_TOKEN"]

    cmd: list[str] = ["python", SCRIPT, "--org", args.org]
    if args.repo:
        cmd += ["--repo", args.repo]
    if args.force:
        cmd.append("--force")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.orgs_file:
        cmd += ["--orgs-file", args.orgs_file]

    if not args.json_output:
        print(f"[doctor-sync-heart] Running: {' '.join(cmd)}", flush=True)

    result = subprocess.run(cmd, env=env)
    envelope = build_envelope(result.returncode, started, args)

    if args.json_output:
        print(json.dumps(envelope, separators=(",", ":")))
    else:
        print(
            f"[doctor-sync-heart] done rc={result.returncode} "
            f"in {envelope['duration_seconds']}s",
            flush=True,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())