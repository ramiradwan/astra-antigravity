from types import SimpleNamespace
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import luna_gateway


PLAN = {
    "reasoning_requirement": "light",
    "coordination_brief": "Keep the implementation bounded.",
    "acceptance_focus": ["Run tests"],
    "risks": [],
}


class FakeBase:
    def get_task(self, task_id):
        return SimpleNamespace(status="completed", workspace="/isolated/worktree", model="gemini-3.8-flash-high", effort="high")
    def wait_for_update(self, task_id, after_sequence, timeout_seconds):
        return {
            "task_id": task_id,
            "status": "completed",
            "terminal": True,
            "timed_out": False,
            "sequence": 0,
            "activity": None,
        }


class FakeCoordinator:
    def __init__(self):
        self.base = FakeBase()
        self.cancel_calls = []
        self.delegate_calls = []
        self.steer_calls = []

    def delegate(self, task, workspace, isolation, quota_override=False, model=None):
        self.delegate_calls.append((task, workspace, isolation, quota_override, model))
        return {"task_id": "task-started", "status": "running", "current_turn": 1}

    def steer(self, task_id, instruction, quota_override=False):
        self.steer_calls.append((task_id, instruction, quota_override))
        return {"task_id": task_id, "status": "running", "current_turn": 2}

    def wait(self, task_id, timeout_seconds):
        return {"task_id": task_id, "status": "completed", "finished": True}

    def compact_result(self, task_id):
        return {
            "task_id": task_id,
            "status": "completed",
            "current_turn": 1,
            "implementation": {
                "summary": "implemented",
                "files_changed": [],
                "tests_run": [],
                "tests_passed": None,
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
    async def report_progress(self, progress, message):
        return None


class FakeApp:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False
        self.next_id = 1

    async def next_notification_or_message(self):
        return await self.read_async()

    def start(self):
        return None

    def request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self.sent.append({"id": request_id, "method": method, "params": params})
        return request_id

    async def response(self, request_id):
        if request_id == 1:
            return {"id": 1, "result": {}}
        if request_id == 2:
            return {"id": 2, "result": {"config": {}}}
        if request_id == 3:
            return {
                "id": 2,
                "result": {
                    "model": luna_gateway.LUNA_MODEL,
                    "thread": {"id": "thread-luna"},
                },
            }
        if request_id == 4:
            return {"id": 4, "result": {"turn": {"id": "turn-luna"}}}
        raise AssertionError(f"unexpected request id {request_id}")

    async def read_async(self):
        if not self.messages:
            raise luna_gateway.GatewayError("Codex App Server exited unexpectedly (code=1).")
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


def _completed_plan_message(plan=None):
    return [
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-luna", "turnId": "turn-luna",
                "item": {
                    "type": "agentMessage",
                    "text": json.dumps(plan if plan is not None else PLAN),
                }
            },
        },
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-luna", "turn": {"id": "turn-luna", "status": "completed"}},
        },
    ]


class LunaGatewayFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_reroute_during_planning_fails_closed_before_gemini_start(self):
        app = FakeApp(
            [
                {
                    "method": "model/rerouted",
                    "params": {
                        "fromModel": luna_gateway.LUNA_MODEL,
                        "toModel": "gpt-5.6-sol",
                    },
                }
            ]
        )
        with patch("luna_gateway.discover_codex", return_value="/codex"), patch(
            "luna_gateway._AppServerProcess", return_value=app
        ):
            with self.assertRaisesRegex(luna_gateway.GatewayError, "rerouted"):
                await luna_gateway._run_luna_plan(
                    "Plan this task", "/repo", mode="delegation"
                )
        self.assertTrue(app.closed)

    async def test_app_server_death_during_planning_fails_closed(self):
        app = FakeApp(
            [luna_gateway.GatewayError("Codex App Server exited unexpectedly (code=1).")]
        )
        with patch("luna_gateway.discover_codex", return_value="/codex"), patch(
            "luna_gateway._AppServerProcess", return_value=app
        ):
            with self.assertRaisesRegex(luna_gateway.GatewayError, "exited unexpectedly"):
                await luna_gateway._run_luna_plan(
                    "Plan this task", "/repo", mode="delegation"
                )
        self.assertTrue(app.closed)

    async def test_unexpected_tool_request_during_planning_is_rejected(self):
        app = FakeApp(
            [
                {
                    "id": 99,
                    "method": "item/tool/call",
                    "params": {"tool": "exec", "arguments": {}},
                }
            ]
        )
        with patch("luna_gateway.discover_codex", return_value="/codex"), patch(
            "luna_gateway._AppServerProcess", return_value=app
        ):
            with self.assertRaisesRegex(
                luna_gateway.GatewayError, "Unexpected tool/request during planning"
            ):
                await luna_gateway._run_luna_plan(
                    "Plan this task", "/repo", mode="delegation"
                )
        error_replies = [item for item in app.sent if item.get("id") == 99]
        self.assertTrue(error_replies)
        self.assertEqual(error_replies[-1]["error"]["code"], -32601)
        self.assertTrue(app.closed)

    async def test_completed_planning_turn_with_invalid_schema_fails_closed(self):
        app = FakeApp(_completed_plan_message({"coordination_brief": "partial"}))
        with patch("luna_gateway.discover_codex", return_value="/codex"), patch(
            "luna_gateway._AppServerProcess", return_value=app
        ):
            with self.assertRaisesRegex(luna_gateway.GatewayError, "omitted required fields"):
                await luna_gateway._run_luna_plan(
                    "Plan this task", "/repo", mode="delegation"
                )
        self.assertTrue(app.closed)

    async def test_delegate_await_failure_cancels_started_gemini_task(self):
        coord = FakeCoordinator()
        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coord), patch.object(
            luna_gateway, "_run_luna_plan", AsyncMock(return_value=dict(PLAN))
        ), patch.object(
            luna_gateway,
            "_await_terminal",
            AsyncMock(side_effect=RuntimeError("await failed")),
        ), patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}):
            with self.assertRaisesRegex(RuntimeError, "await failed"):
                await luna_gateway.delegate_via_luna(
                    "Implement feature", "/repo", FakeContext()
                , delivery="foreground")
        self.assertEqual(coord.cancel_calls, ["task-started"])

    async def test_continuation_await_failure_cancels_same_task(self):
        coord = FakeCoordinator()
        with patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), patch.object(luna_gateway, "coordinator", coord), patch.object(
            luna_gateway, "_run_luna_plan", AsyncMock(return_value=dict(PLAN))
        ), patch.object(
            luna_gateway,
            "_await_terminal",
            AsyncMock(side_effect=RuntimeError("await failed")),
        ), patch.object(luna_gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}):
            with self.assertRaisesRegex(RuntimeError, "await failed"):
                await luna_gateway.continue_via_luna(
                    "task-existing", "Fix boundary", FakeContext()
                , delivery="foreground")
        self.assertEqual(coord.cancel_calls, ["task-existing"])
        self.assertEqual(coord.steer_calls[0][0], "task-existing")

    def test_codex_override_must_point_to_file(self):
        with patch.dict(os.environ, {"CODEX_PATH": "/definitely/missing/codex"}, clear=False):
            with self.assertRaisesRegex(luna_gateway.GatewayError, "CODEX_PATH is set"):
                luna_gateway.discover_codex()


if __name__ == "__main__":
    unittest.main()
