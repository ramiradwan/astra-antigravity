"""Atomic publication with disposable data files; no companion is launched."""
import contextlib
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion import integration


class AtomicJSONTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="astra-atomic-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "status.json"
        self.pending = self.path.with_suffix(".json.new")
        self.original = {"status": "old"}
        self.updated = {"status": "new", "complete": True}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")

    @staticmethod
    def failure(code):
        error = OSError("Fixture replacement failure")
        if code is not None:
            error.winerror = code
        return error

    def test_only_sharing_errors_retry_and_atomically_publish_complete_json(self):
        replace = os.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                calls = []
                def attempt(source, destination):
                    calls.append(True)
                    if len(calls) == 1:
                        self.assertEqual(json.loads(self.path.read_bytes()), self.original)
                        self.assertEqual(json.loads(self.pending.read_bytes()), self.updated)
                        raise self.failure(code)
                    replace(source, destination)
                self.path.write_text(json.dumps(self.original), encoding="utf-8")
                with patch.object(integration.os, "replace", side_effect=attempt), \
                     patch.object(integration.time, "monotonic", side_effect=(100, 100.5)), \
                     patch.object(integration.time, "sleep") as sleep:
                    integration.atomic_json(self.path, self.updated)
                self.assertEqual(calls, [True, True])
                sleep.assert_called_once_with(.02)
                self.assertEqual(json.loads(self.path.read_bytes()), self.updated)
                self.assertFalse(self.pending.exists())

    def test_unrelated_replace_errors_propagate_without_sleep_or_false_success(self):
        for code in (None, 2, 87):
            with self.subTest(winerror=code):
                error = self.failure(code)
                with patch.object(integration.os, "replace", side_effect=error) as replace, \
                     patch.object(integration.time, "sleep") as sleep:
                    with self.assertRaises(OSError) as caught:
                        integration.atomic_json(self.path, self.updated)
                self.assertIs(caught.exception, error)
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(json.loads(self.path.read_bytes()), self.original)
                self.assertEqual(json.loads(self.pending.read_bytes()), self.updated)

    def test_retry_rechecks_link_guard_and_preserves_original(self):
        redirected = False
        def is_link(path):
            return redirected and path == self.pending
        def after_failure(_delay):
            nonlocal redirected
            redirected = True
        with patch.object(Path, "is_symlink", is_link), \
             patch.object(integration.os, "replace", side_effect=self.failure(32)) as replace, \
             patch.object(integration.time, "sleep", side_effect=after_failure):
            with self.assertRaisesRegex(ValueError, "must not be links"):
                integration.atomic_json(self.path, self.updated)
        replace.assert_called_once()
        self.assertEqual(json.loads(self.path.read_bytes()), self.original)

    @contextlib.contextmanager
    def held_reader(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(self.path), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
        try:
            yield
        finally:
            kernel.CloseHandle(handle)

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_held_windows_reader_releases_and_writer_publishes_complete_json(self):
        blocked = threading.Event()
        failures = []
        replace = os.replace
        def observe(source, destination):
            try:
                return replace(source, destination)
            except OSError as error:
                if getattr(error, "winerror", None) in (5, 32, 33):
                    blocked.set()
                raise
        def publish():
            try:
                integration.atomic_json(self.path, self.updated)
            except Exception as error:
                failures.append(error)
        with patch.object(integration.os, "replace", side_effect=observe):
            with self.held_reader():
                thread = threading.Thread(target=publish)
                thread.start()
                self.addCleanup(thread.join, 5)
                self.assertTrue(blocked.wait(5), "A held reader must exercise actual replacement denial")
                self.assertEqual(json.loads(self.path.read_bytes()), self.original)
                self.assertEqual(json.loads(self.pending.read_bytes()), self.updated)
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(json.loads(self.path.read_bytes()), self.updated)
        self.assertFalse(self.pending.exists())

    @unittest.skipUnless(os.name == "nt", "Windows delete-sharing semantics")
    def test_permanent_windows_reader_reaches_deadline_and_does_not_publish(self):
        # Real held read handle/real failing os.replace; a fixed monotonic clock
        # makes deadline exhaustion deterministic without a slow timing test.
        with self.held_reader(), \
             patch.object(integration.time, "monotonic", side_effect=(100, 100.99, 101)), \
             patch.object(integration.time, "sleep") as sleep:
            with self.assertRaises(OSError) as caught:
                integration.atomic_json(self.path, self.updated)
        self.assertIn(caught.exception.winerror, (5, 32, 33))
        self.assertEqual(sleep.call_count, 1)
        self.assertLessEqual(sleep.call_args.args[0], .02)
        self.assertEqual(json.loads(self.path.read_bytes()), self.original)
        self.assertEqual(json.loads(self.pending.read_bytes()), self.updated)


if __name__ == "__main__":
    unittest.main()
