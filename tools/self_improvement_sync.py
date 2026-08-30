"""
self_improvement_sync.py — Heart's gated self-improvement sync

Heart pumps self-improvement commits (KB updates, link refreshes, agentic
repo seeds) into a configurable set of repos. To avoid producing confusing
recommits that race against a user's in-flight changes, this module adds
a session-aware cooldown.

Inputs (per Heart cycle):
  - active_session    — /Mouth signals "user is coding right now" (recent < N min)
  - recent_user_push  — git log of the target repo shows user-authored commits in the last M min
  - recent_other_push — git log shows commits by another coder in the last M min
  - manual_request    — admin or godadmin triggered a re-run

Decisions:
  - ACTIVE_USER_SESSION    block ALL self-improvement commits (avoid races)
  - RECENT_USER_PUSH       block commit to that repo (don't fight the user)
  - RECENT_OTHER_PUSH      retrigger cooldown — other coder's work is signal of change
  - NONE                   run normally

The cooldown is per-repo. When triggered, the repo is locked for COOLDOWN_MIN
unless a new external change or manual request resets it. A heartbeat tick
with no changes does NOT reset the cooldown (it just doesn't trigger work).

Outputs:
  - Heart inbox entry: /Brain/heartbeat/self_improvement/<repo>.lock.yaml
  - Decision log:      /Brain/audit/self_improvement.yaml
  - Briefing injection: passed to admin_briefing() on dashboard

This module is a no-op for non-git repos. It is safe to import before
the actual git CLI is available (it shells out and catches errors).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))

# Tunable cooldowns
ACTIVE_SESSION_COOLDOWN_MIN = 15
RECENT_USER_PUSH_COOLDOWN_MIN = 30
RECENT_OTHER_PUSH_COOLDOWN_MIN = 5
GIT_LOG_WINDOW_MIN = 15

# Cached at module level to avoid spawning 6 subprocess calls per repo
_USER_IDS: Optional[set[str]] = None


@dataclass
class SyncDecision:
    repo: str
    action: str              # "commit" | "skip" | "cooldown" | "manual_force"
    reason: str
    cooldown_until: Optional[str] = None
    last_seen: Optional[str] = None
    last_user_push: Optional[str] = None
    last_other_push: Optional[str] = None
    active_session: bool = False
    manual_request: bool = False
    ts: str = ""

    def to_dict(self) -> dict:
        return self.__dict__


# ── Git introspection ────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_git(repo_path: Path, *args: str, timeout: int = 10) -> Optional[str]:
    """Run a git command and return stdout, or None on error."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path)] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return None


def _repo_root(path: str) -> Optional[Path]:
    """Find the git repo root for `path` (file or dir). Returns None if not git."""
    p = Path(path)
    if not p.exists():
        return None
    if p.is_file():
        p = p.parent
    root = _run_git(p, "rev-parse", "--show-toplevel")
    if root:
        return Path(root)
    # Try parent
    parent = p.parent if p.parent != p else None
    if parent:
        root = _run_git(parent, "rev-parse", "--show-toplevel")
        if root:
            return Path(root)
    return None


def recent_commits(repo_root: Path, since_iso: str) -> list[dict]:
    """
    Return commits since `since_iso` with author + email + timestamp.
    Format: [{ sha, author, email, ts, subject }]
    """
    out = _run_git(
        repo_root,
        "log",
        f"--since={since_iso}",
        "--pretty=format:%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
        "--no-merges",
    )
    if not out:
        return []

    commits = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\x1f", 4)
        if len(parts) < 5:
            continue
        sha, author, email, ts, subject = parts
        commits.append({
            "sha": sha,
            "author": author,
            "email": email,
            "ts": ts,
            "subject": subject,
        })
    return commits


def _user_identities() -> set[str]:
    """
    Read the local + global git config to identify the user.
    Cached at module level — only one subprocess call per scope per process.
    """
    global _USER_IDS
    if _USER_IDS is not None:
        return _USER_IDS
    ids = set()
    for scope in ("--global", "--local", ""):
        for key in ("user.name", "user.email"):
            args = ["git", "config"]
            if scope:
                args.append(scope)
            args.append(key)
            try:
                out = subprocess.run(args, capture_output=True, text=True, timeout=3).stdout.strip()
                if out:
                    ids.add(out.lower())
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
    _USER_IDS = ids
    return ids


# ── Session detection ─────────────────────────────────────────────────────

def detect_active_session() -> bool:
    """
    Check /Mouth for an in-flight user session.
    Returns True if a session started within ACTIVE_SESSION_COOLDOWN_MIN minutes.

    Implementation: checks /Brain/heartbeat/sessions/active.yaml
    (written by /Mouth) for any session with `last_input_at` within window.
    """
    sessions = BRAIN_PATH / "heartbeat" / "sessions" / "active.yaml"
    if not sessions.exists():
        return False
    try:
        import yaml
        data = yaml.safe_load(sessions.read_text())
    except (ImportError, Exception):
        # Try plain JSON fallback
        try:
            data = json.loads(sessions.read_text())
        except Exception:
            return False
    if not isinstance(data, dict):
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ACTIVE_SESSION_COOLDOWN_MIN)
    for sess in data.get("sessions", []):
        last = sess.get("last_input_at", "")
        try:
            t = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if t > cutoff:
            return True
    return False


# ── Cooldown lock file ───────────────────────────────────────────────────

def _lock_path(repo: str) -> Path:
    safe = repo.replace("/", "_").replace(":", "_")
    return BRAIN_PATH / "heartbeat" / "self_improvement" / f"{safe}.lock.yaml"


def _read_lock(repo: str) -> Optional[dict]:
    p = _lock_path(repo)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_lock(repo: str, payload: dict) -> None:
    p = _lock_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2))


def _is_in_cooldown(repo: str) -> bool:
    lock = _read_lock(repo)
    if not lock:
        return False
    until = lock.get("cooldown_until")
    if not until:
        return False
    try:
        t = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    return t > datetime.now(timezone.utc)


def _set_cooldown(repo: str, minutes: int, reason: str) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    _write_lock(repo, {
        "repo": repo,
        "cooldown_until": until.isoformat(),
        "reason": reason,
        "set_at": _now(),
    })


# ── Main decision function ───────────────────────────────────────────────

def decide_sync(
    repo: str,
    *,
    manual_request: bool = False,
) -> SyncDecision:
    """
    Decide whether to commit a self-improvement update to `repo`.

    Returns a SyncDecision with `action`:
      - "commit"        — proceed with the self-improvement commit
      - "skip"          — cooldown active; skip silently
      - "cooldown"      — cooldown active; will retry after the cooldown window
      - "manual_force"  — manual request overrides cooldown; proceed
    """
    now = _now()
    decision = SyncDecision(
        repo=repo,
        action="commit",
        reason="no blockers",
        ts=now,
        manual_request=manual_request,
    )

    # Manual request always forces a commit (and resets cooldown)
    if manual_request:
        decision.action = "manual_force"
        decision.reason = "admin or godadmin manual request"
        _write_lock(repo, {"repo": repo, "cooldown_until": None, "reason": "manual", "set_at": now})
        return decision

    # Check active session
    if detect_active_session():
        decision.action = "cooldown"
        decision.reason = "user has an active session — hold off to avoid recommit races"
        decision.active_session = True
        _set_cooldown(repo, ACTIVE_SESSION_COOLDOWN_MIN, decision.reason)
        return decision

    # Check existing cooldown
    if _is_in_cooldown(repo):
        decision.action = "skip"
        decision.reason = "cooldown active; will retry after window"
        lock = _read_lock(repo)
        if lock:
            decision.cooldown_until = lock.get("cooldown_until")
        return decision

    # Check recent git history for user/other activity
    repo_root = _repo_root(repo)
    since = (datetime.now(timezone.utc) - timedelta(minutes=GIT_LOG_WINDOW_MIN)).isoformat()

    last_user_push = None
    last_other_push = None
    if repo_root:
        user_ids = _user_identities()
        commits = recent_commits(repo_root, since)
        for c in commits:
            who = (c.get("author", "") + " " + c.get("email", "")).lower()
            if any(uid in who for uid in user_ids):
                last_user_push = c
                break
        for c in commits:
            who = (c.get("author", "") + " " + c.get("email", "")).lower()
            if not any(uid in who for uid in user_ids):
                last_other_push = c
                break

    decision.last_user_push = last_user_push["ts"] if last_user_push else None
    decision.last_other_push = last_other_push["ts"] if last_other_push else None
    decision.last_seen = decision.last_other_push or decision.last_user_push

    # Recent user push — block (don't fight the user)
    if last_user_push:
        decision.action = "cooldown"
        decision.reason = f"user pushed {last_user_push['sha'][:7]} at {last_user_push['ts']} — wait for them to finish"
        _set_cooldown(repo, RECENT_USER_PUSH_COOLDOWN_MIN, decision.reason)
        return decision

    # Recent push by another coder — retrigger a short cooldown and re-evaluate next cycle
    if last_other_push:
        decision.action = "cooldown"
        decision.reason = f"another coder pushed {last_other_push['sha'][:7]} at {last_other_push['ts']} — wait for KB reconcile"
        _set_cooldown(repo, RECENT_OTHER_PUSH_COOLDOWN_MIN, decision.reason)
        return decision

    # All clear
    decision.action = "commit"
    decision.reason = "no recent activity; safe to commit"
    return decision


# ── Heart cycle phase wrapper ────────────────────────────────────────────

def run_phase(target_repos: list[str], *, manual: bool = False) -> dict:
    """
    Heart phase entry: evaluate every target repo and emit a structured
    decision log + cooldowns. Does not perform the actual commits.
    Caller (Heart) reads decisions and only commits where action='commit'.
    """
    decisions = [decide_sync(r, manual_request=manual) for r in target_repos]
    audit = {
        "ts": _now(),
        "manual": manual,
        "decisions": [d.to_dict() for d in decisions],
        "committable": [d.repo for d in decisions if d.action in ("commit", "manual_force")],
        "blocked": [d.repo for d in decisions if d.action in ("cooldown", "skip")],
    }

    # Append to audit log
    audit_path = BRAIN_PATH / "audit" / "self_improvement.yaml"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(audit) + "\n")

    return audit


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Heart self-improvement sync gate")
    p.add_argument("repos", nargs="+", help="repos to evaluate")
    p.add_argument("--manual", action="store_true", help="manual force")
    p.add_argument("--brain-path", type=Path, default=Path(os.environ.get("BRAIN_PATH", "Brain")))
    args = p.parse_args()

    BRAIN_PATH = args.brain_path
    result = run_phase(args.repos, manual=args.manual)
    print(json.dumps(result, indent=2))
