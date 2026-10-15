import tempfile
import threading
import time
import unittest
from pathlib import Path

from server import SERVER_INSTANCE_ID, TaskManager, TaskRecord


class TaskEventTests(unittest.TestCase):
    def test_startup_decisions_are_replayed_once_and_stale_turns_are_excluded(self):
        event = {"event": "step_update", "step_update": {
            "conversation_id": "conversation", "step_index": 1,
            "step_type": "tool", "state": "WAITING_FOR_INPUT",
            "tool_name": "request_user_input", "tool_info": {"parameters": {"question": "Choose policy"}}}}
        with self.manager._lock:
            self.manager._publish_progress(self.record, event)
        updates = []
        unsubscribe = self.manager.subscribe_decisions(self.record.task_id, updates.append)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["decision_reason"], "Choose policy")
        with self.manager._lock:
            self.manager._publish_progress(self.record, event)
        self.assertEqual(len(updates), 1)
        unsubscribe()
        self.record.current_turn_index += 1
        newer = []
        self.manager.subscribe_decisions(self.record.task_id, newer.append)()
        self.assertEqual(newer, [])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = TaskManager(self.root / "state")
        now = time.time()
        self.record = TaskRecord(
            task_id="task-events",
            workspace=str(self.root),
            status="running",
            created_at=now,
            updated_at=now,
            server_instance_id=SERVER_INSTANCE_ID,
            log_path=str(self.root / "state" / "task-events.log"),
            meta_path=str(self.root / "state" / "task-events.json"),
        )
        with self.manager._lock:
            self.manager._tasks[self.record.task_id] = self.record
            self.manager._notify(self.record)

    def tearDown(self):
        self.tmp.cleanup()

    def test_progress_event_wakes_condition_waiter_without_log_polling(self):
        result = {}
        done = threading.Event()

        def wait():
            result.update(
                self.manager.wait_for_update(
                    self.record.task_id, after_sequence=0, timeout_seconds=2
                )
            )
            done.set()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self.assertFalse(done.wait(0.05))

        event = {
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
        }
        with self.manager._lock:
            self.manager._publish_progress(self.record, event)

        self.assertTrue(done.wait(1))
        thread.join(timeout=1)
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(result["tests"]["state"], "running")
        self.assertFalse(result["terminal"])
        self.assertFalse(result["timed_out"])

    def test_terminal_transition_wakes_waiter_even_without_new_progress(self):
        result = {}
        done = threading.Event()

        def wait():
            result.update(
                self.manager.wait_for_update(
                    self.record.task_id, after_sequence=0, timeout_seconds=2
                )
            )
            done.set()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self.assertFalse(done.wait(0.05))
        with self.manager._lock:
            self.record.status = "completed"
            self.manager._notify(self.record)

        self.assertTrue(done.wait(1))
        thread.join(timeout=1)
        self.assertTrue(result["terminal"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["sequence"], 0)

    def test_timeout_is_explicit_and_does_not_change_task_state(self):
        result = self.manager.wait_for_update(
            self.record.task_id, after_sequence=0, timeout_seconds=0.1
        )
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["terminal"])
        self.assertEqual(self.record.status, "running")


if __name__ == "__main__":
    unittest.main()
