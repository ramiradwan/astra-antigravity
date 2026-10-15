"""Observe whether current Codex forwards MCP progress through App Server.

This is a capability canary, not a production gate. The MCP lifecycle must work
and must not re-sample the model while the tool is pending. A missing
item/mcpToolCall/progress notification is reported as supported=false rather
than treated as a test failure.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_FIXTURE = ROOT / "tests" / "fixtures" / "codex_luna_gateway_server.py"
CALL_ID = "call-progress-canary"


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
        {"type": "response.created", "response": {"id": "resp-progress-call"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": CALL_ID,
                "namespace": "mcp__luna_gateway",
                "name": "delegate_via_luna",
                "arguments": json.dumps(
                    {
                        "task": "Exercise MCP progress forwarding",
                        "workspace": str(ROOT),
                        "isolation": "worktree",
                    },
                    separators=(",", ":"),
                ),
            },
        },
        _completed("resp-progress-call"),
    ]
    return "".join(_event(item["type"], item) for item in events).encode("utf-8")


def _second_response() -> bytes:
    events = [
        {"type": "response.created", "response": {"id": "resp-progress-final"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "id": "msg-progress-final",
                "content": [
                    {"type": "output_text", "text": "MCP progress canary complete"}
                ],
            },
        },
        _completed("resp-progress-final"),
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
    server_version = "AstraMcpProgressCanary/1"

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
        body = json.loads(self.rfile.read(length) or b"{}")
        state: State = self.server.state  # type: ignore[attr-defined]
        number = state.record(body)

        if state.early_request:
            payload = json.dumps(
                {"error": "model request arrived while MCP tool was pending"}
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
            payload = json.dumps({"error": "unexpected extra model request"}).encode(
                "utf-8"
            )
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


class JsonRpcClient:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.messages: queue.Queue[dict] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                self.messages.put({"_invalid_json": line})

    def _read_stderr(self):
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr.append(raw.rstrip())
            if len(self.stderr) > 200:
                del self.stderr[:50]

    def send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, predicate, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        held: list[dict] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for App Server message")
                message = self.messages.get(timeout=remaining)
                if predicate(message):
                    return message
                held.append(message)
        finally:
            for message in held:
                self.messages.put(message)

    def response(self, request_id: int, timeout: float = 30.0) -> dict:
        return self.wait_for(lambda msg: msg.get("id") == request_id, timeout)

    def diagnostics(self) -> dict:
        return {
            "returncode": self.proc.poll(),
            "stderr": "\n".join(self.stderr[-80:]),
        }


def _toml_string(value: str) -> str:
    return json.dumps(value)


def run() -> None:
    sys.path.insert(0, str(ROOT / "plugins" / "astra-antigravity"))
    from luna_gateway import discover_codex
    codex = discover_codex()
    if not codex:
        raise RuntimeError("Codex executable not found; install Codex before running canary")

    with tempfile.TemporaryDirectory(prefix="astra-mcp-progress-") as tmp_raw:
        tmp = Path(tmp_raw)
        codex_home = tmp / "codex-home"
        codex_home.mkdir()
        started = tmp / "gateway-started"
        completed_marker = tmp / "gateway-completed"

        state = State(completed_marker)
        model_server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        model_server.state = state  # type: ignore[attr-defined]
        model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
        model_thread.start()
        host, port = model_server.server_address

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

[mcp_servers.luna_gateway]
command = {_toml_string(sys.executable)}
args = [{_toml_string(str(GATEWAY_FIXTURE))}]
startup_timeout_sec = 10
tool_timeout_sec = 30
required = true
env_vars = ["ASTRA_TEST_GATEWAY_STARTED_FILE", "ASTRA_TEST_GATEWAY_COMPLETED_FILE", "ASTRA_TEST_GATEWAY_STEP_DELAY"]
'''
        (codex_home / "config.toml").write_text(config, encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "CODEX_HOME": str(codex_home),
                "OPENAI_API_KEY": "dummy",
                "ASTRA_TEST_GATEWAY_STARTED_FILE": str(started),
                "ASTRA_TEST_GATEWAY_COMPLETED_FILE": str(completed_marker),
                "ASTRA_TEST_GATEWAY_STEP_DELAY": "0.35",
            }
        )
        proc = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(ROOT),
            start_new_session=os.name != "nt",
        )
        rpc = JsonRpcClient(proc)
        observed: list[dict] = []
        try:
            rpc.send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "astra-mcp-progress-canary",
                            "title": "Astra MCP Progress Canary",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            initialized = rpc.response(1)
            if "error" in initialized:
                raise AssertionError(f"initialize failed: {initialized}")
            rpc.send({"method": "initialized"})

            rpc.send(
                {
                    "id": 2,
                    "method": "thread/start",
                    "params": {
                        "model": "mock-model",
                        "cwd": str(ROOT),
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                        "ephemeral": True,
                    },
                }
            )
            thread_response = rpc.response(2)
            if "error" in thread_response:
                raise AssertionError(
                    f"thread/start failed: {thread_response}\n{rpc.diagnostics()}"
                )
            thread_id = ((thread_response.get("result") or {}).get("thread") or {}).get(
                "id"
            )
            if not thread_id:
                raise AssertionError(f"thread/start returned no thread id: {thread_response}")

            rpc.send(
                {
                    "id": 3,
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": "Call luna_gateway delegate_via_luna exactly once, then report the result.",
                            }
                        ],
                        "model": "mock-model",
                    },
                }
            )
            turn_started = rpc.response(3)
            if "error" in turn_started:
                raise AssertionError(
                    f"turn/start failed: {turn_started}\n{rpc.diagnostics()}"
                )

            deadline = time.monotonic() + 35
            turn_completed = None
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    message = rpc.messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("timed out waiting for progress canary turn") from exc
                observed.append(message)
                if message.get("method") == "turn/completed":
                    turn_completed = message
                    break
                if message.get("id") is not None and message.get("method"):
                    raise AssertionError(
                        f"unexpected App Server request during MCP canary: {message}"
                    )

            if turn_completed is None:
                raise AssertionError("progress canary turn did not complete")
            turn = (turn_completed.get("params") or {}).get("turn") or {}
            if turn.get("status") != "completed":
                raise AssertionError(f"progress canary turn failed: {turn_completed}")
            if state.early_request:
                raise AssertionError("model was re-sampled while MCP tool call was pending")
            if len(state.requests) != 2:
                raise AssertionError(
                    f"expected exactly two model requests, got {len(state.requests)}"
                )
            if not started.exists() or not completed_marker.exists():
                raise AssertionError("slow gateway fixture did not complete its MCP lifecycle")

            progress_messages = [
                message
                for message in observed
                if message.get("method") == "item/mcpToolCall/progress"
            ]
            mcp_started = [
                message
                for message in observed
                if message.get("method") == "item/started"
                and ((message.get("params") or {}).get("item") or {}).get("type")
                == "mcpToolCall"
            ]
            mcp_completed = [
                message
                for message in observed
                if message.get("method") == "item/completed"
                and ((message.get("params") or {}).get("item") or {}).get("type")
                == "mcpToolCall"
            ]
            if not mcp_started or not mcp_completed:
                raise AssertionError("App Server did not expose the MCP tool call lifecycle")

            diagnostics = {
                "mcp_progress_forwarding_supported": bool(progress_messages),
                "progress_notifications_sent": 2,
                "progress_events_observed": len(progress_messages),
                "model_resampled_while_pending": state.early_request,
                "request_count": len(state.requests),
                "mcp_call_started": bool(mcp_started),
                "mcp_call_completed": bool(mcp_completed),
                "app_server": rpc.diagnostics(),
            }
            print(json.dumps(diagnostics, indent=2))
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            model_server.shutdown()
            model_server.server_close()
            model_thread.join(timeout=2)


if __name__ == "__main__":
    run()
