import unittest
from pathlib import Path
import sys

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import tempfile
import proxy_policy
from proxy_policy import (
    ANTIGRAVITY_CORE_TOOLS,
    ANTIGRAVITY_REQUIRED_TOOLS,
    FORBIDDEN_PROXY_MODELS,
    PROXY_BOOTSTRAP_MESSAGE,
    PROXY_FORK_TURNS,
    PROXY_MODEL,
    PROXY_REASONING_EFFORT,
    _normalize_agent_path,
    attest_child_proxy,
    check_proxy_mcp_tools,
    extract_runtime_metadata,
    find_child_rollout,
    get_proxy_spec,
    get_verification_policy,
    inspect_rollout_attestation,
    resolve_child_thread_id,
    resolve_delegation_route,
    validate_proxy_model,
    validate_runtime_attestation,
    validate_spawn_arguments,
)


class TestProxyPolicy(unittest.TestCase):
    def test_proxy_spec_requests_luna_low_effort_fork_none(self):
        spec = get_proxy_spec()
        self.assertEqual(spec["task_name"], "gemini_lifecycle_proxy")
        self.assertEqual(spec["model"], "gpt-5.6-luna")
        self.assertEqual(spec["reasoning_effort"], "low")
        self.assertEqual(spec["fork_turns"], "none")
        self.assertEqual(spec["message"], PROXY_BOOTSTRAP_MESSAGE)
        self.assertIn("Attestation bootstrap only", spec["message"])

    def test_proxy_never_silently_falls_back_to_sol_terra_astra(self):
        valid_spawn = get_proxy_spec()
        valid_runtime = {"model": "gpt-5.6-luna", "effort": "low"}
        for forbidden in ("gpt-5-sol", "gpt-5-terra", "gpt-5-astra", "sol", "terra", "astra", "inherit", "default"):
            self.assertFalse(validate_proxy_model(forbidden))
            route, reason = resolve_delegation_route(
                luna_available=True,
                child_model=forbidden,
                spawn_args=valid_spawn,
                runtime_metadata=valid_runtime,
            )
            self.assertEqual(route, "direct_mcp")
            self.assertIn("forbidden", reason.lower())

    def test_validate_proxy_model_accepts_luna(self):
        self.assertTrue(validate_proxy_model("gpt-5.6-luna"))
        self.assertTrue(validate_proxy_model("gpt-5.6-luna:latest"))
        self.assertFalse(validate_proxy_model(None))
        self.assertFalse(validate_proxy_model(""))

    def test_direct_mcp_fallback_if_luna_cannot_spawn(self):
        route, reason = resolve_delegation_route(luna_available=False)
        self.assertEqual(route, "direct_mcp")
        self.assertIn("unavailable", reason.lower())

    def test_luna_proxy_mcp_tool_access_check(self):
        all_tools = list(ANTIGRAVITY_REQUIRED_TOOLS)
        self.assertTrue(check_proxy_mcp_tools(all_tools))

        # Only core tools
        self.assertTrue(check_proxy_mcp_tools(ANTIGRAVITY_CORE_TOOLS))

        # Missing a core tool (e.g. dynamic tool inheritance failed under fork_turns=none)
        partial_tools = ["delegate_to_gemini", "wait_for_task"]
        self.assertFalse(check_proxy_mcp_tools(partial_tools))

        # If tools missing, routes to direct_mcp
        route, reason = resolve_delegation_route(
            luna_available=True,
            child_model="gpt-5.6-luna",
            tools_available=False,
            spawn_args=get_proxy_spec(),
            runtime_metadata={"model": "gpt-5.6-luna", "effort": "low"},
        )
        self.assertEqual(route, "direct_mcp")
        self.assertIn("cannot access antigravity mcp tools", reason.lower())

    def test_confirmed_luna_proxy_selected(self):
        route, reason = resolve_delegation_route(
            luna_available=True,
            child_model="gpt-5.6-luna",
            tools_available=True,
            spawn_args=get_proxy_spec(),
            runtime_metadata={"model": "gpt-5.6-luna", "effort": "low"},
        )
        self.assertEqual(route, "luna_proxy")
        self.assertIn("confirmed", reason.lower())

    def test_no_mandatory_second_verifier_worker(self):
        policy = get_verification_policy()
        self.assertEqual(policy["verifier"], "parent_astra")
        self.assertFalse(policy["mandatory_worker"])
        self.assertEqual(policy["optional_worker_model"], "gpt-5.6-luna")
        self.assertIn("accepted", policy["verdicts"])
        self.assertIn("needs_fix", policy["verdicts"])
        self.assertIn("failed_verification", policy["verdicts"])

    def test_validate_spawn_arguments_valid(self):
        valid_spec = {
            "task_name": "gemini_lifecycle_proxy",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "fork_turns": "none",
        }
        valid, reason = validate_spawn_arguments(valid_spec)
        self.assertTrue(valid)
        self.assertIn("explicitly specify", reason.lower())

        # Without task_name is also valid
        spec_no_name = {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "fork_turns": "none",
        }
        valid, _ = validate_spawn_arguments(spec_no_name)
        self.assertTrue(valid)

        # Agent type absent or empty string/None is valid
        for allowed_empty in (None, ""):
            valid, _ = validate_spawn_arguments({**valid_spec, "agent_type": allowed_empty})
            self.assertTrue(valid)

    def test_validate_spawn_arguments_rejects_invalid(self):
        self.assertFalse(validate_spawn_arguments(None)[0])
        self.assertFalse(validate_spawn_arguments("")[0])
        self.assertFalse(validate_spawn_arguments({})[0])

        # Missing or non-Luna model
        for bad_model in (None, "", "gpt-5-sol", "gpt-5-terra", "gpt-5-astra", "default", "inherit"):
            valid, reason = validate_spawn_arguments({
                "model": bad_model,
                "reasoning_effort": "low",
                "fork_turns": "none",
            })
            self.assertFalse(valid)
            self.assertIn("model", reason.lower())

        # Invalid reasoning effort
        for bad_effort in (None, "", "medium", "high", "max"):
            valid, reason = validate_spawn_arguments({
                "model": "gpt-5.6-luna",
                "reasoning_effort": bad_effort,
                "fork_turns": "none",
            })
            self.assertFalse(valid)
            self.assertIn("reasoning_effort", reason.lower())

        # Missing or invalid fork_turns
        for bad_fork in (None, "", "all", "last", "true"):
            valid, reason = validate_spawn_arguments({
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "fork_turns": bad_fork,
            })
            self.assertFalse(valid)
            self.assertIn("fork_turns", reason.lower())

        # Any non-empty agent_type is strictly forbidden (Codex bug #32831 / #33268)
        for bad_agent in ("worker", "verifier", "general", "default", "luna", "luna_low", "none"):
            valid, reason = validate_spawn_arguments({
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "fork_turns": "none",
                "agent_type": bad_agent,
            })
            self.assertFalse(valid)
            self.assertIn("forbidden", reason.lower())

    def test_extract_runtime_metadata(self):
        self.assertEqual(extract_runtime_metadata(None), {})
        self.assertEqual(extract_runtime_metadata("not-a-dict"), {})

        # thread_settings_applied format
        ts_event = {
            "type": "thread_settings_applied",
            "thread_settings": {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
        }
        meta = extract_runtime_metadata(ts_event)
        self.assertEqual(meta["model"], "gpt-5.6-luna")
        self.assertEqual(meta["effort"], "low")

        # turn_context with nested collaboration mode (actual live Codex Desktop rollout format)
        tc_event = {
            "type": "turn_context",
            "payload": {
                "model": "gpt-5.6-luna",
                "effort": "low",
                "collaboration_mode": {
                    "settings": {
                        "model": "gpt-5.6-luna",
                        "reasoning_effort": "low",
                    }
                },
            },
        }
        meta = extract_runtime_metadata(tc_event)
        self.assertEqual(meta["model"], "gpt-5.6-luna")
        self.assertEqual(meta["effort"], "low")

        # top-level fields
        direct = {"model": "gpt-5.6-luna", "effort": "low"}
        self.assertEqual(extract_runtime_metadata(direct), direct)

    def test_validate_runtime_attestation(self):
        # Valid Luna metadata
        valid, extracted, reason = validate_runtime_attestation({
            "model": "gpt-5.6-luna",
            "effort": "low",
        })
        self.assertTrue(valid)
        self.assertEqual(extracted["model"], "gpt-5.6-luna")
        self.assertIn("confirmed", reason.lower())

        # None or empty
        self.assertFalse(validate_runtime_attestation(None)[0])
        self.assertFalse(validate_runtime_attestation({})[0])

        # Forbidden non-Luna model fallback
        for forbidden in ("gpt-5-sol", "gpt-5-terra", "gpt-5-astra", "sol", "terra", "astra"):
            valid, _, reason = validate_runtime_attestation({
                "model": forbidden,
                "effort": "low",
            })
            self.assertFalse(valid)
            self.assertIn("non-luna", reason.lower())

        # Non-low reasoning effort
        for bad_effort in ("medium", "high", "max"):
            valid, _, reason = validate_runtime_attestation({
                "model": "gpt-5.6-luna",
                "effort": bad_effort,
            })
            self.assertFalse(valid)
            self.assertIn("reasoning effort", reason.lower())

        # Never accept LLM text self-attestation without metadata
        text_claim = {"content": "I am Luna and I will help you with this task."}
        valid, _, _ = validate_runtime_attestation(text_claim)
        self.assertFalse(valid)

    def test_inspect_rollout_attestation(self):
        # Live Codex JSONL turn_context event
        turn_context_line = (
            '{"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low", '
            '"collaboration_mode": {"settings": {"model": "gpt-5.6-luna", "reasoning_effort": "low"}}}}'
        )
        valid, meta, reason = inspect_rollout_attestation(turn_context_line)
        self.assertTrue(valid)
        self.assertEqual(meta["model"], "gpt-5.6-luna")
        self.assertEqual(meta["effort"], "low")

        # thread_settings_applied event
        ts_line = '{"type": "thread_settings_applied", "thread_settings": {"model": "gpt-5.6-luna", "reasoning_effort": "low"}}'
        valid, meta, reason = inspect_rollout_attestation(ts_line)
        self.assertTrue(valid)
        self.assertEqual(meta["model"], "gpt-5.6-luna")

        # Rollout with non-Luna model (e.g. parent Sol fallback)
        sol_line = '{"type": "turn_context", "payload": {"model": "gpt-5-sol", "effort": "high"}}'
        valid, meta, reason = inspect_rollout_attestation(sol_line)
        self.assertFalse(valid)
        self.assertEqual(meta["model"], "gpt-5-sol")
        self.assertIn("non-luna", reason.lower())

        # Latest effective setting: earlier Luna, but later switches to Sol -> fails
        switched_lines = "\n".join([
            turn_context_line,
            sol_line,
        ])
        valid, meta, reason = inspect_rollout_attestation(switched_lines)
        self.assertFalse(valid)
        self.assertIn("non-luna", reason.lower())

        # Latest effective setting: earlier Luna/low, but later switches to Luna/high -> fails
        effort_switched = "\n".join([
            turn_context_line,
            '{"type": "thread_settings_applied", "thread_settings": {"model": "gpt-5.6-luna", "reasoning_effort": "high"}}',
        ])
        valid, meta, reason = inspect_rollout_attestation(effort_switched)
        self.assertFalse(valid)
        self.assertIn("reasoning effort", reason.lower())

        # Rollout with only LLM text claims ("I am Luna") but no attestation event
        chat_lines = "\n".join([
            '{"type": "event", "name": "agent_started"}',
            '{"type": "message", "content": "I am Luna and am ready to proxy your task."}',
        ])
        valid, _, reason = inspect_rollout_attestation(chat_lines)
        self.assertFalse(valid)
        self.assertIn("no turn_context", reason.lower())

        # Inspecting from a real file on disk with thread correlation
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".jsonl") as f:
            f.write('{"type": "session_meta", "payload": {"id": "child-thread-uuid-999"}}\n')
            f.write(turn_context_line + "\n")
            temp_path = Path(f.name)

        try:
            # Matches expected thread ID
            valid, meta, _ = inspect_rollout_attestation(temp_path, expected_thread_id="child-thread-uuid-999")
            self.assertTrue(valid)
            self.assertEqual(meta["model"], "gpt-5.6-luna")

            # Mismatched thread ID
            valid, _, reason = inspect_rollout_attestation(temp_path, expected_thread_id="different-thread-uuid")
            self.assertFalse(valid)
            self.assertIn("did not match expected child thread id", reason.lower())
        finally:
            if temp_path.exists():
                temp_path.unlink()

        # Non-existent file
        valid, _, _ = inspect_rollout_attestation(Path("non_existent_rollout.jsonl"))
        self.assertFalse(valid)

    def test_find_child_rollout(self):
        self.assertIsNone(find_child_rollout(None))
        self.assertIsNone(find_child_rollout(""))
        self.assertIsNone(find_child_rollout("nonexistent-thread-id-000", sessions_dir=Path("nonexistent_dir")))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            rollout_file = tmppath / "rollout-2026-09-05T18-00-00-target-child-777.jsonl"
            rollout_file.write_text('{"type": "turn_context"}\n', encoding="utf-8")

            found = find_child_rollout("target-child-777", sessions_dir=tmppath)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, rollout_file.name)

    def test_resolve_delegation_route_with_attestation(self):
        valid_spawn = {
            "task_name": "gemini_lifecycle_proxy",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "fork_turns": "none",
        }
        valid_runtime = {
            "model": "gpt-5.6-luna",
            "effort": "low",
        }

        # Both valid: routes to luna_proxy
        route, reason = resolve_delegation_route(
            luna_available=True,
            tools_available=True,
            spawn_args=valid_spawn,
            runtime_metadata=valid_runtime,
        )
        self.assertEqual(route, "luna_proxy")
        self.assertIn("confirmed", reason.lower())

        # Missing spawn_args (None) fails closed to direct_mcp
        route, reason = resolve_delegation_route(
            luna_available=True,
            tools_available=True,
            spawn_args=None,
            runtime_metadata=valid_runtime,
        )
        self.assertEqual(route, "direct_mcp")
        self.assertIn("spawn arguments were not attested", reason.lower())

        # Missing runtime_metadata (None) fails closed to direct_mcp
        route, reason = resolve_delegation_route(
            luna_available=True,
            tools_available=True,
            spawn_args=valid_spawn,
            runtime_metadata=None,
        )
        self.assertEqual(route, "direct_mcp")
        self.assertIn("runtime metadata was not attested", reason.lower())

        # Rejected spawn args (e.g. any agent_type)
        bad_spawn = {**valid_spawn, "agent_type": "worker"}
        route, reason = resolve_delegation_route(
            luna_available=True,
            tools_available=True,
            spawn_args=bad_spawn,
            runtime_metadata=valid_runtime,
        )
        self.assertEqual(route, "direct_mcp")
        self.assertIn("rejected", reason.lower())

        # Rejected runtime metadata (e.g. fallback to Sol)
        bad_runtime = {"model": "gpt-5-sol", "effort": "high"}
        route, reason = resolve_delegation_route(
            luna_available=True,
            tools_available=True,
            spawn_args=valid_spawn,
            runtime_metadata=bad_runtime,
        )
        self.assertEqual(route, "direct_mcp")
        self.assertIn("rejected", reason.lower())

        # Empty dict runtime metadata
        route, reason = resolve_delegation_route(
            luna_available=True,
            tools_available=True,
            spawn_args=valid_spawn,
            runtime_metadata={},
        )
        self.assertEqual(route, "direct_mcp")
        self.assertIn("rejected", reason.lower())

    def test_normalize_agent_path(self):
        self.assertEqual(_normalize_agent_path("/root/gemini_lifecycle_proxy"), "gemini_lifecycle_proxy")
        self.assertEqual(_normalize_agent_path("gemini_lifecycle_proxy"), "gemini_lifecycle_proxy")
        self.assertEqual(_normalize_agent_path("/gemini_lifecycle_proxy"), "gemini_lifecycle_proxy")
        # Ensure removeprefix does not strip characters like 'r', 'o', 't' erroneously
        self.assertEqual(_normalize_agent_path("/root/root_runner"), "root_runner")
        self.assertEqual(_normalize_agent_path("root_runner"), "root_runner")
        self.assertEqual(_normalize_agent_path(None), "")

    def test_resolve_child_thread_id(self):
        # 1. Real Codex Desktop V2 parent rollout line with SubAgentActivity
        parent_line_1 = (
            '{"timestamp":"2026-09-05T17:34:13.071Z","ordinal":23,"type":"event_msg","payload":{'
            '"type":"item_completed","thread_id":"parent-thread-001","turn_id":"turn-1","item":{'
            '"type":"SubAgentActivity","id":"call_J12zpGGwadjdi9ClRd6Q6uGe","kind":"started",'
            '"agent_thread_id":"01a072a2-959f-7793-82a0-f9f786ffb457","agent_path":"/root/gemini_lifecycle_proxy"}}}'
        )
        # 2. Top-level sub_agent_activity payload format
        parent_line_2 = (
            '{"timestamp":"2026-07-19T08:46:41.076Z","type":"event_msg","payload":{'
            '"type":"sub_agent_activity","event_id":"call_3l03FOdAvSmQ7DppinmXi7pF",'
            '"occurred_at_ms":1784450779804,"agent_thread_id":"019f798d-e307-7432-b1b4-861b35d69931",'
            '"agent_path":"/root/color_perception","kind":"started"}}'
        )

        rollout_text = "\n".join([parent_line_2, parent_line_1])

        # Resolve by canonical task name
        child_id = resolve_child_thread_id(rollout_text, "/root/gemini_lifecycle_proxy")
        self.assertEqual(child_id, "01a072a2-959f-7793-82a0-f9f786ffb457")

        # Resolve by simple task name
        child_id = resolve_child_thread_id(rollout_text, "gemini_lifecycle_proxy")
        self.assertEqual(child_id, "01a072a2-959f-7793-82a0-f9f786ffb457")

        # Unknown task name returns None
        self.assertIsNone(resolve_child_thread_id(rollout_text, "unknown_task"))

        # Non-started activity (e.g. kind="interacted") is ignored
        interacted_line = (
            '{"type":"event_msg","payload":{"type":"sub_agent_activity","agent_thread_id":"wrong-id",'
            '"agent_path":"/root/gemini_lifecycle_proxy","kind":"interacted"}}'
        )
        self.assertIsNone(resolve_child_thread_id(interacted_line, "/root/gemini_lifecycle_proxy"))

    def test_resolve_child_thread_id_ordering_and_timestamp(self):
        # Rollout with two distinct spawns of /root/gemini_lifecycle_proxy
        events = [
            # Spawn 1
            '{"timestamp":"2026-09-05T10:00:00.000Z","type":"function_call","payload":{"call_id":"call_turn1","name":"spawn_agent","arguments":"{\\"task_name\\":\\"gemini_lifecycle_proxy\\"}"}}',
            '{"timestamp":"2026-09-05T10:00:01.000Z","type":"event_msg","payload":{"type":"item_completed","item":{"type":"SubAgentActivity","kind":"started","agent_thread_id":"child-uuid-1","agent_path":"/root/gemini_lifecycle_proxy"}}}',
            '{"timestamp":"2026-09-05T10:00:02.000Z","type":"function_call_output","payload":{"call_id":"call_turn1","output":"{\\"task_name\\":\\"/root/gemini_lifecycle_proxy\\"}"}}',
            # Spawn 2
            '{"timestamp":"2026-09-05T10:05:00.000Z","type":"function_call","payload":{"call_id":"call_turn2","name":"spawn_agent","arguments":"{\\"task_name\\":\\"gemini_lifecycle_proxy\\"}"}}',
            '{"timestamp":"2026-09-05T10:05:01.000Z","type":"event_msg","payload":{"type":"item_completed","item":{"type":"SubAgentActivity","kind":"started","agent_thread_id":"child-uuid-2","agent_path":"/root/gemini_lifecycle_proxy"}}}',
            '{"timestamp":"2026-09-05T10:05:02.000Z","type":"function_call_output","payload":{"call_id":"call_turn2","output":"{\\"task_name\\":\\"/root/gemini_lifecycle_proxy\\"}"}}',
        ]
        rollout_text = "\n".join(events)

        # Correlate by spawn_call_id
        child_1 = resolve_child_thread_id(rollout_text, "/root/gemini_lifecycle_proxy", spawn_call_id="call_turn1")
        self.assertEqual(child_1, "child-uuid-1")

        child_2 = resolve_child_thread_id(rollout_text, "/root/gemini_lifecycle_proxy", spawn_call_id="call_turn2")
        self.assertEqual(child_2, "child-uuid-2")

        # Unknown call_id fails closed
        self.assertIsNone(resolve_child_thread_id(rollout_text, "/root/gemini_lifecycle_proxy", spawn_call_id="call_nonexistent"))

        # Correlate by spawn_timestamp
        child_ts1 = resolve_child_thread_id(rollout_text, "/root/gemini_lifecycle_proxy", spawn_timestamp="2026-09-05T10:00:00.000Z")
        self.assertEqual(child_ts1, "child-uuid-1")

        child_ts2 = resolve_child_thread_id(rollout_text, "/root/gemini_lifecycle_proxy", spawn_timestamp="2026-09-05T10:05:00.000Z")
        self.assertEqual(child_ts2, "child-uuid-2")

        # Without spawn_call_id or timestamp, resolves the latest spawn
        child_latest = resolve_child_thread_id(rollout_text, "/root/gemini_lifecycle_proxy")
        self.assertEqual(child_latest, "child-uuid-2")

        # Ambiguous case: >1 started activities occurring after a spawn fails closed
        ambiguous_events = [
            '{"type":"function_call","payload":{"call_id":"call_ambig","name":"spawn_agent","arguments":"{\\"task_name\\":\\"gemini_lifecycle_proxy\\"}"}}',
            '{"type":"event_msg","payload":{"type":"sub_agent_activity","kind":"started","agent_thread_id":"child-a","agent_path":"/root/gemini_lifecycle_proxy"}}',
            '{"type":"event_msg","payload":{"type":"sub_agent_activity","kind":"started","agent_thread_id":"child-b","agent_path":"/root/gemini_lifecycle_proxy"}}',
        ]
        self.assertIsNone(resolve_child_thread_id("\n".join(ambiguous_events), "/root/gemini_lifecycle_proxy", spawn_call_id="call_ambig"))

    def test_inspect_rollout_attestation_multi_identifier(self):
        child_rollout = "\n".join([
            '{"type": "session_meta", "payload": {"id": "child-uuid-1", "session_id": "root-uuid", '
            '"parent_thread_id": "parent-uuid-1", "agent_path": "/root/gemini_lifecycle_proxy", "agent_nickname": "Ampere"}}',
            '{"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low", '
            '"collaboration_mode": {"settings": {"model": "gpt-5.6-luna", "reasoning_effort": "low"}}}}',
        ])

        # All identifiers match
        valid, meta, reason = inspect_rollout_attestation(
            child_rollout,
            expected_thread_id="child-uuid-1",
            expected_parent_thread_id="parent-uuid-1",
            expected_agent_path="/root/gemini_lifecycle_proxy",
            expected_nickname="Ampere",
        )
        self.assertTrue(valid)
        self.assertEqual(meta["model"], "gpt-5.6-luna")

        # Expected parent thread ID mismatch fails
        valid, _, reason = inspect_rollout_attestation(
            child_rollout,
            expected_thread_id="child-uuid-1",
            expected_parent_thread_id="WRONG-parent-uuid",
        )
        self.assertFalse(valid)
        self.assertIn("parent_thread_id", reason.lower())

        # Expected agent path mismatch fails
        valid, _, reason = inspect_rollout_attestation(
            child_rollout,
            expected_thread_id="child-uuid-1",
            expected_agent_path="different_agent",
        )
        self.assertFalse(valid)
        self.assertIn("agent_path", reason.lower())

        # Expected nickname mismatch fails independently
        valid, _, reason = inspect_rollout_attestation(
            child_rollout,
            expected_thread_id="child-uuid-1",
            expected_nickname="DifferentNick",
        )
        self.assertFalse(valid)
        self.assertIn("agent_nickname", reason.lower())

    def test_inspect_rollout_attestation_strict_absence_and_nested_schema(self):
        # Nested schema: source.subagent.thread_spawn
        nested_rollout = "\n".join([
            '{"type": "session_meta", "payload": {"id": "child-nested-1", "source": {"subagent": {"thread_spawn": {'
            '"parent_thread_id": "parent-uuid-nested", "agent_path": "/root/gemini_lifecycle_proxy", "agent_nickname": "Darwin"}}}}}',
            '{"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}}',
        ])
        valid, meta, reason = inspect_rollout_attestation(
            nested_rollout,
            expected_thread_id="child-nested-1",
            expected_parent_thread_id="parent-uuid-nested",
            expected_agent_path="/root/gemini_lifecycle_proxy",
            expected_nickname="Darwin",
        )
        self.assertTrue(valid)
        self.assertEqual(meta["model"], "gpt-5.6-luna")

        # Strict fail on absence: rollout missing parent_thread_id when expected fails
        missing_parent = "\n".join([
            '{"type": "session_meta", "payload": {"id": "child-1", "agent_path": "/root/gemini_lifecycle_proxy"}}',
            '{"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}}',
        ])
        valid, _, reason = inspect_rollout_attestation(
            missing_parent,
            expected_thread_id="child-1",
            expected_parent_thread_id="parent-1",
        )
        self.assertFalse(valid)
        self.assertIn("parent_thread_id", reason.lower())

        # Strict fail on absence: rollout missing agent_path when expected fails
        missing_path = "\n".join([
            '{"type": "session_meta", "payload": {"id": "child-1", "parent_thread_id": "parent-1"}}',
            '{"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}}',
        ])
        valid, _, reason = inspect_rollout_attestation(
            missing_path,
            expected_thread_id="child-1",
            expected_agent_path="/root/gemini_lifecycle_proxy",
        )
        self.assertFalse(valid)
        self.assertIn("agent_path", reason.lower())

        # Missing session_meta completely fails when expected identifiers are supplied
        no_meta = '{"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}}'
        valid, _, reason = inspect_rollout_attestation(
            no_meta,
            expected_thread_id="child-1",
            expected_parent_thread_id="parent-1",
        )
        self.assertFalse(valid)
        self.assertIn("session_meta", reason.lower())

    def test_attest_child_proxy_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            child_id = "01a072a2-959f-7793-82a0-f9f786ffb457"
            parent_id = "01a072a2-5a6f-7ea2-8925-560f11f445ea"

            # Create parent rollout
            parent_file = tmppath / "rollout-parent.jsonl"
            parent_file.write_text(
                '{"timestamp":"2026-09-05T20:00:00Z","type":"function_call","payload":{"call_id":"call_123","name":"spawn_agent","arguments":"{\\"task_name\\":\\"gemini_lifecycle_proxy\\"}"}}\n'
                '{"timestamp":"2026-09-05T20:00:01Z","type":"event_msg","payload":{"type":"item_completed","thread_id":"' + parent_id + '","item":{'
                '"type":"SubAgentActivity","id":"act_123","kind":"started",'
                '"agent_thread_id":"' + child_id + '","agent_path":"/root/gemini_lifecycle_proxy"}}}\n',
                encoding="utf-8",
            )

            # Create child rollout
            child_file = tmppath / f"rollout-2026-09-05T20-00-00-{child_id}.jsonl"
            child_file.write_text(
                '{"type":"session_meta","payload":{"id":"' + child_id + '","parent_thread_id":"' + parent_id + '","agent_path":"/root/gemini_lifecycle_proxy","agent_nickname":"Ampere"}}\n'
                '{"type":"turn_context","payload":{"model":"gpt-5.6-luna","effort":"low"}}\n',
                encoding="utf-8",
            )

            # Successful end-to-end attestation
            valid, meta, reason = attest_child_proxy(
                parent_rollout=parent_file,
                canonical_task_name="/root/gemini_lifecycle_proxy",
                spawn_call_id="call_123",
                expected_parent_thread_id=parent_id,
                expected_nickname="Ampere",
                sessions_dir=tmppath,
            )
            self.assertTrue(valid)
            self.assertEqual(meta["model"], "gpt-5.6-luna")
            self.assertEqual(meta["child_thread_id"], child_id)
            self.assertIn("confirmed", reason.lower())

            # Missing child rollout fails closed
            child_file.unlink()
            valid, meta, reason = attest_child_proxy(
                parent_rollout=parent_file,
                canonical_task_name="/root/gemini_lifecycle_proxy",
                spawn_call_id="call_123",
                sessions_dir=tmppath,
            )
            self.assertFalse(valid)
            self.assertIn("not found", reason.lower())


if __name__ == "__main__":
    unittest.main()
