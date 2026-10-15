"""Prove the root Codex context sees only the high-level Luna gateway lifecycle.

The harness uses a local fake Responses endpoint plus the slow credential-free
root gateway fixture.  It asserts that one delegate_via_luna MCP call remains in
flight without another root-model request, progress notifications do not enter
the model-visible tool output, and the root tool inventory contains no raw
Antigravity lifecycle surface.
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
GATEWAY_FIXTURE = ROOT / "tests" / "fixtures" / "codex_luna_gateway_server.py"
CALL_ID = "call-root-luna-gateway"


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
        {"type": "response.created", "response": {"id": "resp-root-gateway-call"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": CALL_ID,
                "namespace": "mcp__luna_gateway",
                "name": "delegate_via_luna",
                "arguments": json.dumps(
                    {
                        "task": "Implement the context-firewall acceptance fixture",
                        "workspace": str(ROOT),
                        "isolation": "worktree",
                    },
                    separators=(",", ":"),
                ),
            },
        },
        _completed("resp-root-gateway-call"),
    ]
    return "".join(_event(item["type"], item) for item in events).encode("utf-8")


def _second_response() -> bytes:
    events = [
        {"type": "response.created", "response": {"id": "resp-root-final"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "id": "msg-root-final",
                "content": [
                    {
                        "type": "output_text",
                        "text": "root context firewall passed",
                    }
                ],
            },
        },
        _completed("resp-root-final"),
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
    server_version = "AstraRootContextFirewall/1"

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
                {"error": "root model request arrived while Luna gateway was pending"}
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
            payload = json.dumps({"error": "unexpected extra root model request"}).encode("utf-8")
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


def _discover_codex() -> str:
    sys.path.insert(0, str(ROOT / "plugins" / "astra-antigravity"))
    from luna_gateway import discover_codex
    codex = discover_codex()
    if not codex:
        raise RuntimeError("Codex executable not found; install the pinned CLI first")
    if sys.platform == "win32" and codex.lower().endswith(".cmd"):
        target_dir = Path(codex).resolve().parent
        for candidate in ("codex.exe", "codex"):
            direct = target_dir / candidate
            if direct.is_file() and direct.name.endswith(".exe"):
                return str(direct)
        node_modules_bin = target_dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.exe"
        if node_modules_bin.is_file():
            return str(node_modules_bin)
    return codex


def run() -> None:
    codex = _discover_codex()

    with tempfile.TemporaryDirectory(prefix="astra-root-firewall-") as tmp_raw:
        tmp = Path(tmp_raw)
        codex_home = tmp / "codex-home"
        codex_home.mkdir()
        started = tmp / "gateway-started"
        completed = tmp / "gateway-completed"

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
                "ASTRA_TEST_GATEWAY_COMPLETED_FILE": str(completed),
                "ASTRA_TEST_GATEWAY_STEP_DELAY": "0.7",
            }
        )
        command = [
            codex,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(ROOT),
            "Call luna_gateway delegate_via_luna exactly once for this implementation task, then report the result.",
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
            "gateway_started": started.exists(),
            "gateway_completed": completed.exists(),
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
        if proc.returncode != 0:
            raise AssertionError("Codex exited non-zero:\n" + json.dumps(diagnostics, indent=2))
        if state.early_request:
            raise AssertionError(
                "Root Codex re-sampled while delegate_via_luna was pending:\n"
                + json.dumps(diagnostics, indent=2)
            )
        if not started.exists() or not completed.exists():
            raise AssertionError(
                "Root Codex did not execute the full gateway fixture:\n"
                + json.dumps(diagnostics, indent=2)
            )
        if len(state.requests) != 2:
            raise AssertionError(
                "Expected exactly two root model requests:\n" + json.dumps(diagnostics, indent=2)
            )

        first_tools = json.dumps(state.requests[0].get("tools") or [], separators=(",", ":"))
        if "delegate_via_luna" not in first_tools:
            raise AssertionError("Root tool inventory did not expose delegate_via_luna")
        for forbidden in (
            "delegate_to_gemini",
            "await_task",
            "wait_for_task",
            "get_task_progress_summary",
            "read_task_progress",
            "get_task_result",
            "steer_gemini",
        ):
            if forbidden in first_tools:
                raise AssertionError(f"Raw Antigravity tool leaked into root inventory: {forbidden}")

        second_input = json.dumps(state.requests[1].get("input") or [], separators=(",", ":"))
        if CALL_ID not in second_input or "function_call_output" not in second_input:
            raise AssertionError("Root follow-up request did not contain the gateway tool output")
        for forbidden in (
            "Inspecting repository",
            "Running pytest -q",
            "step_update",
            "log_path",
            '"pid"',
        ):
            if forbidden in second_input:
                raise AssertionError(f"Progress/diagnostic data leaked into root tool output: {forbidden}")
        for required in ("task-root-firewall", "gpt-6-luna", "gateway fixture complete"):
            if required not in second_input:
                raise AssertionError(f"Compact terminal gateway output missing {required!r}")
        if "root context firewall passed" not in proc.stdout:
            raise AssertionError(
                "Codex did not emit the scripted final answer:\n" + json.dumps(diagnostics, indent=2)
            )

        print(
            json.dumps(
                {key: value for key, value in diagnostics.items() if key not in {"stdout", "stderr"}},
                indent=2,
            )
        )


if __name__ == "__main__":
    run()
