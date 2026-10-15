import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN))
import communication
import luna_gateway as gateway
import model_availability
import quota_server
from recovery import inspect_saved_tasks

PLAN = {"reasoning_requirement": "light", "coordination_brief": "Keep scope", "acceptance_focus": [], "risks": []}


class Context:
    def __init__(self):
        self.events = []

    async def report_progress(self, progress, message):
        self.events.append((progress, message))


class CommunicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_tool_error_and_subagent_metadata_are_safe(self):
        ctx = Context()
        emitter = gateway._ProgressEmitter(ctx)
        await emitter.forward(1, {"source_step_type": "tool", "source_state": "DONE",
                                  "source_error": True, "activity": "SECRET raw traceback"})
        await emitter.forward(2, {"source_step_type": "agent_response", "subagent_state": "ACTIVE",
                                  "activity": "SECRET transcript conversation_id"})
        await emitter.close()
        self.assertEqual([text for _, text in ctx.events],
                         ["Antigravity tool reported a blocker", "Antigravity subagent activity running"])

    async def test_noise_and_duplicate_failures_are_bounded_and_not_prose(self):
        ctx = Context()
        emitter = gateway._ProgressEmitter(ctx)
        for index in range(2000):
            await emitter.forward(index, {"source_step_type": "agent_response",
                                         "activity": "error completed PID transcript"})
            await emitter.forward(index, {"source_step_type": "tool", "source_tool_name": "run_command",
                                         "source_state": "DONE",
                                         "tests": {"last_command": "pytest", "state": "failed"}})
        await emitter.emit("Antigravity implementation failed")
        await emitter.close()
        self.assertEqual([message for _, message in ctx.events],
                         ["Tests failed", "Antigravity implementation failed"])
        self.assertTrue(all(len(message) <= 160 for _, message in ctx.events))

    async def test_many_distinct_events_have_a_total_budget(self):
        ctx = Context()
        emitter = gateway._ProgressEmitter(ctx)
        with patch.object(communication.time, "monotonic", side_effect=range(0, 10000, 3)):
            for index in range(1000):
                await emitter.forward(index, {"source_step_type": "tool",
                                             "source_tool_name": "write_to_file",
                                             "source_state": "DONE",
                                             "current_file": f"src/{index}.py"})
        await emitter.emit("Antigravity implementation completed")
        await emitter.close()
        self.assertLessEqual(len(ctx.events), 32)
        self.assertLessEqual(len(emitter.seen), 64)
        self.assertEqual(ctx.events[-1][1], "Antigravity implementation completed")
        self.assertEqual([seq for seq, _ in ctx.events], list(range(1, len(ctx.events) + 1)))

    async def test_no_token_means_no_notifications_or_timer(self):
        ctx = Context()
        ctx.request_context = SimpleNamespace(meta=None)
        emitter = gateway._ProgressEmitter(ctx)
        await emitter.emit("Planning delegation")
        await emitter.forward(1, {"source_step_type": "tool", "source_state": "ERROR"})
        self.assertEqual(ctx.events, [])
        self.assertIsNone(emitter.flush_task)

    async def test_quiet_burst_flushes_only_latest_and_closes(self):
        ctx = Context()
        emitter = gateway._ProgressEmitter(ctx)
        clock = [0.0]
        sleeps = asyncio.Queue()

        async def controlled_sleep(delay):
            release = asyncio.get_running_loop().create_future()
            sleeps.put_nowait((delay, release))
            await release

        with patch.object(communication, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(communication.asyncio, "sleep", controlled_sleep):
            await emitter.emit("Updated a.py", lifecycle=False)
            await emitter.emit("Updated b.py", lifecycle=False)
            await emitter.emit("Updated c.py", lifecycle=False)
            delay, release = await asyncio.wait_for(sleeps.get(), 1)
            self.assertEqual(delay, communication.ORDINARY_PROGRESS_INTERVAL_SECONDS)
            clock[0] = delay
            release.set_result(None)
            await emitter.flush_task
        await emitter.close()
        self.assertEqual([message for _, message in ctx.events], ["Updated a.py", "Updated c.py"])
        self.assertTrue(emitter.closed)

    async def test_overdue_flush_cannot_follow_a_newer_immediate_update(self):
        ctx = Context()
        emitter = gateway._ProgressEmitter(ctx)
        clock = [0.0]
        sleeps = asyncio.Queue()

        async def controlled_sleep(delay):
            release = asyncio.get_running_loop().create_future()
            sleeps.put_nowait((delay, release))
            await release

        with patch.object(communication, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(communication.asyncio, "sleep", controlled_sleep):
            await emitter.emit("Updated a.py", lifecycle=False)
            await emitter.emit("Updated b.py", lifecycle=False)
            _, release = await asyncio.wait_for(sleeps.get(), 1)
            # Simulate a busy host: the ordinary interval elapsed before the
            # scheduled flush got CPU time, while the latest update arrived.
            clock[0] = communication.ORDINARY_PROGRESS_INTERVAL_SECONDS + 1
            await emitter.emit("Updated c.py", lifecycle=False)
            release.set_result(None)
            await emitter.flush_task
        await emitter.close()
        self.assertEqual([message for _, message in ctx.events], ["Updated a.py", "Updated c.py"])

    async def test_old_timer_respects_interval_after_a_new_immediate_update(self):
        ctx = Context()
        emitter = gateway._ProgressEmitter(ctx)
        clock = [0.0]
        sleeps = asyncio.Queue()

        async def controlled_sleep(delay):
            release = asyncio.get_running_loop().create_future()
            sleeps.put_nowait((delay, release))
            await release

        with patch.object(communication, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(communication.asyncio, "sleep", controlled_sleep):
            await emitter.emit("Updated a.py", lifecycle=False)
            await emitter.emit("Updated b.py", lifecycle=False)
            _, old_release = await asyncio.wait_for(sleeps.get(), 1)
            clock[0] = communication.ORDINARY_PROGRESS_INTERVAL_SECONDS + 1
            await emitter.emit("Updated c.py", lifecycle=False)
            await emitter.emit("Updated d.py", lifecycle=False)
            old_release.set_result(None)
            delay, release = await asyncio.wait_for(sleeps.get(), 1)
            self.assertEqual(delay, communication.ORDINARY_PROGRESS_INTERVAL_SECONDS)
            self.assertEqual([message for _, message in ctx.events], ["Updated a.py", "Updated c.py"])
            clock[0] += delay
            release.set_result(None)
            await emitter.flush_task
        await emitter.close()
        self.assertEqual([message for _, message in ctx.events], ["Updated a.py", "Updated c.py", "Updated d.py"])


class ReportTests(unittest.TestCase):
    def test_unavailable_json_and_text_rows_are_not_advertisements(self):
        self.assertEqual(model_availability.parse_model_slugs(
            '[{"id":"gemini-4-argon-high","available":false}]'), set())
        self.assertEqual(model_availability.parse_model_slugs(
            'gemini-4-argon-high unavailable'), set())
    def test_large_unicode_blockers_and_both_aliases_fit_wire_budget(self):
        terminal = {"task_id": "task-large", "status": "failed", "current_turn": 1,
                    "error": "failure" * 10000, "quota": {},
                    "implementation": {"summary": "summary" * 10000,
                        "tests_passed": False, "files_changed": ["file" * 1000] * 200,
                        "tests_run": ["pytest" * 1000] * 200,
                        "remaining_work": ["阻止" * 10000] * 200,
                        "risks": ["risk" * 10000] * 200,
                        "worktree": {"path": "/repo", "base_commit": "a" * 40}}}
        plan = {"coordination_brief": "plan" * 10000,
                "acceptance_focus": ["focus" * 1000] * 200, "risks": ["risk" * 1000] * 200}
        result = gateway._result_with_luna(terminal, plan, phase="delegation")
        self.assertLessEqual(len(json.dumps(result, separators=(",", ":"))), 8000)
        self.assertEqual(result["planner"], result["luna"])
        self.assertFalse(result["implementation"]["tests_passed"])
        self.assertTrue(result["implementation"]["remaining_work"])
        self.assertTrue(result["error"])
        self.assertTrue(result["truncation"])
        self.assertEqual(result["implementation"]["worktree"]["base_commit"], "a" * 40)

    def test_no_false_positive_from_prose_model_mentions(self):
        slugs = model_availability.parse_model_slugs(
            "Requested gemini-4-argon-high is unavailable\n"
            "gemini-3.8-flash-high\tGemini Flash\n")
        self.assertEqual(slugs, {"gemini-3.8-flash-high"})

    def test_quota_excludes_other_version_and_unrelated_singleton(self):
        buckets = {"gemini-3.8-flash-5h": {"remaining_fraction": 0.01},
                   "gemini-4-argon-5h": {"remaining_fraction": 0.8},
                   "gemini-5h": {"remaining_fraction": 0.5},
                   "claude-5h": {"remaining_fraction": 0.001}}
        self.assertEqual(set(quota_server._relevant_buckets(buckets)),
                         {"gemini-4-argon-5h", "gemini-5h"})
        self.assertEqual(quota_server._relevant_buckets({"other": {"remaining_fraction": 1}}), {})


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediately_completed_start_is_still_owned_and_reported(self):
        from test_luna_gateway_runtime import FakeCoordinator
        coord = FakeCoordinator()
        coord.delegate = lambda *a, **k: {"task_id": "task-fast", "status": "completed", "current_turn": 1}
        with patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), \
             patch.object(gateway, "_run_luna_plan", AsyncMock(return_value=PLAN)):
            result = await gateway.delegate_via_luna("Implement", "/repo", Context(), delivery="foreground")
        self.assertEqual(result["task_id"], "task-fast")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(coord.cancel_calls, [])

    async def test_cancel_during_threaded_start_stops_exact_late_task(self):
        entered, release = threading.Event(), threading.Event()
        cancelled = []
        record = SimpleNamespace(status="running", workspace="/repo")
        def delegate(*args, **kwargs):
            entered.set()
            release.wait(1)
            return {"task_id": "task-owned", "status": "running"}
        def cancel(task_id):
            cancelled.append(task_id)
            record.status = "cancelled"
        coord = SimpleNamespace(delegate=delegate, cancel=cancel,
                                base=SimpleNamespace(get_task=lambda tid: record))
        with patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), \
             patch.object(gateway, "_run_luna_plan", AsyncMock(return_value=PLAN)):
            call = asyncio.create_task(gateway.delegate_via_luna("Implement", "/repo", Context(), delivery="foreground"))
            await asyncio.to_thread(entered.wait, 1)
            call.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await call
        self.assertEqual(set(cancelled), {"task-owned"})
        self.assertEqual(record.status, "cancelled")

    async def test_whole_deadline_covers_planning_before_any_start(self):
        coord = SimpleNamespace(delegate=lambda *a, **k: self.fail("unexpected start"))
        async def stalled(*args, **kwargs):
            await asyncio.sleep(1)
        with patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), \
             patch.object(gateway, "_run_luna_plan", stalled), \
             patch.object(gateway, "LUNA_GATEWAY_TIMEOUT_SECONDS", 0.02):
            with self.assertRaises(asyncio.TimeoutError):
                await gateway.delegate_via_luna("Implement", "/repo", Context(), delivery="foreground")

    def test_blocked_correction_cannot_reuse_previous_success(self):
        with self.assertRaises(gateway.GatewayError):
            gateway._compact_started({"status": "quota_blocked", "message": "Low quota"},
                                     "task-existing")


class RecoveryTests(unittest.TestCase):
    def test_linked_worktree_identity_newest_order_and_orphan_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo, linked, state = root / "repo", root / "linked", root / "state"
            state.mkdir()
            def git(*args):
                subprocess.run(["git", *map(str, args)], check=True, capture_output=True)
            git("init", "-q", repo)
            git("-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "--allow-empty", "-qm", "base")
            git("-C", repo, "worktree", "add", "--detach", linked)
            for index in range(25):
                (state / f"task-{index}.json").write_text(json.dumps({
                    "task_id": f"task-{index}", "workspace": str(linked), "updated_at": index,
                    "status": "running" if index == 24 else "completed",
                    "server_instance_id": "old-server", "current_turn_index": 1,
                    "turns": [{"turn_index": 1, "structured_output": {
                        "summary": "Evidence", "tests_run": [], "tests_passed": False,
                        "remaining_work": ["Fix test"], "risks": []}}]}))
            before = {path.name: path.read_bytes() for path in state.iterdir()}
            coord = SimpleNamespace(base=SimpleNamespace(state_dir=state, _session_processes={}))
            result = inspect_saved_tasks(coord, str(repo))
            self.assertEqual(result["tasks"][0]["task_id"], "task-24")
            self.assertEqual(result["tasks"][0]["status"], "unknown")
            self.assertFalse(result["tasks"][0]["correction_supported"])
            self.assertEqual(result["total_matching"], 25)
            self.assertLessEqual(len(result["tasks"]), 20)
            self.assertEqual(len(result["tasks"]) + result["tasks_truncated_count"], 25)
            self.assertLessEqual(communication.json_size(result), 8000)
            self.assertEqual(before, {path.name: path.read_bytes() for path in state.iterdir()})
            git("-C", repo, "worktree", "remove", linked)

    def test_read_only_current_turn_and_no_parent_directory_or_transcript_leak(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            workspace, unrelated, state = root / "ws", root / "other", root / "state"
            for folder in (workspace, unrelated, state):
                folder.mkdir()
            record = {"task_id": "task-a", "workspace": str(workspace), "status": "completed",
                      "current_turn_index": 2, "updated_at": 10, "latest_response": "PID SECRET transcript",
                      "turns": [{"turn_index": 1, "structured_output": {"summary": "OLD", "tests_passed": True}}]}
            path = state / "task-a.json"
            path.write_text(json.dumps(record))
            (state / "task-a.worktree.json").write_text(json.dumps({"task_id": "task-a"}))
            before = path.read_bytes()
            coord = SimpleNamespace(base=SimpleNamespace(state_dir=state, _session_processes={}))
            result = inspect_saved_tasks(coord, str(workspace), "task-a")
            self.assertEqual(len(result["tasks"]), 1)
            self.assertEqual(result["tasks"][0]["summary"], "")
            self.assertIsNone(result["tasks"][0]["tests_passed"])
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(inspect_saved_tasks(coord, str(root))["tasks"], [])


class WireAndPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_serializes_once_and_read_only_tools_are_annotated(self):
        from test_luna_gateway_runtime import FakeCoordinator
        coord = FakeCoordinator()
        coord.compact_result = lambda tid: {"task_id": tid, "status": "failed", "implementation": {
            "summary": "漢" * 10000, "tests_passed": False, "tests_run": [],
            "remaining_work": ["Fix failing test"], "risks": [], "structured_report_valid": True}}
        with patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}), \
             patch.object(gateway, "_run_luna_plan", AsyncMock(return_value=PLAN)):
            blocks = await gateway.mcp.call_tool("delegate_via_luna", {"task": "Implement", "workspace": "/repo", "delivery": "foreground"})
        self.assertIsInstance(blocks, list)
        self.assertEqual(len(blocks), 1)
        self.assertLessEqual(len(blocks[0].text), 8000)
        result = json.loads(blocks[0].text)
        self.assertIs(result["implementation"]["tests_passed"], False)
        tools = {tool.name: tool for tool in await gateway.mcp.list_tools()}
        self.assertIsNone(tools["delegate_via_luna"].outputSchema)
        for name in ("check_integration", "inspect_delegation"):
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
        for tool in tools.values():
            self.assertIsNotNone(tool.annotations)
            for field in ("readOnlyHint", "destructiveHint", "openWorldHint"):
                self.assertIs(type(getattr(tool.annotations, field)), bool, (tool.name, field))
        for name in ("delegate_via_luna", "continue_via_luna"):
            self.assertFalse(tools[name].annotations.readOnlyHint)
            self.assertTrue(tools[name].annotations.destructiveHint)
            self.assertTrue(tools[name].annotations.openWorldHint)
        self.assertTrue(tools["cleanup_task_worktree"].annotations.destructiveHint)
        self.assertFalse(tools["ack_delegation_events"].annotations.destructiveHint)
        self.assertFalse(tools["cancel_delegation"].annotations.openWorldHint)

    async def test_wrong_thread_missing_ids_and_early_notifications_cannot_win(self):
        from test_luna_gateway_failures import FakeApp, _completed_plan_message
        stale = _completed_plan_message({**PLAN, "coordination_brief": "STALE"})
        for item in stale:
            item["params"]["threadId"] = "other-thread"
        missing = {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "text": json.dumps({**PLAN, "coordination_brief": "MISSING IDS"})}}}
        app = FakeApp([*stale, missing, *_completed_plan_message(PLAN)])
        with patch.object(gateway, "discover_codex", return_value="codex"), \
             patch.object(gateway, "_AppServerProcess", return_value=app):
            self.assertEqual(await gateway._run_luna_plan("Plan", "/repo", mode="delegation"), PLAN)
        self.assertTrue(app.closed)


class PinnedLaunchTests(unittest.TestCase):
    def test_hot_and_cold_launches_preserve_legacy_and_explicit_models(self):
        import server
        with tempfile.TemporaryDirectory() as raw:
            manager = server.TaskManager(Path(raw))
            for model, effort in ((server.LEGACY_DEFAULT_MODEL, "high"), (server.DEFAULT_MODEL, "high"),
                                  ("gemini-4-argon-high", "low")):
                record = server.TaskRecord(task_id="task-pinned", workspace=raw, status="completed",
                    created_at=1, updated_at=1, server_instance_id="old", log_path="", meta_path="",
                    conversation_id="conversation", model=model, effort=effort)
                with patch.object(server, "find_agy", return_value="agy"), \
                     patch.object(server.subprocess, "Popen") as popen:
                    manager._launch_session(record)
                    manager._launch_fallback_turn(record, "Fix")
                for call in popen.call_args_list:
                    command = call.args[0]
                    self.assertEqual(command[command.index("--model") + 1], model)
                    self.assertEqual(command[command.index("--effort") + 1], effort)
