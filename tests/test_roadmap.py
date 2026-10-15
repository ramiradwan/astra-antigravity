import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from advanced_coordinator import HANDOFF_MARKER, AdvancedWorktreeCoordinator


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class FakeBase:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.records = {}
        self._tasks = self.records
        self._session_processes = {}

    def get_task(self, task_id):
        return self.records.get(task_id)

    def read_progress(self, task_id, offset, limit):
        return {"task_id": task_id, "offset": offset, "limit": limit, "events": []}


class FakeQuota:
    def __init__(self, state_dir):
        self.base = FakeBase(state_dir)
        self.counter = 0
        self.prompts = []
        self.responses = {}

    def delegate(self, task, workspace, quota_override=False):
        self.prompts.append(task)
        self.counter += 1
        task_id = f"task-{self.counter}"
        now = time.time()
        record = SimpleNamespace(
            task_id=task_id,
            workspace=str(Path(workspace).resolve()),
            status="running",
            current_turn_index=1,
            pid=1000 + self.counter,
            log_path=str(self.base.state_dir / f"{task_id}.log"),
            conversation_id=f"cid-{self.counter}",
            session_alive=True,
            error=None,
            exit_code=None,
            created_at=now,
            updated_at=now,
            queued_instruction=None,
        )
        self.base.records[task_id] = record
        self.base._session_processes[task_id] = object()
        return {
            "task_id": task_id,
            "status": "running",
            "workspace": record.workspace,
            "pid": record.pid,
            "quota_before": {"available": True, "min_remaining_fraction": 0.8},
        }

    def result(self, task_id, max_chars):
        record = self.base.records[task_id]
        return {
            "task_id": task_id,
            "status": record.status,
            "workspace": record.workspace,
            "response": self.responses.get(task_id, "done"),
            "quota_before": {"available": True, "min_remaining_fraction": 0.8},
            "quota_after": {"available": True, "min_remaining_fraction": 0.7},
            "quota_delta": {"buckets": {"gemini": {"consumed_fraction": 0.1}}},
            "quota_blocked_next_turn": False,
            "quota_blocked_reason": None,
        }

    def status(self, task_id):
        record = self.base.records[task_id]
        return {"task_id": task_id, "status": record.status, "workspace": record.workspace}

    def wait(self, task_id, timeout):
        record = self.base.records[task_id]
        return {
            "task_id": task_id,
            "status": record.status,
            "finished": record.status == "completed",
            "current_turn": record.current_turn_index,
        }

    def steer(self, task_id, instruction):
        self.prompts.append(instruction)
        return {"task_id": task_id, "status": "running", "instruction": instruction}

    def cancel(self, task_id):
        self.base.records[task_id].status = "cancelled"
        return {"task_id": task_id, "status": "cancelled", "cancelled": True}

    def get_quota_snapshot(self, workspace=None):
        return {
            "available": True,
            "blocked": False,
            "warning": False,
            "min_remaining_fraction": 0.8,
        }


class RoadmapTests(unittest.TestCase):
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
        self.coordinator = AdvancedWorktreeCoordinator(self.quota)

    def tearDown(self):
        self.tmp.cleanup()

    def test_structured_result_uses_authoritative_git_and_quota(self):
        delegated = self.coordinator.delegate("Implement the change", str(self.repo), "shared")
        task_id = delegated["task_id"]
        self.assertNotIn(HANDOFF_MARKER, self.quota.prompts[0])
        self.assertIn("required structured result", self.quota.prompts[0])
        (self.repo / "app.txt").write_text("changed\n", encoding="utf-8")
        self.quota.responses[task_id] = (
            "finished\n"
            f"{HANDOFF_MARKER}\n"
            + json.dumps({
                "summary": "implemented",
                "files_changed": ["made-up.txt"],
                "tests_run": ["python -m unittest"],
                "tests_passed": True,
                "remaining_work": [],
                "risks": [],
                "commit_sha": "not-authoritative",
                "worktree": "/fake",
                "quota_impact": {"claimed": "value"},
            })
        )
        self.quota.base.records[task_id].status = "completed"
        result = self.coordinator.result(task_id)
        implementation = result["implementation"]
        self.assertTrue(implementation["structured_report_valid"])
        self.assertEqual(implementation["files_changed"], ["app.txt"])
        self.assertIsNone(implementation["commit_sha"])
        self.assertEqual(implementation["reported"]["commit_sha"], "not-authoritative")
        self.assertEqual(
            implementation["quota_impact"]["delta"]["buckets"]["gemini"]["consumed_fraction"],
            0.1,
        )

    def test_malformed_handoff_falls_back_gracefully(self):
        delegated = self.coordinator.delegate("Implement fallback", str(self.repo), "shared")
        task_id = delegated["task_id"]
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        self.quota.responses[task_id] = "Implementation finished but structured output broke {not-json"
        self.quota.base.records[task_id].status = "completed"
        implementation = self.coordinator.result(task_id)["implementation"]
        self.assertFalse(implementation["structured_report_valid"])
        self.assertIn("new.txt", implementation["files_changed"])
        self.assertTrue(any("structured" in risk.lower() for risk in implementation["risks"]))
        self.assertTrue(implementation["summary"])

    def test_status_includes_compact_progress_summary(self):
        delegated = self.coordinator.delegate("Implement progress", str(self.repo), "shared")
        task_id = delegated["task_id"]
        record = self.quota.base.records[task_id]
        Path(record.log_path).write_text(
            json.dumps({"event": "init", "conversation_id": "cid"}) + "\n"
            + json.dumps({
                "event": "step_update",
                "step_update": {"text_delta": "Updated src/app.py and pytest tests/test_app.py passed"},
            }) + "\n",
            encoding="utf-8",
        )
        status = self.coordinator.status(task_id)
        summary = status["progress"]
        self.assertEqual(summary["phase"], "working")
        self.assertEqual(summary["current_file"], "tests/test_app.py")
        self.assertTrue(summary["tests"]["passed"])
        self.assertIn("Updated", summary["last_change"])

    def test_recommend_delegation_is_advisory_and_prefers_worktree(self):
        health = {
            "ready": True,
            "workspace": {"git_repository": True, "worktree_eligible": True},
            "quota": {"available": True, "blocked": False, "warning": False},
        }
        task = "Implement a new API endpoint with validation, persistence, tests, and update callers. " * 8
        with patch.object(self.coordinator, "check_health", return_value=health):
            recommendation = self.coordinator.recommend_delegation(task, str(self.repo))
        self.assertEqual(recommendation["recommendation"], "delegate")
        self.assertEqual(recommendation["suggested_isolation"], "worktree")
        self.assertTrue(recommendation["task_assessment"]["implementation_heavy"])

    def test_steer_reapplies_native_result_instruction(self):
        delegated = self.coordinator.delegate("Implement first", str(self.repo), "shared")
        self.coordinator.steer(delegated["task_id"], "Fix verifier findings")
        self.assertNotIn(HANDOFF_MARKER, self.quota.prompts[-1])
        self.assertIn("required structured result", self.quota.prompts[-1])


if __name__ == "__main__":
    unittest.main()
