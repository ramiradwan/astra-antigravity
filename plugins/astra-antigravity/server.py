"""Event-aware compatibility layer over the proven Antigravity task manager.

The original process/session implementation is preserved in ``server_legacy``.
This module keeps its public API while adding structured progress state and a
condition-driven ``wait_for_update`` primitive used by the long-running MCP
``await_task`` tool.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import server_legacy as legacy
from compatibility import call_supported
from task_progress import ProgressState, reduce_antigravity_event
from task_types import saved_task_type, schema_path

# Public compatibility surface used by quota/worktree coordinators and tests.
DEFAULT_MODEL = legacy.DEFAULT_MODEL
DEFAULT_EFFORT = legacy.DEFAULT_EFFORT
LEGACY_DEFAULT_MODEL = legacy.LEGACY_DEFAULT_MODEL
LEGACY_DEFAULT_EFFORT = legacy.LEGACY_DEFAULT_EFFORT
DEFAULT_TIMEOUT = legacy.DEFAULT_TIMEOUT
DEFAULT_SESSION_IDLE_TTL_SECONDS = legacy.DEFAULT_SESSION_IDLE_TTL_SECONDS
MAX_WAIT_SECONDS = legacy.MAX_WAIT_SECONDS
MAX_EVENT_STRING_CHARS = legacy.MAX_EVENT_STRING_CHARS
DEFAULT_MAX_RESULT_CHARS = legacy.DEFAULT_MAX_RESULT_CHARS
MAX_PROGRESS_LIMIT = legacy.MAX_PROGRESS_LIMIT
DEFAULT_PROGRESS_LIMIT = legacy.DEFAULT_PROGRESS_LIMIT
TERMINAL_TURN_STATUSES = legacy.TERMINAL_TURN_STATUSES
SERVER_INSTANCE_ID = legacy.SERVER_INSTANCE_ID
TaskRecord = legacy.TaskRecord
truncate_value = legacy.truncate_value
find_agy = legacy.find_agy
get_state_dir = legacy.get_state_dir
save_metadata = legacy.save_metadata
_usage_delta = legacy._usage_delta
IMPLEMENTATION_SCHEMA_PATH = schema_path("implementation")


def antigravity_sandbox_enabled() -> bool:
    """Return whether Astra should request Antigravity terminal sandboxing."""
    value = os.environ.get("ASTRA_ANTIGRAVITY_SANDBOX")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _legacy_find_agy_proxy():
    """Preserve the historical ``server.find_agy`` patch seam after the split."""
    return find_agy()


# Inherited legacy methods resolve their defining module globals. Route that one
# dependency back through this module so existing callers/tests that replace
# ``server.find_agy`` continue to affect start_task as they did before the split.
legacy.find_agy = _legacy_find_agy_proxy

mcp = FastMCP("Antigravity")


class TaskManager(legacy.TaskManager):
    """Legacy task manager plus structured, condition-driven progress events."""

    def __init__(self, state_dir=None):
        super().__init__(state_dir)
        self._progress_states: dict[str, ProgressState] = {}
        self._decision_history = {}
        self._decision_listeners = {}

    def _base_command(
        self,
        workspace: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        task_type: str = "implementation",
    ) -> list[str]:
        cmd = [
            find_agy(),
            "--model",
            model or DEFAULT_MODEL,
            "--effort",
            effort or DEFAULT_EFFORT,
            "--dangerously-skip-permissions",
            "--print-timeout",
            DEFAULT_TIMEOUT,
            "--output-format",
            "stream-json",
            "--json-schema",
            str(schema_path(task_type)),
        ]
        if antigravity_sandbox_enabled():
            cmd.append("--sandbox")
        if workspace:
            cmd.extend(["--add-dir", str(workspace)])
        return cmd

    def _launch_session(self, record: TaskRecord) -> subprocess.Popen:
        return subprocess.Popen(
            call_supported(self._base_command, record.workspace, model=record.model,
                           effort=record.effort, task_type=saved_task_type(record))
            + ["--input-format", "stream-json"],
            **self._popen_kwargs(record.workspace, True),
        )

    def _launch_fallback_turn(self, record: TaskRecord, prompt: str) -> subprocess.Popen:
        return subprocess.Popen(
            call_supported(self._base_command, record.workspace, model=record.model,
                           effort=record.effort, task_type=saved_task_type(record))
            + ["-p", prompt, "--conversation", record.conversation_id],
            **self._popen_kwargs(record.workspace, False),
        )

    def _record_result(self, record: TaskRecord, result: dict, prompt: str) -> str:
        state = super()._record_result(record, result, prompt)
        structured = result.get("structured_output")
        if record.turns:
            record.turns[-1]["task_type"] = saved_task_type(record)
            record.turns[-1]["structured_output"] = (
                dict(structured) if isinstance(structured, dict) else None
            )
        return state

    def structured_output(self, task_id: str) -> dict | None:
        """Return native structured output for the task's current completed turn."""
        record = self.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")
        if not record.turns:
            return None
        latest = record.turns[-1]
        if latest.get("turn_index") != record.current_turn_index:
            return None
        structured = latest.get("structured_output")
        return dict(structured) if isinstance(structured, dict) else None

    def start_task(
        self,
        task_prompt: str,
        workspace: str,
        model: str | None = None,
        effort: str | None = None,
        task_type: str = "implementation",
        required_capabilities: list[str] | None = None,
    ) -> TaskRecord:
        """Start a task without allowing an already-dead child to escape as running.

        The legacy starter intentionally returns quickly. If the child has already
        exited during that short startup window, the stdout reader may still be
        publishing its final result/failure on another thread. Reconcile that
        boundary before returning so callers never observe a dead process as an
        apparently healthy running task.
        """
        record = super().start_task(task_prompt, workspace, model=model, effort=effort,
                                    task_type=task_type, required_capabilities=required_capabilities)
        proc = self._session_processes.get(record.task_id)
        if not proc or proc.poll() is None or record.status in TERMINAL_TURN_STATUSES:
            return record

        # The process is already dead. Give the reader a bounded opportunity to
        # consume any final result event and publish the authoritative terminal
        # state before synthesizing an EOF failure ourselves.
        deadline = time.monotonic() + 1.0
        with self._state_changed:
            while record.status not in TERMINAL_TURN_STATUSES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state_changed.wait(timeout=remaining)

            if record.status not in TERMINAL_TURN_STATUSES:
                record.status = "failed"
                record.error = (
                    record.error
                    or "Persistent Antigravity session ended before a result event."
                )
                record.session_alive = False
                record.exit_code = getattr(proc, "returncode", None)
                self._release_workspace(record)
                self._notify(record)

        if record.status == "failed":
            raise RuntimeError(f"Antigravity task failed to start: {record.error}")
        return record

    def _publish_progress(self, record: TaskRecord, event: dict) -> ProgressState:
        previous = self._progress_states.get(record.task_id, ProgressState())
        current = reduce_antigravity_event(previous, event)
        if current.sequence > previous.sequence:
            self._progress_states[record.task_id] = current
            if current.decision_required:
                update = {"current_turn": record.current_turn_index, **current.to_dict()}
                history = self._decision_history.setdefault(record.task_id, deque(maxlen=8))
                history.append(update)
                for callback in tuple(self._decision_listeners.get(record.task_id, ())):
                    try:
                        callback(dict(update))
                    except Exception:
                        # A closed receiver/loop must not kill the process reader.
                        listeners = self._decision_listeners.get(record.task_id)
                        if listeners:
                            listeners.discard(callback)
            self._notify(record)
        return current

    def subscribe_decisions(self, task_id, callback):
        """Replay startup input requests and subscribe at the process-reader boundary."""
        with self._lock:
            record = self.get_task(task_id)
            if record is None:
                raise ValueError(f"Task '{task_id}' not found")
            listeners = self._decision_listeners.setdefault(task_id, set())
            listeners.add(callback)
            for update in self._decision_history.get(task_id, ()):
                if update["current_turn"] == record.current_turn_index:
                    callback(dict(update))

        def unsubscribe():
            with self._lock:
                listeners.discard(callback)
                if not listeners:
                    self._decision_listeners.pop(task_id, None)
        return unsubscribe

    def progress_state(self, task_id: str) -> dict:
        record = self.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")
        state = self._progress_states.get(task_id, ProgressState())
        return {
            "task_id": task_id,
            "status": record.status,
            "current_turn": record.current_turn_index,
            **state.to_dict(),
            "updated_at": record.updated_at,
        }

    def wait_for_update(
        self,
        task_id: str,
        after_sequence: int = 0,
        timeout_seconds: float = 30.0,
    ) -> dict:
        """Block until structured progress advances or the task is terminal."""
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError):
            raise ValueError("timeout_seconds must be a number.")
        if not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be finite.")
        try:
            after_sequence = max(0, int(after_sequence))
        except (TypeError, ValueError):
            raise ValueError("after_sequence must be an integer.")

        deadline = time.monotonic() + max(0.1, min(timeout_seconds, MAX_WAIT_SECONDS))
        with self._state_changed:
            while True:
                record = self.get_task(task_id)
                if not record:
                    raise ValueError(f"Task '{task_id}' not found.")
                state = self._progress_states.get(task_id, ProgressState())
                terminal = record.status in TERMINAL_TURN_STATUSES
                if state.sequence > after_sequence or terminal:
                    return {
                        "task_id": task_id,
                        "status": record.status,
                        "terminal": terminal,
                        "timed_out": False,
                        "current_turn": record.current_turn_index,
                        **state.to_dict(),
                        "error": record.error,
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "task_id": task_id,
                        "status": record.status,
                        "terminal": False,
                        "timed_out": True,
                        "current_turn": record.current_turn_index,
                        **state.to_dict(),
                        "error": record.error,
                    }
                self._state_changed.wait(timeout=remaining)

    def _run_session(self, record: TaskRecord, proc: subprocess.Popen) -> None:
        stderr = deque(maxlen=32)
        err_thread = threading.Thread(
            target=self._read_stderr, args=(proc, stderr), daemon=True
        )
        err_thread.start()
        response = ""
        try:
            with open(record.log_path, "a", encoding="utf-8") as log:
                for raw in iter(proc.stdout.readline, b""):
                    line = (
                        raw.decode("utf-8", errors="replace")
                        if isinstance(raw, bytes)
                        else str(raw)
                    )
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line.strip())
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("event")
                    if kind == "init":
                        cid = event.get("conversation_id") or event.get("init", {}).get(
                            "conversation_id"
                        )
                        with self._lock:
                            if cid:
                                record.conversation_id = cid
                            self._notify(record)
                    elif kind == "step_update":
                        delta = event.get("step_update", {}).get("text_delta")
                        with self._lock:
                            if delta:
                                response += delta
                                record.latest_response = response
                            self._publish_progress(record, event)
                    elif kind == "result":
                        result = event.get("result", {})
                        with self._lock:
                            if result.get("response"):
                                response = result["response"]
                            state = self._record_result(
                                record, result, record.active_prompt or ""
                            )
                            if record.status == "cancelled":
                                self._release_workspace(record)
                                self._notify(record)
                                break
                            if state != "completed":
                                record.status = state
                                record.error = (
                                    result.get("error")
                                    or result.get("status")
                                    or "Antigravity turn failed."
                                )
                                record.queued_instruction = None
                                self._release_workspace(record)
                                self._notify(record)
                                try:
                                    if proc.stdin and not getattr(
                                        proc.stdin, "closed", False
                                    ):
                                        proc.stdin.close()
                                except Exception:
                                    pass
                                break
                            queued = record.queued_instruction
                            if queued:
                                record.queued_instruction = None
                                record.current_turn_index += 1
                                record.active_prompt = queued
                                record.latest_response = ""
                                record.status = "running"
                                response = ""
                                self._notify(record)
                            else:
                                record.status = "completed"
                                record.active_prompt = None
                                self._release_workspace(record)
                                self._schedule_idle_close(record)
                                self._notify(record)
                                response = ""
                                continue
                        try:
                            self._send_prompt(record, queued)
                        except Exception as exc:
                            with self._lock:
                                record.status = "failed"
                                record.error = (
                                    f"Failed to send queued prompt to persistent session: {exc}"
                                )
                                self._release_workspace(record)
                                self._notify(record)
                            break
        except Exception as exc:
            with self._lock:
                if record.status not in ("cancelled", "completed"):
                    record.status = "failed"
                    record.error = str(exc)[:4096]
                    self._release_workspace(record)
                    self._notify(record)
        finally:
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            err_thread.join(timeout=2)
            with self._lock:
                self._cancel_idle_timer(record.task_id)
                self._session_processes.pop(record.task_id, None)
                self._session_threads.pop(record.task_id, None)
                record.session_alive = False
                record.exit_code = getattr(proc, "returncode", None)
                if record.status in ("started", "running"):
                    record.status = "failed"
                    record.error = (
                        "".join(stderr).strip()
                        or "Persistent Antigravity session ended before a result event."
                    )
                    self._release_workspace(record)
                self._notify(record)

    def _run_fallback(self, record: TaskRecord, prompt: str, proc: subprocess.Popen) -> None:
        stderr = deque(maxlen=32)
        err_thread = threading.Thread(
            target=self._read_stderr, args=(proc, stderr), daemon=True
        )
        err_thread.start()
        result = None
        response = ""
        try:
            with open(record.log_path, "a", encoding="utf-8") as log:
                for raw in iter(proc.stdout.readline, b""):
                    line = (
                        raw.decode("utf-8", errors="replace")
                        if isinstance(raw, bytes)
                        else str(raw)
                    )
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line.strip())
                    except Exception:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("event") == "step_update":
                        response += event.get("step_update", {}).get("text_delta") or ""
                        with self._lock:
                            record.latest_response = response
                            self._publish_progress(record, event)
                    elif event.get("event") == "result":
                        result = event.get("result", {})
                        response = result.get("response") or response
            proc.wait()
            err_thread.join(timeout=2)
            with self._lock:
                record.latest_response = response
                if record.status != "cancelled":
                    if result:
                        state = self._record_result(record, result, prompt)
                        record.status = state
                        record.error = None if state == "completed" else (
                            result.get("error")
                            or result.get("status")
                            or "Antigravity fallback turn failed."
                        )
                    else:
                        record.status = "failed"
                        record.error = (
                            "".join(stderr).strip()
                            or "Fallback turn ended without a result event."
                        )
                self._release_workspace(record)
                self._notify(record)
        except Exception as exc:
            with self._lock:
                record.status = "failed"
                record.error = str(exc)[:4096]
                self._release_workspace(record)
                self._notify(record)
        finally:
            with self._lock:
                self._session_processes.pop(record.task_id, None)
                self._session_threads.pop(record.task_id, None)
                record.pid = None
                record.session_alive = False
                record.exit_code = getattr(proc, "returncode", None)
                self._notify(record)


manager = TaskManager()


@mcp.tool()
def delegate_to_gemini(task: str, workspace: str) -> dict:
    r = manager.start_task(task, workspace)
    return {
        "task_id": r.task_id,
        "status": r.status,
        "pid": r.pid,
        "workspace": r.workspace,
        "log": r.log_path,
        "conversation_id": r.conversation_id,
        "session_alive": r.session_alive,
    }


@mcp.tool()
def wait_for_task(task_id: str, timeout_seconds: float = 30.0) -> dict:
    return manager.wait_for_task(task_id, timeout_seconds)


@mcp.tool()
def get_task_status(task_id: str) -> dict:
    r = manager.get_task(task_id)
    if not r:
        raise ValueError(f"Task '{task_id}' not found.")
    return {
        "task_id": r.task_id,
        "status": r.status,
        "conversation_id": r.conversation_id,
        "workspace": r.workspace,
        "current_turn": r.current_turn_index,
        "has_queued_instruction": bool(r.queued_instruction),
        "pid": r.pid,
        "exit_code": r.exit_code,
        "error": r.error,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "session_alive": r.session_alive,
        "is_owned": r.task_id in manager._session_processes,
        "progress": manager.progress_state(task_id),
    }


@mcp.tool()
def read_task_progress(
    task_id: str, offset: int = 0, limit: int = DEFAULT_PROGRESS_LIMIT
) -> dict:
    return manager.read_progress(task_id, offset, limit)


@mcp.tool()
def get_task_result(task_id: str, max_chars: int = DEFAULT_MAX_RESULT_CHARS) -> dict:
    return manager.get_result(task_id, max_chars)


@mcp.tool()
def steer_gemini(task_id: str, instruction: str) -> dict:
    return manager.steer_task(task_id, instruction)


@mcp.tool()
def follow_up_gemini(task_id: str, instruction: str) -> dict:
    return manager.steer_task(task_id, instruction)


@mcp.tool()
def cancel_task(task_id: str) -> dict:
    return manager.cancel_task(task_id)


if __name__ == "__main__":
    mcp.run()
