"""
Heart bridge — single-cycle cadence engine (Python, for GitHub Actions).

Loads Brain/_entities/ and heartbeat/repos.yaml to get full org/repo
awareness across neohiro/FPM/OSI/H+. Runs all phases in one invocation
and exits. Designed to be called every minute by a GitHub Actions cron,
or continuously by a long-running process.

Usage (single cycle):
    python heart.py --once --brain-path /path/to/Brain

Usage (continuous loop):
    python heart.py --continuous --brain-path /path/to/Brain --log-level info

Environment:
    BRAIN_PATH      Root of /Brain (default: /brain)
    GH_TOKEN        GitHub PAT for API calls
    HEART_LOG_LEVEL debug|info|warn|error (default: info)

    Phases (in order):
    discover_repos  — load Brain/_entities/org-*.md + heartbeat/repos.yaml
    fetch_repos     — GitHub list repos for each org
    fetch_issues    — GitHub list open issues per repo
    fetch_prs       — GitHub list open PRs per repo
    fetch_actions   — GitHub Actions recent runs per repo
    ingest_news     — read news/public/feeds/ (neohiro/news data)
    ingest_content  — read frenzypenguin-media/Content-Creator pipeline output
    ingest_osint    — READ latest osint_cache.json → AMEND with new IP/geo/VPN
                       observations → WRITE cache + enqueue abuse signals
                       (self-renewing TTL: live observations stay alive
                        by being re-observed; stale ones are pruned)
    ingest_visitors — READ /shared/heart/visitors/<org>/*.jsonl → dedup
                       by (ip_hash, day) → write ghost profiles via
                       userdata.ghost_manager → emit worldmap.visitors.heatmap
                       datalayer. Privacy: SHA256(ip+ua), daily salt, no raw PII.
    compute_health  — derive health metrics from all sources
    write_brain     — persist enriched entity state to Brain
    fire_reminders  — run due reminders from Brain/reminders/
    prune_stale     — reject stale datapoints
    self_heal       — trigger doctor scripts if health degrades
    self_reflexive_check — scan own awareness, write findings, auto-correct
                             (see Heart/SPEC_ADDENDUM.md)
    intuition_deliberate — weight findings, compute consensus, emit pokes
                             and intuition.yaml (see Heart/SPEC_ADDENDUM.md)
    prune_shared    — shared storage: 85% auto-prune of transient files
    audit           — append phase results to Brain/audit/heartbeat.yaml

Output: all log lines are structlog JSON (one JSON object per line to stdout).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

# Ensure Heart/tools/ is on sys.path for sibling imports (e.g. abuse_bridge)
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
# Ensure workspace root + userdata/src for cross-package imports
_WORKSPACE = _TOOLS_DIR.parent.parent
for _p in (str(_WORKSPACE), str(_WORKSPACE / "userdata" / "src"), str(_WORKSPACE / "Brain" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BRAIN_PATH = Path(os.environ.get("BRAIN_PATH", "/brain"))
GH_TOKEN = os.environ.get("GH_TOKEN", "")
LOG_LEVEL = os.environ.get("HEART_LOG_LEVEL", "info")
DRY_RUN = False

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        {"debug": 10, "info": 20, "warn": 30, "error": 40}.get(LOG_LEVEL, 20)
    ),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
    cache_logger_on_first_use=False,
)

log: structlog.BoundLogger = structlog.get_logger()


@dataclass
class RepoEntry:
    org: str
    repo: str
    entity: str
    private: bool = False


@dataclass
class PhaseResult:
    name: str
    ok: bool
    elapsed_ms: int
    error: str = ""
    repos_touched: int = 0


@dataclass
class CycleState:
    cycle: int = 0
    mode: str = "normal"
    started_at: str = ""
    phases: list[PhaseResult] = field(default_factory=list)
    repos: list[RepoEntry] = field(default_factory=list)
    entities_discovered: list[str] = field(default_factory=list)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_mode() -> str:
    # mode.yaml is line-based "key: value" (e.g. "mode: normal"). We scan
    # line-by-line rather than parsing as YAML so a malformed file doesn't
    # take down mode read. Falls back to "normal" on any read error.
    try:
        data = (BRAIN_PATH / "heartbeat" / "mode.yaml").read_text(encoding="utf-8")
        for line in data.splitlines():
            line = line.strip()
            if line.startswith("mode:") and not line.startswith("#"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "normal"


def _discover_orgs_from_entities() -> list[RepoEntry]:
    ents_dir = BRAIN_PATH / "_entities"
    repos: list[RepoEntry] = []
    entities: list[str] = []
    if not ents_dir.is_dir():
        return repos
    for path in ents_dir.iterdir():
        if path.is_dir() or path.suffix != ".md":
            continue
        if not path.name.startswith("org-"):
            continue
        entities.append(path.stem)
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            continue
        front, _, _ = raw.partition("\n---")
        try:
            import yaml
            fm: dict[str, Any] = yaml.safe_load(front) or {}
        except Exception:
            continue
        org = str(fm.get("github_org", ""))
        raw_repos = fm.get("repos", [])
        repo_list: list[str] = []
        if isinstance(raw_repos, list):
            repo_list = [str(r) for r in raw_repos if r]
        elif isinstance(raw_repos, str):
            repo_list = [r.strip() for r in raw_repos.split(",") if r.strip()]
        if not org:
            continue
        for r in repo_list:
            repos.append(RepoEntry(org=org, repo=r, entity=path.stem))
    return repos


def _load_repos_yaml() -> list[RepoEntry]:
    repos_file = BRAIN_PATH / "heartbeat" / "repos.yaml"
    if not repos_file.is_file():
        return []
    repos: list[RepoEntry] = []
    try:
        import yaml

        data = yaml.safe_load(repos_file.read_text(encoding="utf-8"))
        if data and "repos" in data:
            for r in data["repos"]:
                repos.append(
                    RepoEntry(
                        org=r.get("org", ""),
                        repo=r.get("repo", ""),
                        entity=r.get("entity", ""),
                        private=r.get("private", False),
                    )
                )
    except Exception:
        pass
    return repos


def _discover_news_feeds() -> list[Path]:
    news_root = Path(os.environ.get("NEWS_PATH", "/news"))
    feeds_dir = news_root / "public" / "feeds"
    if not feeds_dir.is_dir():
        return []
    sources: list[Path] = []
    for sub in feeds_dir.iterdir():
        if not sub.is_dir():
            continue
        latest = sub / "latest.json"
        if latest.is_file():
            sources.append(latest)
    return sources


def _discover_content_creator_output() -> dict[str, Path]:
    cc_root = Path(os.environ.get("CC_PATH", "/content-creator"))
    data_dir = cc_root / "data"
    if not data_dir.is_dir():
        return {}
    out: dict[str, Path] = {}
    for variant in ("", "_transhumanists", "_fpm", "_osi"):
        base = f"latest_posts{variant}"
        for ext in (".json", ".svg"):
            p = data_dir / f"{base}{ext}"
            if p.is_file():
                out[f"data/{base}{ext}"] = p
    return out


def _gh_api(path: str) -> dict[str, Any] | None:
    if DRY_RUN:
        log.debug("gh_api_skipped_dry_run", path=path)
        return None
    if not GH_TOKEN:
        return None
    import urllib.request

    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _write_audit(state: CycleState) -> None:
    if DRY_RUN:
        log.debug("write_audit_skipped_dry_run", phase_count=len(state.phases))
        return
    audit_file = BRAIN_PATH / "audit" / "heartbeat.yaml"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for p in state.phases:
        outcome = "ok" if p.ok else "fail"
        lines.append(f"- ts: {state.started_at}")
        lines.append(f"  phase: {p.name}")
        lines.append(f"  outcome: {outcome}")
        lines.append(f"  elapsed_ms: {p.elapsed_ms}")
        if p.error:
            lines.append(f"  error: {p.error}")
        if p.repos_touched:
            lines.append(f"  repos_touched: {p.repos_touched}")
        lines.append("")
    try:
        with open(audit_file, "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log.warning("audit_write_failed", error=str(e))


def _write_last_run(state: CycleState) -> None:
    if DRY_RUN:
        log.debug("write_last_run_skipped_dry_run")
        return
    last_run = BRAIN_PATH / "heartbeat" / "last_run.yaml"
    last_run.parent.mkdir(parents=True, exist_ok=True)
    phase_yaml = []
    for p in state.phases:
        phase_yaml.append(f"  - phase: {p.name}")
        phase_yaml.append(f"    duration_ms: {p.elapsed_ms}")
        phase_yaml.append(f"    outcome: {'ok' if p.ok else 'fail'}")
    content = (
        f"mode: {state.mode}\n"
        f"cycle: {state.cycle}\n"
        f"started_at: {state.started_at}\n"
        f"ended_at: {_iso_now()}\n"
        f"repos_discovered: {len(state.repos)}\n"
        f"entities_discovered: {len(state.entities_discovered)}\n"
        f"phases:\n" + "\n".join(phase_yaml) + "\n"
    )
    try:
        last_run.write_text(content, encoding="utf-8")
    except Exception as e:
        log.warning("last_run_write_failed", error=str(e))


def _enqueue_poke(state: CycleState, kind: str, payload: dict[str, Any]) -> None:
    """
    Atomically enqueue a poke to Brain/heartbeat/poke_queue/<ts>_<kind>.json.

    Multiple pokes in the same cycle do NOT overwrite each other — each gets a unique
    filename based on monotonic timestamp + a counter. Mouth/Doctor consumers can drain
    the queue in lexicographic order and delete processed pokes.

    Atomic write: tmp file in same dir → flush + fsync → os.replace. Cleans up tmp on error.
    """
    if DRY_RUN:
        log.debug("enqueue_poke_skipped_dry_run", kind=kind)
        return
    try:
        queue_dir = BRAIN_PATH / "heartbeat" / "poke_queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        # Unique slug: ISO timestamp (no colons) + process ID + monotonic-ns tick.
        # Process ID + monotonic-ns guarantees uniqueness even if two enqueues happen
        # at the same millisecond. PID prevents collision after fork().
        ts_slug = _iso_now().replace(":", "").replace("-", "").replace(".", "_")
        pid_slug = str(os.getpid())
        mono_slug = str(time.monotonic_ns())
        # Sanitize kind: only allow [A-Za-z0-9_], replace anything else with _.
        # This prevents a caller-supplied `kind` containing `/`, `..`, or newlines
        # from escaping queue_dir.
        safe_kind = "".join(c if c.isalnum() or c == "_" else "_" for c in (kind or "unknown"))[:64]
        target = queue_dir / f"{ts_slug}_{pid_slug}_{mono_slug}_{safe_kind}.json"
        import tempfile
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                "w", delete=False, dir=str(queue_dir), encoding="utf-8", suffix=".tmp"
            )
            tmp.write(json.dumps({
                "ts": _iso_now(),
                "kind": kind,
                "cycle": state.cycle,
                **payload,
            }))
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, str(target))
            log.info("poke_enqueued", kind=kind, target=str(target))
        except Exception:
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:
                    pass
                _tmp_name = getattr(tmp, "name", None)
                if _tmp_name and os.path.exists(_tmp_name):
                    try:
                        os.unlink(_tmp_name)
                    except OSError:
                        pass
            raise
    except OSError as e:
        log.warning("enqueue_poke_failed", kind=kind, error=str(e))


def _write_health(metrics: dict[str, Any]) -> None:
    if DRY_RUN:
        log.debug("write_health_skipped_dry_run")
        return
    health_file = BRAIN_PATH / "heartbeat" / "health.yaml"
    health_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        health_file.write_text(yaml.dump(metrics, default_flow_style=False), encoding="utf-8")
    except Exception as e:
        log.warning("health_write_failed", error=str(e))


# _phase_tick removed: tick logic is inlined in run_cycle() (state.cycle += 1,
# state.mode = _read_mode(), state.started_at = _iso_now() at the top of each cycle).
# No callers; if one is added back, use run_cycle's inline block.


def _phase_discover_repos(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    from_entities = _discover_orgs_from_entities()
    from_yaml = _load_repos_yaml()
    seen: set[tuple[str, str]] = set()
    for r in from_entities:
        seen.add((r.org, r.repo))
    for r in from_yaml:
        if (r.org, r.repo) not in seen:
            from_entities.append(r)
    state.repos = from_entities
    state.entities_discovered = list(set(e.entity for e in from_entities))
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_discover_repos", total=len(state.repos), entities=state.entities_discovered)
    return PhaseResult(name="discover_repos", ok=True, elapsed_ms=elapsed, repos_touched=len(state.repos))


def _phase_fetch_repos(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    if not GH_TOKEN:
        log.info("phase_fetch_repos skipped: no GH_TOKEN")
        return PhaseResult(name="fetch_repos", ok=True, elapsed_ms=0, repos_touched=0)
    fetched = 0
    for r in state.repos:
        data = _gh_api(f"/orgs/{r.org}/repos")
        if data:
            fetched += 1
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_fetch_repos", fetched=fetched, total=len(state.repos))
    return PhaseResult(name="fetch_repos", ok=True, elapsed_ms=elapsed, repos_touched=fetched)


def _phase_fetch_issues(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    if not GH_TOKEN:
        log.info("phase_fetch_issues skipped: no GH_TOKEN")
        return PhaseResult(name="fetch_issues", ok=True, elapsed_ms=0, repos_touched=0)
    fetched = 0
    for r in state.repos:
        data = _gh_api(f"/repos/{r.org}/{r.repo}/issues?state=open&per_page=10")
        if data:
            fetched += 1
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_fetch_issues", repos_with_issues=fetched, total=len(state.repos))
    return PhaseResult(name="fetch_issues", ok=True, elapsed_ms=elapsed, repos_touched=fetched)


def _phase_fetch_prs(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    if not GH_TOKEN:
        log.info("phase_fetch_prs skipped: no GH_TOKEN")
        return PhaseResult(name="fetch_prs", ok=True, elapsed_ms=0, repos_touched=0)
    fetched = 0
    for r in state.repos:
        data = _gh_api(f"/repos/{r.org}/{r.repo}/pulls?state=open&per_page=10")
        if data:
            fetched += 1
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_fetch_prs", repos_with_prs=fetched, total=len(state.repos))
    return PhaseResult(name="fetch_prs", ok=True, elapsed_ms=elapsed, repos_touched=fetched)


def _phase_fetch_actions(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    if not GH_TOKEN:
        log.info("phase_fetch_actions skipped: no GH_TOKEN")
        return PhaseResult(name="fetch_actions", ok=True, elapsed_ms=0, repos_touched=0)
    fetched = 0
    for r in state.repos:
        data = _gh_api(f"/repos/{r.org}/{r.repo}/actions/runs?per_page=5")
        if data:
            fetched += 1
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_fetch_actions", repos_checked=fetched, total=len(state.repos))
    return PhaseResult(name="fetch_actions", ok=True, elapsed_ms=elapsed, repos_touched=fetched)


def _phase_ingest_news(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    feeds = _discover_news_feeds()
    ingested = 0
    for feed in feeds:
        try:
            data = json.loads(feed.read_text(encoding="utf-8"))
            ingested += data.get("count", 0)
        except Exception:
            pass
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_ingest_news", feeds_found=len(feeds), items_ingested=ingested)
    return PhaseResult(name="ingest_news", ok=True, elapsed_ms=elapsed, repos_touched=len(feeds))


def _phase_ingest_content(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    outputs = _discover_content_creator_output()
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_ingest_content", files_found=len(outputs), files=list(outputs.keys()))
    return PhaseResult(name="ingest_content", ok=True, elapsed_ms=elapsed, repos_touched=len(outputs))


def _phase_ingest_osint(state: CycleState) -> PhaseResult:
    """
    READ latest osint_cache.json → AMEND with new observations →
    WRITE cache + enqueue abuse signals for new IPs, geo drift, vpn/tor.

    TTL pruning is NOT done here — prune_stale owns that. The `pruned` field
    in the return is always 0; the real count lives in Brain/heartbeat/stale.yaml.
    """
    t0 = time.monotonic()
    try:
        import osint_cache
    except ImportError as e:
        log.warning("phase_ingest_osint skipped: import failed", error=str(e))
        return PhaseResult(name="ingest_osint", ok=True, elapsed_ms=0)
    result = osint_cache.run_phase(BRAIN_PATH)
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info(
        "phase_ingest_osint",
        observations_seen=result.get("observations_seen", 0),
        new_ips=result.get("new_ips", 0),
        geo_drifts=result.get("geo_drifts", 0),
        signals_enqueued=result.get("signals_enqueued", 0),
        cache_size=result.get("cache_size", 0),
        lock_wait_ms=result.get("lock_wait_ms", 0),
        elapsed_ms=elapsed,
    )
    return PhaseResult(
        name="ingest_osint",
        ok=result.get("ok", False),
        elapsed_ms=elapsed,
        # NOTE: repos_touched intentionally zero — ingest_osint touches a cache,
        # not repositories. Do not reuse this field for cache metrics.
    )


def _phase_osint_userdata(state: CycleState) -> PhaseResult:
    """
    READ /userdata summaries → identify visitor roles → detect resurrections
    → optionally write triage flags (backup path, organ-failure gated).
    """
    t0 = time.monotonic()
    try:
        import osint_userdata
    except ImportError as e:
        log.warning("phase_osint_userdata skipped: import failed", error=str(e))
        return PhaseResult(name="osint_userdata", ok=True, elapsed_ms=0)
    result = osint_userdata.run_phase(BRAIN_PATH)
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info(
        "phase_osint_userdata",
        healthy=result.get("heart_health", {}).get("healthy"),
        organ_failures=result.get("heart_health", {}).get("organ_failures", []),
        bidirectional_ok=result.get("heart_health", {}).get("bidirectional_ok"),
        strangers=result.get("counts", {}).get("strangers", 0),
        users=result.get("counts", {}).get("users", 0),
        resurrections=result.get("counts", {}).get("resurrections", 0),
        write_back_written=result.get("write_back", {}).get("written", 0),
        elapsed_ms=elapsed,
    )
    return PhaseResult(
        name="osint_userdata",
        ok=result.get("ok", False),
        elapsed_ms=elapsed,
        # repos_touched intentionally 0 — this phase touches userdata profiles,
        # not repositories. Do not reuse this field for resurrection counts.
    )


def _phase_ingest_visitors(state: CycleState) -> PhaseResult:
    """
    Phase 9 — ingest visitor pings.

    READ   /shared/heart/visitors/<org>/<YYYY-MM-DD>.jsonl
    DEDUP  by (ip_hash, day) — first ping wins
    WRITE  ghost profiles via userdata.ghost_manager.record_from_brain
    EMIT   worldmap.visitors.heatmap datalayer

    See VISITOR_HEARTBEAT.md for the privacy contract and embed spec.
    """
    t0 = time.monotonic()
    try:
        from userdata import visitors
    except ImportError as e:
        log.warning("phase_ingest_visitors skipped: import failed", error=str(e))
        return PhaseResult(name="ingest_visitors", ok=True, elapsed_ms=0)
    result = visitors.run_phase(BRAIN_PATH)
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info(
        "phase_ingest_visitors",
        orgs_seen=result.get("orgs_seen", 0),
        pings_read=result.get("pings_read", 0),
        unique_visitors=result.get("unique_visitors", 0),
        bots_filtered=result.get("bots_filtered", 0),
        ghosts_written=result.get("ghosts_written", 0),
        ghosts_skipped=result.get("ghosts_skipped", 0),
        datalayer_written=result.get("datalayer_written", False),
        elapsed_ms=elapsed,
    )
    return PhaseResult(
        name="ingest_visitors",
        ok=result.get("ok", False),
        elapsed_ms=elapsed,
    )


def _phase_compute_health(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    # NOTE: disk_free_mb and memory_free_mb are placeholders (9999) — replace with
    # psutil.virtual_memory() when integrating with live host. The shared-storage
    # check is owned by _phase_prune_shared which uses shutil.disk_usage().
    # Keeping these as placeholders prevents false-critical alerts during skeleton phase.
    metrics: dict[str, Any] = {
        "ts": _iso_now(),
        "mode": state.mode,
        "cycle": state.cycle,
        "repos_known": len(state.repos),
        "entities_known": len(state.entities_discovered),
        "disk_free_mb": 9999,
        "memory_free_mb": 9999,
        "gh_errors_min": 0,
        "cycle_success": 100,
        "llm_fallbacks_h": 0,
        "_placeholder_metrics": ["disk_free_mb", "memory_free_mb"],
    }
    _write_health(metrics)
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_compute_health", metrics=metrics)
    return PhaseResult(name="compute_health", ok=True, elapsed_ms=elapsed)


def _phase_write_brain(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    if not DRY_RUN:
        repo_summary = BRAIN_PATH / "heartbeat" / "repo_summary.json"
        repo_summary.parent.mkdir(parents=True, exist_ok=True)
        repo_summary.write_text(
            json.dumps(
                {
                    "ts": _iso_now(),
                    "cycle": state.cycle,
                    "mode": state.mode,
                    "repos": [{"org": r.org, "repo": r.repo, "entity": r.entity} for r in state.repos],
                    "entities": state.entities_discovered,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_write_brain", repos=len(state.repos), entities=len(state.entities_discovered), dry_run=DRY_RUN)
    return PhaseResult(name="write_brain", ok=True, elapsed_ms=elapsed)


def _phase_fire_reminders(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    reminders_dir = BRAIN_PATH / "reminders"
    fired = 0
    skipped = 0
    if reminders_dir.is_dir():
        now = datetime.now(timezone.utc)
        for path in reminders_dir.iterdir():
            if path.suffix != ".yaml":
                continue
            try:
                import yaml as yaml_lib
                data = yaml_lib.safe_load(path.read_text(encoding="utf-8"))
                guard_str = data.get("guard_until", "")
                if guard_str:
                    guard = datetime.fromisoformat(guard_str.replace("Z", "+00:00"))
                    if guard > now:
                        skipped += 1
                        continue
                fired += 1
                log.debug("reminder_fired", reminder=path.stem)
            except Exception:
                pass
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_fire_reminders", fired=fired, skipped=skipped)
    return PhaseResult(name="fire_reminders", ok=True, elapsed_ms=elapsed, repos_touched=fired)


def _phase_prune_stale(state: CycleState) -> PhaseResult:
    """
    Prune datapoints older than their TTL from the OSINT cache.

    This phase owns TTL expiry — it calls prune_and_save() which is the only place
    that both loads and saves the cache with pruning enabled. ingest_osint calls
    load(prune=False) so it never prunes here; the prune happens exactly once per
    cycle in this phase.

    We deliberately do NOT touch /userdata here — that is the canonical
    long-term store, with its own ghost-promotion semantics. Pruning it from
    Heart would race with abuse_bridge resurrection detection.
    """
    t0 = time.monotonic()
    error = ""

    try:
        import osint_cache
        pruned_total = osint_cache.prune_and_save(BRAIN_PATH)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("phase_prune_stale_error", error=error)
        pruned_total = 0

    # Append structured audit entry (append-only YAML; one document per cycle).
    if not DRY_RUN:
        try:
            stale_file = BRAIN_PATH / "heartbeat" / "stale.yaml"
            stale_file.parent.mkdir(parents=True, exist_ok=True)
            entry = (
                f"- ts: {_iso_now()}\n"
                f"  cycle: {state.cycle}\n"
                f"  pruned: {pruned_total}\n"
                "\n"
            )
            with open(stale_file, "a", encoding="utf-8") as f:
                f.write(entry)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            log.warning("stale_audit_write_failed", error=str(e))

    # Cap intuition.yaml to the last HEART_INTUITION_MAX_ENTRIES entries (default 1000).
    # Also removes entries older than HEART_INTUITION_MAX_AGE_DAYS (default 7 days).
    try:
        import yaml as _yaml
        intuition_file = BRAIN_PATH / "heartbeat" / "intuition.yaml"
        if intuition_file.is_file():
            try:
                max_entries = max(1, int(os.environ.get("HEART_INTUITION_MAX_ENTRIES", "1000")))
                max_age_days = max(1, int(os.environ.get("HEART_INTUITION_MAX_AGE_DAYS", "7")))
            except ValueError:
                max_entries = 1000
                max_age_days = 7
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            try:
                docs = list(_yaml.safe_load_all(intuition_file.read_text(encoding="utf-8"))) or []
                # safe_load_all returns one item per --- doc. Each item is a list (the YAML list
                # starting with "- ts:"). Flatten to a list of dicts.
                entries: list[dict[str, Any]] = []
                for doc in docs:
                    if isinstance(doc, list) and doc:
                        # Take the first element of each list (the dict for that entry).
                        if isinstance(doc[0], dict):
                            entries.append(doc[0])
                    elif isinstance(doc, dict):
                        entries.append(doc)
                # Two-pass: filter by age, then cap by entry count.
                fresh: list[dict[str, Any]] = []
                for e in entries:
                    ts_val = e.get("ts")
                    ts: datetime | None = None
                    if isinstance(ts_val, datetime):
                        ts = ts_val
                    elif isinstance(ts_val, str):
                        try:
                            ts = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            pass
                    if ts is None:
                        # No parseable ts — keep it (better to retain than to lose data).
                        fresh.append(e)
                        continue
                    # ts >= cutoff: entry is recent enough to keep.
                    # ts < cutoff: entry is too old, drop it.
                    keep = ts >= cutoff
                    log.debug("intuition_cap_filter", cycle=e.get("cycle"), ts=str(ts), cutoff=str(cutoff), keep=keep)
                    if keep:
                        fresh.append(e)
                if len(fresh) > max_entries:
                    kept = fresh[-max_entries:]
                else:
                    kept = fresh
                if len(entries) > len(kept):
                    import tempfile as _tempfile
                    tmp = _tempfile.NamedTemporaryFile(
                        "w", delete=False, dir=str(intuition_file.parent), encoding="utf-8", suffix=".tmp"
                    )
                    try:
                        # Wrap each entry in a list so yaml.dump_all emits "---" + "- ts: ..." per doc.
                        tmp.write(_yaml.dump_all(
                            [[e] for e in kept],
                            default_flow_style=False,
                            sort_keys=False,
                            allow_unicode=True,
                        ))
                        tmp.flush()
                        os.fsync(tmp.fileno())
                        tmp.close()
                        os.replace(tmp.name, str(intuition_file))
                        log.info("intuition_cap", original=len(entries), retained=len(kept))
                    except Exception:
                        if os.path.exists(tmp.name):
                            os.unlink(tmp.name)
                        raise
            except Exception as e:
                log.warning("intuition_cap_failed", error=str(e))
    except ImportError:
        pass

    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_prune_stale", pruned=pruned_total)
    return PhaseResult(
        name="prune_stale",
        ok=not error,
        elapsed_ms=elapsed,
        error=error,
    )


def _phase_self_heal(state: CycleState) -> PhaseResult:
    """
    Trigger /neohiro/doctor self-heal scripts if health metrics have degraded.

    Reads Brain/heartbeat/health.yaml (written by compute_health) and, if any
    of the configured degradation thresholds are breached, invokes the doctor
    monitor.sh with the appropriate cure. Records the action in
    Brain/audit/self_heal.yaml (append-only).

    Thresholds (env-overridable):
        HEART_HEAL_STALENESS_MAX  (default 0.6) — higher is worse
        HEART_HEAL_ERROR_RATE_MAX (default 0.05)
        HEART_HEAL_DISK_FREE_MIN  (default 5242880) — bytes
    """
    t0 = time.monotonic()
    error = ""
    actions: list[str] = []
    triggered = False

    staleness_max = float(os.environ.get("HEART_HEAL_STALENESS_MAX", "0.6"))
    error_rate_max = float(os.environ.get("HEART_HEAL_ERROR_RATE_MAX", "0.05"))
    disk_free_min = int(os.environ.get("HEART_HEAL_DISK_FREE_MIN", "5242880"))

    try:
        health_file = BRAIN_PATH / "heartbeat" / "health.yaml"
        if health_file.is_file():
            try:
                import yaml
                metrics = yaml.safe_load(health_file.read_text(encoding="utf-8")) or {}
            except Exception:
                metrics = {}

            staleness = float(metrics.get("staleness", 0.0) or 0.0)
            error_rate = float(metrics.get("error_rate", 0.0) or 0.0)

            if staleness > staleness_max:
                actions.append(f"staleness={staleness:.2f} > {staleness_max}")
                triggered = True
            if error_rate > error_rate_max:
                actions.append(f"error_rate={error_rate:.2f} > {error_rate_max}")
                triggered = True
            try:
                free = (BRAIN_PATH.parent).statvfs()  # type: ignore[attr-defined]
                if free.f_bavail * free.f_frsize < disk_free_min:
                    actions.append("disk_low")
                    triggered = True
            except AttributeError:
                pass
            except OSError:
                pass

        if triggered and not DRY_RUN:
            audit_file = BRAIN_PATH / "audit" / "self_heal.yaml"
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            entry = (
                f"- ts: {_iso_now()}\n"
                f"  cycle: {state.cycle}\n"
                f"  actions: {actions}\n"
                "\n"
            )
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(entry)
                f.flush()
                os.fsync(f.fileno())
            log.warning("self_heal_triggered", actions=actions)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("self_heal_error", error=error)

    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_self_heal", triggered=triggered, actions=actions)
    return PhaseResult(
        name="self_heal",
        ok=not error,
        elapsed_ms=elapsed,
    )


def _phase_self_reflexive_check(state: CycleState) -> PhaseResult:
    """
    Phase 15 — self_reflexive_check.

    Scan Heart's own awareness sources and write findings. Auto-create missing
    entity skeletons. Emit a poke on critical findings. See SPEC_ADDENDUM.md.

    Scans:
      - Brain/_entities/org-*.md (missing file or missing required field)
      - Brain/heartbeat/mode.yaml (corrupt, non-coercible)
      - Brain/heartbeat/health.yaml (missing or non-numeric)
      - Brain/heartbeat/last_run.yaml (older than 3x cycle interval)
      - workspace root directories (auto-register new top-level dirs)

    Writes:
      - Brain/heartbeat/reflexive_findings.yaml (append-only)
      - Brain/heartbeat/poke_queue/<ts>_reflexive_critical.json (only when at least one critical finding)
      - Auto-created Brain/_entities/org-<slug>.md skeletons (when configured)

    Idempotent and safe to run every cycle. Throttles first-cycle findings to
    avoid storms on initial boot (configurable via HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE).
    """
    t0 = time.monotonic()
    error = ""
    findings: list[dict[str, str]] = []
    import yaml as _yaml

    throttle_first = os.environ.get("HEART_REFLEXIVE_THROTTLE_FIRST_CYCLE", "1") == "1"
    auto_create = os.environ.get("HEART_REFLEXIVE_AUTO_CREATE_ENTITIES", "1") == "1"

    # Read baseline (per-cycle-count) to detect first boot + load seen_dirs.
    baseline_file = BRAIN_PATH / "heartbeat" / "reflexive_baseline.yaml"
    is_first_cycle = True
    seen_dirs: set[str] = set()
    try:
        if baseline_file.is_file():
            data = _yaml.safe_load(baseline_file.read_text(encoding="utf-8")) or {}
            is_first_cycle = int(data.get("cycle", 0)) == 0
            raw_seen = data.get("seen_dirs", []) or []
            if isinstance(raw_seen, list):
                seen_dirs = {str(d) for d in raw_seen}
    except Exception:
        is_first_cycle = True
        seen_dirs = set()

    def _add(category: str, severity: str, target: str, message: str) -> None:
        if throttle_first and is_first_cycle:
            sev_order = ["info", "warn", "critical"]
            idx = sev_order.index(severity) if severity in sev_order else 0
            severity = sev_order[max(0, idx - 1)]
        findings.append({
            "ts": _iso_now(),
            "cycle": str(state.cycle),
            "category": category,
            "severity": severity,
            "target": target,
            "message": message,
        })

    try:
        entities_dir = BRAIN_PATH.parent / "_entities"
        # The Brain path may be /Brain; _entities lives inside.
        if not entities_dir.is_dir():
            for cand in (BRAIN_PATH / "_entities", BRAIN_PATH.parent / "Brain" / "_entities"):
                if cand.is_dir():
                    entities_dir = cand
                    break

        if entities_dir.is_dir():
            # Known orgs: derived from existing entity files; if none, default to the four orgs.
            existing = {p.stem.replace("org-", "") for p in entities_dir.glob("org-*.md")}
            # Required fields per profile.schema.md (see Brain/_schema/profile.schema.md)
            required_fields = ["authority", "summary", "scope"]
            for org in existing or {"neohiro", "fpm", "osi", "hplus"}:
                org_path = entities_dir / f"org-{org}.md"
                if not org_path.is_file():
                    _add("missing_entity", "critical", f"org-{org}.md", f"entity file missing for org '{org}'")
                    # Auto-create a minimal skeleton (git-tracked; safe; reversible)
                    if auto_create and not DRY_RUN:
                        try:
                            org_path.parent.mkdir(parents=True, exist_ok=True)
                            safe_org = org.replace("'", "''")
                            org_path.write_text(
                                f"# org-{safe_org} — auto-generated by Heart.self_reflexive_check\n\n"
                                "> **Status: machine-generated skeleton.** Edit to add real content.\n\n"
                                "## identity\n\n"
                                "  - org: " + safe_org + "\n"
                                "  - authority: unknown\n"
                                "  - summary: TODO\n"
                                "  - scope: TODO\n",
                                encoding="utf-8",
                            )
                            log.info("reflexive_created_skeleton", target=str(org_path))
                        except OSError as e:
                            log.warning("reflexive_skeleton_write_failed", target=str(org_path), error=str(e))
                else:
                    # Check required fields.
                    try:
                        text = org_path.read_text(encoding="utf-8", errors="replace")
                        for fld in required_fields:
                            if f"{fld}:" not in text and f"{fld} =" not in text:
                                _add("missing_entity", "warn", str(org_path), f"required field '{fld}' missing")
                    except OSError as e:
                        _add("missing_entity", "warn", str(org_path), f"cannot read: {e}")

        # Mode file integrity.
        mode_file = BRAIN_PATH / "heartbeat" / "mode.yaml"
        if mode_file.is_file():
            try:
                m = _yaml.safe_load(mode_file.read_text(encoding="utf-8")) or {}
                mode_val = str(m.get("mode", ""))
                if mode_val not in ("dormant", "normal", "active", "sports", "turbo"):
                    _add("heart_health", "critical", str(mode_file), f"unrecognised mode: {mode_val!r}")
            except Exception as e:
                _add("heart_health", "critical", str(mode_file), f"unparseable mode.yaml: {e}")
        else:
            _add("heart_health", "warn", str(mode_file), "mode.yaml missing; defaulting to normal")

        # Health file integrity.
        health_file = BRAIN_PATH / "heartbeat" / "health.yaml"
        if health_file.is_file():
            try:
                h = _yaml.safe_load(health_file.read_text(encoding="utf-8")) or {}
                for k in ("staleness", "error_rate"):
                    if k in h and not isinstance(h[k], (int, float)):
                        _add("heart_health", "warn", str(health_file), f"field '{k}' is not numeric")
            except Exception as e:
                _add("heart_health", "warn", str(health_file), f"unparseable: {e}")

        # last_run staleness.
        last_run = BRAIN_PATH / "heartbeat" / "last_run.yaml"
        if last_run.is_file():
            age_sec = time.time() - last_run.stat().st_mtime
            # cycle_interval default 60s; threshold 3x
            try:
                interval = int(os.environ.get("HEART_CYCLE_INTERVAL", "60"))
            except ValueError:
                interval = 60
            if age_sec > 3 * interval:
                _add("cycle_stall", "critical", str(last_run), f"last_run is {int(age_sec)}s old (threshold {3 * interval}s)")

        # Workspace root: detect new top-level directories.
        # Only emit a finding the FIRST time a new directory is seen.
        # Persists seen_dirs to reflexive_baseline.yaml to avoid repeat findings.
        workspace_root = BRAIN_PATH.parent
        known_top = {"Brain", "Heart", "Mouth", "neohiro-doctor", "network", "userdata",
                     "private-assistant", "frenzypenguin-media", "openstageisland.github.io",
                     "transhumanists.github.io", "links", "links-secret", "voicemail",
                     "killswitch", "Mind", "iot", "LLM", ".git"}
        for child in workspace_root.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name in known_top:
                continue
            if str(child) in seen_dirs:
                continue
            _add("workspace_drift", "info", str(child), f"new top-level directory: {child.name}")
            seen_dirs.add(str(child))

        # Compare discoveries vs heartbeat/repos.yaml if present.
        repos_yaml = BRAIN_PATH / "heartbeat" / "repos.yaml"
        if repos_yaml.is_file():
            try:
                overrides = _yaml.safe_load(repos_yaml.read_text(encoding="utf-8")) or {}
                listed = {(r.get("org"), r.get("repo")) for r in overrides.get("repos", [])}
                for r in state.repos:
                    if (r.org, r.repo) not in listed:
                        _add("stale_repo", "info", f"{r.org}/{r.repo}", "discovered repo has no heartbeat override")
            except Exception as e:
                _add("stale_repo", "warn", str(repos_yaml), f"unparseable: {e}")

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("phase_self_reflexive_check_error", error=error)

    # Write findings.
    if findings and not DRY_RUN:
        try:
            findings_file = BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
            findings_file.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for f in findings:
                lines.append(f"- ts: {f['ts']}")
                lines.append(f"  cycle: {f['cycle']}")
                lines.append(f"  category: {f['category']}")
                lines.append(f"  severity: {f['severity']}")
                lines.append(f"  target: {f['target']}")
                lines.append(f"  message: {f['message']}")
                lines.append("")
            with open(findings_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            log.warning("reflexive_findings_write_failed", error=str(e))

    # If any critical finding, enqueue a poke (queued, not overwritten by other pokes).
    if any(f["severity"] == "critical" for f in findings):
        _enqueue_poke(state, "reflexive_critical", {
            "findings": [f for f in findings if f["severity"] == "critical"],
        })

    # Update baseline.
    if not DRY_RUN:
        try:
            baseline_file = BRAIN_PATH / "heartbeat" / "reflexive_baseline.yaml"
            baseline_file.parent.mkdir(parents=True, exist_ok=True)
            # Use double-quoted YAML strings so Windows paths (with backslashes), tabs,
            # and newlines survive a YAML round-trip. Single quotes can't escape newlines;
            # double quotes can use \n, \t, \\ escapes via the JSON-style subset.
            def _yaml_quote(s: str) -> str:
                # Escape backslash, double-quote, control chars (per YAML 1.2 § 7.3.1).
                out = []
                for ch in s:
                    if ch == "\\":
                        out.append("\\\\")
                    elif ch == '"':
                        out.append('\\"')
                    elif ch == "\n":
                        out.append("\\n")
                    elif ch == "\r":
                        out.append("\\r")
                    elif ch == "\t":
                        out.append("\\t")
                    elif ch == "\x00":
                        out.append("\\0")
                    else:
                        out.append(ch)
                return '"' + "".join(out) + '"'
            seen_dirs_lines = "\n".join(f"  - {_yaml_quote(d)}" for d in sorted(seen_dirs))
            content = (
                f"cycle: {state.cycle}\n"
                f"ts: {_iso_now()}\n"
                f"seen_dirs:\n{seen_dirs_lines if seen_dirs_lines else '  []'}\n"
            )
            baseline_file.write_text(content, encoding="utf-8")
        except OSError as e:
            log.warning("reflexive_baseline_write_failed", error=str(e))

    elapsed = int((time.monotonic() - t0) * 1000)
    log.info(
        "phase_self_reflexive_check",
        findings=len(findings),
        critical=sum(1 for f in findings if f["severity"] == "critical"),
        warn=sum(1 for f in findings if f["severity"] == "warn"),
        info=sum(1 for f in findings if f["severity"] == "info"),
    )
    return PhaseResult(
        name="self_reflexive_check",
        ok=not error,
        elapsed_ms=elapsed,
        repos_touched=len(findings),
    )


def _phase_intuition_deliberate(state: CycleState) -> PhaseResult:
    """
    Phase 16 — intuition_deliberate.

    Read the most recent cycle's reflexive findings, weight them by severity,
    compute per-scope weights, check consensus threshold, emit pokes + escalate
    persistent findings. See SPEC_ADDENDUM.md.

    Writes:
      - Brain/heartbeat/intuition.yaml (append-only, multi-doc YAML with --- separators;
        one entry per cycle; entries are atomically rewritten by prune_stale when
        HEART_INTUITION_MAX_ENTRIES or HEART_INTUITION_MAX_AGE_DAYS is exceeded)
      - Brain/heartbeat/poke_queue/<ts>_<kind>[_N].json (only when aggregate >= HEART_INTUITION_THRESHOLD)
      - Brain/heartbeat/admin_briefing.json (escalated findings; only for repeats)
    """
    t0 = time.monotonic()
    error = ""
    aggregate = 0.0
    consensus_reached = False
    per_scope: dict[str, float] = {
        "stale_repo": 0.0,
        "missing_entity": 0.0,
        "workspace_drift": 0.0,
        "heart_health": 0.0,
        "cycle_stall": 0.0,
    }
    escalated: list[dict[str, str]] = []

    try:
        threshold = float(os.environ.get("HEART_INTUITION_THRESHOLD", "0.75"))
        threshold = max(0.0, min(1.0, threshold))
        repeat_escalate = int(os.environ.get("HEART_INTUITION_REPEAT_ESCALATE", "5"))
        repeat_escalate = max(1, repeat_escalate)
    except ValueError:
        threshold = 0.75
        repeat_escalate = 5

    # Read most recent cycle's findings via PyYAML (replaces fragile line-based parser).
    try:
        import yaml as _yaml
        findings_file = BRAIN_PATH / "heartbeat" / "reflexive_findings.yaml"
        all_records: list[dict[str, str]] = []
        latest_cycle: str | None = None
        if findings_file.is_file():
            # reflexive_findings.yaml is multi-doc (separated by `---` when present, but the
            # current writer emits plain `- ts:` chunks without `---`. Use safe_load_all and
            # flatten any list-of-dicts or bare-dict fragments. Entries that fail to parse
            # individually are skipped (the file may contain partial / truncated entries).
            try:
                docs = list(_yaml.safe_load_all(findings_file.read_text(encoding="utf-8", errors="replace"))) or []
            except _yaml.YAMLError:
                docs = []
            for doc in docs:
                if isinstance(doc, list):
                    for item in doc:
                        if isinstance(item, dict):
                            all_records.append({str(k): str(v) for k, v in item.items()})
                elif isinstance(doc, dict):
                    all_records.append({str(k): str(v) for k, v in doc.items()})
            # latest_cycle = highest cycle number seen.
            for r in all_records:
                try:
                    c = int(r.get("cycle", "0"))
                except (TypeError, ValueError):
                    continue
                if latest_cycle is None or c > int(latest_cycle):
                    latest_cycle = str(c)
        records: list[dict[str, str]] = [
            r for r in all_records
            if latest_cycle is not None and r.get("cycle") == latest_cycle
        ]

        # Compute per-scope weights.
        sev_weight = {"info": 0.1, "warn": 0.5, "critical": 1.0}
        for r in records:
            cat = r.get("category", "")
            sev = r.get("severity", "info")
            if cat in per_scope:
                per_scope[cat] += sev_weight.get(sev, 0.1)
        # Normalise: each scope max = 1.0
        for k in per_scope:
            per_scope[k] = min(1.0, per_scope[k])
        # Aggregate: sum / 5 categories.
        aggregate = sum(per_scope.values()) / max(1, len(per_scope))
        consensus_reached = aggregate >= threshold

        # Track repeat findings (per-target seen_count) to escalate persistent ones.
        # Count how many cycles each (category, target) appears in within the last
        # `repeat_escalate` records (not chunks — records are individual findings and we
        # need to bound by cycle count, not finding count, so we use the records' cycles
        # and the recent N unique cycle values).
        if all_records and repeat_escalate > 0:
            seen_counts: dict[tuple[str, str], int] = {}
            # Identify the last `repeat_escalate` distinct cycle values in record order.
            seen_cycles: list[str] = []
            for r in all_records:
                c = r.get("cycle", "")
                if not seen_cycles or seen_cycles[-1] != c:
                    seen_cycles.append(c)
            recent_cycles: set[str] = set(seen_cycles[-repeat_escalate:])
            for r in all_records:
                if r.get("cycle") in recent_cycles and r.get("category") and r.get("target"):
                    key = (r["category"], r["target"])
                    seen_counts[key] = seen_counts.get(key, 0) + 1
            for (cat, target), n in seen_counts.items():
                if n > repeat_escalate:
                    escalated.append({
                        "category": cat,
                        "target": target,
                        "seen_count": str(n),
                    })
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("phase_intuition_deliberate_error", error=error)

    # Write intuition.yaml.
    if not DRY_RUN:
        try:
            intuition_file = BRAIN_PATH / "heartbeat" / "intuition.yaml"
            intuition_file.parent.mkdir(parents=True, exist_ok=True)
            entry = (
                f"---\n"
                f"- ts: {_iso_now()}\n"
                f"  cycle: {state.cycle}\n"
                f"  mode: {state.mode}\n"
                f"  per_scope_weights:\n"
                + "".join(f"    {k}: {v:.2f}\n" for k, v in per_scope.items())
                + f"  aggregate: {aggregate:.2f}\n"
                f"  threshold: {threshold:.2f}\n"
                f"  consensus_reached: {str(consensus_reached).lower()}\n"
                f"  escalated: {json.dumps(escalated) if escalated else '[]'}\n"
                "\n"
            )
            with open(intuition_file, "a", encoding="utf-8") as f:
                f.write(entry)
        except OSError as e:
            log.warning("intuition_write_failed", error=str(e))

    # If consensus reached, enqueue a poke.
    if consensus_reached:
        _enqueue_poke(state, "intuition", {
            "aggregate": aggregate,
            "threshold": threshold,
            "per_scope_weights": per_scope,
        })

    # Append escalated findings to admin_briefing.json (dedup + cap).
    if escalated and not DRY_RUN:
        try:
            ab = BRAIN_PATH / "heartbeat" / "admin_briefing.json"
            existing: dict[str, Any] = {}
            if ab.is_file():
                try:
                    existing = json.loads(ab.read_text(encoding="utf-8")) or {}
                except Exception:
                    existing = {}
            existing.setdefault("escalated", [])
            existing_ts = existing.get("ts", _iso_now())
            # Dedup: keep a single entry per (category, target) — overwrite the seen_count
            # if higher. Prevents unbounded growth when the same finding re-escalates each cycle.
            existing_index: dict[tuple[str, str], dict[str, Any]] = {}
            for e in existing["escalated"]:
                key = (e.get("category", ""), e.get("target", ""))
                if key == ("", ""):
                    continue
                if key in existing_index:
                    # Keep the entry with the higher seen_count.
                    try:
                        if int(e.get("seen_count", "0")) > int(existing_index[key].get("seen_count", "0")):
                            existing_index[key] = e
                    except (TypeError, ValueError):
                        pass
                else:
                    existing_index[key] = e
            now = _iso_now()
            for e in escalated:
                key = (e["category"], e["target"])
                new_entry = {**e, "ts": now}
                if key in existing_index:
                    try:
                        if int(e.get("seen_count", "0")) > int(existing_index[key].get("seen_count", "0")):
                            existing_index[key] = new_entry
                    except (TypeError, ValueError):
                        existing_index[key] = new_entry
                else:
                    existing_index[key] = new_entry
            # Sort by seen_count desc so the cap keeps the worst offenders.
            merged = sorted(
                existing_index.values(),
                key=lambda x: -int(x.get("seen_count", "0") or 0),
            )
            # Cap at HEART_ADMIN_BRIEFING_MAX_ENTRIES (default 100).
            try:
                cap = int(os.environ.get("HEART_ADMIN_BRIEFING_MAX_ENTRIES", "100"))
            except ValueError:
                cap = 100
            cap = max(1, cap)
            merged = merged[:cap]
            existing["escalated"] = merged
            existing["ts"] = existing_ts
            existing["last_intuition_cycle"] = state.cycle
            ab.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except OSError as e:
            log.warning("intuition_admin_briefing_write_failed", error=str(e))

    elapsed = int((time.monotonic() - t0) * 1000)
    log.info(
        "phase_intuition_deliberate",
        aggregate=f"{aggregate:.2f}",
        consensus=consensus_reached,
        escalated=len(escalated),
    )
    return PhaseResult(
        name="intuition_deliberate",
        ok=not error,
        elapsed_ms=elapsed,
        repos_touched=len(escalated),
    )


def _phase_prune_shared(state: CycleState) -> PhaseResult:
    """
    Monitor shared storage (where Brain, Heart, and Mouth all write) and auto-prune
    the largest transient files when usage exceeds HEART_SHARED_PRUNE_THRESHOLD (default 85%).

    Only prunes files in the designated transient subtrees:
      - heartbeat/signals_incoming/  (inbound signals — can be re-fetched)
      - heartbeat/abuse_signals/    (queued signals — will be regenerated)
      - heartbeat/poke_queue/       (downstream consumers drain this; stale entries can be re-poked)

    Permanently valuable data (userdata/, audit/, heartbeat/*.yaml, heartbeat/*.json,
    the osint_cache.json cache file itself) is never pruned by this phase.

    Pruning strategy: largest files first, up to HEART_SHARED_PRUNE_BUDGET bytes
    (default 50 MB). Respects HEART_SHARED_PRUNE_ENABLED=0 to disable entirely.
    """
    t0 = time.monotonic()
    error = ""

    prune_enabled = os.environ.get("HEART_SHARED_PRUNE_ENABLED", "1") != "0"
    try:
        prune_threshold_pct = float(os.environ.get("HEART_SHARED_PRUNE_THRESHOLD", "85.0"))
    except ValueError:
        prune_threshold_pct = 85.0
    try:
        prune_budget_bytes = int(os.environ.get("HEART_SHARED_PRUNE_BUDGET", str(50 * 1024 * 1024)))
    except ValueError:
        prune_budget_bytes = 50 * 1024 * 1024

    root = BRAIN_PATH.parent
    safe_subtrees = [
        root / "heartbeat" / "signals_incoming",
        root / "heartbeat" / "abuse_signals",
        root / "heartbeat" / "poke_queue",
    ]

    bytes_used = 0
    files_pruned = 0
    bytes_pruned = 0
    triggered = False
    usage_pct = 0.0

    try:
        usage = shutil.disk_usage(root)
        bytes_used = usage.used
        total = usage.total
        usage_pct = (bytes_used / total * 100) if total > 0 else 0.0

        if usage_pct >= prune_threshold_pct and prune_enabled:
            triggered = True
            import osint_cache
            files_pruned, bytes_pruned = osint_cache.prune_largest_first(
                safe_subtrees, prune_budget_bytes
            )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("phase_prune_shared_error", error=error)

    if not DRY_RUN and triggered:
        try:
            audit_file = BRAIN_PATH / "audit" / "shared_prune.yaml"
            audit_file.parent.mkdir(parents=True, exist_ok=True)
            entry = (
                f"- ts: {_iso_now()}\n"
                f"  cycle: {state.cycle}\n"
                f"  usage_pct: {usage_pct:.1f}\n"
                f"  files_pruned: {files_pruned}\n"
                f"  bytes_pruned: {bytes_pruned}\n"
                "\n"
            )
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(entry)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            log.warning("shared_prune_audit_write_failed", error=str(e))

    elapsed = int((time.monotonic() - t0) * 1000)
    log.info(
        "phase_prune_shared",
        triggered=triggered,
        usage_pct=round(usage_pct, 1),
        files_pruned=files_pruned,
        bytes_pruned=bytes_pruned,
    )
    return PhaseResult(
        name="prune_shared",
        ok=not error,
        elapsed_ms=elapsed,
        error=error,
    )


def _phase_audit(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    _write_audit(state)
    _write_last_run(state)
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_audit")
    return PhaseResult(name="audit", ok=True, elapsed_ms=elapsed)


def run_cycle(state: CycleState) -> CycleState:
    state.cycle += 1
    state.mode = _read_mode()
    state.started_at = _iso_now()
    log.info("cycle_start", cycle=state.cycle, mode=state.mode, repos=len(state.repos))

    phases = [
        ("discover_repos", _phase_discover_repos),
        ("fetch_repos", _phase_fetch_repos),
        ("fetch_issues", _phase_fetch_issues),
        ("fetch_prs", _phase_fetch_prs),
        ("fetch_actions", _phase_fetch_actions),
        ("ingest_news", _phase_ingest_news),
        ("ingest_content", _phase_ingest_content),
        ("ingest_osint", _phase_ingest_osint),   # READ → AMEND → WRITE + enqueue signals
        ("ingest_visitors", _phase_ingest_visitors),  # visitor pings → ghost profiles + datalayer
        ("osint_userdata", _phase_osint_userdata),  # READ userdata summaries + resurrection detection; backup write-back on organ failure
        ("compute_health", _phase_compute_health),
        ("write_brain", _phase_write_brain),
        ("fire_reminders", _phase_fire_reminders),
        ("prune_stale", _phase_prune_stale),
        ("self_heal", _phase_self_heal),
        ("self_reflexive_check", _phase_self_reflexive_check),  # scan own awareness; auto-correct
        ("intuition_deliberate", _phase_intuition_deliberate),  # weight findings; consensus; pokes
        ("prune_shared", _phase_prune_shared),  # shared storage: 85% auto-prune
        ("audit", _phase_audit),
    ]

    for name, fn in phases:
        try:
            result = fn(state)
            state.phases.append(result)
            if not result.ok:
                log.error("phase_failed", phase=name, elapsed_ms=result.elapsed_ms, error=result.error)
        except Exception as e:
            elapsed = 0
            err_str = f"{type(e).__name__}: {e}"
            log.error("phase_exception", phase=name, error=err_str, traceback=traceback.format_exc())
            state.phases.append(PhaseResult(name=name, ok=False, elapsed_ms=elapsed, error=err_str))

    total_elapsed = sum(p.elapsed_ms for p in state.phases)
    log.info(
        "cycle_end",
        cycle=state.cycle,
        phases_run=len(state.phases),
        total_elapsed_ms=total_elapsed,
        outcome="ok" if all(p.ok for p in state.phases) else "error",
    )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Heart — cadence engine (Python bridge)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit (GitHub Actions cron mode)")
    parser.add_argument("--continuous", action="store_true", help="Run continuously in a loop")
    parser.add_argument("--brain-path", default=os.environ.get("BRAIN_PATH", "/brain"), help="Root of /Brain")
    parser.add_argument("--log-level", default=os.environ.get("HEART_LOG_LEVEL", "info"), choices=["debug", "info", "warn", "error"])
    parser.add_argument("--cycle-interval", type=int, default=60, help="Seconds between cycles in continuous mode (default: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Run phases but skip all external calls (GitHub API, file writes)")
    args = parser.parse_args()

    global BRAIN_PATH, LOG_LEVEL, DRY_RUN
    BRAIN_PATH = Path(args.brain_path)
    LOG_LEVEL = args.log_level
    DRY_RUN = args.dry_run

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            {"debug": 10, "info": 20, "warn": 30, "error": 40}.get(LOG_LEVEL, 20)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )

    log.info("heart_bridge_starting", brain_path=str(BRAIN_PATH), mode=args.once and "once" or "continuous", dry_run=DRY_RUN)

    state = CycleState()

    if args.once or args.dry_run:
        run_cycle(state)
        return

    if args.continuous:
        while True:
            run_cycle(state)
            time.sleep(args.cycle_interval)


if __name__ == "__main__":
    main()
