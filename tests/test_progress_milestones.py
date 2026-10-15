from types import SimpleNamespace
import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import luna_gateway


PLAN = {
    "reasoning_requirement": "light",
    "coordination_brief": "Keep the implementation bounded.",
    "acceptance_focus": ["Run focused tests"],
    "risks": [],
}


class FakeBase:
    def get_task(self, task_id):
        return SimpleNamespace(status="completed", workspace="/isolated/worktree", model="gemini-3.8-flash-high", effort="high")
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
                "sequence": 7,
                "activity": "Inspecting repository",
            }
        if self.calls == 2:
            return {
                "task_id": task_id,
                "status": "running",
                "terminal": False,
                "timed_out": False,
                "sequence": 9,
                "activity": "Running pytest -q",
                "source_step_type": "tool", "source_tool_name": "run_command",
                "source_state": "ACTIVE", "tests": {"last_command": "pytest -q", "state": "running"},
            }
        return {
            "task_id": task_id,
            "status": "completed",
            "terminal": True,
            "timed_out": False,
            "sequence": 9,
            "activity": "Running pytest -q",
                "source_step_type": "tool", "source_tool_name": "run_command",
                "source_state": "ACTIVE", "tests": {"last_command": "pytest -q", "state": "running"},
        }


class FakeCoordinator:
    def __init__(self):
        self.base = FakeBase()
        self.delegate_calls = []
        self.steer_calls = []

    def delegate(self, task, workspace, isolation, quota_override=False, model=None):
        self.delegate_calls.append((task, workspace, isolation, quota_override))
        return {"task_id": "task-123", "status": "running", "current_turn": 1}

    def steer(self, task_id, instruction, quota_override=False):
        self.steer_calls.append((task_id, instruction, quota_override))
        return {"task_id": task_id, "status": "running", "current_turn": 2}

    def wait(self, task_id, timeout_seconds):
        return {"task_id": task_id, "status": "completed", "finished": True}

    def compact_result(self, task_id):
        return {
            "task_id": task_id,
            "status": "completed",
            "current_turn": 2,
            "implementation": {
                "summary": "implemented",
                "files_changed": ["example.py"],
                "tests_run": ["pytest -q"],
                "tests_passed": True,
                "remaining_work": [],
                "risks": [],
            },
            "quota": {"available": True},
        }

    def status(self, task_id):
        return {
            "task_id": task_id,
            "status": "completed",
            "effective_workspace": "/isolated/worktree",
        }

    def cancel(self, task_id):
        return {"task_id": task_id, "status": "cancelled"}


class FakeContext:
    def __init__(self):
        self.events = []

    async def report_progress(self, progress, message):
        self.events.append((progress, message))


class ProgressMilestoneTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegation_milestones_are_sparse_and_monotonic(self):
        coordinator = FakeCoordinator()
        ctx = FakeContext()
        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coordinator), patch.object(
            luna_gateway, "_run_luna_plan", AsyncMock(return_value=dict(PLAN))
        ):
            await luna_gateway.delegate_via_luna("Implement feature", "/repo", ctx, delivery="foreground")

        self.assertEqual(
            ctx.events,
            [
                (1, "Planning delegation"),
                (2, "Antigravity implementation started"),
                (3, "Tests running"),
                (4, "Antigravity implementation completed"),
            ],
        )

    async def test_continuation_milestones_keep_same_monotonic_stream(self):
        coordinator = FakeCoordinator()
        ctx = FakeContext()
        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coordinator), patch.object(
            luna_gateway, "_run_luna_plan", AsyncMock(return_value=dict(PLAN))
        ):
            await luna_gateway.continue_via_luna(
                "task-123", "Fix the verifier finding", ctx
            , delivery="foreground")

        self.assertEqual(
            ctx.events,
            [
                (1, "Planning corrective continuation"),
                (2, "Antigravity correction started"),
                (3, "Tests running"),
                (4, "Antigravity correction completed"),
            ],
        )

    async def test_progress_messages_do_not_expose_internal_identifiers(self):
        coordinator = FakeCoordinator()
        ctx = FakeContext()
        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coordinator), patch.object(
            luna_gateway, "_run_luna_plan", AsyncMock(return_value=dict(PLAN))
        ):
            await luna_gateway.delegate_via_luna("Implement feature", "/repo", ctx, delivery="foreground")

        rendered = "\n".join(message for _, message in ctx.events).lower()
        for forbidden in ("conversation_id", "log_path", "pid", "step_update", "quota"):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
