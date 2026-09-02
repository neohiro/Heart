#!/usr/bin/env python3
"""
grounding.py — Heart grounding_audit phase (per GROUNDING.md § 2).

Pick N random (entity, datapoint) pairs from /shared/brain/knowledge/sources.yaml,
re-fetch each from source, compare against the cached value in
/shared/brain/knowledge/self/_latest.yaml or per-scope state files,
write one JSONL line to /shared/brain/audit/grounding.jsonl,
update /shared/public/health/grounding.json.

Emits a poke if aggregate grounding_rate < 0.90 for two consecutive cycles.

Usage:
    python Heart/tools/grounding.py
    python Heart/tools/grounding.py --sample-size 30 --once
    python Heart/tools/grounding.py --dry-run

Environment:
    NEOHIRO_SHARED_ROOT   /shared
    GH_TOKEN              GitHub PAT (required for github fetches)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import ulid

DEFAULT_SAMPLE_SIZE = 20
MIN_SAMPLE = 20


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _shared_root() -> Path:
    return Path(os.environ.get('NEOHIRO_SHARED_ROOT', '/shared'))


def _sources_path() -> Path:
    return _shared_root() / 'brain' / 'knowledge' / 'sources.yaml'


def _self_latest_path() -> Path:
    return _shared_root() / 'brain' / 'knowledge' / 'self' / '_latest.yaml'


def _audit_path() -> Path:
    p = _shared_root() / 'brain' / 'audit' / 'grounding.jsonl'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _health_path() -> Path:
    p = _shared_root() / 'public' / 'health' / 'grounding.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _state_dir() -> Path:
    p = _shared_root() / 'brain' / 'watch' / 'state'
    p.mkdir(parents=True, exist_ok=True)
    return p


# ─── YAML I/O ────────────────────────────────────────────────────────────────

_YAML_PARSE_ERROR: list = []


def _read_yaml(path: Path) -> tuple[dict, bool]:
    """Read and parse a YAML file. Returns (data, ok).
    ok=False means the file could not be read or parsed.
    An empty or missing file returns ({}, True) — callers treat empty as no data."""
    if not path.is_file():
        return {}, True
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding='utf-8')) or {}, True
    except yaml.YAMLError as e:
        print(f'grounding: yaml parse error in {path}: {e}', file=sys.stderr)
        return {}, False


def _load_sources() -> tuple[list[dict], bool]:
    """Load sources from sources.yaml. Returns (sources, ok).
    ok=False if the file could not be read or parsed — callers must treat
    this as an error condition, not as "no sources registered"."""
    data, ok = _read_yaml(_sources_path())
    if not ok:
        return [], False
    sources = data.get('sources', []) or []
    return sources, True


def _load_self_latest() -> dict:
    data, _ = _read_yaml(_self_latest_path())
    return data


def _sanitize_scope_for_path(scope: str) -> str:
    return scope.replace(':', '_').replace('/', '_').replace('\\', '_')


def _load_per_scope_state(scope: str, var: str) -> dict | None:
    safe_scope = _sanitize_scope_for_path(scope)
    p = _state_dir() / safe_scope / f'{var}.json'
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


# ─── Fetcher ────────────────────────────────────────────────────────────────

MAX_RESPONSE_BYTES = 1_000_000


def _fetch_github(source: dict, var: str) -> tuple[str | None, int]:
    """Fetch a GitHub variable. Returns (value, latency_ms)."""
    repo = source.get('repo')
    if not repo or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        return None, 0
    if not re.fullmatch(r'[a-zA-Z0-9_/.-]+', var):
        return None, 0
    token = os.environ.get('GH_TOKEN', '')
    url = f'https://api.github.com/repos/{repo}/{var}'
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'neohiro-grounding/1.0',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, headers=headers)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = b''
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    print(f'  [warn] github fetch {repo}/{var} response too large ({len(body)} bytes)', file=sys.stderr)
                    return None, int((time.monotonic() - start) * 1000)
        latency = int((time.monotonic() - start) * 1000)
        data = json.loads(body)
        if var == 'releases/latest':
            return data.get('tag_name'), latency
        val = data.get(var)
        return (str(val) if val is not None else None), latency
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, OSError) as e:
        latency = int((time.monotonic() - start) * 1000)
        print(f'  [warn] github fetch {repo}/{var} failed: {e}', file=sys.stderr)
        return None, latency


def _fetch_rss(source: dict) -> tuple[str | None, int]:
    """Fetch RSS feed title (deterministic digest)."""
    urls = source.get('urls', [])
    if not urls:
        return None, 0
    url = urls[0]
    req = urllib.request.Request(url, headers={'User-Agent': 'neohiro-grounding/1.0'})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = b''
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    print(f'  [warn] rss fetch {url} response too large ({len(body)} bytes)', file=sys.stderr)
                    return None, int((time.monotonic() - start) * 1000)
        latency = int((time.monotonic() - start) * 1000)
        digest = hashlib.sha256(body).hexdigest()[:16]
        return digest, latency
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        latency = int((time.monotonic() - start) * 1000)
        print(f'  [warn] rss fetch {url} failed: {e}', file=sys.stderr)
        return None, latency


def _fetch_value(source: dict, var: str) -> tuple[str | None, int]:
    """Dispatch to the right fetcher based on source type."""
    stype = source.get('type', 'github')
    if stype == 'rss':
        return _fetch_rss(source)
    if stype in ('api', 'github'):
        return _fetch_github(source, var)
    return None, 0


# ─── Audit / health writes ────────────────────────────────────────────────

def _append_audit(entry: dict) -> None:
    """Append one line to grounding.jsonl (atomic per line). Best-effort: a
    write failure does NOT propagate, so the sample fetch loop in main() never
    aborts due to audit-disk issues."""
    p = _audit_path()
    try:
        with p.open('a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError as e:
        print(f'grounding: warning: cannot append audit entry: {e}', file=sys.stderr)


def _write_health(scope_count: int, samples: list[dict]) -> None:
    """Write /shared/public/health/grounding.json."""
    matched = sum(1 for s in samples if s.get('matched'))
    total = len(samples)
    rate = matched / total if total > 0 else 1.0
    band = 'green' if rate >= 0.95 else ('yellow' if rate >= 0.90 else 'red')
    mismatched = [s for s in samples if not s.get('matched')]
    payload = {
        'version': 1,
        't': int(time.time()),
        'scope_count': scope_count,
        'scopes_sampled': total,
        'matched': matched,
        'mismatched': total - matched,
        'grounding_rate': round(rate, 3),
        'band': band,
        'mismatched_scopes': [
            {
                'scope': s['scope'],
                'variable': s['variable'],
                'cached_value': s.get('cached_value'),
                'fetched_value': s.get('fetched_value'),
                'since': s['ts'],
            }
            for s in mismatched[:20]
        ],
    }
    p = _health_path()
    stage = p.with_suffix('.json.stage')
    try:
        stage.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        stage.replace(p)
    except OSError as e:
        print(f'grounding: warning: cannot write health file: {e}', file=sys.stderr)


# ─── Poke emitter ──────────────────────────────────────────────────────────

def _last_rate_path() -> Path:
    p = _shared_root() / 'brain' / 'audit' / 'grounding.last_rate.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_last_cycle_rate() -> tuple[float | None, str | None]:
    """Read the previous cycle's grounding_rate. Returns (rate, ts) or (None, None).

    Implemented via a sidecar file (O(1) read) written at the end of every cycle.
    The sidecar is the canonical "previous rate" source; the audit JSONL remains
    the longitudinal log but is not used for the poke policy because bounded
    tail-reads cannot guarantee we see the second-most-recent aggregate once
    the audit grows past the tail size."""
    p = _last_rate_path()
    if not p.is_file():
        return None, None
    try:
        with p.open('r', encoding='utf-8') as f:
            data = json.load(f)
        rate = data.get('grounding_rate')
        ts = data.get('ts')
        if rate is None:
            return None, None
        return float(rate), str(ts) if ts is not None else None
    except (json.JSONDecodeError, OSError, ValueError):
        return None, None


def _emit_poke(reason: str, priority: str = 'high', fingerprint: str = '') -> None:
    """Write a poke to /shared/heart/audit/instant/ for doctor/heartctl to pick up."""
    poke_dir = _shared_root() / 'heart' / 'audit' / 'instant'
    poke_dir.mkdir(parents=True, exist_ok=True)
    ts = _iso_now()
    payload = {
        'ts': ts,
        'phase': 'grounding_audit',
        'severity': priority,
        'reason': reason,
        'fingerprint': fingerprint,
    }
    # Use ULID for the filename to guarantee uniqueness even when multiple
    # pokes are emitted within the same second.
    slug = ulid.new().str
    p = poke_dir / f'grounding-{slug}.yaml'
    try:
        from atomic import write_yaml
        write_yaml(p, payload)
    except Exception as e:
        print(f'warning: failed to emit poke: {e}', file=sys.stderr)


# ─── Sample selection ─────────────────────────────────────────────────────

def _build_samples(sources: list[dict], n: int, rng: random.Random) -> list[tuple[dict, str]]:
    """Return N (source, variable) pairs.

    Variables per source type:
      - github/api: 'releases/latest', 'stargazers_count', 'open_issues_count'
      - rss: 'top_hash' (digest of the first item)

    Each source yields up to 3 vars; cycle through when sources < n.
    """
    pairs: list[tuple[dict, str]] = []
    github_vars = ['releases/latest', 'stargazers_count', 'open_issues_count']
    for i, s in enumerate(sources):
        stype = s.get('type', 'github')
        if stype == 'rss':
            pairs.append((s, 'top_hash'))
        elif stype in ('api', 'github'):
            pairs.append((s, github_vars[i % len(github_vars)]))
    if not pairs:
        return []
    rng.shuffle(pairs)
    # If we have fewer pairs than n, cycle through sources to fill
    if len(pairs) < n:
        base = list(pairs)
        while len(pairs) < n:
            pairs.append(base[len(pairs) % len(base)])
    return pairs[:n]


# ─── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        prog='grounding',
        description='Heart grounding_audit phase — verify cached values against sources',
    )
    parser.add_argument('--sample-size', type=int, default=DEFAULT_SAMPLE_SIZE,
                        help=f'number of (source, var) pairs to sample (default: {DEFAULT_SAMPLE_SIZE}, min: {MIN_SAMPLE})')
    parser.add_argument('--once', action='store_true', help='run once and exit (default)')
    parser.add_argument('--dry-run', action='store_true', help='print plan but do not fetch or write')
    parser.add_argument('--seed', type=int, default=None, help='seed for deterministic sampling')
    parser.add_argument('--report-json', action='store_true',
                        help='emit a single-line JSON report on stdout at the end '
                             '(parseable by Prometheus exporters / CI step summaries)')
    args = parser.parse_args()

    n = max(MIN_SAMPLE, args.sample_size)
    rng = random.Random(args.seed)

    sources, sources_ok = _load_sources()
    if not sources_ok:
        print('grounding: cannot load sources.yaml — parse error (see warnings above)')
        return 1
    if not sources:
        print('grounding: no sources registered at /shared/brain/knowledge/sources.yaml')
        return 1

    scope_count = len({s.get('id') for s in sources if s.get('id')})
    pairs = _build_samples(sources, n, rng)
    if not pairs:
        print('grounding: no sampleable pairs')
        return 1

    if args.dry_run:
        print(f'grounding: dry run, {len(pairs)} samples planned across {scope_count} sources')
        for s, v in pairs:
            print(f'  - {s.get("id")} / {v}')
        if args.report_json:
            plan_report = {
                'ts': _iso_now(),
                'mode': 'dry_run',
                'sample_size': len(pairs),
                'scope_count': scope_count,
                'grounding_rate': None,
                'band': None,
                'matched': None,
                'total': None,
                'previous_rate': _read_last_cycle_rate()[0],
                'samples': [
                    {'scope': s.get('id'), 'variable': v, 'matched': None, 'cached_value': None, 'fetched_value': None, 'latency_ms': 0}
                    for s, v in pairs
                ],
            }
            print(json.dumps(plan_report))
        return 0

    print(f'grounding: sampling {len(pairs)} pairs across {scope_count} sources')
    samples: list[dict] = []

    for source, var in pairs:
        scope_id = source.get('id', 'unknown')
        # Get cached value from per-scope state
        cached_state = _load_per_scope_state(scope_id, var) or _load_per_scope_state(scope_id, '')
        cached_value = cached_state.get('value') if cached_state else None

        # Fetch from source
        fetched, latency = _fetch_value(source, var)

        sample_matched = (
            cached_value is not None
            and fetched is not None
            and str(cached_value) == str(fetched)
        )

        entry = {
            'ts': _iso_now(),
            'scope': scope_id,
            'variable': var,
            'cached_value': cached_value,
            'fetched_value': fetched,
            'matched': sample_matched,
            'source_url': source.get('urls', [source.get('repo', '')])[0] if source.get('urls') or source.get('repo') else '',
            'latency_ms': latency,
            'fingerprint': f'grounding|{scope_id}|{var}',
        }
        _append_audit(entry)
        samples.append(entry)

        print(f'  [{"OK" if sample_matched else "NO "}] {scope_id}/{var}: cached={cached_value!r} fetched={fetched!r} ({latency}ms)')

    # Read the previous cycle's rate BEFORE we write this cycle's aggregate,
    # so the poke policy compares the current rate to the *previous* one,
    # not to itself. (Per GROUNDING.md § 2.1.5, we need two consecutive cycles
    # both below 0.90 before emitting a poke.)
    last_rate, _last_ts = _read_last_cycle_rate()

    # Write the public health file
    _write_health(scope_count, samples)

    # Aggregate
    matched = sum(1 for s in samples if s.get('matched'))
    total = len(samples)
    rate = matched / total if total else 1.0
    band = 'green' if rate >= 0.95 else ('yellow' if rate >= 0.90 else 'red')

    # Append the aggregate to audit JSONL for downstream readers
    aggregate_entry = {
        'ts': _iso_now(),
        'grounding_rate': round(rate, 3),
        'band': band,
        'matched': matched,
        'total': total,
        'scope_count': scope_count,
    }
    _append_audit(aggregate_entry)
    # Write the sidecar file for O(1) poke policy reads next cycle.
    # This is the canonical "previous rate" source; the audit JSONL remains
    # the longitudinal log but is not used for the poke policy (see comment
    # on _read_last_cycle_rate).
    # Use atomic.write_text so a crash mid-write cannot leave a partial
    # JSON file (which would silently disable the poke policy forever).
    try:
        from atomic import write_text as _atomic_write_text  # noqa: E402 (same dir)
        _atomic_write_text(_last_rate_path(), json.dumps(aggregate_entry, indent=2))
    except OSError:
        print('grounding: warning: cannot write grounding.last_rate.json', file=sys.stderr)

    print(f'\ngrounding_rate: {rate:.3f} ({matched}/{total}) band={band}')

    # Poke policy (per GROUNDING.md § 2.1.5): fire only if BOTH the current
    # cycle AND the previous cycle have rate < 0.90.
    if rate < 0.90 and last_rate is not None and last_rate < 0.90:
        _emit_poke(
            reason=f'grounding_rate {rate:.3f} < 0.90 (also {last_rate:.3f} last cycle)',
            priority='high',
            fingerprint='grounding-degradation',
        )
        print('grounding: emitted high-priority poke (rate < 0.90 for two consecutive cycles)')
    elif rate < 0.90:
        print(f'grounding: rate {rate:.3f} < 0.90 but last cycle was {last_rate}; waiting for one more cycle before poke')

    if rate < 0.95:
        # Increment intuition weight would go here in production
        print('grounding: intuition weight increment for under-95% scopes')

    if args.report_json:
        report = {
            'ts': aggregate_entry['ts'],
            'mode': 'live',
            'grounding_rate': rate,
            'band': band,
            'matched': matched,
            'total': total,
            'scope_count': scope_count,
            'previous_rate': last_rate,
            'mismatched_scopes': [
                {'scope': s['scope'], 'variable': s['variable'],
                 'cached_value': s.get('cached_value'), 'fetched_value': s.get('fetched_value'),
                 'latency_ms': s.get('latency_ms', 0)}
                for s in samples if not s.get('matched')
            ],
        }
        print(json.dumps(report))

    return 0


if __name__ == '__main__':
    sys.exit(main())
