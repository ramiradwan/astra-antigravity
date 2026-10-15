import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from worktree_coordinator import WorktreeCoordinator


class Record:
    def __init__(self, task_id, workspace):
        self.task_id = task_id
        self.workspace = str(Path(workspace).resolve())
        self.status = "running"
        self.current_turn_index = 1
        self.pid = 1000
        self.log_path = "log"
        self.conversation_id = "cid"
        self.session_alive = True
        self.error = None
        self.exit_code = None
        self.created_at = time.time()
        self.updated_at = time.time()
        self.queued_instruction = None


class FakeBase:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.records = {}
        self._session_processes = {}

    def get_task(self, task_id):
        return self.records.get(task_id)

    def read_progress(self, task_id, offset, limit):
        return {"task_id": task_id, "offset": offset, "limit": limit, "events": []}


class FakeQuota:
    def __init__(self, state_dir):
        self.base = FakeBase(state_dir)
        self.counter = 0
        self.delegate_calls = []
        self.block = False

    def delegate(self, task, workspace, quota_override=False):
        self.delegate_calls.append((task, str(Path(workspace).resolve())))
        if self.block:
            return {"status": "quota_blocked", "message": "low quota"}
        self.counter += 1
        tid = f"task-{self.counter}"
        r = Record(tid, workspace)
        self.base.records[tid] = r
        self.base._session_processes[tid] = object()
        return {
            "task_id": tid,
            "status": "running",
            "workspace": r.workspace,
            "pid": r.pid,
            "quota_before": {"available": True},
        }

    def wait(self, task_id, timeout):
        r = self.base.records[task_id]
        return {"task_id": task_id, "status": r.status, "finished": r.status == "completed", "current_turn": r.current_turn_index}

    def status(self, task_id):
        r = self.base.records[task_id]
        return {"task_id": task_id, "status": r.status, "workspace": r.workspace}

    def result(self, task_id, max_chars):
        r = self.base.records[task_id]
        return {"task_id": task_id, "status": r.status, "workspace": r.workspace, "response": "done"}

    def steer(self, task_id, instruction):
        return {"task_id": task_id, "status": "running", "instruction": instruction}

    def cancel(self, task_id):
        r = self.base.records[task_id]
        if r.status == "completed":
            r.session_alive = False
            return {"task_id": task_id, "status": "completed", "cancelled": False, "session_closed": True}
        r.status = "cancelled"
        r.session_alive = False
        return {"task_id": task_id, "status": "cancelled", "cancelled": True}

    def get_quota_snapshot(self, workspace=None):
        return {"available": True, "workspace": workspace}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "app.txt")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base_commit = git(self.repo, "rev-parse", "HEAD")
        self.quota = FakeQuota(self.root / "state")
        self.coordinator = WorktreeCoordinator(self.quota)
        self.old_root = os.environ.get("ASTRA_ANTIGRAVITY_WORKTREE_ROOT")
        os.environ["ASTRA_ANTIGRAVITY_WORKTREE_ROOT"] = str(self.root / "isolated")

    def tearDown(self):
        # Force-remove any linked worktrees before tempdir teardown.
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"], capture_output=True)
        if self.old_root is None:
            os.environ.pop("ASTRA_ANTIGRAVITY_WORKTREE_ROOT", None)
        else:
            os.environ["ASTRA_ANTIGRAVITY_WORKTREE_ROOT"] = self.old_root
        self.tmp.cleanup()

    def test_shared_mode_preserves_workspace(self):
        out = self.coordinator.delegate("x", str(self.repo), "shared")
        self.assertEqual(out["isolation"], "shared")
        self.assertEqual(Path(out["effective_workspace"]), self.repo.resolve())
        self.assertIsNone(out["worktree"])

    def test_two_parallel_worktrees_are_independent(self):
        one = self.coordinator.delegate("one", str(self.repo), "worktree")
        two = self.coordinator.delegate("two", str(self.repo), "worktree")
        p1 = Path(one["effective_workspace"])
        p2 = Path(two["effective_workspace"])
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.is_dir())
        self.assertTrue(p2.is_dir())
        self.assertEqual(one["worktree"]["base_commit"], self.base_commit)
        self.assertEqual(two["worktree"]["base_commit"], self.base_commit)
        self.assertTrue(one["worktree"]["branch"].startswith("astra/gemini/wt-"))
        self.assertTrue(two["worktree"]["branch"].startswith("astra/gemini/wt-"))
        self.assertNotEqual(one["worktree"]["branch"], two["worktree"]["branch"])

        (p1 / "app.txt").write_text("one\n", encoding="utf-8")
        (p2 / "app.txt").write_text("two\n", encoding="utf-8")
        self.assertEqual((self.repo / "app.txt").read_text(), "base\n")
        self.assertEqual((p1 / "app.txt").read_text(), "one\n")
        self.assertEqual((p2 / "app.txt").read_text(), "two\n")

        r1 = self.coordinator.result("task-1")
        self.assertTrue(r1["worktree"]["dirty"])
        self.assertIn("app.txt", r1["worktree"]["changed_files"])

    def test_subdirectory_workspace_maps_into_worktree(self):
        sub = self.repo / "pkg" / "src"
        sub.mkdir(parents=True)
        (sub / "x.txt").write_text("x\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "subdir")
        out = self.coordinator.delegate("sub", str(sub), "worktree")
        effective = Path(out["effective_workspace"])
        self.assertEqual(effective.relative_to(Path(out["worktree"]["worktree_root"])), Path("pkg/src"))
        self.assertTrue((effective / "x.txt").is_file())

    def test_dirty_source_is_rejected_by_default(self):
        (self.repo / "app.txt").write_text("dirty\n")
        with self.assertRaisesRegex(ValueError, "clean source repository"):
            self.coordinator.delegate("x", str(self.repo), "worktree")
        self.assertEqual(self.quota.delegate_calls, [])

    def test_quota_block_cleans_provisional_worktree_and_branch(self):
        self.quota.block = True
        out = self.coordinator.delegate("x", str(self.repo), "worktree")
        self.assertEqual(out["status"], "quota_blocked")
        self.assertIsNone(out["worktree"])
        listing = git(self.repo, "worktree", "list", "--porcelain")
        self.assertNotIn("pending-", listing)
        branches = git(self.repo, "branch", "--list", "astra/gemini/*")
        self.assertEqual(branches.strip(), "")

    def test_cleanup_refuses_dirty_worktree_without_force(self):
        out = self.coordinator.delegate("x", str(self.repo), "worktree")
        tid = out["task_id"]
        self.quota.base.records[tid].status = "completed"
        wt = Path(out["effective_workspace"])
        (wt / "app.txt").write_text("changed\n")
        refused = self.coordinator.cleanup(tid)
        self.assertFalse(refused["cleaned"])
        self.assertTrue(wt.exists())
        removed = self.coordinator.cleanup(tid, force=True)
        self.assertTrue(removed["cleaned"])
        self.assertFalse(wt.exists())
        self.assertFalse(removed["branch_deleted"])
        self.assertIn("astra/gemini/wt-", git(self.repo, "branch", "--list", "astra/gemini/*"))

    def test_cleanup_can_delete_clean_branch_explicitly(self):
        out = self.coordinator.delegate("x", str(self.repo), "worktree")
        tid = out["task_id"]
        self.quota.base.records[tid].status = "completed"
        self.assertTrue(self.quota.base.records[tid].session_alive)
        removed = self.coordinator.cleanup(tid, delete_branch=True)
        self.assertFalse(self.quota.base.records[tid].session_alive)
        self.assertTrue(removed["cleaned"])
        self.assertTrue(removed["branch_deleted"])
        self.assertEqual(git(self.repo, "branch", "--list", "astra/gemini/*"), "")

    def test_cleanup_rejects_active_task(self):
        out = self.coordinator.delegate("x", str(self.repo), "worktree")
        with self.assertRaisesRegex(ValueError, "while its Gemini turn is active"):
            self.coordinator.cleanup(out["task_id"])

    def test_invalid_isolation_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported isolation"):
            self.coordinator.delegate("x", str(self.repo), "container")


if __name__ == "__main__":
    unittest.main()
