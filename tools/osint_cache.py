"""
osint_cache.py — Heart OSINT cache: READ latest → AMEND → WRITE.

Pattern (per heartbeat cycle):
    1. READ  latest cached observations from Brain/heartbeat/osint_cache.json
    2. AMEND merge incoming raw observations from signals_incoming/ and
          any enrichments fetched this cycle
    3. WRITE back to osint_cache.json  (atomic: write to .tmp + rename)
       + ENQUEUE abuse signals for new IPs, geo drift, vpn/tor status changes

Design goals:
  - Faultless:     atomic writes (temp + rename), corruption-tolerant reads
  - Fast:          one JSON file, O(n) merge where n = new observations
  - Self-evolving: TTL-based natural expiry; renews on next cycle
  - Privacy-first: raw IPs never written; only ip-hash:<sha256> stored

Cache TTL:
  An observation is considered live if last_seen < (now - ttl_minutes).
  On each cycle the cache is loaded, amended, and written back — so the
  cache is self-renewing: a live observation's last_seen is refreshed
  every cycle it appears, keeping it alive indefinitely.
  Observations not seen for ttl_minutes are pruned on next load.

Surface fields (stored in cache per IP hash):
  Stable (accumulated across cycles):
    ip_hash         — ip-hash:<sha256> (never raw IP)
    country         — ISO country name
    country_code    — ISO 3166-1 alpha-2
    isp             — ISP / hosting provider name
    is_vpn          — VPN detected this cycle
    is_tor          — Tor exit node detected this cycle
    is_proxy        — open proxy detected this cycle
    first_seen      — ISO timestamp of first observation
    last_seen       — ISO timestamp of most recent observation
    last_country    — country code at last observation
    last_country_code
    geo_drift_count — number of times country changed
    last_drift_at   — ISO timestamp of last drift event
    signals_enqueued— list of signal_type strings already sent
    tags            — list of tag strings (auto + manual)
    source          — last source that produced this observation
  Ephemeral (overwritten each cycle, not persisted):
    asn             — autonomous system number (enrichment)
    org             — AS organisation name  (enrichment)
    is_datacenter   — hosting/DC IP flag    (enrichment)
    is_mobile       — mobile carrier flag   (enrichment)
    connection_type — residential/business  (enrichment)
    host_count      — shared-hosting count (enrichment)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_WORKSPACE = Path(__file__).resolve().parent.parent.parent
for _p in (str(_WORKSPACE), str(_WORKSPACE / "userdata" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)



# Canonical /userdata writer — owns the on-disk schema, the file naming, and
# the signals-merge semantics. We import lazily (inside write_to_userdata)
# so importing this module never fails when userdata isn't on the path.
_ghosts_mod = None
def _ghosts():
    global _ghosts_mod
    if _ghosts_mod is None:
        import importlib
        _ghosts_mod = importlib.import_module("userdata.ghosts")
    return _ghosts_mod

CACHE_FILE = "osint_cache.json"
SIGNALS_INCOMING_DIR = "signals_incoming"
USERDATA_PENDING_DIR = "userdata_pending"
CACHE_TTL_MINUTES = 60
CACHE_VERSION = 1

# Default rate cap on /userdata writes per cycle. If exceeded, the rest are
# queued in Brain/heartbeat/userdata_pending/ and replayed next cycle. 0 disables.
_DEFAULT_USERDATA_MAX_WRITES_PER_CYCLE = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(s: str, fallback: str = "x") -> str:
    if not s:
        return fallback
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in s) or fallback


def _acquire_lock(lock_path: Path, timeout_s: int = 30) -> Path:
    """
    Acquire an exclusive file-based lock using mkdir(2) semantics.

    mkdir is atomic on both POSIX and Windows NT, so this works cross-platform
    without any fcntl / msvcrt imports. On success returns the lockfile path.
    On timeout raises TimeoutError.

    Lock is released by rmdir() — mkdir creates the lock as a directory so that
    the existence-check and create are one atomic syscall.
    """
    import time

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock_path.mkdir(parents=True, exist_ok=False)
            return lock_path
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Could not acquire lock {lock_path} within {timeout_s}s — another process is running ingest_osint")
            time.sleep(0.1)


def _release_lock(lock_path: Path) -> None:
    """Release a lock by removing the lockfile. Best-effort.

    The lock is a directory (mkdir is atomic cross-platform), so unlink would
    fail on POSIX and Windows alike. Use rmdir instead.
    """
    try:
        lock_path.rmdir()
    except OSError:
        pass


# ── IP hashing (privacy-first) ─────────────────────────────────────────────

_IPV6_MAPPED_PREFIX = "::ffff:"
_IPV6_MAPPED_PREFIX_LOWER = "::ffff:"


def _normalize_ip(raw_ip: str) -> str:
    """
    Normalize an IP address to a canonical form before hashing.

    Handles IPv4-mapped IPv6 addresses (::ffff:192.0.2.1 → 192.0.2.1) so that
    the same host observed over IPv4 and IPv6 produces the same hash. Without
    this normalization an attacker who can trigger observations from both the
    raw and mapped representations could correlate the two hashes and recover the
    IP address from the ip-hash pseudonym.
    """
    ip = raw_ip.strip()
    if ip.lower().startswith(_IPV6_MAPPED_PREFIX_LOWER):
        ip = ip[len(_IPV6_MAPPED_PREFIX):]
    return ip


def _hash_ip(raw_ip: str) -> str:
    normalized = _normalize_ip(raw_ip)
    h = hashlib.sha256(normalized.encode()).hexdigest()[:32]
    return f"ip-hash:{h}"


# ── Atomic write ────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: dict) -> None:
    """Write atomically: temp file + rename. Rename is atomic on POSIX and Windows."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


# ── Dedup fingerprint (stable-state hash) ──────────────────────────────────

def _fingerprint(obs: dict) -> str:
    """
    Compute a short SHA-256 fingerprint over the stable (slow-changing) fields
    of an observation. If the fingerprint is unchanged between cycles, the
    observation is functionally identical to its prior state and the
    /userdata write can be skipped entirely.

    Includes only fields that meaningfully change the entity's posture:
    country_code, is_vpn, is_tor, is_proxy. Volatile fields (last_seen,
    first_seen, geo_drift_count, last_drift_at) are EXCLUDED — those change
    on every cycle and would defeat the dedup.
    """
    parts = (
        str(obs.get("country_code", "")),
        "1" if obs.get("is_vpn") else "0",
        "1" if obs.get("is_tor") else "0",
        "1" if obs.get("is_proxy") else "0",
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def is_unchanged(existing: Optional[dict], raw: dict) -> bool:
    """
    True if the new raw observation would not change the existing record's
    stable state. Caller still updates volatile fields (last_seen) but can
    skip the /userdata write.
    """
    if existing is None:
        return False
    new_fp = _fingerprint({
        "country_code": raw.get("country_code", ""),
        "is_vpn": bool(raw.get("is_vpn")),
        "is_tor": bool(raw.get("is_tor")),
        "is_proxy": bool(raw.get("is_proxy")),
    })
    return existing.get("_fp") == new_fp


# ── /userdata shortest-possible serializer ────────────────────────────────

# Deny-list: these must NEVER appear in /userdata writes, even if a future
# allow-list change accidentally includes them. Fields here are stripped before
# the allow-list (USERDATA_SHORT_KEYS) is applied — defense in depth.
USERDATA_WRITE_DENY = {
    "ip",            # raw IP — never
    "country",       # redundant with country_code, drops 8-10 chars per record
    "last_country", "last_country_code",  # redundant with last_drift_at
    "source",        # always heartbeat_osint — useless on disk
    "signals_enqueued",  # lives in abuse_signals/, not ghost profiles
    "tags",          # unused in Heart OSINT analysis
    "enrichment",    # ephemeral, never persisted across cycles
    "enrich_asn", "enrich_org", "enrich_is_datacenter",
    "enrich_is_mobile", "enrich_connection_type", "enrich_host_count",
}

# ── /userdata write path ──────────────────────────────────────────────────


def _to_ghost_signal(obs: dict) -> dict:
    """
    Build a userdata.ghosts.upsert signal payload from a Heart observation.

    The signal dict is stored in GhostProfile.signals AND scanned for
    fields in _OSINT_TOP_LEVEL_FIELDS (country_code, is_vpn, is_tor,
    is_proxy, geo_drift_count, last_drift_at, _fp) which are applied
    to the GhostProfile top-level. We use the canonical long names so
    upsert() can find them — short keys would be opaque to the canonical
    writer. The deny-list still strips any PII before this dict is built.
    """
    sanitized = {k: v for k, v in obs.items() if k not in USERDATA_WRITE_DENY}
    return {
        "type": "ip_observed",
        "source": "heartbeat_osint",
        "country_code": sanitized.get("country_code"),
        "is_vpn": bool(sanitized.get("is_vpn", False)),
        "is_tor": bool(sanitized.get("is_tor", False)),
        "is_proxy": bool(sanitized.get("is_proxy", False)),
        "geo_drift_count": int(sanitized.get("geo_drift_count", 0)),
        "last_drift_at": sanitized.get("last_drift_at"),
        "first_seen": sanitized.get("first_seen"),
        "last_seen": sanitized.get("last_seen"),
        "_fp": sanitized.get("_fp"),
    }


def _userdata_ghost_fingerprint_via_ghosts(ip_hash: str) -> Optional[str]:
    """Read the on-disk ghost profile (via the canonical ghosts.get()) and
    return the cached Heart fingerprint from the top-level `_fp` field."""
    try:
        ghosts = _ghosts()
        existing = ghosts.get(ip_hash)
    except Exception:
        return None
    if existing is None:
        return None
    return existing._fp


def _enqueue_pending_userdata(brain_path: Path, ip_hash: str, payload: dict) -> None:
    """
    Save a write to a pending-queue file so the next cycle can replay it.
    Used when the rate cap is hit.
    """
    pending = brain_path / "heartbeat" / USERDATA_PENDING_DIR
    pending.mkdir(parents=True, exist_ok=True)
    ts = _now().replace(":", "-").replace(".", "-").replace("+", "-")
    fname = f"userdata_pending_{_safe_filename(ip_hash)}_{ts}.json"
    (pending / fname).write_text(
        json.dumps({"ip_hash": ip_hash, "payload": payload}, default=str),
        encoding="utf-8",
    )


def _drain_pending_userdata(brain_path: Path, remaining_quota: int) -> int:
    """
    Replay queued writes from previous cycles. Stops when remaining_quota
    is exhausted. Returns the number of writes drained.
    """
    if remaining_quota <= 0:
        return 0
    pending = brain_path / "heartbeat" / USERDATA_PENDING_DIR
    if not pending.exists():
        return 0
    drained = 0
    for p in sorted(pending.glob("userdata_pending_*.json")):
        if drained >= remaining_quota:
            break
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
            ip_hash = entry.get("ip_hash", "")
            payload = entry.get("payload", {})
            _ghosts().upsert(ghost_id=ip_hash, signal=payload)
            p.unlink()
            drained += 1
        except (json.JSONDecodeError, OSError):
            # Corrupt pending file — discard to avoid replay loops
            try:
                p.unlink()
            except OSError:
                pass
    return drained


def write_to_userdata(obs: dict, userdata_dir: Path) -> tuple[bool, str]:
    """
    WRITE a Heart observation to /userdata as a ghost profile, via the
    canonical userdata.ghosts.upsert() writer. Returns (written, reason).

    Privacy pipeline:
      1. Type-check obs
      2. Strip deny-list fields (silently)
      3. Compare local _fp against on-disk ghost profile (skip if unchanged)
      4. Build a signal payload with canonical field names
      5. Delegate to userdata.ghosts.upsert (canonical writer)
    """
    if not isinstance(obs, dict):
        return False, f"denied: obs must be a dict, got {type(obs).__name__}"

    sanitized = {k: v for k, v in obs.items() if k not in USERDATA_WRITE_DENY}
    ip_hash = sanitized.get("ip_hash", "")
    if not ip_hash:
        return False, "empty: no ip_hash"

    # Dedup: compare local _fp against the on-disk ghost profile.
    local_fp = sanitized.get("_fp")
    on_disk_fp = _userdata_ghost_fingerprint_via_ghosts(ip_hash)
    if local_fp and on_disk_fp and local_fp == on_disk_fp:
        return False, "unchanged: fingerprint match, no write"

    payload = _to_ghost_signal(sanitized)
    if not payload:
        return False, "empty: no fields to write"

    try:
        _ghosts().upsert(ghost_id=ip_hash, signal=payload)
    except OSError as e:
        return False, f"write_failed: {e}"
    return True, "ok"


# ── Cache load / prune ─────────────────────────────────────────────────────

def load(brain_path: str | Path) -> dict:
    """
    READ: load the existing osint_cache.json, pruning observations older than TTL.
    Returns a valid cache dict (never raises — corrupted files are replaced with empty).
    """
    bp = Path(brain_path)
    cache_file = bp / "heartbeat" / CACHE_FILE
    if not cache_file.exists():
        return _empty_cache()

    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        backup = cache_file.with_suffix(".bak")
        try:
            shutil.copy2(cache_file, backup)
        except OSError:
            pass
        return _empty_cache()

    observations = cache.get("observations", {})
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=CACHE_TTL_MINUTES)
    pruned = 0

    for ip_hash, obs in list(observations.items()):
        last_seen_str = obs.get("last_seen", "")
        if last_seen_str:
            try:
                t = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
                if t < cutoff:
                    del observations[ip_hash]
                    pruned += 1
            except (ValueError, TypeError):
                observations[ip_hash] = None  # mark for deletion
                pruned += 1
        else:
            observations[ip_hash] = None
            pruned += 1

    # Remove marked-None entries
    observations = {k: v for k, v in observations.items() if v is not None}

    cache["observations"] = observations
    cache["generated_at"] = _now()
    cache["pruned"] = pruned
    return cache


def _empty_cache() -> dict:
    return {
        "version": CACHE_VERSION,
        "generated_at": _now(),
        "ttl_minutes": CACHE_TTL_MINUTES,
        "observations": {},
        "pruned": 0,
    }


# ── Core: amend one raw observation ─────────────────────────────────────────

def _amend_observation(existing: Optional[dict], raw: dict) -> dict:
    """
    AMEND: merge one raw observation into an existing observation dict (or start fresh).
    Detects geo drift, vpn/tor changes, and marks signals to enqueue.

    Sets _fp (fingerprint) on every entry for /userdata dedup.  If _fp is unchanged
    from the prior cycle, run_phase skips the /userdata write.

    Raw observation shape:
        {
            "ip": "192.0.2.1",
            "country": "Belgium",
            "country_code": "BE",
            "isp": "Proximus",
            "is_vpn": false,
            "is_tor": false,
            "is_proxy": false,
            "source": "heartbeat_osint",
            "enrichment": { "asn": "1234", "org": "..." }  // optional
        }
    """
    now = _now()
    ip_raw = raw.get("ip", "")
    ip_hash = _hash_ip(ip_raw)

    if existing is None:
        entry = {
            "ip_hash": ip_hash,
            "country": raw.get("country", ""),
            "country_code": raw.get("country_code", ""),
            "isp": raw.get("isp", ""),
            "is_vpn": bool(raw.get("is_vpn")),
            "is_tor": bool(raw.get("is_tor")),
            "is_proxy": bool(raw.get("is_proxy")),
            "first_seen": now,
            "last_seen": now,
            "last_country": raw.get("country_code", ""),
            "last_country_code": raw.get("country_code", ""),
            "geo_drift_count": 0,
            "last_drift_at": None,
            "signals_enqueued": [],
            "tags": [],
            "source": raw.get("source", "heartbeat_osint"),
            "_signal": "new_ip",
            "_fp": _fingerprint({
                "country_code": raw.get("country_code", ""),
                "is_vpn": bool(raw.get("is_vpn")),
                "is_tor": bool(raw.get("is_tor")),
                "is_proxy": bool(raw.get("is_proxy")),
            }),
        }
        enrichment = raw.get("enrichment", {})
        if enrichment:
            for k, v in enrichment.items():
                if v is not None:
                    entry[f"enrich_{k}"] = v
        return entry

    # Merge enrichment (ephemeral — always overwritten, not persisted across cycles).
    enrichment = raw.get("enrichment", {})
    if enrichment:
        for k, v in enrichment.items():
            if v is not None:
                existing[f"enrich_{k}"] = v

    if existing.get("_signal") == "new_ip":
        return existing

    # Update last_seen — keeps observation alive (self-renewing TTL)
    existing["last_seen"] = now
    existing["source"] = raw.get("source", existing.get("source", "heartbeat_osint"))

    # Country drift detection
    new_country = raw.get("country_code", "")
    last_country = existing.get("last_country_code", "")
    if new_country and last_country and new_country != last_country:
        existing["geo_drift_count"] = existing.get("geo_drift_count", 0) + 1
        existing["last_drift_at"] = now
        existing["last_country"] = new_country
        existing["last_country_code"] = new_country
        existing["_signal"] = "geo_drift"

    # VPN/Tor/Proxy status changes
    for flag in ("is_vpn", "is_tor", "is_proxy"):
        was = existing.get(flag, False)
        now_flag = bool(raw.get(flag))
        if now_flag and not was:
            existing[flag] = True
            existing.setdefault("_signal", flag)

    # Merge stable surface fields (only overwrite if non-empty)
    for field in ("country", "country_code", "isp"):
        val = raw.get(field)
        if val:
            existing[field] = val

    # Recompute fingerprint after any stable-field change; store for /userdata dedup.
    existing["_fp"] = _fingerprint({
        "country_code": existing.get("country_code", ""),
        "is_vpn": existing.get("is_vpn", False),
        "is_tor": existing.get("is_tor", False),
        "is_proxy": existing.get("is_proxy", False),
    })

    return existing


# ── Load incoming raw observations from signals_incoming/ ───────────────────

def _load_incoming(brain_path: Path) -> list[dict]:
    """READ: collect raw observation files dropped by OSINT producers."""
    incoming = brain_path / "heartbeat" / SIGNALS_INCOMING_DIR
    if not incoming.exists():
        return []

    observations = []
    for p in sorted(incoming.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not raw.get("ip"):
                p.unlink()
                continue
            observations.append(raw)
            p.unlink()
        except (json.JSONDecodeError, OSError):
            try:
                p.unlink()
            except OSError:
                pass
    return observations


# ── Save cache ──────────────────────────────────────────────────────────────

def save(brain_path: str | Path, cache: dict) -> None:
    """WRITE: atomically persist the cache."""
    bp = Path(brain_path)
    cache_file = bp / "heartbeat" / CACHE_FILE
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(cache_file, cache)


# ── Enqueue abuse signals ───────────────────────────────────────────────────

def enqueue_signal(
    ip_hash: str,
    signal_type: str,
    fields: dict,
    brain_path: str | Path,
) -> None:
    """Enqueue an AbuseSignal for the abuse_bridge to consume next cycle."""
    bp = Path(brain_path)
    inbox = bp / "heartbeat" / "abuse_signals"
    inbox.mkdir(parents=True, exist_ok=True)

    signal = {
        "source": "heartbeat_osint",
        "entity_id": ip_hash,
        "signal_type": signal_type,
        "received_at": _now(),
        "fields": fields,
    }

    ts = _now().replace(":", "-").replace(".", "-").replace("+", "-")
    fname = f"osint_{_safe_filename(signal_type)}_{_safe_filename(ip_hash)}_{ts}.json"
    (inbox / fname).write_text(json.dumps(signal), encoding="utf-8")


# ── Top-level run_phase ─────────────────────────────────────────────────────

def run_phase(brain_path: str | Path) -> dict:
    """
    READ latest cache → AMEND with incoming observations → WRITE cache + enqueue signals.

    Also writes changed observations to /userdata via userdata.ghosts.upsert() —
    the canonical writer. Writes are rate-limited to USERDATA_MAX_WRITES_PER_CYCLE
    (default 500). Overflow is queued to Brain/heartbeat/userdata_pending/ and
    replayed in the next cycle.

    Returns:
        {
            "phase": "ingest_osint",
            "observations_seen": N,
            "new_ips": N,
            "geo_drifts": N,
            "signals_enqueued": N,
            "cache_size": N,
            "pruned": N,
            "userdata_writes": N,
            "userdata_skipped": N,
            "userdata_pending_drained": N,
            "ok": bool
        }
    """
    start = datetime.now(timezone.utc)
    bp = Path(brain_path)
    userdata_dir = Path(os.environ.get("USERDATA_DIR", "/var/lib/userdata"))
    max_writes = int(os.environ.get(
        "USERDATA_MAX_WRITES_PER_CYCLE", _DEFAULT_USERDATA_MAX_WRITES_PER_CYCLE
    ))
    # Pending writes from prior cycles count against this cycle's budget.
    pending_drained = _drain_pending_userdata(bp, max_writes)
    writes_remaining = max(0, max_writes - pending_drained)

    # Acquire cross-process lock for the duration of the phase. The Python
    # bridge and the Go reference can run in parallel; without this lock both
    # processes compute `writes_remaining` against the same starting value and
    # both proceed to write past the per-cycle cap.
    lock_path = bp / "heartbeat" / ".osint_run_phase.lock"
    _acquire_lock(lock_path, timeout_s=30)

    cache = load(bp)
    observations = cache["observations"]

    raw_observations = _load_incoming(bp)
    new_ips = 0
    geo_drifts = 0
    signals_enqueued = 0
    userdata_writes = 0
    userdata_skipped = 0

    try:
        for raw in raw_observations:
            ip_hash = _hash_ip(raw.get("ip", ""))
            existing = observations.get(ip_hash)
            amended = _amend_observation(existing, raw)
            observations[ip_hash] = amended

            signal = amended.pop("_signal", None)

            def _do_write() -> None:
                """Write to /userdata if writes_remaining allows, else enqueue."""
                nonlocal userdata_writes, userdata_skipped, writes_remaining
                if writes_remaining <= 0:
                    _enqueue_pending_userdata(bp, ip_hash, _to_ghost_signal(amended))
                    userdata_skipped += 1
                    return
                written, reason = write_to_userdata(amended, userdata_dir)
                if written:
                    userdata_writes += 1
                    writes_remaining -= 1
                else:
                    userdata_skipped += 1

            if signal == "new_ip":
                new_ips += 1
                enqueue_signal(
                    ip_hash=ip_hash,
                    signal_type="ip_observed",
                    fields={
                        "ip": ip_hash,
                        "country": amended.get("country", ""),
                        "country_code": amended.get("country_code", ""),
                        "isp": amended.get("isp", ""),
                        "is_vpn": amended.get("is_vpn", False),
                        "is_tor": amended.get("is_tor", False),
                        "is_proxy": amended.get("is_proxy", False),
                        "geo_drift_count": amended.get("geo_drift_count", 0),
                    },
                    brain_path=bp,
                )
                signals_enqueued += 1
                _do_write()

            elif signal == "geo_drift":
                geo_drifts += 1
                enqueue_signal(
                    ip_hash=ip_hash,
                    signal_type="ip_drift",
                    fields={
                        "ip": ip_hash,
                        "new_country": amended.get("country_code", ""),
                        "old_country": amended.get("last_country", ""),
                        "drift_count": amended.get("geo_drift_count", 0),
                    },
                    brain_path=bp,
                )
                signals_enqueued += 1
                _do_write()

            elif signal in ("is_vpn", "is_tor", "is_proxy"):
                enqueue_signal(
                    ip_hash=ip_hash,
                    signal_type="ip_observed",
                    fields={
                        "ip": ip_hash,
                        signal: True,
                        "country": amended.get("country_code", ""),
                    },
                    brain_path=bp,
                )
                signals_enqueued += 1
                _do_write()
    finally:
        cache["observations"] = observations
        save(bp, cache)
        _release_lock(lock_path)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000

    return {
        "phase": "ingest_osint",
        "observations_seen": len(raw_observations),
        "new_ips": new_ips,
        "geo_drifts": geo_drifts,
        "signals_enqueued": signals_enqueued,
        "cache_size": len(observations),
        "pruned": cache.get("pruned", 0),
        "userdata_writes": userdata_writes,
        "userdata_skipped": userdata_skipped,
        "userdata_pending_drained": pending_drained,
        "duration_ms": int(elapsed),
        "ok": True,
    }
