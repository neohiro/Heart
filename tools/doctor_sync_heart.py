#!/usr/bin/env python3
"""
doctor_sync_heart.py -- Heart dispatcher
Runs neohiro-doctor/tools/sync_doctor_workflow.py to push doctor.yml
to every repo in the 4 orgs. Runs as a Heart cycle scope.

Args (forwarded to sync_doctor_workflow.py):
    --org {neohiro,frenzypenguin-media,openstageisland,transhumanists,all}
    --apply   (apply changes; default: dry-run)
    --repo    (single 'owner/name')
    --force   (re-upload even if matches)

Env:
    NEOHIRO_DOCTOR_ROOT    Path to neohiro-doctor checkout (default: /brain/neohiro-doctor)
    GH_TOKEN               PAT for cross-repo API calls
"""
from __future__ import annotations

import os
import subprocess
import sys

DOCTOR_ROOT: str = os.environ.get("NEOHIRO_DOCTOR_ROOT", "/brain/neohiro-doctor")
# Build path with os.path.join for cross-platform safety. The default
# (`/brain/neohiro-doctor`) is a Docker container path; on Linux runners
# `os.path.join` returns it verbatim, on Windows it falls back to that
# string with native separators -- which the dispatcher never runs on
# outside of dev/test, where `Path.exists()` will report missing and
# the script will exit with code 1.
SCRIPT: str = os.path.join(DOCTOR_ROOT, "tools", "sync_doctor_workflow.py")


def main() -> int:
    if not os.path.isfile(SCRIPT):
        sys.stderr.write(f"ERROR: {SCRIPT} not found\n")
        return 1

    env = os.environ.copy()
    if "GH_TOKEN" in env and "GITHUB_TOKEN" not in env:
        env["GITHUB_TOKEN"] = env["GH_TOKEN"]

    cmd = ["python", SCRIPT, "--org", "all", "--apply"]
    print(f"[doctor-sync-heart] Running: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, env=env)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
