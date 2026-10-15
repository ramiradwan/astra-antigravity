"""Credential-free pinned-Codex conformance for mandatory Luna planning.

The production gateway does not depend on native custom roles or planner
lifecycle tools. It requires a fresh read-only Luna/low App Server turn
to return one structured coordination plan before Gemini starts. This harness
exercises that exact contract against a real Codex App Server while routing the
model request to a local fake Responses endpoint.
"""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import patch
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANNER_CANDIDATES = ["gpt-6-luna", "gpt-6.1-sol"]
PLAN = {
    "reasoning_requirement": "light",
    "coordination_brief": "Keep the implementation scoped to the requested feature.",
    "acceptance_focus": ["Run pytest -q", "Inspect the final diff"],
    "risks": ["Preserve compatibility"],
}
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


def _model_response() -> bytes:
    plan_text = json.dumps(PLAN, separators=(",", ":"))
    events = [
        {"type": "response.created", "response": {"id": "resp-luna-plan"}},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "id": "msg-luna-plan",
                "content": [{"type": "output_text", "text": plan_text}],
            },
        },
        _completed("resp-luna-plan"),
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
    server_version = "AstraLunaPlanningConformance/1"

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
        state.record(body)
        payload = _model_response()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


async def run_plans(tmp, state):
    # Import after selecting a private state directory. No credentials/config edits.
    sys.path.insert(0, str(ROOT / "plugins" / "astra-antigravity"))
    import luna_gateway
    workspace = tmp / "workspace with spaces"
    workspace.mkdir()
    for model in PLANNER_CANDIDATES:
        with patch.dict(os.environ, {"ASTRA_ANTIGRAVITY_PLANNER_MODEL": model}):
            result = await luna_gateway._run_luna_plan(
                "Plan this scoped implementation task.", str(workspace), mode="delegation")
        if result != PLAN:
            raise AssertionError(f"Unexpected plan from {model}: {result}")
    if len(state.requests) != len(PLANNER_CANDIDATES):
        raise AssertionError(f"Expected one request per planner: {len(state.requests)}")
    for model, request in zip(PLANNER_CANDIDATES, state.requests):
        if request.get("model") != model or (request.get("reasoning") or {}).get("effort") != "low":
            raise AssertionError("Planner model/effort mismatch")
        tools = request.get("tools") or []
        if any(tool.get("name") not in {"request_user_input", "view_image"} for tool in tools):
            raise AssertionError(f"Tools leaked into the planning request: {tools}")
        if (request.get("text") or {}).get("format", {}).get("schema") != luna_gateway.LUNA_PLAN_SCHEMA:
            raise AssertionError("Production output schema was not sent")


def run():
    with tempfile.TemporaryDirectory(prefix="astra-production-planner-") as tmp_raw:
        tmp = Path(tmp_raw)
        home = tmp / "codex-home"
        home.mkdir()
        state = State()
        model_server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesHandler)
        model_server.state = state
        thread = threading.Thread(target=model_server.serve_forever, daemon=True)
        thread.start()
        host, port = model_server.server_address
        marker = tmp / "inherited-mcp-must-not-start"
        fixture = tmp / "inherited.py"
        fixture.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n", encoding="utf-8")
        (home / "config.toml").write_text(f'''model = "gpt-6.1-sol"
model_provider = "mock"
[features]
plugins = false
remote_plugin = false
[model_providers.mock]
name = "mock"
base_url = "http://{host}:{port}/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
[mcp_servers.inherited_required]
command = {json.dumps(sys.executable)}
args = [{json.dumps(str(fixture))}]
required = true
startup_timeout_sec = 1
''', encoding="utf-8")
        env = {"CODEX_HOME": str(home), "OPENAI_API_KEY": "dummy",
               "ASTRA_ANTIGRAVITY_STATE_DIR": str(tmp / "state"),
               "ASTRA_ANTIGRAVITY_PLANNER_EFFORT": "low"}
        try:
            with patch.dict(os.environ, env):
                asyncio.run(run_plans(tmp, state))
            if marker.exists():
                raise AssertionError("Inherited MCP server started in the private planner")
            print(json.dumps({"verified_models": PLANNER_CANDIDATES,
                              "requests": len(state.requests), "inherited_mcp_disabled": True}))
        finally:
            model_server.shutdown()
            model_server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    run()
