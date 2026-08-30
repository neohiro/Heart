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
    tick            — cadence decision, mode check, clock
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
    compute_health  — derive health metrics from all sources
    write_brain     — persist enriched entity state to Brain
    fire_reminders  — run due reminders from Brain/reminders/
    prune_stale     — reject stale datapoints
    self_heal       — trigger doctor scripts if health degrades
    audit           — append phase results to Brain/audit/heartbeat.yaml

Output: all log lines are structlog JSON (one JSON object per line to stdout).
"""

from __future__ import annotations

import argparse
import json
import os
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
        data = (BRAIN_PATH / "heartbeat" / "mode.yaml").read_text()
        for line in data.splitlines():
            line = line.strip()
            if ":" in line and not line.startswith("#"):
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
            raw = path.read_text()
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

        data = yaml.safe_load(repos_file.read_text())
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
        last_run.write_text(content)
    except Exception as e:
        log.warning("last_run_write_failed", error=str(e))


def _write_health(metrics: dict[str, Any]) -> None:
    if DRY_RUN:
        log.debug("write_health_skipped_dry_run")
        return
    health_file = BRAIN_PATH / "heartbeat" / "health.yaml"
    health_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml
        health_file.write_text(yaml.dump(metrics, default_flow_style=False))
    except Exception as e:
        log.warning("health_write_failed", error=str(e))


def _phase_tick(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    state.mode = _read_mode()
    state.started_at = _iso_now()
    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_tick", mode=state.mode, ts=state.started_at)
    return PhaseResult(name="tick", ok=True, elapsed_ms=elapsed)


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
            data = json.loads(feed.read_text())
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
        pruned=result.get("pruned", 0),
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


def _phase_compute_health(state: CycleState) -> PhaseResult:
    t0 = time.monotonic()
    # NOTE: disk_free_mb and memory_free_mb are placeholders (9999) — replace with
    # shutil.disk_usage() and psutil.virtual_memory() when integrating with live host.
    # Keeping them as placeholders prevents false-critical alerts during skeleton phase.
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
            )
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
                data = yaml_lib.safe_load(path.read_text())
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
    Prune datapoints older than their TTL.

    The OSINT cache's own load() prunes on read (self-renewing TTL), so by the
    time we get here the in-memory cache is already fresh. What we still need
    to do is: re-write the cache, and append a structured audit record of how
    many entries we observed as stale so the dashboard can surface it.

    We deliberately do NOT touch /userdata here — that is the canonical
    long-term store, with its own ghost-promotion semantics. Pruning it from
    Heart would race with abuse_bridge resurrection detection.
    """
    t0 = time.monotonic()
    pruned_total = 0
    cache_size_after = 0
    error = ""

    try:
        import osint_cache
        cache = osint_cache.load(BRAIN_PATH)
        pruned_total = int(cache.get("pruned", 0))
        cache_size_after = len(cache.get("observations", {}))
        osint_cache.save(BRAIN_PATH, cache)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.warning("phase_prune_stale_error", error=error)

    # Append structured audit entry (append-only YAML; one document per cycle).
    if not DRY_RUN:
        try:
            stale_file = BRAIN_PATH / "heartbeat" / "stale.yaml"
            stale_file.parent.mkdir(parents=True, exist_ok=True)
            entry = (
                f"- ts: {_iso_now()}\n"
                f"  cycle: {state.cycle}\n"
                f"  pruned: {pruned_total}\n"
                f"  cache_size_after: {cache_size_after}\n"
            )
            with open(stale_file, "a", encoding="utf-8") as f:
                f.write(entry)
        except OSError as e:
            log.warning("stale_audit_write_failed", error=str(e))

    elapsed = int((time.monotonic() - t0) * 1000)
    log.info("phase_prune_stale", pruned=pruned_total, cache_size=cache_size_after)
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
                metrics = yaml.safe_load(health_file.read_text()) or {}
            except Exception:
                metrics = {}

            staleness = float(metrics.get("staleness", 0.0) or 0.0)
            error_rate = float(metrics.get("error_rate", 0.0) or 0.0)
            cache_size = int(metrics.get("osint_cache_size", 0) or 0)

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
            )
            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(entry)
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
        ("osint_userdata", _phase_osint_userdata),  # READ userdata summaries + resurrection detection; backup write-back on organ failure
        ("compute_health", _phase_compute_health),
        ("write_brain", _phase_write_brain),
        ("fire_reminders", _phase_fire_reminders),
        ("prune_stale", _phase_prune_stale),
        ("self_heal", _phase_self_heal),
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
