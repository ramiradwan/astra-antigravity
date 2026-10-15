"""Isolated lifecycle tests: no real registry, installation, provider, or shell."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile
import urllib.error

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from companion import integration, maintenance, updates, headless, app
from companion.controller import Controller, worker as actual_worker
from test_companion import Providers, WorkerAPI


class FakeRegistry:
    HKEY_CURRENT_USER, REG_SZ, REG_DWORD = 1, 1, 4
    def __init__(self):
        self.values = {}
    class Key:
        def __init__(self, path): self.path = path
        def __enter__(self): return self
        def __exit__(self, *_): pass
    def CreateKey(self, _hive, path):
        self.values.setdefault(path, {})
        return self.Key(path)
    def OpenKey(self, _hive, path):
        if path not in self.values: raise FileNotFoundError()
        return self.Key(path)
    def QueryValueEx(self, key, name):
        if name not in self.values[key.path]: raise FileNotFoundError()
        return self.values[key.path][name], self.REG_SZ
    def SetValueEx(self, key, name, _reserved, _kind, value): self.values[key.path][name] = value
    def DeleteValue(self, key, name):
        if name not in self.values[key.path]: raise FileNotFoundError()
        del self.values[key.path][name]
    def DeleteKey(self, _hive, path):
        if path not in self.values: raise FileNotFoundError()
        del self.values[path]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.install = self.root / "installed"
        self.install.mkdir()
        self.state = self.root / "private"
        self.state.mkdir()
        self.registry = FakeRegistry()
        # The signed-upgrade fixtures model a 0.4.0 runtime upgrading to 0.4.1.
        self.patches = [patch.object(integration, "VERSION", "0.4.0"),
            patch.object(integration, "install_root", return_value=self.install),
            patch.dict(sys.modules, {"winreg": self.registry}), patch.dict(os.environ, {"APPDATA": str(self.root / "roaming")})]
        for item in self.patches: item.start()
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.trusted = {"url": "https://updates.example.test/stable.json", "public_key": base64.b64encode(public).decode(), "repository": "ramiradwan/astra-antigravity"}
        self.exe_name = integration.launcher_executable().name
        self.asset_name = "AstraAntigravity.exe" if os.name == "nt" else "AstraAntigravity"
    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.temporary.cleanup()
    def archive(self, entries=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, content in (entries or {self.asset_name: b"NEW-TEST-EXE", "LICENSE": b"license"}).items():
                archive.writestr(name, content)
        return stream.getvalue()
    def envelope(self, blob=None, **changes):
        blob = blob or self.archive()
        value = {"schema": 1, "sequence": 2, "version": "0.4.1", "platform": sys.platform,
            "url": "https://github.com/ramiradwan/astra-antigravity/releases/download/v0.4.1/companion.zip",
            "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest(), "state_schema": 1}
        value.update(changes)
        raw = json.dumps(value, sort_keys=True).encode()
        return json.dumps({"manifest": base64.b64encode(raw).decode(), "signature": base64.b64encode(self.key.sign(raw)).decode()}).encode()
    def old_install(self):
        folder = self.install / "0.4.0"
        folder.mkdir()
        executable = folder / self.exe_name
        executable.write_bytes(b"OLD-TEST-EXE")
        integration.launcher_executable().write_bytes(b"BOOTSTRAP")
        selection = {"version": "0.4.0", "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "sequence": 1}
        integration.atomic_json(self.install / "current.json", selection)
        return selection
    def stage(self):
        blob = self.archive()
        return updates.stage(self.envelope(blob), self.trusted, self.install, download=lambda *_: blob)

    def test_signed_manifest_rejects_tampering_wrong_key_replay_and_untrusted_asset(self):
        envelope = self.envelope()
        self.assertEqual(updates.verify(envelope, self.trusted)["version"], "0.4.1")
        wrong = {**self.trusted, "public_key": base64.b64encode(bytes(32)).decode()}
        with self.assertRaises(InvalidSignature): updates.verify(envelope, wrong)
        packet = json.loads(envelope)
        packet["manifest"] = base64.b64encode(b"{}").decode()
        with self.assertRaises(InvalidSignature): updates.verify(json.dumps(packet).encode(), self.trusted)
        for changes in ({"sequence": 1}, {"url": "https://evil.example/companion.zip"}, {"version": "0.3.9"}, {"state_schema": 2}, {"platform": "unrelated"}, {"size": updates.MAX_RELEASE + 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                updates.verify(self.envelope(**changes), self.trusted, minimum_sequence=1)

    def test_standalone_installer_is_signed_and_same_release_only(self):
        installer = {"url": "https://github.com/ramiradwan/astra-antigravity/releases/download/v0.4.1/AstraAntigravity.exe", "size": 123, "sha256": "a" * 64}
        self.assertEqual(updates.verify(self.envelope(installer=installer), self.trusted)["installer"], installer)
        with self.assertRaises(ValueError): updates.verify(self.envelope(installer={**installer, "url": "https://evil.example/app.exe"}), self.trusted)

    def test_stage_checks_download_hash_and_archive_paths_before_writing(self):
        blob = self.archive()
        with self.assertRaisesRegex(ValueError, "checksum"):
            updates.stage(self.envelope(blob), self.trusted, self.install, download=lambda *_: b"different")
        evil = self.archive({self.asset_name: b"EXE", "LICENSE": b"license", "../escape": b"no"})
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            updates.stage(self.envelope(evil), self.trusted, self.install, download=lambda *_: evil)
        self.assertFalse((self.root / "escape").exists())
        self.assertFalse((self.install / "0.4.1").exists())

    def test_staged_executable_is_reverified_against_signed_archive(self):
        self.stage()
        self.assertEqual(updates.verify_staged(self.install, self.trusted)["version"], "0.4.1")
        (self.install / "0.4.1" / self.exe_name).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "verification"):
            updates.verify_staged(self.install, self.trusted)

    def test_linux_archive_installs_stable_lowercase_name_and_reverifies_on_windows(self):
        blob = self.archive({"AstraAntigravity": b"SIGNED-LINUX-TEST-EXE", "LICENSE": b"license"})
        envelope = self.envelope(blob, platform="linux")
        verify = updates.verify
        def linux_verify(packet, trusted, **options):
            return verify(packet, trusted, platform="linux", **options)
        with patch.object(updates, "verify", side_effect=linux_verify), patch.object(integration, "launcher_executable", return_value=self.install / "astra-antigravity"):
            selected = updates.stage(envelope, self.trusted, self.install, download=lambda *_: blob)
            installed = self.install / "0.4.1/astra-antigravity"
            self.assertEqual(installed.read_bytes(), b"SIGNED-LINUX-TEST-EXE")
            names = {path.name for path in installed.parent.iterdir()}
            self.assertIn("astra-antigravity", names)
            self.assertNotIn("AstraAntigravity", names)
            self.assertEqual(updates.verify_staged(self.install, self.trusted), selected)
            with zipfile.ZipFile(self.install / "0.4.1/release.zip") as original:
                self.assertIn("AstraAntigravity", original.namelist())
            installed.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "verification"):
                updates.verify_staged(self.install, self.trusted)

    def test_update_failure_rolls_back_selection_without_touching_task_state(self):
        old = self.old_install()
        self.stage()
        journal = self.state / "worker.sqlite3"
        journal.write_bytes(b"SAVED-TASK-HANDLES-AND-PAUSE")
        started = []
        def start(executable, arguments):
            started.append((executable, arguments))
            return SimpleNamespace(poll=lambda: 1)
        with patch.object(updates, "config", return_value=self.trusted):
            self.assertEqual(maintenance.activate_update(self.state, healthy=lambda *_: False, start=start), "rolled_back")
        self.assertEqual(json.loads((self.install / "current.json").read_bytes()), old)
        self.assertEqual(journal.read_bytes(), b"SAVED-TASK-HANDLES-AND-PAUSE")
        self.assertEqual(len(started), 2)
        self.assertIn("--validate-update", started[0][1])
        self.assertFalse((self.install / "pending-update.json").exists())
        self.assertEqual(json.loads((self.install / "failed-update.json").read_bytes())["sequence"], 2)

    def test_successful_update_keeps_previous_verified_application(self):
        old = self.old_install()
        self.stage()
        with patch.object(updates, "config", return_value=self.trusted):
            self.assertEqual(maintenance.activate_update(self.state, healthy=lambda *_: True, start=lambda *_: object()), "updated")
        self.assertEqual(json.loads((self.install / "previous.json").read_bytes()), old)
        self.assertTrue((self.install / "0.4.0" / self.exe_name).is_file())
        self.assertEqual(integration.selected_executable().parent.name, "0.4.1")

    @unittest.skipUnless(os.name == "nt", "Windows registry behavior")
    def test_startup_migrates_only_owned_version_and_retains_opt_in(self):
        self.old_install()
        run = "Software\\Microsoft\\Windows\\CurrentVersion\\Run"
        key = self.registry.CreateKey(1, run)
        self.registry.SetValueEx(key, integration.APP_ID, 0, 1, integration.startup_definition(self.install / "0.3.1" / self.exe_name))
        integration.migrate_startup(integration.launcher_executable())
        self.assertTrue(integration.startup_enabled(integration.launcher_executable()))
        integration.set_startup(integration.launcher_executable(), False)
        self.assertFalse(integration.startup_enabled(integration.launcher_executable()))
        self.registry.SetValueEx(key, integration.APP_ID, 0, 1, '"C:\\Other\\app.exe" --background')
        with self.assertRaises(ValueError): integration.migrate_startup(integration.launcher_executable())

    @unittest.skipUnless(os.name == "nt", "Windows installation mutex")
    def test_installation_mutex_excludes_concurrent_installer_without_real_install(self):
        errors = []
        def another():
            try:
                with integration.installation_lock(timeout=.01):
                    errors.append("unexpected acquisition")
            except ValueError as error:
                errors.append(str(error))
        with integration.installation_lock():
            contender = threading.Thread(target=another)
            contender.start(); contender.join(2)
            self.assertFalse(contender.is_alive())
        self.assertEqual(errors, ["Another installation or removal is in progress"])

    def test_older_installer_cannot_replace_newer_selected_version(self):
        old = self.old_install()
        before = sorted(path.name for path in self.install.iterdir())
        with patch.object(sys, "frozen", True, create=True), patch.object(integration, "VERSION", "0.3.1"):
            with self.assertRaisesRegex(ValueError, "downgrade"):
                integration.install_current_app()
        self.assertEqual(json.loads((self.install / "current.json").read_bytes()), old)
        self.assertEqual(sorted(path.name for path in self.install.iterdir()), before)

    @unittest.skipUnless(os.name == "nt", "Windows registry behavior")
    def test_uninstaller_removes_owned_integrations_without_other_entries(self):
        self.old_install()
        launcher = integration.launcher_executable()
        integration.register_protocol(launcher)
        integration.register_application(launcher)
        integration.set_startup(launcher, True)
        key = self.registry.CreateKey(1, "Software\\Unrelated")
        self.registry.SetValueEx(key, "keep", 0, 1, "untouched")
        integration.remove_integrations()
        self.assertFalse(integration.startup_enabled(launcher))
        self.assertEqual(self.registry.values["Software\\Unrelated"], {"keep": "untouched"})
        self.assertFalse((self.root / "roaming/Microsoft/Windows/Start Menu/Programs/Delegate to Antigravity.url").exists())

    def test_uninstaller_preserves_workspace_provider_and_default_task_history(self):
        self.old_install()
        workspace = self.root / "workspace"; workspace.mkdir(); (workspace / "report.txt").write_text("keep")
        provider = self.root / "provider-auth"; provider.write_text("private-test-provider")
        worker = self.state / "worker"; worker.mkdir(); (worker / "auth.json").write_text("synthetic")
        (worker / "worker.sqlite3").write_bytes(b"history")
        maintenance.remove_files(self.install)
        maintenance.remove_connection_proofs(self.state)
        self.assertFalse(self.install.exists())
        self.assertFalse((worker / "auth.json").exists())
        self.assertEqual((worker / "worker.sqlite3").read_bytes(), b"history")
        self.assertEqual((workspace / "report.txt").read_text(), "keep")
        self.assertTrue(provider.exists())

    def test_uninstaller_refuses_unknown_files_before_deleting_anything(self):
        self.old_install()
        (self.install / "0.4.0/user-file.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "unknown"):
            maintenance.remove_files(self.install)
        self.assertTrue(integration.launcher_executable().exists())
        self.assertTrue((self.install / "0.4.0/user-file.txt").exists())

    def test_uninstaller_verifies_bootstrap_proof_pairs_and_preserves_unknown_cache(self):
        self.old_install()
        cache = self.install / "bootstrap"; cache.mkdir()
        binary = b"SIGNED-TEST-INSTALLER"
        digest = hashlib.sha256(binary).hexdigest()
        installer = {"url": "https://github.com/ramiradwan/astra-antigravity/releases/download/v0.4.1/AstraAntigravity.exe", "size": len(binary), "sha256": digest}
        (cache / (digest + ".exe")).write_bytes(binary)
        (cache / (digest + ".manifest.json")).write_bytes(self.envelope(installer=installer))
        integration.atomic_json(cache / "channel.json", {"sequence": 2, "version": "0.4.1", "installer_sha256": digest})
        with patch.object(updates, "config", return_value=self.trusted):
            maintenance.validate_bootstrap(cache)
            unknown = cache / "personal.txt"; unknown.write_text("keep")
            with self.assertRaisesRegex(ValueError, "Unknown"):
                maintenance.remove_files(self.install)
            self.assertTrue(unknown.exists()); self.assertTrue(integration.launcher_executable().exists())
            unknown.unlink()
            maintenance.remove_files(self.install)
        self.assertFalse(self.install.exists())

    def test_completion_retry_uses_same_capability_and_stops_on_permanent_error(self):
        value = {"token": "TEST-PURPOSE-BOUND-TOKEN"}
        with patch.object(maintenance, "complete", side_effect=[urllib.error.URLError("offline"), None]) as complete, patch.object(maintenance.time, "sleep") as sleep:
            self.assertTrue(maintenance.complete_with_retry(value, "https://relay.example", "succeeded", "Removed"))
            self.assertEqual(complete.call_count, 2)
            self.assertIs(complete.call_args.args[0], value)
            sleep.assert_called_once_with(1)
        denied = urllib.error.HTTPError("https://relay.example", 401, "denied", {}, None)
        with patch.object(maintenance, "complete", side_effect=denied) as complete, patch.object(maintenance.time, "sleep") as sleep:
            self.assertFalse(maintenance.complete_with_retry(value, "https://relay.example", "succeeded", "Removed"))
            complete.assert_called_once(); sleep.assert_not_called()
        with patch.object(maintenance, "complete") as complete:
            self.assertFalse(maintenance.complete_with_retry(value, "https://relay.example", "succeeded", "Removed", maximum_seconds=0))
            complete.assert_not_called()

    def test_offline_removal_completion_keeps_protected_pending_receipt(self):
        folder = self.state / "worker"; folder.mkdir()
        integration.atomic_json(folder / "worker.json", {"relay": "https://relay.example"})
        completion = {"url": "https://relay.example/worker/removals/owner/op/complete", "token": "x" * 32, "expires_at": time.time() * 1000 + 60000}
        integration.atomic_json(self.state / "maintenance.json", {"schema": 1, "action": "uninstall", "state": "prepared", "nonce": "test-nonce", "parent_pid": 1,
            "operation_id": "op", "erase_history": False, "completion": completion})
        with patch.object(maintenance, "wait_for_parent"), patch.object(integration, "remove_integrations"), patch.object(maintenance, "remove_files"), patch.object(maintenance, "remove_connection_proofs"), patch.object(maintenance, "complete_with_retry", return_value=False), patch.object(maintenance, "cleanup_helper", return_value=False):
            maintenance.run(self.state, "test-nonce")
        self.assertTrue(json.loads((self.state / "maintenance-receipt.json").read_bytes())["completion_pending"])
        retained = json.loads((self.state / "maintenance.json").read_bytes())
        self.assertEqual(retained["completion"], completion)
        self.assertNotIn("nonce", retained)

    def test_completion_capability_is_bounded_to_own_service_and_operation(self):
        value = {"url": "https://relay.example.test/worker/removals/owner/operation/complete", "token": "x" * 32, "expires_at": time.time() * 1000 + 60000}
        maintenance.validate_completion(value, "https://relay.example.test")
        for changed in ({"url": "https://evil.example/worker/removals/owner/operation/complete"}, {"expires_at": 0}, {"url": value["url"] + "?token=leak"}):
            with self.assertRaises(ValueError): maintenance.validate_completion({**value, **changed}, "https://relay.example.test")

    def test_controller_removal_waits_for_durable_ack_and_idle_delivery(self):
        self.old_install()
        api = WorkerAPI()
        with patch.object(updates, "config", return_value=None):
            controller = Controller(self.state, provider_api=Providers, worker_api=api)
            controller.installed = integration.launcher_executable()
            controller.worker_thread = SimpleNamespace(is_alive=lambda: True, join=lambda: None)
            controller.update({"active_tasks": 1, "pending_delivery": False})
            with patch.object(maintenance, "prepare") as prepare, patch.object(controller, "request_quit") as quit:
                result = controller.control_handler("uninstall", {"erase_history": False}, "remove-1")
                self.assertEqual(result["state"], "pending")
                controller.maintenance_tick(); prepare.assert_not_called()
                controller.control_committed("remove-1")
                controller.maintenance_tick(); prepare.assert_not_called()
                controller.update({"active_tasks": 0, "pending_delivery": True})
                controller.maintenance_tick(); prepare.assert_not_called()
                controller.update({"pending_delivery": False})
                controller.maintenance_tick(); prepare.assert_called_once(); quit.assert_called_once()
            controller.close()

    def test_controller_rejects_arbitrary_operations_and_setting_types(self):
        self.old_install()
        with patch.object(updates, "config", return_value=None):
            controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
            controller.installed = integration.launcher_executable()
            for action, args in (("shell", {"command": "anything"}), ("set_automatic_updates", {"enabled": "yes"}), ("set_automatic_updates", {"enabled": True, "path": "elsewhere"})):
                with self.assertRaises(ValueError): controller.control_handler(action, args, "setting-1")
            result = controller.control_handler("set_automatic_updates", {"enabled": True}, "setting-2")
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(controller.control_handler("set_automatic_updates", {"enabled": True}, "setting-2")["state"], "succeeded")
            with self.assertRaises(ValueError): controller.control_handler("set_automatic_updates", {"enabled": False}, "setting-2")
            self.assertTrue(controller.control_snapshot()["automatic_updates_enabled"])
            controller.close()

    def test_portable_controller_cannot_inherit_or_replay_installed_maintenance(self):
        self.old_install()
        preference = self.state / "preferences.json"
        integration.atomic_json(preference, {"automatic_updates_enabled": True})
        before = preference.read_bytes()
        integration.atomic_json(self.state / "maintenance-receipt.json", {"outcome": "rolled_back"})
        retained = {
            "old-startup": {"action": "set_startup", "parameters": {"enabled": True}, "state": "succeeded", "summary": "Old result", "committed": True},
            "old-updates": {"action": "set_automatic_updates", "parameters": {"enabled": True}, "state": "succeeded", "summary": "Old result", "committed": True},
            "old-removal": {"action": "uninstall", "parameters": {}, "state": "pending", "summary": "Old result", "committed": True},
        }
        integration.atomic_json(self.state / "companion-controls.json", retained)
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(self.install / "0.4.0" / self.exe_name)), \
                patch.object(integration, "launcher_executable", side_effect=AssertionError("Portable mode must not inspect installed launcher")), \
                patch.object(integration, "startup_enabled", side_effect=AssertionError("No installed startup read")), \
                patch.object(integration, "set_startup") as startup, \
                patch.object(updates, "config", side_effect=AssertionError("No update configuration read")), \
                patch.object(updates, "fetch_feed") as feed, patch.object(updates, "stage") as stage, \
                patch.object(maintenance, "prepare") as prepare:
            controller = Controller(self.state, portable=True, provider_api=Providers, worker_api=WorkerAPI())
            try:
                self.assertIsNone(controller.installed)
                self.assertFalse(controller.snapshot()["startup_available"])
                snapshot = controller.control_snapshot()
                self.assertFalse(snapshot["installed"])
                self.assertFalse(snapshot["startup_enabled"])
                self.assertFalse(snapshot["automatic_updates_enabled"])
                self.assertEqual(snapshot["update_status"], "not_installed")
                self.assertIsNone(snapshot["uninstall_status"])
                for call in (lambda: controller.startup(True), lambda: controller.automatic_updates(True),
                        lambda: controller.local_uninstall(approved=True)):
                    with self.assertRaises(ValueError): call()
                for identifier, old in retained.items():
                    with self.subTest(action=old["action"]):
                        with self.assertRaises(ValueError):
                            controller.control_handler(old["action"], old["parameters"], identifier)
                # A retained pending removal or enabled preference cannot make
                # the portable lifecycle touch the installed application's feed.
                controller.maintenance_tick()
                self.assertEqual(preference.read_bytes(), before)
                startup.assert_not_called(); feed.assert_not_called(); stage.assert_not_called(); prepare.assert_not_called()
                # Management-only setup remains usable without installation.
                result = controller.control_handler("check_accounts", {}, "portable-check")
                self.assertEqual(result["state"], "pending")
                controller.operation.join(3)
                self.assertEqual(controller.control_result("portable-check")["state"], "succeeded")
            finally:
                controller.close()

    def test_unowned_frozen_executable_has_no_installed_maintenance_authority(self):
        self.old_install()
        candidate = self.root / "portable-candidate.exe"
        candidate.write_bytes(b"PRIVATE-PORTABLE-TEST")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(candidate)), \
                patch.object(integration, "launcher_executable", side_effect=AssertionError("No global installed launcher")), \
                patch.object(updates, "config", side_effect=AssertionError("No global update configuration")):
            controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
            try:
                self.assertIsNone(controller.installed)
                self.assertFalse(controller.control_snapshot()["installed"])
                for action, parameters in (("set_startup", {"enabled": False}), ("set_automatic_updates", {"enabled": False}), ("uninstall", {})):
                    with self.subTest(action=action), self.assertRaises(ValueError):
                        controller.control_handler(action, parameters, "unowned-" + action)
            finally:
                controller.close()

    def test_owned_installed_runtime_retains_startup_and_update_controls(self):
        self.old_install()
        executable = self.install / "0.4.0" / self.exe_name
        launcher = integration.launcher_executable()
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)), \
                patch.object(integration, "startup_enabled", return_value=False), \
                patch.object(integration, "set_startup") as startup, patch.object(updates, "config", return_value=None):
            controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
            try:
                self.assertEqual(controller.installed, launcher)
                self.assertTrue(controller.snapshot()["startup_available"])
                controller.control_handler("set_startup", {"enabled": True}, "owned-startup")
                startup.assert_called_once_with(launcher, True)
                controller.control_handler("set_automatic_updates", {"enabled": True}, "owned-updates")
                self.assertTrue(controller.control_snapshot()["installed"])
                self.assertTrue(controller.control_snapshot()["startup_enabled"])
                self.assertTrue(controller.control_snapshot()["automatic_updates_enabled"])
                self.assertEqual(json.loads((self.state / "preferences.json").read_bytes()), {"automatic_updates_enabled": True})
            finally:
                controller.close()

    def test_default_application_is_headless_and_never_opens_local_http_or_browser(self):
        with patch.object(headless, "run", return_value=0) as run:
            self.assertEqual(app.main(["--portable", "--state", str(self.state)]), 0)
            self.assertTrue(run.called)
            self.assertTrue(run.call_args.args[0].portable)
            self.assertFalse(hasattr(app, "SetupServer"))
            self.assertFalse(hasattr(app, "select_folder"))

    def test_headless_pairing_requires_no_workspace_provider_or_browser(self):
        class ManagementAPI(WorkerAPI):
            async def initialise(inner, args, state, progress, stopped):
                inner.arguments = args
                progress({"status": "approval_required", "pairing_code": "a" * 24, "verification_url": "https://relay.example.test/pair?code=" + "a" * 24})
                while not stopped.is_set():
                    await __import__("asyncio").sleep(.01)
        api = ManagementAPI()
        provider = SimpleNamespace(accounts=Mock(side_effect=AssertionError("No provider probe before approval")), find_program=lambda *_: None)
        browser = Mock(side_effect=AssertionError("No browser"))
        controller = Controller(self.state, provider_api=provider, worker_api=api, browser=browser)
        thread = threading.Thread(target=headless.run, args=(controller,))
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = headless.status(self.state)
                if value.get("pairing_code"): break
                time.sleep(.02)
            self.assertEqual(value.get("pairing_code"), "a" * 24)
            self.assertEqual(api.arguments.workspace, [])
            self.assertEqual(api.arguments.shared_workspace, [])
            self.assertTrue(api.arguments.setup_only)
            self.assertEqual(api.arguments.approval_mode, "until_revoked")
            provider.accounts.assert_not_called(); browser.assert_not_called()
            headless.submit(self.state, "quit")
        finally:
            controller.request_quit()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_headless_status_excludes_credentials_and_stale_state_is_unreachable(self):
        integration.atomic_json(self.state / "headless-status.json", {"status": "connected", "worker_running": True,
            "observed_at": time.time() * 1000, "credential": "PRIVATE-TEST-PROOF", "pairing_code": "a" * 24})
        self.assertNotIn("credential", headless.status(self.state))
        integration.atomic_json(self.state / "headless-status.json", {"status": "connected", "worker_running": True, "observed_at": 0})
        self.assertEqual(headless.status(self.state)["status"], "unreachable")
        for action, params in (("shell", {}), ("uninstall", {"path": "elsewhere"}), ("attach_request", {"request_id": "bad/path"})):
            with self.assertRaises(ValueError): headless.submit(self.state, action, params)

    def test_setup_account_control_finishes_asynchronously_without_inference(self):
        controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
        initial = controller.control_handler("check_accounts", {}, "check-1")
        self.assertEqual(initial["state"], "pending")
        controller.operation.join(3)
        self.assertEqual(controller.control_result("check-1")["state"], "succeeded")
        self.assertEqual(controller.control_snapshot()["accounts"]["codex"]["status"], "ready")
        self.assertEqual(controller.worker_api.starts, 0)
        controller.close()

    def test_workspace_setup_adds_alias_locally_and_prevents_rebinding(self):
        controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
        config = {"relay": controller.relay, "worker_identity": "identity.saved", "setup_only": True, "workspaces": {}}
        controller.worker_api.private_json(controller.connection / "worker.json", config)
        first = self.root / "first"; first.mkdir()
        second = self.root / "second"; second.mkdir()
        controller.control_handler("configure_workspace", {"path": str(first), "alias": "project", "isolation": "shared"}, "folder-1")
        controller.operation.join(3)
        result = controller.control_result("folder-1")
        self.assertEqual(result["state"], "succeeded")
        self.assertNotIn("path", result["workspace_approved"])
        replay = controller.control_handler("configure_workspace", {"path": str(first), "alias": "project", "isolation": "shared"}, "folder-1")
        self.assertEqual(replay["workspace_approved"], result["workspace_approved"])
        restarted = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
        replay = restarted.control_handler("configure_workspace", {"path": str(first), "alias": "project", "isolation": "shared"}, "folder-1")
        self.assertEqual(replay["workspace_approved"], result["workspace_approved"])
        restarted.close()
        saved = json.loads((controller.connection / "worker.json").read_bytes())
        self.assertEqual(saved["worker_identity"], config["worker_identity"])
        controller.control_handler("configure_workspace", {"path": str(second), "alias": "project", "isolation": "shared"}, "folder-2")
        controller.operation.join(3)
        self.assertEqual(controller.control_result("folder-2")["state"], "failed")
        self.assertEqual(json.loads((controller.connection / "worker.json").read_bytes()), saved)
        controller.close()

    def test_unbound_management_pair_retains_local_task_without_rewriting_claim(self):
        controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
        pending = {"setup_request_id": None, "config": {"relay": controller.relay, "setup_only": True, "workspaces": {}}}
        controller.worker_api.private_json(controller.connection / "pairing.json", pending)
        controller.update({"status": "approval_required"})
        controller.attach_request("original-task")
        self.assertEqual(controller.snapshot()["setup_request_id"], "original-task")
        self.assertIsNone(controller.arguments("init").setup_request_id)
        self.assertEqual(json.loads((controller.connection / "pairing.json").read_bytes()), pending)
        with self.assertRaises(ValueError): controller.attach_request("another-task")
        controller.close()

    def removed_history(self, *, receipt_state="succeeded"):
        folder = self.state / "worker"; folder.mkdir()
        integration.atomic_json(folder / "worker.json", {"relay": "https://relay.example.test", "worker_identity": "old-owner.old-worker",
            "workspaces": {"old": {"path": str(self.root), "isolation": ["shared"]}}})
        (folder / "worker.sqlite3").write_bytes(b"OLD-TASK-HANDLES-AND-CURSORS")
        provider = folder / "provider-tasks"; provider.mkdir(); (provider / "result.json").write_bytes(b"OLD-RESULT")
        integration.atomic_json(self.state / "setup-request.json", {"request_id": "old-task"})
        integration.atomic_json(self.state / "companion-controls.json", {"remove-1": {"action": "uninstall", "state": receipt_state,
            "parameters": {"erase_history": False}, "committed": True, "summary": "Removal finished."}})
        integration.atomic_json(self.state / "maintenance.json", {"state": receipt_state, "action": "uninstall", "operation_id": "remove-1"})
        integration.atomic_json(self.state / "maintenance-receipt.json", {"state": receipt_state, "outcome": "removed", "operation_id": "remove-1"})

    def test_verified_removal_reinstall_archives_old_identity_and_pairs_empty_new_one(self):
        self.removed_history()
        class ManagementAPI(WorkerAPI):
            async def initialise(inner, args, state, progress, stopped):
                inner.pairings += 1
                inner.arguments = args
                integration.atomic_json(Path(state) / "worker.json", {"relay": args.relay, "setup_only": True,
                    "worker_identity": "new-owner.new-worker", "workspaces": {}})
                integration.atomic_json(Path(state) / "auth.json", {"credential": "PRIVATE-TEST-NEW-PROOF"})
                progress({"status": "paired", "workspace_ids": []})
        api = ManagementAPI()
        provider = SimpleNamespace(accounts=Mock(side_effect=AssertionError("No provider probe")), find_program=lambda *_: None)
        controller = Controller(self.state, provider_api=provider, worker_api=api)
        self.assertEqual(controller.snapshot()["status"], "connection_invalid")
        self.assertTrue(controller.prepare_reinstall(new_request_id="new-task"))
        archives = list((self.state / "worker-archive").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "worker.sqlite3").read_bytes(), b"OLD-TASK-HANDLES-AND-CURSORS")
        self.assertEqual((archives[0] / "provider-tasks/result.json").read_bytes(), b"OLD-RESULT")
        self.assertEqual(json.loads((archives[0] / "archive.json").read_bytes())["setup_request_id"], "old-task")
        self.assertEqual(controller.snapshot()["setup_request_id"], "new-task")
        controller.pair()
        self.assertTrue(api.running.wait(2))
        self.assertEqual(api.arguments.workspace, [])
        self.assertEqual(api.arguments.shared_workspace, [])
        self.assertTrue(api.arguments.setup_only)
        self.assertEqual(api.arguments.setup_request_id, "new-task")
        self.assertEqual(json.loads((controller.connection / "worker.json").read_bytes())["worker_identity"], "new-owner.new-worker")
        provider.accounts.assert_not_called()
        self.assertEqual(api.pairings, 1)
        self.assertTrue(json.loads((self.state / "maintenance-receipt.json").read_bytes())["reinstall_consumed"])
        controller.close()

    def test_missing_or_failed_removal_receipt_never_archives_damaged_connection(self):
        self.removed_history(receipt_state="failed")
        controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
        self.assertFalse(controller.prepare_reinstall(new_request_id="new-task"))
        self.assertFalse((self.state / "worker-archive").exists())
        self.assertEqual((controller.connection / "worker.sqlite3").read_bytes(), b"OLD-TASK-HANDLES-AND-CURSORS")
        (self.state / "maintenance-receipt.json").unlink()
        self.assertFalse(controller.prepare_reinstall(new_request_id="new-task"))
        self.assertFalse((self.state / "worker-archive").exists())
        controller.close()

    def test_install_and_check_refresh_paths_before_readiness(self):
        installed = set()
        class Tools:
            def find_program(inner, name, *_): return str(self.root / (name + ".exe")) if name in installed else None
            def install(inner, name, *_): installed.add(name)
            accounts = staticmethod(Providers.accounts)
        with patch.dict(os.environ, {"CODEX_PATH": "OLD-CODEX", "AGY_PATH": "OLD-AGY", "ASTRA_ANTIGRAVITY_GIT_PATH": "OLD-GIT"}):
            controller = Controller(self.state, provider_api=Tools(), worker_api=WorkerAPI())
            controller.control_handler("install_provider", {"provider": "codex"}, "install-code")
            controller.operation.join(3)
            self.assertEqual(controller.control_result("install-code")["state"], "succeeded")
            self.assertEqual(os.environ["CODEX_PATH"], str(self.root / "codex.exe"))
            installed.update(("agy", "git"))
            controller.control_handler("check_accounts", {}, "check-paths")
            controller.operation.join(3)
            self.assertEqual(os.environ["AGY_PATH"], str(self.root / "agy.exe"))
            self.assertEqual(os.environ["ASTRA_ANTIGRAVITY_GIT_PATH"], str(self.root / "git.exe"))
            controller.close()

    def test_saved_restart_probes_accounts_once_without_signin_or_inference(self):
        folder = self.state / "worker"; folder.mkdir()
        integration.atomic_json(folder / "worker.json", {"relay": "https://relay.example.test", "setup_only": True, "workspaces": {}})
        integration.atomic_json(folder / "auth.json", {"credential": "PRIVATE-TEST-SAVED"})
        provider = SimpleNamespace(accounts=Mock(side_effect=Providers.accounts), find_program=lambda *_: None)
        api = WorkerAPI()
        controller = Controller(self.state, provider_api=provider, worker_api=api)
        thread = threading.Thread(target=headless.run, args=(controller,)); thread.start()
        try:
            self.assertTrue(api.running.wait(2))
            provider.accounts.assert_called_once()
            self.assertEqual(controller.control_snapshot()["accounts"]["codex"]["status"], "ready")
        finally:
            controller.request_quit(); thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_expired_uninstall_scrubs_capability_and_cannot_schedule_removal(self):
        self.old_install()
        controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
        controller.installed = integration.launcher_executable()
        completion = {"url": controller.relay + "/worker/removals/owner/expire-1/complete", "token": "x" * 32, "expires_at": time.time() * 1000 + 60000}
        controller.control_handler("uninstall", {"completion": completion}, "expire-1")
        controller.control_expired("expire-1")
        with patch.object(maintenance, "prepare") as prepare:
            controller.control_committed("expire-1"); controller.maintenance_tick(); prepare.assert_not_called()
        self.assertEqual(controller.controls["expire-1"]["state"], "unconfirmed")
        self.assertNotIn("completion", controller.controls["expire-1"]["parameters"])
        controller.close()

    def test_receipt_only_retry_acknowledges_or_expires_without_task_authority(self):
        helper = self.state / ("maintenance-helper-" + "a" * 16 + ".exe"); helper.write_bytes(b"VERIFIED-TEST-HELPER")
        completion = {"url": "https://relay.example/worker/removals/owner/retry-1/complete", "token": "x" * 32, "expires_at": time.time() * 1000 + 60000}
        value = {"operation_id": "retry-1", "completion": completion, "helper_name": helper.name, "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}
        maintenance.retain_delivery(self.state, value, "https://relay.example", "succeeded")
        self.assertEqual(headless.status(self.state)["removal_receipt"]["state"], "pending")
        with patch.object(maintenance, "complete_with_retry", return_value=True) as send, patch.object(app.integration, "install_current_app", side_effect=AssertionError("No install")), patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(app.main(["--state", str(self.state), "--retry-removal"]), 0)
        send.assert_called_once()
        self.assertFalse(helper.exists())
        self.assertEqual(headless.status(self.state)["removal_receipt"]["state"], "acknowledged")
        value["operation_id"] = "expired-1"; value["completion"] = {**completion, "expires_at": 0}
        maintenance.retain_delivery(self.state, value, "https://relay.example", "succeeded")
        self.assertEqual(headless.status(self.state)["removal_receipt"]["state"], "unconfirmed")
        for path in (self.state / "removal-receipts").iterdir():
            self.assertNotIn("completion", json.loads(path.read_bytes()))

    def test_real_worker_controller_setup_dtos_from_zero_workspace_without_inference(self):
        import asyncio
        config = {"relay": "https://relay.example.test", "worker_identity": "owner.saved", "setup_only": True, "workspaces": {}}
        controller = Controller(self.state, provider_api=Providers, worker_api=WorkerAPI())
        integration.atomic_json(controller.connection / "worker.json", config)
        folder = self.root / "approved-folder"; folder.mkdir()
        class Gateway:
            async def readiness(inner, *_): return {"ready": True, "ready_for": {"light": True}}
            async def close(inner): pass
            async def launch(inner, *_): raise AssertionError("Setup must not run inference")
        async def scenario():
            with patch.object(actual_worker, "Gateway", return_value=Gateway()) as factory:
                daemon = actual_worker.Worker(config, controller.connection, controls=controller.arguments("run"))
                self.assertIsNone(daemon.gateway); factory.assert_not_called()
                try:
                    reply = await daemon.computer_control({"operation_id": "wire-check", "action": "check_accounts", "parameters": {}, "revision": 1})
                    self.assertEqual(reply["state"], "pending")
                    await asyncio.to_thread(controller.operation.join, 3)
                    await daemon.maintain_controls()
                    reply = json.loads(daemon.journal.db.execute("SELECT result FROM computer_operations WHERE id='wire-check'").fetchone()[0])
                    self.assertEqual(reply["state"], "succeeded")
                    self.assertEqual(reply["snapshot"]["accounts"]["codex"]["status"], "ready")
                    await daemon.computer_control({"operation_id": "wire-folder", "action": "configure_workspace", "parameters": {"path": str(folder), "alias": "project", "isolation": "shared"}, "revision": 2})
                    await asyncio.to_thread(controller.operation.join, 3)
                    await daemon.maintain_controls()
                    reply = json.loads(daemon.journal.db.execute("SELECT result FROM computer_operations WHERE id='wire-folder'").fetchone()[0])
                    self.assertEqual(reply["state"], "succeeded")
                    self.assertEqual(reply["workspace_approved"]["id"], "project")
                    self.assertNotIn("path", reply["workspace_approved"])
                    self.assertEqual(daemon.workspaces["project"]["path"], folder.resolve())
                    self.assertNotIn(str(folder), json.dumps(reply))
                    self.assertFalse(daemon.computer_snapshot()["setup_only"])
                    factory.assert_called_once()
                finally:
                    await daemon.close()
        asyncio.run(scenario())
        controller.close()
