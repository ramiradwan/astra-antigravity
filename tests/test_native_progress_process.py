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
from test_antigravity_native_process import DirectQuotaAdapter, git


class NativeProgressProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.progress_gates = self.root / "progress-gates"
        self.progress_gates.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-q", "-m", "base")

        self.find_agy_patch = patch("server.find_agy", return_value=sys.executable)
        self.find_agy_patch.start()
        self.addCleanup(self.find_agy_patch.stop)
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
            if workspace:
                cmd.extend(["--add-dir", str(workspace)])
            return cmd

        self.manager._base_command = fixture_base_command
        self.coordinator = AdvancedWorktreeCoordinator(DirectQuotaAdapter(self.manager))

    def tearDown(self):
        # Release a producer even when an assertion failed before its next ack.
        # Finish only this manager's children/readers before removing their logs.
        (self.progress_gates / "release-all").touch()
        with self.manager._lock:
            processes = list(self.manager._session_processes.values())
            readers = list(self.manager._session_threads.values())
        for proc in processes:
            try:
                if proc.stdin and not getattr(proc.stdin, "closed", False):
                    proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        for proc in processes:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        deadline = time.monotonic() + 5
        for reader in readers:
            reader.join(timeout=max(0, deadline - time.monotonic()))
        alive = [reader.name for reader in readers if reader.is_alive()]
        if alive:
            # Keep the diagnostic directory rather than deleting underneath a
            # reader that could still be writing its final metadata.
            self.tmp._finalizer.detach()
            self.fail(f"Owned native fixture reader did not stop: {alive}")
        for proc in processes:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.tmp.cleanup()

    def acknowledge(self, sequence: int) -> None:
        (self.progress_gates / f"turn-1-sequence-{sequence}.ack").touch()

    def test_teardown_releases_a_fixture_waiting_for_consumer_ack(self):
        with patch.dict(os.environ, {
            "ASTRA_FAKE_AGY_PROGRESS_GATE_DIR": str(self.progress_gates),
            "ASTRA_FAKE_AGY_PROGRESS_DELAY": "0",
        }):
            delegated = self.coordinator.delegate(
                "Exercise fixture teardown while an acknowledgment is pending",
                str(self.repo), "shared",
            )
            first = self.manager.wait_for_update(delegated["task_id"], 0, 2)
            self.assertEqual(first["sequence"], 1)
            self.assertFalse(first["terminal"])
            # Deliberately leave the producer gated, as an earlier assertion
            # failure would. tearDown must release it and join its owned reader.

    def test_wait_for_update_exposes_only_four_meaningful_native_transitions(self):
        with patch.dict(os.environ, {
            "ASTRA_FAKE_AGY_PROGRESS_GATE_DIR": str(self.progress_gates),
            "ASTRA_FAKE_AGY_PROGRESS_DELAY": "0",
        }):
            delegated = self.coordinator.delegate(
                "Exercise native progress identity", str(self.repo), "shared"
            )
            task_id = delegated["task_id"]

            first = self.manager.wait_for_update(task_id, 0, 2)
            self.assertEqual(first["sequence"], 1)
            self.assertEqual(first["activity"], "pytest -q")
            self.assertEqual(first["tests"]["state"], "running")
            self.acknowledge(first["sequence"])

            second = self.manager.wait_for_update(task_id, first["sequence"], 2)
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(second["activity"], "pytest -q")
            self.assertEqual(second["tests"]["state"], "passed")
            self.acknowledge(second["sequence"])

            third = self.manager.wait_for_update(task_id, second["sequence"], 2)
            self.assertEqual(third["sequence"], 3)
            self.assertEqual(
                third["activity"], "Antigravity verification subagent running"
            )
            self.acknowledge(third["sequence"])

            fourth = self.manager.wait_for_update(task_id, third["sequence"], 2)
            self.assertEqual(fourth["sequence"], 4)
            self.assertEqual(
                fourth["activity"], "Antigravity verification subagent completed"
            )
            self.acknowledge(fourth["sequence"])

            terminal = self.manager.wait_for_update(task_id, fourth["sequence"], 2)
            self.assertTrue(terminal["terminal"])
            self.assertEqual(terminal["sequence"], 4)

        serialized = json.dumps(terminal)
        self.assertNotIn("Inspecting", serialized)
        self.assertNotIn("fixture-subagent-secret", serialized)
        self.assertNotIn("subagent.log", serialized)
        self.assertNotIn("fixture-secret/worktree", serialized)

        compact = self.coordinator.compact_result(task_id)
        self.assertEqual(compact["status"], "completed")
        self.assertEqual(compact["implementation"]["summary"], "native turn 1")


if __name__ == "__main__":
    unittest.main()
