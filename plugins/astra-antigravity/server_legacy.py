import dataclasses
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from process_lifecycle import stop_owned_process
from task_types import capability_requirements, validate_task_type

mcp = FastMCP("Antigravity")

DEFAULT_MODEL = "gemini-4-argon-high"
DEFAULT_EFFORT = "high"
LEGACY_DEFAULT_MODEL = "gemini-3.8-flash-high"
LEGACY_DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT = "30m"
DEFAULT_SESSION_IDLE_TTL_SECONDS = 600.0
MAX_WAIT_SECONDS = 45.0
MAX_EVENT_STRING_CHARS = 4096
DEFAULT_MAX_RESULT_CHARS = 20000
MAX_PROGRESS_LIMIT = 100
DEFAULT_PROGRESS_LIMIT = 50
TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "cancelled", "unknown"})
SERVER_INSTANCE_ID = uuid.uuid4().hex


def truncate_value(value: Any, max_len: int = MAX_EVENT_STRING_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_len else value[:max_len] + f"... [truncated {len(value)-max_len} chars]"
    if isinstance(value, dict):
        return {k: truncate_value(v, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [truncate_value(v, max_len) for v in value]
    return value


def find_agy() -> str:
    from launcher import discover_agy
    return discover_agy(required=True)


def get_state_dir() -> Path:
    raw = os.environ.get("ASTRA_ANTIGRAVITY_STATE_DIR")
    path = Path(raw).expanduser().resolve() if raw else Path(tempfile.gettempdir()) / "astra-antigravity-tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_metadata(record: "TaskRecord") -> None:
    record.updated_at = time.time()
    target = Path(record.meta_path)
    tmp = target.with_suffix(f".tmp-{uuid.uuid4().hex[:6]}")
    try:
        tmp.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
        try:
            tmp.replace(target)
        except OSError:
            target.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _usage_delta(current: dict | None, previous: dict | None) -> dict | None:
    if not isinstance(current, dict):
        return None
    previous = previous if isinstance(previous, dict) else {}
    return {
        k: (v - previous.get(k, 0) if isinstance(v, (int, float)) and isinstance(previous.get(k, 0), (int, float)) else v)
        for k, v in current.items()
    }


@dataclasses.dataclass
class TaskRecord:
    task_id: str
    workspace: str
    status: str
    created_at: float
    updated_at: float
    server_instance_id: str
    log_path: str
    meta_path: str
    conversation_id: str | None = None
    current_turn_index: int = 1
    turns: list[dict] = dataclasses.field(default_factory=list)
    queued_instruction: str | None = None
    latest_response: str = ""
    error: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    session_alive: bool = False
    active_prompt: str | None = None
    previous_cumulative_usage: dict | None = None
    previous_cumulative_duration: float | None = None
    last_activity_at: float | None = None
    model: str = LEGACY_DEFAULT_MODEL
    effort: str = LEGACY_DEFAULT_EFFORT
    task_type: str = "implementation"
    required_capabilities: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        names = {f.name for f in dataclasses.fields(cls)}
        cleaned = {k: v for k, v in data.items() if k in names}
        if "model" not in cleaned or not cleaned["model"]:
            cleaned["model"] = LEGACY_DEFAULT_MODEL
        if "effort" not in cleaned or not cleaned["effort"]:
            cleaned["effort"] = LEGACY_DEFAULT_EFFORT
        return cls(**cleaned)


class TaskManager:
    def __init__(self, state_dir: Path | None = None):
        self.state_dir = state_dir or get_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state_changed = threading.Condition(self._lock)
        self._session_processes: dict[str, subprocess.Popen] = {}
        self._session_threads: dict[str, threading.Thread] = {}
        self._active_processes = self._session_processes
        self._threads = self._session_threads
        self._tasks: dict[str, TaskRecord] = {}
        self._active_workspaces: dict[str, str] = {}
        self._idle_timers: dict[str, threading.Timer] = {}
        self.initial_wait_seconds = 1.5
        self.session_idle_ttl_seconds = DEFAULT_SESSION_IDLE_TTL_SECONDS

    def _notify(self, record: TaskRecord) -> None:
        record.last_activity_at = time.time()
        save_metadata(record)
        self._state_changed.notify_all()

    @staticmethod
    def _workspace_key(workspace: str) -> str:
        return str(Path(workspace).resolve()).lower()

    def _reserve_workspace(self, record: TaskRecord) -> None:
        key = self._workspace_key(record.workspace)
        owner = self._active_workspaces.get(key)
        if owner and owner != record.task_id:
            raise ValueError(
                f"Workspace '{record.workspace}' is currently locked by active task '{owner}'. "
                "Concurrent turns in the same workspace are not permitted."
            )
        self._active_workspaces[key] = record.task_id

    def _release_workspace(self, record: TaskRecord) -> None:
        key = self._workspace_key(record.workspace)
        if self._active_workspaces.get(key) == record.task_id:
            del self._active_workspaces[key]

    def _base_command(
        self,
        workspace: str | None = None,
        model: str | None = None,
        effort: str | None = None,
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
        ]
        if workspace:
            cmd.extend(["--add-dir", str(workspace)])
        return cmd

    def _popen_kwargs(self, workspace: str, streaming: bool) -> dict:
        kwargs = {
            "cwd": workspace,
            "stdin": subprocess.PIPE if streaming else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        return kwargs

    def _launch_session(self, record: TaskRecord) -> subprocess.Popen:
        return subprocess.Popen(
            self._base_command(record.workspace, model=record.model, effort=record.effort)
            + ["--input-format", "stream-json"],
            **self._popen_kwargs(record.workspace, True),
        )

    def _launch_fallback_turn(self, record: TaskRecord, prompt: str) -> subprocess.Popen:
        return subprocess.Popen(
            self._base_command(record.workspace, model=record.model, effort=record.effort)
            + ["-p", prompt, "--conversation", record.conversation_id],
            **self._popen_kwargs(record.workspace, False),
        )

    def _send_prompt(self, record: TaskRecord, prompt: str) -> None:
        proc = self._session_processes.get(record.task_id)
        if not proc or proc.poll() is not None or not proc.stdin or getattr(proc.stdin, "closed", False):
            raise RuntimeError("Persistent Antigravity session is not writable.")
        payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
        try:
            proc.stdin.write(payload.encode("utf-8"))
        except TypeError:
            proc.stdin.write(payload)
        proc.stdin.flush()

    def _cancel_idle_timer(self, task_id: str) -> None:
        timer = self._idle_timers.pop(task_id, None)
        if timer:
            timer.cancel()

    def _schedule_idle_close(self, record: TaskRecord) -> None:
        self._cancel_idle_timer(record.task_id)
        if self.session_idle_ttl_seconds <= 0:
            return
        timer = threading.Timer(self.session_idle_ttl_seconds, self._close_idle_session, args=(record.task_id,))
        timer.daemon = True
        self._idle_timers[record.task_id] = timer
        timer.start()

    def _close_idle_session(self, task_id: str) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            proc = self._session_processes.get(task_id)
            if not record or record.status != "completed" or not record.session_alive or not proc:
                return
            self._idle_timers.pop(task_id, None)
            stdin = proc.stdin
        try:
            if stdin and not getattr(stdin, "closed", False):
                stdin.close()
        except Exception:
            pass

    def _record_result(self, record: TaskRecord, result: dict, prompt: str) -> str:
        cumulative_usage = result.get("usage")
        cumulative_duration = result.get("duration_seconds")
        duration_delta = None
        if isinstance(cumulative_duration, (int, float)):
            duration_delta = cumulative_duration - (record.previous_cumulative_duration or 0.0)
        if result.get("conversation_id"):
            record.conversation_id = result["conversation_id"]
        response = result.get("response") or record.latest_response or ""
        status = result.get("status")
        record.turns.append({
            "turn_index": record.current_turn_index,
            "prompt": prompt,
            "status": "completed" if status == "SUCCESS" else "failed",
            "response": response,
            "result_status": status,
            "num_turns_cumulative": result.get("num_turns"),
            "usage_cumulative": cumulative_usage,
            "usage_delta": _usage_delta(cumulative_usage, record.previous_cumulative_usage),
            "duration_cumulative": cumulative_duration,
            "duration_delta": duration_delta,
            "model": record.model,
            "effort": record.effort,
        })
        record.latest_response = response
        record.previous_cumulative_usage = cumulative_usage
        record.previous_cumulative_duration = cumulative_duration
        if status == "SUCCESS":
            return "completed"
        if status in ("CANCELED", "INTERRUPTED"):
            return "cancelled"
        return "failed"

    @staticmethod
    def _read_stderr(proc: subprocess.Popen, chunks: deque) -> None:
        try:
            for line in iter(proc.stderr.readline, b""):
                text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                chunks.append(text[-4096:])
        except Exception:
            pass

    def _run_session(self, record: TaskRecord, proc: subprocess.Popen) -> None:
        stderr = deque(maxlen=32)
        err_thread = threading.Thread(target=self._read_stderr, args=(proc, stderr), daemon=True)
        err_thread.start()
        response = ""
        try:
            with open(record.log_path, "a", encoding="utf-8") as log:
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
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
                        cid = event.get("conversation_id") or event.get("init", {}).get("conversation_id")
                        with self._lock:
                            if cid:
                                record.conversation_id = cid
                            self._notify(record)
                    elif kind == "step_update":
                        delta = event.get("step_update", {}).get("text_delta")
                        if delta:
                            response += delta
                            with self._lock:
                                record.latest_response = response
                    elif kind == "result":
                        result = event.get("result", {})
                        with self._lock:
                            if result.get("response"):
                                response = result["response"]
                            state = self._record_result(record, result, record.active_prompt or "")
                            if record.status == "cancelled":
                                self._release_workspace(record)
                                self._notify(record)
                                break
                            if state != "completed":
                                record.status = state
                                record.error = result.get("error") or result.get("status") or "Antigravity turn failed."
                                record.queued_instruction = None
                                self._release_workspace(record)
                                self._notify(record)
                                try:
                                    if proc.stdin and not getattr(proc.stdin, "closed", False):
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
                                record.error = f"Failed to send queued prompt: {exc}"
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
                    record.error = "".join(stderr).strip() or "Persistent Antigravity session ended before a result event."
                    self._release_workspace(record)
                self._notify(record)

    def _run_fallback(self, record: TaskRecord, prompt: str, proc: subprocess.Popen) -> None:
        stderr = deque(maxlen=32)
        err_thread = threading.Thread(target=self._read_stderr, args=(proc, stderr), daemon=True)
        err_thread.start()
        result = None
        response = ""
        try:
            with open(record.log_path, "a", encoding="utf-8") as log:
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
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
                            result.get("error") or result.get("status") or "Antigravity fallback turn failed."
                        )
                    else:
                        record.status = "failed"
                        record.error = "".join(stderr).strip() or "Fallback turn ended without a result event."
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

    def start_task(
        self,
        task_prompt: str,
        workspace: str,
        model: str | None = None,
        effort: str | None = None,
        task_type: str = "implementation",
        required_capabilities: list[str] | None = None,
    ) -> TaskRecord:
        validate_task_type(task_type)
        required_capabilities = capability_requirements(task_type, required_capabilities)
        if not task_prompt.strip():
            raise ValueError("Task prompt must not be empty.")
        workspace_path = Path(workspace).expanduser().resolve()
        if not workspace_path.is_dir():
            raise ValueError(f"Workspace does not exist: {workspace_path}")
        find_agy()
        now = time.time()
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        record = TaskRecord(
            task_id=task_id,
            workspace=str(workspace_path),
            status="started",
            created_at=now,
            updated_at=now,
            server_instance_id=SERVER_INSTANCE_ID,
            log_path=str(self.state_dir / f"{task_id}.log"),
            meta_path=str(self.state_dir / f"{task_id}.json"),
            active_prompt=task_prompt,
            model=model or DEFAULT_MODEL,
            effort=effort or DEFAULT_EFFORT,
            task_type=task_type,
            required_capabilities=required_capabilities,
        )
        with self._lock:
            self._reserve_workspace(record)
            self._tasks[task_id] = record
            self._notify(record)
        try:
            proc = self._launch_session(record)
        except Exception:
            with self._lock:
                self._release_workspace(record)
                self._tasks.pop(task_id, None)
            raise
        with self._lock:
            record.pid = proc.pid
            record.session_alive = True
            record.status = "running"
            self._session_processes[task_id] = proc
            thread = threading.Thread(target=self._run_session, args=(record, proc), daemon=True)
            self._session_threads[task_id] = thread
            thread.start()
            self._notify(record)
        try:
            self._send_prompt(record, task_prompt)
        except Exception as exc:
            with self._lock:
                record.status = "failed"
                record.error = str(exc)
                self._release_workspace(record)
                self._notify(record)
            try:
                proc.terminate()
            except Exception:
                pass
            raise
        if self.initial_wait_seconds > 0:
            time.sleep(self.initial_wait_seconds)
        with self._lock:
            if record.status == "failed":
                raise RuntimeError(f"Antigravity task failed to start: {record.error}")
            return record

    def steer_task(self, task_id: str, instruction: str) -> dict:
        if not instruction.strip():
            raise ValueError("Instruction must not be empty.")
        with self._lock:
            record = self.get_task(task_id)
            if not record:
                raise ValueError(f"Task '{task_id}' not found.")
            if record.status in ("started", "running"):
                if record.queued_instruction:
                    raise ValueError("A follow-up is already queued.")
                record.queued_instruction = instruction
                self._notify(record)
                return {
                    "task_id": task_id, "status": "queued", "queued": True,
                    "live_interruption": False, "turn_index": record.current_turn_index + 1,
                    "conversation_id": record.conversation_id,
                }
            if record.status != "completed":
                raise ValueError(f"Cannot steer task '{task_id}' in status '{record.status}'.")
            if not record.conversation_id:
                raise ValueError(f"Cannot steer task '{task_id}': conversation ID was not captured.")
            self._reserve_workspace(record)
            self._cancel_idle_timer(task_id)
            record.server_instance_id = SERVER_INSTANCE_ID
            record.current_turn_index += 1
            record.active_prompt = instruction
            record.latest_response = ""
            record.error = None
            record.exit_code = None
            record.status = "running"
            proc = self._session_processes.get(task_id)
            hot = bool(
                record.session_alive and proc and proc.poll() is None and proc.stdin
                and not getattr(proc.stdin, "closed", False)
            )
            self._notify(record)
        if hot:
            try:
                self._send_prompt(record, instruction)
            except Exception as exc:
                with self._lock:
                    record.status = "failed"
                    record.error = f"Failed to send follow-up to persistent session: {exc}"
                    self._release_workspace(record)
                    self._notify(record)
                raise
            return {
                "task_id": task_id, "status": "running", "queued": False,
                "live_interruption": False, "turn_index": record.current_turn_index,
                "conversation_id": record.conversation_id, "session_reused": True, "pid": record.pid,
            }
        try:
            proc = self._launch_fallback_turn(record, instruction)
        except Exception as exc:
            with self._lock:
                record.status = "failed"
                record.error = str(exc)
                self._release_workspace(record)
                self._notify(record)
            raise
        with self._lock:
            record.pid = proc.pid
            record.session_alive = False
            self._session_processes[task_id] = proc
            thread = threading.Thread(target=self._run_fallback, args=(record, instruction, proc), daemon=True)
            self._session_threads[task_id] = thread
            thread.start()
            self._notify(record)
        return {
            "task_id": task_id, "status": "running", "queued": False,
            "live_interruption": False, "turn_index": record.current_turn_index,
            "conversation_id": record.conversation_id, "session_reused": False, "pid": record.pid,
        }

    def cancel_task(self, task_id: str) -> dict:
        with self._lock:
            record = self.get_task(task_id)
            if not record:
                raise ValueError(f"Task '{task_id}' not found.")
            proc = self._session_processes.get(task_id)
            if record.status in ("failed", "cancelled", "unknown"):
                return {"task_id": task_id, "status": record.status, "cancelled": False}
            if record.status == "completed":
                record.queued_instruction = None
                self._cancel_idle_timer(task_id)
                try:
                    if proc and proc.stdin and not getattr(proc.stdin, "closed", False):
                        proc.stdin.close()
                    stop_owned_process(proc)
                except Exception as exc:
                    return {"task_id": task_id, "status": "completed", "cancelled": False,
                            "error": str(exc)[:500]}
                record.session_alive = False
                record.pid = None
                self._notify(record)
                return {"task_id": task_id, "status": "completed", "cancelled": False, "session_closed": bool(proc)}
            record.status = "cancelled"
            record.queued_instruction = None
            self._release_workspace(record)
            self._cancel_idle_timer(task_id)
            self._notify(record)
        if proc and proc.poll() is None:
            try:
                stop_owned_process(proc)
            except Exception as exc:
                return {"task_id": task_id, "status": "cancelled", "cancelled": True, "error": str(exc)[:500]}
        return {"task_id": task_id, "status": "cancelled", "cancelled": True}

    def get_task(self, task_id: str) -> TaskRecord | None:
        if not re.fullmatch(r"task-[A-Za-z0-9-]+", task_id):
            raise ValueError("Invalid task ID.")
        with self._lock:
            if task_id in self._tasks:
                return self._tasks[task_id]
            path = self.state_dir / f"{task_id}.json"
            if not path.is_file():
                return None
            try:
                record = TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                return None
            if record.server_instance_id != SERVER_INSTANCE_ID:
                if record.status in ("started", "running"):
                    record.status = "unknown"
                    record.error = "Process ownership lost across server restart."
                record.session_alive = False
                record.pid = None
            self._tasks[task_id] = record
            return record

    def wait_for_task(self, task_id: str, timeout_seconds: float = 30.0) -> dict:
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError):
            raise ValueError("timeout_seconds must be a number.")
        if not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be finite.")
        deadline = time.monotonic() + max(0.1, min(timeout_seconds, MAX_WAIT_SECONDS))
        with self._state_changed:
            while True:
                record = self.get_task(task_id)
                if not record:
                    raise ValueError(f"Task '{task_id}' not found.")
                if record.status in TERMINAL_TURN_STATUSES:
                    return {
                        "task_id": task_id, "status": record.status, "finished": True,
                        "conversation_id": record.conversation_id, "current_turn": record.current_turn_index,
                        "session_alive": record.session_alive, "error": record.error,
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "task_id": task_id, "status": record.status, "finished": False,
                        "conversation_id": record.conversation_id, "current_turn": record.current_turn_index,
                        "session_alive": record.session_alive, "error": record.error,
                    }
                self._state_changed.wait(timeout=remaining)

    def read_progress(self, task_id: str, offset: int = 0, limit: int = DEFAULT_PROGRESS_LIMIT) -> dict:
        record = self.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")
        offset = max(0, offset)
        limit = max(1, min(limit, MAX_PROGRESS_LIMIT))
        events, total = [], 0
        path = Path(record.log_path)
        if path.is_file():
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.endswith("\n") and record.status in ("started", "running"):
                        break
                    raw = line.strip()
                    if not raw:
                        continue
                    index, total = total, total + 1
                    if index < offset or len(events) >= limit:
                        continue
                    try:
                        event = json.loads(raw)
                        if not isinstance(event, dict):
                            event = {"event": "raw_log", "content": raw[:4096]}
                    except json.JSONDecodeError:
                        event = {"event": "raw_log", "content": raw[:4096]}
                    events.append(truncate_value(event))
        next_offset = offset + len(events)
        return {
            "task_id": task_id, "status": record.status, "conversation_id": record.conversation_id,
            "current_turn": record.current_turn_index, "offset": offset, "limit": limit,
            "total_events": total, "next_offset": next_offset, "has_more": next_offset < total, "events": events,
        }

    def get_result(self, task_id: str, max_chars: int = DEFAULT_MAX_RESULT_CHARS) -> dict:
        record = self.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")
        max_chars = max(100, min(max_chars, 100000))
        response = record.latest_response or ""
        truncated = len(response) > max_chars
        if truncated:
            response = response[:max_chars] + f"\n... [Truncated: {len(record.latest_response)-max_chars} additional characters]"
        turn = record.turns[-1] if record.turns else None
        return {
            "task_id": task_id, "status": record.status, "conversation_id": record.conversation_id,
            "num_turns": record.current_turn_index, "response": response, "truncated": truncated,
            "duration_seconds": turn.get("duration_delta") if turn else None,
            "duration_cumulative": turn.get("duration_cumulative") if turn else None,
            "usage": turn.get("usage_delta") if turn else None,
            "usage_cumulative": turn.get("usage_cumulative") if turn else None,
            "error": record.error, "log_path": record.log_path,
            "session_alive": record.session_alive, "pid": record.pid,
        }


manager = TaskManager()


@mcp.tool()
def delegate_to_gemini(task: str, workspace: str) -> dict:
    """Start one persistent Gemini 3.8 Flash High Antigravity session."""
    r = manager.start_task(task, workspace)
    return {
        "task_id": r.task_id, "status": r.status, "pid": r.pid, "workspace": r.workspace,
        "log": r.log_path, "conversation_id": r.conversation_id, "session_alive": r.session_alive,
    }


@mcp.tool()
def wait_for_task(task_id: str, timeout_seconds: float = 30.0) -> dict:
    """Block until the current Gemini turn finishes or the bounded wait expires."""
    return manager.wait_for_task(task_id, timeout_seconds)


@mcp.tool()
def get_task_status(task_id: str) -> dict:
    """Inspect current-turn and persistent-session lifecycle state."""
    r = manager.get_task(task_id)
    if not r:
        raise ValueError(f"Task '{task_id}' not found.")
    return {
        "task_id": r.task_id, "status": r.status, "conversation_id": r.conversation_id,
        "workspace": r.workspace, "current_turn": r.current_turn_index,
        "has_queued_instruction": bool(r.queued_instruction),
        "queued_instruction_preview": (
            r.queued_instruction[:100] + "..." if r.queued_instruction and len(r.queued_instruction) > 100
            else r.queued_instruction
        ),
        "pid": r.pid, "exit_code": r.exit_code, "error": r.error,
        "created_at": r.created_at, "updated_at": r.updated_at,
        "session_alive": r.session_alive, "is_owned": r.task_id in manager._session_processes,
    }


@mcp.tool()
def read_task_progress(task_id: str, offset: int = 0, limit: int = DEFAULT_PROGRESS_LIMIT) -> dict:
    """Read bounded Antigravity NDJSON progress events across turns."""
    return manager.read_progress(task_id, offset, limit)


@mcp.tool()
def get_task_result(task_id: str, max_chars: int = DEFAULT_MAX_RESULT_CHARS) -> dict:
    """Return latest response plus per-turn and cumulative usage metadata."""
    return manager.get_result(task_id, max_chars)


@mcp.tool()
def steer_gemini(task_id: str, instruction: str) -> dict:
    """Follow up on the same task, reusing the hot Antigravity session when available."""
    return manager.steer_task(task_id, instruction)


@mcp.tool()
def follow_up_gemini(task_id: str, instruction: str) -> dict:
    """Alias for steer_gemini."""
    return manager.steer_task(task_id, instruction)


@mcp.tool()
def cancel_task(task_id: str) -> dict:
    """Cancel the currently running Antigravity turn/process."""
    return manager.cancel_task(task_id)


if __name__ == "__main__":
    mcp.run()
