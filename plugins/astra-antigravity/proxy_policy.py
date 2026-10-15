"""Proxy routing, safeguards, and verification policy for Astra Antigravity delegation."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Iterable

PROXY_MODEL = "gpt-5.6-luna"
PROXY_REASONING_EFFORT = "low"
PROXY_FORK_TURNS = "none"

PROXY_BOOTSTRAP_MESSAGE = (
    "Attestation bootstrap only. Do not call tools. Do not inspect files. "
    "Do not delegate. Reply READY only."
)

FORBIDDEN_PROXY_MODELS = frozenset({
    "gpt-5-sol",
    "gpt-5-terra",
    "gpt-5-astra",
    "sol",
    "terra",
    "astra",
    "default",
    "inherit",
})

ANTIGRAVITY_REQUIRED_TOOLS = frozenset({
    "check_antigravity_health",
    "recommend_delegation",
    "get_gemini_quota",
    "delegate_to_gemini",
    "wait_for_task",
    "get_task_status",
    "get_task_progress_summary",
    "read_task_progress",
    "get_task_result",
    "steer_gemini",
    "follow_up_gemini",
    "cancel_task",
    "cleanup_task_worktree",
})

ANTIGRAVITY_CORE_TOOLS = frozenset({
    "delegate_to_gemini",
    "wait_for_task",
    "get_task_result",
    "steer_gemini",
})


def get_proxy_spec() -> dict[str, str]:
    """Return explicit worker spawn arguments for the Gemini lifecycle proxy (Phase 1 inert bootstrap)."""
    return {
        "task_name": "gemini_lifecycle_proxy",
        "model": PROXY_MODEL,
        "reasoning_effort": PROXY_REASONING_EFFORT,
        "fork_turns": PROXY_FORK_TURNS,
        "message": PROXY_BOOTSTRAP_MESSAGE,
    }


def validate_spawn_arguments(args: dict | None) -> tuple[bool, str]:
    """Validate that spawn_agent was called with explicit Luna parameters and no overriding role."""
    if not isinstance(args, dict):
        return False, "Spawn arguments must be a dictionary."

    model = str(args.get("model") or "").strip()
    if not validate_proxy_model(model):
        return False, f"Spawn argument 'model' must be '{PROXY_MODEL}', got '{model or None}'."

    effort = str(args.get("reasoning_effort") or "").strip().lower()
    if effort != PROXY_REASONING_EFFORT:
        return (
            False,
            f"Spawn argument 'reasoning_effort' must be '{PROXY_REASONING_EFFORT}', got '{effort or None}'.",
        )

    fork_turns = str(args.get("fork_turns") or "").strip().lower()
    if fork_turns != PROXY_FORK_TURNS:
        return (
            False,
            f"Spawn argument 'fork_turns' must be '{PROXY_FORK_TURNS}', got '{fork_turns or None}'.",
        )

    # agent_type MUST be absent. Upstream Codex bug #32831: custom roles can rebuild
    # the child config and discard explicit spawn-time model/effort settings.
    # We strictly reject any non-empty agent_type (no role whitelist).
    agent_type = args.get("agent_type")
    if agent_type is not None and str(agent_type).strip():
        return (
            False,
            f"Spawn argument 'agent_type' ('{agent_type}') is forbidden. Custom roles can rebuild child config and discard model/effort overrides in Codex (upstream bug #32831). Omit agent_type completely.",
        )

    return True, "Spawn arguments explicitly specify Luna low reasoning effort, fork_turns=none, and omit agent_type."


def extract_runtime_metadata(data: dict | None) -> dict:
    """Extract effective model and reasoning effort from Codex child runtime metadata or rollout events."""
    if not isinstance(data, dict):
        return {}

    # 1. Direct thread_settings_applied event
    ts = data.get("thread_settings")
    if isinstance(ts, dict):
        return {
            "model": ts.get("model"),
            "effort": ts.get("reasoning_effort") or ts.get("effort"),
        }

    # 2. turn_context event payload or unwrapped payload
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
    model = payload.get("model")
    effort = payload.get("effort") or payload.get("reasoning_effort")

    collab = payload.get("collaboration_mode")
    if isinstance(collab, dict):
        settings = collab.get("settings")
        if isinstance(settings, dict):
            model = model or settings.get("model")
            effort = effort or settings.get("reasoning_effort") or settings.get("effort")

    # 3. Direct top-level fields
    if not model and "model" in data:
        model = data.get("model")
    if not effort:
        effort = data.get("effort") or data.get("reasoning_effort")

    return {
        "model": model,
        "effort": effort,
    }


def validate_runtime_attestation(metadata: dict | None) -> tuple[bool, dict, str]:
    """Validate that the spawned child thread's effective runtime metadata proves Luna execution.

    Never accepts generated LLM text claims ("I am Luna") as attestation.
    """
    if not metadata or not isinstance(metadata, dict):
        return False, {}, "No child runtime metadata was provided."

    extracted = extract_runtime_metadata(metadata)
    model = extracted.get("model")
    effort = str(extracted.get("effort") or "").strip().lower()

    if not validate_proxy_model(model):
        return (
            False,
            extracted,
            f"Child runtime metadata reported non-Luna model: '{model}'. Forbidden parent fallback detected.",
        )

    if effort != PROXY_REASONING_EFFORT:
        return (
            False,
            extracted,
            f"Child runtime metadata reported reasoning effort '{effort}', expected '{PROXY_REASONING_EFFORT}'.",
        )

    return (
        True,
        extracted,
        f"Child runtime attestation confirmed: model={model}, effort={effort}.",
    )


def _normalize_agent_path(path: str | None) -> str:
    """Normalize agent path by removing leading /root/ prefix and slashes."""
    if not path:
        return ""
    clean = str(path).strip()
    return clean.removeprefix("/root/").lstrip("/")


def _parse_timestamp(ts: object) -> float | None:
    """Parse float, int, or ISO-8601 string timestamp into seconds since epoch."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        val = float(ts)
        return val / 1000.0 if val > 1e11 else val
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            val = float(s)
            return val / 1000.0 if val > 1e11 else val
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.datetime.fromisoformat(s)
            return dt.timestamp()
        except Exception:
            return None
    return None


def resolve_child_thread_id(
    parent_rollout: str | Path,
    canonical_task_name: str = "gemini_lifecycle_proxy",
    spawn_call_id: str | None = None,
    spawn_timestamp: float | str | None = None,
) -> str | None:
    """Resolve the spawned child thread ID from the parent rollout's sub_agent_activity lifecycle event.

    In Codex MultiAgent V2, spawn_agent returns only {'task_name': '/root/gemini_lifecycle_proxy', 'nickname': '...'}.
    The authoritative child UUID is emitted in the parent rollout as a SubAgentActivity event (kind='started').

    Correlates strictly by rollout ordering and position:
    1. Finds the exact spawn_agent function_call matching spawn_call_id, spawn_timestamp, or matching task_name.
    2. Identifies matching sub_agent_activity(kind='started', agent_path=task_name) occurring AFTER that spawn.
    3. Bounds search to the current turn (stops if another spawn_agent function_call occurs).
    4. If zero or more than one plausible matching events occur, fails closed (returns None) to avoid ambiguous correlation.
    """
    lines: list[str] = []
    if isinstance(parent_rollout, Path):
        try:
            if parent_rollout.is_file():
                lines = parent_rollout.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            return None
    elif isinstance(parent_rollout, str):
        if "\n" not in parent_rollout:
            try:
                p = Path(parent_rollout)
                if p.is_file():
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                else:
                    lines = parent_rollout.splitlines()
            except (OSError, ValueError):
                lines = parent_rollout.splitlines()
        else:
            lines = parent_rollout.splitlines()
    else:
        return None

    norm_target = _normalize_agent_path(canonical_task_name)
    target_clean = str(canonical_task_name or "").strip()

    parsed_events: list[tuple[int, dict]] = []
    for idx, line in enumerate(lines):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict):
            parsed_events.append((idx, ev))

    if not parsed_events:
        return None

    # Step 1: Find the spawn_agent function_call position
    spawn_idx = -1
    parsed_spawn_time = _parse_timestamp(spawn_timestamp)

    for idx, ev in parsed_events:
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload

        types_combined = f"{ev.get('type', '')} {payload.get('type', '')} {item.get('type', '')}".lower()
        if "function_call" in types_combined or "tool_call" in types_combined:
            name = str(payload.get("name") or ev.get("name") or item.get("name") or "")
            if name == "spawn_agent":
                call_id = str(payload.get("call_id") or ev.get("call_id") or item.get("call_id") or item.get("id") or "")

                if spawn_call_id and call_id and call_id != spawn_call_id:
                    continue

                ev_time = _parse_timestamp(ev.get("timestamp") or payload.get("timestamp") or payload.get("occurred_at_ms"))
                if parsed_spawn_time is not None and ev_time is not None:
                    if abs(ev_time - parsed_spawn_time) > 10.0:
                        continue

                raw_args = payload.get("arguments") or ev.get("arguments") or item.get("arguments")
                args_dict = {}
                if isinstance(raw_args, dict):
                    args_dict = raw_args
                elif isinstance(raw_args, str):
                    try:
                        args_dict = json.loads(raw_args)
                    except Exception:
                        pass

                arg_task = str(args_dict.get("task_name") or "")
                if norm_target and arg_task:
                    if _normalize_agent_path(arg_task) != norm_target:
                        continue

                spawn_idx = idx
                if spawn_call_id and call_id == spawn_call_id:
                    break

    has_any_call = any(
        "function_call" in f"{ev.get('type', '')} {(ev.get('payload') or {}).get('type', '')}".lower()
        for _, ev in parsed_events
    )
    if spawn_call_id and spawn_idx == -1 and has_any_call:
        return None

    # Step 2: Search for started sub_agent_activity occurring strictly AFTER spawn_idx
    candidate_activities: list[dict] = []
    for idx, ev in parsed_events:
        if spawn_idx >= 0 and idx <= spawn_idx:
            continue

        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload

        types_combined = f"{ev.get('type', '')} {payload.get('type', '')} {item.get('type', '')}".lower()

        # If we encounter a subsequent spawn_agent function_call, stop searching
        if spawn_idx >= 0 and idx > spawn_idx and ("function_call" in types_combined or "tool_call" in types_combined):
            name = str(payload.get("name") or ev.get("name") or item.get("name") or "")
            if name == "spawn_agent":
                break

        if "sub_agent_activity" not in types_combined and "subagentactivity" not in types_combined and "collabagentspawnendevent" not in types_combined:
            continue

        kind = str(item.get("kind") or payload.get("kind") or "").strip().lower()
        if kind != "started":
            continue

        agent_path = str(item.get("agent_path") or payload.get("agent_path") or item.get("task_name") or "")
        if not agent_path:
            continue

        norm_path = _normalize_agent_path(agent_path)
        if norm_path != norm_target and target_clean != agent_path:
            continue

        if parsed_spawn_time is not None:
            ev_time = _parse_timestamp(ev.get("timestamp") or payload.get("timestamp") or payload.get("occurred_at_ms"))
            if ev_time is not None and ev_time < parsed_spawn_time - 1.0:
                continue

        child_id = str(item.get("agent_thread_id") or payload.get("agent_thread_id") or item.get("new_thread_id") or "")
        if child_id:
            candidate_activities.append({
                "thread_id": child_id,
                "event_idx": idx,
                "agent_path": agent_path,
            })

    # Step 3: Enforce unambiguous correlation
    if len(candidate_activities) == 1:
        return candidate_activities[0]["thread_id"]

    return None


def find_child_rollout(thread_id: str, sessions_dir: str | Path | None = None) -> Path | None:
    """Locate the child session rollout file matching the exact spawned child thread ID."""
    if not thread_id or not isinstance(thread_id, str):
        return None

    clean_id = thread_id.strip()
    if not clean_id:
        return None

    base_dir = Path(sessions_dir) if sessions_dir else Path.home() / ".codex" / "sessions"
    if not base_dir.is_dir():
        return None

    try:
        matches = list(base_dir.rglob(f"*{clean_id}*.jsonl"))
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return matches[0]
    except Exception:
        pass

    return None


def inspect_rollout_attestation(
    rollout: str | Path,
    expected_thread_id: str | None = None,
    expected_parent_thread_id: str | None = None,
    expected_agent_path: str | None = None,
    expected_nickname: str | None = None,
) -> tuple[bool, dict, str]:
    """Inspect a Codex child session rollout file or JSONL text to verify effective model and effort.

    Evaluates the LATEST effective child runtime setting across all turn_context and
    thread_settings_applied events, rather than stopping on the first match.
    If any later event switches away from Luna or low effort, attestation fails.

    Enforces strict multi-identifier provenance verification:
      - PRIMARY: session_meta.id matches expected_thread_id
      - SECONDARY: session_meta.parent_thread_id matches expected_parent_thread_id
      - SECONDARY: session_meta.agent_path matches expected_agent_path
      - SECONDARY (optional): session_meta.agent_nickname matches expected_nickname

    Supports both flat payload fields and nested source.subagent.thread_spawn structures.
    Absence of any expected identifier is treated as an attestation failure.
    """
    lines: list[str] = []
    is_file = False
    if isinstance(rollout, Path):
        try:
            is_file = rollout.is_file()
        except (OSError, ValueError):
            is_file = False
    elif isinstance(rollout, str):
        if "\n" not in rollout:
            try:
                is_file = Path(rollout).is_file()
            except (OSError, ValueError):
                is_file = False

    if is_file:
        try:
            lines = Path(rollout).read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return False, {}, f"Failed to read rollout file: {exc}"
    elif isinstance(rollout, str):
        lines = rollout.splitlines()
    else:
        return False, {}, "Rollout must be a file path or JSONL string."

    if expected_thread_id or expected_parent_thread_id or expected_agent_path or expected_nickname:
        clean_expected_thread = expected_thread_id.strip() if expected_thread_id else None
        clean_expected_parent = expected_parent_thread_id.strip() if expected_parent_thread_id else None
        clean_expected_path = _normalize_agent_path(expected_agent_path) if expected_agent_path else None
        clean_expected_nickname = expected_nickname.strip().lower() if expected_nickname else None

        session_meta_found = False
        meta_valid = True
        mismatch_reason = ""

        for line in lines:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue

            if event.get("type") == "session_meta":
                session_meta_found = True
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                source_dict = payload.get("source") if isinstance(payload.get("source"), dict) else {}
                subagent_dict = source_dict.get("subagent") if isinstance(source_dict.get("subagent"), dict) else {}
                spawn_source = subagent_dict.get("thread_spawn") if isinstance(subagent_dict.get("thread_spawn"), dict) else {}

                actual_id = str(payload.get("id") or payload.get("session_id") or "").strip()
                actual_parent = str(payload.get("parent_thread_id") or spawn_source.get("parent_thread_id") or "").strip()
                actual_path = str(payload.get("agent_path") or spawn_source.get("agent_path") or "").strip()
                actual_nickname = str(payload.get("agent_nickname") or payload.get("nickname") or spawn_source.get("agent_nickname") or "").strip()

                if clean_expected_thread:
                    if not actual_id:
                        meta_valid = False
                        mismatch_reason = "Child rollout session_meta did not contain a thread/session id."
                        break
                    if clean_expected_thread != actual_id and clean_expected_thread not in actual_id and actual_id not in clean_expected_thread:
                        meta_valid = False
                        mismatch_reason = f"Rollout session_meta.id '{actual_id}' did not match expected child thread ID '{clean_expected_thread}'."
                        break

                if clean_expected_parent:
                    if not actual_parent:
                        meta_valid = False
                        mismatch_reason = f"Child rollout session_meta did not contain parent_thread_id (expected '{clean_expected_parent}')."
                        break
                    if clean_expected_parent != actual_parent and clean_expected_parent not in actual_parent and actual_parent not in clean_expected_parent:
                        meta_valid = False
                        mismatch_reason = f"Rollout session_meta.parent_thread_id '{actual_parent}' did not match expected parent thread ID '{clean_expected_parent}'."
                        break

                if clean_expected_path:
                    if not actual_path:
                        meta_valid = False
                        mismatch_reason = f"Child rollout session_meta did not contain agent_path (expected '{expected_agent_path}')."
                        break
                    norm_actual_path = _normalize_agent_path(actual_path)
                    if clean_expected_path != norm_actual_path:
                        meta_valid = False
                        mismatch_reason = f"Rollout session_meta.agent_path '{actual_path}' did not match expected agent path '{expected_agent_path}'."
                        break

                if clean_expected_nickname:
                    if not actual_nickname:
                        meta_valid = False
                        mismatch_reason = f"Child rollout session_meta did not contain agent_nickname (expected '{expected_nickname}')."
                        break
                    if clean_expected_nickname != actual_nickname.lower():
                        meta_valid = False
                        mismatch_reason = f"Rollout session_meta.agent_nickname '{actual_nickname}' did not match expected nickname '{expected_nickname}'."
                        break

                break

        if not session_meta_found:
            return False, {}, "Child rollout does not contain a session_meta event; cannot attest provenance."

        if not meta_valid:
            return False, {}, mismatch_reason

    attestation_events: list[tuple[dict, dict]] = []
    for line in lines:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in ("turn_context", "thread_settings_applied"):
            extracted = extract_runtime_metadata(event)
            attestation_events.append((event, extracted))

    if not attestation_events:
        return False, {}, "No turn_context or thread_settings_applied attestation found in rollout."

    # Evaluate the LATEST effective event
    latest_event, latest_extracted = attestation_events[-1]
    valid, extracted, reason = validate_runtime_attestation(latest_event)
    if not valid:
        return (
            False,
            extracted,
            f"Latest child runtime attestation failed: {reason}",
        )

    # Verify no event in the sequence switched to a forbidden model
    for _, past_extracted in attestation_events:
        past_model = past_extracted.get("model")
        if past_model and not validate_proxy_model(past_model):
            return (
                False,
                latest_extracted,
                f"Rollout contains event with forbidden non-Luna model: '{past_model}'.",
            )

    return (
        True,
        latest_extracted,
        f"Latest child runtime attestation confirmed: model={extracted.get('model')}, effort={extracted.get('effort')}.",
    )


def attest_child_proxy(
    *,
    parent_rollout: str | Path,
    canonical_task_name: str = "/root/gemini_lifecycle_proxy",
    spawn_call_id: str | None = None,
    spawn_timestamp: float | str | None = None,
    expected_parent_thread_id: str | None = None,
    expected_nickname: str | None = None,
    sessions_dir: str | Path | None = None,
) -> tuple[bool, dict, str]:
    """Complete end-to-end attestation of a Codex MultiAgent V2 child proxy.

    1. Resolves child_thread_id from the parent rollout's sub_agent_activity event strictly by ordering.
    2. Locates the child session rollout file matching child_thread_id.
    3. Verifies child session_meta provenance across multiple independent identifiers:
       - PRIMARY: child ID == child_thread_id
       - SECONDARY: parent_thread_id == expected_parent_thread_id
       - SECONDARY: agent_path == canonical_task_name
       - SECONDARY (optional): agent_nickname == expected_nickname
    4. Evaluates latest effective turn_context / thread_settings_applied for model=gpt-5.6-luna and effort=low.
    """
    child_id = resolve_child_thread_id(
        parent_rollout,
        canonical_task_name=canonical_task_name,
        spawn_call_id=spawn_call_id,
        spawn_timestamp=spawn_timestamp,
    )
    if not child_id:
        return (
            False,
            {},
            f"Could not resolve child thread ID for '{canonical_task_name}' from parent rollout sub_agent_activity.",
        )

    child_path = find_child_rollout(child_id, sessions_dir=sessions_dir)
    if not child_path:
        return (
            False,
            {"child_thread_id": child_id},
            f"Child rollout file for thread '{child_id}' was not found in sessions directory.",
        )

    valid, extracted, reason = inspect_rollout_attestation(
        child_path,
        expected_thread_id=child_id,
        expected_parent_thread_id=expected_parent_thread_id,
        expected_agent_path=canonical_task_name,
        expected_nickname=expected_nickname,
    )
    if not valid:
        return False, extracted, reason

    extracted["child_thread_id"] = child_id
    extracted["child_rollout_path"] = str(child_path)
    return True, extracted, reason


def validate_proxy_model(model_name: str | None) -> bool:
    """Verify that a candidate or runtime child model is strictly Luna."""
    if not model_name or not isinstance(model_name, str):
        return False
    clean = model_name.strip().lower()
    if clean in FORBIDDEN_PROXY_MODELS:
        return False
    for forbidden in FORBIDDEN_PROXY_MODELS:
        if forbidden in clean and clean != PROXY_MODEL:
            return False
    return clean == PROXY_MODEL or clean.startswith(f"{PROXY_MODEL}:") or clean.startswith(f"{PROXY_MODEL}-")


def check_proxy_mcp_tools(tools: Iterable[str]) -> bool:
    """Verify that Antigravity MCP tools are visible/available in the proxy toolset."""
    available = {t.strip() for t in tools if isinstance(t, str)}
    return ANTIGRAVITY_CORE_TOOLS.issubset(available)


def resolve_delegation_route(
    *,
    luna_available: bool = True,
    child_model: str | None = PROXY_MODEL,
    tools_available: bool = True,
    spawn_args: dict | None = None,
    runtime_metadata: dict | None = None,
) -> tuple[str, str]:
    """Resolve whether delegation proceeds via Luna proxy or directly via parent MCP.

    Fails closed: requires explicit attestation of BOTH spawn arguments and child runtime metadata.
    If either is missing, invalid, or non-Luna, it rejects the proxy and routes directly to parent MCP.
    Never silently falls back to Sol/Terra/Astra.
    """
    if not luna_available:
        return "direct_mcp", "Luna proxy is unavailable; falling back to direct parent MCP calls."

    if spawn_args is None:
        return "direct_mcp", "Spawn arguments were not attested; falling back to direct parent MCP calls."

    valid_args, args_reason = validate_spawn_arguments(spawn_args)
    if not valid_args:
        return "direct_mcp", f"Proxy spawn arguments rejected: {args_reason} Switching to direct parent MCP."

    if runtime_metadata is None:
        return "direct_mcp", "Runtime metadata was not attested; falling back to direct parent MCP calls."

    valid_meta, _, meta_reason = validate_runtime_attestation(runtime_metadata)
    if not valid_meta:
        return "direct_mcp", f"Proxy runtime attestation rejected: {meta_reason} Switching to direct parent MCP."

    if child_model is not None and not validate_proxy_model(child_model):
        return (
            "direct_mcp",
            f"Child model '{child_model}' is not Luna; fallback to Sol/Terra/Astra is forbidden, using direct parent MCP.",
        )

    if not tools_available:
        return (
            "direct_mcp",
            "Luna proxy cannot access Antigravity MCP tools under fork_turns=none; bypassing to direct parent MCP.",
        )

    return "luna_proxy", "Luna proxy confirmed with verified runtime attestation and Antigravity MCP access."


def get_verification_policy() -> dict[str, object]:
    """Return the verification policy: parent Astra verifies by default."""
    return {
        "verifier": "parent_astra",
        "mandatory_worker": False,
        "optional_worker_model": PROXY_MODEL,
        "verdicts": ["accepted", "needs_fix", "failed_verification"],
    }
