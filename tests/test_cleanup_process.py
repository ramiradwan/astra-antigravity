import sys
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
FIXTURE = ROOT / "tests" / "fixtures" / "fake_agy_native_result.py"
sys.path.insert(0, str(PLUGIN_DIR))

import server
import worktree_coordinator
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
                "quota_delta": {"buckets": {}},
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


class CleanupProcessTests(unittest.TestCase):
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

        self.old_worktree_root = __import__("os").environ.get(
            "ASTRA_ANTIGRAVITY_WORKTREE_ROOT"
        )
        __import__("os").environ["ASTRA_ANTIGRAVITY_WORKTREE_ROOT"] = str(
            self.root / "worktrees"
        )

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
                "--sandbox",
            ]
            if workspace:
                cmd.extend(["--add-dir", str(workspace)])
            return cmd

        self.manager._base_command = fixture_base_command
        self.coordinator = AdvancedWorktreeCoordinator(DirectQuotaAdapter(self.manager))

    def tearDown(self):
        for proc in list(self.manager._session_processes.values()):
            try:
                if proc.stdin and not getattr(proc.stdin, "closed", False):
                    proc.stdin.close()
            except Exception:
                pass
        time.sleep(0.05)
        self.find_agy_patch.stop()
        env = __import__("os").environ
        if self.old_worktree_root is None:
            env.pop("ASTRA_ANTIGRAVITY_WORKTREE_ROOT", None)
        else:
            env["ASTRA_ANTIGRAVITY_WORKTREE_ROOT"] = self.old_worktree_root
        self.tmp.cleanup()

    def test_completed_hot_session_releases_then_retries_windows_lock(self):
        delegated = self.coordinator.delegate(
            "Implement a small change", str(self.repo), "worktree"
        )
        task_id = delegated["task_id"]
        self.assertTrue(self.coordinator.wait(task_id, 3)["finished"])
        self.assertTrue(self.manager.get_task(task_id).session_alive)
        self.assertEqual(self.manager.get_task(task_id).status, "completed")

        original_remove = self.coordinator.store.remove
        calls = []

        def transient_then_real(task_id_arg, force=False, delete_branch=False):
            calls.append((task_id_arg, force, delete_branch))
            if len(calls) == 1:
                return {
                    "task_id": task_id_arg,
                    "cleaned": False,
                    "branch_deleted": False,
                    "remove_error": "error: failed to delete worktree: Permission denied",
                    "branch_delete_error": None,
                    "worktree": self.coordinator.store.refresh(task_id_arg),
                }
            return original_remove(
                task_id_arg, force=force, delete_branch=delete_branch
            )

        with patch.object(
            self.coordinator.store, "remove", side_effect=transient_then_real
        ), patch.object(
            worktree_coordinator, "_is_windows", return_value=True
        ), patch.object(
            worktree_coordinator.time, "sleep", wraps=time.sleep
        ):
            cleaned = self.coordinator.cleanup(
                task_id, force=True, delete_branch=True
            )

        self.assertTrue(cleaned["cleaned"])
        self.assertTrue(cleaned["branch_deleted"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.manager.get_task(task_id).session_alive)
        self.assertEqual(self.manager.get_task(task_id).status, "completed")
        self.assertFalse(Path(delegated["effective_workspace"]).exists())
        self.assertEqual(self.coordinator.compact_result(task_id)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
