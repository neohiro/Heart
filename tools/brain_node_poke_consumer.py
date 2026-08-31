"""
brain_node_poke_consumer.py — surfaces brain_node session stall pokes in the doctor
output and via `heartctl doctor`.

Reads `brain_node_stall-*.yaml` files from <shared>/heart/audit/instant/, deduplicates
by fingerprint, and formats them as structured doctor alerts.

SPEC: Brain/BRAIN_NODE_OPENCODE_ROUTING.md § 3.2
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# atomic.py is a sibling module; add this directory to sys.path so the
# `from atomic import write_text` below resolves whether this file is
# imported as part of the Heart package or run directly.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from atomic import write_text as _atomic_text

from heartctl import _shared_root

# One fingerprint per line. Capped to the last 10 000 to prevent unbounded growth
# on long-running deployments. Older fingerprints are silently dropped.
MAX_SEEN = 10_000
# Path lives under <shared>/heart/audit/ so it survives container restarts and is
# writable even when the container root is read-only (Heart runs read_only: true).
_SEEN_FILE = _shared_root() / "heart" / "audit" / ".brain_node_stall_seen"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to path via atomic.write_text (mkstemp + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_text(path, text, prefix=path.name + ".")


def _load_pokes() -> list[dict]:
    """Load all unread brain_node stall pokes from audit/instant/."""
    seen: set[str] = set()
    seen_order: list[str] = []  # insertion order, oldest-first
    if _SEEN_FILE.is_file():
        for line in _SEEN_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                seen_order.append(line)

    poke_dir = _shared_root() / "heart" / "audit" / "instant"
    if not poke_dir.is_dir():
        return []

    pokes: list[dict] = []
    for p in poke_dir.glob("brain_node_stall-*.yaml"):
        fp = _fingerprint(p)
        if not fp or fp in seen:
            continue
        poke = _parse_poke(p)
        if not poke:
            # Don't mark as seen; allow a re-attempt next call in case the
            # file was mid-write. Bounded by the cap so this is safe.
            continue
        pokes.append(poke)
        seen.add(fp)
        seen_order.append(fp)

    # Persist the union, capped FIFO (drop oldest). New entries MUST be retained.
    if seen_order[len(seen_order) - len(pokes):] != seen_order[-len(pokes):]:
        # Sanity: make sure new entries are at the tail (insertion-order is preserved).
        pass
    if seen_order:
        cap = seen_order[-MAX_SEEN:]
        _atomic_write_text(_SEEN_FILE, "\n".join(cap) + "\n")

    return pokes


def _fingerprint(p: Path) -> str | None:
    """Extract the fingerprint value from a poke YAML file."""
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith("fingerprint:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _parse_poke(p: Path) -> dict | None:
    """Parse a poke YAML file into a dict."""
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return None


def _ts_key(p: dict[str, Any]) -> tuple[int, Any]:
    """Sort key that accepts both string and datetime ts values without TypeError.

    Returns (0, ts) when ts is a real value, (1, "") when missing — so missing-ts
    entries sort last under max(), which is the conservative pick.
    """
    ts = p.get("ts")
    if ts is None:
        return (1, "")
    if isinstance(ts, (str, datetime)):
        return (0, ts)
    return (1, str(ts))


def surface_stall_alerts() -> int:
    """Print stall alerts to stdout and return count of unique stalled sessions."""
    pokes = _load_pokes()
    if not pokes:
        return 0
    print("=== Brain Node Session Stalls ===")
    by_session: dict[str, list[dict]] = defaultdict(list)
    for poke in pokes:
        sid = poke.get("subject", {}).get("task_id", "unknown")
        by_session[sid].append(poke)
    for sid, session_pokes in sorted(by_session.items()):
        latest = max(session_pokes, key=_ts_key)
        stalled_since = latest.get("subject", {}).get("stalled_since") or "unknown"
        display_ts = (
            stalled_since[:19].replace("T", " ")
            if isinstance(stalled_since, str) and stalled_since != "unknown"
            else stalled_since
        )
        print(
            f"  [!] session {sid}\n"
            f"      stalled since: {display_ts}\n"
            f"      pokes emitted: {len(session_pokes)}\n"
            f"      reason: {latest.get('reason', 'no file change')}"
        )
        session_dir = _shared_root() / f"brain/opencode/sessions/{sid}"
        if session_dir.is_dir():
            files = sorted(f.name for f in session_dir.iterdir() if f.is_file())
            if files:
                preview = ", ".join(files[:5])
                suffix = f", {len(files) - 5} more)" if len(files) > 5 else ")"
                print(f"      session files ({len(files)}): {preview}{suffix}")
        print()
    return len(by_session)


if __name__ == "__main__":
    sys.exit(0 if surface_stall_alerts() == 0 else 1)
