"""
atomic.py — compatibility shim.

Canonical source: Brain/src/atomic.py
This module re-exports from the canonical source so that existing call sites
`from atomic import write_json, ...` continue to work without modification.

Only `os` is re-imported at module level — this is required for
`patch("atomic.os.fdopen", ...)` in test_atomic.py to resolve correctly.
The canonical module provides the actual implementation.
"""

from __future__ import annotations

import os
import sys

from Brain.src.atomic import (  # noqa: F401, E402
    write_json,
    write_json_text,
    write_text,
    write_yaml,
    write_yaml_multi_doc,
    write_bytes,
    FileLock,
    acquire_file_lock,
    validate_audit_entry,
    load_and_validate_audit,
)
