#!/usr/bin/env python3
"""
github_notifications_heart.py — bridge GitHub webhook events into Brain awareness
and (per-user) into the encrypted notification store at neohiro/userdata.

This is the dispatcher wired into Heart via `_phase_ingest_github_notifications`
and registered in `Heart/schedules/REGISTRY.yaml` as `github-notifications`.
It is the canonical implementation referenced by iot/SPEC_GITHUB_NOTIFICATIONS.md
and the AGENTS.md "GitHub notifications → /Brain awareness" common-task entry.

Pipeline (per cycle):

    1. Read all unprocessed files from iot/cache/github/<file>.json
       (cursor lives in /var/lib/brain/state/github_state.json).
    2. For each event, take the pre-normalized `record["normalized"]`
       block written by iot.server (see iot/webhooks/github.py). If the
       normalization failed, we attempt `normalize_event()` once more in
       the dispatcher (covers Heart running without the iot server
       being aware of the new payload shape).
    3. Apply rate limiting (default 200 events/min/org) to prevent
       runaway backfills from saturating userdata writes.
    4. Route:
         - route_to_brain: append a per-repo delta to
           /var/lib/brain/awareness/github/<org>/<repo>.yaml.
         - route_to_userdata: if any tracked login is in
           `addresses.{author,assignees,requested_reviewers,mentioned}`,
           write an age-encrypted notification via
           userdata.memory_bridge.write_notification.
         - route_to_mouth: if the event passes the headline filter,
           append a one-line JSON object to
           /var/lib/mouth/headlines/github.jsonl.
    5. Mark the delivery_id processed in github_state.json and (unless
       --keep-cache) move the cache file to iot/cache/github/processed/.
    6. Emit an audit line to /var/lib/userdata/audit/github_notifications.jsonl.

Environment / paths (all overridable):
    IOT_CACHE_DIR              /var/lib/iot/cache
    BRAIN_AWARENESS_GITHUB_DIR /var/lib/brain/awareness/github
    BRAIN_STATE_DIR            /var/lib/brain/state
    USERDATA_DIR               /var/lib/userdata
    MOUTH_HEADLINES_DIR        /var/lib/mouth/headlines
    GITHUB_NOTIFY_RATE_PER_MIN 200          # per-org cap
    GITHUB_NOTIFY_PROCESSED_KEEP_FILES 1   # 1 = leave file in cache (default)
    GITHUB_TRACKED_LOGINS      wout,admin   # comma-separated; users to route userdata to

CLI:
    python Heart/tools/github_notifications_heart.py --once
    python Heart/tools/github_notifications_heart.py --once --dry-run
    python Heart/tools/github_notifications_heart.py --once --reset-processed
    python Heart/tools/github_notifications_heart.py --status
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Make sibling Heart/scripts/_lib importable
_HERE = Path(__file__).resolve().parent
for _p in (
    str(_HERE),
    str(_HERE.parent / "scripts" / "_lib"),
    str(_HERE.parent.parent / "Heart" / "scripts" / "_lib"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from heart_dispatch import setup_logging  # type: ignore
except Exception:  # pragma: no cover - fallback when lib missing
    import structlog

    def setup_logging(*, quiet: bool = False, level: str = "info") -> Any:  # type: ignore
        lvl = {"debug": 10, "info": 20, "warn": 30, "error": 40}.get(level.lower(), 20)
        if quiet:
            lvl = max(lvl, 30)
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(lvl),
            logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
            cache_logger_on_first_use=False,
        )
        return structlog.get_logger()


# ─── Paths & config ─────────────────────────────────────────────────────────

IOT_CACHE_DIR = Path(os.environ.get("IOT_CACHE_DIR", "/var/lib/iot/cache"))
BRAIN_AWARENESS_DIR = Path(
    os.environ.get("BRAIN_AWARENESS_GITHUB_DIR", "/var/lib/brain/awareness/github")
)
BRAIN_STATE_DIR = Path(os.environ.get("BRAIN_STATE_DIR", "/var/lib/brain/state"))
USERDATA_DIR = Path(os.environ.get("USERDATA_DIR", "/var/lib/userdata"))
MOUTH_HEADLINES_DIR = Path(
    os.environ.get("MOUTH_HEADLINES_DIR", "/var/lib/mouth/headlines")
)

# Dead-letter queue directory for events that were permanently skipped
# (e.g. unknown event type, oversized payload, normalization failed). Files
# here are NOT re-processed automatically; operators can inspect and replay
# them via the iot replay endpoint.
DLQ_DIR = Path(os.environ.get("GITHUB_NOTIFY_DLQ_DIR", str(BRAIN_STATE_DIR / "github_dlq")))

# ─── Constants ────────────────────────────────────────────────────────────────

# Maximum cache file size to read (prevents OOM on malicious/large payloads)
MAX_CACHE_FILE_BYTES = 1_000_000  # 1 MB

# Maximum number of cache files to process per cycle (prevents unbounded backlog)
MAX_FILES_PER_CYCLE = 500

# Valid event types we route (must match iot.webhooks.github.KNOWN_EVENT_TYPES)
ROUTED_EVENT_TYPES = frozenset({
    "pull_request",
    "issues",
    "issue_comment",
    "push",
    "release",
    "workflow_run",
    "star",
    "fork",
    "member",
    "dependabot_alert",
    "advisory",
})

# Rate limiter cleanup interval (cycles) - purge org buckets not seen in this many cycles
RATE_LIMITER_CLEANUP_EVERY = 10

try:
    RATE_PER_MIN = max(1, int(os.environ.get("GITHUB_NOTIFY_RATE_PER_MIN", "200")))
except ValueError:
    RATE_PER_MIN = 200

KEEP_CACHE = os.environ.get("GITHUB_NOTIFY_PROCESSED_KEEP_FILES", "1") == "1"

DEFAULT_TRACKED = "wout,admin"


def _get_tracked_logins() -> set[str]:
    """Read TRACKED_LOGINS at call time so env changes are respected."""
    return {
        x.strip().lower()
        for x in os.environ.get("GITHUB_TRACKED_LOGINS", DEFAULT_TRACKED).split(",")
        if x.strip()
    }


LOG_LEVEL = os.environ.get("NEOHIRO_LOG_LEVEL", "info")
log = setup_logging(quiet=False, level=LOG_LEVEL)

# Source for normalized events when iot cache is absent (e.g. dry run).
try:
    _IOT_SRC = Path(__file__).resolve().parents[2] / "iot" / "src"
    if str(_IOT_SRC) not in sys.path:
        sys.path.insert(0, str(_IOT_SRC))
    from iot.webhooks.github import normalize_event  # type: ignore
except Exception:  # pragma: no cover
    normalize_event = None  # type: ignore


# ─── State (cursor of processed delivery_ids) ───────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomic JSON write: tmp + fsync + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# Threshold for emitting a high-priority poke on dispatch errors
try:
    ERROR_POKE_THRESHOLD = max(1, int(os.environ.get("GITHUB_NOTIFY_ERROR_POKE_THRESHOLD", "5")))
except ValueError:
    ERROR_POKE_THRESHOLD = 5

# Threshold for emitting a poke on rate-limit saturation (per cycle)
try:
    RATE_LIMIT_POKE_THRESHOLD = max(1, int(os.environ.get("GITHUB_NOTIFY_RATE_LIMIT_POKE_THRESHOLD", "50")))
except ValueError:
    RATE_LIMIT_POKE_THRESHOLD = 50

# Threshold for emitting a poke on stale cache backlog
try:
    BACKLOG_POKE_THRESHOLD = max(10, int(os.environ.get("GITHUB_NOTIFY_BACKLOG_POKE_THRESHOLD", "500")))
except ValueError:
    BACKLOG_POKE_THRESHOLD = 500


def _shared_root() -> Path:
    return Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared"))


def _emit_poke(reason: str, priority: str = "high", fingerprint: str = "") -> None:
    poke_dir = _shared_root() / "heart" / "audit" / "instant"
    poke_dir.mkdir(parents=True, exist_ok=True)
    if fingerprint:
        seen_path = poke_dir / f".github_notifications_seen_{fingerprint}"
        try:
            if seen_path.exists():
                last = datetime.fromisoformat(seen_path.read_text(encoding="utf-8").strip())
                age_s = (datetime.now(timezone.utc) - last).total_seconds()
                if age_s < 3600:
                    return
        except (OSError, ValueError):
            pass
    ts = _now_iso()
    payload = {"ts": ts, "phase": "ingest_github_notifications", "severity": priority, "reason": reason, "fingerprint": fingerprint}
    try:
        import ulid as _ulid
        slug = _ulid.new().str
    except ImportError:
        slug = secrets.token_hex(8)
    p = poke_dir / f"github_notifications-{slug}.yaml"
    try:
        from atomic import write_yaml
        write_yaml(p, payload)
    except Exception as e:
        log.warning("poke_emit_failed", error=f"{type(e).__name__}: {e}")
        return
    if fingerprint:
        with contextlib.suppress(OSError):
            _atomic_write_text(seen_path, ts)


def _metrics_path() -> Path:
    return _shared_root() / "heart" / "metrics" / "github_notifications.prom"


def _write_metrics(counters: dict, *, cycle_duration_ms: int, backlog: int, backlog_real: int) -> None:
    try:
        p = _metrics_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        lines.append("# HELP github_notifications_events_total Event counts by counter name.")
        lines.append("# TYPE github_notifications_events_total counter")
        for k, v in sorted(counters.items()):
            if not isinstance(v, (int, float)):
                continue
            lines.append(f'github_notifications_events_total{{counter="{k}"}} {int(v)}')
        lines.append("# HELP github_notifications_routes_total Route outcomes.")
        lines.append("# TYPE github_notifications_routes_total counter")
        lines.append(f'github_notifications_routes_total{{kind="brain"}} {int(counters.get("brain_updates", 0))}')
        lines.append(f'github_notifications_routes_total{{kind="userdata"}} {int(counters.get("userdata_writes", 0))}')
        lines.append(f'github_notifications_routes_total{{kind="mouth"}} {int(counters.get("headlines_emitted", 0))}')
        lines.append("# HELP github_notifications_cycle_duration_ms Last cycle duration.")
        lines.append("# TYPE github_notifications_cycle_duration_ms gauge")
        lines.append(f"github_notifications_cycle_duration_ms {int(cycle_duration_ms)}")
        lines.append("# HELP github_notifications_backlog Pending cache files at end of cycle (includes intentional skips).")
        lines.append("# TYPE github_notifications_backlog gauge")
        lines.append(f"github_notifications_backlog {int(backlog)}")
        lines.append("# HELP github_notifications_backlog_real Pending files minus intentional skips (oversized + unknown_type + no_org_repo).")
        lines.append("# TYPE github_notifications_backlog_real gauge")
        lines.append(f"github_notifications_backlog_real {int(backlog_real)}")
        _atomic_write_text(p, "\n".join(lines) + "\n")
    except Exception as e:
        log.warning("metrics_write_failed", error=f"{type(e).__name__}: {e}")


def _state_path() -> Path:
    return BRAIN_STATE_DIR / "github_state.json"


def _load_processed_set(reset: bool = False) -> set[str]:
    """Load the set of processed delivery_ids. If reset=True, return an empty set."""
    if reset:
        return set()
    p = _state_path()
    if not p.is_file():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ids = data.get("processed_ids", [])
        if not isinstance(ids, list):
            return set()
        return {str(x) for x in ids if x}
    except (json.JSONDecodeError, OSError):
        return set()


def _save_processed_set(ids: set[str], last_event_at: str | None) -> None:
    """Persist the processed-id set with a bounded ring (max 5000 ids)."""
    cap = 5000
    ordered = sorted(ids)
    if len(ordered) > cap:
        # Keep the most recently seen ids; this is a ring buffer.
        # Caller is expected to only add newly processed ids each call.
        ordered = ordered[-cap:]
    payload = {
        "processed_ids": ordered,
        "last_event_at": last_event_at,
        "ts": _now_iso(),
    }
    _atomic_write_json(_state_path(), payload)

    # ── Cross-host dedup (HA mode) ──────────────────────────────────────────
    # In a multi-Heart deployment, each host keeps its own cursor in
    # github_state.json. A shared append-only ledger of seen delivery_ids
    # lets any host reject dupes that another host already processed.
    # The local cursor is still the source of truth; the shared ledger is
    # a *secondary check* that costs O(N) to read once per cycle.
    try:
        if os.environ.get("GITHUB_NOTIFY_HA_DEDUP", "0") == "1":
            shared_ledger = _shared_root() / "brain" / "state" / "github_processed_set.jsonl"
            shared_ledger.parent.mkdir(parents=True, exist_ok=True)
            with shared_ledger.open("a", encoding="utf-8") as f:
                # Write the last 100 ids in insertion order (approximate recency).
                # We iterate `ordered` and keep a sliding window of the last 100.
                tail: list[str] = []
                for d in ordered:
                    tail.append(d)
                    if len(tail) > 100:
                        tail = tail[1:]
                for d in tail:
                    f.write(json.dumps({"delivery_id": d, "ts": _now_iso()}) + "\n")
            # Compact if > 1 MB (rotate to .1, .2, ...)
            try:
                if shared_ledger.stat().st_size > 1_000_000:
                    _rotate_ledger(shared_ledger)
            except OSError:
                pass
    except Exception as e:
        log.warning("ha_dedup_write_failed", error=f"{type(e).__name__}: {e}")


def _rotate_ledger(ledger: Path, keep_rotations: int = 3) -> None:
    for i in range(keep_rotations, 0, -1):
        src = ledger if i == 1 else ledger.with_suffix(ledger.suffix + f".{i - 1}")
        dst = ledger.with_suffix(ledger.suffix + f".{i}")
        with contextlib.suppress(OSError):
            if src.exists():
                shutil.move(str(src), str(dst))


def _load_shared_processed_set() -> set[str]:
    """Load delivery_ids from the shared HA dedup ledger (current + rotations).

    Returns the union; this is read once per cycle to avoid processing events
    that another Heart instance in the cluster already routed.
    """
    out: set[str] = set()
    try:
        shared_dir = _shared_root() / "brain" / "state"
        candidates = [shared_dir / "github_processed_set.jsonl"]
        # Include rotated files (most-recent first to short-circuit)
        for i in range(1, 4):
            candidates.append(shared_dir / f"github_processed_set.jsonl.{i}")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            did = str(entry.get("delivery_id") or "")
                            if did:
                                out.add(did)
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
            if len(out) > 100_000:  # safety: avoid unbounded growth
                break
    except Exception:
        pass
    return out


# ─── HA lease mechanism (Proposal 3) ────────────────────────────────────────
#
# In a multi-Heart deployment, each host can lease the right to process
# specific delivery_ids for a short window. Other hosts honour the lease
# and skip those delivery_ids, preventing double-counting brain counters.
#
# Lease file: $NEOHIRO_SHARED_ROOT/brain/state/github_lease_{host}.json
# Schema: {"owner": "host", "expires_at": iso8601, "in_progress": [delivery_id, ...]}
#
# A lease is honoured if its expires_at is in the future. Stale leases
# (host crashed mid-cycle) are ignored.

try:
    LEASE_DURATION_SECONDS = max(10, int(os.environ.get("GITHUB_NOTIFY_LEASE_SECONDS", "120")))
except ValueError:
    LEASE_DURATION_SECONDS = 120
def _leases_dir() -> Path:
    return _shared_root() / "brain" / "state"


def _lease_path(host_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", host_id or "unknown")
    return _leases_dir() / f"github_lease_{safe}.json"


def _host_id() -> str:
    """Return a stable per-host identifier (env override or hostname fallback)."""
    return os.environ.get("GITHUB_NOTIFY_HOST_ID") or socket.gethostname() or "unknown"


def _acquire_lease(in_progress: list[str], *, ttl: int = LEASE_DURATION_SECONDS) -> Path | None:
    """Write a lease for this host claiming the in-flight delivery_ids.

    Returns the lease path on success, None on failure (best-effort).
    """
    host = _host_id()
    p = _lease_path(host)
    now = datetime.now(timezone.utc)
    payload = {
        "owner": host,
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        "in_progress": list(in_progress),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(p, payload)
        return p
    except Exception as e:
        log.warning("ha_lease_write_failed", host=host, error=f"{type(e).__name__}: {e}")
        return None


def _release_lease(in_progress: list[str]) -> None:
    """Remove delivery_ids from the in-progress set; delete the lease if empty."""
    host = _host_id()
    p = _lease_path(host)
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        existing = data.get("in_progress", [])
        if not isinstance(existing, list):
            existing = []
        # Filter out completed delivery_ids
        remaining = [x for x in existing if x not in set(in_progress)]
        if not remaining:
            with contextlib.suppress(OSError):
                p.unlink()
        else:
            data["in_progress"] = remaining
            data["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=LEASE_DURATION_SECONDS)
            ).isoformat()
            _atomic_write_json(p, data)
    except (OSError, json.JSONDecodeError):
        # Stale or corrupt lease; remove it.
        with contextlib.suppress(OSError):
            p.unlink()


def _load_active_leases() -> set[str]:
    """Return union of all in-progress delivery_ids from non-expired leases."""
    out: set[str] = set()
    now = datetime.now(timezone.utc)
    leases = _leases_dir()
    if not leases.is_dir():
        return out
    try:
        for p in leases.iterdir():
            if not p.is_file() or not p.name.startswith("github_lease_") or not p.name.endswith(".json"):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Stale/corrupt lease: treat as expired.
                with contextlib.suppress(OSError):
                    p.unlink()
                continue
            if not isinstance(data, dict):
                continue
            try:
                exp = datetime.fromisoformat(str(data.get("expires_at", "")).replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if exp <= now:
                with contextlib.suppress(OSError):
                    p.unlink()
                continue
            for did in data.get("in_progress", []) or []:
                if did:
                    out.add(str(did))
    except OSError:
        pass
    return out


# ─── Routing rules ──────────────────────────────────────────────────────────


def _should_headline(event: dict) -> bool:
    """Return True if this event deserves a Mouth headline (subject to rate caps)."""
    et = str(event.get("event_type", ""))
    action = str(event.get("action", "") or "").lower()
    severity = str(event.get("severity", "info"))
    subject_state = str((event.get("subject") or {}).get("state", "")).lower()
    if et == "pull_request":
        # A PR is "merged" if either the action verb is "merged" or
        # the subject state was normalised to "merged" (which happens
        # when the upstream sends action="closed" with merged=True).
        return action == "merged" or subject_state == "merged"
    if et == "issues":
        if action != "opened":
            return False
        sev = (event.get("subject", {}) or {}).get("labels") or []
        return bool({"critical", "p0", "sev:critical", "high", "sev:high", "security", "vulnerability"} & {str(s).lower() for s in sev})
    if et == "workflow_run":
        return (event.get("subject", {}) or {}).get("state") in {"failure", "failed"}
    if et == "release":
        return action in {"published", "released", ""}
    if et == "dependabot_alert":
        return action in {"created", "opened", "reopened", "new", ""} and severity in {"high", "critical"}
    if et == "advisory":
        return severity in {"high", "critical"}
    if et == "member":
        return action in {"added", ""}
    return False


def _is_security_event(event: dict) -> bool:
    return bool(event.get("is_security"))


def _addresses_logins(event: dict) -> set[str]:
    """Flatten addresses block into a set of lowercased logins."""
    out: set[str] = set()
    addr = event.get("addresses") or {}
    for key in ("author", "assignees", "requested_reviewers", "mentioned"):
        v = addr.get(key)
        if isinstance(v, list):
            for x in v:
                if x:
                    out.add(str(x).lower())
        elif isinstance(v, str) and v:
            out.add(v.lower())
    return out


def _userdata_targets(event: dict) -> list[str]:
    """Filter addresses against tracked logins; preserve order."""
    tracked = _get_tracked_logins()
    if not tracked:
        return []
    addrs = _addresses_logins(event)
    actor = str(event.get("actor", "")).lower()
    candidates: list[str] = []
    for login in addrs:
        if login in tracked:
            candidates.append(login)
    if actor in tracked and actor not in candidates:
        candidates.append(actor)
    # Dependabot alerts: route to the repo owner (assumed Wout)
    if not candidates and event.get("event_type") == "dependabot_alert":
        candidates.extend(sorted(tracked))
    return candidates


# ─── Brain awareness ───────────────────────────────────────────────────────


def _awareness_path(org: str, repo: str) -> Path:
    safe_org = _safe_slug(org)
    safe_repo = _safe_slug(repo)
    return BRAIN_AWARENESS_DIR / safe_org / f"{safe_repo}.yaml"


def _safe_slug(s: str) -> str:
    """Reduce to a safe filesystem slug."""
    import re as _re

    cleaned = _re.sub(r"[^A-Za-z0-9._-]", "_", s or "")
    if cleaned in (".", "..", "") or cleaned.startswith("."):
        cleaned = "unknown"
    return cleaned


def _ensure_awareness(path: Path) -> dict[str, Any]:
    """Load existing repo awareness yaml or create an empty doc."""
    if not path.is_file():
        return {
            "schema_version": 1,
            "org": path.parent.name,
            "repo": path.stem,
            "counters": {
                "open_prs": 0,
                "open_issues": 0,
                "workflows_failed_total": 0,
                "releases_published_total": 0,
                "dependabot_alerts_open": 0,
                "events_total": 0,
            },
            "events": [],
        }
    try:
        import yaml

        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {
            "schema_version": 1,
            "org": path.parent.name,
            "repo": path.stem,
            "counters": {},
            "events": [],
        }
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", 1)
    data.setdefault("org", path.parent.name)
    data.setdefault("repo", path.stem)
    counters = data.setdefault("counters", {})
    if not isinstance(counters, dict):
        counters = {}
    counters.setdefault("open_prs", 0)
    counters.setdefault("open_issues", 0)
    counters.setdefault("workflows_failed_total", 0)
    counters.setdefault("releases_published_total", 0)
    counters.setdefault("dependabot_alerts_open", 0)
    counters.setdefault("events_total", 0)
    if not isinstance(data.get("events"), list):
        data["events"] = []
    return data


def _apply_counter_delta(counters: dict[str, Any], key: str, delta: int) -> None:
    """Apply a bounded counter delta (clamp ≥ 0)."""
    try:
        new_val = int(counters.get(key, 0)) + int(delta)
    except (TypeError, ValueError):
        new_val = int(delta)
    if new_val < 0:
        new_val = 0
    counters[key] = new_val


def _route_to_brain(event: dict, *, dry_run: bool) -> dict[str, Any] | None:
    """Apply the event to per-repo awareness counters and event log.

    Returns the (possibly updated) awareness doc for unit-testing, or
    None on parse error.
    """
    org = str(event.get("org", "")).strip()
    repo = str(event.get("repo", "")).strip()
    if not org or not repo:
        return None
    path = _awareness_path(org, repo)
    doc = _ensure_awareness(path)
    counters = doc.setdefault("counters", {})

    et = str(event.get("event_type", ""))
    action = str(event.get("action", "") or "").lower()
    subj = event.get("subject", {}) or {}

    counters["events_total"] = int(counters.get("events_total", 0)) + 1

    if et == "pull_request":
        state = (subj.get("state") or "").lower()
        if action == "opened" or (action in {"reopened", "ready_for_review"} and state == "open"):
            _apply_counter_delta(counters, "open_prs", +1)
        elif action == "closed":
            _apply_counter_delta(counters, "open_prs", -1)
    elif et == "issues":
        state = (subj.get("state") or "").lower()
        if action == "opened" or (action == "reopened" and state == "open"):
            _apply_counter_delta(counters, "open_issues", +1)
        elif action == "closed":
            _apply_counter_delta(counters, "open_issues", -1)
    elif et == "workflow_run":
        if (subj.get("state") or "").lower() in {"failure", "failed"}:
            counters["workflows_failed_total"] = int(counters.get("workflows_failed_total", 0)) + 1
    elif et == "release":
        if action in {"published", "released", ""}:
            counters["releases_published_total"] = int(counters.get("releases_published_total", 0)) + 1
    elif et == "dependabot_alert":
        if action in {"fixed", "dismissed", "closed"}:
            _apply_counter_delta(counters, "dependabot_alerts_open", -1)
        elif action in {"created", "opened", "reopened", "new", ""}:
            _apply_counter_delta(counters, "dependabot_alerts_open", +1)

    # Bounded event log: keep last 200 to avoid unbounded growth.
    events = doc.setdefault("events", [])
    events.append({
        "delivery_id": event.get("delivery_id"),
        "ts": event.get("received_at"),
        "event_type": et,
        "action": event.get("action"),
        "subject": {
            "kind": subj.get("kind"),
            "title": subj.get("title"),
            "url": subj.get("url"),
            "state": subj.get("state"),
        },
        "actor": event.get("actor"),
        "severity": event.get("severity"),
        "summary": event.get("raw_summary"),
    })
    if len(events) > 200:
        del events[: len(events) - 200]

    doc["updated"] = _now_iso()
    if not dry_run:
        import yaml

        _atomic_write_text(path, yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True))
    return doc


# ─── Userdata notifications ─────────────────────────────────────────────────


def _route_to_userdata(event: dict, *, dry_run: bool) -> int:
    """Write an age-encrypted notification per tracked user.

    Uses `userdata.memory_bridge.MemoryBridge.write_notification`.
    Returns the number of notifications actually written.
    """
    targets = _userdata_targets(event)
    if not targets:
        return 0
    try:
        from userdata.memory_bridge import MemoryBridge  # type: ignore
    except Exception as e:  # pragma: no cover - userdata not on path
        log.warning("userdata_import_failed", error=f"{type(e).__name__}: {e}")
        return 0

    notification = _to_notification(event)
    written = 0
    for login in targets:
        mb = MemoryBridge(login)
        if dry_run:
            written += 1
            continue
        try:
            mb.write_notification(notification)
            written += 1
        except PermissionError as e:
            log.warning("userdata_no_identity", login=login, error=str(e))
        except FileNotFoundError as e:
            log.warning("userdata_age_missing", login=login, error=str(e))
        except Exception as e:
            log.warning("userdata_write_failed", login=login, error=f"{type(e).__name__}: {e}")
    return written


def _to_notification(event: dict) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "delivery_id": event.get("delivery_id"),
        "received_at": event.get("received_at"),
        "source": "github",
        "event_type": event.get("event_type"),
        "action": event.get("action"),
        "severity": event.get("severity"),
        "is_security": bool(event.get("is_security")),
        "summary": event.get("raw_summary"),
        "url": (event.get("subject") or {}).get("url"),
        "org": event.get("org"),
        "repo": event.get("repo"),
        "actor": event.get("actor"),
        "subject": event.get("subject"),
        "addresses": event.get("addresses"),
    }


# ─── Mouth headlines ───────────────────────────────────────────────────────


def _route_to_mouth(event: dict, *, dry_run: bool) -> bool:
    if not _should_headline(event):
        return False
    MOUTH_HEADLINES_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "schema_version": 1,
        "ts": event.get("received_at"),
        "source": "github",
        "delivery_id": event.get("delivery_id"),
        "severity": event.get("severity"),
        "summary": event.get("raw_summary"),
        "url": (event.get("subject") or {}).get("url"),
        "org": event.get("org"),
        "repo": event.get("repo"),
        "event_type": event.get("event_type"),
        "action": event.get("action"),
    }, ensure_ascii=False)
    if dry_run:
        return True
    path = MOUTH_HEADLINES_DIR / "github.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())
    return True


# ─── Audit log ──────────────────────────────────────────────────────────────


def _audit_event(event: dict, brain_doc: dict | None, userdata_written: int, headline: bool) -> None:
    audit_path = USERDATA_DIR / "audit" / "github_notifications.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _now_iso(),
        "delivery_id": event.get("delivery_id"),
        "event_type": event.get("event_type"),
        "action": event.get("action"),
        "severity": event.get("severity"),
        "is_security": bool(event.get("is_security")),
        "org": event.get("org"),
        "repo": event.get("repo"),
        "actor": event.get("actor"),
        "userdata_targets": sorted(_userdata_targets(event)) if userdata_written else [],
        "headlined": bool(headline),
        "summary": event.get("raw_summary"),
    }
    if brain_doc is not None:
        counters = brain_doc.get("counters") or {}
        entry["brain_counters_after"] = {
            k: counters.get(k) for k in (
                "open_prs",
                "open_issues",
                "workflows_failed_total",
                "releases_published_total",
                "dependabot_alerts_open",
                "events_total",
            )
        }
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        with contextlib.suppress(OSError):
            os.fsync(f.fileno())


# ─── Iteration over cache ──────────────────────────────────────────────────


class _PerOrgRateLimiter:
    """Sliding window rate limiter keyed by org. Defaults to 200/min/org.

    Includes periodic cleanup of stale org buckets to prevent memory leaks
    in long-running continuous mode.
    """

    def __init__(self, per_minute: int = RATE_PER_MIN, window_sec: int = 60):
        self.per_minute = max(1, per_minute)
        self.window_sec = window_sec
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._cycles_since_cleanup = 0

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets[key]
        cutoff = now - self.window_sec
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            return False
        bucket.append(now)
        return True

    def cleanup_stale(self) -> int:
        """Remove buckets with no recent entries. Returns number of buckets removed."""
        now = time.monotonic()
        cutoff = now - self.window_sec
        stale = [k for k, v in self._buckets.items() if not v or v[-1] < cutoff]
        for k in stale:
            del self._buckets[k]
        return len(stale)


def _iter_cache_files(cache_dir: Path) -> Iterable[Path]:
    """Yield every json file in the github cache (excluding `latest.json` and processed/)."""
    src = cache_dir / "github"
    if not src.is_dir():
        return
    processed = src / "processed"
    for p in sorted(src.iterdir()):
        if not p.is_file():
            continue
        if p.name == "latest.json":
            continue
        if processed in p.parents:
            continue
        if not p.name.endswith(".json"):
            continue
        yield p


def _load_event_from_cache(path: Path) -> dict | None:
    """Extract the normalized event from a cache file written by iot.server."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("cache_read_failed", path=str(path), error=f"{type(e).__name__}: {e}")
        return None
    if not isinstance(raw, dict):
        return None
    norm = raw.get("normalized")
    if isinstance(norm, dict) and norm.get("delivery_id"):
        return norm
    # Fall back to the embedded `data` payload + try normalize again.
    data = raw.get("data")
    if not isinstance(data, dict):
        return None
    if normalize_event is None:
        return None
    headers = {
        "X-GitHub-Event": raw.get("event_type") or "",
        "X-GitHub-Delivery": (data.get("delivery_id") if isinstance(data, dict) else "") or "",
    }
    return normalize_event(headers, data)


def _archive_cache_file(path: Path, *, dry_run: bool) -> None:
    """Move the cache file to cache/github/processed/<same-name>."""
    if KEEP_CACHE or dry_run:
        return
    processed_dir = path.parent / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / path.name
    try:
        shutil.move(str(path), str(dest))
    except OSError as e:
        log.warning("cache_archive_failed", path=str(path), error=str(e))


def _write_dlq(event_path: Path, reason: str, *, dry_run: bool) -> None:
    """Move a permanently-skipped event to the dead-letter queue.

    DLQ files are stored under DLQ_DIR with a ".dlq" suffix so operators can
    inspect them and replay via the iot replay endpoint.
    """
    if dry_run:
        return
    try:
        DLQ_DIR.mkdir(parents=True, exist_ok=True)
        dlq_name = event_path.name + ".dlq"
        dest = DLQ_DIR / dlq_name
        if dest.exists():
            dlq_name = f"{event_path.stem}-{_now_iso().replace(':', '')}-{reason}{event_path.suffix}.dlq"
            dest = DLQ_DIR / dlq_name
        shutil.move(str(event_path), str(dest))
        log.info("dlq_entry_written", path=str(dest), reason=reason)
    except Exception as e:
        log.warning("dlq_write_failed", path=str(event_path), reason=reason, error=str(e))


# ─── Main loop ─────────────────────────────────────────────────────────────


def run_once(*, dry_run: bool = False, reset_processed: bool = False, quiet: bool = False) -> dict[str, Any]:
    """Drain one cycle of github cache into brain/userdata/mouth."""
    if quiet:
        log.warning("github_notifications_quiet_noisy_logs")
    t0 = time.monotonic()
    processed_ids = _load_processed_set(reset=reset_processed)
    # Track IDs newly added in this cycle (for HA lease publication at cycle end).
    processed_this_cycle: set[str] = set()

    # HA dedup: merge in delivery_ids seen by other Heart instances in the
    # cluster. Read once per cycle, then merge; do not write back (the local
    # cursor is the source of truth; the shared ledger is a secondary check).
    # Also honour active leases: any delivery_ids another host is currently
    # processing are skipped to prevent double-counting on concurrent cycles.
    if os.environ.get("GITHUB_NOTIFY_HA_DEDUP", "0") == "1":
        shared_ids = _load_shared_processed_set()
        if shared_ids:
            processed_ids |= shared_ids
        active_leases_ids = _load_active_leases()
        if active_leases_ids:
            skipped_by_lease = 0
            for did in active_leases_ids:
                if did not in processed_ids:
                    skipped_by_lease += 1
                    processed_ids.add(did)
            if skipped_by_lease:
                log.debug("ha_leases_skipped", count=skipped_by_lease)

    limiter = _PerOrgRateLimiter()

    counters = {
        "events_seen": 0,
        "events_skipped_already_processed": 0,
        "events_skipped_normalize_failed": 0,
        "events_skipped_rate_limited": 0,
        "events_skipped_no_org_repo": 0,
        "events_skipped_file_limit": 0,
        "events_skipped_oversized": 0,
        "events_skipped_unknown_type": 0,
        "brain_updates": 0,
        "userdata_writes": 0,
        "headlines_emitted": 0,
        "errors": 0,
        "files_archived": 0,
    }
    last_event_at: str | None = None
    files_processed = 0
    files_seen_total = 0  # track incrementally to avoid re-iterating dir at end for backlog
    # Skipped-but-intentional = not-yet-processed files that were correctly filtered
    # (oversized, unknown type, no org/repo). These are excluded from real backlog.
    files_skipped_intentional = 0

    for path in _iter_cache_files(IOT_CACHE_DIR):
        files_seen_total += 1
        # Enforce per-cycle file limit to prevent unbounded backlog processing
        if files_processed >= MAX_FILES_PER_CYCLE:
            counters["events_skipped_file_limit"] = counters.get("events_skipped_file_limit", 0) + 1
            continue

        # Enforce max file size to prevent OOM on malicious/large payloads
        try:
            if path.stat().st_size > MAX_CACHE_FILE_BYTES:
                counters["events_skipped_oversized"] = counters.get("events_skipped_oversized", 0) + 1
                files_skipped_intentional += 1
                if path.name.endswith(".json"):
                    delivery = path.name.split("-")[1] if "-" in path.name else "unknown"
                    processed_ids.add(delivery)
                    processed_this_cycle.add(delivery)
                _write_dlq(path, "oversized", dry_run=dry_run)
                continue
        except OSError:
            pass  # If stat fails, let _load_event_from_cache handle it

        event = _load_event_from_cache(path)
        if not event or not event.get("delivery_id"):
            counters["events_skipped_normalize_failed"] += 1
            _write_dlq(path, "normalize_failed", dry_run=dry_run)
            continue

        # Validate event type is one we know how to route
        et = str(event.get("event_type", ""))
        if et not in ROUTED_EVENT_TYPES:
            counters["events_skipped_unknown_type"] = counters.get("events_skipped_unknown_type", 0) + 1
            files_skipped_intentional += 1
            processed_ids.add(str(event["delivery_id"]))
            processed_this_cycle.add(str(event["delivery_id"]))
            _write_dlq(path, f"unknown_type:{et}", dry_run=dry_run)
            continue

        delivery = str(event["delivery_id"])
        is_replay = bool(event.get("_replay"))
        if delivery in processed_ids and not is_replay:
            counters["events_skipped_already_processed"] += 1
            continue
        if is_replay:
            counters["events_replayed"] = counters.get("events_replayed", 0) + 1
            log.info(
                "replay_detected",
                delivery=delivery,
                replay_at=event["_replay"].get("replay_at"),
            )
        org = str(event.get("org", "")).strip()
        repo = str(event.get("repo", "")).strip()
        if not org or not repo:
            counters["events_skipped_no_org_repo"] += 1
            files_skipped_intentional += 1
            processed_ids.add(delivery)
            processed_this_cycle.add(delivery)
            _write_dlq(path, "no_org_repo", dry_run=dry_run)
            continue
        if not limiter.allow(org):
            counters["events_skipped_rate_limited"] += 1
            continue

        files_processed += 1

        try:
            brain_doc = _route_to_brain(event, dry_run=dry_run)
            userdata_written = _route_to_userdata(event, dry_run=dry_run)
            headline_emitted = _route_to_mouth(event, dry_run=dry_run)
            _audit_event(event, brain_doc, userdata_written, headline_emitted)
        except Exception as e:
            counters["errors"] += 1
            log.warning(
                "route_failed",
                delivery=delivery,
                error=f"{type(e).__name__}: {e}",
            )
            # Don't mark processed on error: next cycle will retry.
            continue

        counters["events_seen"] += 1
        if brain_doc is not None:
            counters["brain_updates"] += 1
        counters["userdata_writes"] += userdata_written
        if headline_emitted:
            counters["headlines_emitted"] += 1
        processed_ids.add(delivery)
        processed_this_cycle.add(delivery)
        ts = event.get("received_at")
        if ts and (last_event_at is None or str(ts) > last_event_at):
            last_event_at = str(ts)
        _archive_cache_file(path, dry_run=dry_run)
        if not KEEP_CACHE and not dry_run:
            counters["files_archived"] += 1

    # Periodic cleanup of stale rate limiter buckets to prevent memory leak
    # in continuous mode (every RATE_LIMITER_CLEANUP_EVERY cycles).
    if limiter._cycles_since_cleanup >= RATE_LIMITER_CLEANUP_EVERY:
        removed = limiter.cleanup_stale()
        if removed:
            log.debug("rate_limiter_cleanup", removed_buckets=removed)
        limiter._cycles_since_cleanup = 0
    else:
        limiter._cycles_since_cleanup += 1

    # Always save cursor so subsequent calls know what's already done.
    # The cursor file is cheap to write; the alternative is that replayed
    # deliveries get re-processed on every cycle, which would double-count
    # brain counters. (See test_replay_does_not_double_count.)
    #
    # NOTE: even in dry_run the cursor is persisted. This means a subsequent
    # non-dry-run cycle will skip events already in the cursor. This is the
    # current (2026-09) design: dry-run advances the cursor so the next
    # production cycle does not double-process. If you want a true no-op
    # dry-run that does not touch state, invoke `run_once(dry_run=True)` in
    # isolation and do not follow with a non-dry-run cycle.
    if not dry_run:
        _save_processed_set(processed_ids, last_event_at)
    else:
        try:
            _save_processed_set(processed_ids, last_event_at)
        except Exception as e:
            log.warning("dry_run_cursor_save_failed", error=f"{type(e).__name__}: {e}")

    log.info(
        "github_notifications_cycle",
        **counters,
        dry_run=dry_run,
    )

    # Write Prometheus metrics (best-effort, non-fatal)
    if not dry_run:
        cycle_ms = int((time.monotonic() - t0) * 1000)
        # backlog = files we saw but did NOT process (skipped or already-processed).
        # backlog_real = backlog minus intentional skips (oversized, unknown type,
        # no org/repo). Only backlog_real triggers a poke — intentional skips are
        # healthy, not degradation.
        backlog = files_seen_total - files_processed
        backlog_real = max(0, backlog - files_skipped_intentional)
        _write_metrics(counters, cycle_duration_ms=cycle_ms, backlog=backlog, backlog_real=backlog_real)

        # Pokes on degradation (deduped via fingerprint sidecar, 1h TTL).
        # Suppress pokes in `--once` mode (manual operator run) to avoid spam.
        if counters["errors"] >= ERROR_POKE_THRESHOLD:
            _emit_poke(
                reason=f"{counters['errors']} dispatch errors in one cycle",
                priority="high",
                fingerprint="dispatch-errors",
            )
        if counters["events_skipped_rate_limited"] >= RATE_LIMIT_POKE_THRESHOLD:
            _emit_poke(
                reason=f"{counters['events_skipped_rate_limited']} events rate-limited (org limit saturated)",
                priority="medium",
                fingerprint="rate-limit-saturation",
            )
        if backlog_real >= BACKLOG_POKE_THRESHOLD:
            _emit_poke(
                reason=f"real backlog {backlog_real} files (threshold {BACKLOG_POKE_THRESHOLD})",
                priority="medium",
                fingerprint="cache-backlog",
            )

        # HA: publish a lease so other Heart instances know what this host
        # processed this cycle. The lease is a JSON file with a TTL; other
        # hosts honour it to prevent double-counting during concurrent cycles.
        if os.environ.get("GITHUB_NOTIFY_HA_DEDUP", "0") == "1" and processed_this_cycle:
            _acquire_lease(list(processed_this_cycle))

    return counters


def get_status() -> dict[str, Any]:
    """Return a dict with cache_pending, brain_repos, audit_total, etc."""
    state = _state_path()
    processed_ids = _load_processed_set()
    cache_pending = 0
    cache_dir = IOT_CACHE_DIR / "github"
    if cache_dir.is_dir():
        for p in cache_dir.iterdir():
            if not p.is_file() or p.name == "latest.json" or not p.name.endswith(".json"):
                continue
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            norm = raw.get("normalized") if isinstance(raw, dict) else None
            delivery = str((norm or {}).get("delivery_id") or "")
            if delivery and delivery not in processed_ids:
                cache_pending += 1
    awareness_repos = 0
    if BRAIN_AWARENESS_DIR.is_dir():
        for org_dir in BRAIN_AWARENESS_DIR.iterdir():
            if org_dir.is_dir():
                awareness_repos += sum(1 for p in org_dir.iterdir() if p.is_file() and p.suffix in {".yaml", ".yml"})
    audit_total = 0
    audit = USERDATA_DIR / "audit" / "github_notifications.jsonl"
    if audit.is_file():
        with audit.open("r", encoding="utf-8") as f:
            audit_total = sum(1 for line in f if line.strip())
    headlines_pending = 0
    hl = MOUTH_HEADLINES_DIR / "github.jsonl"
    if hl.is_file():
        with hl.open("r", encoding="utf-8") as f:
            headlines_pending = sum(1 for line in f if line.strip())
    dlq_total = 0
    if DLQ_DIR.is_dir():
        with contextlib.suppress(OSError):
            dlq_total = sum(1 for p in DLQ_DIR.iterdir() if p.is_file())
    last_event_at = None
    if state.is_file():
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            last_event_at = data.get("last_event_at")
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "cache_pending": cache_pending,
        "processed_total": len(processed_ids),
        "awareness_repos": awareness_repos,
        "userdata_notifications_total": audit_total,
        "mouth_headlines_pending": headlines_pending,
        "dlq_total": dlq_total,
        "dlq_path": str(DLQ_DIR),
        "last_event_at": last_event_at,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge GitHub webhook events into Brain/userdata/Mouth")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit (default)")
    parser.add_argument("--continuous", action="store_true", help="Run continuously every N seconds")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles (default: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Do not write anything")
    parser.add_argument("--quiet", action="store_true", help="Reduce log noise")
    parser.add_argument("--reset-processed", action="store_true", help="Reset the processed-id cursor before running")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    args = parser.parse_args()

    global log
    log = setup_logging(quiet=args.quiet, level=LOG_LEVEL)

    if args.status:
        print(json.dumps(get_status(), indent=2, default=str))
        return 0

    if not args.once and not args.continuous:
        args.once = True

    if args.once:
        try:
            counters = run_once(dry_run=args.dry_run, reset_processed=args.reset_processed, quiet=args.quiet)
        except Exception as e:
            log.error("github_notifications_failed", error=f"{type(e).__name__}: {e}")
            return 1
        # When stdout is a TTY, also print the counters as plain JSON for the operator.
        if sys.stdout.isatty():
            print(json.dumps(counters, indent=2, default=str))
        return 0

    while True:
        try:
            run_once(dry_run=args.dry_run, reset_processed=False, quiet=args.quiet)
        except Exception as e:
            log.error("github_notifications_failed", error=f"{type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
