import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from task_progress import ProgressState, reduce_antigravity_event


class TaskProgressTests(unittest.TestCase):
    def test_only_explicit_native_input_requests_require_parent_decisions(self):
        prose = self.apply({"event": "step_update", "step_update": {
            "step_type": "agent_response", "state": "DONE", "text_delta": "Blocked; ask the user for help"}})
        self.assertFalse(prose.decision_required)
        failed = self.apply({"event": "step_update", "step_update": {
            "step_type": "tool", "state": "ERROR", "tool_name": "run_command",
            "tool_info": {"parameters": {"CommandLine": "pytest -q"}, "error": "recoverable failure"}}})
        self.assertFalse(failed.decision_required)
        blocked = self.apply({"event": "step_update", "step_update": {
            "step_type": "tool", "state": "WAITING_FOR_INPUT", "tool_name": "request_user_input",
            "tool_info": {"parameters": {"question": "Choose policy"}}}})
        self.assertTrue(blocked.decision_required)
        self.assertEqual(blocked.decision_reason, "Choose policy")

    def setUp(self):
        self.state = ProgressState()

    def apply(self, event):
        self.state = reduce_antigravity_event(self.state, event)
        return self.state

    def test_arbitrary_agent_text_cannot_invent_current_file_or_test_result(self):
        state = self.apply({
            "event": "step_update",
            "step_update": {
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": (
                    "I inspected .venv/Scripts/python.exe and remotes/origin/ws-7.3c4. "
                    "An old pytest run failed, but I am now reasoning about the task."
                ),
            },
        })
        self.assertIsNone(state.current_file)
        self.assertIsNone(state.last_test_command)
        self.assertIsNone(state.test_state)
        self.assertEqual(state.activity, "I inspected .venv/Scripts/python.exe and remotes/origin/ws-7.3c4. An old pytest run failed, but I am now reasoning about the task.")

    def test_run_command_uses_structured_command_and_result(self):
        self.apply({
            "event": "step_update",
            "step_update": {
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "python -m pytest tests/test_api.py -q"},
                },
            },
        })
        self.assertEqual(self.state.last_test_command, "python -m pytest tests/test_api.py -q")
        self.assertEqual(self.state.test_state, "running")
        self.assertIsNone(self.state.current_file)

        self.apply({
            "event": "step_update",
            "step_update": {
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "python -m pytest tests/test_api.py -q"},
                    "output": "18 passed in 0.42s",
                },
            },
        })
        self.assertEqual(self.state.test_state, "passed")

    def test_failed_non_test_command_does_not_poison_test_state(self):
        self.apply({
            "event": "step_update",
            "step_update": {
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "pytest -q"},
                    "output": "12 passed",
                },
            },
        })
        self.assertEqual(self.state.test_state, "passed")

        self.apply({
            "event": "step_update",
            "step_update": {
                "state": "ERROR",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "dir /s"},
                    "error": {"message": "Access denied: tests/old_failure.txt"},
                },
            },
        })
        self.assertEqual(self.state.test_state, "passed")
        self.assertEqual(self.state.last_test_command, "pytest -q")

    def test_file_operation_uses_structured_target_not_paths_in_payload(self):
        state = self.apply({
            "event": "step_update",
            "step_update": {
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "write_to_file",
                "tool_info": {
                    "name": "write_to_file",
                    "parameters": {
                        "TargetFile": "src/legal/release.json",
                        "CodeContent": "references icons/icon128.png and .venv/Scripts/python.exe",
                    },
                },
            },
        })
        self.assertEqual(state.current_file, "src/legal/release.json")
        self.assertEqual(state.last_change, "Updated src/legal/release.json")

    def test_duplicate_equivalent_event_does_not_advance_sequence(self):
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
        first = self.apply(event)
        first_sequence = first.sequence
        second = self.apply(event)
        self.assertEqual(second.sequence, first_sequence)


if __name__ == "__main__":
    unittest.main()
