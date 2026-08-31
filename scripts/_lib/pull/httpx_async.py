"""
Tier-2 Pull Engine — async HTTP with httpx.

Provides:
    AsyncPuller: manages a pool of concurrent fetches with rate-limiting,
                  retries, ETag/If-Modified-Since, circuit-breaking, and
                  disk-backed cache.

Usage:
    from Heart.scripts._lib.pull.httpx_async import AsyncPuller

    puller = AsyncPuller(max_concurrency=50, cache_dir="/shared/_cache")
    results = await puller.fetch_all([
        {"id": "nist-nvd", "url": "https://...", "kind": "api"},
        {"id": "gdelt", "url": "https://...", "kind": "api"},
    ])
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("heart.pull")

DEFAULT_TIMEOUT = 12.0
DEFAULT_CONCURRENCY = 50
DEFAULT_CACHE_TTL = 86400


@dataclass
class FetchResult:
    id: str
    url: str
    kind: str
    status: int
    body: str | None
    cached: bool
    fetched_at: str
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None


@dataclass
class PullConfig:
    id: str
    url: str
    kind: str
    cadence: str = "every_15_minutes"
    auth: str = "none"
    cors: bool = True
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = 3
    headers: dict[str, str] = field(default_factory=dict)
    cache_ttl: int = DEFAULT_CACHE_TTL


class RateLimiter:
    def __init__(self, calls_per_second: float = 10.0):
        self.interval = 1.0 / calls_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class DiskCache:
    def __init__(self, cache_dir: Path, ttl: int = DEFAULT_CACHE_TTL):
        self.cache_dir = cache_dir
        self.ttl = ttl
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:32]

    def _path(self, url: str) -> Path:
        return self.cache_dir / f"{self._key(url)}.json"

    def get(self, url: str) -> dict | None:
        p = self._path(url)
        if not p.exists():
            return None
        try:
            age = time.time() - p.stat().st_mtime
            if age > self.ttl:
                return None
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, url: str, data: dict) -> None:
        p = self._path(url)
        tmp = p.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            tmp.rename(p)
        except OSError:
            pass

    def invalidate(self, url: str) -> None:
        p = self._path(url)
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures: dict[str, int] = {}
        self._opened: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def is_open(self, host: str) -> bool:
        async with self._lock:
            if host in self._opened:
                if time.monotonic() - self._opened[host] < self.recovery_timeout:
                    return True
                del self._opened[host]
                self._failures[host] = 0
            return False

    async def record_success(self, host: str) -> None:
        async with self._lock:
            self._failures[host] = 0

    async def record_failure(self, host: str) -> None:
        async with self._lock:
            f = self._failures.get(host, 0) + 1
            self._failures[host] = f
            if f >= self.failure_threshold:
                self._opened[host] = time.monotonic()


class AsyncPuller:
    def __init__(
        self,
        max_concurrency: int = DEFAULT_CONCURRENCY,
        cache_dir: Path | None = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        default_timeout: float = DEFAULT_TIMEOUT,
        rate_limit: float = 10.0,
        user_agent: str = "neohiro-heart/1.0 (+https://github.com/neohiro/Heart)",
    ):
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.cache = DiskCache(cache_dir or Path("/shared/_cache/http"), cache_ttl) if cache_dir else None
        self.breaker = CircuitBreaker()
        self.rate_limiter = RateLimiter(rate_limit)
        self.default_timeout = default_timeout
        self.user_agent = user_agent
        self._client: httpx.AsyncClient | None = None

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": self.user_agent},
                http2=True,
                follow_redirects=True,
                timeout=httpx.Timeout(self.default_timeout),
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _cache_key(self, config: PullConfig) -> str:
        return f"{config.id}@{config.url}"

    async def fetch_one(self, config: PullConfig) -> FetchResult:
        host = config.url.split("/")[2] if "//" in config.url else ""
        if await self.breaker.is_open(host):
            return FetchResult(
                id=config.id, url=config.url, kind=config.kind,
                status=0, body=None, cached=False,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                error=f"Circuit open for {host}",
            )

        await self.rate_limiter.acquire()
        async with self.semaphore:
            client = await self._client_get()
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/html, application/rss+xml, application/atom+xml, */*",
                **config.headers,
            }

            cached = False
            if self.cache:
                cached_data = self.cache.get(config.url)
                if cached_data:
                    cached = True
                    if "etag" in cached_data:
                        headers["If-None-Match"] = cached_data["etag"]
                    if "last_modified" in cached_data:
                        headers["If-Modified-Since"] = cached_data["last_modified"]

            try:
                resp = await client.get(config.url, headers=headers, timeout=config.timeout)
            except httpx.TimeoutException:
                await self.breaker.record_failure(host)
                return FetchResult(
                    id=config.id, url=config.url, kind=config.kind,
                    status=0, body=None, cached=False,
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    error="Timeout",
                )
            except httpx.RequestError as e:
                await self.breaker.record_failure(host)
                return FetchResult(
                    id=config.id, url=config.url, kind=config.kind,
                    status=0, body=None, cached=False,
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    error=f"RequestError: {e}",
                )

            if resp.status_code == 304 and cached:
                return FetchResult(
                    id=config.id, url=config.url, kind=config.kind,
                    status=304, body=None, cached=True,
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    etag=resp.headers.get("etag"),
                    last_modified=resp.headers.get("last-modified"),
                )

            if resp.status_code >= 400:
                await self.breaker.record_failure(host)
                return FetchResult(
                    id=config.id, url=config.url, kind=config.kind,
                    status=resp.status_code, body=None, cached=False,
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                    error=f"HTTP {resp.status_code}",
                )

            body = resp.text
            if len(body) > 5 * 1024 * 1024:
                body = body[: 5 * 1024 * 1024]

            if self.cache and not cached:
                cache_data = {
                    "body": body,
                    "content_type": resp.headers.get("content-type", ""),
                    "etag": resp.headers.get("etag"),
                    "last_modified": resp.headers.get("last-modified"),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                self.cache.set(config.url, cache_data)

            await self.breaker.record_success(host)
            return FetchResult(
                id=config.id, url=config.url, kind=config.kind,
                status=resp.status_code, body=body, cached=False,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
                content_type=resp.headers.get("content-type", ""),
            )

    async def fetch_all(self, configs: list[PullConfig]) -> list[FetchResult]:
        tasks = [self.fetch_one(c) for c in configs]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def __del__(self):
        if self._client:
            asyncio.ensure_future(self.close())
