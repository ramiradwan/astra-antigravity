import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from task_progress import ProgressState, reduce_antigravity_event


CID = "conv-progress-semantics"


def native_step(step_index, state, step_type, **fields):
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": CID,
            "step_index": step_index,
            "state": state,
            "step_type": step_type,
            **fields,
        },
    }


class NativeProgressSemanticsTests(unittest.TestCase):
    def test_streaming_agent_response_active_fragments_are_model_invisible_progress(self):
        state = ProgressState()
        for fragment in ("Inspecting ", "the repository ", "before editing"):
            state = reduce_antigravity_event(
                state,
                native_step(2, "ACTIVE", "agent_response", text_delta=fragment),
            )
        self.assertEqual(state.sequence, 0)
        self.assertIsNone(state.activity)

        done = reduce_antigravity_event(
            state,
            native_step(
                2,
                "DONE",
                "agent_response",
                text_delta="I inspected the repository before editing.",
            ),
        )
        self.assertEqual(done.sequence, 1)
        self.assertEqual(done.activity, "I inspected the repository before editing.")

    def test_native_user_input_and_empty_checkpoint_do_not_publish_progress(self):
        state = reduce_antigravity_event(
            ProgressState(), native_step(0, "DONE", "user_input")
        )
        self.assertEqual(state.sequence, 0)
        checkpoint = reduce_antigravity_event(
            state, native_step(3, "DONE", "checkpoint")
        )
        self.assertEqual(checkpoint.sequence, 0)

    def test_subagent_metadata_becomes_compact_activity_without_identifier_leakage(self):
        event = native_step(
            5,
            "ACTIVE",
            "tool",
            tool_name="invoke_subagent",
            subagent_info={
                "subagents": [
                    {
                        "type_name": "research",
                        "role": "verification",
                        "conversation_id": "subagent-secret-conversation",
                        "log_uri": "file:///secret/subagent.log",
                        "workspace_uris": ["file:///secret/worktree"],
                    }
                ]
            },
        )
        active = reduce_antigravity_event(ProgressState(), event)
        self.assertEqual(active.activity, "Antigravity verification subagent running")
        serialized = json.dumps(active.to_dict())
        self.assertNotIn("subagent-secret-conversation", serialized)
        self.assertNotIn("subagent.log", serialized)
        self.assertNotIn("secret/worktree", serialized)

        done_event = native_step(
            5,
            "DONE",
            "tool",
            tool_name="invoke_subagent",
            subagent_info=event["step_update"]["subagent_info"],
        )
        done = reduce_antigravity_event(active, done_event)
        self.assertEqual(done.sequence, active.sequence + 1)
        self.assertEqual(done.activity, "Antigravity verification subagent completed")

    def test_canonical_tool_info_name_and_structured_error_drive_test_state(self):
        active = reduce_antigravity_event(
            ProgressState(),
            native_step(
                7,
                "ACTIVE",
                "tool",
                tool_info={
                    "name": "run_command",
                    "parameters": {"CommandLine": "pytest -q"},
                },
            ),
        )
        self.assertEqual(active.test_state, "running")
        failed = reduce_antigravity_event(
            active,
            native_step(
                7,
                "DONE",
                "tool",
                tool_info={
                    "name": "run_command",
                    "parameters": {"CommandLine": "pytest -q"},
                    "error": {"type": "CommandError", "message": "2 tests failed"},
                },
            ),
        )
        self.assertEqual(failed.test_state, "failed")

    def test_legacy_event_without_native_identity_preserves_existing_behavior(self):
        legacy = {
            "event": "step_update",
            "step_update": {
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": "Legacy progress remains visible",
            },
        }
        state = reduce_antigravity_event(ProgressState(), legacy)
        self.assertEqual(state.sequence, 1)
        self.assertEqual(state.activity, "Legacy progress remains visible")


if __name__ == "__main__":
    unittest.main()
