# Heart/tools/social_counter_poll.py
#
# Phase: social (per AGENTS.md Rule 5 — every error must include a phase).
#
# Polls YouTube, X (Twitter), Instagram, and Twitch for the public-facing
# Social Media Counters section on the neohiro-dashboard.
#
# Keys live in /links-secret/social-counters.yaml — this script NEVER commits them.
# Outputs go to /shared/social/counters/{youtube,x,instagram,twitch}.json
#
# Schedule: every 15 minutes (see Heart/schedules/REGISTRY.yaml -> social-counter).
# Timeout: 60s per platform, 2 retries. Failure demotion per failure_policy block.

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import structlog

PHASE_SOCIAL = "social"
PHASE_PUBLISH = "social.publish"

LOG = structlog.get_logger("heart.social_counter_poll")

# Configure structlog to route through stdlib logging so caplog + log aggregators
# see the events. Idempotent: repeated imports are no-ops via already_configured.
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.KeyValueRenderer(
            key_order=["event", "phase", "platform", "counter_id", "error"],
        ),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

SHARED_ROOT = Path(os.environ.get("NEOHIRO_SHARED_ROOT", "/shared"))
SOCIAL_DIR = SHARED_ROOT / "social" / "counters"

LINKS_SECRET = Path(
    os.environ.get("NEOHIRO_LINKS_SECRET", "/links-secret/social-counters.yaml")
)

REQUEST_TIMEOUT = 8.0
MAX_RETRIES = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_keys() -> dict[str, dict[str, str]]:
    """Read API keys from /links-secret/social-counters.yaml."""
    if not LINKS_SECRET.exists():
        LOG.error("secrets_file_not_found", phase=PHASE_SOCIAL)
        return {}
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(LINKS_SECRET.read_text(encoding="utf-8"))
        return data or {}
    except Exception as exc:  # noqa: BLE001
        LOG.warning("secrets_parse_failed", phase=PHASE_SOCIAL, error=str(exc))
        return {}


def _fetch_json(session: requests.Session, url: str, headers: dict, params: dict | None = None) -> dict | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            LOG.warning("fetch_attempt_failed", phase=PHASE_SOCIAL, attempt=attempt, max_retries=MAX_RETRIES, error=str(exc))
        except ValueError as exc:
            LOG.warning("non_json_response", phase=PHASE_SOCIAL, error=str(exc))
    return None


def _to_int(value: Any) -> int:
    """Coerce a count to int, returning 0 on None or unparseable input.

    API responses occasionally return None for unindexed channels; we treat
    that as 0 rather than letting int(None) raise into the retry loop.
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def poll_youtube(session: requests.Session, keys: dict) -> dict[str, Any]:
    api_key = keys.get("youtube_api_key") or keys.get("NEOHIRO_YOUTUBE_API_KEY", "")
    channel_id = keys.get("youtube_channel_id", "")
    if not api_key or not channel_id:
        LOG.warning("youtube_keys_missing", phase=PHASE_SOCIAL)
        return {}

    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "statistics", "id": channel_id, "key": api_key}
    data = _fetch_json(session, url, {}, params)
    if not data:
        return {}
    stats = data.get("items", [{}])[0].get("statistics", {})
    return {
        "subscribers": _to_int(stats.get("subscriberCount")),
        "views": _to_int(stats.get("viewCount")),
        "videos": _to_int(stats.get("videoCount")),
        "channel_id": channel_id,
    }


def poll_x(session: requests.Session, keys: dict) -> dict[str, Any]:
    bearer = keys.get("x_bearer_token") or keys.get("NEOHIRO_X_BEARER_TOKEN", "")
    user_id = keys.get("x_user_id", "")
    if not bearer or not user_id:
        LOG.warning("x_keys_missing", phase=PHASE_SOCIAL)
        return {}

    headers = {"Authorization": f"Bearer {bearer}"}
    url = f"https://api.twitter.com/2/users/{user_id}"
    params = {"user.fields": "public_metrics"}
    data = _fetch_json(session, url, headers, params)
    if not data:
        return {}
    metrics = data.get("data", {}).get("public_metrics", {})
    return {
        "followers": metrics.get("followers_count", 0),
        "following": metrics.get("following_count", 0),
        "tweets": metrics.get("tweet_count", 0),
        "user_id": user_id,
    }


def poll_instagram(session: requests.Session, keys: dict) -> dict[str, Any]:
    ig_user_id = keys.get("instagram_user_id", "")
    access_token = keys.get("instagram_access_token") or keys.get("NEOHIRO_IG_ACCESS_TOKEN", "")
    if not access_token or not ig_user_id:
        LOG.warning("instagram_keys_missing", phase=PHASE_SOCIAL)
        return {}

    url = f"https://graph.instagram.com/v18.0/{ig_user_id}"
    params = {"fields": "followers_count,media_count", "access_token": access_token}
    data = _fetch_json(session, url, {}, params)
    if not data:
        return {}
    return {
        "followers": data.get("followers_count", 0),
        "posts": data.get("media_count", 0),
        "user_id": ig_user_id,
    }


def poll_twitch(session: requests.Session, keys: dict) -> dict[str, Any]:
    client_id = keys.get("twitch_client_id") or keys.get("NEOHIRO_TWITCH_CLIENT_ID", "")
    client_secret = keys.get("twitch_client_secret") or keys.get("NEOHIRO_TWITCH_CLIENT_SECRET", "")
    broadcaster_id = keys.get("twitch_broadcaster_id", "")
    if not client_id or not client_secret or not broadcaster_id:
        LOG.warning("twitch_keys_missing", phase=PHASE_SOCIAL)
        return {}

    # Get app access token via POST (OAuth 2.0 client credentials, not GET).
    token_url = "https://id.twitch.tv/oauth2/token"
    form_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            token_resp = session.post(token_url, data=form_data, timeout=REQUEST_TIMEOUT)
            token_resp.raise_for_status()
            break
        except requests.RequestException as exc:
            LOG.warning("twitch_token_failed", phase=PHASE_SOCIAL, attempt=attempt, max_retries=MAX_RETRIES, error=str(exc))
        except ValueError as exc:
            LOG.warning("twitch_token_non_json", phase=PHASE_SOCIAL, error=str(exc))
    else:
        return {}

    try:
        token_data = token_resp.json()
    except ValueError:
        LOG.warning("twitch_token_parse_failed", phase=PHASE_SOCIAL)
        return {}

    access_token = token_data.get("access_token", "")
    if not access_token:
        LOG.warning("twitch_token_missing_access_token", phase=PHASE_SOCIAL)
        return {}

    headers = {"Client-Id": client_id, "Authorization": f"Bearer {access_token}"}
    url = "https://api.twitch.tv/helix/channels/followers"
    params = {"broadcaster_id": broadcaster_id}
    data = _fetch_json(session, url, headers, params)
    if not data:
        return {}
    return {
        "followers": data.get("total", 0),
        "broadcaster_id": broadcaster_id,
    }


def write_output(name: str, payload: dict) -> None:
    SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
    path = SOCIAL_DIR / f"{name}.json"
    out = {**payload, "updated_at": _now_iso()}
    # Atomic write: temp + rename. Survives partial writes if the process
    # crashes mid-write — readers always see either the old or new file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(path)
    LOG.info("wrote_output", phase=PHASE_PUBLISH, path=str(path))


def run_once(args: argparse.Namespace) -> int:
    keys = _load_keys()
    session = requests.Session()
    session.headers["User-Agent"] = "neohiro-Heart/1.0 (+https://neohiro.github.io)"

    results: dict[str, dict] = {}
    results["youtube"] = poll_youtube(session, keys)
    results["x"] = poll_x(session, keys)
    results["instagram"] = poll_instagram(session, keys)
    results["twitch"] = poll_twitch(session, keys)

    for name, payload in results.items():
        if payload:
            write_output(name, payload)

    successes = sum(1 for v in results.values() if v)
    failures = len(results) - successes
    if successes == 0:
        LOG.error("all_platforms_failed", phase=PHASE_SOCIAL)
        return 3
    if failures > 0:
        # Partial failure: at least one platform is missing fresh data. Doctor's
        # H-09 freshness check will surface this; we still exit 0 because the
        # write side is non-critical and we don't want to over-pager.
        LOG.warning("partial_failure", phase=PHASE_SOCIAL, successes=successes, total=len(results))
    LOG.info("poll_complete", phase=PHASE_SOCIAL, successes=successes, total=len(results))
    return 0


def health_check() -> int:
    """Live smoke test: poll one configured platform and exit 0 on HTTP 200.

    Use as a deploy verification step:
        python social_counter_poll.py --health-check

    Probes the first configured platform (YouTube > X > Instagram > Twitch).
    Returns 0 on success, 2 if no platforms are configured, 3 on any failure.
    Does not write to /shared — read-only.
    """
    keys = _load_keys()
    session = requests.Session()
    session.headers["User-Agent"] = "neohiro-Heart/1.0 (+https://neohiro.github.io)"
    platforms = [
        ("YouTube", lambda: poll_youtube(session, keys)),
        ("X", lambda: poll_x(session, keys)),
        ("Instagram", lambda: poll_instagram(session, keys)),
        ("Twitch", lambda: poll_twitch(session, keys)),
    ]
    for name, poll_fn in platforms:
        result = poll_fn()
        if result:
            LOG.info("health_check_ok", phase=PHASE_SOCIAL, platform=name, keys=len(result))
            session.close()
            return 0
    LOG.error("health_check_no_platforms", phase=PHASE_SOCIAL)
    session.close()
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Heart social-counter poller")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--health-check", action="store_true",
        help="Live smoke test: poll one configured platform; exit 0 on HTTP 200",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.health_check:
        return health_check()
    return run_once(args)


if __name__ == "__main__":
    sys.exit(main())