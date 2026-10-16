"""Retry semantics for transient Windows file locks during atomic replace.

An external scanner or workspace watcher (Windows Search Indexer, OneDrive, an
IDE file watcher) can hold a short-lived handle on a freshly written file, so
``os.replace`` fails with ``ERROR_ACCESS_DENIED`` (WinError 5) or a sharing
violation. The helper retries exactly those errors and still raises a real
permission problem. ``os.replace`` is patched so no OS-level lock is needed.
"""

from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from coat_sim.core.atomic_io import replace_with_retry
from coat_sim.core.data_files import write_data_file


class _SharingViolation(OSError):
    winerror = 32


class ReplaceWithRetryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "value.tmp"
        self.destination = Path(self.directory.name) / "value.json"
        self.destination.write_text("old", encoding="utf-8")

    def _flaky(self, failures, calls):
        real = os.replace

        def attempt(source, destination):
            calls.append(1)
            if len(calls) <= failures:
                raise PermissionError(13, "拒绝访问。")
            return real(source, destination)

        return attempt

    def test_replaces_normally_without_retry(self):
        self.source.write_text("new", encoding="utf-8")
        replace_with_retry(self.source, self.destination)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "new")
        self.assertFalse(self.source.exists())

    def test_transient_permission_error_is_retried(self):
        self.source.write_text("new", encoding="utf-8")
        calls = []
        with patch("coat_sim.core.atomic_io.os.replace",
                   side_effect=self._flaky(3, calls)):
            replace_with_retry(self.source, self.destination, delay=0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "new")

    def test_sharing_violation_is_retried(self):
        self.source.write_text("new", encoding="utf-8")
        calls = []
        real = os.replace

        def attempt(source, destination):
            calls.append(1)
            if len(calls) <= 1:
                raise _SharingViolation(32, "sharing violation")
            return real(source, destination)

        with patch("coat_sim.core.atomic_io.os.replace", side_effect=attempt):
            replace_with_retry(self.source, self.destination, delay=0)
        self.assertEqual(len(calls), 2)

    def test_persistent_lock_raises_after_attempts(self):
        self.source.write_text("new", encoding="utf-8")
        with patch("coat_sim.core.atomic_io.os.replace",
                   side_effect=PermissionError(13, "拒绝访问。")) as mocked:
            with self.assertRaises(PermissionError):
                replace_with_retry(self.source, self.destination, attempts=4, delay=0)
        self.assertEqual(mocked.call_count, 4)

    def test_unrelated_oserror_is_not_retried(self):
        self.source.write_text("new", encoding="utf-8")
        with patch("coat_sim.core.atomic_io.os.replace",
                   side_effect=OSError(2, "no such file")) as mocked:
            with self.assertRaises(OSError):
                replace_with_retry(self.source, self.destination, attempts=5, delay=0)
        self.assertEqual(mocked.call_count, 1)


class ProductAtomicWriteTests(unittest.TestCase):
    """The product writer must survive a transient lock, not just the helper."""

    def test_data_file_write_survives_transient_lock(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        target = base / "note.txt"
        calls = []
        real = os.replace

        def attempt(source, destination):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, "拒绝访问。")
            return real(source, destination)

        with patch("coat_sim.core.data_files.data_path", return_value=target):
            with patch("coat_sim.core.data_files.DATA_ROOT", base):
                with patch("coat_sim.core.atomic_io.os.replace", side_effect=attempt):
                    write_data_file("note.txt", "hello")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")
        self.assertGreaterEqual(len(calls), 3)
        self.assertFalse(list(base.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
