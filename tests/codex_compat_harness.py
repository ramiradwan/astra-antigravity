"""Falsify hidden Codex model re-sampling while MCP await_task is pending.

This harness uses only local fixtures:
- a fake OpenAI Responses SSE endpoint that scripts exactly two model turns;
- the credential-free slow MCP await server in tests/fixtures;
- a pinned externally installed Codex CLI.

The invariant is strict: after response #1 tells Codex to call MCP await_task,
no second /v1/responses request may arrive until the MCP fixture writes its
completion marker. A violation exits non-zero immediately.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_FIXTURE = ROOT / "tests" / "fixtures" / "codex_await_server.py"
CALL_ID = "call-codex-await"


def _event(kind: str, payload: dict | None = None) -> str:
    if payload is None:
        return f"event: {kind}\n\n"
    return f"event: {kind}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _completed(response_id: str) -> dict:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        },
    }


def _first_response() -> bytes:
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp-await-call"},
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": CALL_ID,
                "namespace": "mcp__antigravity",
                "name": "await_task",
                "arguments": json.dumps({"task_id": "task-wire"}),
            },
        },
        _completed("resp-await-call"),
    ]
    return "".join(_event(item["type"], item) for item in events).encode("utf-8")


def _second_response() -> bytes:
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp-final"},
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "id": "msg-final",
                "content": [
                    {
                        "type": "output_text",
                        "text": "codex await compatibility passed",
                    }
                ],
            },
        },
        _completed("resp-final"),
    ]
    return "".join(_event(item["type"], item) for item in events).encode("utf-8")


class State:
    def __init__(self, completed_marker: Path):
        self.completed_marker = completed_marker
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.early_request = False

    def record(self, body: dict) -> int:
        with self.lock:
            number = len(self.requests) + 1
            if number > 1 and not self.completed_marker.exists():
                self.early_request = True
            self.requests.append(body)
            return number


class ResponsesHandler(BaseHTTPRequestHandler):
    server_version = "AstraCodexCompat/1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path.endswith("/models"):
            payload = json.dumps({"object": "list", "data": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {"_invalid_json": raw.decode("utf-8", errors="replace")}

        state: State = self.server.state  # type: ignore[attr-defined]
        number = state.record(body)
        if state.early_request:
            payload = json.dumps(
                {"error": "model request arrived before await_task completed"}
            ).encode("utf-8")
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if number == 1:
            payload = _first_response()
        elif number == 2:
            payload = _second_response()
        else:
            payload = json.dumps({"error": "unexpected extra model request"}).encode("utf-8")
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


def _toml_string(value: str) -> str:
    return json.dumps(value)


def run() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "astra-antigravity"))
    from luna_gateway import discover_codex
    codex = discover_codex()
    if not codex:
        raise RuntimeError("Codex executable not found; install the pinned CLI first")

    with tempfile.TemporaryDirectory(prefix="astra-codex-compat-") as tmp_raw:
        tmp = Path(tmp_raw)
        codex_home = tmp / "codex-home"
        codex_home.mkdir()
        started = tmp / "await-started"
        completed = tmp / "await-completed"

        state = State(completed)
        server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        server.state = state  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        config = f'''model = "mock-model"
model_provider = "mock"

[features]
plugins = false
remote_plugin = false

[model_providers.mock]
name = "mock"
base_url = "http://{host}:{port}/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"

[mcp_servers.antigravity]
command = {_toml_string(sys.executable)}
args = [{_toml_string(str(MCP_FIXTURE))}]
startup_timeout_sec = 10
tool_timeout_sec = 30
required = true
env_vars = ["ASTRA_TEST_AWAIT_STARTED_FILE", "ASTRA_TEST_AWAIT_COMPLETED_FILE", "ASTRA_TEST_AWAIT_STEP_DELAY"]
'''
        (codex_home / "config.toml").write_text(config, encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(codex_home),
                "OPENAI_API_KEY": "dummy",
                "ASTRA_TEST_AWAIT_STARTED_FILE": str(started),
                "ASTRA_TEST_AWAIT_COMPLETED_FILE": str(completed),
                "ASTRA_TEST_AWAIT_STEP_DELAY": "0.7",
            }
        )

        command = [
            codex,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(ROOT),
            "Call the antigravity await_task tool for task-wire exactly once, then report its result.",
        ]
        try:
            proc = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        diagnostics = {
            "returncode": proc.returncode,
            "request_count": len(state.requests),
            "early_request": state.early_request,
            "await_started": started.exists(),
            "await_completed": completed.exists(),
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }

        if proc.returncode != 0:
            raise AssertionError("Codex exited non-zero:\n" + json.dumps(diagnostics, indent=2))
        if state.early_request:
            raise AssertionError(
                "Codex requested another model turn while await_task was still pending:\n"
                + json.dumps(diagnostics, indent=2)
            )
        if not started.exists() or not completed.exists():
            raise AssertionError(
                "Codex did not execute the full MCP await fixture:\n"
                + json.dumps(diagnostics, indent=2)
            )
        if len(state.requests) != 2:
            raise AssertionError(
                "Expected exactly two model requests (tool call then final answer):\n"
                + json.dumps(diagnostics, indent=2)
            )

        second_input = json.dumps(state.requests[1].get("input", []))
        if CALL_ID not in second_input or "function_call_output" not in second_input:
            raise AssertionError(
                "Second model request did not contain the await_task tool output:\n"
                + json.dumps(diagnostics, indent=2)
            )
        if "codex await compatibility passed" not in proc.stdout:
            raise AssertionError(
                "Codex did not emit the scripted final answer:\n"
                + json.dumps(diagnostics, indent=2)
            )

        print(json.dumps({key: value for key, value in diagnostics.items() if key not in {"stdout", "stderr"}}, indent=2))


if __name__ == "__main__":
    run()
