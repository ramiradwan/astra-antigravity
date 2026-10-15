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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PLUGIN_DIR))

import quota_server


def quota_snapshot(remaining, bucket="gemini-5h"):
    return {
        "checked_at": time.time(),
        "model": quota_server.DEFAULT_MODEL,
        "available": True,
        "buckets": {bucket: {"remaining_fraction": remaining}},
        "all_buckets": {bucket: {"remaining_fraction": remaining}},
        "min_remaining_fraction": remaining,
        "remaining_fraction": remaining,
        "limiting_bucket": bucket,
        "warning": remaining < 0.15,
        "blocked": remaining < 0.05,
        "warn_fraction": 0.15,
        "min_start_fraction": 0.05,
        "error": None,
        "source": "test",
    }


class FakeBaseManager:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.records = {}
        self._session_processes = {}
        self.start_calls = []
        self.steer_calls = []
        self.cancel_calls = []
        self._counter = 0

    def start_task(self, task, workspace):
        self.start_calls.append((task, workspace))
        self._counter += 1
        tid = f"task-{self._counter}"
        record = SimpleNamespace(
            task_id=tid,
            status="running",
            workspace=str(Path(workspace).resolve()),
            current_turn_index=1,
            pid=1000 + self._counter,
            log_path=str(self.state_dir / f"{tid}.log"),
            conversation_id=f"cid-{self._counter}",
            session_alive=True,
            error=None,
            exit_code=None,
            created_at=time.time(),
            updated_at=time.time(),
            queued_instruction=None,
        )
        self.records[tid] = record
        self._session_processes[tid] = object()
        return record

    def get_task(self, task_id):
        return self.records.get(task_id)

    def wait_for_task(self, task_id, timeout):
        r = self.records[task_id]
        return {
            "task_id": task_id,
            "status": r.status,
            "finished": r.status in ("completed", "failed", "cancelled", "unknown"),
            "conversation_id": r.conversation_id,
            "current_turn": r.current_turn_index,
            "session_alive": r.session_alive,
            "error": r.error,
        }

    def complete(self, task_id):
        self.records[task_id].status = "completed"

    def steer_task(self, task_id, instruction):
        r = self.records[task_id]
        if r.status != "completed":
            raise ValueError("not completed")
        self.steer_calls.append((task_id, instruction))
        r.current_turn_index += 1
        r.status = "running"
        return {
            "task_id": task_id,
            "status": "running",
            "queued": False,
            "turn_index": r.current_turn_index,
            "conversation_id": r.conversation_id,
            "session_reused": True,
            "pid": r.pid,
        }

    def get_result(self, task_id, max_chars):
        r = self.records[task_id]
        return {
            "task_id": task_id,
            "status": r.status,
            "conversation_id": r.conversation_id,
            "num_turns": r.current_turn_index,
            "response": "done",
            "error": r.error,
            "session_alive": r.session_alive,
        }

    def read_progress(self, task_id, offset, limit):
        return {"task_id": task_id, "offset": offset, "limit": limit, "events": []}

    def cancel_task(self, task_id):
        self.cancel_calls.append(task_id)
        r = self.records[task_id]
        r.status = "cancelled"
        return {"task_id": task_id, "status": "cancelled", "cancelled": True}


class QuotaParsingTests(unittest.TestCase):
    def test_exact_antigravity_usage_shape(self):
        payload = {
            "command": {"data": {"groups": [
                {"name": "Gemini Models", "buckets": [
                    {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.11, "reset_in_seconds": 90},
                    {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.71},
                ]},
                {"name": "Third party", "buckets": [
                    {"id": "3p-5h", "remaining_fraction": 0.02}
                ]},
            ]}}}
        buckets = quota_server._extract_buckets(payload)
        relevant = quota_server._relevant_buckets(buckets)
        self.assertEqual(relevant["gemini-5h"]["remaining_fraction"], 0.11)
        self.assertEqual(relevant["gemini-weekly"]["remaining_fraction"], 0.71)
        self.assertNotIn("3p-5h", relevant)

    def test_nested_json_and_text_fallback(self):
        nested = {"response": json.dumps({"quota": {"gemini-weekly": {"remaining_fraction": 0.42}}})}
        self.assertTrue(quota_server._extract_buckets(nested))
        text = quota_server._extract_buckets("Gemini Flash: 12.5% remaining\n")
        self.assertAlmostEqual(next(iter(text.values()))["remaining_fraction"], 0.125)

    def test_delta(self):
        delta = quota_server._quota_delta(quota_snapshot(0.70), quota_snapshot(0.62))
        row = delta["buckets"]["gemini-5h"]
        self.assertAlmostEqual(row["remaining_fraction_delta"], -0.08)
        self.assertAlmostEqual(row["consumed_fraction"], 0.08)


class QuotaCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        self.base = FakeBaseManager(Path(self.tmp.name) / "state")
        self.q = quota_server.QuotaCoordinator(self.base)
        self.q._start_monitor = lambda *args, **kwargs: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_quota_snapshot_uses_read_only_usage_command(self):
        payload = {"command": {"data": {"groups": [{"buckets": [
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.10},
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.60},
        ]}]}}}
        cp = subprocess.CompletedProcess(["agy"], 0, stdout=json.dumps(payload), stderr="")
        with patch("quota_server.server.find_agy", return_value="agy"), patch("quota_server.subprocess.run", return_value=cp) as run:
            snap = self.q.get_quota_snapshot(str(self.workspace))
        self.assertTrue(snap["available"])
        self.assertTrue(snap["warning"])
        self.assertFalse(snap["blocked"])
        self.assertEqual(snap["limiting_bucket"], "gemini-5h")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[1:3], ["-p", "/usage"])
        self.assertIn("--output-format", cmd)

    def test_unreadable_quota_fails_open(self):
        cp = subprocess.CompletedProcess(["agy"], 1, stdout="", stderr="old cli")
        with patch("quota_server.server.find_agy", return_value="agy"), patch("quota_server.subprocess.run", return_value=cp):
            snap = self.q.get_quota_snapshot()
        self.assertFalse(snap["available"])
        self.assertFalse(snap["blocked"])
        self.assertIn("exited with code", snap["error"])

    def test_initial_low_quota_blocks_without_starting_base_task(self):
        self.q.get_quota_snapshot = lambda workspace=None: quota_snapshot(0.03)
        result = self.q.delegate("implement", str(self.workspace))
        self.assertEqual(result["status"], "quota_blocked")
        self.assertEqual(self.base.start_calls, [])
        self.assertEqual(result["limiting_bucket"], "gemini-5h")

    def test_before_after_and_delta_are_attached_to_result(self):
        snaps = iter([quota_snapshot(0.70), quota_snapshot(0.62)])
        self.q.get_quota_snapshot = lambda workspace=None: next(snaps)
        delegated = self.q.delegate("work", str(self.workspace))
        tid = delegated["task_id"]
        self.base.complete(tid)
        result = self.q.result(tid)
        self.assertAlmostEqual(result["quota_before"]["min_remaining_fraction"], 0.70)
        self.assertAlmostEqual(result["quota_after"]["min_remaining_fraction"], 0.62)
        self.assertAlmostEqual(result["quota_delta"]["buckets"]["gemini-5h"]["consumed_fraction"], 0.08)

    def test_running_followup_is_held_until_post_turn_quota_check(self):
        snaps = iter([quota_snapshot(0.20), quota_snapshot(0.03)])
        self.q.get_quota_snapshot = lambda workspace=None: next(snaps)
        tid = self.q.delegate("turn1", str(self.workspace))["task_id"]
        queued = self.q.steer(tid, "turn2")
        self.assertTrue(queued["queued"])
        self.assertEqual(self.base.steer_calls, [])
        self.base.complete(tid)
        self.q._record_post_turn_quota(tid, 1)
        advanced = self.q._advance_pending(tid)
        self.assertFalse(advanced)
        self.assertEqual(self.base.steer_calls, [])
        state = self.q._get_state(tid)
        self.assertTrue(state["quota_blocked_next_turn"])
        self.assertIsNone(state["pending_instruction"])

    def test_safe_queued_followup_advances_same_task(self):
        snaps = iter([quota_snapshot(0.80), quota_snapshot(0.70)])
        self.q.get_quota_snapshot = lambda workspace=None: next(snaps)
        tid = self.q.delegate("turn1", str(self.workspace))["task_id"]
        self.q.steer(tid, "turn2")
        self.base.complete(tid)
        self.q._record_post_turn_quota(tid, 1)
        self.assertTrue(self.q._advance_pending(tid))
        self.assertEqual(self.base.steer_calls, [(tid, "turn2")])
        self.assertEqual(self.base.get_task(tid).current_turn_index, 2)
        self.assertEqual(self.base.get_task(tid).status, "running")

    def test_direct_completed_followup_is_blocked_by_fresh_preflight(self):
        snaps = iter([quota_snapshot(0.20), quota_snapshot(0.03)])
        self.q.get_quota_snapshot = lambda workspace=None: next(snaps)
        tid = self.q.delegate("turn1", str(self.workspace))["task_id"]
        self.base.complete(tid)
        result = self.q.steer(tid, "turn2")
        self.assertEqual(result["status"], "quota_blocked")
        self.assertEqual(self.base.steer_calls, [])
        self.assertEqual(self.base.get_task(tid).current_turn_index, 1)

    def test_wait_does_not_return_previous_turn_if_background_monitor_already_advanced(self):
        snaps = iter([quota_snapshot(0.80), quota_snapshot(0.70), quota_snapshot(0.60)])
        self.q.get_quota_snapshot = lambda workspace=None: next(snaps)
        tid = self.q.delegate("turn1", str(self.workspace))["task_id"]
        self.q.steer(tid, "turn2")
        base_wait_calls = 0

        def fake_wait(task_id, timeout):
            nonlocal base_wait_calls
            base_wait_calls += 1
            r = self.base.get_task(task_id)
            if base_wait_calls == 1:
                r.status = "completed"
                result = {"task_id": tid, "status": "completed", "finished": True,
                          "conversation_id": r.conversation_id, "current_turn": 1,
                          "session_alive": True, "error": None}
                self.q._record_post_turn_quota(tid, 1)
                self.q._advance_pending(tid)
                return result
            r.status = "completed"
            return {"task_id": tid, "status": "completed", "finished": True,
                    "conversation_id": r.conversation_id, "current_turn": 2,
                    "session_alive": True, "error": None}

        self.base.wait_for_task = fake_wait
        result = self.q.wait(tid, 1.0)
        self.assertTrue(result["finished"])
        self.assertEqual(result["current_turn"], 2)
        self.assertEqual(base_wait_calls, 2)

    def test_cancel_discards_quota_pending_followup(self):
        self.q.get_quota_snapshot = lambda workspace=None: quota_snapshot(0.8)
        tid = self.q.delegate("turn1", str(self.workspace))["task_id"]
        self.q.steer(tid, "turn2")
        result = self.q.cancel(tid)
        self.assertTrue(result["cancelled"])
        self.assertFalse(self.q._get_state(tid)["pending_instruction"])

    def test_quota_override_bypasses_start_floor_and_records_audit_metadata(self):
        self.q.get_quota_snapshot = lambda workspace=None: quota_snapshot(0.04802)

        # 1. Without override: blocked
        blocked = self.q.delegate("blocked_task", str(self.workspace), quota_override=False)
        self.assertEqual(blocked["status"], "quota_blocked")
        self.assertFalse(blocked["quota_override_used"])
        self.assertEqual(len(self.base.start_calls), 0)

        # 2. With override: allowed and recorded
        allowed = self.q.delegate("override_task", str(self.workspace), quota_override=True)
        self.assertEqual(allowed["status"], "running")
        self.assertTrue(allowed["quota_override_used"])
        self.assertAlmostEqual(allowed["quota_before"]["remaining_fraction"], 0.04802)
        self.assertEqual(len(self.base.start_calls), 1)

        tid = allowed["task_id"]

        # Status inspection reflects override
        status = self.q.status(tid)
        self.assertTrue(status["quota_override_used"])
        self.assertAlmostEqual(status["quota_before"]["remaining_fraction"], 0.04802)

        # Result retains auditable override flag and before quota
        self.base.complete(tid)
        res = self.q.result(tid)
        self.assertTrue(res["quota_override_used"])
        self.assertAlmostEqual(res["quota_before"]["remaining_fraction"], 0.04802)

    def test_quota_override_in_steer_and_advance_pending(self):
        snaps = iter([quota_snapshot(0.03), quota_snapshot(0.03), quota_snapshot(0.03)])
        self.q.get_quota_snapshot = lambda workspace=None: next(snaps)

        # Start with override
        tid = self.q.delegate("turn1", str(self.workspace), quota_override=True)["task_id"]

        # Steer while running queues follow-up with override preserved
        queued = self.q.steer(tid, "turn2")
        self.assertTrue(queued["queued"])
        self.assertTrue(queued["quota_override_used"])

        # Complete turn 1 and advance pending
        self.base.complete(tid)
        self.q._record_post_turn_quota(tid, 1)
        advanced = self.q._advance_pending(tid)
        self.assertTrue(advanced)
        self.assertEqual(self.base.steer_calls, [(tid, "turn2")])

    def test_quota_override_retains_server_instance_id_and_process_ownership(self):
        real_state = Path(self.tmp.name) / "real_state"
        manager = quota_server.server.TaskManager(real_state)
        q = quota_server.QuotaCoordinator(manager)
        q._start_monitor = lambda *args, **kwargs: None
        q.get_quota_snapshot = lambda workspace=None: quota_snapshot(0.04802)

        fake_proc = SimpleNamespace(pid=8888, poll=lambda: None)
        with patch.object(manager, "_launch_session", return_value=fake_proc), \
             patch.object(manager, "_run_session"), \
             patch.object(manager, "_send_prompt"), \
             patch("server.find_agy", return_value="agy"):
            delegated = q.delegate("task", str(self.workspace), quota_override=True)
            tid = delegated["task_id"]

            # Task started within the same MCP server instance
            record = manager.get_task(tid)
            self.assertEqual(record.server_instance_id, quota_server.server.SERVER_INSTANCE_ID)
            self.assertEqual(record.status, "running")
            self.assertIsNone(record.error)

            # Process ownership is retained
            status = q.status(tid)
            self.assertTrue(status["is_owned"])
            self.assertTrue(status["quota_override_used"])
            self.assertEqual(status["status"], "running")

            # Simulate the bug when a second/different process opens the metadata:
            record.server_instance_id = "different-server-instance"
            quota_server.server.save_metadata(record)
            del manager._tasks[tid]

            lost = manager.get_task(tid)
            self.assertEqual(lost.status, "unknown")
            self.assertEqual(lost.error, "Process ownership lost across server restart.")
            self.assertFalse(lost.session_alive)


if __name__ == "__main__":
    unittest.main()
