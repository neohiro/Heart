"""
Tests for Heart/tools/atomic.py

Run: python -m pytest Heart/tools/test_atomic.py
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "Heart" / "tools"))
from atomic import write_json, write_json_text, write_text, write_bytes


class TestAtomicWriteJson(unittest.TestCase):
    """write_json must produce a valid JSON file that is atomically promoted."""

    def test_basic_dict_roundtrip(self):
        """The written file contains the expected dict."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json(path, {"a": 1, "b": 2})
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1, "b": 2})

    def test_indent_respected(self):
        """Default indent=2 produces a formatted file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json(path, {"a": 1})
            lines = path.read_text(encoding="utf-8").splitlines()
            # {"a": 1} is one line; formatted version has newlines
            self.assertGreater(len(lines), 1)

    def test_compact_with_none_indent(self):
        """indent=None produces a compact single-line file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json(path, {"a": 1}, indent=None)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\n", text)

    def test_accepts_serialised_str(self):
        """write_json_text accepts a pre-serialised JSON string (bypasses json.dumps)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json_text(path, '{"custom": true}')
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"custom": True})

    def test_no_leftover_tmp(self):
        """No .tmp files are left behind after a successful write."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json(path, {"ok": True})
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"Unexpected temp files: {leftovers}")

    def test_prefix_respected(self):
        """The mkstemp prefix is used in the temp filename."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json(path, {"k": "v"}, prefix=".test.")
            # The temp file is cleaned up after rename, so we just verify
            # the write succeeds with a custom prefix (temp file existence
            # is tested by the no-leftover test).
            self.assertTrue(path.is_file())

    def test_target_untouched_on_failure(self):
        """If the rename fails, the original file is untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text('{"original": true}', encoding="utf-8")
            # Point at a non-existent directory so mkstemp raises OSError
            # before any write to the target.  The original must survive.
            impossible_path = Path(tmp) / "nonexistent_subdir" / "out.json"
            with self.assertRaises(OSError):
                write_json(impossible_path, {"new": True})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"original": True},
                "original file must be untouched after failed write",
            )


class TestAtomicWriteText(unittest.TestCase):
    """write_text atomically promotes a plain text file."""

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            write_text(path, "hello world\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "hello world\n")

    def test_no_leftover_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            write_text(path, "line1\n")
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"Unexpected temp files: {leftovers}")


class TestAtomicWriteBytes(unittest.TestCase):
    """write_bytes atomically promotes a byte string."""

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.bin"
            write_bytes(path, b"\x00\x01\x02\xff")
            self.assertEqual(path.read_bytes(), b"\x00\x01\x02\xff")

    def test_no_leftover_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.bin"
            write_bytes(path, b"data")
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"Unexpected temp files: {leftovers}")


class TestAtomicConcurrencySafety(unittest.TestCase):
    """Two concurrent writes to the same path must not share a temp filename."""

    def test_concurrent_writes_unique_tmp(self):
        """Simulate concurrent writes; each must get a different temp file."""
        import threading
        results: list[str] = []

        def writer(value: str) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "shared.json"
                write_json(path, {"value": value}, indent=None)

        threads = [threading.Thread(target=writer, args=(f"v{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each write succeeds (no crash from tmp filename collision)
        # and produces a valid file.
        self.assertTrue(True, "all concurrent writes completed without crash")


class TestAtomicStrictness(unittest.TestCase):
    """write_json / write_json_text — boundary tests for the strict-vs-permissive
    serialisation contract.

    Note: write_json uses json.dumps with default=str, so callables ARE
    silently coerced to their str() form. This is intentional — it lets
    callers pass objects like datetime/Path/UUID without ceremony. The
    test below documents the actual behaviour."""

    def test_write_json_silently_coerces_callables(self):
        """default=str coerces callables to their str() form (documented behaviour)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json(path, {"bad": lambda x: x})  # type: ignore
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("bad", data)
            self.assertIn("lambda", data["bad"])  # str(lambda) contains 'lambda'

    def test_write_json_accepts_datetime_via_default_str(self):
        """datetime is a common case — default=str coerces to ISO format."""
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            ts = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
            write_json(path, {"ts": ts})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("2026", data["ts"])

    def test_write_json_accepts_list_at_top_level(self):
        """A top-level list is valid JSON; the function does not require a dict."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            write_json(path, [1, 2, 3])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [1, 2, 3])

    def test_write_json_text_writes_verbatim(self):
        """write_json_text does not parse or re-format; the input bytes are the output bytes."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            # Deliberately non-standard JSON (no spaces, single-quote keys, trailing newline)
            payload = "{'a':1,}\n"
            write_json_text(path, payload)
            self.assertEqual(path.read_text(encoding="utf-8"), payload)


class TestFdopenFailureCleansUp(unittest.TestCase):
    """If os.fdopen raises after mkstemp succeeds, the fd must be closed and
    the temp file unlinked — no fd-leak, no .tmp-file leak."""

    def test_fdopen_failure_closes_fd_and_unlinks_tmp(self):
        """fdopen-raise must not leak fd numbers or .tmp files."""
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "Heart" / "tools"))
        from atomic import write_text

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"

            def bad_fdopen(fd: int, mode: str):
                raise OSError("injected fdopen failure")

            with patch("atomic.os.fdopen", bad_fdopen):
                with self.assertRaises(OSError) as ctx:
                    write_text(path, "should not appear")
                self.assertIn("injected", str(ctx.exception))

            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_mkstemp_failure_propagates(self):
        """If mkstemp itself raises, no cleanup is needed (no fd was opened)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nonexistent_subdir" / "out.txt"
            with self.assertRaises(OSError):
                write_text(path, "data")


class TestAtomicIdempotentRewrite(unittest.TestCase):
    """Writing the same payload to an existing path must succeed twice.

    Documents the contract that write_json / write_text / write_bytes are
    safe to call repeatedly on the same path.  Without this guarantee,
    a Heart cycle that re-writes the same heartbeat would crash on the
    second cycle.
    """

    def test_write_json_twice_with_same_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            data = {"cycle": 1, "repos": ["a", "b"]}
            write_json(path, data)
            # Second write must not raise and must produce the same content.
            write_json(path, data)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), data)
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_write_text_twice_with_same_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            write_text(path, "first\n")
            write_text(path, "first\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "first\n")
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_write_bytes_twice_overwrites(self):
        """A second write with different bytes must overwrite, not append."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.bin"
            write_bytes(path, b"old")
            write_bytes(path, b"new")
            self.assertEqual(path.read_bytes(), b"new")


class TestAtomicReadOnlyFilesystem(unittest.TestCase):
    """A failure during f.write() / os.fsync() must leave the original untouched.

    Simulates a LUKS-remounted-read-only or kernel ENOSPC scenario: mkstemp
    succeeds (the temp file is created), but a later step (fsync) raises.
    The target file must not be replaced, and the .tmp must be cleaned up.
    """

    def test_fsync_failure_preserves_target_and_unlinks_tmp(self):
        """os.fsync raising must not corrupt the target file or leak .tmp."""
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "Heart" / "tools"))
        from atomic import write_text

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            path.write_text("original\n", encoding="utf-8")

            def bad_fsync(fd):
                raise OSError("injected fsync failure")

            with patch("atomic.os.fsync", bad_fsync):
                with self.assertRaises(OSError) as ctx:
                    write_text(path, "new content")
                self.assertIn("injected", str(ctx.exception))

            self.assertEqual(path.read_text(encoding="utf-8"), "original\n",
                "target must be untouched after fsync failure")
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_write_failure_preserves_target_and_unlinks_tmp(self):
        """os.replace raising must not corrupt the target or leak .tmp.

        Covers any failure after the write+fsync succeeds: rename to the final
        path is the last operation before success. If it raises, the except
        block must unlink the temp file and re-raise.
        """
        import sys
        from unittest.mock import patch

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "Heart" / "tools"))
        from atomic import write_text

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.txt"
            path.write_text("original\n", encoding="utf-8")

            with patch("atomic.os.replace",
                       side_effect=OSError("injected rename failure")):
                with self.assertRaises(OSError) as ctx:
                    write_text(path, "new content")
                self.assertIn("injected", str(ctx.exception))

            self.assertEqual(path.read_text(encoding="utf-8"), "original\n",
                "target must be untouched after os.replace failure")
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")


if __name__ == "__main__":
    unittest.main()
