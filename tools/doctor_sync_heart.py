#!/usr/bin/env python3
"""
doctor_sync_heart.py — Heart dispatcher
Runs neohiro-doctor/tools/sync_doctor_workflow.py to push doctor.yml
to every repo in the 4 orgs. Runs as a Heart cycle scope.

Args (forwarded):
    --org {neohiro,frenzypenguin-media,openstageisland,transhumanists,all}
    --apply   (apply changes; default: dry-run)
    --repo    (single 'owner/name')
    --force   (re-upload even if matches)

Env:
    NEOHIRO_DOCTOR_ROOT    Path to neohiro-doctor checkout
    GH_TOKEN               PAT for cross-repo API calls
"""
import os
import subprocess
import sys
from pathlib import Path

DOCTOR_ROOT = Path(os.environ.get(
    "NEOHIRO_DOCTOR_ROOT",
    "/brain/neohiro-doctor"
))
SCRIPT = DOCTOR_ROOT / "tools" / "sync_doctor_workflow.py"


def main():
    if not SCRIPT.exists():
        print(f"ERROR: {SCRIPT} not found", file=sys.stderr)
        return 1

    args = [sys.argv[0], "--org", "all", "--apply"]
    print(f"Running: python {SCRIPT} {' '.join(args[1:])}", flush=True)
    result = subprocess.run(
        ["python", str(SCRIPT), *args[1:]],
        capture_output=False,
        text=True,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
