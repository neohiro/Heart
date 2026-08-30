"""
Heart/docker/role_history.py — role transition logic with ghost-promotion.

Records every role change in /userdata/<login>/role_history.yaml.
Handles the ghost-promotion rule: when a stranger becomes a login, the
ghost label is preserved so the original anonymous footprint remains accessible
only to admins, not merged into the authenticated identity without consent.

Usage:
    python role_history.py --login wout --to-role authorized
    python role_history.py --login wout --to-role developer --reason "sidejob"
    python role_history.py --login wout --show-history

Exit codes: 0 = success, 1 = error
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USERDATA_ROOT = Path(os.environ.get("USERDATA_PATH", "/userdata"))


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


VALID_ROLES = {"stranger", "login", "authorized", "admin", "developer"}
VALID_REASONS = {
    "first_visit",
    "github_oauth_linked",
    "github_readback_verified",
    "admin_promotion",
    "admin_demotion",
    "sidejob",
    "developer_demotion",
    "deauth",
}

# Map role transitions to semantically valid reasons. Used to reject
# nonsensical transitions like login->admin with reason="sidejob".
REASON_FOR_TRANSITION: dict[tuple[str, str], set[str]] = {
    ("stranger", "login"):      {"github_oauth_linked"},
    ("login", "authorized"):    {"github_readback_verified"},
    ("authorized", "admin"):    {"admin_promotion"},
    ("admin", "authorized"):    {"admin_demotion"},
    ("authorized", "developer"):{"sidejob"},
    ("admin", "developer"):     {"sidejob"},
    ("developer", "authorized"):{"developer_demotion"},
    ("developer", "admin"):     {"developer_demotion"},
    # demotions
    ("login", "stranger"):      {"deauth"},
    ("authorized", "stranger"): {"deauth"},
    ("admin", "stranger"):      {"deauth"},
    ("developer", "stranger"):  {"deauth"},
    # same-role (e.g. re-verify)
    ("login", "login"):         {"github_oauth_linked", "github_readback_verified"},
    ("authorized", "authorized"):{"github_readback_verified"},
}


def _userdata_path(login: str) -> Path:
    return USERDATA_ROOT / login / "role_history.yaml"


def _read_role_history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        import yaml
        return yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError as e:
        print(f"ERROR: malformed role_history.yaml at {path}: {e}", file=sys.stderr)
        return []
    except OSError as e:
        print(f"ERROR: read failed for {path}: {e}", file=sys.stderr)
        return []


def _write_role_history(path: Path, history: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        # Atomic write: write to tmp then rename so partial files never appear.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(yaml.dump(history, default_flow_style=False))
        tmp.replace(path)
    except OSError as e:
        print(f"ERROR: write failed: {e}", file=sys.stderr)
        raise SystemExit(1)


def _current_role(history: list[dict[str, Any]]) -> str | None:
    if not history:
        return None
    return history[-1].get("role")


def cmd_show(login: str) -> int:
    path = _userdata_path(login)
    history = _read_role_history(path)
    if not history:
        print(f"No role history for {login}")
        return 0
    print(f"=== Role history: {login} ===")
    for entry in history:
        ghost = entry.get("ghost", False)
        print(
            f"  {entry.get('at', '?')}  "
            f"{entry.get('role', '?'):<12}"
            f"{'(ghost)' if ghost else ''}"
            f"  reason={entry.get('reason', '?')}"
        )
    return 0


def cmd_transition(login: str, to_role: str, reason: str = "") -> int:
    if to_role not in VALID_ROLES:
        print(f"ERROR: invalid role '{to_role}'. Valid: {', '.join(sorted(VALID_ROLES))}", file=sys.stderr)
        return 1
    if reason and reason not in VALID_REASONS:
        print(f"WARNING: reason '{reason}' not in known set; using anyway", file=sys.stderr)

    path = _userdata_path(login)
    path.parent.mkdir(parents=True, exist_ok=True)
    history = _read_role_history(path)
    from_role = _current_role(history) or "stranger"

    # Semantic check: reject nonsensical reason/transition combos unless --force.
    if reason and not reason.startswith("__force_"):
        allowed = REASON_FOR_TRANSITION.get((from_role, to_role))
        if allowed is not None and reason not in allowed:
            print(
                f"ERROR: reason '{reason}' is not valid for transition "
                f"{from_role} -> {to_role}. Expected one of: {sorted(allowed)}",
                file=sys.stderr,
            )
            return 1

    ghost = False
    if from_role == "stranger" and to_role == "login":
        ghost = True

    new_entry: dict[str, Any] = {
        "role": to_role,
        "at": _iso_now(),
        "reason": reason or "transition",
    }
    if ghost:
        new_entry["ghost"] = True
    else:
        new_entry["ghost"] = False

    history.append(new_entry)
    _write_role_history(path, history)
    print(
        f"{login}: {from_role} -> {to_role}"
        + (f" (ghost preserved)" if ghost else "")
        + f" [{reason or 'transition'}]"
    )
    return 0


def cmd_current(login: str) -> int:
    path = _userdata_path(login)
    history = _read_role_history(path)
    role = _current_role(history)
    print(role or "unknown")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="role_history — role transition with ghost-promotion")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="show full role history")
    show.add_argument("--login", required=True, help="GitHub login")

    trans = sub.add_parser("transition", help="record a role transition")
    trans.add_argument("--login", required=True, help="GitHub login")
    trans.add_argument("--to-role", required=True, choices=list(VALID_ROLES), help="new role")
    trans.add_argument("--reason", default="", help="reason (optional)")

    cur = sub.add_parser("current", help="print current role")
    cur.add_argument("--login", required=True, help="GitHub login")

    args = parser.parse_args()

    if args.command == "show":
        return cmd_show(args.login)
    elif args.command == "transition":
        return cmd_transition(args.login, args.to_role, args.reason)
    elif args.command == "current":
        return cmd_current(args.login)
    return 1


if __name__ == "__main__":
    sys.exit(main())
