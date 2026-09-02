---
# CRAWLER_ARCHITECTURE.md — neohiro public-database enrichment engine
# Status: DRAFT 2026-08-31
# Owner: Heart + Brain + Mouth
# Scope: every public (or soon-public) database in the 4-org fleet:
#   /apis, /LLM, /links, /links-secret, /news, /milestones, /worldmap,
#   /iot, /network, /Heartbeat SVGs, /private-assistant (public plug),
#   /userdata (public plug later)
#
# This spec defines:
#   1. A canonical 5-tier engine stack
#   2. A unified data model (Document)
#   3. A fetch lifecycle (resolve → pull → extract → enrich → cache → publish)
#   4. 13 new fetcher scopes wired into Heart/schedules/REGISTRY.yaml
#   5. Per-repo plug map (which tier feeds which DB)
#   6. Open-source tools to pull in (httpx, scrapling, selectolax, etc.)
#   7. Improvement loops (self-heal, grounding, LLM cascade)

## 0. Design principles

- **One engine, many feeds.** Every public DB is fed by a `Fetcher` registered
  in a single registry; same lifecycle, same observability, same SLO.
- **Async-first.** `httpx` + `asyncio` everywhere new. `urllib` and
  `requests` only for legacy code paths.
- **Cache is the product.** Every fetcher writes `/shared/<scope>/cache.json`
  atomically with last-good fallback. The dashboard reads cache, not origin.
- **Stealth is opt-in.** `scrapling` is only for anti-bot sites (Shodan
  pages, LinkedIn, Twitter). Plain `httpx` is the default.
- **Improvement loops are first-class.** Three loops:
    - **Grounding** (already in `Heart/tools/grounding.py`) — re-fetch and
      diff cached vs live, write `grounding.jsonl`, emit poke at < 90 %.
    - **Self-heal** (`apis/self_healer/`) — replace dead feeds automatically.
    - **LLM cascade** (already in `LLM/`) — use the smallest free LLM
      that can extract/normalize, escalate to a paid one only if it fails.
- **Two-tier everywhere.** Heart Docker is primary; GitHub Actions
  cron is the fallback (see `DATABASE_ENRICHMENT.md` § 2).

## 1. Engine stack (5 tiers)

```
┌────────────────────────────────────────────────────────────────────┐
│ TIER 5: PUBLISH                                                    │
│   /shared/<scope>/latest.json  ← every fetcher writes here          │
│   Mouth pulls, validates (3-stage), ships to channel               │
│   Dashboard / SVG generator reads /shared/.../latest.json          │
└────────────────────────────────────────────────────────────────────┘
                            ▲
┌────────────────────────────────────────────────────────────────────┐
│ TIER 4: ENRICH                                                     │
│   /Heart/scripts/enrich/*  — LLM cascade summaries, geo-coding,    │
│   dedup, cross-ref with /Brain/_entities/*.md                      │
│   Inputs: raw body from Tier 3                                     │
│   Outputs: /shared/<scope>/enriched/<doc_id>.json                 │
└────────────────────────────────────────────────────────────────────┘
                            ▲
┌────────────────────────────────────────────────────────────────────┐
│ TIER 3: EXTRACT                                                    │
│   /Heart/scripts/_lib/extract/*  — pluggable parsers:              │
│     feedparser_rss     : RSS/Atom (stdlib only, regex fallback)    │
│     json_api           : JSON:API, REST                             │
│     geojson            : GeoJSON FeatureCollection                 │
│     csv                : CSV with header detection                 │
│     sparql             : Wikidata SPARQL JSON                      │
│     sitemap            : XML sitemap parser + recursive discovery  │
│     html_selectolax    : fast CSS-selector HTML extract (1k lines) │
│     html_scrapling     : anti-bot HTML (stealth browser profile)   │
│   Output: list[Document] (canonical schema)                        │
└────────────────────────────────────────────────────────────────────┘
                            ▲
┌────────────────────────────────────────────────────────────────────┐
│ TIER 2: PULL                                                       │
│   /Heart/scripts/_lib/pull/*  — async HTTP client:                 │
│     httpx_async        : 50+ concurrent, HTTP/2, retries           │
│     scrapling_stealth  : adaptive fingerprinting (Brain-only)      │
│     feed_fetcher       : RSS/Atom specific (feedparser)            │
│   With: rate limiter, circuit breaker, ETag/If-Modified-Since,    │
│         disk cache (24 h), atomic write                            │
└────────────────────────────────────────────────────────────────────┘
                            ▲
┌────────────────────────────────────────────────────────────────────┐
│ TIER 1: RESOLVE                                                    │
│   /Heart/scripts/_lib/resolve/*  — feed registry + DNS + dedup:    │
│     links/feeds/*.yaml  : canonical registry (existing)            │
│     sitemap_walker      : given a URL → discover all child URLs    │
│     github_api_resolve  : owner/repo → repos/commits/issues API    │
│     s3_dump_resolve     : s3://bucket → list of keys (data dumps)  │
│   Output: list[Resource] {url, kind, cadence, auth, license}       │
└────────────────────────────────────────────────────────────────────┘
```

## 2. Canonical Document schema

Every fetcher emits the same shape (JSON). Downstream tiers are
type-checked against this.

```python
# /Heart/scripts/_lib/schema/document.py
{
  "id":          "sha256(url|fetched_at|cadence_id)[:16]",  # stable
  "url":         "https://...",           # canonical
  "source_id":   "nist-nvd-cve",          # matches links/feeds/*.yaml
  "kind":        "rss|api|html|geojson|csv|dump",
  "fetched_at":  "2026-08-31T14:22:01Z",
  "published":   "2026-08-31T14:00:00Z",  # origin's stamp (nullable)
  "title":       "...",
  "summary":     "...",                    # raw extract
  "body":        "...",                    # raw extract (may be HTML)
  "author":      "...",
  "tags":        ["cve", "osv"],
  "geo":         {"lat": 50.85, "lon": 4.35, "accuracy": "country"},
  "refs":        {"doi": "...", "isbn": "...", "cve": "CVE-..."},
  "license":     "CC0|CC-BY|ODbL|...|unknown",
  "trust":       0.0,                      # 0.0-1.0 from grounding_audit
  "raw":         {...},                    # full origin payload (debug)
  "_extractor":  "json_api@1",             # which Tier-3 module parsed it
  "_tier":       3,                       # provenance for self-audit
}
```

## 3. Fetch lifecycle

```
REGISTRY.yaml entry
   ↓
[1] resolve(): load links/feeds/<topic>.yaml → Resource
   ↓
[2] pull():   async fetch with rate-limit + cache
   - cache hit & not stale → skip
   - 304 Not Modified       → keep cache
   - 2xx                    → body
   - 4xx/5xx × 3            → circuit-break + dead_feeds.jsonl
   ↓
[3] extract(): Document list
   - feedparser_rss     for kind=rss
   - json_api           for kind=api
   - geojson            for kind=geojson
   - html_selectolax    for kind=html (no anti-bot)
   - html_scrapling     for kind=html (anti-bot, Brain-only)
   ↓
[4] enrich():
   - geo-code (resolve country/region)
   - dedup (against /shared/<scope>/seen.jsonl bloom)
   - LLM cascade (free model → paid fallback) for:
       summary, sentiment, category, urgency_score
   ↓
[5] atomic write: /shared/<scope>/cache.json (tmp + fsync + rename)
   ↓
[6] publish: Mouth picks up, runs 3-stage validation, posts to channel
   ↓
[7] grounding_audit: re-fetch sample, compare, write grounding.jsonl
```

## 4. New fetcher scopes (to add to Heart/schedules/REGISTRY.yaml)

13 new scopes. Each lives in `Heart/scripts/<scope>/run.py` and is
tested in `Heart/scripts/tests/test_dispatchers.py` (31 tests pass
today; add ~40 more).

| # | Scope id | Path | Cadence | Tier-2 | Tier-3 | Feeds → Repo |
|---|----------|------|---------|--------|--------|--------------|
| 1 | `apis-populate` | Heart/scripts/apis-populate/ | 6 h | httpx_async | json_api | /apis, /LLM/tools.json |
| 2 | `llm-populate` | Heart/scripts/llm-populate/ | 1 h | httpx_async | json_api | /LLM/{providers,models,free_models}.json |
| 3 | `awesome-populate` | Heart/scripts/awesome-populate/ | 24 h | httpx_async | readme_parser | /links/awesome/ |
| 4 | `links-populate` | Heart/scripts/links-populate/ | 1 h | httpx_async | html_selectolax | /links/links.json |
| 5 | `wiki-populate` | Heart/scripts/wiki-populate/ | 24 h | httpx_async | json_api (sparql) | /worldmap/knowledge.wikidata |
| 6 | `paper-populate` | Heart/scripts/paper-populate/ | 6 h | httpx_async | json_api | /milestones/papers |
| 7 | `patent-populate` | Heart/scripts/patent-populate/ | 24 h | httpx_async | json_api | /milestones/patents |
| 8 | `transport-populate` | Heart/scripts/transport-populate/ | 15 m | httpx_async | json_api | /worldmap/transport |
| 9 | `aircraft-populate` | Heart/scripts/aircraft-populate/ | 5 m | httpx_async | json_api | /worldmap/transport.aircraft |
| 10 | `iot-populate` | Heart/scripts/iot-populate/ | 5 m | httpx_async + scrapling_stealth | json_api | /iot/cache/ |
| 11 | `crypto-populate` | Heart/scripts/crypto-populate/ | 5 m | httpx_async | json_api | /monetization/public |
| 12 | `sitemap-walk` | Heart/scripts/sitemap-walk/ | 24 h | httpx_async | sitemap | /links/sitemap/ |
| 13 | `package-populate` | Heart/scripts/package-populate/ | 6 h | httpx_async | json_api | /LLM/runtime |

### 4.1 Scope details

#### 1. `apis-populate` → /apis, /LLM/tools.json
- Pulls from `public-apis/public-apis` JSON: `https://api.publicapis.org/random?count=...`
  (or the maintained fork `public-api-lists/public-api-lists` → `apis.json`).
- Filters: `auth=null && cors=true && category in {<our 20 categories>}`.
- Quality score: `auth + cors + https + category_relevance + uptime`.
- Output: `/shared/llm/tools.json` (replaces README regex).
- Cadence: 6 h.
- License: feeds inherit from `public-api-lists` (MIT for the project, data is
  a derivative of public APIs).

#### 2. `llm-populate` → /LLM/{providers,models,free_models}.json
- Sources (free public models, no key):
    - `https://openrouter.ai/api/v1/models` (free model list)
    - `https://huggingface.co/api/models?filter=text-generation&full=true`
    - `https://api.groq.com/openai/v1/models` (no key for listing)
    - `https://api.together.xyz/v1/models`
- Extract: id, context_length, pricing, modalities, deprecation.
- Cache: 1 h (model lists change slowly).
- License: provider-specific, MIT/Apache for our extracted metadata.
- Output: `/shared/llm/models.json`, `/shared/llm/free_models.json`.

#### 3. `awesome-populate` → /links/awesome/
- 35+ FOSS catalog READMEs in `links/feeds/awesome.yaml`:
    - `sindresorhus/awesome`, `jnv/lists`, `quozd/awesome-php`,
      `awesome-foss/awesome-sysadmin`, `openobserve/awesome-grafana`,
      `mcp-get/mcp-get`, etc.
- Uses `html_selectolax` for h2/h3 + link extraction.
- Output: `/shared/links/awesome/<list_id>.json` with
  `{section, items: [{name, url, desc, tags}]}`.
- Cadence: 24 h.

#### 4. `links-populate` → /links/links.json
- Validates + categorizes the 100+ URLs already in `/links/`.
- Detects: dead (404), redirected (3xx), upgraded (HTTPS), CORS, license.
- Output: `/shared/links/links.json` with health + category.
- Cadence: 1 h.
- Existing dispatcher: `links-validate` covers validation only; this
  one adds the categorization + scoring layer.

#### 5. `wiki-populate` → /worldmap/knowledge.wikidata
- Wikidata SPARQL queries: cities, monuments, observatories, IP ranges.
- Examples:
    - `SELECT ?item ?itemLabel ?coord WHERE { ?item wdt:P31/wdt:P279* wd:Q515 . ?item wdt:P625 ?coord }`
    - `SELECT ?org ?orgLabel ?coord WHERE { ?org wdt:P31 wd:Q4830453 . ?org wdt:P625 ?coord }`
- Output: `/shared/worldmap/knowledge.wikidata.json`.
- Cadence: 24 h.
- License: CC0.

#### 6. `paper-populate` → /milestones/papers
- Sources:
    - OpenAlex: `https://api.openalex.org/works?sort=publication_date:desc`
    - arXiv RSS: `https://export.arxiv.org/rss/<category>` (cs.AI, cs.LG, q-bio, etc.)
    - Crossref: `https://api.crossref.org/works?rows=50`
    - Semantic Scholar: `https://api.semanticscholar.org/graph/v1/paper/search`
- Extract: title, authors, year, doi, citations, fields_of_study.
- Output: `/shared/milestones/papers.json`.
- Cadence: 6 h.
- License: CC0 (OpenAlex), CC0 (Crossref), arXiv non-exclusive.

#### 7. `patent-populate` → /milestones/patents
- Sources:
    - USPTO PEDS: `https://api.uspto.gov/api/v1/patent/applications`
    - EPO OPS: `https://ops.epo.org/3.2/rest-services/published-data/search`
    - WIPO PatentScope RSS per country
- Extract: patent number, title, assignee, inventors, IPC class, date.
- Output: `/shared/milestones/patents.json`.
- Cadence: 24 h.
- License: Public.

#### 8. `transport-populate` → /worldmap/transport
- Sources (GTFS + GBFS):
    - `https://api.gbfs.org/gbfs.json` (system directory)
    - `https://mobilitydatabase.org/api/feeds` (catalog)
    - Transitland Atlas: `https://transit.land/api/v2/rest/feeds`
- Extract: agency, route, stop, vehicle types.
- Output: `/shared/worldmap/transport.json`.
- Cadence: 15 m (live arrivals).
- License: per feed (mostly CC-BY).

#### 9. `aircraft-populate` → /worldmap/transport.aircraft
- Already partially in `geo.yaml` (`opensky-network`).
- Add: ADSBExchange public CSV, OpenSky by bbox.
- Extend to per-continent bbox so we cover the whole world in 5-10 calls.
- Output: `/shared/worldmap/aircraft.json` (anonymized state vectors).
- Cadence: 5 m.
- License: OpenSky (CC-BY), ADSBExchange (per-IP).

#### 10. `iot-populate` → /iot/cache/
- Already has iot/cache/ structure. Add:
    - Weather: Open-Meteo per coordinates (5 m).
    - Air quality: OpenAQ per nearest station.
    - Tailscale device health: every device on the tailnet.
- Output: `/shared/iot/<device_id>/latest.json`.
- Cadence: 5 m.
- License: public domain / CC-BY.

#### 11. `crypto-populate` → /monetization/public
- Sources (all free public, no auth for public endpoints):
    - CoinGecko: `https://api.coingecko.com/api/v3/coins/markets`
    - SEC EDGAR full-text: `https://efts.sec.gov/LATEST/search-index?q=...`
    - Chainabuse: `https://www.chainabuse.com/api/reports` (already in osint)
- Output: `/shared/monetization/public/{prices,filings,scams}.json`.
- Cadence: 5 m.
- License: public.

#### 12. `sitemap-walk` → /links/sitemap/
- For a curated set of domains (e.g. wikipedia.org, github.com/explore,
  arxiv.org, openalex.org, musicbrainz.org):
    - Fetch `https://<domain>/sitemap.xml` (or `sitemap_index.xml`).
    - Recursive parse of `<sitemap>` and `<urlset>`.
    - Diff against last walk; emit new URLs to `/shared/links/discovery/`.
- Cadence: 24 h.
- License: public discovery.

#### 13. `package-populate` → /LLM/runtime
- Tracks package metadata across the 40+ ecosystems we use:
    - npm: `https://registry.npmjs.org/<pkg>` + `https://api.npmjs.org/downloads/...`
    - PyPI: `https://pypi.org/pypi/<pkg>/json`
    - crates.io: `https://crates.io/api/v1/crates/<crate>`
    - Go proxy: `https://proxy.golang.org/<module>/@latest`
- Tracks: latest version, dependents, license, vulnerabilities
  (cross-ref with `worldmap.osint.cve.oss`).
- Output: `/shared/llm/package_health.json`.
- Cadence: 6 h.
- License: per registry (npm CC-BY-SA, PyPI public, crates MIT).

## 5. Per-repo plug map (which fetcher feeds which DB)

| Repo / DB | New scope(s) | Existing dispatcher | Output path |
|-----------|--------------|---------------------|-------------|
| `neohiro/apis` | `apis-populate` | — | `/shared/llm/tools.json` |
| `neohiro/LLM` | `llm-populate`, `package-populate` | — | `/shared/llm/{models,free_models,package_health}.json` |
| `neohiro/links` | `awesome-populate`, `links-populate`, `sitemap-walk` | `links-validate` | `/shared/links/links.json` |
| `neohiro/news` | (existing) | `news-populate` | `/shared/news/*` |
| `transhumanists/milestones` | `paper-populate`, `patent-populate` | — | `/shared/milestones/{papers,patents}.json` |
| `neohiro/worldmap` | `wiki-populate`, `transport-populate`, `aircraft-populate`, `iot-populate` | `osint-populate` | `/shared/worldmap/*` |
| `neohiro/iot` | `iot-populate` | (existing sensors) | `/shared/iot/<device>/latest.json` |
| `neohiro/network` | (uses `iot-populate`, `aircraft-populate`) | `hz-scrape` | `/shared/hub/latest.json` |
| `Heartbeat SVGs` | (uses `grounding-audit`) | `grounding-audit` | `/shared/public/health/grounding.json` |
| `neohiro/private-assistant` (public plug) | `crypto-populate` | — | `/shared/monetization/public/*` |
| `neohiro/userdata` (public plug later) | (deferred — privacy first) | — | — |
| `neohiro/monetization` | `crypto-populate` | — | `/shared/monetization/public/*` |

## 6. Open-source tools to pull in

| Tier | Tool | Why | License | Status |
|------|------|-----|---------|--------|
| Pull | `httpx` (async) | 50+ concurrent, HTTP/2, retries | BSD-3 | **Adopt now** — `Heart/scripts/_lib/pull/httpx_async.py` |
| Pull | `scrapling` (Brain) | Adaptive anti-bot | Apache-2.0 | Already in `Brain/docker/scrapling/` |
| Pull | `urllib` (stdlib) | Zero-dep fallback for Heart dispatchers | PSF | In use |
| Pull | `feedparser` | RSS/Atom | BSD-2 | In use (apis) |
| Extract | `selectolax` (Python) | 10x faster than BS4, CSS selectors | MIT | **Adopt now** — `Heart/scripts/_lib/extract/html_selectolax.py` |
| Extract | `beautifulsoup4` | HTML extract, fallback | MIT | In use (apis) |
| Extract | `lxml` | Robust XML | BSD-3 | In use (apis) |
| Extract | `defusedxml` | XXE-safe XML parse | PSF | **Adopt now** — every XML parse should use this |
| Extract | `wikitextprocessor` (Wiktionary/Wikipedia) | Parse wiki dumps, expand templates | MIT | Optional (offline dump) |
| Extract | `json-stream` | Stream large JSON | MIT | Optional |
| Resolve | `tldextract` | Public suffix list | BSD-3 | **Adopt now** — domain categorization |
| Resolve | `publicsuffix2` | Public suffix list (offline) | MIT | Alternative to tldextract |
| Resolve | `dnspython` | DNS resolve for sitemap walker | ISC | Optional |
| Storage | `sqlite-utils` | SQLite for dedup + seen | Apache-2.0 | Optional |
| Storage | `orjson` | Fast JSON (5x) | Apache-2.0 | **Adopt now** for cache writes |
| Async | `tenacity` | Retry/backoff helpers | Apache-2.0 | Optional |
| Async | `aiohttp` (alt) | Async HTTP, fallback | Apache-2.0 | Optional |

### 6.1 `requirements.txt` (proposed)

```text
# Heart/scripts/requirements.txt
httpx>=0.27            # async HTTP, HTTP/2, retries
selectolax>=0.3.21     # fast CSS-selector HTML extract
orjson>=3.9            # 5x faster JSON
defusedxml>=0.7         # XXE-safe XML
tldextract>=5.1        # public suffix list
feedparser>=6.0        # RSS/Atom
beautifulsoup4>=4.12   # HTML fallback
lxml>=5.0              # XML parser
structlog>=24.1        # structured logging (already in use)
pyyaml>=6.0            # YAML parsing (already in use)
```

## 7. Improvement loops (self-improvement over time)

### 7.1 Grounding (already implemented)
- `Heart/tools/grounding.py` re-fetches sample of (entity, datapoint) pairs
  from `Brain/knowledge/sources.yaml`, compares against cached value,
  writes `/shared/public/health/grounding.json`.
- Threshold: 0.90 → poke godadmin if < for 2 cycles.
- This becomes the natural "is the public DB up to date?" metric.

### 7.2 Self-heal feed replacement
- `apis/self_healer/source_checker.py` already exists.
- Extend it: when a feed goes dead, query `links/feeds/awesome.yaml`
  for alternates in the same category, propose to godadmin via
  `github_issue` channel, then auto-add on approval.
- Schedule: every 24 h.

### 7.3 LLM cascade (already in `LLM/`)
- For every enrichment step (geo-coding, summarization, category),
  try the smallest free LLM first (`openrouter/free` → `groq/llama-3.1-8b`
  → `mistral-7b` → `claude-haiku` only if all fail).
- Already in `LLM/router.py` cascade logic. Wire it into the `enrich` tier.

### 7.4 Field-typed cadence
- Fast fields (aircraft, prices, weather): every 5 min.
- Medium (news, OSINT, BGP): 15-60 min.
- Slow (papers, patents, soil, demographics): 6-24 h.
- This avoids wasting cycles on slow-changing data.

### 7.5 LLM-driven scope discovery
- Every week, run an LLM with the current `links/feeds/*.yaml` and ask:
  "Which public APIs/feeds would close gaps in our worldmap, world model,
  or capability set?" — append proposals to `/shared/brain/feed_proposals/`
  for godadmin review.

## 8. Storage layout

```
/shared/
├── apis/                    # /apis cache
│   ├── cache.json           # {entries: [Document]}
│   ├── dead.jsonl           # {url, last_ok, last_fail, reason}
│   └── changelog.jsonl
├── llm/
│   ├── models.json
│   ├── free_models.json
│   ├── unlimited.json
│   ├── package_health.json
│   └── tools.json
├── links/
│   ├── links.json           # {entries: [{url, kind, status, license}]}
│   ├── awesome/<list_id>.json
│   └── discovery/           # sitemap-walk output
├── news/                    # existing
├── osint/                   # existing
├── worldmap/
│   ├── knowledge.wikidata.json
│   ├── transport.json
│   ├── aircraft.json
│   └── ...                  # existing layers
├── milestones/
│   ├── papers.json
│   ├── patents.json
│   └── events.json
├── iot/                     # existing
├── monetization/public/
│   ├── prices.json
│   ├── filings.json
│   └── scams.json
└── public/health/
    ├── grounding.json
    └── feed_health.json     # aggregated across all scopes
```

## 9. Observability (every fetcher emits)

```
/shared/heart/audit/<scope>/
├── last_run.json            # {started, ended, count, errors, ms_total}
├── seen.jsonl               # sha256(doc_id) for dedup
└── errors.jsonl             # {ts, kind, msg, url, trace}
```

Aggregate: `/shared/heart/audit/feed_health.json` is the union of all
scopes, queried by `neohiro-doctor` and the dashboard.

## 10. Heart cycle integration (phases)

Add these phases to `Heart/tools/heart.py`:

```
phase: pull_public_feeds
  - iterate Heart/schedules/REGISTRY.yaml scopes
  - for each scope.status == "implemented":
      run with timeout, capture last_run, append seen.jsonl
  - emit /shared/heart/audit/feed_health.json

phase: enrich_public
  - for each /shared/<scope>/cache.json with kind in {rss, api}:
      call LLM cascade for summary + category
      write /shared/<scope>/enriched/<id>.json

phase: ground_public
  - already exists as _phase_grounding_audit (2026-08-31)
  - sample N (entity, datapoint) pairs, re-fetch, compare
```

## 11. Existing code to refactor (avoid duplication)

| Existing | Refactor to |
|----------|-------------|
| `apis/scrapers/rss_fetcher.py` (330 lines, feedparser + BS4) | Tier-3 `feedparser_rss` module + Tier-3 `html_selectolax` |
| `Heart/scripts/news-populate/run.py` (custom regex RSS parser) | Tier-3 `feedparser_rss` (with `feedparser` dep) |
| `news/src/news/sources/rss.py` (`xml.etree.ElementTree`) | Tier-3 `feedparser_rss` (single impl) |
| `Heart/scripts/osint-populate/run.py` (urllib ThreadPoolExecutor) | Tier-2 `httpx_async` (50+ concurrent) |
| `Heart/scripts/tools-populate/run.py` (regex README parser) | Tier-2/3 `httpx_async` + structured JSON from `public-api-lists/apis.json` |
| `Heart/tools/visitor_counter_scraper.py` | Tier-2 `httpx_async` (vendor API only) |
| `Heart/scripts/hz-scrape/hz_scrape.py` | Tier-2 `httpx_async` (already Python) |

## 12. Roadmap

### Phase 1 (1 week)
- Land `httpx_async` + `selectolax` + `orjson` + `defusedxml` as new
  Tier-2/3 modules under `Heart/scripts/_lib/`.
- Port `news-populate` and `osint-populate` to use them.
- Land `sitemap-walk` (cheapest scope, validates the engine).
- Add 13 scope entries to REGISTRY.yaml with `status: scaffold`.
- Land `docs/feed_health.json` aggregate.
- Tests: ≥ 70 dispatcher tests pass.

### Phase 2 (2 weeks)
- Land `apis-populate`, `llm-populate`, `package-populate`, `links-populate`.
- Wire into `/apis` and `/LLM`.
- Add LLM cascade hook to enrich tier.
- Add `feed_health.json` → dashboard doctor bar.

### Phase 3 (3 weeks)
- Land `wiki-populate`, `paper-populate`, `patent-populate`.
- Wire into `/milestones` and `/worldmap/knowledge.wikidata`.
- Add LLM-driven scope discovery (weekly cron).

### Phase 4 (4 weeks)
- Land `transport-populate`, `aircraft-populate`, `iot-populate`.
- Wire into `/worldmap/transport.*`.
- Land `crypto-populate` → `/monetization/public/`.
- Self-heal: dead feed → propose alternates via github_issue channel.

## 13. Cross-repo applicability (where this all lives)

| Concern | Where |
|---------|-------|
| Tier-2/3 modules | `Heart/scripts/_lib/` (new) |
| Per-scope scripts | `Heart/scripts/<scope>/run.py` |
| Schedule | `Heart/schedules/REGISTRY.yaml` |
| Feed registry | `links/feeds/<topic>.yaml` (extend with 13 new) |
| Public DB consumers | `/apis`, `/LLM`, `/links`, `/milestones`, `/worldmap`, `/iot`, `/monetization` |
| Grounding | `Brain/knowledge/sources.yaml` (extend) |
| LLM cascade | `LLM/router.py` (existing) |
| Stealth scraper | `Brain/docker/scrapling/` (existing) |
| Healthz | `Heart/scripts/hz-scrape/` (existing) |
| Doctor | `neohiro-doctor/monitor.sh` reads `feed_health.json` |
| Dashboard | `neohiro-dashboard` shows `feed_health.json` + per-scope stats |

## 14. Risk + safety

- **Rate limits**: every fetcher respects `Retry-After`, has its own
  budget counter, demotes on consecutive failures.
- **ToS**: all sources are either public APIs, RSS feeds, or
  public-domain / CC datasets. No scraping of logged-in pages.
- **License**: every document carries `license` field; downstream
  rendering strips bodies with `license=unknown` until verified.
- **PII**: this spec covers public DBs only. `userdata` and
  `private-assistant` are deliberately excluded (covered in
  `private-assistant/SPEC_ADDENDUM.md` and `userdata/README.md`).
- **Storage cap**: `/shared/` is capped per `STORAGE_ARCHITECTURE.md`;
  every fetcher respects its quota and trims old entries.
- **Ground truth**: grounding_audit provides a public, measurable
  signal that the engine is doing its job.

## 15. References (specs to read before changing)

- `Heart/schedules/REGISTRY.md` (Heart schedule registry spec)
- `Heart/scripts/_lib/heart_dispatch.py` (shared dispatcher utilities)
- `DATABASE_ENRICHMENT.md` (two-tier Heart + GitHub Actions)
- `GROUNDING.md` (grounding probe)
- `LLM_ROUTER_CASCADE.md` (free-tier cascade)
- `LLM_MARKET_INDEX.md` (LLM market index)
- `STORAGE_ARCHITECTURE.md` (shared drive + quotas)
- `Brain/BRAIN_LIVE_OBSERVER.md` (file watcher)
- `Brain/docker/scrapling/` (anti-bot scraper, Brain-only)
- `apis/self_healer/` (dead feed replacement)
- `private-assistant/SPEC_ADDENDUM.md` (canonical PII chain)
- `network/SPEC_HUB.md` (mesh healthz hub)
- `links/feeds/awesome.yaml` (FOSS catalogs)
- `links/feeds/geo.yaml` (geoscience feeds)
- `links/feeds/osint.yaml` (OSINT feeds)
- `links/feeds/news.yaml` (news feeds)
- `links/feeds/worldmap.yaml` (worldmap datalayers)
