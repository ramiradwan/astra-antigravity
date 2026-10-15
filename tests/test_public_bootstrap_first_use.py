"""Bootstrap verifier contracts; no install, live registry, network or pairing."""
import base64
import contextlib
import copy
import ctypes
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import verify_public_bootstrap_first_use as first_use


class FirstUseTests(unittest.TestCase):
    def setUp(self):
        self.pins = json.loads(first_use.PINS_PATH.read_text(encoding="utf-8"))
        self.pins["bootstrap_sha256"] = "a" * 64  # fixture only, never a public pin
        self.environment = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows",
                            "GITHUB_REPOSITORY": first_use.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
                            "GITHUB_REF": "refs/heads/main"}
        self.event = {"repository": {"private": False, "full_name": first_use.REPOSITORY}}

    def package(self, extra=None, metadata=None, member_kind=None):
        content = {"package/package.json": json.dumps(metadata or {"name": "delegate-to-antigravity", "version": self.pins["bootstrap_version"],
                   "bin": {"delegate-to-antigravity": "bin.mjs"}, "scripts": {"test": "node --test"}}).encode(),
                   "package/release-config.json": json.dumps({"manifest_url": first_use.MANIFEST_URL, "public_key": first_use.PUBLIC_KEY}).encode(),
                   **{"package/" + name: b"fixture" for name in ("bin.mjs", "bootstrap.mjs", "LICENSE", "README.md")}}
        if extra:
            content.update(extra)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, raw in content.items():
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                if member_kind and name == "package/bin.mjs":
                    item.type = member_kind
                    item.linkname = "../../outside"
                    item.size = 0
                archive.addfile(item, io.BytesIO(raw))
        return stream.getvalue()

    def test_missing_release_pin_fails_before_download_or_process(self):
        # Exercise incomplete release preparation through the real entry point.
        incomplete = {**self.pins, "bootstrap_sha256": None}
        with patch.object(first_use, "json_file", return_value=incomplete), patch.object(first_use, "download") as download, \
             patch.object(first_use.subprocess, "Popen") as process:
            with self.assertRaisesRegex(first_use.CheckFailed, "pins_complete"):
                first_use.verify({})
        download.assert_not_called()
        process.assert_not_called()

    def test_reviewed_pins_reject_alternate_repository_urls_and_zero_hash(self):
        first_use.validate_pins(self.pins)
        for key, value in (("bootstrap_url", self.pins["bootstrap_url"] + "?token=fixture"),
                           ("installer_url", self.pins["installer_url"].replace("ramiradwan", "other")),
                           ("manifest_url", "https://example.invalid/manifest.json"),
                           ("installer_sha256", "0" * 64), ("sequence", True)):
            with self.subTest(key=key):
                changed = {**self.pins, key: value}
                with self.assertRaises(first_use.CheckFailed):
                    first_use.validate_pins(changed)

    def test_only_public_standard_windows_dispatch_main_is_accepted(self):
        first_use.runner_guard(self.environment, self.event, platform="nt")
        for key, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_REF", "refs/heads/unreviewed"),
                           ("GITHUB_REPOSITORY", "ramiradwan/astra-antigravity-plugin"),
                           ("RUNNER_ENVIRONMENT", "self-hosted"), ("RUNNER_OS", "Linux")):
            with self.subTest(key=key), self.assertRaises(first_use.CheckFailed):
                first_use.runner_guard({**self.environment, key: value}, self.event, platform="nt")
        for event in ({"repository": {"private": True, "full_name": first_use.REPOSITORY}},
                      {"repository": {"private": False, "full_name": "other/repo"}}):
            with self.assertRaises(first_use.CheckFailed):
                first_use.runner_guard(self.environment, event, platform="nt")

    def test_redirects_allow_only_github_release_and_github_asset_storage(self):
        for url in (self.pins["bootstrap_url"], "https://release-assets.githubusercontent.com/assets/a?sig=private", "https://objects.githubusercontent.com/a"):
            self.assertTrue(first_use.approved_redirect(url))
        for url in ("http://github.com/ramiradwan/astra-antigravity/releases/download/a", "https://github.com.evil.invalid/a",
                    "https://github.com/other/repo/releases/download/a", "https://github.com/login", "https://user:secret@objects.githubusercontent.com/a",
                    "https://objects.githubusercontent.com:8080/a", "https://objects.githubusercontent.com/a#fragment"):
            self.assertFalse(first_use.approved_redirect(url))

    def test_download_hash_mismatch_and_redirect_rejected_before_execution(self):
        response = Mock(status=200)
        response.read.return_value = b"different bytes"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(first_use.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(first_use.CheckFailed, "public_asset_hash"):
                first_use.download(self.pins["bootstrap_url"], self.pins["bootstrap_sha256"], 1024)
        opener.open.side_effect = urllib.error.HTTPError("https://fixture.invalid", 302, "Found", {"Location": "https://evil.invalid/secret"}, None)
        with patch.object(first_use.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(first_use.CheckFailed, "approved_asset_redirect"):
                first_use.download(self.pins["bootstrap_url"], self.pins["bootstrap_sha256"], 1024)

    def test_exact_package_inventory_and_no_install_hooks_or_dependencies(self):
        first_use.validate_package(self.package(), self.pins)
        for raw in (self.package(extra={"package/.npmrc": b"token=fixture"}),
                    self.package(extra={"../../outside": b"fixture"}),
                    self.package(member_kind=tarfile.SYMTYPE),
                    self.package(metadata={"name": "delegate-to-antigravity", "version": self.pins["bootstrap_version"], "bin": {"delegate-to-antigravity": "bin.mjs"},
                                           "scripts": {"preinstall": "run-something"}}),
                    self.package(metadata={"name": "delegate-to-antigravity", "version": self.pins["bootstrap_version"], "bin": {"delegate-to-antigravity": "bin.mjs"},
                                           "dependencies": {"unreviewed": "latest"}})):
            with self.assertRaises(first_use.CheckFailed):
                first_use.validate_package(raw, self.pins)

    def test_manifest_must_match_release_version_sequence_and_installer(self):
        manifest = {"schema": 1, "state_schema": 1, "platform": "win32", "version": self.pins["companion_version"],
                    "sequence": self.pins["sequence"], "installer": {"url": self.pins["installer_url"],
                    "sha256": self.pins["installer_sha256"], "size": self.pins["installer_size"]}}
        def envelope(value):
            return json.dumps({"manifest": base64.b64encode(json.dumps(value).encode()).decode(), "signature": "fixture"}).encode()
        first_use.validate_manifest(envelope(manifest), self.pins)
        for key, value in (("version", "0.4.0"), ("sequence", self.pins["sequence"] - 1), ("installer", {**manifest["installer"], "sha256": "b" * 64})):
            with self.assertRaisesRegex(first_use.CheckFailed, "reviewed_manifest"):
                first_use.validate_manifest(envelope({**manifest, key: value}), self.pins)

    def test_child_environment_excludes_credentials_and_untrusted_node_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = first_use.child_environment({"SYSTEMROOT": r"C:\Windows", "PATH": "fixture", "GITHUB_TOKEN": "private",
                "GOOGLE_API_KEY": "private", "NODE_OPTIONS": "--require unreviewed", "NPM_CONFIG_USERCONFIG": "private"}, Path(directory))
            self.assertEqual(environment["SYSTEMROOT"], r"C:\Windows")
            for key in ("GITHUB_TOKEN", "GOOGLE_API_KEY", "NODE_OPTIONS"):
                self.assertNotIn(key, environment)
            self.assertEqual(Path(environment["NPM_CONFIG_USERCONFIG"]).read_bytes(), b"")

    def test_existing_user_install_is_never_touched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(first_use, "registry_exists", return_value=False), patch.object(first_use, "registry_value", return_value=None):
                with self.assertRaisesRegex(first_use.CheckFailed, "fresh_profile"):
                    first_use.fresh_preflight(root, root / "state", root / "shortcut")
            with patch.object(first_use, "registry_exists", return_value=True), patch.object(first_use, "registry_value", return_value=None):
                with self.assertRaisesRegex(first_use.CheckFailed, "fresh_profile"):
                    first_use.fresh_preflight(root / "absent", root / "state", root / "shortcut")

    def state_fixture(self, state):
        (state / "worker").mkdir()
        live = {"status": "approval_required", "worker_running": False, "companion_version": "0.4.1", "startup_enabled": False,
                "validation_pending": False, "pid": 123, "observed_at": 10000, "pairing_code": "PRIVATE_FIXTURE"}
        pending = {"config": {"setup_only": True, "workspaces": {}, "cloud_task_id": None}, "setup_request_id": None,
                   "secret": "PRIVATE_FIXTURE", "paired": {"claim": "PRIVATE_FIXTURE"}}
        (state / "headless-status.json").write_text(json.dumps(live))
        (state / "worker/pairing.json").write_text(json.dumps(pending))
        return live, pending

    def test_pairing_requires_fresh_unapproved_management_only_no_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            live, pending = self.state_fixture(state)
            first_use.verify_pending(state, self.pins, now=10001)
            for key, value in (("setup_only", False), ("workspaces", {"private": {"path": "private"}}), ("cloud_task_id", "private")):
                changed = copy.deepcopy(pending)
                changed["config"][key] = value
                (state / "worker/pairing.json").write_text(json.dumps(changed))
                with self.assertRaisesRegex(first_use.CheckFailed, "management_only_pairing"):
                    first_use.verify_pending(state, self.pins, now=10001)
            (state / "worker/pairing.json").write_text(json.dumps(pending))
            for name in ("auth.json", "worker.json", "worker.sqlite3", "provider-tasks"):
                path = state / "worker" / name
                path.touch()
                with self.assertRaisesRegex(first_use.CheckFailed, "no_authorized_worker_or_tasks"):
                    first_use.verify_pending(state, self.pins, now=10001)
                path.unlink()
            with self.assertRaisesRegex(first_use.CheckFailed, "fresh_heartbeat"):
                first_use.verify_pending(state, self.pins, now=16000)

    def test_snapshots_detect_observed_owned_provider_and_nested_children(self):
        records = [{"pid": 3, "parent": 2, "name": "codex.exe"}, {"pid": 2, "parent": 1, "name": "node.exe"},
                   {"pid": 4, "parent": 999, "name": "other.exe"}]
        self.assertEqual(first_use.descendants(records, 1), {2, 3})
        observer = first_use.Observation({})
        observer.reader = Mock()
        observer.reader.is_alive.return_value = False
        observer.stopped.set()
        observer.records = [{"pid": 2, "parent": first_use.os.getpid(), "name": "node.exe"},
                            {"pid": 3, "parent": 2, "name": "codex.exe"}]
        report = {}
        with self.assertRaisesRegex(first_use.CheckFailed, "supplementary_observation_checks"):
            observer.verify(report)
        self.assertIs(report["no_provider_process_observed"], False)

    def test_snapshots_flag_owned_windows_and_listeners(self):
        for key, check in (("windows", "no_window_observed"), ("listeners", "no_listener_observed")):
            observer = first_use.Observation({})
            observer.reader = Mock()
            observer.reader.is_alive.return_value = False
            observer.stopped.set()
            observer.records = [{"pid": 2, "parent": first_use.os.getpid(), "name": "AstraAntigravity.exe"}]
            observer.records += [{"sample": True, "windows": [], "listeners": [], key: [2]}] * 3
            report = {}
            with self.assertRaisesRegex(first_use.CheckFailed, "supplementary_observation_checks"):
                observer.verify(report)
            self.assertIs(report[check], False)
            self.assertIn("no_provider_process_observed", report)
            self.assertIn("no_window_observed", report)
            self.assertIn("no_listener_observed", report)

    def test_cleanup_uses_stop_then_uninstall_and_never_status_after_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            commands = []
            (state / "headless-status.json").write_text('{"pid":123}')
            def run(args, _environment):
                commands.append(args[1])
                if args[1] == "--stop":
                    (state / "headless-status.json").write_text('{"status":"not_running"}')
                else:
                    (state / "maintenance-receipt.json").write_text('{"state":"succeeded","outcome":"removed"}')
            with patch.object(first_use, "verified_executable", return_value=True), patch.object(first_use, "captured", side_effect=run), \
                 patch.object(first_use, "wait_pid_exit", side_effect=lambda pid: commands.append("wait-pid-" + str(pid))), \
                 patch.object(first_use, "registry_exists", return_value=False), patch.object(first_use, "registry_value", return_value=None):
                first_use.cleanup(state / "absent-root", state, state / "absent-shortcut", self.pins, {})
            self.assertEqual(commands, ["--stop", "wait-pid-123", "--uninstall"])

    def test_main_never_echoes_arbitrary_exception_or_private_state(self):
        for error in (RuntimeError("token=PRIVATE_FIXTURE https://private.invalid/?claim=PRIVATE_FIXTURE"), first_use.CheckFailed("pins_complete")):
            output = io.StringIO()
            with patch.object(first_use, "verify", side_effect=error), contextlib.redirect_stdout(output):
                self.assertEqual(first_use.main(), 1)
            result = json.loads(output.getvalue())
            self.assertNotIn("PRIVATE_FIXTURE", output.getvalue())
            self.assertTrue(all(type(value) is bool for value in result.values()))

    def test_child_output_stays_private_and_overflow_is_fixed_error(self):
        for maximum in (1024, 4):
            process = Mock(stdout=io.BytesIO(b"PRIVATE_PAIRING_CODE"), stderr=io.BytesIO(b"PRIVATE_SECRET"), returncode=0)
            process.poll.return_value = 0
            output = io.StringIO()
            with patch.object(first_use.subprocess, "Popen", return_value=process), patch.object(first_use, "MAX_OUTPUT", maximum), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                if maximum == 1024:
                    self.assertEqual(first_use.captured(["fixture-command"], {}), b"PRIVATE_PAIRING_CODE")
                else:
                    with self.assertRaisesRegex(first_use.CheckFailed, "bounded_command_output"):
                        first_use.captured(["fixture-command"], {})
            self.assertEqual(output.getvalue(), "")

    def test_observer_start_failure_is_preserved_before_install_even_if_close_fails(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            temporary = Path(directory)
            node = temporary / "node.exe"
            node.touch()
            npx = temporary / "node_modules/npm/bin/npx-cli.js"
            npx.parent.mkdir(parents=True)
            npx.touch()
            observer = first_use.Observation({})
            observer.started.set()
            observer.api = SimpleNamespace(windows_ready=True, tcp_ready=False, udp_ready=False)
            observer.records = [{"name": "PRIVATE_FIXTURE", "PRIVATE_FIXTURE": True}]
            observer.start = Mock(side_effect=first_use.CheckFailed("observation_sample_ready"))
            observer.verify = Mock(side_effect=first_use.CheckFailed("expected_processes_only"))
            observer.close = Mock(side_effect=RuntimeError("PRIVATE_FIXTURE"))
            mocks = {"json_file": Mock(side_effect=[self.pins, {}]), "runner_environment_guard": Mock(), "runner_guard": Mock(),
                     "paths": Mock(return_value=(temporary / "install", temporary / "state", temporary / "shortcut")),
                     "fresh_preflight": Mock(), "download": Mock(return_value=b"fixture"), "validate_package": Mock(),
                     "validate_manifest": Mock(), "child_environment": Mock(return_value={"PATH": "fixture"}),
                     "captured": Mock(return_value=b"v24.0.0"), "Observation": Mock(return_value=observer), "cleanup": Mock()}
            for key, value in mocks.items():
                stack.enter_context(patch.object(first_use, key, value))
            stack.enter_context(patch.object(first_use.shutil, "which", return_value=str(node)))
            stack.enter_context(patch.dict(first_use.os.environ, {"RUNNER_TEMP": str(temporary), "GITHUB_EVENT_PATH": "fixture"}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(first_use.main(), 1)
            result = json.loads(output.getvalue())
            self.assertIs(result["observation_sample_ready"], False)
            self.assertIs(result["process_snapshots_used"], False)
            self.assertIs(result["process_start_trace_used"], False)
            self.assertIs(result["sampling_can_miss_short_lived_activity"], True)
            self.assertIs(result["windows_sample_ready"], True)
            self.assertIs(result["tcp_sample_ready"], False)
            self.assertIs(result["observation_sample_available"], False)
            self.assertIs(result["observation_closed"], False)
            self.assertNotIn("expected_processes_only", result)
            self.assertNotIn("PRIVATE_FIXTURE", output.getvalue())
            observer.verify.assert_not_called()
            mocks["cleanup"].assert_not_called()
            mocks["captured"].assert_called_once()  # node version only; no npx

    def test_observer_first_sample_timeout_has_a_specific_safe_check(self):
        observer = first_use.Observation()
        with patch.object(first_use.threading, "Thread") as thread, patch.object(observer.ready, "wait", return_value=False):
            with self.assertRaisesRegex(first_use.CheckFailed, "^observation_sample_ready$"):
                observer.start()
        thread.return_value.start.assert_called_once()

    def test_monitor_diagnostics_expose_only_fixed_boolean_status(self):
        observer = first_use.Observation({})
        observer.reader = Mock()
        observer.reader.is_alive.return_value = False
        observer.records = [{"PRIVATE_FIXTURE": "secret"}]
        with patch.object(first_use, "NativeWindowsSnapshots", side_effect=RuntimeError("PRIVATE_FIXTURE")):
            observer._read()
        report = {}
        observer.diagnostics(report)
        self.assertIs(report["monitor_started"], True)
        self.assertIs(report["monitor_read_failed"], True)
        self.assertIs(report["monitor_exited"], True)
        self.assertTrue(all(type(value) is bool for value in report.values()))
        self.assertNotIn("PRIVATE_FIXTURE", json.dumps(report))

    def test_snapshots_ignore_baseline_and_unrelated_providers(self):
        observer = first_use.Observation()
        observer.reader = Mock()
        observer.reader.is_alive.return_value = False
        observer.stopped.set()
        baseline = [{"pid": first_use.os.getpid(), "parent": 999, "name": "python.exe"},
                    {"pid": 999, "parent": 0, "name": "python.exe"}]
        snapshot = {"processes": baseline, "windows": {999}, "listeners": {999}}
        observer._capture(snapshot, baseline=True)
        later = {**snapshot, "processes": baseline + [{"pid": 123456, "parent": first_use.os.getpid(), "name": "node.exe"},
                                                     {"pid": 888, "parent": 999, "name": "codex.exe"}]}
        observer._capture(later)
        observer._capture(later)
        self.assertTrue(observer.has_sample())
        report = {}
        observer.verify(report)
        self.assertTrue(report["no_provider_process_observed"])
        self.assertTrue(report["no_window_observed"])
        self.assertTrue(report["no_listener_observed"])
        self.assertFalse(any(record.get("pid") == first_use.os.getpid() for record in observer.records))

    def test_snapshot_ancestry_keeps_an_observed_parent_after_it_exits(self):
        observer = first_use.Observation()
        observer._capture({"processes": [], "windows": set(), "listeners": set()}, baseline=True)
        observer._capture({"processes": [{"pid": 2, "parent": first_use.os.getpid(), "name": "node.exe"}], "windows": set(), "listeners": set()})
        observer._capture({"processes": [{"pid": 3, "parent": 2, "name": "AstraAntigravity.exe"}], "windows": set(), "listeners": set()})
        self.assertEqual(first_use.descendants(observer.records, first_use.os.getpid()), {2, 3})

    def test_observer_native_failure_wakes_start_and_late_failure_prevents_success(self):
        observer = first_use.Observation()
        with patch.object(first_use, "NativeWindowsSnapshots", side_effect=first_use.CheckFailed("native_tcp_snapshot")):
            with self.assertRaisesRegex(first_use.CheckFailed, "native_tcp_snapshot"):
                observer.start()
        with self.assertRaisesRegex(first_use.CheckFailed, "native_tcp_snapshot"):
            observer.close()
        observer = first_use.Observation()
        observer.reader = Mock()
        observer.reader.is_alive.return_value = False
        observer.records = [{"pid": 2, "parent": first_use.os.getpid(), "name": "node.exe"}]
        observer.records += [{"sample": True, "windows": [], "listeners": [], "running": []}] * 3
        # A snapshot can fail while close drains the final in-flight sample.
        def fail_on_join(**_kwargs):
            observer.failed, observer.failure_check = True, "native_udp_snapshot"
        observer.reader.join.side_effect = fail_on_join
        with self.assertRaisesRegex(first_use.CheckFailed, "native_udp_snapshot"):
            observer.close()

    def test_final_inflight_sample_is_verified_only_after_stop_and_join(self):
        observer = first_use.Observation()
        observer.reader = Mock()
        observer.reader.is_alive.return_value = False
        observer.records = [{"pid": 2, "parent": first_use.os.getpid(), "name": "node.exe"}]
        observer.records += [{"sample": True, "windows": [], "listeners": [], "running": []}] * 3
        def final_sample(**_kwargs):
            observer.records.append({"pid": 3, "parent": 2, "name": "codex.exe"})
        observer.reader.join.side_effect = final_sample
        with self.assertRaisesRegex(first_use.CheckFailed, "observation_healthy"):
            observer.verify({})
        observer.close()
        report = {}
        with self.assertRaisesRegex(first_use.CheckFailed, "supplementary_observation_checks"):
            observer.verify(report)
        self.assertIs(report["no_provider_process_observed"], False)

    def test_only_exact_pinned_installer_cache_name_is_allowed(self):
        digest = self.pins["installer_sha256"]
        for name, accepted in ((digest + ".exe", True), ("b" * 64 + ".exe", False), ("PRIVATE_FIXTURE.exe", False)):
            observer = first_use.Observation(installer_sha256=digest)
            observer.reader = Mock()
            observer.reader.is_alive.return_value = False
            observer.stopped.set()
            observer.records = [{"pid": 2, "parent": first_use.os.getpid(), "name": name}]
            observer.records += [{"sample": True, "windows": [], "listeners": [], "running": []}] * 3
            report = {}
            if accepted:
                observer.verify(report)
            else:
                with self.assertRaisesRegex(first_use.CheckFailed, "supplementary_observation_checks"):
                    observer.verify(report)
            self.assertIs(report["pinned_installer_name_observed"], accepted)
            self.assertIs(report["expected_processes_only"], accepted)
            self.assertIs(report["unknown_owned_executable_observed"], not accepted)
            self.assertIs(report["no_provider_process_observed"], True)
            self.assertIs(report["no_window_observed"], True)
            self.assertIs(report["no_listener_observed"], True)
            self.assertTrue(all(type(value) is bool for value in report.values()))
            self.assertNotIn("PRIVATE_FIXTURE", json.dumps(report))

    def test_native_layouts_and_owner_table_parsers(self):
        self.assertEqual(ctypes.sizeof(first_use.ProcessEntry32W), 568 if ctypes.sizeof(ctypes.c_void_p) == 8 else 556)
        self.assertEqual(first_use.ProcessEntry32W.name.offset, 44 if ctypes.sizeof(ctypes.c_void_p) == 8 else 36)
        for row_type, size, pid_offset in ((first_use.Tcp4Row, 24, 20), (first_use.Tcp6Row, 56, 52),
                                          (first_use.Udp4Row, 12, 8), (first_use.Udp6Row, 28, 24)):
            with self.subTest(row=row_type):
                self.assertEqual(ctypes.sizeof(row_type), size)
                self.assertEqual(row_type.pid.offset, pid_offset)
                row = row_type()
                row.pid = 123
                offset = first_use.table_offset(row_type)
                raw = (1).to_bytes(4, "little") + bytes(offset - 4) + bytes(row)
                self.assertEqual(first_use.table_owner_pids(raw, row_type), {123})
                for malformed in (raw[:offset - 1], raw[:-1], (10001).to_bytes(4, "little") + raw[4:]):
                    with self.assertRaisesRegex(first_use.CheckFailed, "native_table_shape"):
                        first_use.table_owner_pids(malformed, row_type)

    def test_native_table_growth_is_bounded_and_api_codes_fail_closed(self):
        row = first_use.Tcp6Row()
        row.pid = 42
        raw = (1).to_bytes(4, "little") + bytes(first_use.table_offset(type(row)) - 4) + bytes(row)
        calls = []
        def growing(buffer, size_pointer, order, family, table_class, reserved):
            calls.append((order, family, table_class, reserved))
            size = ctypes.cast(size_pointer, ctypes.POINTER(first_use.U32))
            if len(calls) == 1:
                size.contents.value = 4
                return 122
            size.contents.value = len(raw)
            if len(calls) == 2:
                return 122
            ctypes.memmove(buffer, raw, len(raw))
            return 0
        self.assertEqual(first_use.native_table(growing, 23, 3, type(row), "native_tcp_snapshot"), {42})
        self.assertEqual(calls, [(False, 23, 3, 0)] * 3)
        with self.assertRaisesRegex(first_use.CheckFailed, "native_udp_snapshot"):
            first_use.native_table(lambda *_: 5, 2, 1, first_use.Udp4Row, "native_udp_snapshot")
        def excessive(_buffer, size_pointer, *_args):
            ctypes.cast(size_pointer, ctypes.POINTER(first_use.U32)).contents.value = first_use.MAX_NATIVE_TABLE + 1
            return 122
        with self.assertRaisesRegex(first_use.CheckFailed, "native_table_limit"):
            first_use.native_table(excessive, 2, 1, first_use.Udp4Row, "native_udp_snapshot")

    def test_toolhelp_closes_handles_on_first_or_next_failure(self):
        native = object.__new__(first_use.NativeWindowsSnapshots)
        native.kernel = Mock()
        native.kernel.CreateToolhelp32Snapshot.return_value = 123
        native.kernel.Process32FirstW.return_value = 0
        with self.assertRaisesRegex(first_use.CheckFailed, "native_process_snapshot"):
            native.processes()
        native.kernel.CloseHandle.assert_called_once_with(123)
        native.kernel.reset_mock()
        native.kernel.Process32FirstW.return_value = 1
        native.kernel.Process32NextW.return_value = 0
        with patch.object(ctypes, "get_last_error", return_value=5, create=True):
            with self.assertRaisesRegex(first_use.CheckFailed, "native_process_snapshot"):
                native.processes()
        native.kernel.CloseHandle.assert_called_once_with(123)

    def test_window_callback_failure_is_private_and_destroyed_window_is_ignored(self):
        native = object.__new__(first_use.NativeWindowsSnapshots)
        native.window_callback = lambda callback: callback
        native.user = Mock()
        native.user.EnumWindows.side_effect = lambda callback, _parameter: callback(1, 0)
        native.user.IsWindowVisible.side_effect = RuntimeError("PRIVATE_FIXTURE")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(first_use.CheckFailed, "native_visible_windows"):
                native.visible_windows()
        self.assertEqual(output.getvalue(), "")
        native.user.IsWindowVisible.side_effect = None
        native.user.IsWindowVisible.return_value = 1
        native.user.GetWindowThreadProcessId.return_value = 0
        self.assertEqual(native.visible_windows(), set())

    def test_pairing_output_requires_exact_fresh_code_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            code = "0123456789abcdef01234567"
            (state / "headless-status.json").write_text(json.dumps({"pairing_code": code}))
            output = ("Installing the headless companion.\nIn ChatGPT, connect this computer using code: " + code + "\n").encode()
            first_use.verify_pairing_output(output, state)
            for incorrect in (b"The companion is installed. Return to Computers in ChatGPT to continue setup.\n",
                              output.replace(code.encode(), b"f" * 24), output + output):
                with self.assertRaisesRegex(first_use.CheckFailed, "^pairing_code_returned$"):
                    first_use.verify_pairing_output(incorrect, state)


if __name__ == "__main__":
    unittest.main()
