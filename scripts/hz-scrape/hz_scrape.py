#!/usr/bin/env python3
"""hz-scrape — Heart scope that scrapes healthz from all tailscale devices.

Runs as a Heart scheduler scope (see Heart/schedules/REGISTRY.yaml). For every
online device on the tailnet, GETs http://<device>.<tailnet>:9600/healthz,
persists the raw snapshot, merges into a hub latest.json, and writes a
public/metrics/hub/<ts>.json rollup for the SVG renderer.

CLI:
    hz-scrape --once                    one run
    hz-scrape --once --dry-run          do not write anything; print plan
    hz-scrape --config <path.yaml>      load config
    hz-scrape --tailnet <name>          override tailnet name
    hz-scrape --device <fqdn>           scrape a single device (debug)
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import pathlib
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ─── Platform shims ────────────────────────────────────────────────────
# fcntl is Unix-only. On Windows, the lock is a no-op (a best-effort file
# existence check). Production runs on Linux per SPEC_HUB.md; this shim
# keeps tests runnable on the dev box without changing behaviour.
try:
    import fcntl  # type: ignore[import-not-found]
    import errno  # type: ignore[import-not-found]

    def _lock_exclusive(fd: Any) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            # Only EWOULDBLOCK / EAGAIN mean "another process holds the lock".
            # Any other OSError (no errno, EBADF, ENOLCK, …) is a real failure;
            # returning True would silently bypass the lock and let two
            # processes race to write latest.json.
            err = getattr(exc, "errno", None)
            if err in (errno.EWOULDBLOCK, errno.EAGAIN):
                return False
            raise

    def _lock_unlock(fd: Any) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass

except ImportError:  # Windows / non-POSIX
    def _lock_exclusive(fd: Any) -> bool:
        log.warning(
            "fcntl unavailable; file lock is a no-op on this platform. "
            "Concurrent hz-scrape runs may race. Production deployments "
            "must run on Linux."
        )
        return True

    def _lock_unlock(fd: Any) -> None:
        return None

# ─── Defaults ──────────────────────────────────────────────────────────
HEALTHZ_PORT = 9600
HEALTHZ_PATH = "/healthz"
DEFAULT_TAILNET = "tail.ts.net"
DEFAULT_SHARED_ROOT = "/shared"
HUB_DIR = "hub"                       # /shared/hub
SNAPSHOTS_DIR = "snapshots"           # /shared/hub/snapshots/<device>/
LATEST_FILE = "latest.json"
SUMMARY_FILE = "summary.json"
PUBLIC_METRICS_DIR = "public/metrics/hub"
PUBLIC_HEALTH_FILE = "public/health/hub.json"

DEFAULT_TIMEOUT = 5.0       # seconds per scrape
DEFAULT_STALE_AFTER = 300   # s — last_seen older → mark offline
DEFAULT_RETENTION = 3600    # s — evict after this long offline
DEFAULT_MAX_WORKERS = 8
DEFAULT_RUN_TIMEOUT = 90    # s — outer bound on the whole scrape cycle

log = logging.getLogger("hz-scrape")


# ─── Config ────────────────────────────────────────────────────────────

@dataclasses.dataclass
class Config:
    tailnet: str = DEFAULT_TAILNET
    shared_root: str = DEFAULT_SHARED_ROOT
    timeout: float = DEFAULT_TIMEOUT
    stale_after: int = DEFAULT_STALE_AFTER
    retention: int = DEFAULT_RETENTION
    max_workers: int = DEFAULT_MAX_WORKERS
    run_timeout: int = DEFAULT_RUN_TIMEOUT
    exit_node: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        c = cls()
        for k, v in d.items():
            if hasattr(c, k) and v is not None:
                setattr(c, k, v)
        return c


# ─── Tailscale device discovery ────────────────────────────────────────

@dataclasses.dataclass
class Device:
    node_id: str
    hostname: str
    fqdn: str
    online: bool
    is_self: bool = False
    override_port: Optional[int] = None  # only for single-device debug / tests; never set in production


def discover_devices(tailnet: str) -> List[Device]:
    """Run `tailscale status --json` and parse online peers + self.

    Returns a list of Device records. Skips devices without an Online flag
    and skips exit-node-only devices if --exclude-exit is set (not yet wired).
    """
    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except FileNotFoundError:
        log.error("tailscale binary not found; install tailscale or set --device for single-mode")
        return []
    except subprocess.TimeoutExpired:
        log.error("tailscale status --json timed out")
        return []
    if out.returncode != 0:
        log.error("tailscale status failed: %s", out.stderr.strip() or out.stdout.strip())
        return []

    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        log.error("tailscale status returned invalid JSON: %s", exc)
        return []

    devices: List[Device] = []

    # Self
    self_node_id = data.get("SelfNodeID", "")
    self_dns = data.get("SelfDNSName", "")
    if self_dns.endswith("."):
        self_dns = self_dns[:-1]
    if self_node_id and self_dns:
        devices.append(Device(
            node_id=self_node_id,
            hostname=self_dns.split(".")[0],
            fqdn=self_dns,
            online=True,
            is_self=True,
        ))

    # Peers
    for node_id, peer in data.get("Peer", {}).items():
        dns = peer.get("DNSName", "")
        if dns.endswith("."):
            dns = dns[:-1]
        if not dns:
            continue
        online = bool(peer.get("Online", False))
        if not online:
            continue
        devices.append(Device(
            node_id=node_id,
            hostname=dns.split(".")[0],
            fqdn=dns,
            online=True,
        ))

    log.info("discovered %d online device(s) on tailnet %s", len(devices), tailnet)
    return devices


# ─── HTTP scrape ───────────────────────────────────────────────────────

class ScrapeError(Exception):
    """Raised when a single device scrape fails."""


def scrape_device(device: Device, timeout: float) -> Dict[str, Any]:
    """GET <device.fqdn>:<port>/healthz, return parsed JSON.

    IPv6 addresses are wrapped in brackets per RFC 3986 so urlopen resolves
    them correctly (e.g. http://[::1]:9600/healthz).
    In production, device.override_port is None and the port comes from HEALTHZ_PORT.
    """
    if device.override_port is not None:
        port = device.override_port
    else:
        port = HEALTHZ_PORT

    # IPv6 check: an address with at least two colons needs brackets in the URL.
    host = device.fqdn
    if host.count(":") >= 2:
        host = f"[{host}]"
    url = f"http://{host}:{port}{HEALTHZ_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                raise ScrapeError(f"HTTP {resp.status} from {url}")
            body = resp.read(1 * 1024 * 1024)  # 1 MiB cap
            return json.loads(body)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise ScrapeError(f"{type(exc).__name__}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"invalid JSON: {exc}") from exc
    except (TimeoutError, OSError) as exc:
        raise ScrapeError(f"network error: {exc}") from exc


# ─── Atomic write helpers ──────────────────────────────────────────────

def atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    """Stage to <path>.staging.<pid>, replace atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + f".staging.{os.getpid()}")
    try:
        with open(staging, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(staging, path)
    except Exception:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: pathlib.Path, obj: Any) -> None:
    """Atomic JSON write with 2-space indent + trailing newline."""
    data = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    atomic_write_bytes(path, data + b"\n")


# ─── Heartbeat counter (.heartbeat mmap) ───────────────────────────────

def bump_heartbeat(shared_root: pathlib.Path) -> None:
    """Bump the 4-byte heartbeat counter atomically.

    Uses file locking so concurrent processes do not lose increments.
    Falls back gracefully on non-POSIX platforms (best-effort; no guarantees).
    """
    hb = shared_root / ".heartbeat"
    lock_fd = None
    try:
        lock_fd = open(str(hb) + ".lock", "w")
        try:
            _lock_exclusive(lock_fd)
        except OSError:
            pass
        try:
            if hb.exists():
                with open(hb, "rb") as f:
                    raw = f.read(4)
                if len(raw) == 4:
                    (n,) = struct.unpack(">I", raw)
                    n += 1
                else:
                    n = 1
            else:
                n = 1
            atomic_write_bytes(hb, struct.pack(">I", n))
        finally:
            if lock_fd is not None:
                _lock_unlock(lock_fd)
                lock_fd.close()
            try:
                (shared_root / ".heartbeat.lock").unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        log.warning("bump_heartbeat failed: %s", exc)


# ─── Snapshot persistence ──────────────────────────────────────────────

# Counter for monotonic snapshot filenames within a single process. Two
# devices scraped in the same nanosecond get distinct names.
import threading as _threading
_SNAPSHOT_COUNTER = 0
_SNAPSHOT_LOCK = _threading.Lock()


def write_device_snapshot(
    device: Device,
    snap: Dict[str, Any],
    shared_root: pathlib.Path,
) -> None:
    """Write /shared/hub/snapshots/<device>/<ts_ns>-<seq>.json atomically.

    Uses nanosecond Unix time + a process-local sequence to guarantee unique
    filenames even when two snapshots are taken within the same nanosecond.
    The counter is guarded by a lock so future refactors that call this from
    multiple threads cannot silently overwrite a snapshot.
    """
    with _SNAPSHOT_LOCK:
        global _SNAPSHOT_COUNTER
        _SNAPSHOT_COUNTER += 1
        seq = _SNAPSHOT_COUNTER
    ts_ns = time.time_ns()
    base = shared_root / HUB_DIR / SNAPSHOTS_DIR / device.hostname
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{ts_ns}-{seq}.json"
    atomic_write_json(path, snap)


def prune_stale_snapshots(shared_root: pathlib.Path, now: int, retention: int) -> int:
    """Delete per-device snapshot files older than `retention` seconds.

    Returns the number of files removed. Files are matched by the leading
    nanosecond timestamp in the filename (the part before the first '-')
    rather than mtime, so filesystem clock skew or copy-from-backup
    scenarios do not cause premature deletion.

    Safe to run concurrently with write_device_snapshot because:
      1. write_device_snapshot uses nanosecond clock + a process-local
         counter, so a newly written file's ts_ns is always greater than
         any file pruned at the same cycle (modulo clock skew, which the
         caller controls by passing `now` from time.time()).
      2. atomic_write_json writes to a staging file then renames; a prune
         that sees the partial staging file simply deletes it.
    """
    snap_root = shared_root / HUB_DIR / SNAPSHOTS_DIR
    if not snap_root.exists():
        return 0
    cutoff = now - retention
    cutoff_ns = cutoff * 1_000_000_000
    pruned = 0
    for dev_dir in snap_root.iterdir():
        if not dev_dir.is_dir():
            continue
        for snap_file in dev_dir.iterdir():
            if not snap_file.is_file() or snap_file.suffix != ".json":
                continue
            # Filename: "<ts_ns>-<seq>.json" — extract the leading ns.
            stem = snap_file.stem
            dash = stem.find("-")
            if dash <= 0:
                continue
            try:
                ts_ns = int(stem[:dash])
            except ValueError:
                # Filename not generated by write_device_snapshot; leave it.
                continue
            if ts_ns < cutoff_ns:
                try:
                    snap_file.unlink()
                    pruned += 1
                except OSError as exc:
                    log.warning("prune: could not delete %s: %s", snap_file, exc)
    return pruned


# ─── Latest merge ──────────────────────────────────────────────────────

def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Coerce a value to int, returning default on any type/value error.

    Distinguishes 'missing/invalid' (None) from 'valid zero' (0) so downstream
    consumers can detect corrupt healthz payloads.
    """
    if value is None:
        return default
    if isinstance(value, bool):  # bool is a subclass of int; reject
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return default
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def _coerce_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Coerce a value to float; returns default on any error."""
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    if isinstance(value, str):
        try:
            v = float(value.strip())
        except (TypeError, ValueError):
            return default
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    return default


def _per_device_summary(snap: Dict[str, Any], now: int) -> Dict[str, Any]:
    """Compact per-device summary from a raw healthz snapshot.

    Invalid/missing fields are represented as ``None`` (not 0) so the SVG
    renderer can distinguish 'valid zero' from 'no data'. Coercion is
    strict: non-numeric strings, NaN, and infinity all yield None.
    """
    host = snap.get("host") or {}
    if not isinstance(host, dict):
        host = {}
    services = snap.get("services") or {}
    if not isinstance(services, dict):
        services = {}
    containers = snap.get("containers")
    mh = snap.get("mental_health") or {}
    if not isinstance(mh, dict):
        mh = {}

    mem_total = _coerce_int(host.get("mem_total_mb"))
    mem_avail = _coerce_int(host.get("mem_available_mb"))
    if mem_total and mem_total > 0 and mem_avail is not None and 0 <= mem_avail <= mem_total:
        mem_used_pct = round((mem_total - mem_avail) / mem_total * 100, 1)
    else:
        mem_used_pct = None

    bad_services = sorted([
        name for name, st in services.items()
        if isinstance(st, str) and st not in ("ok", "OK", "")
    ])

    band = mh.get("band")
    if band not in ("green", "yellow", "red", "black"):
        band = "unknown"

    # Per-collector status (from Go healthz's CollectorStatus field). Lets
    # the hub rollup surface "docker collector failing" without forcing the
    # operator to grep the free-form notes strings.
    cs_raw = mh.get("collector_status")
    if not isinstance(cs_raw, dict):
        cs_raw = {}
    failed_collectors = sorted(
        name for name, st in cs_raw.items()
        if isinstance(st, str) and st != "ok"
    )

    return {
        "hostname": snap.get("hostname") or "",
        "role": snap.get("role") or "",
        "tag": list(snap.get("tag") or []),
        "last_seen": now,
        "age_s": 0,
        "band": band,
        "score": _coerce_int(mh.get("score")) or 0,
        "online": True,
        "uptime_s": _coerce_int(host.get("uptime_s")),
        "mem_used_pct": mem_used_pct,
        "disk_used_pct": _coerce_int(host.get("disk_root_used_pct")),
        "cpu_pct": _coerce_float(host.get("cpu_pct")),
        "container_count": (
            len(containers) if isinstance(containers, list) else None
        ),
        "bad_services": bad_services,
        "notes": list(mh.get("notes") or []),
        "failed_collectors": failed_collectors,
    }


def merge_latest(
    device_results: Dict[Tuple[str, str], Tuple[Device, Optional[Dict[str, Any]], Optional[str]]],
    now: int,
) -> Dict[str, Any]:
    """Build the fresh-merge portion of hub latest.json from a single cycle's results.

    `device_results[(hostname, node_id)] = (Device, snapshot_or_None, error_or_None)`.
    The hostname is used as the JSON key in the output; node_id disambiguates
    collisions so no data is silently lost when two devices share a hostname.
    Returns a hub latest dict for devices scraped in this cycle only; offline
    carry-forward is performed by ``merge_with_prior``.
    """
    out: Dict[str, Any] = {
        "version": 1,
        "t": now,
        "device_count": 0,
        "devices": {},
    }

    for key, (dev, snap, _err) in device_results.items():
        hostname, _node_id = key
        if dev is None or snap is None:
            continue
        summary = _per_device_summary(snap, now)
        out["devices"][hostname] = summary

    out["device_count"] = len(out["devices"])

    bands: Dict[str, int] = {"green": 0, "yellow": 0, "red": 0, "black": 0}
    total_score = 0
    for d in out["devices"].values():
        band = d.get("band", "")
        if band in bands:
            bands[band] += 1
        total_score += d.get("score", 0) or 0
    out["summary"] = {
        "bands": bands,
        "avg_score": round(total_score / max(1, out["device_count"]), 1),
        "online_count": out["device_count"],
        "errors": [
            {"device": key[0], "error": err}
            for key, (_, _, err) in device_results.items()
            if err is not None
        ],
    }
    return out


def merge_with_prior(
    fresh: Dict[str, Any],
    prior: Optional[Dict[str, Any]],
    now: int,
    stale_after: int,
    retention: int,
) -> Dict[str, Any]:
    """Merge fresh results with prior latest; mark stale + evict."""
    if prior is None:
        return fresh

    out = {
        "version": 1,
        "t": now,
        "device_count": 0,
        "devices": {},
    }

    # Fresh results win
    for hostname, summary in fresh["devices"].items():
        out["devices"][hostname] = summary

    # Carry forward offline devices
    for hostname, prev in (prior.get("devices") or {}).items():
        if hostname in out["devices"]:
            continue
        last_seen = int(prev.get("last_seen", 0))
        age = now - last_seen
        if age > retention:
            # Evict
            continue
        prev_copy = dict(prev)
        prev_copy["online"] = False
        prev_copy["age_s"] = age
        out["devices"][hostname] = prev_copy

    out["device_count"] = sum(1 for d in out["devices"].values() if d.get("online"))

    bands = {"green": 0, "yellow": 0, "red": 0, "black": 0}
    total_score = 0
    online_count = 0
    for d in out["devices"].values():
        if d.get("online"):
            online_count += 1
            band = d.get("band", "")
            if band in bands:
                bands[band] += 1
            total_score += d.get("score", 0)
    out["summary"] = {
        "bands": bands,
        "avg_score": round(total_score / max(1, online_count), 1),
        "online_count": online_count,
        "errors": fresh.get("summary", {}).get("errors", []),
    }
    return out


# ─── Public rollup ─────────────────────────────────────────────────────

def write_public_rollup(latest: Dict[str, Any], shared_root: pathlib.Path) -> None:
    """Write /shared/public/metrics/hub/<ts>.json + /shared/public/health/hub.json."""
    now = int(time.time())
    rollup_path = shared_root / PUBLIC_METRICS_DIR / f"{now}.json"
    atomic_write_json(rollup_path, latest)

    # Mirror
    mirror_path = shared_root / PUBLIC_HEALTH_FILE
    atomic_write_json(mirror_path, latest)


# ─── Main scrape loop ──────────────────────────────────────────────────

def run_once(
    cfg: Config,
    only_device: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    shared_root = pathlib.Path(cfg.shared_root)
    if not shared_root.exists():
        raise SystemExit(f"shared root {cfg.shared_root} does not exist")

    # Lock: prevent concurrent runs
    lock_path = shared_root / HUB_DIR / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    if not _lock_exclusive(lock_fd):
        log.warning("another hz-scrape is running; skipping this cycle")
        lock_fd.close()
        return {"skipped": True}

    try:
        # Discover
        if only_device:
            # Parse: may be "host", "host:port", "host.ts.net", or "1.2.3.4[:port]".
            # For directory naming we want a sanitised bare hostname — strip port
            # and any trailing dot. IPv6 FQDNs are extremely rare on the tailnet.
            raw = only_device
            port_override: Optional[int] = None
            if raw.startswith("["):
                # [ipv6]:port
                end = raw.find("]")
                if end > 0:
                    after = raw[end + 1:]
                    if after.startswith(":"):
                        try:
                            port_override = int(after[1:])
                        except ValueError:
                            raise SystemExit(f"--device: invalid port in {raw!r}")
                    raw = raw[1:end]
            elif ":" in raw and raw.count(":") == 1:
                # host:port (single colon = not IPv6)
                h, _, p = raw.partition(":")
                if p.isdigit():
                    port_override = int(p)
                    raw = h
            hostname = raw.rstrip(".").split(".")[0]
            if not hostname:
                raise SystemExit(f"--device: empty hostname in {only_device!r}")
            devices = [Device(
                node_id="manual",
                hostname=hostname,
                fqdn=raw,
                online=True,
                override_port=port_override,
            )]
        else:
            devices = discover_devices(cfg.tailnet)
        if not devices:
            log.warning("no devices to scrape")

        # Concurrent scrape. Keyed by (hostname, node_id) to handle hostname
        # collisions (two devices with the same DNS name on different tailscale
        # IDs). The per-device snapshot write uses dev from results.values() so
        # is unaffected; merge_latest unpacks the tuple to get the hostname.
        results: Dict[Tuple[str, str], Tuple[Device, Optional[Dict[str, Any]], Optional[str]]] = {}

        def _do(dev: Device) -> Tuple[Tuple[str, str], Optional[Dict[str, Any]], Optional[str]]:
            key = (dev.hostname, dev.node_id)
            try:
                snap = scrape_device(dev, cfg.timeout)
                return key, snap, None
            except ScrapeError as exc:
                return key, None, str(exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("unexpected error scraping %s", dev.hostname)
                return key, None, f"unexpected: {exc}"

        with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
            futures = {ex.submit(_do, d): d for d in devices}
            timed_out = False
            try:
                for fut in cf.as_completed(futures, timeout=cfg.run_timeout):
                    try:
                        key, snap, err = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        key, snap, err = ("?", "?"), None, f"future error: {exc}"
                    dev = futures[fut]
                    results[key] = (dev, snap, err)
            except cf.TimeoutError:
                log.error("scrape cycle exceeded run_timeout=%ds; cancelling pending futures", cfg.run_timeout)
                timed_out = True
                # Cancel every future that hasn't completed. Without this,
                # the workers keep scraping and the executor's __exit__
                # waits for them — and worse, late results can leak into
                # the next cycle's `results` dict because the `results`
                # mapping is reused.
                for fut in futures:
                    if not fut.done():
                        fut.cancel()
            if timed_out:
                # Wait for cancellation to actually settle so log lines from
                # background threads are flushed before we move on.
                ex.shutdown(wait=True, cancel_futures=True)

        now = int(time.time())
        # Persist per-device snapshots
        if not dry_run:
            # Prune stale snapshots first so this cycle's writes don't
            # race with the prune on the same directory. The retention
            # window matches merge_with_prior: snapshots older than
            # `retention` seconds are deleted (and the directory entries
            # for stale devices in latest.json are evicted by
            # merge_with_prior at the same time).
            pruned = prune_stale_snapshots(shared_root, now, cfg.retention)
            if pruned:
                log.info("pruned %d stale snapshot file(s) older than %ds", pruned, cfg.retention)
            for _key, (dev, snap, _err) in results.items():
                if dev is not None and snap is not None:
                    write_device_snapshot(dev, snap, shared_root)

        # Build fresh latest
        fresh = merge_latest(results, now)

        # Merge with prior
        if not dry_run:
            prior_path = shared_root / HUB_DIR / LATEST_FILE
            prior: Optional[Dict[str, Any]] = None
            if prior_path.exists():
                try:
                    prior = json.loads(prior_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    log.warning("could not read prior latest.json: %s", exc)
            latest = merge_with_prior(fresh, prior, now, cfg.stale_after, cfg.retention)

            # Write latest + summary
            atomic_write_json(shared_root / HUB_DIR / LATEST_FILE, latest)

            # Compact summary
            summary = {
                "version": 1,
                "t": latest["t"],
                "device_count": latest["device_count"],
                "online_count": latest["summary"]["online_count"],
                "avg_score": latest["summary"]["avg_score"],
                "bands": latest["summary"]["bands"],
                "devices": [
                    {"hostname": h, "band": d.get("band"), "score": d.get("score"),
                     "online": d.get("online"), "age_s": d.get("age_s")}
                    for h, d in latest["devices"].items()
                ],
            }
            atomic_write_json(shared_root / HUB_DIR / SUMMARY_FILE, summary)

            # Public rollup
            write_public_rollup(latest, shared_root)

            # Bump heartbeat
            bump_heartbeat(shared_root)
        else:
            latest = fresh
            log.info("[dry-run] would write %d device snapshot(s)", len(results))

        log.info(
            "scrape complete: %d scraped, %d online, %d errors, avg_score=%s",
            len(results),
            latest.get("summary", {}).get("online_count", 0),
            len(latest.get("summary", {}).get("errors", [])),
            latest.get("summary", {}).get("avg_score", "n/a"),
        )
        return latest
    finally:
        try:
            _lock_unlock(lock_fd)
        except OSError:
            pass
        lock_fd.close()


# ─── CLI ───────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="hz-scrape", description=__doc__.splitlines()[0])
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="Don't write anything")
    p.add_argument("--prune-only", action="store_true",
                   help="Only prune stale snapshots; do not scrape or write")
    p.add_argument("--config", type=pathlib.Path, help="Config YAML/JSON path")
    p.add_argument("--tailnet", help="Tailscale tailnet name (e.g. tail.ts.net)")
    p.add_argument("--shared-root", help="Path to /shared")
    p.add_argument("--device", help="Scrape a single device (FQDN or hostname)")
    p.add_argument("--timeout", type=float, help="Per-device HTTP timeout (seconds)")
    p.add_argument("--max-workers", type=int, help="Concurrent scrapes")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    cfg = Config()
    if args.config:
        import yaml
        with open(args.config, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = Config.from_dict(data)
    # CLI overrides
    if args.tailnet:
        cfg.tailnet = args.tailnet
    if args.shared_root:
        cfg.shared_root = args.shared_root
    if args.timeout is not None:
        cfg.timeout = args.timeout
    if args.max_workers is not None:
        cfg.max_workers = args.max_workers

    try:
        if getattr(args, "prune_only", False):
            logging.basicConfig(level=logging.INFO,
                              format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
            now = int(time.time())
            shared = pathlib.Path(cfg.shared_root)
            pruned = prune_stale_snapshots(shared, now, cfg.retention)
            log.info("pruned %d stale snapshot(s)", pruned)
            return 0
        result = run_once(cfg, only_device=args.device, dry_run=args.dry_run)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("hz-scrape failed: %s", exc)
        return 1

    if result.get("skipped"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
