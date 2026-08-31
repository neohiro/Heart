#!/usr/bin/env python3
"""
memory_bridge_heart.py — bridge encrypted user memory from neohiro/userdata to Brain.

Canonical source: neohiro/userdata/data/users/<login>/*.age
Destination:       /shared/brain/_entities/user/<login>.yaml

This script runs every 5 minutes (per HEART_SCHEDULE_REGISTRY.md).
It:
  1. Enumerates all age-encrypted profiles in neohiro/userdata
  2. Decrypts with the operator's identity key
  3. Projects the resolved entity (tone, interests, locale, geo, authority) into
     /shared/brain/_entities/user/<login>.yaml for Brain to use
  4. Also writes the resolved tone to /shared/mouth/context/<login>_tone.yaml
     so Mouth can inject the user's voice without decrypting anything

Reads from:  USERDATA_DIR, USERDATA_IDENTITY_DIR, GODADMIN_IDENTITY, BRAIN_ENTITY_DIR
Writes to:   /shared/brain/_entities/user/<login>.yaml
             /shared/mouth/context/<login>_tone.yaml  (plaintext tone projection)

Usage:
    python Heart/tools/memory_bridge_heart.py --once
    python Heart/tools/memory_bridge_heart.py --once --dry-run
    # In continuous mode (from Heart cycle):
    python Heart/tools/memory_bridge_heart.py --continuous

Environment:
    USERDATA_DIR              Root of neohiro/userdata (default: /var/lib/userdata)
    USERDATA_IDENTITY_DIR     Identity keys (default: /var/lib/userdata/identities)
    GODADMIN_IDENTITY         Path to godadmin age secret key file (for godadmin decryption)
    BRAIN_ENTITY_DIR          Brain entity dir (default: /shared/brain/_entities)
    MOUTH_CONTEXT_DIR         Mouth context dir (default: /shared/mouth/context)
    NEOHIRO_LOG_LEVEL         debug|info|warn|error
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

USERDATA_DIR = Path(os.environ.get("USERDATA_DIR", "/var/lib/userdata"))
IDENTITY_DIR = Path(os.environ.get("USERDATA_IDENTITY_DIR", "/var/lib/userdata/identities"))
GODADMIN_IDENTITY = os.environ.get("GODADMIN_IDENTITY", "")
BRAIN_ENTITY_DIR = Path(os.environ.get("BRAIN_ENTITY_DIR", "/shared/brain/_entities"))
MOUTH_CONTEXT_DIR = Path(os.environ.get("MOUTH_CONTEXT_DIR", "/shared/mouth/context"))
LOG_LEVEL = os.environ.get("NEOHIRO_LOG_LEVEL", "info")

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        {"debug": 10, "info": 20, "warn": 30, "error": 40}.get(LOG_LEVEL, 20)
    ),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
    cache_logger_on_first_use=False,
)
log = structlog.get_logger()


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── age I/O ─────────────────────────────────────────────────────────

def _decrypt_age(path: Path, identities: list[str]) -> bytes | None:
    """Try age decryption with one or more identities. Returns plaintext or None."""
    if not path.exists():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        pt = Path(tmp) / "plaintext"
        for i, ident in enumerate(identities):
            cmd = ["age", "-d", "-i", ident, "-o", str(pt), str(path)]
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0:
                return pt.read_bytes()
        return None


def _list_user_dirs() -> list[tuple[str, Path]]:
    """Return [(login, profile_yaml_age_path)] for all encrypted user profiles."""
    users_dir = USERDATA_DIR / "users"
    if not users_dir.is_dir():
        return []
    out = []
    for login_dir in users_dir.iterdir():
        if not login_dir.is_dir():
            continue
        profile = login_dir / "profile.yaml.age"
        if profile.exists():
            out.append((login_dir.name, profile))
    return out


# ─── Projectors ─────────────────────────────────────────────────────

def _project_to_brain_entity(login: str, profile: dict[str, Any]) -> dict[str, Any]:
    """
    Project the decrypted userdata profile into a Brain entity YAML frontmatter.

    Maps userdata fields → Brain entity fields per USERDATA_SUMMARIES.md § 7.
    The projection is lossless for the fields Brain cares about.
    """
    warmth_map = {
        "friendly": "+", "warm": "+", "energetic": "++",
        "neutral": "+-", "cold": "-", "formal": "--",
    }
    posture_map = {
        "casual": "casual", "relaxed": "casual", "professional": "formal",
        "formal": "formal", "playful": "playful", "guarded": "guarded",
    }
    enthusiasm_map = {
        "enthusiastic": "++", "engaged": "+", "neutral": "+-",
        "reserved": "-", "minimal": "--",
    }

    authority = profile.get("authority", "login")
    warmth = warmth_map.get(str(profile.get("warmth", "neutral")), "+-")
    posture = posture_map.get(str(profile.get("posture", "casual")), "casual")
    enthusiasm = enthusiasm_map.get(str(profile.get("enthusiasm", "neutral")), "+-")

    tone = profile.get("tone", {})
    interests = profile.get("interests", [])
    locale = profile.get("locale", {})
    geo = profile.get("geo", {})

    # Build YAML frontmatter
    frontmatter = {
        "type": "user",
        "id": f"user:{login}",
        "github_login": login,
        "authority": authority,
        "warmth": warmth,
        "posture": posture,
        "enthusiasm": enthusiasm,
        "identity": {
            "display_name": profile.get("display_name", login),
            "language": locale.get("language", "en"),
            "timezone": locale.get("timezone", "UTC"),
            "country": locale.get("country", geo.get("country", "")),
        },
        "tone": {
            "style": tone.get("style", "concise"),
            "verbosity": tone.get("verbosity", "medium"),
            "humor": tone.get("humor", "dry"),
            "emoji": tone.get("emoji", "sparing"),
            "language": tone.get("language", locale.get("language", "en")),
        },
        "interests": interests if isinstance(interests, list) else [],
        "schema_version": 1,
        "updated": _iso_now(),
        "source": "userdata_memory_bridge",
    }

    # Free-form prose block
    display_name = profile.get("display_name", login)
    name_prose = f"\n# {display_name}\n\n"
    if interests:
        iana = ", ".join(interests[:8])
        name_prose += f"**Interests:** {iana}\n\n"
    if tone:
        style = tone.get("style", "concise")
        humor = tone.get("humor", "dry")
        name_prose += f"**Style:** {style}, {humor} humor. "
    name_prose += f"Authority: {authority}. Updated {_iso_now()}.\n"

    return {"frontmatter": frontmatter, "prose": name_prose}


def _project_to_mouth_tone(login: str, profile: dict[str, Any]) -> dict[str, Any]:
    """
    Project a minimal tone block for Mouth's context injection.
    Written to /shared/mouth/context/<login>_tone.yaml — plaintext, no PII,
    no age-encryption needed since it's user preference only.
    """
    tone = profile.get("tone", {})
    locale = profile.get("locale", {})
    interests = profile.get("interests", [])
    if isinstance(interests, list):
        top = interests[:5]
    else:
        top = []

    return {
        "login": login,
        "schema_version": 1,
        "style": tone.get("style", "concise"),
        "verbosity": tone.get("verbosity", "medium"),
        "humor": tone.get("humor", "dry"),
        "emoji": tone.get("emoji", "sparing"),
        "language": tone.get("language", locale.get("language", "en")),
        "timezone": locale.get("timezone", "UTC"),
        "top_interests": top,
        "updated": _iso_now(),
    }


def _write_yaml_atomic(path: Path, data: dict[str, Any], prose: str = "") -> None:
    """Atomic YAML write: stage → fsync → rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    front = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    with tempfile.NamedTemporaryFile(
        "w", delete=False, dir=str(path.parent), encoding="utf-8", suffix=".tmp"
    ) as tmp:
        tmp.write("---\n")
        tmp.write(front)
        if prose:
            tmp.write(prose)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, str(path))


def _identity_key_for(login: str) -> list[str]:
    """Return list of age identity file paths to try for a login."""
    key = IDENTITY_DIR / f"{login}.key.txt"
    keys = []
    if key.exists():
        keys.append(str(key))
    if GODADMIN_IDENTITY:
        keys.append(GODADMIN_IDENTITY)
    return keys


# ─── Main ─────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge encrypted user memory to Brain entities")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--continuous", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without writing")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between cycles (default: 300)")
    parser.add_argument("--login", help="Process only this login")
    args = parser.parse_args()

    log.info("memory_bridge_start", userdata_dir=str(USERDATA_DIR),
              brain_dir=str(BRAIN_ENTITY_DIR), dry_run=args.dry_run)

    users = _list_user_dirs()
    log.info("memory_bridge_users_found", count=len(users))

    processed = 0
    errors = 0

    for login, profile_path in users:
        if args.login and login != args.login:
            continue

        identities = _identity_key_for(login)
        if not identities:
            log.warn("no_identity_key", login=login)
            errors += 1
            continue

        raw = _decrypt_age(profile_path, identities)
        if raw is None:
            log.warn("decrypt_failed", login=login, path=str(profile_path))
            errors += 1
            continue

        try:
            profile = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warn("profile_parse_failed", login=login)
            errors += 1
            continue

        # Project to Brain entity
        brain_proj = _project_to_brain_entity(login, profile)
        brain_path = BRAIN_ENTITY_DIR / "user" / f"{login}.yaml"
        if not args.dry_run:
            _write_yaml_atomic(brain_path, brain_proj["frontmatter"], brain_proj["prose"])

        # Project tone to Mouth context
        tone_proj = _project_to_mouth_tone(login, profile)
        tone_path = MOUTH_CONTEXT_DIR / f"{login}_tone.yaml"
        if not args.dry_run:
            _write_yaml_atomic(tone_path, tone_proj)

        log.info("memory_bridge_user_done", login=login,
                 authority=profile.get("authority", "?"),
                 interests=len(profile.get("interests", [])),
                 brain_path=str(brain_path),
                 tone_path=str(tone_path))
        processed += 1

    log.info("memory_bridge_cycle_done", processed=processed, errors=errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
