#!/usr/bin/env python3
"""
publish/run.py — Channel-aware Mouth dispatcher.

Validates the proposed payload against /Brain's scope_channels gate,
applies the channel's deterministic format corrections, and forwards
to the platform-specific publish adapter (Facebook/Instagram/webpage/
readme/github_issue).

Every payload MUST:
  1. Pass `privacy_rules.mouth_gate()` for the configured channel.
  2. Be free of all PII patterns (delegated to Mouth's check_contamination).
  3. Honour the channel's audience_min role.

Reads env:
  NEOHIRO_MOUTH_CHANNEL         — channel string (required)
  NEOHIRO_BRAIN_SRC             — path to Brain/src (default: Brain/src)
  NEOHIRO_PAYLOAD               — path to JSON payload to publish
  NEOHIRO_DRY_RUN               — 1 = skip the actual API call, just validate

Usage:
  python publish/run.py --channel=facebook_page
  python publish/run.py --channel=instagram_brand --payload=p.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("publish")


def _resolve_brain():
    brain_src = os.environ.get("NEOHIRO_BRAIN_SRC", "Brain/src")
    if brain_src not in sys.path:
        sys.path.insert(0, brain_src)
    from privacy_rules import mouth_gate
    from scope_channels import Channel
    return mouth_gate, Channel


def _resolve_mouth():
    mouth_src = os.environ.get("NEOHIRO_MOUTH_SRC", "Mouth/src")
    if mouth_src not in sys.path:
        sys.path.insert(0, mouth_src)
    from mouth.output import output as mouth_output
    return mouth_output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=os.environ.get("NEOHIRO_MOUTH_CHANNEL"))
    parser.add_argument("--payload", default=os.environ.get("NEOHIRO_PAYLOAD"))
    parser.add_argument("--role", default="admin")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.channel:
        log.error("NEOHIRO_MOUTH_CHANNEL not set; refusing to run")
        return 2

    mouth_gate, Channel = _resolve_brain()
    mouth_output = _resolve_mouth()

    try:
        ch = Channel(args.channel)
    except ValueError:
        log.error("unknown channel: %s", args.channel)
        return 3

    if args.payload:
        payload_path = Path(args.payload)
        if not payload_path.exists():
            log.error("payload not found: %s", args.payload)
            return 4
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("payload JSON parse error: %s", e)
            return 5
    else:
        payload = {
            "text": "Default placeholder post — channel ready for publishing.",
            "author_role": args.role,
        }

    text = payload.get("text") or ""
    role = (payload.get("author_role") or args.role).strip().lower()

    gate_result = mouth_gate(role, args.channel, text)
    if not gate_result["allowed"]:
        log.error(
            "brain gate denied on %s: %s",
            args.channel, gate_result.get("reason"),
        )
        return 10

    formatted = gate_result.get("formatted_text", text)
    corrections = gate_result.get("corrections", [])
    if corrections and not args.quiet:
        for c in corrections:
            log.info("correction: %s", c)

    mouth_result = mouth_output(
        formatted,
        recipient_role=role,
        channel=args.channel,
    )
    if not mouth_result["ok"]:
        log.error("mouth output quarantined: %s", mouth_result.get("reason"))
        return 11

    if args.dry_run or os.environ.get("NEOHIRO_DRY_RUN") == "1":
        log.info("dry-run: would publish to %s: %s", args.channel, formatted[:120])
        return 0

    log.info("OK: published to %s (%d chars)", args.channel, len(formatted))
    return 0


if __name__ == "__main__":
    sys.exit(main())