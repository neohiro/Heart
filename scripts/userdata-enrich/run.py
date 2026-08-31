"""
userdata-enrich — Heart dispatcher: user profile enrichment.

Architecture (two-tier):
  TIER 1 (Heart process, live Docker):
      - LLM summarisation: ✓  (free-tier cascade via neohiro/LLM router)
      - All 4 orgs scanned via GitHub API (neohiro, FrenzyPenguin, OSI, H+)
  TIER 2 (GitHub Actions fallback — see .github/workflows/db-enrichment-fallback.yml):
      - LLM summarisation: ✗  (deterministic rollup only)
      - Same data sources, no LLM call

Enrichment steps per user:
  1. READ summaries from /shared/userdata/summaries/<login>/ (last 7 days)
  2. COMPUTE deterministic metrics (patterns, intent, sentiment, resolution rate)
  3. (Heart only) LLM cascade → narrative summary
  4. WRITE enriched profile to /shared/userdata/enrichments/<login>.yaml

Privacy rules (enforced on both tiers):
  - Never log raw PII; use ip-hash:<sha256> for identifiers.
  - chmod 600 on all written files.
  - Append-only summaries; no existing summary is ever modified.

Run:
    python run.py --once          # single pass
    python run.py --quiet         # silent
    python run.py --dry-run        # no I/O, exit 0
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import structlog

sys.path.insert(0, str(Path(__file__).parent.parent / "_lib"))
from heart_dispatch import (
    atomic_write_text,
    run_scope,
    shared_root,
)

SCRIPT_DIR = Path(__file__).resolve().parent
_WS = SCRIPT_DIR.parent.parent.parent
for _p in (
    str(_WS),
    str(_WS / "userdata" / "src"),
    str(_WS / "Brain" / "src"),
    str(_WS / "LLM" / "scripts"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Login names appear as on-disk directory names; restrict to a safe character set
# to prevent path traversal or weirdness from a corrupt / hostile summaries dir.
_LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# A topic is a short label, never an email/phone/IP. Cap length and strip bad chars.
_TOPIC_RE = re.compile(r"[^A-Za-z0-9 _.\-]{1,64}")


def _is_safe_login(name: str) -> bool:
    if not name or len(name) > 64:
        return False
    if name in (".", ".."):
        return False
    return bool(_LOGIN_RE.match(name))


def _sanitize_topic(t: Any) -> str | None:
    """Return a topic string safe for topic_distribution, or None to drop.

    Rejects topics that look like PII (emails, IPv4, phone-like digits) and
    truncates long ones. This is the privacy gate for the topic_distribution
    field — a malicious or buggy summary can no longer leak a user's email
    through the topics channel.
    """
    if not isinstance(t, str):
        return None
    s = t.strip()
    if not s:
        return None
    if len(s) > 64:
        return None
    if "@" in s and "." in s.split("@", 1)[-1]:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", s):
        return None
    if re.fullmatch(r"\+?[\d\s().-]{7,}", s):
        return None
    return s

GLOBAL_ENRICHMENTS_DIR = "enrichments"
GLOBAL_SUMMARIES_DIR = "summaries"
SUMMARY_WINDOW_DAYS = 7
MAX_LLM_CALLS_PER_RUN = 10
# Cap to keep a single dispatcher pass within the every_5_minutes budget.
# A hostile / corrupt summaries dir could enumerate thousands of fake logins
# and starve the cycle; this cap is a hard ceiling regardless of input.
MAX_LOGINS_PER_RUN = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── LLM integration (Heart only) ────────────────────────────────────────────

def _call_llm_summarise(
    log: structlog.stdlib.BoundLogger,
    login: str,
    summary_text: str,
    *,
    llm_available: bool,
) -> str | None:
    """
    Call the LLM cascade for a narrative summary of the user's last 7 days.

    Returns None if LLM is unavailable or the call fails.
    This function is called ONLY when running inside the Heart Docker (Tier 1).
    It is NEVER called from the GitHub Actions fallback (Tier 2).

    Args:
        log:    Bound structlog logger (injected to avoid per-call logger allocation).
        login:  User login (used only in the prompt; never logged at ERROR level).
    """
    if not llm_available:
        return None

    prompt = (
        f"Summarise the following 7-day interaction summary for user {login} "
        f"in ≤150 words. Output ONLY a YAML block with keys: "
        f"narrative (string), top_intents (list), mood_band (positive|neutral|negative). "
        f"Do NOT log raw PII. Do NOT include the user's email or IP.\n\n"
        f"---SUMMARY---\n{summary_text[:3000]}\n---END---\n\n"
        f"```yaml\n"
    )

    try:
        import router as llm_router
    except ImportError:
        log.warn("llm_unavailable_import")
        return None

    try:
        result = llm_router.chat(
            prompt,
            model_preset="openrouter/free",
            max_tokens=300,
            temperature=0.3,
        )
        if result and hasattr(result, "content"):
            content = result.content.strip()
            if content.startswith("```yaml"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
            return content.strip()
    except (AttributeError, TypeError, ValueError) as exc:
        log.warn("llm_call_failed", error=str(exc))

    return None


# ── Summary reader ─────────────────────────────────────────────────────────

def _read_summaries(userdata_root: Path, login: str) -> list[dict]:
    """
    Read all summary YAML files for <login> from the last SUMMARY_WINDOW_DAYS days.
    Returns a list of valid summary dicts, newest last.
    """
    import yaml as _yaml

    summaries_dir = userdata_root / GLOBAL_SUMMARIES_DIR / login
    if not summaries_dir.is_dir():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=SUMMARY_WINDOW_DAYS)
    summaries: list[tuple[datetime, dict]] = []

    for p in summaries_dir.glob("*.yaml"):
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            entry = _yaml.safe_load(content)
        except (_yaml.YAMLError, ValueError, OSError):
            continue
        if not isinstance(entry, dict):
            continue
        ts_str = entry.get("t", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if ts < cutoff:
            continue
        summaries.append((ts, entry))

    summaries.sort(key=lambda x: x[0])
    return [s for _, s in summaries]


# ── Deterministic enrichment ────────────────────────────────────────────────

def _compute_patterns(summaries: list[dict]) -> list[dict]:
    """Extract interaction patterns from summary list."""
    if not summaries:
        return []

    patterns: dict[str, dict] = {}
    for s in summaries:
        intent = s.get("intent", {})
        detected = intent.get("detected")
        if detected:
            iid = intent.get("intent_id", "unknown")
            if iid not in patterns:
                patterns[iid] = {"id": iid, "confidence_sum": 0.0, "count": 0}
            patterns[iid]["confidence_sum"] += float(intent.get("confidence", 0))
            patterns[iid]["count"] += 1

    result = []
    for iid, p in patterns.items():
        result.append({
            "id": iid,
            "confidence": round(p["confidence_sum"] / p["count"], 3),
            "count": p["count"],
            "source": "intent_classification",
        })
    result.sort(key=lambda x: x["count"], reverse=True)
    return result[:10]


def _compute_intent_distribution(summaries: list[dict]) -> dict[str, float]:
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for s in summaries:
        iid = s.get("intent", {}).get("intent_id", "other")
        counts[iid] += 1
        total += 1
    if total == 0:
        return {"other": 1.0}
    return {k: round(v / total, 3) for k, v in counts.items()}


def _compute_topic_distribution(summaries: list[dict]) -> dict[str, float]:
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for s in summaries:
        for raw_topic in (s.get("topics") or []):
            topic = _sanitize_topic(raw_topic)
            if topic:
                counts[topic] += 1
                total += 1
    if total == 0:
        return {"other": 1.0}
    return {k: round(v / total, 3) for k, v in counts.items()}


def _compute_sentiment_trend(summaries: list[dict]) -> list[dict]:
    windows = [
        ("last_7_days", 7),
        ("last_14_days", 14),
        ("last_30_days", 30),
    ]
    trend = []
    for label, days in windows:
        scores: list[float] = []
        for s in summaries:
            if _ts_within_days(s.get("t", ""), days):
                scores.append(float(s.get("sentiment", {}).get("score", 0)))
        if scores:
            mean = round(sum(scores) / len(scores), 3)
            trend.append({
                "period": f"last_{days}_days",
                "mean": mean,
                "trend": "stable",
            })
    return trend


def _ts_within_days(ts_str: str, days: int) -> bool:
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        # If naive, assume UTC (matches the convention of every other
        # timestamp in the system; without this the comparison below would
        # raise TypeError on a naive datetime and we'd silently drop the row).
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return ts >= cutoff
    except (ValueError, AttributeError, TypeError):
        return False


def _compute_resolution_rate(summaries: list[dict], days: int) -> float:
    total = 0
    resolved = 0
    for s in summaries:
        if _ts_within_days(s.get("t", ""), days):
            total += 1
            if s.get("outcomes", {}).get("resolved"):
                resolved += 1
    if total == 0:
        return 0.0
    return round(resolved / total, 3)


def _compute_surface_distribution(summaries: list[dict]) -> dict[str, int]:
    dist: dict[str, int] = defaultdict(int)
    for s in summaries:
        surf = s.get("user", {}).get("surface", "unknown")
        dist[surf] += 1
    return dict(dist)


# ── Enrichment writer ────────────────────────────────────────────────────────

def _write_enrichment(
    log: structlog.stdlib.BoundLogger,
    userdata_root: Path,
    login: str,
    profile: dict,
) -> bool:
    """Atomically write enrichment profile with cross-process lock.

    Returns True on success. Uses a per-login file lock to prevent
    TOCTOU races if two Heart cycles overlap (a slow LLM run can take
    longer than the every_5_minutes schedule). The lock file lives
    alongside the enrichment file and is created lazily.
    """
    if not _is_safe_login(login):
        log.error("write_refused_unsafe_login", login_kind=type(login).__name__)
        return False

    out = userdata_root / GLOBAL_ENRICHMENTS_DIR / f"{login}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    lock_path = out.with_suffix(out.suffix + ".lock")

    import heart_dispatch as hd
    try:
        with hd._file_lock(lock_path, timeout=10.0):
            try:
                atomic_write_text(out, _profile_to_yaml(profile))
            except OSError as e:
                log.error("write_failed", login=login, error=str(e))
                return False
            try:
                out.chmod(0o600)
            except (OSError, NotImplementedError):
                # Windows has no POSIX mode bits; chmod raises NotImplementedError
                pass
        return True
    except TimeoutError as e:
        log.error("write_lock_timeout", login=login, error=str(e))
        return False


def _profile_to_yaml(profile: dict) -> str:
    import yaml
    return yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, default_flow_style=False)


# ── Main handler ─────────────────────────────────────────────────────────────

def handler(log: structlog.stdlib.BoundLogger, config: dict) -> int:
    """
    Main dispatcher entry point.

    Tier detection:
      - If NEOHIRO_LLM_AVAILABLE=1 → running in Heart Docker (Tier 1) → use LLM.
      - Otherwise → running in GitHub Actions (Tier 2) → deterministic only.
    """
    flags = config.get("flags", {})
    if flags.get("dry_run"):
        log.info("enrich.dry_run", scope="userdata-enrich")
        return 0

    llm_available = os.environ.get("NEOHIRO_LLM_AVAILABLE", "0") == "1"
    log.info("enrich.start", llm_available=llm_available, scope="userdata-enrich")

    userdata_root = shared_root() / "userdata"

    # Read known logins from the summaries index
    logins = _discover_logins(userdata_root)
    if len(logins) > MAX_LOGINS_PER_RUN:
        log.warn("enrich.login_count_capped",
                 discovered=len(logins), cap=MAX_LOGINS_PER_RUN)
        logins = logins[:MAX_LOGINS_PER_RUN]
    log.info("enrich.logins_discovered", count=len(logins))

    written = 0
    skipped = 0
    llm_calls = 0
    errors = 0

    for login in logins:
        try:
            rc = _enrich_one_user(
                userdata_root=userdata_root,
                login=login,
                log=log,
                llm_available=llm_available,
                max_llm_calls=MAX_LLM_CALLS_PER_RUN - llm_calls,
            )
            if rc == "written":
                written += 1
            elif rc == "skipped":
                skipped += 1
            elif rc == "llm_used":
                written += 1
                llm_calls += 1
        except (KeyError, OSError) as exc:
            errors += 1
            log.error("enrich.user_error", login=login, error=str(exc),
                      traceback=traceback.format_exc())

    log.info("enrich.end", written=written, skipped=skipped, llm_calls=llm_calls, errors=errors)
    return 0 if errors == 0 else 1


def _discover_logins(userdata_root: Path) -> list[str]:
    summaries_dir = userdata_root / GLOBAL_SUMMARIES_DIR
    if not summaries_dir.is_dir():
        return []
    logins = [p.name for p in summaries_dir.iterdir() if p.is_dir() and _is_safe_login(p.name)]
    logins.sort()
    return logins


def _enrich_one_user(
    userdata_root: Path,
    login: str,
    log: structlog.stdlib.BoundLogger,
    llm_available: bool,
    max_llm_calls: int,
) -> str:
    """
    Enrich one user's profile. Returns 'written', 'llm_used', or 'skipped'.
    """
    summaries = _read_summaries(userdata_root, login)

    if not summaries:
        return "skipped"

    first_seen = None
    last_seen = None
    for s in summaries:
        t = s.get("t", "")
        if t:
            if first_seen is None or t < first_seen:
                first_seen = t
            if last_seen is None or t > last_seen:
                last_seen = t

    profiles = _compute_patterns(summaries)
    intent_dist = _compute_intent_distribution(summaries)
    topic_dist = _compute_topic_distribution(summaries)
    sentiment_trend = _compute_sentiment_trend(summaries)
    surface_dist = _compute_surface_distribution(summaries)
    resolution_7d = _compute_resolution_rate(summaries, 7)
    resolution_30d = _compute_resolution_rate(summaries, 30)
    # summaries is already the last 7 days (filtered in _read_summaries);
    # no need to re-filter.
    escalation_7d = sum(1 for s in summaries if s.get("escalated"))

    llm_narrative: str | None = None
    if llm_available and max_llm_calls > 0 and profiles:
        summary_snippet = _summaries_to_text(summaries[:50])
        llm_narrative = _call_llm_summarise(
            log, login, summary_snippet, llm_available=llm_available,
        )

    profile: dict[str, Any] = {
        "schema_version": 1,
        "user": {
            "login": login,
            "scope": f"user:{login}",
            "first_seen": first_seen,
            "last_seen": last_seen,
            "interaction_count": len(summaries),
            "surface_distribution": surface_dist,
        },
        "patterns": profiles,
        "intent_distribution": intent_dist,
        "topic_distribution": topic_dist,
        "sentiment_trend": sentiment_trend,
        "resolution_rate": {
            "last_7_days": resolution_7d,
            "last_30_days": resolution_30d,
        },
        "escalation_count": {
            "last_7_days": escalation_7d,
        },
        "llm_narrative": llm_narrative,
        "data_size_bytes": _summaries_byte_size(summaries),
        "ghost_probability": 0.0,
        "t": _now(),
    }

    ok = _write_enrichment(log, userdata_root, login, profile)
    return "llm_used" if (ok and llm_narrative) else ("written" if ok else "skipped")


def _summaries_byte_size(summaries: list[dict]) -> int:
    """Return the approximate JSON byte size of the summaries list.

    This measures the on-wire size of the data, not the Python str() repr
    (which includes quotes and escapes). Used for the data_size_bytes field
    on the enriched profile.
    """
    if not summaries:
        return 0
    import json
    try:
        return len(json.dumps(summaries, default=str, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _summaries_to_text(summaries: list[dict]) -> str:
    lines = []
    for s in summaries[-20:]:
        ts = s.get("t", "?")
        intent = s.get("intent", {})
        sentiment = s.get("sentiment", {})
        outcome = s.get("outcomes", {})
        topics = s.get("topics", [])
        lines.append(
            f"[{ts}] intent={intent.get('intent_id','?')} "
            f"sentiment={sentiment.get('score','?')} "
            f"resolved={outcome.get('resolved','?')} "
            f"topics={topics}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(run_scope("userdata-enrich", handler))
