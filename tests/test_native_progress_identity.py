import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from task_progress import ProgressState, reduce_antigravity_event


CID = "conv-native-progress"


def tool_event(step_index, state, command="pytest -q", **tool_info):
    info = {
        "name": "run_command",
        "parameters": {"CommandLine": command},
        **tool_info,
    }
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": CID,
            "step_index": step_index,
            "state": state,
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": info,
        },
    }


class NativeProgressIdentityTests(unittest.TestCase):
    def test_identical_commands_at_different_step_indexes_are_distinct(self):
        first = reduce_antigravity_event(
            ProgressState(), tool_event(4, "DONE", output="12 passed")
        )
        second = reduce_antigravity_event(
            first, tool_event(9, "DONE", output="12 passed")
        )
        self.assertEqual(second.sequence, first.sequence + 1)
        self.assertEqual(second.source_step_index, 9)

    def test_duplicate_native_transition_does_not_advance(self):
        event = tool_event(4, "ACTIVE")
        first = reduce_antigravity_event(ProgressState(), event)
        second = reduce_antigravity_event(first, event)
        self.assertEqual(second.sequence, first.sequence)
        self.assertEqual(second.source_transition, (CID, 4, "tool", "ACTIVE"))

    def test_active_to_done_on_same_native_step_advances(self):
        active = reduce_antigravity_event(ProgressState(), tool_event(4, "ACTIVE"))
        done = reduce_antigravity_event(
            active, tool_event(4, "DONE", output="12 passed")
        )
        self.assertEqual(done.sequence, active.sequence + 1)
        self.assertEqual(done.test_state, "passed")
        self.assertEqual(done.source_transition, (CID, 4, "tool", "DONE"))

    def test_native_identity_is_conversation_scoped(self):
        first = reduce_antigravity_event(ProgressState(), tool_event(4, "ACTIVE"))
        other = tool_event(4, "ACTIVE")
        other["step_update"]["conversation_id"] = "conv-other"
        second = reduce_antigravity_event(first, other)
        self.assertEqual(second.sequence, first.sequence + 1)
        self.assertEqual(second.source_conversation_id, "conv-other")


if __name__ == "__main__":
    unittest.main()
