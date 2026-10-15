import sys
from pathlib import Path
import tempfile
import unittest


PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import quota_server
from worktree_coordinator import WorktreeCoordinator


class Record:
    def __init__(self, task_id: str, workspace: str):
        self.task_id = task_id
        self.workspace = workspace
        self.status = "completed"
        self.session_alive = True


class FakeBase:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.record = Record("task-1", str(self.state_dir.parent / "wt"))
        self.cancel_calls = []

    def get_task(self, task_id: str):
        return self.record if task_id == self.record.task_id else None

    def cancel_task(self, task_id: str) -> dict:
        self.cancel_calls.append(task_id)
        self.record.session_alive = False
        return {
            "task_id": task_id,
            "status": self.record.status,
            "cancelled": False,
            "session_closed": True,
        }


class FakeStore:
    def __init__(self, root: Path):
        self.metadata = {
            "task_id": "task-1",
            "isolation": "worktree",
            "source_workspace": str(root / "repo"),
            "source_repo_root": str(root / "repo"),
            "effective_workspace": str(root / "wt"),
            "worktree_root": str(root / "wt"),
            "worktree_exists": True,
            "branch": "astra/gemini/wt-test",
            "dirty": False,
        }
        self.remove_calls = []

    def refresh(self, task_id: str, fallback_workspace=None) -> dict:
        return dict(self.metadata)

    def load(self, task_id: str) -> dict:
        return dict(self.metadata)

    def remove(self, task_id: str, force=False, delete_branch=False) -> dict:
        self.remove_calls.append((task_id, force, delete_branch))
        return {
            "task_id": task_id,
            "cleaned": True,
            "branch_deleted": False,
            "remove_error": None,
            "branch_delete_error": None,
            "worktree": dict(self.metadata),
        }


class CleanupQuotaRaceTests(unittest.TestCase):
    def test_cleanup_clears_pending_followup_before_closing_completed_session(self):
        with tempfile.TemporaryDirectory() as tmp_raw:
            root = Path(tmp_raw)
            base = FakeBase(root / "state")
            quota = quota_server.QuotaCoordinator(base)
            quota._set_state("task-1", pending_instruction="queued verifier correction")
            store = FakeStore(root)
            coordinator = WorktreeCoordinator(quota, store)

            result = coordinator.cleanup("task-1", force=True)

            self.assertTrue(result["cleaned"])
            self.assertIsNone(quota._get_state("task-1").get("pending_instruction"))
            self.assertEqual(base.cancel_calls, ["task-1"])
            self.assertFalse(base.record.session_alive)
            self.assertEqual(base.record.status, "completed")
            self.assertEqual(store.remove_calls, [("task-1", True, False)])


if __name__ == "__main__":
    unittest.main()
