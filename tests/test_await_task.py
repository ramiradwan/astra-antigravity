import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from worktree_server import _await_task


class FakeContext:
    def __init__(self):
        self.progress = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append((progress, total, message))


class FakeBase:
    def __init__(self):
        self.calls = 0

    def wait_for_update(self, task_id, after_sequence, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            return {
                "task_id": task_id,
                "status": "running",
                "terminal": False,
                "timed_out": False,
                "sequence": 1,
                "activity": "pytest -q",
                "tests": {"last_command": "pytest -q", "state": "running", "passed": None},
            }
        if self.calls == 2:
            return {
                "task_id": task_id,
                "status": "running",
                "terminal": False,
                "timed_out": False,
                "sequence": 2,
                "activity": "pytest -q",
                "tests": {"last_command": "pytest -q", "state": "passed", "passed": True},
            }
        return {
            "task_id": task_id,
            "status": "completed",
            "terminal": True,
            "timed_out": False,
            "sequence": 2,
            "activity": "pytest -q",
            "tests": {"last_command": "pytest -q", "state": "passed", "passed": True},
        }


class FakeCoordinator:
    def __init__(self):
        self.base = FakeBase()
        self.wait_calls = 0

    def wait(self, task_id, timeout_seconds):
        self.wait_calls += 1
        return {"task_id": task_id, "status": "completed", "finished": True}

    def result(self, task_id):
        return {
            "task_id": task_id,
            "status": "completed",
            "implementation": {"summary": "done", "tests_passed": True},
        }


class AwaitTaskTests(unittest.TestCase):
    def test_single_await_streams_condition_events_then_returns_terminal_result(self):
        coordinator = FakeCoordinator()
        ctx = FakeContext()
        result = asyncio.run(
            _await_task(
                "task-1",
                ctx,
                coordinator_obj=coordinator,
                wait_slice_seconds=0.01,
            )
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["implementation"]["summary"], "done")
        self.assertEqual(coordinator.base.calls, 3)
        self.assertEqual(coordinator.wait_calls, 1)
        self.assertEqual([row[2] for row in ctx.progress], ["pytest -q", "pytest -q"])
        self.assertEqual([row[0] for row in ctx.progress], [1, 2])

    def test_timed_out_internal_heartbeat_does_not_emit_progress(self):
        class TimeoutThenDoneBase(FakeBase):
            def wait_for_update(self, task_id, after_sequence, timeout_seconds):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "task_id": task_id,
                        "status": "running",
                        "terminal": False,
                        "timed_out": True,
                        "sequence": 0,
                        "activity": None,
                        "tests": {"last_command": None, "state": None, "passed": None},
                    }
                return {
                    "task_id": task_id,
                    "status": "completed",
                    "terminal": True,
                    "timed_out": False,
                    "sequence": 0,
                    "activity": None,
                    "tests": {"last_command": None, "state": None, "passed": None},
                }

        coordinator = FakeCoordinator()
        coordinator.base = TimeoutThenDoneBase()
        ctx = FakeContext()
        result = asyncio.run(
            _await_task(
                "task-1",
                ctx,
                coordinator_obj=coordinator,
                wait_slice_seconds=0.01,
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(ctx.progress, [])


if __name__ == "__main__":
    unittest.main()
