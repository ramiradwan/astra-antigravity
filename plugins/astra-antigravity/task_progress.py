from __future__ import annotations

import dataclasses
import re
from typing import Any

TEST_COMMAND_RE = re.compile(
    r"(?i)(?:^|\s)(?:python\s+-m\s+)?(?:pytest|unittest)(?:\s|$)|"
    r"(?:^|\s)(?:npm|pnpm|yarn)\s+test(?:\s|$)|"
    r"(?:^|\s)(?:cargo|go|dotnet|mvn|gradle)\s+test(?:\s|$)"
)

FILE_PARAMETER_KEYS = (
    "TargetFile",
    "target_file",
    "path",
    "file_path",
    "FilePath",
    "AbsolutePath",
)
COMMAND_PARAMETER_KEYS = (
    "CommandLine",
    "command_line",
    "command",
    "cmd",
)
FILE_TOOL_NAMES = {
    "write_to_file",
    "replace_file_content",
    "replace_file_contents",
    "edit_file",
    "create_file",
    "delete_file",
    "apply_patch",
}


def _clean(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return None
    return value[-limit:]


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first_string(mapping: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _error_text(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        for key in ("message", "error", "detail"):
            text = _clean(value.get(key))
            if text:
                return text
    return None


def _subagent_activity(step: dict, tool_state: str) -> str | None:
    info = _mapping(step.get("subagent_info"))
    subagents = info.get("subagents")
    if not isinstance(subagents, list) or not subagents:
        return None

    labels = []
    for subagent in subagents:
        row = _mapping(subagent)
        label = _clean(row.get("role"), 80) or _clean(row.get("type_name"), 80)
        if label and label not in labels:
            labels.append(label)

    if len(labels) == 1:
        subject = f"{labels[0]} subagent"
    elif labels:
        subject = "subagents " + ", ".join(labels[:3])
    else:
        subject = "subagent"

    if tool_state == "ACTIVE":
        return f"Antigravity {subject} running"
    if tool_state == "DONE":
        return f"Antigravity {subject} completed"
    return f"Antigravity {subject} {tool_state.lower()}" if tool_state else f"Antigravity {subject}"


@dataclasses.dataclass(frozen=True)
class ProgressState:
    sequence: int = 0
    activity: str | None = None
    current_file: str | None = None
    last_change: str | None = None
    last_test_command: str | None = None
    test_state: str | None = None
    source_conversation_id: str | None = None
    source_step_index: int | None = None
    source_step_type: str | None = None
    source_state: str | None = None
    source_tool_name: str | None = None
    source_transition: tuple | None = None
    event_signature: tuple | None = None
    subagent_state: str | None = None
    decision_required: bool = False
    decision_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "activity": self.activity,
            "current_file": self.current_file,
            "last_change": self.last_change,
            "source_step_type": self.event_signature[0] if self.event_signature else None,
            "source_tool_name": self.event_signature[1] if self.event_signature else None,
            "source_state": self.event_signature[2] if self.event_signature else None,
            "source_error": bool(self.event_signature[7]) if self.event_signature else False,
            "subagent_state": self.subagent_state,
            "decision_required": self.decision_required,
            "decision_reason": self.decision_reason,
            "tests": {
                "last_command": self.last_test_command,
                "state": self.test_state,
                "passed": True if self.test_state == "passed" else False if self.test_state == "failed" else None,
            },
        }


def _signature(step: dict, tool_info: dict, parameters: dict) -> tuple:
    tool_name = str(step.get("tool_name") or tool_info.get("name") or "")
    state = str(step.get("state") or "")
    step_type = str(step.get("step_type") or "")
    command = _first_string(parameters, COMMAND_PARAMETER_KEYS)
    target = _first_string(parameters, FILE_PARAMETER_KEYS)
    text = _clean(step.get("text_delta"))
    output = _clean(tool_info.get("output"))
    error = _error_text(tool_info.get("error")) or ("Tool error" if tool_info.get("error") else None)
    return (step_type, tool_name, state, command, target, text, output, error,
            tool_info.get("requires_user_input") is True,
            parameters.get("BlockedOnUser") is True,
            _first_string(parameters, ("question", "Message", "message")))


def _native_transition(step: dict) -> tuple | None:
    conversation_id = step.get("conversation_id")
    step_index = step.get("step_index")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return None
    if not isinstance(step_index, int) or isinstance(step_index, bool):
        return None
    return (
        conversation_id,
        step_index,
        str(step.get("step_type") or ""),
        str(step.get("state") or "").upper(),
    )


def reduce_antigravity_event(state: ProgressState, event: dict) -> ProgressState:
    if not isinstance(event, dict) or event.get("event") != "step_update":
        return state

    step = _mapping(event.get("step_update"))
    tool_info = _mapping(step.get("tool_info"))
    parameters = _mapping(tool_info.get("parameters"))
    source_transition = _native_transition(step)
    signature = _signature(step, tool_info, parameters)
    if source_transition is not None:
        if source_transition == state.source_transition:
            return state
    elif signature == state.event_signature:
        return state

    step_type = str(step.get("step_type") or "")
    tool_name = str(step.get("tool_name") or tool_info.get("name") or "")
    tool_state = str(step.get("state") or "").upper()
    # Only an explicit native input request crosses the model context boundary.
    # A failed command or arbitrary response prose is not a request for help.
    decision_required = step_type == "tool" and (
        tool_state in {"BLOCKED", "WAITING_FOR_INPUT"}
        or tool_info.get("requires_user_input") is True
        or parameters.get("BlockedOnUser") is True
        or tool_name in {"ask_user", "request_user_input"} and tool_state in {"ACTIVE", "RUNNING"}
    )
    decision_reason = None
    if decision_required:
        decision_reason = _first_string(parameters, ("question", "Message", "message"))
        decision_reason = _clean(decision_reason, 180) or "Worker requires parent input"

    # Native streams may emit many ACTIVE text fragments for one response step.
    # They are intentionally not promoted to Astra progress. Legacy events retain
    # the prior behavior because they lack native step identity.
    if source_transition is not None:
        if step_type == "user_input":
            return state
        if step_type == "agent_response" and tool_state != "DONE":
            return state
        if step_type == "agent_response" and not _clean(step.get("text_delta")):
            return state
        if step_type == "checkpoint":
            if tool_state != "DONE":
                return state
            if not (_clean(step.get("text_delta")) or _clean(step.get("summary"))):
                return state

    sequence = state.sequence + 1
    activity = state.activity
    current_file = state.current_file
    last_change = state.last_change
    last_test_command = state.last_test_command
    test_state = state.test_state

    subagent_activity = _subagent_activity(step, tool_state)
    if subagent_activity:
        activity = subagent_activity
    elif step_type == "agent_response":
        activity = _clean(step.get("text_delta")) or activity
    elif step_type == "checkpoint":
        activity = _clean(step.get("text_delta")) or _clean(step.get("summary")) or activity
    elif step_type == "tool":
        command = _first_string(parameters, COMMAND_PARAMETER_KEYS)
        target = _first_string(parameters, FILE_PARAMETER_KEYS)

        if tool_name == "run_command" and command:
            activity = _clean(command) or activity
            if TEST_COMMAND_RE.search(command):
                last_test_command = command
                if tool_state in {"ACTIVE", "RUNNING", "STARTED"}:
                    test_state = "running"
                elif tool_state == "ERROR" or tool_info.get("error"):
                    test_state = "failed"
                elif tool_state in {"DONE", "COMPLETED", "SUCCESS"}:
                    test_state = "passed"
        elif target and (tool_name in FILE_TOOL_NAMES or "file" in tool_name):
            normalized = target.replace("\\", "/")
            current_file = normalized
            activity = f"{tool_name}: {normalized}"
            if tool_state in {"DONE", "COMPLETED", "SUCCESS"}:
                last_change = f"Updated {normalized}"
        else:
            error = _error_text(tool_info.get("error"))
            activity = (
                _clean(step.get("text_delta"))
                or error
                or _clean(tool_info.get("output"))
                or (
                    f"{tool_name} completed"
                    if tool_name and tool_state == "DONE"
                    else f"{tool_name} running"
                    if tool_name and tool_state == "ACTIVE"
                    else activity
                )
            )

    return ProgressState(
        sequence=sequence,
        activity=activity,
        current_file=current_file,
        last_change=last_change,
        last_test_command=last_test_command,
        test_state=test_state,
        source_conversation_id=(source_transition[0] if source_transition else state.source_conversation_id),
        source_step_index=(source_transition[1] if source_transition else state.source_step_index),
        source_step_type=(step_type if source_transition else state.source_step_type),
        source_state=(tool_state if source_transition else state.source_state),
        source_tool_name=(tool_name if source_transition else state.source_tool_name),
        source_transition=source_transition if source_transition is not None else state.source_transition,
        event_signature=signature,
        subagent_state=tool_state if subagent_activity else None,
        decision_required=decision_required,
        decision_reason=decision_reason,
    )
