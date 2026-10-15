"""Credential-free Antigravity CLI fixture for native structured-result tests."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def pause() -> None:
    delay = float(os.environ.get("ASTRA_FAKE_AGY_PROGRESS_DELAY", "0") or 0)
    if delay > 0:
        time.sleep(delay)


def wait_for_progress_ack(turn: int, sequence: int) -> None:
    """Optional test-only handshake; normal fixture callers remain ungated."""
    raw = os.environ.get("ASTRA_FAKE_AGY_PROGRESS_GATE_DIR")
    if not raw:
        return
    gates = Path(raw)
    acknowledged = gates / f"turn-{turn}-sequence-{sequence}.ack"
    released = gates / "release-all"
    deadline = time.monotonic() + 30
    while not acknowledged.is_file() and not released.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Fixture progress acknowledgment timed out: turn {turn}, sequence {sequence}"
            )
        time.sleep(0.01)


def structured(turn: int, resumed: bool = False) -> dict:
    label = "resumed" if resumed else f"turn {turn}"
    return {
        "summary": f"native {label}",
        "tests_run": ["pytest -q"],
        "tests_passed": True,
        "remaining_work": [],
        "risks": [],
    }


def result_payload(conversation_id: str, turn: int, resumed: bool = False) -> dict:
    report = structured(turn, resumed=resumed)
    return {
        "event": "result",
        "result": {
            "conversation_id": conversation_id,
            "status": "SUCCESS",
            "response": json.dumps(report) + "\n",
            "structured_output": report,
            "num_turns": turn,
            "duration_seconds": float(turn),
            "usage": {
                "input_tokens": 100 * turn,
                "output_tokens": 10 * turn,
                "total_tokens": 110 * turn,
            },
        },
    }


def step(conversation_id: str, step_index: int, state: str, step_type: str, **fields) -> dict:
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": conversation_id,
            "step_index": step_index,
            "state": state,
            "step_type": step_type,
            **fields,
        },
    }


def emit_turn_progress(conversation_id: str, turn: int) -> None:
    base = turn * 10
    emit(step(conversation_id, base, "DONE", "user_input"))
    emit(step(conversation_id, base + 1, "ACTIVE", "agent_response", text_delta="Inspecting "))
    emit(step(conversation_id, base + 1, "ACTIVE", "agent_response", text_delta="the repository"))
    pause()
    emit(
        step(
            conversation_id,
            base + 2,
            "ACTIVE",
            "tool",
            tool_name="run_command",
            tool_info={
                "name": "run_command",
                "parameters": {"CommandLine": "pytest -q"},
            },
        )
    )
    wait_for_progress_ack(turn, 1)
    pause()
    emit(
        step(
            conversation_id,
            base + 2,
            "DONE",
            "tool",
            tool_name="run_command",
            tool_info={
                "name": "run_command",
                "parameters": {"CommandLine": "pytest -q"},
                "output": "1 passed",
            },
        )
    )
    wait_for_progress_ack(turn, 2)
    pause()
    subagent_info = {
        "subagents": [
            {
                "type_name": "research",
                "role": "verification",
                "conversation_id": "fixture-subagent-secret",
                "log_uri": "file:///fixture-secret/subagent.log",
                "workspace_uris": ["file:///fixture-secret/worktree"],
            }
        ]
    }
    emit(
        step(
            conversation_id,
            base + 3,
            "ACTIVE",
            "tool",
            tool_name="invoke_subagent",
            subagent_info=subagent_info,
        )
    )
    wait_for_progress_ack(turn, 3)
    pause()
    emit(
        step(
            conversation_id,
            base + 3,
            "DONE",
            "tool",
            tool_name="invoke_subagent",
            subagent_info=subagent_info,
        )
    )
    wait_for_progress_ack(turn, 4)
    # Native checkpoint bookkeeping without useful text must not create progress.
    emit(step(conversation_id, base + 4, "DONE", "checkpoint"))


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--print-timeout")
    parser.add_argument("--output-format")
    parser.add_argument("--json-schema")
    parser.add_argument("--add-dir")
    parser.add_argument("--input-format")
    parser.add_argument("-p", dest="prompt")
    parser.add_argument("--conversation")
    args, _ = parser.parse_known_args()

    invocation_log = os.environ.get("ASTRA_FAKE_AGY_INVOCATION_LOG")
    if invocation_log:
        with open(invocation_log, "a", encoding="utf-8") as handle:
            handle.write(("sandbox" if args.sandbox else "unsandboxed") + "\n")

    expected_sandbox = os.environ.get("ASTRA_FAKE_AGY_EXPECT_SANDBOX")
    if expected_sandbox is not None:
        expected = expected_sandbox.strip().lower() in {"1", "true", "yes", "on"}
        if args.sandbox != expected:
            print(
                f"sandbox expectation mismatch: expected={expected} actual={args.sandbox}",
                file=sys.stderr,
                flush=True,
            )
            return 23

    if args.sandbox and env_enabled("ASTRA_FAKE_AGY_FAIL_WHEN_SANDBOXED"):
        print("simulated sandbox launch failure", file=sys.stderr, flush=True)
        return 24

    schema_path = Path(args.json_schema or "")
    if not schema_path.is_file():
        print("missing native result schema", file=sys.stderr, flush=True)
        return 2
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if set(schema.get("required") or []) != {
        "summary",
        "tests_run",
        "tests_passed",
        "remaining_work",
        "risks",
    }:
        print("unexpected native result schema", file=sys.stderr, flush=True)
        return 2
    if args.output_format != "stream-json":
        print("expected stream-json output", file=sys.stderr, flush=True)
        return 2

    workspace = Path(args.add_dir).resolve() if args.add_dir else Path.cwd().resolve()

    if args.input_format == "stream-json":
        conversation_id = "fake-native-session"
        emit(
            {
                "event": "init",
                "conversation_id": conversation_id,
                "init": {
                    "cwd": str(workspace),
                    "tools": ["run_command", "write_to_file"],
                    "permission_mode": "always-proceed",
                    "json_schema": schema,
                },
            }
        )
        turn = 0
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            if message.get("event") != "user":
                continue
            turn += 1
            target = workspace / "native_result.txt"
            target.write_text(f"turn {turn}\n", encoding="utf-8")
            emit_turn_progress(conversation_id, turn)
            release = os.environ.get("ASTRA_FAKE_AGY_RELEASE_FILE")
            if release:
                emit(step(conversation_id, turn * 10 + 6, "WAITING_FOR_INPUT", "tool",
                          tool_name="request_user_input", tool_info={
                              "name": "request_user_input", "parameters": {"question": "Choose the migration policy"}}))
                deadline = time.monotonic() + 30
                while not Path(release).is_file():
                    if time.monotonic() > deadline:
                        raise RuntimeError("Fixture result release timed out")
                    time.sleep(0.02)
            emit(result_payload(conversation_id, turn))
        return 0

    if args.prompt is not None and args.conversation:
        target = workspace / "native_result.txt"
        target.write_text("resumed\n", encoding="utf-8")
        emit_turn_progress(args.conversation, 2)
        emit(result_payload(args.conversation, 2, resumed=True))
        return 0

    print("unsupported fixture invocation", file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
