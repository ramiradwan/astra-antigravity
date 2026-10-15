"""Parent-visible Luna gateway for Antigravity delegation.

The root Codex/Astra session sees only this MCP server. A private Codex App
Server runs a bounded, read-only Luna planning turn before each Gemini start or
correction. The gateway owns the Antigravity lifecycle in a background job and
publishes durable, compact decisions/results to a host relay. Explicit
foreground mode retains the single condition-driven await. Neither mode relies
on native custom roles or asks the planner to operate lifecycle tools.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import TextContent, ToolAnnotations
from communication import ProgressEmitter, bound_report, json_size
from background_delegations import BackgroundDelegations, inspect_saved_receipt
from command_probes import run_probe

from advanced_coordinator import AdvancedWorktreeCoordinator
from git_worktrees import (
    ISOLATION_SHARED,
    ISOLATION_WORKTREE,
    find_git,
    probe_git_workspace,
)
from launcher import discover_agy, discover_uv, get_plugin_version
from model_availability import check_model_availability, get_available_models
from model_routing import PROFILES, advertised_profiles, preferences, select_model
from compatibility import call_supported
from task_types import (CONTRACTS, TASK_TYPES, capability_requirements, check_capabilities,
                        result_key, result_schema, saved_task_type, semantic_result,
                        task_instructions, validate_task_type)
import server
from server import DEFAULT_EFFORT, DEFAULT_MODEL, LEGACY_DEFAULT_EFFORT, LEGACY_DEFAULT_MODEL

DEFAULT_PLANNER_MODEL = "gpt-6-luna"
DEFAULT_PLANNER_EFFORT = "low"
ALLOWED_PLANNER_MODELS = frozenset({"gpt-6-luna", "gpt-6.1-sol"})
LUNA_MODEL = DEFAULT_PLANNER_MODEL
LUNA_EFFORT = DEFAULT_PLANNER_EFFORT

LUNA_GATEWAY_TIMEOUT_SECONDS = 3500.0
LUNA_PLAN_TIMEOUT_SECONDS = 180.0
WAIT_SLICE_SECONDS = 30.0

MAX_REPORT_BUDGET = 8000
MAX_PROGRESS_MESSAGE_CHARS = 160
ORDINARY_PROGRESS_INTERVAL_SECONDS = 2.0
MAX_ORDINARY_PROGRESS_EVENTS = 30

_background_manager = None


@asynccontextmanager
async def _gateway_lifespan(_server):
    global _background_manager
    try:
        yield
    finally:
        if _background_manager is not None:
            await _background_manager.close()
            _background_manager = None


mcp = FastMCP("Astra Luna Antigravity Gateway", lifespan=_gateway_lifespan)
coordinator = AdvancedWorktreeCoordinator()


def _background():
    global _background_manager
    if _background_manager is None:
        _background_manager = BackgroundDelegations(coordinator.base.state_dir)
    return _background_manager


def gateway_tool(**options):
    """Serialize once, compactly; the SDK otherwise duplicates typed dict reports."""
    def register(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def wire(*args, **kwargs):
                value = await function(*args, **kwargs)
                return [TextContent(type="text", text=json.dumps(value, separators=(",", ":"), ensure_ascii=True))]
        else:
            @wraps(function)
            def wire(*args, **kwargs):
                value = function(*args, **kwargs)
                return [TextContent(type="text", text=json.dumps(value, separators=(",", ":"), ensure_ascii=True))]
        mcp.tool(structured_output=False, **options)(wire)
        return function
    return register

ProgressCallback = Callable[[int, str], Awaitable[None]]

LUNA_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning_requirement": {"type": "string", "enum": ["light", "heavy"]},
        "coordination_brief": {"type": "string", "maxLength": 4000},
        "acceptance_focus": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 500},
        },
        "risks": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 500},
        },
    },
    "required": ["coordination_brief", "acceptance_focus", "risks", "reasoning_requirement"],
    "additionalProperties": False,
}

APP_SERVER_FEATURE_OVERRIDES = (
    "-c", "features.plugins=false",
    "-c", "features.remote_plugin=false",
    "-c", "features.hooks=false",
    "-c", "features.multi_agent=false",
    "-c", "features.shell_tool=false",
    "-c", "features.unified_exec=false",
    "-c", 'web_search="disabled"',
)


class GatewayError(RuntimeError):
    pass


def resolve_planner_settings() -> tuple[str, str]:
    if "ASTRA_ANTIGRAVITY_PLANNER_MODEL" in os.environ:
        model = os.environ["ASTRA_ANTIGRAVITY_PLANNER_MODEL"].strip()
        if model not in ALLOWED_PLANNER_MODELS:
            raise GatewayError(
                f"Invalid ASTRA_ANTIGRAVITY_PLANNER_MODEL: '{model}'. "
                f"Allowed values are exactly: {', '.join(sorted(ALLOWED_PLANNER_MODELS))}."
            )
    else:
        model = DEFAULT_PLANNER_MODEL

    if "ASTRA_ANTIGRAVITY_PLANNER_EFFORT" in os.environ:
        effort = os.environ["ASTRA_ANTIGRAVITY_PLANNER_EFFORT"].strip().lower()
        if effort != "low":
            raise GatewayError(
                f"Invalid ASTRA_ANTIGRAVITY_PLANNER_EFFORT: '{effort}'. Planner effort must be 'low'."
            )
    else:
        effort = DEFAULT_PLANNER_EFFORT

    return model, effort


get_planner_model_settings = resolve_planner_settings


def _kill_process_tree(pid: int) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(pid, 9)
        except Exception:
            pass


def _find_native_codex_companion(cmd_path: Path) -> str | None:
    parent = cmd_path.parent
    exe = parent / "codex.exe"
    if exe.is_file():
        return str(exe.resolve())
    triple = "aarch64-pc-windows-msvc" if os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() == "arm64" else "x86_64-pc-windows-msvc"
    package = "codex-win32-arm64" if triple.startswith("aarch64") else "codex-win32-x64"
    for modules in (parent / "node_modules", parent.parent, parent.parent / "node_modules"):
        roots = (modules / "@openai" / package,
                 modules / "@openai" / "codex" / "node_modules" / "@openai" / package,
                 modules / "@openai" / "codex")
        for root in roots:
            candidate = root / "vendor" / triple / "bin" / "codex.exe"
            if candidate.is_file():
                return str(candidate.resolve())
    return None


def discover_codex() -> str:
    """Locate Codex executable with native Windows preference without mutating user config."""
    override = os.environ.get("CODEX_PATH") or os.environ.get("CODEX_BIN")
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            if os.name == "nt" and path.suffix.lower() in (".cmd", ".bat", ".ps1"):
                companion = _find_native_codex_companion(path)
                if companion:
                    return companion
            return str(path.resolve())
        raise GatewayError(f"CODEX_PATH is set but does not point to a file: {override}")

    if os.name == "nt":
        found_exe = shutil.which("codex.exe")
        if found_exe:
            return str(Path(found_exe).resolve())

    found = shutil.which("codex")
    if found:
        found_path = Path(found)
        if os.name == "nt" and found_path.suffix.lower() in (".cmd", ".bat", ".ps1"):
            companion = _find_native_codex_companion(found_path)
            if companion:
                return companion
        return str(found_path.resolve())

    raise GatewayError(
        "Codex CLI was not found. Install Codex or set CODEX_PATH to the Codex executable."
    )


def _compact_started(result: dict, expected_task_id: str | None = None) -> dict:
    # Very short turns can finish before the synchronous startup returns.
    # A blocked correction has no new turn and must never reuse its old result.
    if result.get("status") not in ("started", "running", "queued", "completed", "failed", "cancelled"):
        raise GatewayError(str(result.get("message") or result.get("error") or result.get("status")))
    task_id = result.get("task_id") or expected_task_id
    if not task_id:
        message = result.get("message") or result.get("error") or result.get("status")
        raise GatewayError(f"Antigravity lifecycle did not start a task: {message}")
    if expected_task_id and task_id != expected_task_id:
        raise GatewayError(
            f"Antigravity lifecycle changed task_id from {expected_task_id} to {task_id}."
        )
    return {
        "task_id": task_id,
        "status": result.get("status"),
        "current_turn": result.get("current_turn") or result.get("num_turns"),
    }


def _parse_planner_plan(text: str | None) -> dict:
    if not text:
        raise GatewayError("Planner completed without a coordination plan.")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GatewayError("Planner coordination plan was not valid JSON.") from exc
    if not isinstance(value, dict) or isinstance(value, list):
        raise GatewayError("Planner coordination plan was not a JSON object.")

    expected_keys = set(LUNA_PLAN_SCHEMA["required"])
    actual_keys = set(value.keys())
    missing = sorted(expected_keys - actual_keys)
    if missing:
        raise GatewayError(f"Planner coordination plan omitted required fields: {', '.join(missing)}")
    extra = sorted(actual_keys - expected_keys)
    if extra:
        raise GatewayError(f"Planner coordination plan contained unexpected fields: {', '.join(extra)}")

    if value.get("reasoning_requirement") not in ("light", "heavy"):
        raise GatewayError("Planner reasoning_requirement must be light or heavy")

    raw_brief = value.get("coordination_brief")
    if not isinstance(raw_brief, str) or isinstance(raw_brief, bool):
        raise GatewayError("Planner coordination plan 'coordination_brief' must be a string.")
    brief = raw_brief.strip()[:4000]
    if not brief:
        raise GatewayError("Planner coordination plan contained an empty coordination_brief.")

    def _validate_bounded_string_list(key: str, max_items: int = 20, max_chars: int = 500) -> list[str]:
        raw_list = value.get(key)
        if not isinstance(raw_list, list) or isinstance(raw_list, (str, bytes)):
            raise GatewayError(f"Planner coordination plan '{key}' must be a list of strings.")
        if len(raw_list) > max_items:
            raise GatewayError(f"Planner coordination plan '{key}' exceeded maximum {max_items} items.")
        result = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, str) or isinstance(item, bool):
                raise GatewayError(
                    f"Planner coordination plan '{key}[{idx}]' must be a string, got {type(item).__name__}."
                )
            result.append(item[:max_chars])
        return result

    return {
        "reasoning_requirement": value["reasoning_requirement"],
        "coordination_brief": brief,
        "acceptance_focus": _validate_bounded_string_list("acceptance_focus"),
        "risks": _validate_bounded_string_list("risks"),
    }


def _parse_luna_plan(text: str | None) -> dict:
    return _parse_planner_plan(text)


class _AppServerProcess:
    def __init__(self, codex: str, cwd: str):
        self.codex = codex
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.stderr_lines: deque[str] = deque(maxlen=120)
        self._stderr_thread: threading.Thread | None = None
        self._next_id = 1
        self.queued_notifications: deque[dict] = deque()

    def start(self) -> None:
        cmd = [self.codex, *APP_SERVER_FEATURE_OVERRIDES, "app-server"]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=self.cwd,
                env=os.environ.copy(),
                start_new_session=os.name != "nt",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise GatewayError(f"Failed to start Codex App Server: {exc}") from exc
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def diagnostics(self) -> str:
        return "\n".join(self.stderr_lines)

    def send(self, payload: dict) -> None:
        proc = self.proc
        if not proc or not proc.stdin:
            raise GatewayError("Codex App Server stdin is unavailable.")
        proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def request(self, method: str, params: dict) -> int:
        request_id = self._next_id
        self._next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        return request_id

    def read_message(self) -> dict:
        proc = self.proc
        if not proc or not proc.stdout:
            raise GatewayError("Codex App Server stdout is unavailable.")
        line = proc.stdout.readline()
        if not line:
            code = proc.poll()
            raise GatewayError(f"Codex App Server exited unexpectedly (code={code}).")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GatewayError("Codex App Server emitted invalid JSON.") from exc
        if not isinstance(value, dict):
            raise GatewayError("Codex App Server emitted a non-object JSON-RPC message.")
        return value

    async def read_async(self) -> dict:
        return await asyncio.to_thread(self.read_message)

    async def next_notification_or_message(self) -> dict:
        if self.queued_notifications:
            return self.queued_notifications.popleft()
        return await self.read_async()

    async def response(self, request_id: int) -> dict:
        while True:
            message = await self.read_async()
            if message.get("id") == request_id:
                return message
            if message.get("id") is not None and message.get("method"):
                raise GatewayError(
                    f"Unexpected App Server request before turn start: {message.get('method')}"
                )
            self.queued_notifications.append(message)

    def close(self) -> None:
        proc = self.proc
        if not proc:
            return
        try:
            if getattr(self, "thread_id", None) and getattr(self, "turn_id", None) and proc.poll() is None:
                self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
        except Exception:
            pass
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
        if self._stderr_thread:
            self._stderr_thread.join(timeout=1)


def _planning_instructions(mode: str) -> str:
    if mode == "delegation":
        focus = "Turn the parent task into a concise coordination brief for the selected Antigravity worker."
    else:
        focus = "Turn the parent's verification findings into a concise corrective brief for the Antigravity worker."
    return (
        "You are the mandatory Astra Luna delegation coordinator. "
        "You are planning only: do not edit files, execute commands, call tools, or implement the task. "
        f"{focus} Preserve the parent's requested scope and do not change workspace, isolation, or task identity. "
        "Return only the requested structured coordination plan."
        " Preserve the explicit task type and capability requirements; do not turn analysis, research or documents into coding tasks. "
        "Classify reasoning_requirement as light for routine tasks and heavy for difficult "
        "architecture, correctness, concurrency, or ambiguous multi-step reasoning. "
        "The gateway selects the implementation model; do not invent model slugs or change policy."
    )


async def _run_luna_plan(prompt: str, workspace: str, *, mode: str) -> dict:
    planner_model, planner_effort = resolve_planner_settings()
    codex = discover_codex()
    app = _AppServerProcess(codex, workspace)
    app.start()
    final_message: str | None = None

    async def run_inner() -> dict:
        nonlocal final_message
        init_id = app.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "astra-antigravity-luna-gateway",
                    "title": "Antigravity task planner",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        initialized = await app.response(init_id)
        if initialized.get("error"):
            raise GatewayError(f"Codex App Server initialize failed: {initialized['error']}")
        app.send({"method": "initialized"})
        config_req = app.request("config/read", {"includeLayers": False, "cwd": workspace})
        config_reply = await app.response(config_req)
        if config_reply.get("error"):
            raise GatewayError("Could not read effective planner configuration")
        effective = (config_reply.get("result") or {}).get("config") or {}
        disabled_servers = {name: {"enabled": False}
                            for name in (effective.get("mcp_servers") or {})}

        thread_req = app.request(
            "thread/start",
            {
                "model": planner_model,
                "config": {"mcp_servers": disabled_servers, "web_search": "disabled"},
                "cwd": workspace,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "developerInstructions": _planning_instructions(mode),
            },
        )
        started = await app.response(thread_req)
        if started.get("error"):
            raise GatewayError(f"Planner thread/start failed: {started['error']}")
        result = started.get("result") or {}
        if result.get("model") != planner_model:
            raise GatewayError(
                f"Planner enforcement failed: thread model is {result.get('model')!r}, expected {planner_model!r}."
            )
        thread_id = (result.get("thread") or {}).get("id")
        if not thread_id:
            raise GatewayError("Planner thread/start returned no thread id.")

        app.thread_id = thread_id
        turn_req = app.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "model": planner_model,
                "effort": planner_effort,
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "outputSchema": LUNA_PLAN_SCHEMA,
            },
        )
        turn_started = await app.response(turn_req)
        if turn_started.get("error"):
            raise GatewayError(f"Planner turn/start failed: {turn_started['error']}")
        turn_result = turn_started.get("result") or {}
        turn_id = (turn_result.get("turn") or {}).get("id")
        if not turn_id:
            raise GatewayError("Planner turn/start returned no turn id")
        app.turn_id = turn_id

        while True:
            message = await app.next_notification_or_message()
            method = message.get("method")
            params = message.get("params") or {}

            if message.get("id") is not None and method:
                app.send({"id": message["id"], "error": {
                    "code": -32601, "message": "Unsupported request during planning"}})
                raise GatewayError(f"Unexpected tool/request during planning: {method}")

            msg_thread = params.get("threadId") or params.get("thread_id")
            if msg_thread and msg_thread != thread_id:
                continue
            msg_turn = params.get("turnId") or params.get("turn_id") or (params.get("turn") or {}).get("id")
            if msg_turn and turn_id and msg_turn != turn_id:
                continue

            if method in ("item/started", "item/completed", "turn/completed"):
                if msg_thread != thread_id or msg_turn != turn_id:
                    continue

            if method in ("item/started", "item/completed"):
                item = params.get("item") or {}
                item_type = item.get("type")
                if item_type in ("commandExecution", "fileEdit", "fileChange", "toolCall", "mcpToolCall", "dynamicToolCall", "collabToolCall", "execCommand", "browser", "webSearch"):
                    raise GatewayError(f"Unexpected tool activity during planning turn: {item_type}")
                if method == "item/completed" and item_type == "agentMessage":
                    final_message = item.get("text")
                continue

            if method == "model/rerouted":
                rerouted_model = params.get("toModel") or params.get("to_model")
                raise GatewayError(
                    f"Planner enforcement failed: backend rerouted to {rerouted_model!r}."
                )

            if method == "turn/completed" and msg_thread != thread_id:
                continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                if turn.get("status") != "completed":
                    error = turn.get("error") or {}
                    message_text = error.get("message") or turn.get("status")
                    raise GatewayError(f"Planner turn did not complete successfully: {message_text}")
                break

        return _parse_planner_plan(final_message)

    try:
        plan = await asyncio.wait_for(run_inner(), timeout=LUNA_PLAN_TIMEOUT_SECONDS)
        return plan
    finally:
        app.close()


async def _await_terminal(
    coordinator_obj,
    task_id: str,
    progress: ProgressCallback,
) -> dict:
    sequence = 0
    while True:
        update = await asyncio.to_thread(
            coordinator_obj.base.wait_for_update,
            task_id,
            sequence,
            WAIT_SLICE_SECONDS,
        )
        new_sequence = int(update.get("sequence") or 0)
        if new_sequence > sequence:
            sequence = new_sequence
            await progress(
                sequence,
                update,
            )
        if not update.get("terminal"):
            continue

        settled = await asyncio.to_thread(coordinator_obj.wait, task_id, 0.1)
        if not settled.get("finished"):
            continue
        if hasattr(coordinator_obj, "compact_result"):
            return await asyncio.to_thread(coordinator_obj.compact_result, task_id)
        return await asyncio.to_thread(coordinator_obj.result, task_id)


def _format_plan(plan: dict) -> str:
    lines = ["Luna coordination brief:", plan["coordination_brief"]]
    if plan.get("acceptance_focus"):
        lines.append("Acceptance focus:")
        lines.extend(f"- {item}" for item in plan["acceptance_focus"])
    if plan.get("risks"):
        lines.append("Coordination risks:")
        lines.extend(f"- {item}" for item in plan["risks"])
    return "\n".join(lines)


def _delegation_prompt(task: str, task_type="implementation", required_capabilities=None) -> str:
    return (
        "Prepare a bounded coordination plan for the following parent-authorized task. "
        "Do not add unrelated scope.\n\nTask contract:\n"
        + task_instructions(task_type, required_capabilities) + "\n\nParent task:\n" + task
    )


def _continuation_prompt(task_id: str, findings: str, previous: dict) -> str:
    task_type = previous.get("task_type", "implementation")
    implementation = semantic_result(previous)
    if task_type != "implementation":
        return (
            "Prepare a bounded corrective coordination plan for the existing delegated task. "
            "Preserve its task type, workspace and scope.\n\n"
            f"Task type: {task_type}\nTask id: {task_id}\nParent verification findings:\n{findings}\n\n"
            "Previous compact result:\n" + json.dumps(bound_report({"result": implementation}, max_budget=7000),
                                                      separators=(",", ":"), ensure_ascii=True)
        )
    compact_previous = {
        "summary": implementation.get("summary"),
        "files_changed": implementation.get("files_changed") or [],
        "tests_run": implementation.get("tests_run") or [],
        "tests_passed": implementation.get("tests_passed"),
        "remaining_work": implementation.get("remaining_work") or [],
        "risks": implementation.get("risks") or [],
    }
    return (
        "Prepare a bounded corrective coordination plan for the existing delegated task. "
        "Do not replace the task or broaden scope.\n\n"
        f"Task id: {task_id}\nParent verification findings:\n{findings}\n\n"
        "Previous compact implementation result:\n"
        + json.dumps(bound_report({"implementation": compact_previous}, max_budget=7000),
                     separators=(",", ":"), ensure_ascii=True)
    )


def _json_size(obj):
    return json_size(obj)


def compact_terminal_report(terminal, plan, *, phase, planner_model,
                            planner_effort, requested_gemini_model,
                            requested_gemini_effort, max_budget=MAX_REPORT_BUDGET):
    task_type = terminal.get("task_type", "implementation")
    key = result_key(task_type)
    impl = semantic_result(terminal)
    wt = impl.get("worktree") or {}
    planner = {"model": planner_model, "reasoning_effort": planner_effort,
               "phase": phase, "coordination": plan}
    result = {
        "task_id": terminal.get("task_id"), "status": terminal.get("status"),
        "current_turn": terminal.get("current_turn"),
        "task_type": task_type,
        key: {
            key: impl.get(key) for key in (
                "summary", "files_changed", "tests_run", "tests_passed",
                "remaining_work", "risks", "commit_sha", "structured_report_valid")
        },
        "quota": {key: terminal.get("quota", {}).get(key) for key in (
            "available", "min_remaining_fraction", "warning", "blocked", "limiting_bucket")},
        "planner": planner, "luna": planner,
        "antigravity": {"requested_model": requested_gemini_model,
                   "requested_effort": requested_gemini_effort},
    }
    if task_type != "implementation":
        result[key] = {name: impl.get(name) for name in tuple(result_schema(task_type)["properties"]) + (
            "files_changed", "artifact_evidence", "structured_report_valid")}
        result["verification"] = terminal.get("verification") or {
            "required": True, "structural_checks_passed": False,
            "focus": CONTRACTS[task_type]["verification_focus"], "issues": ["Verification metadata is missing."],
        }
    result[key]["worktree"] = {
        key: wt.get(key) for key in (
            "path", "workspace", "branch", "base_commit", "head_commit",
            "dirty", "diff_stat", "commits_ahead")}
    if terminal.get("error"):
        result["error"] = str(terminal["error"])
    return bound_report(result, max_budget)


def _result_with_luna(
    terminal: dict,
    plan: dict,
    *,
    phase: str,
    planner_model: str = DEFAULT_PLANNER_MODEL,
    planner_effort: str = DEFAULT_PLANNER_EFFORT,
    requested_gemini_model: str = DEFAULT_MODEL,
    requested_gemini_effort: str = DEFAULT_EFFORT,
) -> dict:
    return compact_terminal_report(
        terminal,
        plan,
        phase=phase,
        planner_model=planner_model,
        planner_effort=planner_effort,
        requested_gemini_model=requested_gemini_model,
        requested_gemini_effort=requested_gemini_effort,
    )


async def _progress_from_context(ctx: Context, sequence: int, message: str) -> None:
    try:
        await ctx.report_progress(progress=sequence, message=message)
    except Exception:
        pass


class _ProgressEmitter(ProgressEmitter):
    def __init__(self, ctx):
        super().__init__(ctx, _progress_from_context)


_pending_starts: set[asyncio.Task] = set()
_active_corrections: set[str] = set()


def _retain_start(task):
    _pending_starts.add(task)
    def settled(future):
        _pending_starts.discard(future)
        if not future.cancelled():
            future.exception()
    task.add_done_callback(settled)


async def _execute_gateway(task, workspace, ctx, *, existing_id=None,
                           isolation=ISOLATION_WORKTREE, quota_override=False,
                           reasoning="auto", model=None, progress=None,
                           task_type="implementation", required_capabilities=None):
    progress = progress if progress is not None else _ProgressEmitter(ctx)
    cancelled = threading.Event()
    owner = {"task_id": None}
    startup = None
    planner_model, planner_effort = resolve_planner_settings()
    phase = "continuation" if existing_id else "delegation"
    requested_model, requested_effort = DEFAULT_MODEL, DEFAULT_EFFORT
    selection = None

    async def unavailable(choice, plan=None):
        await progress.emit("Implementation model unavailable; see the offered model choices.")
        value = {"status": "model_unavailable", "task_id": existing_id,
                 "task_type": task_type,
                 "implementation_started": False, "message": choice["message"],
                 "model_selection": choice}
        if plan:
            metadata = {"model": planner_model, "reasoning_effort": planner_effort,
                        "phase": phase, "coordination": plan}
            value.update(planner=metadata, luna=metadata)
        return bound_report(value)

    def start_sync(instruction):
        # Capture ownership INSIDE the executor thread. Cancelling its awaiting
        # coroutine cannot lose the task id or prevent the eventual cleanup.
        if cancelled.is_set():
            raise GatewayError("Delegation was cancelled before startup")
        if existing_id:
            record = coordinator.base.get_task(existing_id)
            if record is None or record.status != "completed":
                raise GatewayError("Task state changed during corrective planning; inspect before retrying")
            owner["task_id"] = existing_id
            started = coordinator.steer(existing_id, instruction, quota_override=quota_override)
        else:
            started = call_supported(coordinator.delegate, instruction, workspace, isolation,
                                     quota_override=quota_override, model=requested_model,
                                     task_type=task_type, required_capabilities=required_capabilities)
        captured = _compact_started(started, existing_id)
        owner["task_id"] = captured["task_id"]
        if hasattr(progress, "captured_start"):
            progress.captured_start(captured["task_id"])
        if cancelled.is_set():
            coordinator.cancel(captured["task_id"])
        return captured

    async def body():
        nonlocal startup, workspace, requested_model, requested_effort, selection, task_type, required_capabilities
        if existing_id:
            status = await asyncio.to_thread(coordinator.status, existing_id)
            record = coordinator.base.get_task(existing_id)
            if record is None:
                raise ValueError(f"Task '{existing_id}' not found")
            task_type = saved_task_type(record)
            required_capabilities = capability_requirements(task_type, getattr(record, "required_capabilities", None))
        capabilities = check_capabilities(task_type, required_capabilities)
        if not capabilities["available"]:
            return {"status": "capability_unavailable", "task_id": existing_id, "task_type": task_type,
                    "task_started": False, "capabilities": capabilities,
                    "message": "Required capabilities are unavailable through this gateway: "
                               + ", ".join(capabilities["unavailable"]) + ". Supply local material or use a capable host tool."}
        catalog = await asyncio.to_thread(get_available_models, server.find_agy)
        if existing_id:
            requested_model = getattr(record, "model", None) or LEGACY_DEFAULT_MODEL
            requested_effort = getattr(record, "effort", None) or LEGACY_DEFAULT_EFFORT
            workspace = status.get("effective_workspace") or status.get("workspace")
            if requested_model not in catalog:
                profile = next((name for name, data in PROFILES.items()
                                if re.fullmatch(data["pattern"], requested_model)), None)
                if profile:
                    choice = select_model(PROFILES[profile]["reasoning"], catalog, model=profile)
                else:
                    choice = {"requested_model": requested_model,
                              "message": f"Pinned task model '{requested_model}' is unavailable. The existing task is preserved."}
                choice["message"] = f"Pinned task model '{requested_model}' is unavailable. " + choice["message"]
                choice["message"] += " Corrections retain model pins; review saved work before a new delegation with another model."
                choice["retry"] = None
                return await unavailable(choice)
            if status.get("status") != "completed":
                raise GatewayError(f"Task '{existing_id}' is not completed; inspect its work before new delegation.")
            previous = await asyncio.to_thread(coordinator.compact_result, existing_id)
            prompt = _continuation_prompt(existing_id, task, previous)
            await progress.emit("Planning corrective continuation")
        else:
            preferences()  # Validate explicit policy before any planning or mutation.
            chosen_reasoning = PROFILES[model]["reasoning"] if model else reasoning
            if chosen_reasoning != "auto":
                selection = select_model(chosen_reasoning, catalog, model=model)
                if not selection["available"]:
                    return await unavailable(selection)
            prompt = _delegation_prompt(task, task_type, required_capabilities)
            if chosen_reasoning != "auto":
                prompt += f"\nParent-selected reasoning requirement: {chosen_reasoning}."
            await progress.emit("Planning delegation")
        plan = await _run_luna_plan(prompt, str(workspace), mode=phase)
        if not existing_id:
            selection = selection or select_model(plan["reasoning_requirement"], catalog, model=model)
            if not selection["available"]:
                return await unavailable(selection, plan)
            requested_model, requested_effort = selection["model"], selection["effort"]
            if selection.get("notice"):
                await progress.emit(selection["notice"])
        instruction = task.rstrip() + "\n\n" + _format_plan(plan)
        startup = asyncio.create_task(asyncio.to_thread(start_sync, instruction))
        _retain_start(startup)
        started = await asyncio.shield(startup)
        task_id = started["task_id"]
        record = coordinator.base.get_task(task_id)
        if record is None or saved_task_type(record) != task_type:
            raise GatewayError("The started task did not preserve its authorized task type")
        if capability_requirements(task_type, getattr(record, "required_capabilities", None)) != capability_requirements(task_type, required_capabilities):
            raise GatewayError("The started task did not preserve its capability requirements")
        progress.workspace = getattr(record, "workspace", None) or workspace
        if hasattr(progress, "started"):
            await progress.started(started)
        await progress.emit("Antigravity correction started" if existing_id else f"Antigravity {task_type} started")
        terminal = await _await_terminal(coordinator, task_id, progress.forward)
        state = terminal.get("status") or "unknown"
        label = "correction" if existing_id else task_type
        await progress.emit(f"Antigravity {label} {state}")
        result = _result_with_luna(terminal, plan, phase=phase,
                                planner_model=planner_model, planner_effort=planner_effort,
                                requested_gemini_model=requested_model,
                                requested_gemini_effort=requested_effort)
        if selection:
            result["model_selection"] = selection
        result["capabilities"] = capabilities
        return bound_report(result)

    try:
        # This covers preflight, planning, executor startup, progress and result
        # delivery preparation, rather than starting a timer only after launch.
        return await asyncio.wait_for(body(), timeout=LUNA_GATEWAY_TIMEOUT_SECONDS)
    except BaseException:
        cancelled.set()
        if startup is not None and not startup.done():
            try:
                await asyncio.wait_for(asyncio.shield(startup), timeout=60)
            except (Exception, asyncio.CancelledError):
                # Retained start_sync will stop its exact task when it settles.
                pass
        if owner["task_id"]:
            try:
                cleanup = asyncio.create_task(asyncio.to_thread(coordinator.cancel, owner["task_id"]))
                _retain_start(cleanup)
                await asyncio.wait_for(asyncio.shield(cleanup), timeout=30)
            except (Exception, asyncio.CancelledError):
                pass
        raise
    finally:
        await progress.close()


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def delegate_via_luna(task: str, workspace: str, ctx: Context,
                           isolation: str = ISOLATION_WORKTREE,
                           quota_override: bool = False,
                           reasoning: Literal["auto", "light", "heavy"] = "auto",
                           model: Literal["sonnet-5.5", "gemini-3.8-high", "argon-high", "opus-5.5"] | None = None,
                           delivery: Literal["background", "foreground"] = "background",
                           request_id: str | None = None,
                           task_type: Literal["implementation", "analysis", "research", "document"] = "implementation",
                           required_capabilities: list[str] | None = None) -> dict:
    """Start a Luna-planned job and return its receipt; let the parent continue independent work.

    A background relay receives decisions/completion with receive_delegation_events.
    Use foreground explicitly only for clients without background execution.
    request_id makes launch retries idempotent; never reuse it for different work.
    task_type selects instructions, result schema and parent verification.
    Supported capabilities are local_files and shell. Declare external requirements
    (web, browser, connectors) explicitly; unverified capabilities stop before planning.
    Sends the scoped task to authenticated Codex/Antigravity and consumes their quotas.
    The worker can run commands and modify or delete files within the authorized task.
    """
    validate_task_type(task_type)
    required_capabilities = capability_requirements(task_type, required_capabilities)
    if not task.strip():
        raise ValueError("task must not be empty")
    if not workspace.strip():
        raise ValueError("workspace must not be empty")
    if isolation not in (ISOLATION_WORKTREE, ISOLATION_SHARED):
        raise ValueError(f"Unsupported isolation mode: {isolation}")
    if reasoning not in ("auto", "light", "heavy") or model is not None and model not in PROFILES:
        raise ValueError("Unsupported reasoning requirement or model profile")
    if model and reasoning != "auto" and PROFILES[model]["reasoning"] != reasoning:
        raise ValueError("Selected model profile does not match the reasoning requirement")
    if delivery not in ("background", "foreground"):
        raise ValueError("Unsupported delivery mode")
    if request_id is not None and delivery == "foreground":
        raise ValueError("request_id is supported only for background delivery")
    resolve_planner_settings()
    if delivery == "background":
        inputs = {"task": task, "workspace": str(Path(workspace).expanduser().resolve()),
                  "isolation": isolation, "quota_override": quota_override, "reasoning": reasoning, "model": model}
        # Default implementation retries retain the 0.8 request fingerprint.
        if task_type != "implementation":
            inputs["task_type"] = task_type
        if required_capabilities != capability_requirements(task_type):
            inputs["required_capabilities"] = required_capabilities
        receipt, _ = _background().start(
            workspace=str(Path(workspace).expanduser().resolve()), phase="delegation",
            inputs=inputs, task_type=task_type,
            request_id=request_id, base=coordinator.base,
            execute=lambda progress: _execute_gateway(
                task, workspace, None, isolation=isolation, quota_override=quota_override,
                reasoning=reasoning, model=model, progress=progress,
                task_type=task_type, required_capabilities=required_capabilities))
        return receipt
    return await _execute_gateway(task, workspace, ctx, isolation=isolation,
                                  quota_override=quota_override, reasoning=reasoning, model=model,
                                  task_type=task_type, required_capabilities=required_capabilities)


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
async def continue_via_luna(task_id: str, findings: str, ctx: Context,
                           quota_override: bool = False,
                           delivery: Literal["background", "foreground"] = "background",
                           request_id: str | None = None) -> dict:
    """Correct the exact completed task with its saved model/workspace pins.

    Sends findings through authenticated Codex/Antigravity, consumes provider quotas
    and can run commands or modify/delete files within the authorized correction.
    Background mode returns a receipt; foreground mode awaits the bounded handoff.
    """
    if not findings.strip():
        raise ValueError("findings must not be empty")
    if not task_id.strip():
        raise ValueError("task_id must not be empty")
    if delivery not in ("background", "foreground"):
        raise ValueError("Unsupported delivery mode")
    if request_id is not None and delivery == "foreground":
        raise ValueError("request_id is supported only for background delivery")
    resolve_planner_settings()
    if delivery == "background":
        # Idempotent retries return the existing receipt even while a correction
        # is running; a different request may never start another correction.
        manager = _background()
        record = await asyncio.to_thread(coordinator.base.get_task, task_id)
        receipt, created = manager.start(
            workspace="", phase="continuation", task_id=task_id,
            task_type=saved_task_type(record),
            inputs={"task_id": task_id, "findings": findings, "quota_override": quota_override},
            request_id=request_id, base=coordinator.base, busy=task_id in _active_corrections,
            execute=lambda progress: _execute_gateway(
                findings, None, None, existing_id=task_id, quota_override=quota_override,
                progress=progress), finished=lambda: _active_corrections.discard(task_id))
        if created:
            _active_corrections.add(task_id)
        return receipt
    if task_id in _active_corrections:
        raise GatewayError("A correction of this task is already pending")
    _active_corrections.add(task_id)
    try:
        return await _execute_gateway(findings, None, ctx, existing_id=task_id,
                                      quota_override=quota_override)
    finally:
        _active_corrections.discard(task_id)


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
async def cancel_delegation(task_id: str | None = None, delegation_id: str | None = None) -> dict:
    """Cancel by delegation receipt (including planning) or an exact foreground task id.

    Background cancellation returns a request receipt; await its final event.
    Stops the worker and records cancellation. Existing file changes remain.
    """
    if bool(task_id) == bool(delegation_id):
        raise ValueError("Supply exactly one of task_id or delegation_id")
    if delegation_id:
        return await _background().cancel(delegation_id)
    raw = await asyncio.to_thread(coordinator.cancel, task_id)
    return bound_report({key: raw.get(key) for key in (
        "task_id", "status", "cancelled", "session_closed", "error")})


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def receive_delegation_events(delegation_id: str, after_sequence: int | None = None) -> dict:
    """For background relays: block until a decision or terminal event; never poll status.

    Ordinary progress cannot complete this call. A disconnected receiver does
    not cancel the worker. Resume with the last delivered sequence after reconnect.
    Omit the cursor to skip acknowledged events; an explicit cursor replays the
    archive from that point if the host lost its own context.
    """
    return await _background().receive(delegation_id, after_sequence)


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def ack_delegation_events(delegation_id: str, sequence: int) -> dict:
    """Persist the delivered event cursor after the host accepts model-facing delivery.

    Later receives skip acknowledged events unless an explicit replay cursor is supplied.
    """
    return _background().acknowledge(delegation_id, sequence)


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def read_delegation_result(delegation_id: str) -> dict:
    """Read one saved terminal handoff for verification; not a progress/status tool."""
    return _background().result(delegation_id)


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def cleanup_task_worktree(
    task_id: str,
    force: bool = False,
    delete_branch: bool = False,
) -> dict:
    """Delete the delegated worktree after parent review or abandonment.

    force can discard uncommitted changes; delete_branch also deletes the task's
    local branch. Save or integrate needed work first. Saved task history remains.
    """
    raw = coordinator.cleanup(task_id, force=force, delete_branch=delete_branch)
    worktree = raw.get("worktree") or {}
    return bound_report({key: raw.get(key) for key in (
        "task_id", "cleaned", "branch_deleted", "remove_error", "branch_delete_error", "message")})


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def check_integration(workspace: str, isolation: Literal["worktree", "shared"] = "worktree",
                            task_type: Literal["implementation", "analysis", "research", "document"] = "implementation",
                            required_capabilities: list[str] | None = None) -> dict:
    """Read-only dependency, task capability, isolation and model preflight; no inference."""
    return await asyncio.to_thread(_check_integration_sync, workspace, isolation, task_type, required_capabilities)


def _check_integration_sync(workspace: str, isolation="worktree", task_type="implementation", required_capabilities=None) -> dict:
    validate_task_type(task_type)
    if isolation not in (ISOLATION_SHARED, ISOLATION_WORKTREE):
        raise ValueError("Unsupported isolation mode")
    capabilities = check_capabilities(task_type, required_capabilities)
    remediations: list[str] = []
    if not capabilities["available"]:
        remediations.append("Unavailable gateway capabilities: " + ", ".join(capabilities["unavailable"]))
    if not Path(workspace).expanduser().is_dir():
        remediations.append(f"Workspace directory does not exist: {workspace}")

    codex_ready = False
    codex_path = None
    codex_version = None
    try:
        codex_path = discover_codex()
        proc = run_probe([codex_path, "--version"])
        if proc.returncode == 0:
            codex_version = (proc.stdout or "").strip()
            codex_ready = True
        else:
            remediations.append(f"Codex CLI at {codex_path} returned non-zero ({proc.returncode}).")
    except Exception as exc:
        remediations.append(f"Codex CLI discovery failed: {exc}. Install Codex or set CODEX_PATH.")

    agy_ready = False
    agy_path = None
    agy_version = None
    try:
        agy_path = discover_agy(required=False)
        if agy_path:
            proc = run_probe([agy_path, "--version"])
            if proc.returncode == 0:
                agy_version = (proc.stdout or "").strip()
                agy_ready = True
            else:
                remediations.append(f"Antigravity CLI at {agy_path} returned non-zero ({proc.returncode}).")
        else:
            remediations.append("Antigravity CLI (agy) was not found. Install agy and run agy once to authenticate.")
    except Exception as exc:
        remediations.append(f"Antigravity CLI discovery failed: {exc}. Install agy or set AGY_PATH.")

    git_ready = False
    git_path = None
    git_version = None
    try:
        git_path = find_git()
        proc = run_probe([git_path, "--version"])
        if proc.returncode == 0:
            git_version = (proc.stdout or "").strip()
            git_ready = True
        else:
            remediations.append(f"Git at {git_path} returned non-zero ({proc.returncode}).")
    except Exception as exc:
        remediations.append(f"Git discovery failed: {exc}. Install Git or set ASTRA_ANTIGRAVITY_GIT_PATH.")

    python_path = sys.executable
    try:
        uv_path = discover_uv(required=False)
    except Exception as exc:
        uv_path = None
        remediations.append(str(exc))
    from importlib.metadata import version
    sdk_version = version("mcp")
    if sdk_version != "1.29.1":
        remediations.append("Install mcp[cli]==1.29.1 in the selected Python environment.")

    planner_model = DEFAULT_PLANNER_MODEL
    planner_effort = DEFAULT_PLANNER_EFFORT
    planner_valid = True
    try:
        planner_model, planner_effort = resolve_planner_settings()
    except Exception as exc:
        planner_valid = False
        remediations.append(f"Planner settings invalid: {exc}")

    workspace_info = probe_git_workspace(workspace, allow_dirty=coordinator.store.allow_dirty_source)
    if isolation == ISOLATION_WORKTREE and not workspace_info.get("git_repository"):
        remediations.append(f"Workspace '{workspace}' is not a Git repository. Initialize git for worktree isolation.")

    model_availability = {"profiles": {}, "routing": {}, "error": None}
    ready_for = {"light": False, "heavy": False}
    if agy_ready and agy_path:
        try:
            catalog = get_available_models(lambda: agy_path, timeout=5.0)
            model_availability["profiles"] = advertised_profiles(catalog)
            for requirement in ready_for:
                choice = select_model(requirement, catalog)
                model_availability["routing"][requirement] = choice
                ready_for[requirement] = choice["available"]
                if not choice["available"]:
                    remediations.append(choice["message"])
        except Exception as exc:
            model_availability["error"] = str(exc)
            remediations.append(f"Model routing check failed: {exc}")
    else:
        model_availability["error"] = "Antigravity CLI unavailable"

    workspace_ready = (git_ready and bool(workspace_info.get("worktree_eligible"))) if isolation == ISOLATION_WORKTREE else Path(workspace).expanduser().is_dir()
    base_ready = (codex_ready and agy_ready and planner_valid and workspace_ready
                  and capabilities["available"] and sdk_version == "1.29.1")
    ready_for = {key: base_ready and value for key, value in ready_for.items()}
    all_ready = any(ready_for.values())

    return bound_report({
        "ready": all_ready,
        "ready_for": ready_for,
        "task_type": task_type,
        "task_types": list(TASK_TYPES),
        "isolation": isolation,
        "capabilities": capabilities,
        "plugin_version": get_plugin_version(),
        "mcp_sdk_version": sdk_version,
        "executables": {
            "codex": {"ready": codex_ready, "path": codex_path, "version": codex_version},
            "agy": {"ready": agy_ready, "path": agy_path, "version": agy_version},
            "git": {"ready": git_ready, "path": git_path, "version": git_version},
            "python": {"path": python_path, "version": sys.version.split()[0]},
            "uv": {"ready": bool(uv_path), "path": uv_path},
        },
        "planner_settings": {
            "model": planner_model,
            "effort": planner_effort,
            "valid": planner_valid,
        },
        "workspace": {
            "path": workspace,
            "git_repository": bool(workspace_info.get("git_repository")),
            "worktree_eligible": bool(workspace_info.get("worktree_eligible")),
            "dirty": workspace_info.get("dirty"),
            "repo_root": workspace_info.get("repo_root"),
        },
        "model_availability": model_availability,
        "remediation": remediations if remediations else ["Dependencies are ready; planner access is checked on its first turn."],
    })


@gateway_tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def inspect_delegation(workspace: str, task_id: str | None = None, request_id: str | None = None) -> dict:
    """Inspect saved tasks, or recover a lost launch receipt by request_id, without replaying work."""
    if request_id:
        if task_id:
            raise ValueError("Supply task_id or request_id, not both")
        return inspect_saved_receipt(coordinator.base.state_dir, request_id,
                                     _background_manager.workers if _background_manager else None)
    from recovery import inspect_saved_tasks
    return inspect_saved_tasks(coordinator, workspace, task_id)


if __name__ == "__main__":
    mcp.run()
