"""
heart_shared_prune.py — Shared-storage auto-prune for Heart.

Owns:
  - prune_largest_first: scan safe_subtrees and delete largest-first up to budget
  - prune_and_save: TTL-prune the OSINT cache (delegates to osint_cache.load+save
    with a dry-run gate)

Dry-run gate (three signals, checked in order):
  1. heart.DRY_RUN module flag (authoritative; kept in sync with heartctl.cmd_phase)
  2. HEART_DRY_RUN env var (for standalone callers and test setup)
  3. heartbeat/.dry_run sentinel file (kill-switch or sandboxed test without env vars)
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def _is_dry_run(brain_path: str | Path) -> bool:
    """True when the caller has signalled dry-run mode.

    Three signals, checked in order:
      1. heart.DRY_RUN module flag (kept in sync with heartctl.cmd_phase even
         after the env var is restored to its prior value)
      2. HEART_DRY_RUN env var (for standalone callers and test setup)
      3. heartbeat/.dry_run sentinel file (so a kill-switch or sandboxed
         test can disable disk writes without setting env vars)
    """
    try:
        from Heart.tools.heart import DRY_RUN as _heart_dry_run
        if _heart_dry_run:
            return True
    except (ImportError, AttributeError):
        pass
    if os.environ.get("HEART_DRY_RUN", "") not in ("", "0"):
        return True
    return (Path(brain_path) / "heartbeat" / ".dry_run").is_file()


def _is_path_under(child: Path, root: Path) -> bool:
    """True when child's resolved path lives under root.

    Uses pathlib.Path.is_relative_to (Python 3.9+) and resolves both sides so
    symlinks are followed for the comparison — matches the way callers think
    about subtree containment, regardless of which path representation they
    happened to pass in.
    """
    try:
        return child.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        # resolve() can fail on broken symlinks or platform-specific edge cases.
        return False


def _collect_files(root: Path) -> list[tuple[int, Path]]:
    """Recursively collect (size, path) for every file under root.

    Uses os.scandir directly so each directory is opened exactly once; is_file / stat
    are served from the dirent cache where d_type is available (ext4, NTFS, APFS).
    Symlinks are never followed (follow_symlinks=False for is_dir) AND every entry
    is checked against the resolved root — so a symlink under root pointing
    outside the subtree is dropped before any stat() / unlink() call.
    """
    try:
        root_resolved = root.resolve(strict=False)
    except OSError:
        return []
    return _collect_files_under(root, root_resolved)


def _collect_files_under(root: Path, root_resolved: Path) -> list[tuple[int, Path]]:
    """Inner recursion: root is already resolved to root_resolved, so each
    entry is checked against the resolved root without re-resolving on every
    call. This is a micro-optimization but more importantly it ensures the
    containment check uses a single, consistent root throughout the walk.
    """
    files: list[tuple[int, Path]] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        sub = Path(entry.path)
                        if not _is_path_under(sub, root_resolved):
                            continue
                        files.extend(_collect_files_under(sub, root_resolved))
                    else:
                        st = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(st.st_mode):
                            p = Path(entry.path)
                            if not _is_path_under(p, root_resolved):
                                continue
                            files.append((st.st_size, p))
                except OSError:
                    continue
    except OSError:
        pass
    return files


def prune_largest_first(
    safe_subtrees: list[Path],
    budget_bytes: int,
) -> tuple[int, int]:
    """
    Delete files from `safe_subtrees` (largest first) until at least
    `budget_bytes` has been freed or no files remain.

    Only files inside `safe_subtrees` may be touched. Subtrees that do not
    exist are skipped silently. Accepts pathlib.Path or str entries in the
    input list; all internal paths are normalized to Path.

    Uses a direct os.scandir recursion (no os.walk) so each directory is
    opened exactly once and `is_file` / `stat` are served from the dirent
    cache when d_type is available. Symlinks are never followed AND every
    entry is verified to resolve under its declared safe_subtree before
    being acted on — prevents a symlink from a safe subtree pointing
    outside it from being followed or deleted.

    Returns (files_pruned, bytes_pruned).
    """
    files: list[tuple[int, Path]] = []
    for root in safe_subtrees:
        root = Path(root)
        if root.is_dir():
            files.extend(_collect_files(root))
    files.sort(key=lambda x: x[0], reverse=True)

    bytes_pruned = 0
    files_pruned = 0
    for size, path in files:
        if bytes_pruned >= budget_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        bytes_pruned += size
        files_pruned += 1
    return files_pruned, bytes_pruned


def prune_and_save(brain_path: str | Path) -> int:
    """
    Prune TTL-expired observations from the OSINT cache and write it back.

    Returns the count of pruned entries. Owned by Heart's prune_stale phase
    (which runs once per cycle) — ingest_osint calls load() without pruning.

    In DRY_RUN mode the cache is loaded and counted but NOT written back,
    so the phase can report the pruned count without mutating state.

    Uses a fresh import of osint_cache so callers that clear sys.modules for
    osint_cache don't accidentally pass a stale reference via this module.
    """
    import osint_cache as _oc
    cache = _oc.load(brain_path)
    # Defensive: a corrupt or partial cache can leave pruned=None/missing.
    pruned_raw = cache.get("pruned", 0)
    try:
        pruned = int(pruned_raw) if pruned_raw is not None else 0
    except (TypeError, ValueError):
        pruned = 0
    if not _is_dry_run(brain_path):
        _oc.save(brain_path, cache)
    return pruned
