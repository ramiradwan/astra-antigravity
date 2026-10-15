import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
FIXTURE = ROOT / "tests" / "fixtures" / "fake_agy_native_result.py"
sys.path.insert(0, str(PLUGIN_DIR))

import server
from advanced_coordinator import AdvancedWorktreeCoordinator
from server import TaskManager


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class DirectQuotaAdapter:
    def __init__(self, base):
        self.base = base

    @staticmethod
    def _quota(fraction):
        return {
            "available": True,
            "blocked": False,
            "warning": False,
            "min_remaining_fraction": fraction,
        }

    def delegate(self, task, workspace, quota_override=False):
        record = self.base.start_task(task, workspace)
        return {
            "task_id": record.task_id,
            "status": record.status,
            "workspace": record.workspace,
            "pid": record.pid,
            "quota_before": self._quota(0.8),
        }

    def wait(self, task_id, timeout):
        return self.base.wait_for_task(task_id, timeout)

    def result(self, task_id, max_chars):
        raw = self.base.get_result(task_id, max_chars)
        raw.update(
            {
                "quota_before": self._quota(0.8),
                "quota_after": self._quota(0.7),
                "quota_delta": {
                    "buckets": {"gemini": {"consumed_fraction": 0.1}}
                },
                "quota_blocked_next_turn": False,
                "quota_blocked_reason": None,
            }
        )
        return raw

    def steer(self, task_id, instruction, quota_override=False):
        return self.base.steer_task(task_id, instruction)

    def cancel(self, task_id):
        return self.base.cancel_task(task_id)

    def get_quota_snapshot(self, workspace=None):
        return self._quota(0.8)


class AntigravityNativeProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-q", "-m", "base")

        # Production start_task intentionally requires a discoverable Antigravity
        # binary before launching. The acceptance fixture replaces _base_command
        # with a Python subprocess, so satisfy only that discovery precondition
        # for the lifetime of this test without weakening production behavior.
        self.find_agy_patch = patch("server.find_agy", return_value=sys.executable)
        self.find_agy_patch.start()

        self.manager = TaskManager(self.root / "state")
        self.manager.initial_wait_seconds = 0.02
        self.manager.session_idle_ttl_seconds = 60

        def fixture_base_command(workspace=None, model=None, effort=None):
            cmd = [
                sys.executable,
                str(FIXTURE),
                "--model",
                model or server.DEFAULT_MODEL,
                "--effort",
                effort or server.DEFAULT_EFFORT,
                "--dangerously-skip-permissions",
                "--print-timeout",
                server.DEFAULT_TIMEOUT,
                "--output-format",
                "stream-json",
                "--json-schema",
                str(server.IMPLEMENTATION_SCHEMA_PATH),
            ]
            if server.antigravity_sandbox_enabled():
                cmd.append("--sandbox")
            if workspace:
                cmd.extend(["--add-dir", str(workspace)])
            return cmd

        self.manager._base_command = fixture_base_command
        self.quota = DirectQuotaAdapter(self.manager)
        self.coordinator = AdvancedWorktreeCoordinator(self.quota)

    def tearDown(self):
        for proc in list(self.manager._session_processes.values()):
            try:
                if proc.stdin and not getattr(proc.stdin, "closed", False):
                    proc.stdin.close()
            except Exception:
                pass
        time.sleep(0.05)
        self.find_agy_patch.stop()
        self.tmp.cleanup()

    def _wait_for_idle_close(self, task_id: str) -> None:
        deadline = time.monotonic() + 2
        while self.manager.get_task(task_id).session_alive and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.manager.get_task(task_id).session_alive)

    def test_persistent_session_returns_native_compact_result_for_each_turn(self):
        delegated = self.coordinator.delegate(
            "Implement through native protocol", str(self.repo), "shared"
        )
        task_id = delegated["task_id"]
        self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])

        first = self.coordinator.compact_result(task_id)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["implementation"]["summary"], "native turn 1")
        self.assertEqual(first["implementation"]["tests_run"], ["pytest -q"])
        self.assertTrue(first["implementation"]["tests_passed"])
        self.assertTrue(first["implementation"]["structured_report_valid"])
        self.assertIn("native_result.txt", first["implementation"]["files_changed"])
        self.assertLess(len(json.dumps(first)), 12000)

        progress = self.manager.progress_state(task_id)
        self.assertEqual(progress["tests"]["last_command"], "pytest -q")
        self.assertTrue(progress["tests"]["passed"])

        self.coordinator.steer(task_id, "Apply verifier correction")
        self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])
        second = self.coordinator.compact_result(task_id)
        self.assertEqual(second["implementation"]["summary"], "native turn 2")
        self.assertEqual(self.manager.structured_output(task_id)["summary"], "native turn 2")
        self.assertEqual(
            (self.repo / "native_result.txt").read_text(encoding="utf-8"),
            "turn 2\n",
        )

    def test_idle_session_resumes_with_native_structured_output(self):
        self.manager.session_idle_ttl_seconds = 0.05
        delegated = self.coordinator.delegate(
            "Implement then cool down", str(self.repo), "shared"
        )
        task_id = delegated["task_id"]
        self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])
        self._wait_for_idle_close(task_id)

        follow = self.coordinator.steer(task_id, "Resume after idle close")
        self.assertFalse(follow["session_reused"])
        self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])

        result = self.coordinator.compact_result(task_id)
        self.assertEqual(result["implementation"]["summary"], "native resumed")
        self.assertTrue(result["implementation"]["structured_report_valid"])
        self.assertEqual(self.manager.structured_output(task_id)["summary"], "native resumed")
        self.assertEqual(
            (self.repo / "native_result.txt").read_text(encoding="utf-8"),
            "resumed\n",
        )

    def test_default_sandbox_reaches_persistent_and_resumed_turns(self):
        self.manager.session_idle_ttl_seconds = 0.05
        invocation_log = self.root / "sandbox-invocations.log"
        with patch.dict(
            os.environ,
            {
                "ASTRA_FAKE_AGY_EXPECT_SANDBOX": "1",
                "ASTRA_FAKE_AGY_INVOCATION_LOG": str(invocation_log),
            },
            clear=False,
        ):
            delegated = self.coordinator.delegate(
                "Implement in sandbox", str(self.repo), "shared"
            )
            task_id = delegated["task_id"]
            self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])
            self._wait_for_idle_close(task_id)

            follow = self.coordinator.steer(task_id, "Resume in sandbox")
            self.assertFalse(follow["session_reused"])
            self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])

        self.assertEqual(
            invocation_log.read_text(encoding="utf-8").splitlines(),
            ["sandbox", "sandbox"],
        )

    def test_explicit_sandbox_opt_out_omits_flag(self):
        invocation_log = self.root / "sandbox-opt-out-invocations.log"
        with patch.dict(
            os.environ,
            {
                "ASTRA_ANTIGRAVITY_SANDBOX": "0",
                "ASTRA_FAKE_AGY_EXPECT_SANDBOX": "0",
                "ASTRA_FAKE_AGY_INVOCATION_LOG": str(invocation_log),
            },
            clear=False,
        ):
            delegated = self.coordinator.delegate(
                "Implement without sandbox by explicit policy", str(self.repo), "shared"
            )
            task_id = delegated["task_id"]
            self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])

        self.assertEqual(
            invocation_log.read_text(encoding="utf-8").splitlines(),
            ["unsandboxed"],
        )

    def test_sandbox_failure_does_not_retry_unsandboxed(self):
        invocation_log = self.root / "sandbox-failure-invocations.log"
        failed_record = None
        with patch.dict(
            os.environ,
            {
                "ASTRA_FAKE_AGY_EXPECT_SANDBOX": "1",
                "ASTRA_FAKE_AGY_FAIL_WHEN_SANDBOXED": "1",
                "ASTRA_FAKE_AGY_INVOCATION_LOG": str(invocation_log),
            },
            clear=False,
        ):
            try:
                failed_record = self.manager.start_task(
                    "This sandbox launch must fail", str(self.repo)
                )
            except RuntimeError:
                # Depending on reader/startup scheduling, startup failure may be
                # raised synchronously or published immediately afterward.
                pass

            if failed_record is not None:
                deadline = time.monotonic() + 2
                while (
                    failed_record.status not in server.TERMINAL_TURN_STATUSES
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                self.assertEqual(failed_record.status, "failed")

        self.assertEqual(
            invocation_log.read_text(encoding="utf-8").splitlines(),
            ["sandbox"],
        )


if __name__ == "__main__":
    unittest.main()
