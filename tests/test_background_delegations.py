import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN))

from background_delegations import BackgroundDelegations, DelegationStore, MAX_DECISION_EVENTS
from communication import json_size
from app_server_delivery import AppServerDelivery
import luna_gateway as gateway
from test_luna_gateway_runtime import FakeCoordinator, FakeContext, PLAN


REPORT = {"task_id": "task-test", "status": "completed", "task_type": "implementation", "implementation": {
    "summary": "Implemented the change", "files_changed": ["code.py"],
    "tests_run": ["pytest -q"], "tests_passed": True, "remaining_work": [],
    "risks": [], "structured_report_valid": True,
    "worktree": {"path": "/isolated/tree", "branch": "astra/test"}}}


class BackgroundCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = BackgroundDelegations(self.tmp.name)
        self.gate = asyncio.Event()
        self.base = SimpleNamespace()

    async def asyncTearDown(self):
        self.gate.set()
        await self.manager.close()
        self.tmp.cleanup()

    def launch(self, execute=None, **options):
        async def run(progress):
            await progress.started({"task_id": "task-test"})
            await self.gate.wait()
            return dict(REPORT)
        return self.manager.start(workspace="/repo", phase="delegation", inputs={"task": "implement"},
                                  execute=execute or run, base=self.base, **options)[0]


class BackgroundTests(BackgroundCase):
    async def test_parent_continues_and_ordinary_progress_does_not_wake_receiver(self):
        entered = asyncio.Event()
        async def run(progress):
            await progress.started({"task_id": "task-test"})
            entered.set()
            for index in range(200):
                await progress.forward(index, {"activity": "Transcript must stay private", "source_state": "ERROR"})
                await progress.emit("Tests running", lifecycle=False)
            await self.gate.wait()
            return dict(REPORT)
        receipt = self.launch(run)
        receiving = asyncio.create_task(self.manager.receive(receipt["delegation_id"]))
        await entered.wait()
        # A separate parent operation completes before the worker is released.
        parent_file = Path(self.tmp.name) / "parent-independent-work.txt"
        parent_file.write_text("parent kept working")
        await asyncio.sleep(0)
        self.assertTrue(parent_file.is_file())
        self.assertFalse(receiving.done())
        self.gate.set()
        batch = await asyncio.wait_for(receiving, 2)
        self.assertEqual([event["kind"] for event in batch["events"]], ["completed"])
        self.assertTrue(batch["closed"])
        self.assertNotIn("Transcript", json.dumps(batch))

    async def test_disconnect_only_receiver_then_replay_deduplicate_and_acknowledge(self):
        receipt = self.launch()
        job = receipt["delegation_id"]
        pending = asyncio.create_task(self.manager.receive(job))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.manager.workers[job].done())
        body = {"status": "running", "summary": "Choose the migration policy"}
        event = self.manager.publish(job, "needs_decision", body)
        self.assertIsNone(self.manager.publish(job, "needs_decision", body))
        first = await self.manager.receive(job)
        self.assertEqual((await self.manager.receive(job))["events"], first["events"])
        self.manager.acknowledge(job, event["sequence"])
        final = asyncio.create_task(self.manager.receive(job))
        await asyncio.sleep(0)
        self.assertFalse(final.done())
        self.gate.set()
        batch = await asyncio.wait_for(final, 2)
        self.assertEqual(batch["events"][0]["sequence"], 2)
        self.assertEqual(batch["events"][0]["kind"], "completed")
        self.assertEqual(self.manager.result(job), REPORT)
        self.manager.acknowledge(job, 2)
        self.assertEqual((await self.manager.receive(job))["events"], [])
        # A host that lost its own context may explicitly replay archived data.
        self.assertEqual((await self.manager.receive(job, 0))["events"], first["events"])

    async def test_queued_cancellation_emits_final_event_without_launching(self):
        execute = AsyncMock(return_value=REPORT)
        receipt = self.launch(execute)
        await self.manager.cancel(receipt["delegation_id"])
        batch = await asyncio.wait_for(self.manager.receive(receipt["delegation_id"]), 2)
        self.assertEqual(batch["events"][0]["kind"], "cancelled")
        execute.assert_not_awaited()

    async def test_running_cancellation_is_confirmed_only_after_cleanup(self):
        cleaning = asyncio.Event()
        entered = asyncio.Event()
        async def execute(progress):
            await progress.started({"task_id": "task-test"})
            entered.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                cleaning.set()
                await asyncio.sleep(0.03)
                raise
        receipt = self.launch(execute)
        await entered.wait()
        requested = await self.manager.cancel(receipt["delegation_id"])
        self.assertEqual(requested["status"], "cancelling")
        batch = await asyncio.wait_for(self.manager.receive(receipt["delegation_id"]), 2)
        self.assertTrue(cleaning.is_set())
        self.assertEqual(batch["events"][0]["task_id"], "task-test")
        self.assertEqual(batch["events"][0]["kind"], "cancelled")

    async def test_launch_retries_reuse_receipt_without_inference_and_reject_changed_intent(self):
        execute = AsyncMock(return_value=REPORT)
        first = self.launch(execute, request_id="request-1")
        second = self.launch(execute, request_id="request-1")
        self.assertEqual(first["delegation_id"], second["delegation_id"])
        await self.manager.receive(first["delegation_id"])
        execute.assert_awaited_once()
        with self.assertRaisesRegex(ValueError, "different delegation"):
            self.manager.start(workspace="/repo", phase="delegation", inputs={"task": "different"},
                               execute=execute, base=self.base, request_id="request-1")

    async def test_blocker_budget_leaves_room_for_terminal_and_unicode_is_bounded(self):
        receipt = self.launch()
        job = receipt["delegation_id"]
        for index in range(100):
            event = self.manager.publish(job, "needs_decision", {"status": "running", "summary": str(index) + "界" * 180})
            if event:
                self.assertLessEqual(json_size(event), 600)
        self.gate.set()
        await asyncio.wait_for(self.manager.workers[job], 2)
        self.assertEqual(self.manager.store.get(job)["sequence"], MAX_DECISION_EVENTS + 1)
        event = self.manager.store.events(job, MAX_DECISION_EVENTS)[0]
        self.assertEqual(event["kind"], "completed")
        self.assertLessEqual(json_size(event), 1600)

    async def test_restart_replays_terminal_without_starting_or_reinterpreting_live_ownership(self):
        receipt = self.launch()
        self.gate.set()
        terminal = await self.manager.receive(receipt["delegation_id"])
        recovered = BackgroundDelegations(self.tmp.name)
        try:
            self.assertEqual(await recovered.receive(receipt["delegation_id"]), terminal)
            self.assertEqual(recovered.result(receipt["delegation_id"]), REPORT)
            live = self.launch(request_id="another")
            suspended = await recovered.receive(live["delegation_id"])
            self.assertTrue(suspended["delivery_suspended"])
            self.assertEqual(suspended["status"], "unknown")
            with self.assertRaisesRegex(ValueError, "another server"):
                await recovered.cancel(live["delegation_id"])
            self.assertEqual(recovered.workers, {})
        finally:
            await recovered.close()

    async def test_invalid_cursor_never_skips_undelivered_events(self):
        receipt = self.launch()
        job = receipt["delegation_id"]
        for cursor in (True, -1, 2):
            with self.assertRaises(ValueError):
                await self.manager.receive(job, cursor)
        with self.assertRaises(ValueError):
            self.manager.acknowledge(job, 1)
        with self.assertRaisesRegex(ValueError, "no terminal result"):
            self.manager.result(job)

    async def test_malformed_result_becomes_a_saved_failure(self):
        receipt = self.launch(AsyncMock(return_value="not a report"))
        batch = await asyncio.wait_for(self.manager.receive(receipt["delegation_id"]), 2)
        self.assertEqual(batch["events"][0]["kind"], "failed")
        self.assertIn("invalid terminal report", self.manager.result(receipt["delegation_id"])["error"])

    async def test_private_failure_details_stay_out_of_pushed_events(self):
        private = "App Server raw stderr pid=12345 C:/private/rollout.log Private planning marker"
        receipt = self.launch(AsyncMock(side_effect=RuntimeError(private)))
        batch = await asyncio.wait_for(self.manager.receive(receipt["delegation_id"]), 2)
        self.assertEqual(batch["events"][0]["kind"], "failed")
        self.assertEqual(batch["events"][0]["error_code"], "RuntimeError")
        self.assertNotIn("pid=", json.dumps(batch))
        self.assertNotIn("rollout.log", json.dumps(batch))
        self.assertEqual(self.manager.result(receipt["delegation_id"])["error"], private)


class GatewayBackgroundTests(BackgroundCase):
    async def test_lost_launch_receipt_can_be_recovered_read_only_before_planning(self):
        coord = FakeCoordinator()
        coord.base.state_dir = Path(self.tmp.name)
        with patch.object(gateway, "_background", return_value=self.manager), patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "_background_manager", self.manager), \
             patch.object(gateway, "get_available_models", return_value={"gemini-3.8-flash-high"}), \
             patch.object(gateway, "_run_luna_plan", AsyncMock(return_value=PLAN)):
            receipt = await gateway.delegate_via_luna("Implement", "/repo", FakeContext(), request_id="lost-receipt")
            before = self.manager.store.db.total_changes
            recovered = gateway.inspect_delegation("/repo", request_id="lost-receipt")
            self.assertEqual(recovered, receipt)
            self.assertEqual(self.manager.store.db.total_changes, before)
            self.assertEqual(coord.delegate_calls, [])
            await self.manager.receive(receipt["delegation_id"])

    async def test_corrections_preserve_pins_and_exclude_concurrent_requests(self):
        coord = FakeCoordinator()
        entered = asyncio.Event()
        async def plan(*args, **kwargs):
            entered.set()
            await self.gate.wait()
            return dict(PLAN)
        planner = AsyncMock(side_effect=plan)
        with patch.object(gateway, "_background", return_value=self.manager), patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"gemini-3.8-flash-high"}), \
             patch.object(gateway, "_run_luna_plan", planner):
            first = await gateway.continue_via_luna("task-existing", "Fix", FakeContext(), request_id="correction-1")
            await asyncio.wait_for(entered.wait(), 2)
            retried = await gateway.continue_via_luna("task-existing", "Fix", FakeContext(), request_id="correction-1")
            # Retrying reuses the task while its live status may advance from
            # queued to planning. The closed gate prevents later execution.
            for field in ("delegation_id", "task_id", "task_type", "delivery"):
                self.assertEqual(first[field], retried[field], field)
            for receipt in (first, retried):
                self.assertIn(receipt["status"], ("queued", "planning"))
                self.assertFalse(receipt["terminal"])
            self.assertFalse(self.gate.is_set())
            with self.assertRaisesRegex(ValueError, "already pending"):
                await gateway.continue_via_luna("task-existing", "Another fix", FakeContext(), request_id="correction-2")
            self.gate.set()
            event = await self.manager.receive(first["delegation_id"])
            result = self.manager.result(first["delegation_id"])
            self.assertEqual(event["events"][0]["task_id"], "task-existing")
            self.assertEqual(result["antigravity"]["requested_model"], "gemini-3.8-flash-high")
            self.assertEqual(len(coord.steer_calls), 1)
            self.assertEqual(coord.steer_calls[0][0], "task-existing")
            self.assertEqual(coord.delegate_calls, [])
            self.assertNotIn("task-existing", gateway._active_corrections)

    async def test_cancel_during_late_start_retains_exact_task_identity_and_worktree(self):
        coord = FakeCoordinator()
        entered, release = threading.Event(), threading.Event()
        original = coord.delegate
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("Startup release was not delivered")
            return original(*args, **kwargs)
        coord.delegate = delayed
        with patch.object(gateway, "_background", return_value=self.manager), patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"gemini-3.8-flash-high"}), \
             patch.object(gateway, "_run_luna_plan", AsyncMock(return_value=PLAN)):
            receipt = await gateway.delegate_via_luna("Implement", "/repo", FakeContext())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await gateway.cancel_delegation(delegation_id=receipt["delegation_id"])
            await asyncio.sleep(0)
            release.set()
            batch = await asyncio.wait_for(self.manager.receive(receipt["delegation_id"]), 2)
            self.assertEqual(batch["events"][0]["kind"], "cancelled")
            self.assertEqual(batch["events"][0]["task_id"], "task-123")
            self.assertEqual(batch["events"][0]["worktree"], "/isolated/worktree")
            self.assertTrue(coord.cancel_calls)
            self.assertEqual(set(coord.cancel_calls), {"task-123"})

    async def test_default_gateway_returns_before_planning_and_delivery_reconnect_keeps_same_task(self):
        coord = FakeCoordinator()
        coord.base.state_dir = Path(self.tmp.name)
        entered = asyncio.Event()
        async def plan(*args, **kwargs):
            entered.set()
            await self.gate.wait()
            return dict(PLAN)
        planner = AsyncMock(side_effect=plan)
        with patch.object(gateway, "_background", return_value=self.manager), patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"gemini-3.8-flash-high"}), \
             patch.object(gateway, "_run_luna_plan", planner):
            receipt = await gateway.delegate_via_luna("Implement", "/repo", FakeContext(), request_id="gateway-job")
            self.assertEqual(receipt["status"], "queued")
            self.assertEqual(coord.delegate_calls, [])
            await entered.wait()
            second = await gateway.delegate_via_luna("Implement", "/repo", FakeContext(), request_id="gateway-job")
            self.assertEqual(second["delegation_id"], receipt["delegation_id"])
            receiving = asyncio.create_task(gateway.receive_delegation_events(receipt["delegation_id"]))
            await asyncio.sleep(0)
            receiving.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await receiving
            self.gate.set()
            batch = await asyncio.wait_for(gateway.receive_delegation_events(receipt["delegation_id"]), 2)
            self.assertEqual(batch["events"][0]["kind"], "completed")
            self.assertEqual(len(coord.delegate_calls), 1)
            planner.assert_awaited_once()
            result = gateway.read_delegation_result(receipt["delegation_id"])
            self.assertEqual(result["task_id"], "task-123")
            self.assertEqual(result["antigravity"]["requested_model"], "gemini-3.8-flash-high")
            self.assertEqual(result["implementation"]["worktree"]["path"], "/isolated/worktree")

    async def test_missing_argon_pushes_explicit_opus_offer_without_creating_work(self):
        coord = FakeCoordinator()
        with patch.object(gateway, "_background", return_value=self.manager), patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value={"claude-opus-5-5-thinking"}):
            receipt = await gateway.delegate_via_luna("Heavy work", "/repo", FakeContext(), reasoning="heavy")
            batch = await asyncio.wait_for(gateway.receive_delegation_events(receipt["delegation_id"]), 2)
            event = batch["events"][0]
            self.assertEqual(event["kind"], "needs_decision")
            self.assertTrue(event["terminal"])
            self.assertIn("Argon", event["summary"])
            self.assertIn("Opus", event["summary"])
            self.assertEqual(coord.delegate_calls, [])


class AppServerAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_host_cursor_recovers_an_ack_failure_without_redelivering(self):
        saved = {}
        delivered = []
        fail_ack = True
        async def save(job, sequence):
            saved[job] = sequence
        async def call(method, params):
            nonlocal fail_ack
            if method == "turn/start":
                delivered.append(json.loads(params["toolOutput"]["output"])["sequence"])
                return {"result": {}}
            if params["tool"] == "ack_delegation_events":
                if fail_ack:
                    fail_ack = False
                    raise RuntimeError("lost ACK connection")
                payload = {}
            else:
                sequence = params["arguments"]["after_sequence"] + 1
                payload = {"events": [{"sequence": sequence}], "closed": sequence == 2}
            return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
        with self.assertRaisesRegex(RuntimeError, "lost ACK"):
            await AppServerDelivery(call, "parent", save_cursor=save).relay("job")
        self.assertEqual(saved["job"], 1)
        await AppServerDelivery(call, "parent", save_cursor=save).relay("job", saved["job"])
        self.assertEqual(delivered, [1, 2])
        self.assertEqual(saved["job"], 2)
    async def test_delivery_stays_tool_data_and_ack_happens_after_host_acceptance(self):
        event = {"sequence": 1, "kind": "completed", "terminal": True}
        calls = []
        async def call(method, params):
            calls.append((method, params))
            if method == "mcpServer/tool/call":
                value = {"events": [event], "closed": True} if params["tool"] == "receive_delegation_events" else {}
                return {"result": {"content": [{"type": "text", "text": json.dumps(value)}]}}
            return {"result": {"turn": {"id": "turn-next"}}}
        result = await AppServerDelivery(call, "parent-thread").relay("delegation-test")
        self.assertEqual(result, 1)
        self.assertEqual([method for method, _ in calls], ["mcpServer/tool/call", "turn/start", "mcpServer/tool/call"])
        self.assertEqual(calls[1][1]["input"], [])
        self.assertEqual(calls[1][1]["threadId"], "parent-thread")
        self.assertEqual(json.loads(calls[1][1]["toolOutput"]["output"]), event)
        self.assertEqual(calls[2][1]["tool"], "ack_delegation_events")

    async def test_unsupported_host_or_rejected_delivery_never_acknowledges(self):
        calls = []
        async def call(method, params):
            calls.append((method, params))
            if method == "turn/start":
                return {"error": {"message": "toolOutput unsupported"}}
            return {"result": {"content": [{"type": "text", "text": json.dumps({"events": [{"sequence": 1}], "closed": True})}]}}
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            await AppServerDelivery(call, "parent").relay("job")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
