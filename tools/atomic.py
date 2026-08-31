"""
atomic.py — neohiro shared atomic write primitives.

Design contract:
  Every function here uses mkstemp(3) + fsync(2) + os.replace(2) so that:

    1. The target file is never in a partially-written state for a reader —
       os.replace(2) is atomic on POSIX (new inode replaces old atomically)
       and on Windows NTFS (ReplaceFile API).

    2. Two concurrent callers on the same path never clobber each other —
       mkstemp generates a truly-random suffix, unlike Path.with_suffix(".tmp")
       which produces a shared filename.

    3. A crash or SIGKILL between write and rename leaves the target intact —
       the old file is only replaced after the new file is fully flushed.

  Do NOT use Path.with_suffix, Path.write_text, or any pattern that writes
  directly to the target path.  All new writes must route through here.

  Naming convention for callers:
    prefix  = short, lowercase, dot-prefixed (e.g. ".mode.", ".cache.", ".seen.")
    suffix  = ".tmp"
    dir     = target.parent  (must be on the same filesystem as target for rename)

Error model:
  All functions raise the underlying exception on failure (OSError for
  filesystem errors, TypeError/ValueError for serialisation errors). The
  target file is never left in a partially-written state; the temp file is
  cleaned up before the exception propagates.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


def _atomic_write_impl(path: Path, data: bytes, *, prefix: str) -> None:
    """Internal: atomic write of raw bytes. Used by all public primitives.

    This is the single source of truth for the mkstemp + fsync + os.replace
    sequence.  Public functions serialise their input to bytes and call this.
    """
    tmp_fd = None
    tmp_str = None
    try:
        tmp_fd, tmp_str = tempfile.mkstemp(
            prefix=prefix or ".atomic.",
            suffix=".tmp",
            dir=str(path.parent),
        )
    except Exception:
        # fd is None here — mkstemp failed to open, nothing to clean up
        raise
    try:
        # fdopen raises on illegal mode strings; wrap so we can unlink the
        # fd before re-raising to avoid leaking the fd number.
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp_fd = None  # fd is closed by the with-block; clear so unlink is the only action in except
        os.replace(tmp_str, path)
        tmp_str = None  # rename consumed it; clear so the except doesn't attempt a double-unlink
    except BaseException:
        # BaseException (not Exception) so KeyboardInterrupt and SystemExit
        # still clean up the temp file.  Without this, a SIGINT during a
        # write would leak .tmp files in /shared forever.
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_str is not None:
            try:
                os.unlink(tmp_str)
            except OSError:
                pass
        raise


def write_json(path: Path, data: Any, *, prefix: str = "", indent: int = 2) -> None:
    """
    Atomically write a JSON-serialisable value (typically a dict) to ``path``.

    The value is serialised with ``json.dumps(data, indent=indent, default=str)``.
    Pass ``indent=None`` for a compact single-line file. ``default=str`` ensures
    non-JSON-native types like ``datetime`` and ``Path`` are coerced to strings
    rather than raising ``TypeError``; callers that need strict JSON should
    serialise themselves and call :func:`write_json_text`.

    Args:
        path:    destination file (renamed atomically on success)
        data:    any JSON-serialisable value (typically a dict)
        prefix:  mkstemp prefix, e.g. ".heartbeat."  (default: ".atomic.")
        indent:  passed to json.dumps (default 2; pass None for compact)

    Raises:
        TypeError: if data is not JSON-serialisable even with default=str
        OSError:   if the temp file cannot be created, written, or renamed;
                   the target is untouched on any failure.
    """
    text = json.dumps(data, indent=indent, default=str)
    _atomic_write_impl(path, text.encode("utf-8"), prefix=prefix)


def write_json_text(path: Path, text: str, *, prefix: str = "") -> None:
    """
    Atomically write a pre-serialised JSON string to ``path``.

    Use this when the caller needs full control over the JSON serialisation
    (custom encoder, specific sort order, trailing newline, etc.). The string
    is written verbatim as UTF-8 bytes; ``atomic`` does not re-parse or
    validate it.

    If you have a plain Python dict, prefer :func:`write_json` which uses the
    project's standard indent and default settings.

    Args:
        path:   destination file
        text:   the exact JSON string to write (must be valid JSON; not validated)
        prefix: mkstemp prefix

    Raises:
        OSError: on filesystem failure
    """
    _atomic_write_impl(path, text.encode("utf-8"), prefix=prefix)


def write_text(path: Path, text: str, *, prefix: str = "") -> None:
    """
    Atomically write a plain text string to ``path``.

    Same guarantees as write_json but for plain text (no JSON serialisation).
    The text is encoded as UTF-8.

    Args:
        path:   destination file
        text:   the text to write
        prefix: mkstemp prefix

    Raises:
        OSError: on filesystem failure
    """
    _atomic_write_impl(path, text.encode("utf-8"), prefix=prefix)


def write_yaml_multi_doc(path: Path, docs: list[Any], *, prefix: str = "") -> None:
    """
    Atomically write a list of YAML documents to ``path``, separated by ``---``.

    Each element of ``docs`` is serialised as a separate YAML document. This
    matches the format used by intuition.yaml, self_heal.yaml, and other
    multi-entry heartbeat files that rely on ``---`` document boundaries.

    Args:
        path:  destination file (renamed atomically on success)
        docs:  list of YAML-serialisable values (one document each)
        prefix: mkstemp prefix

    Raises:
        TypeError: if PyYAML is not installed
        OSError:  if the temp file cannot be created, written, or renamed;
                  the target is untouched on any failure.

    Note:
        An empty ``docs`` list is valid and writes a 0-byte file (equivalent
        to clearing all entries, e.g. when the intuition cap filters
        everything). This is intentional; callers that want to skip the write
        when docs is empty must do so themselves.
    """
    if not _HAS_YAML:
        raise TypeError("PyYAML is required for write_yaml_multi_doc; pip install pyyaml")
    text = yaml.dump_all(
        docs,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    _atomic_write_impl(path, text.encode("utf-8"), prefix=prefix)


def write_bytes(path: Path, data: bytes, *, prefix: str = "") -> None:
    """
    Atomically write a byte string to ``path``.

    Used by callers that have raw bytes (e.g. msgpack, protobuf, binary
    formats) and want to control the exact bytes that hit disk.

    Args:
        path:   destination file
        data:   the bytes to write
        prefix: mkstemp prefix

    Raises:
        OSError: on filesystem failure
    """
    _atomic_write_impl(path, data, prefix=prefix)


def write_yaml(path: Path, data: Any, *, prefix: str = "", **yaml_kwargs) -> None:
    """
    Atomically write a YAML-serialisable value to ``path``.

    The value is serialised with ``yaml.dump(data, **yaml_kwargs)`` using
    the project's standard settings (default_flow_style=False,
    sort_keys=False, allow_unicode=True) unless overridden.

    Args:
        path:       destination file (renamed atomically on success)
        data:       any YAML-serialisable value (typically a dict or list)
        prefix:     mkstemp prefix, e.g. ".heartbeat."  (default: ".atomic.")
        yaml_kwargs: passed to yaml.dump (e.g. default_flow_style, sort_keys)

    Raises:
        TypeError:  if PyYAML is not installed (import error)
        TypeError:  if data is not YAML-serialisable
        OSError:    if the temp file cannot be created, written, or renamed;
                    the target is untouched on any failure.
    """
    if not _HAS_YAML:
        raise TypeError("PyYAML is required for write_yaml; pip install pyyaml")
    text = yaml.dump(
        data,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        **yaml_kwargs,
    )
    _atomic_write_impl(path, text.encode("utf-8"), prefix=prefix)


# ── Audit schema validation ────────────────────────────────────────────────

_AUDIT_REQUIRED_FIELDS = {
    "heartbeat": {"ts", "phase", "outcome", "elapsed_ms"},
    "stale": {"ts", "cycle", "pruned"},
    "shared_prune": {"ts", "cycle", "usage_pct", "files_pruned", "bytes_pruned"},
    "self_heal": {"ts", "cycle", "actions"},
    "intuition": {"ts", "cycle", "mode", "per_scope_weights", "aggregate", "threshold", "consensus_reached", "escalated"},
    "reflexive_findings": {"ts", "cycle", "category", "severity", "target", "message"},
}


def validate_audit_entry(entry: dict[str, Any], audit_type: str) -> list[str]:
    """
    Validate an audit entry against the expected schema for its type.

    Args:
        entry: the parsed audit entry (single document from safe_load_all)
        audit_type: one of "heartbeat", "stale", "shared_prune", "self_heal",
                    "intuition", "reflexive_findings"

    Returns:
        List of validation error messages (empty if valid).
    """
    if not isinstance(entry, dict):
        return [f"entry is not a dict: {type(entry).__name__}"]
    if audit_type not in _AUDIT_REQUIRED_FIELDS:
        return [f"unknown audit_type: {audit_type!r}"]
    required = _AUDIT_REQUIRED_FIELDS[audit_type]
    missing = required - set(entry.keys())
    if missing:
        return [f"missing required fields: {sorted(missing)}"]
    return []


def load_and_validate_audit(path: Path, audit_type: str) -> list[dict[str, Any]]:
    """
    Load and validate an audit YAML file.

    Uses safe_load_all to read multi-document YAML, then validates each
    document against the schema for the given audit_type.

    Args:
        path: path to the audit YAML file
        audit_type: one of "heartbeat", "stale", "shared_prune", "self_heal",
                    "intuition", "reflexive_findings"

    Returns:
        List of valid audit entries (dicts). Invalid entries are logged
        as warnings and skipped.

    Raises:
        OSError: if file cannot be read
        TypeError: if PyYAML is not installed
    """
    if not _HAS_YAML:
        raise TypeError("PyYAML is required for load_and_validate_audit; pip install pyyaml")
    if not path.is_file():
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise OSError(f"failed to read audit file {path}: {e}") from e

    try:
        docs = list(yaml.safe_load_all(text)) or []
    except yaml.YAMLError as e:
        raise OSError(f"failed to parse audit YAML {path}: {e}") from e

    valid: list[dict[str, Any]] = []
    for i, doc in enumerate(docs):
        if doc is None:
            continue
        if not isinstance(doc, dict):
            log.warning("audit_invalid_doc_type", path=str(path), doc_index=i, type=type(doc).__name__)
            continue
        # Some writers wrap single entries in a list (e.g. "- ts: ...")
        # safe_load_all gives us the list element as a dict.
        errors = validate_audit_entry(doc, audit_type)
        if errors:
            log.warning("audit_validation_failed", path=str(path), doc_index=i, errors=errors)
            continue
        valid.append(doc)

    return valid


# ── Cross-platform file locking ──────────────────────────────────────────

class FileLock:
    """
    Cross-platform advisory file lock using fcntl (POSIX) or msvcrt (Windows).

    This is a proper advisory lock that:
    - Is automatically released when the process exits (even on crash/SIGKILL)
    - Doesn't leave stale lock files behind
    - Supports timeout and non-blocking modes
    - Works on both POSIX (fcntl.flock) and Windows (msvcrt.locking)

    Usage:
        lock = FileLock(Path("/tmp/mylock"))
        with lock:
            # critical section
            pass
        # lock automatically released

    Or manually:
        lock = FileLock(Path("/tmp/mylock"))
        lock.acquire(timeout=30)
        try:
            # critical section
        finally:
            lock.release()
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd = None
        self._locked = False

    @property
    def path(self) -> Path:
        """The lock file path (read-only)."""
        return self._path

    def acquire(self, timeout_s: float = 30.0, blocking: bool = True) -> bool:
        """
        Acquire the lock.

        Args:
            timeout_s: maximum time to wait for the lock (ignored if blocking=False)
            blocking: if False, return immediately without waiting

        Returns:
            True if lock acquired, False if timeout/non-blocking and not available

        Raises:
            TimeoutError: if blocking=True and timeout exceeded
            OSError: on filesystem error
        """
        if self._locked:
            return True

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if self._path.is_dir():
                    try:
                        self._path.rmdir()
                    except OSError:
                        pass
                self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
                break
            except PermissionError:
                if not self._path.is_dir():
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire lock {self._path} within {timeout_s}s"
                    ) from None
                time.sleep(0.05)

        while True:
            try:
                if sys.platform == "win32":
                    # Windows: use msvcrt.locking
                    import msvcrt
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    # POSIX: use fcntl.flock
                    import fcntl
                    flags = fcntl.LOCK_EX
                    if not blocking:
                        flags |= fcntl.LOCK_NB
                    fcntl.flock(self._fd, flags)
                self._locked = True
                return True
            except (OSError, IOError) as e:
                if sys.platform == "win32":
                    # msvcrt.locking raises OSError with errno=EDEADLOCK or EACCES
                    pass
                else:
                    # fcntl.flock raises IOError with errno=EWOULDBLOCK or EAGAIN
                    pass

                if not blocking:
                    self._close_fd()
                    return False

                if time.monotonic() >= deadline:
                    self._close_fd()
                    raise TimeoutError(
                        f"Could not acquire lock {self._path} within {timeout_s}s"
                    ) from e
                time.sleep(0.1)

    def release(self) -> None:
        """Release the lock. Idempotent."""
        if not self._locked:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass  # best effort
        finally:
            self._close_fd()
            self._locked = False

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def acquire_file_lock(path: Path, timeout_s: int = 30) -> FileLock:
    """
    Convenience function: acquire a file lock and return the lock object.

    The caller must call lock.release() when done, or use the lock as
    a context manager: `with acquire_file_lock(path): ...`

    Args:
        path: path to the lock file
        timeout_s: maximum time to wait for the lock

    Returns:
        FileLock instance (already acquired)

    Raises:
        TimeoutError: if timeout exceeded
    """
    lock = FileLock(path)
    lock.acquire(timeout_s=timeout_s)
    return lock
