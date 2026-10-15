from types import SimpleNamespace
import json
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
    "coordination_brief": "Implement only the requested boundary change.",
    "acceptance_focus": ["Run pytest -q", "Keep the diff scoped"],
    "risks": ["Preserve compatibility"],
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
                "sequence": 1,
                "activity": "Inspecting repository",
            }
        return {
            "task_id": task_id,
            "status": "completed",
            "terminal": True,
            "timed_out": False,
            "sequence": 1,
            "activity": "Inspecting repository",
        }


class FakeCoordinator:
    def __init__(self):
        self.base = FakeBase()
        self.delegate_calls = []
        self.steer_calls = []
        self.cancel_calls = []

    def delegate(self, task, workspace, isolation, quota_override=False, model=None,
                 task_type="implementation", required_capabilities=None):
        self.delegate_calls.append((task, workspace, isolation, quota_override, model))
        return {
            "task_id": "task-123",
            "status": "running",
            "current_turn": 1,
            "effective_workspace": "/isolated/worktree",
        }

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
                "commit_sha": None,
                "worktree": {"path": "/isolated/worktree"},
                "structured_report_valid": True,
            },
            "quota": {"available": True, "blocked": False},
        }

    def status(self, task_id):
        return {
            "task_id": task_id,
            "status": "completed",
            "effective_workspace": "/isolated/worktree",
        }

    def cancel(self, task_id):
        self.cancel_calls.append(task_id)
        return {"task_id": task_id, "status": "cancelled"}


class FakeContext:
    def __init__(self):
        self.events = []

    async def report_progress(self, progress, message):
        self.events.append((progress, message))


class LunaGatewayRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegation_plans_first_then_freezes_parent_lifecycle_inputs(self):
        coord = FakeCoordinator()
        ctx = FakeContext()
        planner = AsyncMock(return_value=dict(PLAN))

        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coord), patch.object(
            luna_gateway, "_run_luna_plan", planner
        ), patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}):
            result = await luna_gateway.delegate_via_luna(
                "Implement feature X",
                "/repo",
                ctx,
                isolation="worktree",
                quota_override=True,
                delivery="foreground",
            )

        planner.assert_awaited_once()
        planning_prompt, planning_workspace = planner.await_args.args[:2]
        self.assertIn("Implement feature X", planning_prompt)
        self.assertEqual(planning_workspace, "/repo")
        self.assertEqual(planner.await_args.kwargs["mode"], "delegation")

        self.assertEqual(len(coord.delegate_calls), 1)
        delegated_task, workspace, isolation, quota_override, model = coord.delegate_calls[0]
        self.assertTrue(delegated_task.startswith("Implement feature X"))
        self.assertIn(PLAN["coordination_brief"], delegated_task)
        self.assertIn("Run pytest -q", delegated_task)
        self.assertEqual(workspace, "/repo")
        self.assertEqual(isolation, "worktree")
        self.assertTrue(quota_override)
        self.assertEqual(model, "claude-sonnet-5-5")

        self.assertEqual(
            ctx.events,
            [
                (1, "Planning delegation"),
                (2, "Antigravity implementation started"),
                (3, "Antigravity implementation completed"),
            ],
        )
        self.assertEqual(result["task_id"], "task-123")
        self.assertEqual(result["implementation"]["summary"], "implemented")
        self.assertEqual(result["luna"]["model"], "gpt-6-luna")
        self.assertEqual(result["luna"]["reasoning_effort"], "low")
        self.assertEqual(result["luna"]["phase"], "delegation")
        self.assertEqual(result["luna"]["coordination"], PLAN)
        self.assertEqual(result["planner"]["model"], "gpt-6-luna")
        self.assertEqual(result["antigravity"]["requested_model"], "claude-sonnet-5-5")

    async def test_continuation_plans_from_authoritative_worktree_and_steers_exact_task(self):
        coord = FakeCoordinator()
        ctx = FakeContext()
        planner = AsyncMock(return_value=dict(PLAN))

        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coord), patch.object(
            luna_gateway, "_run_luna_plan", planner
        ), patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}):
            result = await luna_gateway.continue_via_luna(
                "task-existing",
                "Fix the failing boundary test",
                ctx,
                quota_override=False,
                delivery="foreground",
            )

        planning_prompt, planning_workspace = planner.await_args.args[:2]
        self.assertEqual(planning_workspace, "/isolated/worktree")
        self.assertEqual(planner.await_args.kwargs["mode"], "continuation")
        self.assertIn("task-existing", planning_prompt)
        self.assertIn("Fix the failing boundary test", planning_prompt)
        self.assertIn("implemented", planning_prompt)

        self.assertEqual(len(coord.steer_calls), 1)
        task_id, instruction, quota_override = coord.steer_calls[0]
        self.assertEqual(task_id, "task-existing")
        self.assertTrue(instruction.startswith("Fix the failing boundary test"))
        self.assertIn(PLAN["coordination_brief"], instruction)
        self.assertFalse(quota_override)
        self.assertEqual(result["task_id"], "task-existing")
        self.assertEqual(result["luna"]["phase"], "continuation")
        self.assertEqual(result["luna"]["model"], "gpt-6-luna")
        self.assertEqual(result["planner"]["model"], "gpt-6-luna")
        self.assertEqual(
            ctx.events,
            [
                (1, "Planning corrective continuation"),
                (2, "Antigravity correction started"),
                (3, "Antigravity correction completed"),
            ],
        )

    async def test_luna_planning_failure_prevents_gemini_start(self):
        coord = FakeCoordinator()
        planner = AsyncMock(side_effect=luna_gateway.GatewayError("planning failed"))
        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coord), patch.object(
            luna_gateway, "_run_luna_plan", planner
        ), patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}):
            with self.assertRaisesRegex(luna_gateway.GatewayError, "planning failed"):
                await luna_gateway.delegate_via_luna(
                    "Implement feature",
                    "/repo",
                    FakeContext(),
                    delivery="foreground",
                )
        self.assertEqual(coord.delegate_calls, [])

    async def test_public_inputs_are_validated_before_planning(self):
        coord = FakeCoordinator()
        planner = AsyncMock(return_value=dict(PLAN))
        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coord), patch.object(
            luna_gateway, "_run_luna_plan", planner
        ):
            with self.assertRaisesRegex(ValueError, "task must not be empty"):
                await luna_gateway.delegate_via_luna("   ", "/repo", FakeContext(), delivery="foreground")
            with self.assertRaisesRegex(ValueError, "Unsupported isolation mode"):
                await luna_gateway.delegate_via_luna(
                    "task", "/repo", FakeContext(), isolation="other"
                , delivery="foreground")
            with self.assertRaisesRegex(ValueError, "findings must not be empty"):
                await luna_gateway.continue_via_luna(
                    "task-existing", "   ", FakeContext()
                , delivery="foreground")
        planner.assert_not_awaited()

    def test_plan_parser_requires_schema_and_bounds_model_text(self):
        long_item = "x" * 900
        payload = {
            "reasoning_requirement": "light",
    "coordination_brief": "b" * 5000,
            "acceptance_focus": [long_item] * 20,
            "risks": [long_item] * 20,
        }
        with self.assertRaisesRegex(luna_gateway.GatewayError, "exceeded maximum"):
            luna_gateway._parse_luna_plan(json.dumps({**payload, "risks": [long_item] * 21}))
        parsed = luna_gateway._parse_luna_plan(json.dumps(payload))
        self.assertEqual(len(parsed["coordination_brief"]), 4000)
        self.assertEqual(len(parsed["acceptance_focus"]), 20)
        self.assertEqual(len(parsed["acceptance_focus"][0]), 500)
        self.assertEqual(len(parsed["risks"]), 20)

        with self.assertRaisesRegex(luna_gateway.GatewayError, "not valid JSON"):
            luna_gateway._parse_luna_plan("not-json")
        with self.assertRaisesRegex(luna_gateway.GatewayError, "omitted required fields"):
            luna_gateway._parse_luna_plan(
                json.dumps({"coordination_brief": "brief"})
            )
        with self.assertRaisesRegex(luna_gateway.GatewayError, "empty coordination_brief"):
            luna_gateway._parse_luna_plan(
                json.dumps(
                    {
                        "reasoning_requirement": "light",
    "coordination_brief": "   ",
                        "acceptance_focus": [],
                        "risks": [],
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
