import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock


PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import worktree_coordinator
from worktree_coordinator import WorktreeCoordinator


class Record:
    def __init__(self, task_id, workspace):
        self.task_id = task_id
        self.workspace = workspace
        self.status = "completed"
        self.session_alive = True


class FakeBase:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.records = {"task-1": Record("task-1", str(Path(state_dir).parent / "wt"))}
        self.close_calls = []

    def get_task(self, task_id):
        return self.records.get(task_id)

    def cancel_task(self, task_id):
        self.close_calls.append(task_id)
        record = self.records[task_id]
        record.session_alive = False
        return {
            "task_id": task_id,
            "status": record.status,
            "cancelled": False,
            "session_closed": True,
        }


class FakeQuota:
    def __init__(self, state_dir):
        self.base = FakeBase(state_dir)
        self.cancel_calls = []

    def cancel(self, task_id):
        self.cancel_calls.append(task_id)
        return self.base.cancel_task(task_id)

    def result(self, task_id, max_chars):
        return {"task_id": task_id, "status": self.base.records[task_id].status, "response": "done"}


class FakeStore:
    def __init__(self, metadata, outcomes):
        self.metadata = dict(metadata)
        self.outcomes = list(outcomes)
        self.remove_calls = []

    def refresh(self, task_id, fallback_workspace=None):
        return dict(self.metadata)

    def load(self, task_id):
        return dict(self.metadata)

    def remove(self, task_id, force=False, delete_branch=False):
        self.remove_calls.append((task_id, force, delete_branch))
        if self.outcomes:
            return dict(self.outcomes.pop(0))
        return {
            "task_id": task_id,
            "cleaned": True,
            "branch_deleted": False,
            "remove_error": None,
            "branch_delete_error": None,
            "worktree": dict(self.metadata),
        }


class CleanupHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.metadata = {
            "task_id": "task-1",
            "isolation": "worktree",
            "source_workspace": str(self.root / "repo"),
            "source_repo_root": str(self.root / "repo"),
            "effective_workspace": str(self.root / "wt"),
            "worktree_root": str(self.root / "wt"),
            "worktree_exists": True,
            "branch": "astra/gemini/wt-test",
            "dirty": False,
        }
        self.quota = FakeQuota(self.root / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def _coordinator(self, outcomes):
        return WorktreeCoordinator(self.quota, FakeStore(self.metadata, outcomes))

    def test_completed_session_is_closed_before_worktree_removal(self):
        coordinator = self._coordinator([])
        removed = coordinator.cleanup("task-1", force=True)
        self.assertTrue(removed["cleaned"])
        self.assertEqual(self.quota.base.close_calls, ["task-1"])
        self.assertEqual(self.quota.cancel_calls, ["task-1"])
        self.assertEqual(self.quota.base.records["task-1"].status, "completed")

    def test_windows_permission_denied_is_retried_with_bounded_backoff(self):
        failure = {
            "task_id": "task-1",
            "cleaned": False,
            "branch_deleted": False,
            "remove_error": "error: failed to delete 'C:/Temp/wt': Permission denied",
            "branch_delete_error": None,
            "worktree": dict(self.metadata),
        }
        success = {
            "task_id": "task-1",
            "cleaned": True,
            "branch_deleted": False,
            "remove_error": None,
            "branch_delete_error": None,
            "worktree": dict(self.metadata),
        }
        coordinator = self._coordinator([failure, success])
        with mock.patch.object(worktree_coordinator, "_is_windows", return_value=True), mock.patch.object(
            worktree_coordinator.time, "sleep"
        ) as sleep:
            removed = coordinator.cleanup("task-1", force=True)

        self.assertTrue(removed["cleaned"])
        self.assertEqual(len(coordinator.store.remove_calls), 2)
        sleep.assert_called_once()

    def test_non_lock_git_error_is_not_retried(self):
        failure = {
            "task_id": "task-1",
            "cleaned": False,
            "branch_deleted": False,
            "remove_error": "fatal: not a valid worktree",
            "branch_delete_error": None,
            "worktree": dict(self.metadata),
        }
        coordinator = self._coordinator([failure])
        with mock.patch.object(worktree_coordinator, "_is_windows", return_value=True), mock.patch.object(
            worktree_coordinator.time, "sleep"
        ) as sleep:
            removed = coordinator.cleanup("task-1", force=True)

        self.assertFalse(removed["cleaned"])
        self.assertEqual(len(coordinator.store.remove_calls), 1)
        sleep.assert_not_called()

    def test_dirty_refusal_does_not_close_completed_session(self):
        metadata = dict(self.metadata)
        metadata["dirty"] = True
        store = FakeStore(metadata, [])
        coordinator = WorktreeCoordinator(self.quota, store)

        refused = coordinator.cleanup("task-1", force=False)

        self.assertFalse(refused["cleaned"])
        self.assertEqual(self.quota.base.close_calls, [])
        self.assertEqual(store.remove_calls, [])

    def test_cleanup_preserves_completed_result_state(self):
        coordinator = self._coordinator([])
        coordinator.cleanup("task-1", force=True)
        result = coordinator.result("task-1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["response"], "done")


if __name__ == "__main__":
    unittest.main()
