"""Regression corpus derived from the September 2026 production-style Astra run.

These tests intentionally describe orchestration outcomes rather than individual
implementation helpers. They exist so the failure modes observed in that run
cannot silently return during later refactors.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import advanced_coordinator
import quota_server
from server import TaskManager, TaskRecord
from task_progress import ProgressState, reduce_antigravity_event


class RealRunProgressRegressionTests(unittest.TestCase):
    def test_noisy_real_run_strings_cannot_corrupt_structured_progress(self):
        state = ProgressState()
        events = [
            {
                "event": "step_update",
                "step_update": {
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": (
                        "I inspected .venv/Scripts/python.exe and remotes/origin/ws-7.3c4; "
                        "icons/icon128.png exists. Prior prose mentioned pytest failed."
                    ),
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "pytest -q"},
                    },
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "pytest -q"},
                        "output": "218 passed in 9.3s",
                    },
                },
            },
        ]
        for event in events:
            state = reduce_antigravity_event(state, event)

        data = state.to_dict()
        self.assertIsNone(data["current_file"])
        self.assertEqual(data["tests"]["last_command"], "pytest -q")
        self.assertEqual(data["tests"]["state"], "passed")
        self.assertTrue(data["tests"]["passed"])

    def test_unrelated_command_error_after_passing_tests_does_not_flip_test_result(self):
        state = ProgressState()
        for event in (
            {
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "pytest -q"},
                        "output": "218 passed",
                    },
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "state": "ERROR",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "dir missing-folder"},
                        "error": {"message": "The system cannot find the path specified"},
                    },
                },
            },
        ):
            state = reduce_antigravity_event(state, event)
        self.assertTrue(state.to_dict()["tests"]["passed"])


class _ResultBase:
    def __init__(self, state_dir: Path, full_response: str):
        self.state_dir = state_dir
        self.record = SimpleNamespace(
            task_id="task-real-run-result",
            status="completed",
            workspace=str(state_dir),
            current_turn_index=1,
            latest_response=full_response,
            error=None,
            conversation_id="cid-real-run",
            session_alive=False,
        )

    def get_task(self, task_id):
        return self.record if task_id == self.record.task_id else None

    def get_result(self, task_id, max_chars):
        response = self.record.latest_response
        shown = response if len(response) <= max_chars else response[:max_chars] + "... [truncated]"
        return {
            "task_id": task_id,
            "status": "completed",
            "response": shown,
            "error": None,
            "num_turns": 1,
            "quota_before": {"available": True, "min_remaining_fraction": 0.53},
            "quota_after": {"available": True, "min_remaining_fraction": 0.51},
            "quota_delta": {},
        }


class _QuotaFacade:
    def __init__(self, base):
        self.base = base

    def result(self, task_id, max_chars):
        return self.base.get_result(task_id, max_chars)


class _Store:
    def __init__(self, metadata):
        self.metadata = metadata

    def load(self, task_id):
        return dict(self.metadata)

    def refresh(self, task_id, fallback=None):
        return dict(self.metadata)

    def save(self, task_id, metadata):
        self.metadata = dict(metadata)
        return metadata


class RealRunResultRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _coordinator(self, response):
        base = _ResultBase(self.root, response)
        metadata = {
            "task_id": base.record.task_id,
            "isolation": "worktree",
            "source_workspace": str(self.root),
            "effective_workspace": str(self.root),
            "worktree_root": str(self.root),
            "worktree_exists": True,
            "branch": "astra/gemini/wt-real-run",
            "base_commit": "a" * 40,
            "head_commit": "b" * 40,
            "changed_files": ["release.json", "tests/test_release.py"],
            "diff_stat": "2 files changed",
            "commits_ahead": 1,
            "dirty": True,
        }
        coordinator = advanced_coordinator.AdvancedWorktreeCoordinator(
            quota_coordinator=_QuotaFacade(base), store=_Store(metadata)
        )
        coordinator._git_snapshot = lambda *args, **kwargs: {
            "repo_root": str(self.root),
            "base_commit": "a" * 40,
            "head_commit": "b" * 40,
            "branch": "astra/gemini/wt-real-run",
            "changed_files": ["release.json", "tests/test_release.py"],
            "diff_stat": "2 files changed",
            "commits_ahead": 1,
            "dirty": True,
        }
        return coordinator, base.record.task_id

    def test_handoff_after_huge_response_prefix_still_parses_and_compacts(self):
        report = {
            "summary": "Implemented the production acceptance workflow",
            "files_changed": ["release.json", "tests/test_release.py"],
            "tests_run": ["pytest -q"],
            "tests_passed": True,
            "remaining_work": ["Provider baseline still blocks release"],
            "risks": ["External provider baseline remains a material delta"],
            "commit_sha": "b" * 40,
            "worktree": {"path": str(self.root), "branch": "astra/gemini/wt-real-run"},
            "quota_impact": {
                "before": {"min_remaining_fraction": 0.53},
                "after": {"min_remaining_fraction": 0.51},
            },
        }
        response = (
            "x" * 50000
            + "\nASTRA_IMPLEMENTATION_RESULT\n"
            + json.dumps(report)
        )
        coordinator, task_id = self._coordinator(response)

        full = coordinator.result(task_id, max_chars=1000)
        self.assertTrue(full["implementation"]["structured_report_valid"])
        self.assertEqual(full["implementation"]["summary"], report["summary"])
        self.assertTrue(full["implementation"]["tests_passed"])

        compact = coordinator.compact_result(task_id)
        encoded = json.dumps(compact)
        self.assertLess(len(encoded), 12 * 1024)
        for forbidden in ("response", "log", "pid", "quota_before", "quota_after", "reported"):
            self.assertNotIn(forbidden, compact)
        self.assertEqual(compact["implementation"]["files_changed"], ["release.json", "tests/test_release.py"])


class RealRunLifecycleRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astra-real-run-life-")
        self.state = Path(self.tmp) / "state"
        self.workspace = Path(self.tmp) / "workspace"
        self.workspace.mkdir()
        self.manager = TaskManager(self.state)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _record(self, task_id: str, status: str):
        record = TaskRecord(
            task_id=task_id,
            workspace=str(self.workspace),
            status=status,
            created_at=time.time(),
            updated_at=time.time(),
            server_instance_id="test",
            log_path=str(self.state / f"{task_id}.log"),
            meta_path=str(self.state / f"{task_id}.json"),
            conversation_id="cid-real-run",
        )
        self.manager._tasks[task_id] = record
        return record

    def test_steering_cancelled_task_is_explicitly_rejected(self):
        self._record("task-cancelled-real-run", "cancelled")
        with self.assertRaisesRegex(ValueError, "status 'cancelled'"):
            self.manager.steer_task("task-cancelled-real-run", "continue")

    def test_terminal_cancelled_state_wakes_event_waiter_without_progress(self):
        self._record("task-cancelled-wait", "cancelled")
        update = self.manager.wait_for_update("task-cancelled-wait", 0, 0.1)
        self.assertTrue(update["terminal"])
        self.assertEqual(update["status"], "cancelled")
        self.assertFalse(update["timed_out"])


class _QuotaBase:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.record = SimpleNamespace(
            task_id="task-queued-real-run",
            status="running",
            workspace=str(state_dir),
            current_turn_index=1,
            conversation_id="cid-queued",
            error=None,
        )
        self.cancel_calls = 0

    def get_task(self, task_id):
        return self.record if task_id == self.record.task_id else None

    def wait_for_task(self, task_id, timeout):
        return {"task_id": task_id, "status": self.record.status, "finished": False}

    def cancel_task(self, task_id):
        self.cancel_calls += 1
        self.record.status = "cancelled"
        return {"task_id": task_id, "status": "cancelled", "cancelled": True}


class RealRunQuotaSteeringRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = _QuotaBase(Path(self.tmp.name))
        self.q = quota_server.QuotaCoordinator(self.base)
        self.q._start_monitor = lambda *args, **kwargs: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_followup_while_one_is_pending_has_deterministic_error(self):
        first = self.q.steer(self.base.record.task_id, "use configured F3 workflow")
        self.assertTrue(first["queued"])
        with self.assertRaisesRegex(ValueError, "quota-aware follow-up is already queued"):
            self.q.steer(self.base.record.task_id, "another correction")

    def test_cancel_clears_pending_followup(self):
        self.q.steer(self.base.record.task_id, "use configured F3 workflow")
        state_before = self.q._get_state(self.base.record.task_id)
        self.assertEqual(state_before["pending_instruction"], "use configured F3 workflow")
        cancelled = self.q.cancel(self.base.record.task_id)
        self.assertEqual(cancelled["status"], "cancelled")
        state_after = self.q._get_state(self.base.record.task_id)
        self.assertIsNone(state_after.get("pending_instruction"))


if __name__ == "__main__":
    unittest.main()
