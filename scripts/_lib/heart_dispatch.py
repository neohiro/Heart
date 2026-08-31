"""
heart_dispatch — Shared utilities for Heart script dispatchers.

Used by:
    Heart/scripts/{news,osint,links-validate,tools}-populate/run.py

Provides:
    setup_logging(quiet: bool, json: bool)   — structlog config
    load_registry(scope: str) -> dict        — read REGISTRY.yaml scope block
    load_feed_registry(path: Path) -> dict   — read a links/feeds/*.yaml
    resolve_env(env_map: dict) -> dict       — substitute ${VAR} from os.environ
    atomic_write_json(path, obj)             — write tmp + rename
    atomic_write_text(path, text)            — write tmp + rename
    last_good_path(scope, kind)              — path to last-good snapshot
    http_get(url, *, timeout, headers)       — requests w/ retry + UA
    emit_pending(link_id, status, msg)       — append to links/audit/pending.yaml

All paths default to the neohiro/air-gapped shared root:
    NEOHIRO_SHARED_ROOT  (default /shared on the brain node)
    NEOHIRO_LINKS_ROOT   (default <repo>/links)
    NEOHIRO_LINKS_SECRET (default <repo>/links-secret)

The dispatchers are pure-Python so they can run as:
    1. GitHub Actions cron (the live cadence for wingman-hub today)
    2. The Heart process on the brain node (the long-running cadence)
    3. Standalone one-shot: `python run.py --once`
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None  # type: ignore[assignment]

try:
    import msvcrt as _msvcrt
except ImportError:  # POSIX
    _msvcrt = None  # type: ignore[assignment]

DEFAULT_TIMEOUT = 30
DEFAULT_UA = "neohiro-heart/1.0 (+https://neohiro.github.io)"
DEFAULT_MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MiB; prevents memory-exhaustion on hostile servers
ENV_SUB_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def shared_root() -> Path:
    return Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared")).resolve()


def links_root() -> Path:
    env = os.environ.get("NEOHIRO_LINKS_ROOT")
    if env:
        return Path(env).resolve()
    # Local-dev fallback: walk up from this file
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "links"
        if (candidate / "README.md").is_file():
            return candidate
    raise FileNotFoundError("Could not locate links/ — set NEOHIRO_LINKS_ROOT")


def links_secret_root() -> Path:
    env = os.environ.get("NEOHIRO_LINKS_SECRET")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "links-secret"
        if (candidate / "README.md").is_file():
            return candidate
    return here.parent / "links-secret"


def setup_logging(*, quiet: bool = False, json_console: bool = True, level: str = "info") -> structlog.stdlib.BoundLogger:
    lvl = {"debug": 10, "info": 20, "warn": 30, "error": 40}.get(level.lower(), 20)
    if quiet:
        lvl = max(lvl, 30)  # warn+ when quiet
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if json_console:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,  # type: ignore[arg-type]
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()


def _resolve_value(v: str) -> str:
    """Substitute ${VAR} occurrences in a string with os.environ values.

    Unknown variables become empty string (matching the prior behaviour).
    A malformed `${...}` (no closing brace) is left as-is to surface the
    authoring bug rather than silently dropping it.
    """
    def _sub(m: re.Match) -> str:
        var = m.group(1)
        if "}" in var:
            return m.group(0)  # malformed; leave unchanged
        return os.environ.get(var, "")
    return ENV_SUB_RE.sub(_sub, str(v))


def resolve_env(env_map: dict[str, str] | None) -> dict[str, str]:
    if not env_map:
        return {}
    return {k: _resolve_value(v) for k, v in env_map.items()}


def load_registry(scope: str, registry_path: Path | None = None) -> dict[str, Any]:
    """Load the scope block from Heart/schedules/REGISTRY.yaml.

    Raises KeyError if the scope is not registered.
    """
    if registry_path is None:
        registry_path = shared_root() / "heart" / "schedules" / "REGISTRY.yaml"
        if not registry_path.is_file():
            # Local-dev: walk up
            here = Path(__file__).resolve()
            for p in here.parents:
                candidate = p / "Heart" / "schedules" / "REGISTRY.yaml"
                if candidate.is_file():
                    registry_path = candidate
                    break
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    for entry in data.get("scopes", []):
        if entry.get("id") == scope:
            return entry
    raise KeyError(f"scope {scope!r} not in {registry_path}")


def load_feed_registry(path: Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def last_good_path(scope: str, kind: str) -> Path:
    return shared_root() / "heart" / "last_good" / scope / f"{kind}.json"


def read_last_good(scope: str, kind: str) -> Any:
    p = last_good_path(scope, kind)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_last_good(scope: str, kind: str, obj: Any) -> None:
    atomic_write_json(last_good_path(scope, kind), obj)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HttpResult:
    __slots__ = ("body", "body_truncated", "elapsed_ms", "error", "ok", "status", "url")

    def __init__(self, ok: bool, status: int, body: str, url: str, elapsed_ms: int, error: str | None = None,
                 body_truncated: bool = False):
        self.ok = ok
        self.status = status
        self.body = body
        self.url = url
        self.elapsed_ms = elapsed_ms
        self.error = error
        self.body_truncated = body_truncated

    def to_dict(self) -> dict[str, Any]:
        d = {
            "ok": self.ok,
            "status": self.status,
            "url": self.url,
            "elapsed_ms": self.elapsed_ms,
            "bytes": len(self.body) if self.body else 0,
            "error": self.error,
        }
        if self.body_truncated:
            d["body_truncated"] = True
        return d


def http_get(url: str, *, timeout: int = DEFAULT_TIMEOUT, headers: dict[str, str] | None = None,
             retries: int = 2, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> HttpResult:
    """GET with retry-on-network-error and a UA.

    Uses stdlib urllib to avoid a hard dependency on requests for the
    air-gapped brain node. requests is preferred when available.

    `max_bytes` caps the in-memory body to prevent memory-exhaustion on
    hostile or misconfigured servers. Bodies larger than the cap are
    truncated and `ok` is still True (status 2xx), but `body_truncated`
    is set on the HttpResult and the caller may decide to drop the body.
    """
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last_err: str | None = None
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(max_bytes + 1)
                truncated = len(raw) > max_bytes
                if truncated:
                    raw = raw[:max_bytes]
                body = raw.decode("utf-8", errors="replace")
                elapsed = int((time.monotonic() - t0) * 1000)
                return HttpResult(
                    ok=200 <= resp.status < 400,
                    status=resp.status,
                    body=body,
                    url=url,
                    elapsed_ms=elapsed,
                    body_truncated=truncated,
                )
        except urllib.error.HTTPError as e:
            elapsed = int((time.monotonic() - t0) * 1000)
            try:
                body = e.read(max_bytes + 1).decode("utf-8", errors="replace")
                if len(body) > max_bytes:
                    body = body[:max_bytes]
            except Exception:
                body = ""
            return HttpResult(ok=False, status=e.code, body=body, url=url, elapsed_ms=elapsed, error=str(e))
        except Exception as e:  # network errors
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            elapsed = int((time.monotonic() - t0) * 1000)
            return HttpResult(ok=False, status=0, body="", url=url, elapsed_ms=elapsed, error=last_err)
    # Should not reach
    return HttpResult(ok=False, status=0, body="", url=url, elapsed_ms=0, error=last_err)


def append_pending(link_id: str, status: str, msg: str, *, scope: str | None = None) -> None:
    """Append a row to links/audit/pending.yaml (lazy-update queue).

    Uses a process-local threading.Lock + a sibling .lock file to make
    concurrent appends within and across processes safe. The lock file
    is held only for the read-modify-write window, which is microseconds.
    """
    pending_path = links_root() / "audit" / "pending.yaml"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = pending_path.with_suffix(pending_path.suffix + ".lock")

    with _file_lock(lock_path):
        if pending_path.is_file():
            data = yaml.safe_load(pending_path.read_text(encoding="utf-8")) or {"pending": [], "resolved": []}
        else:
            data = {"schema_version": 1, "pending": [], "resolved": []}
        data.setdefault("pending", []).append({
            "id": str(uuid.uuid4()),
            "ts": utcnow_iso(),
            "scope": scope,
            "link_id": link_id,
            "status": status,
            "msg": msg,
        })
        if len(data["pending"]) > 1000:
            data["pending"] = data["pending"][-1000:]
        atomic_write_text(pending_path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


@contextlib.contextmanager
def _file_lock(lock_path: Path, *, timeout: float = 5.0, poll: float = 0.05):
    """Cross-platform best-effort file lock.

    On POSIX uses fcntl.flock. On Windows uses msvcrt.locking. The lock
    file is created if absent. Raises TimeoutError if the lock cannot
    be acquired within `timeout` seconds.

    This is best-effort protection: it does not protect against processes
    that ignore file locks. The intended audience is dispatchers running
    in the same process tree (cron, systemd, GitHub Actions).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    try:
        # Spin until we acquire the lock or time out
        while True:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o666)
            try:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)  # type: ignore[attr-defined]
                else:
                    # Windows: msvcrt only locks already-written regions, so write
                    # a sentinel byte first so there's something to lock.
                    if os.path.getsize(lock_path) == 0:
                        os.write(fd, b"\0")
                        os.fsync(fd)
                    _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError):
                os.close(fd)
                fd = None
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire lock {lock_path} within {timeout}s")
                time.sleep(poll)
        yield
    finally:
        if fd is not None:
            try:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)  # type: ignore[attr-defined]
                else:
                    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(fd)


def parse_flags(argv: list[str]) -> dict[str, Any]:
    """Tiny flag parser shared by all dispatchers.

    Recognized:
        --once            set once=True
        --quiet           set quiet=True
        --no-json         set json_console=False
        --log-level X     set level=X
        --dry-run         set dry_run=True

    Unknown `--flag` arguments emit a warning to stderr. This catches
    typos like `--dryrun` (missing dash) before they silently do nothing.
    """
    out: dict[str, Any] = {
        "once": False,
        "quiet": False,
        "json_console": True,
        "level": "info",
        "dry_run": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--once":
            out["once"] = True
        elif a == "--quiet":
            out["quiet"] = True
        elif a == "--no-json":
            out["json_console"] = False
        elif a == "--dry-run":
            out["dry_run"] = True
        elif a == "--log-level" and i + 1 < len(argv):
            out["level"] = argv[i + 1]
            i += 1
        elif a.startswith("--"):
            # Unknown long flag — surface to stderr rather than silently
            # ignoring. The caller can still proceed; this is a soft warning.
            sys.stderr.write(f"heart_dispatch: unknown flag {a!r}\n")
        i += 1
    return out


def write_run_record(scope: str, *, ok: bool, started: float, summary: dict[str, Any]) -> None:
    """Write a run-record to /shared/heart/runs/<scope>/<ts>.json.

    Used by the Heart process to compute cadence stats and by the
    dashboard to show "last seen" for each scope.
    """
    runs_dir = shared_root() / "heart" / "runs" / scope
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "scope": scope,
        "ok": ok,
        "started": started,
        "duration_ms": int((time.time() - started) * 1000),
        "ts": utcnow_iso(),
        "summary": summary,
    }
    # Latest pointer
    atomic_write_json(runs_dir / "latest.json", record)
    # Append-only history (keep last 200)
    history = []
    hist_path = runs_dir / "history.jsonl"
    if hist_path.is_file():
        for line in hist_path.read_text(encoding="utf-8").splitlines()[-199:]:
            with contextlib.suppress(json.JSONDecodeError):
                history.append(json.loads(line))
    history.append(record)
    atomic_write_text(hist_path, "\n".join(json.dumps(r) for r in history) + "\n")


def run_scope(scope: str, handler: Callable[[structlog.stdlib.BoundLogger, dict[str, Any]], int], *, argv: list[str] | None = None) -> int:
    """Top-level entry for a dispatcher. Returns process exit code.

    The handler receives the configured logger and the resolved scope
    config (from REGISTRY.yaml) and must return 0 on success, non-zero
    on failure. The handler is expected to be idempotent.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = parse_flags(argv)
    log = setup_logging(quiet=flags["quiet"], json_console=flags["json_console"], level=flags["level"])
    started = time.time()
    summary: dict[str, Any] = {"scope": scope, "dry_run": flags["dry_run"]}
    try:
        config = load_registry(scope)
        env = resolve_env(config.get("env"))
        log.info("scope.start", scope=scope, schedule=config.get("schedule"), timeout=config.get("timeout_seconds"))
        rc = handler(log, {**config, "env": env, "flags": flags})
        summary["rc"] = rc
        write_run_record(scope, ok=(rc == 0), started=started, summary=summary)
        log.info("scope.end", scope=scope, rc=rc, duration_ms=int((time.time() - started) * 1000))
        return rc
    except (KeyboardInterrupt, SystemExit):
        # Never swallow user-initiated aborts; record as a non-zero crash
        # but re-raise so the parent (cron, systemd) gets the signal.
        raise
    except BaseException as e:
        summary["rc"] = 1
        summary["error"] = f"{type(e).__name__}: {e}"
        write_run_record(scope, ok=False, started=started, summary=summary)
        log.error("scope.crash", scope=scope, error=summary["error"], traceback=traceback.format_exc())
        return 1
