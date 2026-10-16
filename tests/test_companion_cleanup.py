"""Removal cleanup regressions: mocks or disposable locked data files only.

No companion executable, installation, worker, provider or user state is run.
Windows fixture tests run only the generated system-shell cleanup against one
hash-checked dummy .exe file under their own new temporary directory.
"""
import base64
import contextlib
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion import maintenance


class CleanupHelperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="astra-cleanup-'fixture-")
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        self.helper = self.state / ("maintenance-helper-" + "a" * 16 + ".exe")
        self.payload = b"not an executable, disposable fixture only"
        self.helper.write_bytes(self.payload)
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.addCleanup(self.temporary.cleanup)

    def script(self):
        return maintenance.helper_cleanup_script(self.helper, self.state, self.digest, len(self.payload), 2_147_483_647)

    def test_script_rejects_nonowned_target_or_invalid_proof(self):
        for values in ((self.root / self.helper.name, self.state, self.digest, len(self.payload), 123),
                       (self.helper, self.state, "not-a-sha", len(self.payload), 123),
                       (self.helper, self.state, self.digest, 0, 123),
                       (self.helper, self.state, self.digest, len(self.payload), 0)):
            with self.assertRaisesRegex(ValueError, "Invalid copied helper cleanup"):
                maintenance.helper_cleanup_script(*values)
        self.assertEqual(self.helper.read_bytes(), self.payload)

    def test_system_shell_inherits_clean_paths_and_dll_directory_is_restored(self):
        for launch_fails in (False, True):
            with self.subTest(launch_fails=launch_fails):
                bundle = self.root / "bundle"
                unrelated = self.root / "bundle-other"
                original_path = os.pathsep.join(map(str, (bundle, bundle / "bin", unrelated)))
                shell = self.root / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                kernel = Mock()
                def directory(length, buffer):
                    if not length:
                        return len(str(bundle)) + 1
                    buffer.value = str(bundle)
                    return len(str(bundle))
                kernel.GetDllDirectoryW.side_effect = directory
                changes = []
                kernel.SetDllDirectoryW.side_effect = lambda value: changes.append(value) or 1
                def spawn(arguments, **options):
                    self.assertEqual(changes, [None])
                    self.assertEqual(options["env"]["PSMODULEPATH"], str(shell.parent / "Modules"))
                    self.assertEqual(options["env"]["PATH"], str(unrelated))
                    self.assertEqual(base64.b64decode(arguments[-1]).decode("utf-16le"), "fixture script")
                    if launch_fails:
                        raise OSError("fixture launch failure")
                    return object()
                with patch.dict(os.environ, {"PATH": original_path, "PSMODULEPATH": "incompatible-core-modules"}, clear=True), \
                     patch.object(sys, "_MEIPASS", str(bundle), create=True), \
                     patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                     patch.object(ctypes, "set_last_error", create=True), patch.object(ctypes, "get_last_error", return_value=0, create=True), \
                     patch.object(maintenance.subprocess, "Popen", side_effect=spawn):
                    if launch_fails:
                        with self.assertRaisesRegex(OSError, "fixture launch failure"):
                            maintenance.launch_cleanup_shell(shell, "fixture script")
                    else:
                        maintenance.launch_cleanup_shell(shell, "fixture script")
                    self.assertEqual(os.environ["PATH"], original_path)
                    self.assertEqual(os.environ["PSMODULEPATH"], "incompatible-core-modules")
                self.assertEqual(changes, [None, str(bundle)])

    @contextlib.contextmanager
    def locked_fixture(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(self.helper), 0x80000000, 1, None, 3, 0x80, None)
        self.assertNotIn(handle, (None, ctypes.c_void_p(-1).value))
        try:
            yield
        finally:
            kernel.CloseHandle(handle)

    def fixture_shell(self, script):
        # Verify the complete file target stays under this disposable fixture
        # before any deletion-capable script is launched. No recursive deletes.
        self.assertTrue(self.helper.resolve().is_relative_to(self.root))
        shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        environment = {key: value for key, value in os.environ.items() if key.casefold() != "psmodulepath"}
        # The pure-.NET script must work with no module autoload available.
        environment["PSMODULEPATH"] = str(self.root / "nonexistent-modules")
        child = subprocess.Popen([str(shell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
                                  base64.b64encode(script.encode("utf-16le")).decode("ascii")],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def finish():
            if child.poll() is None:
                child.kill()  # only this fixture's shell, never a PID/name search
            child.communicate(timeout=5)
        self.addCleanup(finish)
        return child

    def synchronized_retry(self, script):
        marker, gate = self.root / "retry-ready", self.root / "retry-continue"
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        hook = ("[IO.File]::WriteAllText(" + quote(marker) + ",'ready'); "
                "$testUntil=[DateTime]::UtcNow.AddSeconds(5); "
                "while(-not [IO.File]::Exists(" + quote(gate) + ") -and [DateTime]::UtcNow -lt $testUntil) { [Threading.Thread]::Sleep(10) }; "
                "[Threading.Thread]::Sleep(200)")
        self.assertEqual(script.count("[Threading.Thread]::Sleep(200)"), 1)
        return script.replace("[Threading.Thread]::Sleep(200)", hook), marker, gate

    def wait_retry(self, child, marker):
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists(), "The locked fixture must reach the retry path")

    @unittest.skipUnless(os.name == "nt", "Windows sharing and system shell")
    def test_locked_dummy_helper_is_removed_after_bootloader_like_lock_releases(self):
        script, marker, gate = self.synchronized_retry(self.script())
        with self.locked_fixture():
            child = self.fixture_shell(script)
            self.wait_retry(child, marker)
            self.assertTrue(self.helper.exists())
        gate.write_bytes(b"continue")
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertFalse(self.helper.exists())
        self.assertTrue(self.state.is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows sharing and system shell")
    def test_retry_rechecks_hash_and_preserves_replaced_helper_and_history(self):
        history = self.state / "retained-history.json"
        history.write_bytes(b"private fixture history")
        script, marker, gate = self.synchronized_retry(self.script())
        with self.locked_fixture():
            child = self.fixture_shell(script)
            self.wait_retry(child, marker)
        changed = self.payload.upper()  # same size; only the hash check distinguishes it
        self.helper.write_bytes(changed)
        gate.write_bytes(b"continue")
        self.assertEqual(child.wait(timeout=10), 1)
        self.assertEqual(self.helper.read_bytes(), changed)
        self.assertEqual(history.read_bytes(), b"private fixture history")

    @unittest.skipUnless(os.name == "nt", "Windows sharing and system shell")
    def test_persistent_sharing_lock_reaches_bounded_deadline_without_deleting(self):
        script = self.script()
        self.assertEqual(script.count("ElapsedMilliseconds"), 2)
        # Exercise the same deadline logic at one second in this fixture.
        script = script.replace("20000", "1000")
        with self.locked_fixture():
            child = self.fixture_shell(script)
            self.assertEqual(child.wait(timeout=8), 1)
            self.assertEqual(self.helper.read_bytes(), self.payload)

    @unittest.skipUnless(os.name == "nt", "Windows system shell")
    def test_genuinely_missing_helper_succeeds_only_with_valid_state(self):
        script = self.script()
        self.helper.unlink()
        child = self.fixture_shell(script)
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertTrue(self.state.is_dir())

    @unittest.skipUnless(os.name == "nt", "Windows sharing, reparse points and system shell")
    def test_retry_rechecks_state_directory_link_and_preserves_both_targets(self):
        for redirected_contains_helper in (False, True):
            with self.subTest(redirected_contains_helper=redirected_contains_helper):
                self.check_replaced_state_link(redirected_contains_helper)

    def check_replaced_state_link(self, redirected_contains_helper):
        redirected = self.root / "redirected"
        redirected.mkdir()
        redirected_helper = redirected / self.helper.name
        if redirected_contains_helper:
            redirected_helper.write_bytes(self.payload)  # same hash; only link guard distinguishes it
        probe = self.root / "link-permission-probe"
        try:
            probe.symlink_to(redirected, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable; no permission settings changed")
        probe.unlink()
        script, marker, gate = self.synchronized_retry(self.script())
        with self.locked_fixture():
            child = self.fixture_shell(script)
            self.wait_retry(child, marker)
        retained = self.root / "original-state"
        self.assertTrue(self.state.resolve().is_relative_to(self.root))
        self.assertTrue(retained.resolve().is_relative_to(self.root))
        self.state.rename(retained)
        self.state.symlink_to(redirected, target_is_directory=True)
        gate.write_bytes(b"continue")
        self.assertEqual(child.wait(timeout=10), 1)
        if redirected_contains_helper:
            self.assertEqual(redirected_helper.read_bytes(), self.payload)
        else:
            self.assertFalse(redirected_helper.exists())
        self.assertEqual((retained / self.helper.name).read_bytes(), self.payload)
        self.state.unlink()
        retained.rename(self.state)
        if redirected_contains_helper:
            redirected_helper.unlink()
        redirected.rmdir()
        marker.unlink()
        gate.unlink()


if __name__ == "__main__":
    unittest.main()
