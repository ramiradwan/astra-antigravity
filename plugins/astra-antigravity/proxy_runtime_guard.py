"""Strict Codex V2 runtime attestation and proxy activation guards."""

from __future__ import annotations

import json
import re
from pathlib import Path

import proxy_policy

PROXY_ACTIVATION_ACK_PREFIX = "ASTRA_PROXY_ACTIVATION_ACK:"
_NONCE_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def _read_rollout_lines(rollout: str | Path) -> tuple[list[str], Path | None]:
    if isinstance(rollout, Path):
        path = rollout
    elif isinstance(rollout, str) and "\n" not in rollout:
        path = Path(rollout)
    else:
        path = None

    if path is not None:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace").splitlines(), path
        except (OSError, ValueError):
            return [], path

    if isinstance(rollout, str):
        return rollout.splitlines(), None
    return [], None


def _session_meta(lines: list[str]) -> dict:
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict) or event.get("type") != "session_meta":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
        spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
        return {
            "id": str(payload.get("id") or payload.get("session_id") or "").strip(),
            "parent_thread_id": str(payload.get("parent_thread_id") or spawn.get("parent_thread_id") or "").strip(),
            "agent_path": str(payload.get("agent_path") or spawn.get("agent_path") or "").strip(),
            "agent_nickname": str(
                payload.get("agent_nickname")
                or payload.get("nickname")
                or spawn.get("agent_nickname")
                or ""
            ).strip(),
        }
    return {}


def _norm_agent_path(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip().removeprefix("/root/").lstrip("/")


def attest_child_proxy_strict(
    *,
    parent_rollout: str | Path,
    canonical_task_name: str = "/root/gemini_lifecycle_proxy",
    spawn_call_id: str | None = None,
    spawn_timestamp: float | str | None = None,
    expected_parent_thread_id: str,
    expected_nickname: str | None = None,
    sessions_dir: str | Path | None = None,
) -> tuple[bool, dict, str]:
    """Correlate one Codex V2 child and attest exact provenance plus Luna/low runtime.

    Thread identifiers use exact string equality; substring matches are never accepted.
    Agent path and optional nickname are checked independently.
    """
    child_id = proxy_policy.resolve_child_thread_id(
        parent_rollout,
        canonical_task_name=canonical_task_name,
        spawn_call_id=spawn_call_id,
        spawn_timestamp=spawn_timestamp,
    )
    if not child_id:
        return False, {}, "Could not unambiguously correlate the spawned child thread."

    child_path = proxy_policy.find_child_rollout(child_id, sessions_dir=sessions_dir)
    if not child_path:
        return False, {"child_thread_id": child_id}, "Correlated child rollout was not found."

    lines, _ = _read_rollout_lines(child_path)
    meta = _session_meta(lines)
    if not meta:
        return False, {"child_thread_id": child_id}, "Child rollout has no session_meta provenance."

    if meta.get("id") != child_id:
        return False, meta, f"Child session id '{meta.get('id')}' does not exactly match '{child_id}'."

    expected_parent = str(expected_parent_thread_id or "").strip()
    if not expected_parent:
        return False, meta, "Expected parent thread id is required for strict attestation."
    if meta.get("parent_thread_id") != expected_parent:
        return (
            False,
            meta,
            f"Child parent_thread_id '{meta.get('parent_thread_id')}' does not exactly match '{expected_parent}'.",
        )

    if _norm_agent_path(meta.get("agent_path")) != _norm_agent_path(canonical_task_name):
        return False, meta, "Child agent_path does not match the correlated lifecycle proxy task."

    if expected_nickname is not None:
        expected = str(expected_nickname).strip().casefold()
        if not expected or meta.get("agent_nickname", "").casefold() != expected:
            return False, meta, "Child agent_nickname does not match the spawn result nickname."

    valid, runtime, reason = proxy_policy.inspect_rollout_attestation(child_path)
    if not valid:
        return False, {**meta, **runtime}, reason

    result = {
        **meta,
        **runtime,
        "child_thread_id": child_id,
        "child_rollout_path": str(child_path),
        "baseline_line_count": len(lines),
    }
    return True, result, "Strict child provenance and Luna/low runtime attestation confirmed."


def build_activation_probe(nonce: str) -> tuple[str, str]:
    """Build a tool-free followup_task delivery probe and its expected marker."""
    clean = str(nonce or "").strip()
    if not _NONCE_RE.fullmatch(clean):
        raise ValueError("Activation nonce must be 8-128 characters using letters, digits, '.', '_' or '-'.")
    marker = f"{PROXY_ACTIVATION_ACK_PREFIX}{clean}"
    message = (
        "Activation delivery probe only. Do not call tools. Do not inspect files. Do not delegate. "
        f"Reply with exactly: {marker}"
    )
    return message, marker


def inspect_followup_activation(
    rollout: str | Path,
    *,
    baseline_line_count: int,
    expected_marker: str,
) -> tuple[bool, dict, str]:
    """Fail closed unless followup_task starts a new Luna/low turn and delivers its marker.

    The baseline is captured immediately before followup_task. Only later child rollout
    lines count as activation evidence. This detects the Codex failure mode where
    followup_task wakes the child but silently drops its message payload.
    """
    lines, path = _read_rollout_lines(rollout)
    if not lines:
        return False, {}, "Child rollout is unavailable after followup_task."

    try:
        baseline = int(baseline_line_count)
    except (TypeError, ValueError):
        return False, {}, "baseline_line_count must be an integer."
    if baseline < 0 or baseline > len(lines):
        return False, {}, "baseline_line_count is outside the child rollout bounds."

    marker = str(expected_marker or "").strip()
    if not marker.startswith(PROXY_ACTIVATION_ACK_PREFIX):
        return False, {}, "Expected activation marker is invalid."

    new_lines = lines[baseline:]
    if not new_lines:
        return False, {}, "followup_task produced no new child rollout events."

    new_runtime_events = 0
    marker_seen = False
    for line in new_lines:
        if marker in line:
            marker_seen = True
        raw = line.strip()
        if not raw.startswith("{"):
            continue
        try:
            event = json.loads(raw)
        except Exception:
            continue
        if isinstance(event, dict) and event.get("type") in ("turn_context", "thread_settings_applied"):
            new_runtime_events += 1

    if not new_runtime_events:
        return False, {}, "followup_task did not produce a new child turn_context/thread_settings event."

    # Validate the complete appended runtime segment, not only its final event. This
    # rejects any transient switch to Sol/Terra/Astra or non-low effort.
    valid, runtime, reason = proxy_policy.inspect_rollout_attestation("\n".join(new_lines))
    if not valid:
        return False, runtime, f"Activated child turn failed Luna/low attestation: {reason}"

    if not marker_seen:
        return (
            False,
            runtime,
            "Activated child turn did not contain the delivery marker; followup_task payload may have been dropped.",
        )

    payload = {
        **runtime,
        "activation_marker": marker,
        "new_event_count": len(new_lines),
        "new_runtime_event_count": new_runtime_events,
    }
    if path is not None:
        payload["child_rollout_path"] = str(path)
    return True, payload, "followup_task activation and payload delivery were observed on a Luna/low child turn."
