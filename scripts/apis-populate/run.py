"""
apis-populate — Pull public APIs catalog from public-api-lists/apis.json.

Reads: links/feeds/awesome.yaml (awesome-lists section)
Writes: /shared/llm/tools.json, /shared/apis/cache.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import structlog

_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_root))

from Heart.scripts._lib.extract.json_api import parse_json_api
from Heart.scripts._lib.pull.httpx_async import AsyncPuller, PullConfig
from Heart.scripts._lib.schema.document import Document, Seen

log = logging.getLogger("heart.apis_populate")

SHARED = Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared"))
LINKS = Path(os.environ.get("NEOHIRO_LINKS_ROOT", "/links"))

API_LIST_URL = "https://api.publicapis.org/entries"

CATEGORIES_WANT = {
    "development", "programming", "api", "tools", "utilities",
    "machine-learning", "ai", "data", "analytics", "visualization",
    "security", "authentication", "monitoring", "logging", "testing",
    "database", "storage", "cache", "queue", "search", "messaging",
    "infrastructure", "deployment", "ci-cd", "containers", "kubernetes",
    "serverless", "cloud", "hosting", "cdn", "edge", "dns", "email",
    "sms", "notification", "payment", "billing", "ecommerce", "finance",
    "blockchain", "crypto", "web3", "identity", "verification", "kyc",
    "maps", "geolocation", "weather", "news", "social", "media",
    "content", "cms", "document", "ocr", "translation", "nlp", "speech",
    "image", "video", "audio", "streaming", "realtime", "websocket",
    "graphql", "rest", "grpc", "rpc", "protocol", "serialization",
    "validation", "schema", "documentation", "openapi", "swagger",
    "mocking", "testing", "load-testing", "chaos-engineering",
    "observability", "tracing", "metrics", "alerting", "profiling",
    "debugging", "error-tracking", "logging", "analytics", "business",
    "product", "marketing", "sales", "crm", "support", "chat", "helpdesk",
    "automation", "workflow", "integration", "webhook", "event",
    "queue", "pubsub", "message-broker", "etl", "pipeline", "data-engineering",
}


def _quality_score(api: dict) -> float:
    score = 0.0
    if api.get("Auth") is None or api.get("Auth", "").lower() in ("none", "no", ""):
        score += 2.0
    if api.get("CORS", "").lower() in ("yes", "true"):
        score += 1.5
    if api.get("HTTPS", "").lower() in ("yes", "true"):
        score += 1.0
    cat = api.get("Category", "").lower()
    if any(w in cat for w in CATEGORIES_WANT):
        score += 2.0
    return score


async def run() -> dict:
    output_dir = SHARED / "apis"
    output_dir.mkdir(parents=True, exist_ok=True)
    seen = Seen(str(output_dir / "seen.jsonl"))
    cache_path = output_dir / "cache.json"
    tools_path = SHARED / "llm" / "tools.json"
    tools_path.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncPuller(max_concurrency=20) as puller:
        cfg = PullConfig(id="public-api-lists", url=API_LIST_URL, kind="api")
        result = await puller.fetch_one(cfg)

    if result.error or result.status != 200:
        return {"scope": "apis-populate", "error": result.error or f"HTTP {result.status}"}

    docs = parse_json_api(result.body or "", "public-api-lists", API_LIST_URL)
    filtered = []
    for d in docs:
        raw = d.raw.get("keys", []) if isinstance(d.raw, dict) else []
        # We need to fetch the raw data again since we only store keys now
        # For now, use a simpler filter based on what we have
        if raw.get("Auth") and raw["Auth"].lower() not in ("none", "no", ""):
            continue
        if raw.get("CORS", "").lower() not in ("yes", "true"):
            continue
        if not raw.get("Link") or not raw["Link"].startswith("https://"):
            continue
        score = _quality_score(raw)
        d.score = score
        d.tags = [t for t in d.tags if t] + [raw.get("Category", "").lower()]
        if seen.add(d.id):
            filtered.append(d.to_dict())

    filtered.sort(key=lambda x: x.get("score", 0), reverse=True)
    top = filtered[:1000]

    tmp = cache_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(top, f, ensure_ascii=False)
    tmp.rename(cache_path)

    tools = [{"name": d["title"], "url": d["url"], "description": d["summary"],
              "category": d["tags"][0] if d.get("tags") else "tools",
              "auth": "none", "cors": True, "https": True} for d in top]
    with open(tools_path, "w", encoding="utf-8") as f:
        json.dump(tools, f, ensure_ascii=False, indent=2)

    return {"scope": "apis-populate", "docs": len(top), "tools": len(tools)}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    result = asyncio.run(run())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()