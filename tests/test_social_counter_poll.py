"""
test_social_counter_poll.py — Heart/tools/social_counter_poll.py tests

Covers:
  - Key loading: loads valid YAML; handles missing file
  - YouTube: success; missing key → noop; HTTP error → retry
  - X (Twitter): success; missing key → noop
  - Instagram: success; missing key → noop
  - Twitch: token acquisition failure → noop; follower count parsed
  - Output: each platform writes its own JSON with updated_at timestamp
  - Phase: every log line includes a phase string (AGENTS.md Rule 5)
  - Privacy: no raw tokens written to output files
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def poll_mod(tmp_path, monkeypatch):
    """Fresh import with temp shared root and secret path."""
    for k in list(sys.modules.keys()):
        if "social_counter_poll" in k:
            del sys.modules[k]

    secret_path = tmp_path / "links-secret" / "social-counters.yaml"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("NEOHIRO_SHARED_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv("NEOHIRO_LINKS_SECRET", str(secret_path))
    secret_path.write_text(
        """youtube_api_key: "yt_test_key"
youtube_channel_id: "UC_test"
x_bearer_token: "x_test_bearer"
x_user_id: "12345"
instagram_access_token: "ig_test_token"
instagram_user_id: "ig_123"
twitch_client_id: "tw_test_client"
twitch_client_secret: "tw_test_secret"
twitch_broadcaster_id: "tw_broadcaster"
""",
        encoding="utf-8",
    )
    import social_counter_poll
    return social_counter_poll


# ── Key loading ───────────────────────────────────────────────────────────
class TestKeyLoading:
    def test_loads_keys_from_yaml(self, poll_mod):
        keys = poll_mod._load_keys()
        assert keys["youtube_api_key"] == "yt_test_key"
        assert keys["youtube_channel_id"] == "UC_test"
        assert keys["x_bearer_token"] == "x_test_bearer"

    def test_missing_file_returns_empty(self, poll_mod, tmp_path, monkeypatch):
        monkeypatch.setenv("NEOHIRO_LINKS_SECRET", str(tmp_path / "no-such.yaml"))
        for k in list(sys.modules.keys()):
            if "social_counter_poll" in k:
                del sys.modules[k]
        mod = __import__("social_counter_poll")
        assert mod._load_keys() == {}


# ── YouTube ───────────────────────────────────────────────────────────────
class TestYouTube:
    def test_success_parses_stats(self, poll_mod):
        session = MagicMock()
        resp = MagicMock()
        resp.ok = True
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "items": [{
                "statistics": {
                    "subscriberCount": "123456",
                    "viewCount": "9876543",
                    "videoCount": "42",
                }
            }]
        }
        session.get.return_value = resp
        result = poll_mod.poll_youtube(session, poll_mod._load_keys())
        assert result["subscribers"] == 123456
        assert result["views"] == 9876543
        assert result["videos"] == 42

    def test_null_counts_become_zero(self, poll_mod):
        """YouTube sometimes returns null for unindexed channels — must not TypeError."""
        session = MagicMock()
        resp = MagicMock()
        resp.ok = True
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "items": [{
                "statistics": {
                    "subscriberCount": None,
                    "viewCount": None,
                    "videoCount": None,
                }
            }]
        }
        session.get.return_value = resp
        keys = {"youtube_api_key": "k", "youtube_channel_id": "c"}
        result = poll_mod.poll_youtube(session, keys)
        assert result["subscribers"] == 0
        assert result["views"] == 0
        assert result["videos"] == 0

    def test_missing_key_skips(self, poll_mod):
        result = poll_mod.poll_youtube(MagicMock(), {"youtube_api_key": ""})
        assert result == {}

    def test_http_error_retries(self, poll_mod):
        session = MagicMock()
        import requests
        ok = MagicMock()
        ok.ok = True
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"items": [{"statistics": {"subscriberCount": "1"}}]}
        session.get.side_effect = [requests.RequestException("boom"), ok]
        keys = {"youtube_api_key": "k", "youtube_channel_id": "c"}
        result = poll_mod.poll_youtube(session, keys)
        assert result["subscribers"] == 1
        assert session.get.call_count == 2


# ── _to_int helper (defensive int coercion) ─────────────────────────────
class TestToInt:
    def test_int_passthrough(self, poll_mod):
        assert poll_mod._to_int(42) == 42

    def test_string_numeric(self, poll_mod):
        assert poll_mod._to_int("123") == 123

    def test_none_returns_zero(self, poll_mod):
        assert poll_mod._to_int(None) == 0

    def test_unparseable_string_returns_zero(self, poll_mod):
        assert poll_mod._to_int("n/a") == 0

    def test_unparseable_type_returns_zero(self, poll_mod):
        assert poll_mod._to_int({}) == 0


# ── X (Twitter) ───────────────────────────────────────────────────────────
class TestX:
    def test_success_parses_metrics(self, poll_mod):
        session = MagicMock()
        resp = MagicMock()
        resp.ok = True
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "data": {
                "public_metrics": {
                    "followers_count": 5432,
                    "following_count": 210,
                    "tweet_count": 1337,
                }
            }
        }
        session.get.return_value = resp
        result = poll_mod.poll_x(session, poll_mod._load_keys())
        assert result["followers"] == 5432
        assert result["following"] == 210
        assert result["tweets"] == 1337

    def test_missing_key_skips(self, poll_mod):
        result = poll_mod.poll_x(MagicMock(), {"x_bearer_token": ""})
        assert result == {}


# ── Instagram ─────────────────────────────────────────────────────────────
class TestInstagram:
    def test_success_parses_counts(self, poll_mod):
        session = MagicMock()
        resp = MagicMock()
        resp.ok = True
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"followers_count": 999, "media_count": 50}
        session.get.return_value = resp
        result = poll_mod.poll_instagram(session, poll_mod._load_keys())
        assert result["followers"] == 999
        assert result["posts"] == 50

    def test_missing_key_skips(self, poll_mod):
        result = poll_mod.poll_instagram(MagicMock(), {"instagram_access_token": ""})
        assert result == {}


# ── Twitch ────────────────────────────────────────────────────────────────
class TestTwitch:
    def test_token_acquisition_then_followers(self, poll_mod):
        session = MagicMock()
        token_resp = MagicMock()
        token_resp.ok = True
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"access_token": "tw_app_token"}

        followers_resp = MagicMock()
        followers_resp.ok = True
        followers_resp.raise_for_status = MagicMock()
        followers_resp.json.return_value = {"total": 2048}

        # Token uses POST; followers uses GET. Match the call order precisely.
        session.post.return_value = token_resp
        session.get.return_value = followers_resp
        keys = {
            "twitch_client_id": "cid",
            "twitch_client_secret": "csec",
            "twitch_broadcaster_id": "bid",
        }
        result = poll_mod.poll_twitch(session, keys)
        assert result["followers"] == 2048
        # Sanity: token went through POST with form data, followers via GET.
        session.post.assert_called_once()
        session.get.assert_called_once()

    def test_missing_key_skips(self, poll_mod):
        result = poll_mod.poll_twitch(MagicMock(), {"twitch_client_id": ""})
        assert result == {}

    def test_token_failure_returns_empty(self, poll_mod):
        session = MagicMock()
        import requests
        # Token acquisition uses POST; have it raise.
        session.post.side_effect = requests.RequestException("auth down")
        keys = {
            "twitch_client_id": "cid",
            "twitch_client_secret": "csec",
            "twitch_broadcaster_id": "bid",
        }
        result = poll_mod.poll_twitch(session, keys)
        assert result == {}


# ── Output writers ───────────────────────────────────────────────────────
class TestOutput:
    def test_write_output_includes_timestamp(self, poll_mod):
        payload = {"subscribers": 100}
        poll_mod.write_output("youtube", payload)
        out = json.loads(
            (poll_mod.SOCIAL_DIR / "youtube.json").read_text(encoding="utf-8")
        )
        assert out["subscribers"] == 100
        assert "updated_at" in out

    def test_output_is_iso_timestamp(self, poll_mod):
        poll_mod.write_output("x", {"followers": 50})
        out = json.loads((poll_mod.SOCIAL_DIR / "x.json").read_text(encoding="utf-8"))
        # Should be parseable as an ISO datetime.
        from datetime import datetime
        datetime.fromisoformat(out["updated_at"].replace("Z", "+00:00"))

    def test_privacy_no_tokens_in_output(self, poll_mod):
        poll_mod.write_output("youtube", {"subscribers": 1})
        raw = (poll_mod.SOCIAL_DIR / "youtube.json").read_text(encoding="utf-8")
        assert "yt_test_key" not in raw
        assert "bearer" not in raw.lower()


# ── Phase logging ────────────────────────────────────────────────────────
class TestPhaseLogging:
    def test_warn_log_includes_phase(self, poll_mod, caplog):
        with caplog.at_level(logging.WARNING, logger="heart.social_counter_poll"):
            poll_mod.poll_youtube(MagicMock(), {"youtube_api_key": ""})
        assert any(poll_mod.PHASE_SOCIAL in r.message for r in caplog.records)


# ── run_once ────────────────────────────────────────────────────────────
class TestRunOnce:
    def test_all_platforms_succeed(self, poll_mod):
        session = MagicMock()
        # Match each platform's expected JSON.
        def side_effect(*args, **kwargs):
            m = MagicMock()
            m.ok = True
            m.raise_for_status = MagicMock()
            # args[0] is the URL when called positionally.
            url = args[0] if args else kwargs.get("url", "")
            if "youtube" in url:
                m.json.return_value = {"items": [{"statistics": {"subscriberCount": "1"}}]}
            elif "twitter" in url or "api.twitter" in url:
                m.json.return_value = {"data": {"public_metrics": {"followers_count": 1}}}
            elif "instagram" in url or "graph.instagram" in url:
                m.json.return_value = {"followers_count": 1}
            elif "id.twitch" in url:
                m.json.return_value = {"access_token": "tok"}
            else:
                # Twitch /helix/channels/followers — also covers graph-fallback.
                m.json.return_value = {"total": 1}
            return m

        session.get.side_effect = side_effect
        with patch("requests.Session", return_value=session):
            rc = poll_mod.main(["--once", "--quiet"])
        assert rc == 0
        # At least one output file should exist.
        assert any((poll_mod.SOCIAL_DIR / f"{p}.json").exists()
                   for p in ["youtube", "x", "instagram", "twitch"])

    def test_all_fail_returns_error(self, poll_mod):
        session = MagicMock()
        import requests
        session.get.side_effect = requests.RequestException("all down")
        with patch("requests.Session", return_value=session):
            rc = poll_mod.main(["--once", "--quiet"])
        assert rc == 3

    def test_partial_failure_writes_what_succeeds(self, poll_mod):
        """Some platforms up, some down: writes the ones that worked, rc=0."""
        session = MagicMock()
        def side_effect(*args, **kwargs):
            url = args[0] if args else kwargs.get("url", "")
            m = MagicMock()
            m.ok = True
            m.raise_for_status = MagicMock()
            if "youtube" in url:
                m.json.return_value = {"items": [{"statistics": {"subscriberCount": "5"}}]}
            else:
                import requests
                raise requests.RequestException("partial down")
            return m
        session.get.side_effect = side_effect
        with patch("requests.Session", return_value=session):
            rc = poll_mod.main(["--once", "--quiet"])
        assert rc == 0
        assert (poll_mod.SOCIAL_DIR / "youtube.json").exists()
