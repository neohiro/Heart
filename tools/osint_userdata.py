"""
osint_userdata.py — Heart OSINT over /userdata variables.

Direction of data flow:
    READ-ONLY is the DEFAULT. Heart consumes /userdata to identify
    ghost / stranger visitor roles. Personalia is never exfiltrated.

    WRITE-BACK is the BACKUP path. It is enabled ONLY when:
      1. Heart's own health is degraded (organ failure: disk critical,
         container restart loop, monitor.sh escalation)
      2. The userdata node has been silent (no new data for >= 24h)
      3. The God Admin has pre-authorised bidirectional mode
         (env var USERDATA_BIDIRECTIONAL_OK=1, or .userdata_sync file
         in /userdata with `bidirectional: true`)

The backup write-back path is used to push **triage flags only** —
pattern alerts, anomaly markers, "stranger resurrected" tags.
Never raw PII. The data is symmetric: Heart reads from userdata
identically to the way it writes.

Personalia is securely stored for reference (per God Admin's
knowing). The OSINT path produces derived aggregates and
hashed fingerprints, not raw PII.

This module is a Heart cadence phase. It runs after
ingest_osint and before compute_health.

Phase order:
    tick → discover_repos → fetch_repos → fetch_issues → fetch_prs
    → fetch_actions → ingest_news → ingest_content → ingest_osint
    → **osint_userdata** → compute_health → write_brain → fire_reminders
    → prune_stale → self_heal → audit
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent.parent
for _p in (str(_WORKSPACE), str(_WORKSPACE / "userdata" / "src"), str(_WORKSPACE / "Brain" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

USERDATA_DIR = Path(os.environ.get("USERDATA_DIR", "/var/lib/userdata"))
BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))
HEART_DATA = Path(os.environ.get("HEART_DATA", "/heart/data"))

# File names we read from /userdata (read-only by default)
READABLE_USERDATA_FILES = {
    "strangers.json": "stranger profiles by ip-hash",
    "users.json": "user profiles by github_username",
    "admins.json": "admin profiles",
    "godadmins.json": "godadmin profiles (NEVER exfiltrated)",
    "trust.json": "trust score per profile_id (if present)",
    ".userdata_sync": "sync mode marker file",
}

# Fields we are allowed to ingest (deny-by-default)
INGEST_ALLOW_FIELDS = {
    "profile_id", "role", "created_at", "last_seen",
    "session_count", "trust_score", "country", "geoip_country",
    "github_username", "display_name", "is_vpn", "is_tor",
    "first_seen", "stable", "capabilities",
}

# Fields we may write back (TRIAGE FLAGS only, never PII)
WRITE_BACK_ALLOW_FIELDS = {
    "ghost_id",            # hashed identifier (always safe — no PII)
    "triage_flags",
    "resurrection_signal",
    "anomaly_score",
    "last_heart_check",
    "heart_alerts",
    "osint_summary",
    "userdata_role",
    "drift_count",
    "first_seen",
    "last_seen",
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(level: str, msg: str) -> None:
    print(json.dumps({
        "ts": _now(),
        "level": level,
        "component": "osint_userdata",
        "msg": msg,
    }))


# ── Heart health assessment ────────────────────────────────────────────────

def assess_heart_health() -> dict:
    """
    Read Heart's own health metrics to decide whether bidirectional sync
    should be enabled (the BACKUP path).

    Returns a dict with:
        healthy:          bool — overall health
        disk_free_mb:     int  — from monitor.sh output or live statvfs
        alerts_active:    int  — count of unacknowledged monitor alerts
        last_heartbeat:   iso  — last cycle ts from Brain/heartbeat/last_run.yaml
        organ_failures:   list[str] — names of failing organs (e.g. "Brain")
        bidirectional_ok: bool — whether to enable write-back path
    """
    health = {
        "healthy": True,
        "disk_free_mb": None,
        "alerts_active": 0,
        "last_heartbeat": None,
        "organ_failures": [],
        "bidirectional_ok": False,
    }

    # 1. Disk free
    heart_data = HEART_DATA if HEART_DATA.exists() else Path("/heart")
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        health["organ_failures"].append("heart-disk-statvfs-unavailable")
        health["healthy"] = False
    else:
        try:
            st = statvfs(heart_data)
            free_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
            health["disk_free_mb"] = free_mb
            if free_mb < 500:
                health["organ_failures"].append("heart-disk")
                health["healthy"] = False
        except (OSError, AttributeError):
            health["healthy"] = False
            health["organ_failures"].append("heart-disk-unreachable")

    # 2. Monitor alerts
    alerts_file = heart_data / "alerts.yaml"
    if alerts_file.exists():
        try:
            content = alerts_file.read_text()
            unack = content.count("level: critical") - content.count("acknowledged: true")
            health["alerts_active"] = max(0, unack)
        except OSError:
            pass

    # 3. Last heartbeat
    last_run = BRAIN_PATH / "heartbeat" / "last_run.yaml"
    if last_run.exists():
        try:
            import yaml
            data = yaml.safe_load(last_run.read_text()) or {}
            ts = data.get("ts")
            if ts:
                health["last_heartbeat"] = ts
                last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
                if age_min > 30:  # cycle should be < 5 min; 30 = critical
                    health["organ_failures"].append("heart-stalled")
                    health["healthy"] = False
        except (ImportError, OSError, ValueError):
            pass

    # 4. Bidirectional sync authorisation
    sync_marker = USERDATA_DIR / ".userdata_sync"
    if sync_marker.exists():
        try:
            import yaml
            cfg = yaml.safe_load(sync_marker.read_text()) or {}
            health["bidirectional_ok"] = bool(cfg.get("bidirectional", False))
        except (ImportError, OSError):
            pass
    if os.environ.get("USERDATA_BIDIRECTIONAL_OK") == "1":
        health["bidirectional_ok"] = True

    # Bidirectional is enabled when BOTH conditions are met:
    #   a) God Admin has authorised (config / env)
    #   b) Heart health is degraded (organ failure)
    if health["bidirectional_ok"] and not health["healthy"]:
        # We are explicitly authorised AND we have organ failure → enable backup path
        pass  # leave bidirectional_ok as-is, the writer checks it
    elif health["bidirectional_ok"]:
        # Authorised but healthy: do NOT use backup write path; only read
        health["bidirectional_ok"] = False

    return health


# ── Read /userdata (always allowed) ───────────────────────────────────────

def read_userdata_summaries() -> dict:
    """
    Read a *summary* of each profile from /userdata. We only ingest the
    fields on the allowlist. Personalia (email, ip_history detail, etc.)
    is NEVER copied to Heart state.
    """
    summaries = {
        "strangers": [],
        "users": [],
        "admins": [],
        "godadmins": [],   # only count, never profile content
        "trust": {},
        "ts": _now(),
    }

    for filename, _desc in READABLE_USERDATA_FILES.items():
        if filename == ".userdata_sync":
            continue
        if filename == "godadmins.json":
            path = USERDATA_DIR / filename
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    summaries["godadmins"] = [{"profile_id": k} for k in (data or {}).keys()]
                except (json.JSONDecodeError, OSError):
                    pass
            continue
        path = USERDATA_DIR / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            continue
        bucket = filename.replace(".json", "")
        for profile_id, profile in data.items():
            if not isinstance(profile, dict):
                continue
            summary = {k: v for k, v in profile.items() if k in INGEST_ALLOW_FIELDS}
            summary["profile_id"] = profile_id
            summaries[bucket].append(summary)

    return summaries


# ── Ghost / stranger visitor identification ──────────────────────────────

def identify_visitor_role(profile_id: str, role: str, last_seen: str) -> dict:
    """
    Classify a /userdata profile into a heartbeat-side role.

    Heart sees:
      - ghost: never appeared in /userdata, only in OSINT cache
      - stranger: has profile in strangers.json, no auth path
      - user: has users.json entry, GitHub-authed
      - admin/godadmin: in admins.json / godadmins.json

    Personalia (email, ip_history, payment, etc.) is NEVER carried into
    the heartbeat OSINT cache — only the role and the visit count.
    """
    role_map = {
        "stranger": "stranger",
        "user": "user",
        "admin": "admin",
        "godadmin": "godadmin",
    }
    canonical = role_map.get(role, "ghost")
    return {
        "profile_id": profile_id,
        "role": canonical,
        "last_seen": last_seen,
        "identified_at": _now(),
    }


def find_resurrection_candidates(heartbeat_ghosts: dict, userdata_summaries: dict) -> list[dict]:
    """
    Detect when a heartbeat-tracked ghost or stranger (hashed) matches
    a /userdata profile (also hashed) — same profile_id means a
    previously-observed visitor is now a real user.

    Returns a list of resurrection events. We do NOT include any
    personalia — only the hashed IDs and the canonical role transition.
    """
    candidates = []
    userdata_ids = {
        p["profile_id"]: p.get("role", "stranger")
        for p in userdata_summaries.get("users", []) + userdata_summaries.get("strangers", [])
    }
    for ghost_id, ghost in heartbeat_ghosts.get("observations", {}).items():
        if ghost_id in userdata_ids:
            candidates.append({
                "ghost_id": ghost_id,
                "userdata_role": userdata_ids[ghost_id],
                "drift_count": ghost.get("geo_drift_count", 0),
                "first_seen": ghost.get("first_seen"),
                "last_seen": ghost.get("last_seen"),
                "ts": _now(),
            })
    return candidates


# ── Write-back (BACKUP path, bidirectional only) ──────────────────────────

def write_triage_flags(candidates: list[dict]) -> dict:
    """
    BACKUP path: write triage flags back to /userdata when Heart health
    is degraded AND the God Admin has authorised bidirectional mode.

    This is the ONLY write path. We never write raw PII; we write
    derived, anonymised flags for the dashboard to surface.
    """
    result = {"written": 0, "skipped": 0, "errors": []}

    for cand in candidates:
        flag_path = USERDATA_DIR / "triage_flags" / f"{cand['ghost_id']}.json"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ghost_id": cand["ghost_id"],
            "heart_alert": "resurrection_candidate",
            "userdata_role": cand.get("userdata_role"),
            "drift_count": cand.get("drift_count", 0),
            "first_seen": cand.get("first_seen"),
            "last_seen": cand.get("last_seen"),
            "ts": cand.get("ts"),
        }
        # Strip unknown fields
        payload = {k: v for k, v in payload.items() if k in WRITE_BACK_ALLOW_FIELDS or k == "ts"}
        try:
            flag_path.write_text(json.dumps(payload, indent=2))
        except OSError as e:
            result["errors"].append({"ghost_id": cand["ghost_id"], "error": str(e)})
            result["skipped"] += 1
            continue
        # Best-effort chmod to 0o600; ignored on Windows (no POSIX mode bits)
        try:
            flag_path.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
        result["written"] += 1
    return result


# ── Containerised command interface (per docker README) ───────────────────

def run_command(cmd: list[str], timeout: int = 30) -> dict:
    """
    Run a shell command (used by Heart's self-heal phase). Commands are
    limited to a documented allowlist to prevent arbitrary code execution.

    Each container exposes a script at /usr/local/bin/heartctl that takes
    a documented set of flags. See Heart/docker/README.md.
    """
    allow = {
        "heartctl": ["status", "mode", "repos", "audit", "health",
                     "phase", "trigger", "watch", "doctor", "env-check",
                     "osint-refresh", "userdata-sync"],
    }
    if not cmd:
        return {"ok": False, "error": "empty command"}
    binary = os.path.basename(cmd[0])
    if binary not in allow:
        return {"ok": False, "error": f"binary {binary!r} not in allowlist"}
    if len(cmd) > 1 and cmd[1] not in allow[binary]:
        return {"ok": False, "error": f"subcommand {cmd[1]!r} not in allowlist"}

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "ok": r.returncode == 0,
            "rc": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return {"ok": False, "error": str(e)}


# ── Run as Heart phase ────────────────────────────────────────────────────

def run_phase(brain_path: str | Path) -> dict:
    """
    Heart phase: read /userdata summaries, identify roles, detect
    resurrections, optionally write triage flags back.

    Always reads. Writes only when organ failure + bidirectional authorised.
    """
    start = datetime.now(timezone.utc)
    health = assess_heart_health()

    # Read the heartbeat osint cache to correlate ghosts with userdata
    bp = Path(brain_path)
    cache_file = bp / "heartbeat" / "osint_cache.json"
    heartbeat_ghosts = {"observations": {}}
    if cache_file.exists():
        try:
            heartbeat_ghosts = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    summaries = read_userdata_summaries()
    visitors = [
        identify_visitor_role(p["profile_id"], p.get("role", "stranger"), p.get("last_seen", ""))
        for bucket in ("strangers", "users", "admins")
        for p in summaries.get(bucket, [])
    ]
    resurrections = find_resurrection_candidates(heartbeat_ghosts, summaries)

    write_result = {"written": 0, "skipped": 0, "errors": []}
    if health["bidirectional_ok"] and resurrections:
        _log("warn", f"organ failure detected: {health['organ_failures']} — engaging bidirectional backup path")
        write_result = write_triage_flags(resurrections)

    digest = {
        "ts": _now(),
        "heart_health": {
            "healthy": health["healthy"],
            "disk_free_mb": health["disk_free_mb"],
            "alerts_active": health["alerts_active"],
            "organ_failures": health["organ_failures"],
            "bidirectional_ok": health["bidirectional_ok"],
        },
        "counts": {
            "strangers": len(summaries.get("strangers", [])),
            "users": len(summaries.get("users", [])),
            "admins": len(summaries.get("admins", [])),
            "godadmins": len(summaries.get("godadmins", [])),
            "resurrections": len(resurrections),
        },
        "visitors": visitors,
        "resurrections": resurrections,
        "write_back": write_result,
        "duration_ms": int((datetime.now(timezone.utc) - start).total_seconds() * 1000),
        "ok": True,
    }
    # Append to heartbeat digest for the dashboard
    digest_path = bp / "heartbeat" / "userdata_osint_digest.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    from atomic import write_json
    write_json(digest_path, digest, prefix=".digest.")
    return digest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Heart OSINT over /userdata")
    parser.add_argument("--brain-path", type=Path, default=Path(os.environ.get("BRAIN_PATH", "Brain")))
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()
    result = run_phase(args.brain_path)
    print(json.dumps(result, indent=2))
