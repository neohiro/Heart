"""
atomic.py — compatibility shim.

Canonical source: Brain/src/atomic.py
This module re-exports from the canonical source so that existing call sites
`from atomic import write_json, ...` continue to work.  The shim also re-imports
stdlib modules (os, tempfile) so that `patch("atomic.os.fdopen", ...)` in tests
still resolves correctly.
"""

from __future__ import annotations

import json as json_
import logging as logging_
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Re-export everything from the canonical source
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
