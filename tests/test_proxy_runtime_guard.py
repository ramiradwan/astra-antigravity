import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import proxy_runtime_guard


class TestProxyRuntimeGuard(unittest.TestCase):
    def test_attestation_requires_exact_child_and_parent_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            child_id = "01a072a2-959f-7793-82a0-f9f786ffb457"
            parent_id = "01a072a2-5a6f-7ea2-8925-560f11f445ea"
            child = root / f"rollout-{child_id}.jsonl"
            child.write_text(
                "\n".join([
                    '{"type":"session_meta","payload":{"id":"' + child_id + '","source":{"subagent":{"thread_spawn":{'
                    '"parent_thread_id":"' + parent_id + '","agent_path":"/root/gemini_lifecycle_proxy","agent_nickname":"Ampere"}}}}}',
                    '{"type":"turn_context","payload":{"model":"gpt-5.6-luna","effort":"low"}}',
                ]) + "\n",
                encoding="utf-8",
            )

            with patch("proxy_runtime_guard.proxy_policy.resolve_child_thread_id", return_value=child_id), \
                 patch("proxy_runtime_guard.proxy_policy.find_child_rollout", return_value=child):
                valid, meta, _ = proxy_runtime_guard.attest_child_proxy_strict(
                    parent_rollout="{}",
                    expected_parent_thread_id=parent_id,
                    expected_nickname="Ampere",
                    sessions_dir=root,
                )
                self.assertTrue(valid)
                self.assertEqual(meta["child_thread_id"], child_id)

                # Prefix/substring matches are intentionally rejected for IDs.
                valid, _, reason = proxy_runtime_guard.attest_child_proxy_strict(
                    parent_rollout="{}",
                    expected_parent_thread_id=parent_id[:-1],
                    expected_nickname="Ampere",
                    sessions_dir=root,
                )
                self.assertFalse(valid)
                self.assertIn("exactly match", reason)

    def test_child_id_must_exactly_match_session_meta(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            expected_child = "child-uuid-full"
            child = root / f"rollout-{expected_child}.jsonl"
            child.write_text(
                "\n".join([
                    '{"type":"session_meta","payload":{"id":"child-uuid","parent_thread_id":"parent-1",'
                    '"agent_path":"/root/gemini_lifecycle_proxy"}}',
                    '{"type":"turn_context","payload":{"model":"gpt-5.6-luna","effort":"low"}}',
                ]) + "\n",
                encoding="utf-8",
            )
            with patch("proxy_runtime_guard.proxy_policy.resolve_child_thread_id", return_value=expected_child), \
                 patch("proxy_runtime_guard.proxy_policy.find_child_rollout", return_value=child):
                valid, _, reason = proxy_runtime_guard.attest_child_proxy_strict(
                    parent_rollout="{}",
                    expected_parent_thread_id="parent-1",
                    sessions_dir=root,
                )
                self.assertFalse(valid)
                self.assertIn("does not exactly match", reason)

    def test_activation_probe_is_tool_free_and_nonce_bound(self):
        message, marker = proxy_runtime_guard.build_activation_probe("probe_123456")
        self.assertIn("Do not call tools", message)
        self.assertIn("Reply with exactly", message)
        self.assertEqual(marker, "ASTRA_PROXY_ACTIVATION_ACK:probe_123456")
        with self.assertRaises(ValueError):
            proxy_runtime_guard.build_activation_probe("short")

    def test_followup_activation_requires_new_turn_and_delivery_marker(self):
        marker = "ASTRA_PROXY_ACTIVATION_ACK:probe_123456"
        baseline = [
            '{"type":"session_meta","payload":{"id":"child"}}',
            '{"type":"turn_context","payload":{"model":"gpt-5.6-luna","effort":"low"}}',
        ]
        activated = baseline + [
            '{"type":"turn_context","payload":{"model":"gpt-5.6-luna","effort":"low"}}',
            '{"type":"response_item","payload":{"content":"' + marker + '"}}',
        ]
        valid, meta, reason = proxy_runtime_guard.inspect_followup_activation(
            "\n".join(activated),
            baseline_line_count=len(baseline),
            expected_marker=marker,
        )
        self.assertTrue(valid, reason)
        self.assertEqual(meta["model"], "gpt-5.6-luna")

        # A new turn without the marker models a dropped followup_task payload.
        missing_marker = baseline + [
            '{"type":"turn_context","payload":{"model":"gpt-5.6-luna","effort":"low"}}',
            '{"type":"response_item","payload":{"content":"READY"}}',
        ]
        valid, _, reason = proxy_runtime_guard.inspect_followup_activation(
            "\n".join(missing_marker),
            baseline_line_count=len(baseline),
            expected_marker=marker,
        )
        self.assertFalse(valid)
        self.assertIn("payload may have been dropped", reason)

    def test_followup_activation_rejects_model_or_effort_change(self):
        marker = "ASTRA_PROXY_ACTIVATION_ACK:probe_123456"
        baseline = ['{"type":"session_meta","payload":{"id":"child"}}']
        for runtime in (
            '{"type":"turn_context","payload":{"model":"gpt-5-sol","effort":"low"}}',
            '{"type":"turn_context","payload":{"model":"gpt-5.6-luna","effort":"high"}}',
        ):
            rollout = baseline + [runtime, '{"type":"message","content":"' + marker + '"}']
            valid, _, reason = proxy_runtime_guard.inspect_followup_activation(
                "\n".join(rollout),
                baseline_line_count=1,
                expected_marker=marker,
            )
            self.assertFalse(valid)
            self.assertIn("failed Luna/low attestation", reason)

    def test_followup_activation_rejects_transient_non_luna_then_luna(self):
        marker = "ASTRA_PROXY_ACTIVATION_ACK:probe_123456"
        rollout = "\n".join([
            '{"type":"session_meta","payload":{"id":"child"}}',
            '{"type":"turn_context","payload":{"model":"gpt-5-sol","effort":"low"}}',
            '{"type":"thread_settings_applied","thread_settings":{"model":"gpt-5.6-luna","reasoning_effort":"low"}}',
            '{"type":"message","content":"' + marker + '"}',
        ])
        valid, _, reason = proxy_runtime_guard.inspect_followup_activation(
            rollout,
            baseline_line_count=1,
            expected_marker=marker,
        )
        self.assertFalse(valid)
        self.assertIn("forbidden non-Luna model", reason)


if __name__ == "__main__":
    unittest.main()
