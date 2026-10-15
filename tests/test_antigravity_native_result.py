import tempfile
import time
import unittest
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import server
from server import TaskManager, TaskRecord, save_metadata


class AntigravityNativeResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.manager = TaskManager(self.state)
        self.record = TaskRecord(
            task_id="task-native-result",
            workspace=str(self.workspace),
            status="running",
            created_at=time.time(),
            updated_at=time.time(),
            server_instance_id=server.SERVER_INSTANCE_ID,
            log_path=str(self.state / "task-native-result.log"),
            meta_path=str(self.state / "task-native-result.json"),
            active_prompt="implement",
        )
        self.manager._tasks[self.record.task_id] = self.record

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _structured(summary="implemented"):
        return {
            "summary": summary,
            "tests_run": ["pytest -q"],
            "tests_passed": True,
            "remaining_work": [],
            "risks": ["review diff"],
        }

    def test_terminal_result_persists_structured_output_in_turn_record(self):
        structured = self._structured()
        state = self.manager._record_result(
            self.record,
            {
                "status": "SUCCESS",
                "response": '{"summary":"diagnostic only"}',
                "structured_output": structured,
                "num_turns": 1,
                "duration_seconds": 1.0,
                "usage": {"total_tokens": 10},
            },
            "implement",
        )

        self.assertEqual(state, "completed")
        self.assertEqual(self.record.turns[-1]["structured_output"], structured)
        self.assertEqual(self.manager.structured_output(self.record.task_id), structured)

    def test_structured_output_survives_task_manager_restart(self):
        structured = self._structured("persisted")
        self.manager._record_result(
            self.record,
            {
                "status": "SUCCESS",
                "response": "diagnostic",
                "structured_output": structured,
                "num_turns": 1,
                "duration_seconds": 1.0,
                "usage": {"total_tokens": 10},
            },
            "implement",
        )
        self.record.status = "completed"
        save_metadata(self.record)

        restarted = TaskManager(self.state)
        self.assertEqual(restarted.structured_output(self.record.task_id), structured)

    def test_started_followup_does_not_reuse_previous_turn_output(self):
        self.manager._record_result(
            self.record,
            {
                "status": "SUCCESS",
                "response": "first",
                "structured_output": self._structured("first"),
                "num_turns": 1,
                "duration_seconds": 1.0,
                "usage": {"total_tokens": 10},
            },
            "implement",
        )
        self.record.current_turn_index = 2
        self.record.status = "running"

        self.assertIsNone(self.manager.structured_output(self.record.task_id))

    def test_failed_followup_without_structured_output_does_not_reuse_prior_output(self):
        self.manager._record_result(
            self.record,
            {
                "status": "SUCCESS",
                "response": "first",
                "structured_output": self._structured("first"),
                "num_turns": 1,
                "duration_seconds": 1.0,
                "usage": {"total_tokens": 10},
            },
            "implement",
        )
        self.record.current_turn_index = 2
        self.record.active_prompt = "fix regression"
        state = self.manager._record_result(
            self.record,
            {
                "status": "ERROR",
                "response": "failed",
                "error": "tool failed",
                "num_turns": 2,
                "duration_seconds": 2.0,
                "usage": {"total_tokens": 15},
            },
            "fix regression",
        )

        self.assertEqual(state, "failed")
        self.assertIsNone(self.record.turns[-1]["structured_output"])
        self.assertIsNone(self.manager.structured_output(self.record.task_id))


if __name__ == "__main__":
    unittest.main()
