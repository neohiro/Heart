#!/usr/bin/env python3
"""
channel-audit/run.py — emit the full role × channel gate matrix.

Writes the JSON matrix to NEOHIRO_AUDIT_OUT for Doctor to consume.
Run every 6h per Heart/schedules/REGISTRY.yaml.

Usage:
  python channel-audit/run.py [--out /shared/heart/audit/channel_matrix.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("channel_audit")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.environ.get(
        "NEOHIRO_AUDIT_OUT", "/shared/heart/audit/channel_matrix.json"
    ))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    brain_src = os.environ.get("NEOHIRO_BRAIN_SRC", "Brain/src")
    if brain_src not in sys.path:
        sys.path.insert(0, brain_src)

    from scope_channels import audit_channel_matrix, public_channels

    matrix = audit_channel_matrix()
    matrix["public_channels"] = [c.value for c in public_channels()]
    matrix["generated_at"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(matrix, indent=2), encoding="utf-8")
    log.info("wrote channel matrix: %s (%d channels)", out_path, len(matrix["channels"]))

    if args.once:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())