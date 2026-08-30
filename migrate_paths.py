#!/usr/bin/env python3
"""
migrate_paths.py — migrate Heart data paths from Brain/heartbeat/ to Heart/data/.

Run once. Safe to re-run (idempotent).

Files migrated:
  Brain/heartbeat/last_run.yaml     → Heart/data/last_run.yaml
  Brain/heartbeat/health.yaml       → Heart/data/health.yaml
  Brain/heartbeat/repo_summary.json → Heart/data/repo_summary.json
  Brain/heartbeat/repos.yaml       → Heart/data/repos.yaml
  Brain/heartbeat/mode.yaml        → Heart/data/mode.yaml
  Brain/heartbeat/stale.yaml       → Heart/data/stale.yaml
  Brain/audit/heartbeat.yaml       → Heart/data/audit.yaml

Usage:
    python migrate_paths.py --brain-path Brain --heart-path Heart

Dry run (print only):
    python migrate_paths.py --brain-path Brain --heart-path Heart --dry-run
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


MIGRATIONS = [
    ("heartbeat/last_run.yaml", "data/last_run.yaml"),
    ("heartbeat/health.yaml", "data/health.yaml"),
    ("heartbeat/repo_summary.json", "data/repo_summary.json"),
    ("heartbeat/repos.yaml", "data/repos.yaml"),
    ("heartbeat/mode.yaml", "data/mode.yaml"),
    ("heartbeat/stale.yaml", "data/stale.yaml"),
    ("audit/heartbeat.yaml", "data/audit.yaml"),
]


def migrate(brain: Path, heart: Path, dry_run: bool = False) -> tuple[int, int]:
    copied = 0
    skipped = 0
    for src_rel, dst_rel in MIGRATIONS:
        src = brain / src_rel
        dst = heart / dst_rel
        if not src.exists():
            print(f"  SKIP (not found): {src}")
            skipped += 1
            continue
        if dry_run:
            print(f"  DRY: {src} → {dst}")
            copied += 1
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  OK:   {src} → {dst}")
            copied += 1
    return copied, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="migrate Heart data paths Brain/ → Heart/")
    parser.add_argument("--brain-path", required=True, help="Path to Brain")
    parser.add_argument("--heart-path", required=True, help="Path to Heart")
    parser.add_argument("--dry-run", action="store_true", help="Print without copying")
    args = parser.parse_args()

    brain = Path(args.brain_path).resolve()
    heart = Path(args.heart_path).resolve()

    if not brain.exists():
        print(f"ERROR: brain path does not exist: {brain}", file=sys.stderr)
        return 1

    mode = "DRY RUN" if args.dry_run else "MIGRATING"
    print(f"{mode}: Brain={brain} → Heart={heart}")

    copied, skipped = migrate(brain, heart, dry_run=args.dry_run)
    print(f"\nResult: {copied} migrated, {skipped} skipped")
    if args.dry_run:
        print("(dry run — no files written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
