"""
Tier-2 Pull Engine — synchronous urllib wrapper (zero-dep fallback).

This is the legacy Tier-2 path used by existing Heart dispatchers.
New code should use httpx_async.py instead.

Provides:
    http_get(): simple HTTP GET with timeout, retries, UA.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("heart.pull.urllib_fallback")

DEFAULT_TIMEOUT = 12.0
MAX_BODY = 5 * 1024 * 1024


def http_get(url: str, *, timeout: float = DEFAULT_TIMEOUT, headers: dict | None = None) -> tuple[int, str, str]:
    h = {"User-Agent": "neohiro-heart/1.0 (+https://github.com/neohiro/Heart)"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY).decode("utf-8", errors="replace")
            return resp.status, body, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, "", e.headers.get("Content-Type", "") if e.headers else ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("urllib fetch failed: url=%s err=%s", url, e)
        return 0, "", ""
