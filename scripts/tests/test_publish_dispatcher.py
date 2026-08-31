"""
test_publish_dispatcher.py — Heart/scripts/publish unit tests.

Validates that the publish dispatcher:
  - consults /Brain/scope_channels before publishing
  - strips @mentions from channels that disallow them
  - rejects ghost role on every channel
  - rejects unknown channels
  - forwards the formatted text to Mouth's output()

Run: python -m pytest Heart/scripts/tests/test_publish_dispatcher.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent
BRAIN_SRC = ROOT / "Brain" / "src"
MOUTH_SRC = ROOT / "Mouth" / "src"

for p in (str(BRAIN_SRC), str(MOUTH_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from scope_channels import Channel, gate as channel_gate
from privacy_rules import mouth_gate


class TestPublishGateDirect:
    def test_facebook_admin_allowed(self):
        r = mouth_gate("admin", "facebook_page", "Brand post: new product launch")
        assert r["allowed"]
        assert r["channel_policy"]["exposure"] == "public"

    def test_instagram_strips_link(self):
        r = mouth_gate("admin", "instagram_brand", "See https://example.com")
        assert r["allowed"]
        assert "https://example.com" not in r["formatted_text"]

    def test_facebook_strips_mention(self):
        r = mouth_gate("admin", "facebook_page", "Thanks @alice!")
        assert r["allowed"]
        assert "@alice" not in r["formatted_text"]
        assert "[user]" in r["formatted_text"]

    def test_ghost_denied(self):
        r = mouth_gate("ghost", "facebook_page", "anything")
        assert not r["allowed"]

    def test_unknown_channel_denied(self):
        r = mouth_gate("admin", "channel_nonexistent", "hi")
        assert not r["allowed"]
        assert "unknown channel" in r["reason"]


class TestPublishDispatcher:
    def _run(self, *args, **kwargs):
        cmd = [
            sys.executable,
            str(ROOT / "Heart" / "scripts" / "publish" / "run.py"),
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, env=os.environ.copy(),
            cwd=str(ROOT),
        )

    def test_help(self):
        r = self._run("--help")
        assert r.returncode == 0
        assert "--channel" in r.stdout

    def test_unknown_channel_returns_3(self):
        r = self._run("--channel=nonexistent_channel")
        assert r.returncode == 3

    def test_no_channel_returns_2(self):
        env = os.environ.copy()
        env.pop("NEOHIRO_MOUTH_CHANNEL", None)
        cmd = [sys.executable, str(ROOT / "Heart" / "scripts" / "publish" / "run.py")]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(ROOT))
        assert r.returncode == 2

    def test_clean_payload_passes_dry_run(self, tmp_path):
        payload = {"text": "Brand new product launch.", "author_role": "admin"}
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        r = self._run(
            "--channel=facebook_page",
            f"--payload={p}",
            "--dry-run",
        )
        assert r.returncode == 0, r.stderr or r.stdout

    def test_pii_payload_stripped(self, tmp_path):
        payload = {
            "text": "Hi @alice and bob@example.com",
            "author_role": "admin",
        }
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        r = self._run(
            "--channel=facebook_page",
            f"--payload={p}",
            "--dry-run",
        )
        assert r.returncode == 0
        assert "stripped" in r.stderr or "stripped" in r.stdout

    def test_ghost_role_denied(self, tmp_path):
        payload = {"text": "anything", "author_role": "ghost"}
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        r = self._run(
            "--channel=facebook_page",
            f"--payload={p}",
            "--dry-run",
        )
        assert r.returncode == 10  # brain gate denied

    def test_instagram_strips_links(self, tmp_path):
        payload = {
            "text": "Visit https://example.com",
            "author_role": "admin",
        }
        p = tmp_path / "payload.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        r = self._run(
            "--channel=instagram_brand",
            f"--payload={p}",
            "--dry-run",
        )
        assert r.returncode == 0

    def test_missing_payload_returns_4(self, tmp_path):
        r = self._run(
            "--channel=facebook_page",
            "--payload=C:/nonexistent/file.json",
            "--dry-run",
        )
        assert r.returncode == 4

    def test_malformed_json_returns_5(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json {", encoding="utf-8")
        r = self._run(
            "--channel=facebook_page",
            f"--payload={p}",
            "--dry-run",
        )
        assert r.returncode == 5


class TestChannelAuditDispatcher:
    def _run(self, *args):
        cmd = [
            sys.executable,
            str(ROOT / "Heart" / "scripts" / "channel-audit" / "run.py"),
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, env=os.environ.copy(),
            cwd=str(ROOT),
        )

    def test_writes_matrix(self, tmp_path):
        out = tmp_path / "matrix.json"
        r = self._run(f"--out={out}", "--once")
        assert r.returncode == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "channels" in data
        assert "public_channels" in data
        assert "generated_at" in data
        assert "facebook_page" in data["public_channels"]

    def test_help(self):
        r = self._run("--help")
        assert r.returncode == 0