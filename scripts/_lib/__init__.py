"""
Heart scripts _lib — shared Tier-2/3 engine.

Modules:
    pull:        HTTP client (httpx_async, urllib_fallback)
    extract:     Parsers (rss, json_api, geojson, csv_parser, sitemap, html_selectolax)
    schema:      Document model + dedup
    runner:      Unified dispatcher runner
"""
