"""Capability probe for selecting a model-pinned Luna custom agent.

This uses a local fake Responses endpoint and a real installed Codex CLI. It does
not call OpenAI. The first root-model request is captured and inspected to report
whether the model-visible spawn_agent schema can select the configured
``astra_luna_coordinator`` role.

Native custom-role selection is not required by Astra's production gateway,
which uses a direct Codex App Server Luna planning turn. Missing optional schema
capabilities are therefore reported as ``native_custom_role_supported: false``
without turning an informational canary into a failed step. Harness/protocol
failures still raise and exit non-zero.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLE_NAME = "astra_luna_coordinator"


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


def _final_response() -> bytes:
    events = [
        {"type": "response.created", "response": {"id": "resp-role-probe"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "id": "msg-role-probe",
                "content": [
                    {
                        "type": "output_text",
                        "text": "luna role conformance probe captured",
                    }
                ],
            },
        },
        _completed("resp-role-probe"),
    ]
    return "".join(_event(item["type"], item) for item in events).encode("utf-8")


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def record(self, body: dict) -> None:
        with self.lock:
            self.requests.append(body)


class ResponsesHandler(BaseHTTPRequestHandler):
    server_version = "AstraLunaRoleConformance/3"

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
        body = json.loads(raw or b"{}")
        state: State = self.server.state  # type: ignore[attr-defined]
        state.record(body)

        payload = _final_response()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


def _find_spawn_tool(request: dict) -> dict | None:
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("name") == "spawn_agent":
            return tool
        if tool.get("type") == "namespace" and tool.get("name") in {
            "collaboration",
            "multi_agent_v1",
        }:
            for nested in tool.get("tools") or []:
                if isinstance(nested, dict) and nested.get("name") == "spawn_agent":
                    return nested
    return None


def _role_is_advertised(tool: dict) -> bool:
    params = tool.get("parameters") or {}
    props = params.get("properties") or {}
    agent_type = props.get("agent_type")
    if not isinstance(agent_type, dict):
        return False
    description = str(agent_type.get("description") or "")
    return ROLE_NAME in description


def _report_supported(supported: bool, reason: str, **extra) -> None:
    payload = {
        "native_custom_role_supported": supported,
        "reason": reason,
        **extra,
    }
    print(json.dumps(payload, indent=2))


def run() -> None:
    codex = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if not codex:
        raise RuntimeError("Codex executable not found; install the pinned CLI first")

    with tempfile.TemporaryDirectory(prefix="astra-luna-role-") as tmp_raw:
        tmp = Path(tmp_raw)
        codex_home = tmp / "codex-home"
        agents_dir = codex_home / "agents"
        agents_dir.mkdir(parents=True)

        role_file = agents_dir / f"{ROLE_NAME}.toml"
        role_file.write_text(
            '''name = "astra_luna_coordinator"
description = "Required coordinator for Antigravity Gemini delegation."
model = "gpt-6-luna"
model_reasoning_effort = "low"
sandbox_mode = "read-only"
developer_instructions = """
Coordinate Antigravity delegation only. Do not implement repository changes.
"""
''',
            encoding="utf-8",
        )

        state = State()
        server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        server.state = state  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        config = f'''model = "gpt-6.1-sol"
model_provider = "mock"

[model_providers.mock]
name = "mock"
base_url = "http://{host}:{port}/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"

[features]
multi_agent = true

[features.multi_agent_v2]
enabled = true
hide_spawn_agent_metadata = false
expose_spawn_agent_model_overrides = true

[agents]
enabled = true
interrupt_message = false

[agents.{ROLE_NAME}]
description = "Required coordinator for Antigravity Gemini delegation."
config_file = {json.dumps(str(role_file))}
'''
        (codex_home / "config.toml").write_text(config, encoding="utf-8")

        env = os.environ.copy()
        env.update({"CODEX_HOME": str(codex_home), "OPENAI_API_KEY": "dummy"})
        command = [
            codex,
            "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(ROOT),
            "Use the astra_luna_coordinator subagent for this delegation probe.",
        ]
        try:
            proc = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        diagnostics = {
            "returncode": proc.returncode,
            "request_count": len(state.requests),
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-4000:],
        }
        if proc.returncode != 0:
            raise AssertionError("Codex exited non-zero:\n" + json.dumps(diagnostics, indent=2))
        if len(state.requests) != 1:
            raise AssertionError(
                "Expected exactly one root model request for schema capture:\n"
                + json.dumps(diagnostics, indent=2)
            )

        request = state.requests[0]
        tool = _find_spawn_tool(request)
        if tool is None:
            tool_names = [
                t.get("name") for t in request.get("tools") or [] if isinstance(t, dict)
            ]
            _report_supported(
                False,
                "spawn_agent_not_exposed",
                request_count=len(state.requests),
                tool_names=tool_names,
            )
            return

        props = (tool.get("parameters") or {}).get("properties") or {}
        if "fork_turns" not in props:
            _report_supported(
                False,
                "fork_turns_not_exposed",
                request_count=len(state.requests),
            )
            return
        if "agent_type" not in props:
            _report_supported(
                False,
                "agent_type_not_exposed",
                request_count=len(state.requests),
                fork_turns_exposed=True,
            )
            return
        if not _role_is_advertised(tool):
            _report_supported(
                False,
                "configured_role_not_advertised",
                request_count=len(state.requests),
                fork_turns_exposed=True,
                agent_type_exposed=True,
            )
            return

        _report_supported(
            True,
            "configured_role_selectable",
            request_count=len(state.requests),
            fork_turns_exposed=True,
            agent_type_exposed=True,
            role_advertised=True,
        )


if __name__ == "__main__":
    run()
