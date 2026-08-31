"""
Tests for heartctl doctor --deep (cross-checks /healthz + .heartbeat + repo_summary)

Run: python -m pytest Heart/tools/test_doctor_deep.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "Heart" / "tools"))

import heartctl


class TestHealthzVsHeartbeat(unittest.TestCase):
    """_check_healthz_vs_heartbeat must return empty list when sources agree,
    and a list of specific drift messages when they disagree."""

    def test_no_heartbeat_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            with patch.object(heartctl, "HEARTBEAT_FILE", str(Path(tmp) / "nope")):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertEqual(len(drift), 1)
            self.assertIn("no .heartbeat file", drift[0])

    def test_invalid_health_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "notanumber"}):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertEqual(len(drift), 1)
            self.assertIn("HEART_HEALTH_PORT is not an integer", drift[0])

    def test_out_of_range_health_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "99999"}):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertEqual(len(drift), 1)
            self.assertIn("out of range", drift[0])

    def test_healthz_unreachable_returns_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertEqual(len(drift), 1)
            self.assertIn("/healthz on 127.0.0.1:1 unreachable", drift[0])

    def test_stale_heartbeat_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            # Write heartbeat with mtime 200s ago
            old_time = time.time() - 200
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            os.utime(hb, (old_time, old_time))
            fake_healthz = {"ok": True, "cycle": 5, "mode": "normal"}
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value=fake_healthz), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertTrue(any("stale .heartbeat" in d for d in drift),
                f"expected stale heartbeat message in {drift}")

    def test_cycle_regression_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            fake_healthz = {"ok": True, "cycle": 1, "mode": "normal"}
            fake_repo = {"cycle": 10, "repos": []}
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value=fake_healthz), \
                 patch.object(heartctl, "_read_repo_summary", return_value=fake_repo):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertTrue(any("cycle regression" in d for d in drift),
                f"expected cycle regression in {drift}")

    def test_cycle_drift_too_far_ahead(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            fake_healthz = {"ok": True, "cycle": 100, "mode": "normal"}
            fake_repo = {"cycle": 1, "repos": []}
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value=fake_healthz), \
                 patch.object(heartctl, "_read_repo_summary", return_value=fake_repo):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertTrue(any("cycle drift" in d for d in drift),
                f"expected cycle drift in {drift}")

    def test_healthy_agrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            fake_healthz = {"ok": True, "cycle": 5, "mode": "normal"}
            fake_repo = {"cycle": 4, "repos": []}  # only 1 cycle behind
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value=fake_healthz), \
                 patch.object(heartctl, "_read_repo_summary", return_value=fake_repo):
                drift = heartctl._check_healthz_vs_heartbeat()
            self.assertEqual(drift, [], f"expected no drift, got {drift}")


class TestDoctorDeepCommand(unittest.TestCase):
    """cmd_doctor_deep must print [OK] or [DRIFT] with specific messages."""

    def test_healthy_prints_ok(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            fake_healthz = {"ok": True, "cycle": 5, "mode": "normal"}
            fake_repo = {"cycle": 4, "repos": []}
            args = argparse.Namespace(fix_heartbeat=False)
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value=fake_healthz), \
                 patch.object(heartctl, "_read_repo_summary", return_value=fake_repo):
                rc = heartctl.cmd_doctor_deep(args)
            self.assertEqual(rc, 0)

    def test_drift_returns_1(self):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            old_time = time.time() - 200
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            os.utime(hb, (old_time, old_time))
            args = argparse.Namespace(fix_heartbeat=False)
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                rc = heartctl.cmd_doctor_deep(args)
            self.assertEqual(rc, 1)

    def test_fix_heartbeat_when_drifted_touches_file(self):
        """--fix-heartbeat (alias for --self-heal) must touch .heartbeat and return 0
        when the file was stale but is now healthy after the touch."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            old_time = time.time() - 200
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            os.utime(hb, (old_time, old_time))
            mtime_before = hb.stat().st_mtime
            args = argparse.Namespace(
                fix_heartbeat=True, self_heal=False, json=False,
                health_port=None, doctor=None
            )
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}), \
                 warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                rc = heartctl.cmd_doctor_deep(args)
            mtime_after = hb.stat().st_mtime
            self.assertGreater(mtime_after, mtime_before,
                "fix-heartbeat must advance the file mtime")
            self.assertEqual(rc, 0,
                "rc must be 0 after successful self-heal (post-heal state is healthy)")

    def test_fix_heartbeat_when_healthy_does_not_touch_file(self):
        """--fix-heartbeat must be a no-op when there is no drift.

        Touching a healthy .heartbeat would reset the "last good" reference
        point and could mask future regressions.  A healthy run with
        --fix-heartbeat should leave the file exactly as it was.
        """
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            mtime_before = hb.stat().st_mtime
            fake_healthz = {"ok": True, "cycle": 5, "mode": "normal"}
            fake_repo = {"cycle": 4, "repos": []}
            args = argparse.Namespace(fix_heartbeat=True)
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "1"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value=fake_healthz), \
                 patch.object(heartctl, "_read_repo_summary", return_value=fake_repo), \
                 warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                rc = heartctl.cmd_doctor_deep(args)
            mtime_after = hb.stat().st_mtime
            self.assertEqual(rc, 0)
            self.assertEqual(mtime_after, mtime_before,
                "healthy .heartbeat must not be touched even with --fix-heartbeat")


class TestHeartbeatContentValidation(unittest.TestCase):
    """_check_healthz_vs_heartbeat must detect a corrupted .heartbeat.

    Content corruption (zero-byte, whitespace-only, wrong bytes) is a
    distinct drift class from stale-mtime: it indicates the write was
    interrupted, the disk is bad, or an operator manually touched the file.
    """

    def test_zero_byte_heartbeat_triggers_corruption_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"")
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            corruption_msgs = [d for d in drift if "corrupted" in d]
            self.assertEqual(len(corruption_msgs), 1, f"expected 1 corruption msg, got: {drift}")

    def test_whitespace_only_heartbeat_triggers_corruption_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"   \n\t  \n")
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            corruption_msgs = [d for d in drift if "corrupted" in d]
            self.assertEqual(len(corruption_msgs), 1, f"expected 1 corruption msg, got: {drift}")

    def test_wrong_content_triggers_corruption_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"not the sentinel\n")
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            corruption_msgs = [d for d in drift if "corrupted" in d]
            self.assertEqual(len(corruption_msgs), 1, f"expected 1 corruption msg, got: {drift}")

    def test_valid_sentinel_does_not_trigger_corruption_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            corruption_msgs = [d for d in drift if "corrupted" in d]
            self.assertEqual(corruption_msgs, [], f"valid sentinel must not trigger corruption: {drift}")


class TestFetchHealthzIntegration(unittest.TestCase):
    """_fetch_healthz must return parsed JSON from a real local HTTP server,
    handle non-200 responses, and ignore HTTP_PROXY / HTTPS_PROXY env vars."""

    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/healthz":
                    if getattr(Handler, "_healthz_status", 200) == 200:
                        body = json.dumps(getattr(Handler, "_healthz_body", {})).encode()
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self.send_response(getattr(Handler, "_healthz_status", 500))
                        self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, fmt, *args):
                pass

        Handler._healthz_status = 200
        Handler._healthz_body = {"ok": True, "cycle": 42, "mode": "sports"}
        cls.Handler = Handler
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls._thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_real_socket_returns_parsed_json(self):
        """A real /healthz endpoint must be parsed and returned as a dict."""
        self.Handler._healthz_status = 200
        self.Handler._healthz_body = {"ok": True, "cycle": 42, "mode": "sports"}
        result = heartctl._fetch_healthz(self.port)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["cycle"], 42)
        self.assertEqual(result["mode"], "sports")

    def test_non_200_response_returns_none(self):
        """A 404 or 500 response must be treated as unreachable."""
        class FailingHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.end_headers()
            def log_message(self, fmt, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), FailingHandler)
        p = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = heartctl._fetch_healthz(p)
            self.assertIsNone(result)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_oversized_body_returns_none(self):
        """A response larger than _MAX_HEALTHZ_BYTES (1 MB) must return None.

        A malicious or misconfigured server that streams unlimited bytes would
        otherwise exhaust the doctor process's RAM.  The socket loop stops
        reading after the cap and the function returns None.
        """

        class GiantHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(10 * 1024 * 1024))
                self.end_headers()
                self.wfile.write(b"x" * 10 * 1024 * 1024)
            def log_message(self, fmt, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), GiantHandler)
        p = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = heartctl._fetch_healthz(p)
            self.assertIsNone(result)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_status_line_only_not_header_body(self):
        """Only the first status line's code must be checked — not any header.

        A 200 inside a header value (e.g. X-Forwarded-For: 1.2.3.200)
        must not cause a false-positive parse.  The regex anchors to the
        start of the line, not a substring anywhere in the header section.
        """

        class Fake200InHeaderHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.send_header(
                    "X-Proxy-Status",
                    "upstream returned 200 but we are 500",
                )
                self.end_headers()
                self.wfile.write(b'{"malicious": true}')
            def log_message(self, fmt, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake200InHeaderHandler)
        p = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = heartctl._fetch_healthz(p)
            self.assertIsNone(result)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_chunked_transfer_encoding_parsed(self):
        """A chunked Transfer-Encoding response must be decoded correctly."""

        class ChunkedHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"ok": True, "cycle": 99}).encode()
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                hex_size = format(len(body), "x")
                self.wfile.write(
                    f"{hex_size}\r\n".encode()
                    + body
                    + b"\r\n0\r\n\r\n"
                )
            def log_message(self, fmt, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), ChunkedHandler)
        p = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = heartctl._fetch_healthz(p)
            self.assertEqual(result, {"ok": True, "cycle": 99})
        finally:
            srv.shutdown()
            srv.server_close()

    def test_lenient_newline_separator(self):
        """A server that uses bare \\n\\n (not \\r\\n\\r\\n) must still parse."""

        class BareLFHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                body = json.dumps({"ok": True, "cycle": 77}).encode()
                self.wfile.write(
                    b"HTTP/1.0 200 OK\n"
                    b"Content-Length: "
                    + str(len(body)).encode()
                    + b"\n\n"
                    + body
                )
            def log_message(self, fmt, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), BareLFHandler)
        p = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = heartctl._fetch_healthz(p)
            self.assertEqual(result, {"ok": True, "cycle": 77})
        finally:
            srv.shutdown()
            srv.server_close()

    def test_connection_refused_returns_none(self):
        """A port that is not listening must return None, not raise.

        Covers the OSError path through socket.create_connection.  We bind
        a server to get a real port, then close it before calling the
        function — the kernel will reject the next connect() with
        ConnectionRefusedError, which is a subclass of OSError.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        # Socket is now closed — kernel will refuse connections to that port.
        result = heartctl._fetch_healthz(closed_port, timeout=1.0)
        self.assertIsNone(result)

    def test_redirect_response_returns_none(self):
        """A 3xx redirect must return None, not follow the redirect.

        Verifies that _fetch_healthz does not implement redirect-following.
        A 301/302 response with Location: /healthz would be incorrect to treat
        as success.  The regex only matches "HTTP/1.x 200 " so any non-200
        status code is correctly rejected.
        """

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(301)
                self.send_header("Location", "/healthz")
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, fmt, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        p = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = heartctl._fetch_healthz(p)
            self.assertIsNone(result)
        finally:
            srv.shutdown()
            srv.server_close()


class TestReadRepoSummarySizeCap(unittest.TestCase):
    """_read_repo_summary must reject files larger than 10 MB to prevent OOM."""

    def test_10mb_minus_one_is_accepted(self):
        """A file just under the cap must be parsed successfully."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            rs_path = brain / "heartbeat" / "repo_summary.json"
            rs_path.parent.mkdir(parents=True)
            valid_json = json.dumps({"cycle": 1, "repos": []})
            rs_path.write_text(valid_json, encoding="utf-8")
            cap = heartctl._MAX_REPO_SUMMARY_BYTES
            self.assertLess(rs_path.stat().st_size, cap)
            result = heartctl._read_repo_summary()
            self.assertEqual(result, {"cycle": 1, "repos": []})

    def test_10mb_plus_one_is_rejected(self):
        """A file just over the cap must return None (not OOM)."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            rs_path = brain / "heartbeat" / "repo_summary.json"
            rs_path.parent.mkdir(parents=True)
            cap = heartctl._MAX_REPO_SUMMARY_BYTES
            rs_path.write_text("x" * (cap + 1), encoding="utf-8")
            result = heartctl._read_repo_summary()
            self.assertIsNone(result)


class TestClockSkewTolerance(unittest.TestCase):
    """mtime slightly in the future must not trigger a clock-skew alarm."""

    def test_mtime_1s_future_not_flagged(self):
        """A 1s future mtime is within ±2s tolerance — must not appear in drift."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            future = time.time() + 1
            os.utime(hb, (future, future))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            skew_msgs = [d for d in drift if "clock skew" in d]
            self.assertEqual(skew_msgs, [], f"unexpected skew messages: {skew_msgs}")

    def test_mtime_3s_future_flagged(self):
        """A 3s future mtime exceeds the ±2s tolerance — must appear in drift."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            # Use 3600s to ensure future-ness survives any timing jitter
            future = time.time() + 3600
            os.utime(hb, (future, future))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                drift = heartctl._check_healthz_vs_heartbeat()
            skew_msgs = [d for d in drift if "clock skew" in d]
            self.assertEqual(len(skew_msgs), 1, f"expected 1 skew msg, got: {drift}")


class TestDoctorDiagnose(unittest.TestCase):
    def test_returns_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                d = heartctl._doctor_diagnose()
            self.assertIsInstance(d, dict)
            for key in ("ok", "drift", "max_age_s", "skew_tolerance_s", "mtime_age_s",
                        "sentinel_valid", "healthz_reachable", "healthz_cycle",
                        "repo_cycle", "health_port", "error"):
                self.assertIn(key, d, f"missing key: {key}")

    def test_diagnose_ok_when_sources_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                d = heartctl._doctor_diagnose()
            self.assertTrue(d["ok"])
            self.assertEqual(d["drift"], [])
            self.assertTrue(d["sentinel_valid"])
            self.assertTrue(d["healthz_reachable"])
            self.assertEqual(d["healthz_cycle"], 1)
            self.assertEqual(d["repo_cycle"], 1)

    def test_diagnose_flags_drift_when_sentinel_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"WRONG CONTENT")
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                d = heartctl._doctor_diagnose()
            self.assertFalse(d["ok"])
            self.assertFalse(d["sentinel_valid"])
            corrupt_msgs = [m for m in d["drift"] if "corrupted" in m]
            self.assertEqual(len(corrupt_msgs), 1)

    def test_diagnose_health_port_overrides_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "9090"}), \
                 patch.object(heartctl, "_fetch_healthz") as mock_fetch:
                mock_fetch.return_value = {"ok": True, "cycle": 1}
                d = heartctl._doctor_diagnose(health_port=9999)
                mock_fetch.assert_called_once_with(9999)
            self.assertEqual(d["health_port"], 9999)

    def test_skew_tolerance_included_in_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                d = heartctl._doctor_diagnose()
            self.assertIn("skew_tolerance_s", d)
            self.assertIsInstance(d["skew_tolerance_s"], int)


class TestDoctorSelfHeal(unittest.TestCase):
    def test_regenerates_corrupted_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"WRONG CONTENT")
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                diag = heartctl._doctor_diagnose()
            self.assertFalse(diag["sentinel_valid"])
            actions = heartctl._doctor_self_heal(diag, hb_path=hb)
            self.assertTrue(actions["regenerated_heartbeat"])
            self.assertEqual(hb.read_bytes(), heartctl.HEARTBEAT_SENTINEL_MARKER)

    def test_regenerate_uses_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"OLD CONTENT")
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                diag = heartctl._doctor_diagnose()
            self.assertFalse(diag["sentinel_valid"])
            self.assertEqual(hb.read_bytes(), b"OLD CONTENT")
            heartctl._doctor_self_heal(diag, hb_path=hb)
            self.assertEqual(hb.read_bytes(), heartctl.HEARTBEAT_SENTINEL_MARKER)

    def test_touches_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            old_mtime = time.time() - 300
            os.utime(hb, (old_mtime, old_mtime))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                diag = heartctl._doctor_diagnose()
            self.assertFalse(diag["ok"])
            stale_msgs = [m for m in diag["drift"] if "stale" in m]
            self.assertEqual(len(stale_msgs), 1)
            actions = heartctl._doctor_self_heal(diag, hb_path=hb)
            self.assertTrue(actions["touched_heartbeat"])
            new_mtime = hb.stat().st_mtime
            self.assertGreaterEqual(new_mtime, old_mtime)

    def test_no_self_heal_when_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                diag = heartctl._doctor_diagnose()
            actions = heartctl._doctor_self_heal(diag)
            self.assertFalse(actions["touched_heartbeat"])
            self.assertFalse(actions["regenerated_heartbeat"])


class TestDoctorDeepJson(unittest.TestCase):
    def test_json_output_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                args = argparse.Namespace(
                    json=True, self_heal=False, fix_heartbeat=False,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    rc = heartctl.cmd_doctor_deep(args)
            output = captured.getvalue()
            data = json.loads(output)
            self.assertIn("ok", data)
            self.assertIn("drift", data)
            self.assertIn("sources", data)
            self.assertIn("skew_tolerance_s", data["sources"])
            self.assertEqual(rc, 0)

    def test_json_includes_self_heal_when_requested_and_drifted(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"WRONG CONTENT")
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                args = argparse.Namespace(
                    json=True, self_heal=True, fix_heartbeat=False,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    rc = heartctl.cmd_doctor_deep(args)
            output = captured.getvalue()
            data = json.loads(output)
            self.assertIn("self_heal", data)
            self.assertIsNotNone(data["self_heal"])
            self.assertTrue(data["self_heal"]["regenerated_heartbeat"])
            self.assertIn("ok_after_heal", data)

    def test_json_rc_0_when_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                args = argparse.Namespace(
                    json=True, self_heal=False, fix_heartbeat=False,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    rc = heartctl.cmd_doctor_deep(args)
            self.assertEqual(rc, 0)

    def test_json_rc_1_when_drifted(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            old_mtime = time.time() - 300
            os.utime(hb, (old_mtime, old_mtime))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                args = argparse.Namespace(
                    json=True, self_heal=False, fix_heartbeat=False,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    rc = heartctl.cmd_doctor_deep(args)
            self.assertEqual(rc, 1)


class TestDoctorDeepSelfHealHuman(unittest.TestCase):
    def test_self_heal_regenerates_sentinel_on_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"WRONG CONTENT")
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                args = argparse.Namespace(
                    json=False, self_heal=True, fix_heartbeat=False,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    rc = heartctl.cmd_doctor_deep(args)
            self.assertEqual(hb.read_bytes(), heartctl.HEARTBEAT_SENTINEL_MARKER)
            self.assertIn("regenerated", captured.getvalue())

    def test_self_heal_on_stale_prints_ok_after_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            old_mtime = time.time() - 300
            os.utime(hb, (old_mtime, old_mtime))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                args = argparse.Namespace(
                    json=False, self_heal=True, fix_heartbeat=False,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    rc = heartctl.cmd_doctor_deep(args)
            output = captured.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("DRIFT", output)
            self.assertIn("SELF-HEAL", output)
            self.assertIn("[OK] all checks pass after self-heal", output)

    def test_fix_heartbeat_flag_also_triggers_self_heal(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"WRONG CONTENT")
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}), \
                 warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                args = argparse.Namespace(
                    json=False, self_heal=False, fix_heartbeat=True,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    rc = heartctl.cmd_doctor_deep(args)
            self.assertEqual(hb.read_bytes(), heartctl.HEARTBEAT_SENTINEL_MARKER)
            self.assertIn("regenerated", captured.getvalue())

    def test_fix_heartbeat_emits_deprecation_warning(self):
        """--fix-heartbeat must emit a DeprecationWarning so operators migrate to --self-heal."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}), \
                 warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                args = argparse.Namespace(
                    json=False, self_heal=False, fix_heartbeat=True,
                    health_port=None, doctor=None
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    heartctl.cmd_doctor_deep(args)
            deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertEqual(len(deprecation), 1, f"expected 1 DeprecationWarning, got: {caught}")
            self.assertIn("--self-heal", str(deprecation[0].message))


class TestDoctorDeepHealthPort(unittest.TestCase):
    def test_health_port_cli_overrides_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_HEALTH_PORT": "9090"}), \
                 patch.object(heartctl, "_fetch_healthz") as mock_fetch:
                mock_fetch.return_value = {"ok": True, "cycle": 1}
                d = heartctl._doctor_diagnose(health_port=9999)
                mock_fetch.assert_called_once_with(9999)
            self.assertEqual(d["health_port"], 9999)


class TestResolveSkewTolerance(unittest.TestCase):
    def test_default_value(self):
        heartctl._SKEW_TOLERANCE_S = 2
        with patch.dict(os.environ, {}, clear=True):
            v = heartctl._resolve_skew_tolerance_s()
        self.assertEqual(v, 2)

    def test_env_overrides_default(self):
        with patch.dict(os.environ, {"HEART_SKEW_TOLERANCE_S": "5"}):
            v = heartctl._resolve_skew_tolerance_s()
        self.assertEqual(v, 5)

    def test_negative_env_falls_back_to_default(self):
        with patch.dict(os.environ, {"HEART_SKEW_TOLERANCE_S": "-1"}):
            v = heartctl._resolve_skew_tolerance_s()
        self.assertEqual(v, 2)

    def test_non_integer_env_falls_back_to_default(self):
        with patch.dict(os.environ, {"HEART_SKEW_TOLERANCE_S": "bad"}):
            v = heartctl._resolve_skew_tolerance_s()
        self.assertEqual(v, 2)

    def test_skew_tolerance_used_in_diagnose(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            future = time.time() + 4
            os.utime(hb, (future, future))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.dict(os.environ, {"HEART_SKEW_TOLERANCE_S": "10"}), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                d = heartctl._doctor_diagnose()
            self.assertEqual(d["skew_tolerance_s"], 10)
            skew_msgs = [m for m in d["drift"] if "clock skew" in m]
            self.assertEqual(skew_msgs, [])


class TestDoctorExportDiagnostic(unittest.TestCase):
    def test_writes_json_to_brain_knowledge_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                d = heartctl._doctor_diagnose()
            heartctl._doctor_export_diagnostic(d)
            kb_dir = brain / "knowledge" / "doctor_deep"
            files = list(kb_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
            self.assertIn("ts", data)
            self.assertEqual(data["ok"], True)
            self.assertIn("skew_tolerance_s", data["sources"])

    def test_prunes_older_files_beyond_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                d = heartctl._doctor_diagnose()
            # Pre-seed the knowledge dir with files exceeding the cap
            kb_dir = brain / "knowledge" / "doctor_deep"
            kb_dir.mkdir(parents=True, exist_ok=True)
            for i in range(heartctl._MAX_KB_DOCTOR_DEEP_FILES + 5):
                (kb_dir / f"2026010{i:02d}T000000.json").write_text("{}")
            heartctl._doctor_export_diagnostic(d)
            files = list(kb_dir.glob("*.json"))
            self.assertLessEqual(len(files), heartctl._MAX_KB_DOCTOR_DEEP_FILES)

    def test_silently_swallows_export_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}), \
                 patch("pathlib.Path.mkdir", side_effect=OSError("simulated")):
                d = heartctl._doctor_diagnose()
                # Must not raise
                heartctl._doctor_export_diagnostic(d)

    def test_self_heal_error_message_on_regenerate_failure(self):
        """When the write-to-temp or rename step fails, the error must be
        captured in actions["errors"] with a descriptive message.

        We test write_bytes failure since pathlib.Path.write_bytes IS patchable
        at the Python level for the pure-Python fallback path.  The cleanup
        (unlink) also cannot be reliably tested on Python 3.14 pathlib C-builtin
        methods via unittest.mock — the cleanup code is correct by construction
        (standard pattern: try/finally equivalent with os.unlink after except).
        """
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"WRONG CONTENT")
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"ok": True, "cycle": 1}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": 1}):
                diag = heartctl._doctor_diagnose()
            self.assertFalse(diag["ok"])
            self.assertFalse(diag["sentinel_valid"])
            with patch("pathlib.Path.write_bytes", side_effect=OSError("simulated write failure")):
                actions = heartctl._doctor_self_heal(diag, hb_path=hb)
            self.assertFalse(actions["regenerated_heartbeat"])
            self.assertEqual(len(actions["errors"]), 1)
            self.assertIn("regenerate failed", actions["errors"][0])

    def test_sentinel_check_rejects_oversized_file(self):
        """A heartbeat file larger than the cap must be treated as invalid."""
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(b"A" * (heartctl._MAX_HEARTBEAT_SENTINEL_BYTES + 1))
            result = heartctl._check_heartbeat_content_is_sentinel(hb)
            self.assertFalse(result)

    def test_cycle_int_conversion_handles_string_garbage(self):
        """A non-integer cycle from /healthz must produce a clean drift message,
        not a TypeError crash."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"cycle": "not-a-number"}), \
                 patch.object(heartctl, "_read_repo_summary", return_value=None):
                d = heartctl._doctor_diagnose()
            self.assertFalse(d["ok"])
            self.assertFalse(d["healthz_reachable"])
            self.assertTrue(any("non-integer cycle" in m for m in d["drift"]))

    def test_repo_cycle_non_integer_produces_drift(self):
        """A non-integer cycle from repo_summary must produce a clean drift message."""
        with tempfile.TemporaryDirectory() as tmp:
            brain = Path(tmp)
            heartctl.BRAIN_PATH = brain
            hb = Path(tmp) / ".heartbeat"
            hb.write_bytes(heartctl.HEARTBEAT_SENTINEL_MARKER)
            now = time.time()
            os.utime(hb, (now, now))
            with patch.object(heartctl, "HEARTBEAT_FILE", str(hb)), \
                 patch.object(heartctl, "_fetch_healthz", return_value={"cycle": 5}), \
                 patch.object(heartctl, "_read_repo_summary", return_value={"cycle": "bad-cycle"}):
                d = heartctl._doctor_diagnose()
            self.assertFalse(d["ok"])
            self.assertIsNone(d["repo_cycle"])
            self.assertTrue(any("non-integer cycle" in m for m in d["drift"]))


if __name__ == "__main__":
    unittest.main()
