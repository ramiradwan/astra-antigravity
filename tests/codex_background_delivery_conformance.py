"""Native root keeps working, idle/active tool-output push, real subprocess and Git.

No credentials or inference: a scripted Responses endpoint controls the native
Codex harness, and a fake CLI emits real Antigravity stream-json events through
the production gateway. There are no root-model status requests.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/astra-antigravity"))
from app_server_delivery import AppServerDelivery
from luna_gateway import discover_codex
from process_lifecycle import stop_owned_process
from codex_mcp_progress_canary import JsonRpcClient, _event, _completed


def response(number, workspace):
    identifier = f"resp-background-{number}"
    if number in (1, 2):
        name = "delegate_via_luna" if number == 1 else "record_parent_work"
        args = {"task": "Implement the native background contract", "workspace": str(workspace),
                "isolation": "worktree", "request_id": "native-background"} if number == 1 else {}
        item = {"type": "function_call", "call_id": f"background-call-{number}",
                "namespace": "mcp__luna_gateway", "name": name, "arguments": json.dumps(args)}
    else:
        item = {"type": "message", "role": "assistant", "id": f"background-message-{number}",
                "content": [{"type": "output_text", "text": f"background stage {number} accepted"}]}
    events = [{"type": "response.created", "response": {"id": identifier}},
              {"type": "response.output_item.done", "item": item}, _completed(identifier)]
    return "".join(_event(event["type"], event) for event in events).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        state = self.server.state
        with state["lock"]:
            state["requests"].append(body)
            number = len(state["requests"])
        if number == 4:
            state["decision_seen"].set()
            if not state["allow_decision_response"].wait(20):
                raise RuntimeError("Active decision turn was not released")
        if number == 5:
            state["completion_seen"].set()
        if number > 5:
            self.send_response(409)
            self.end_headers()
            return
        payload = response(number, state["workspace"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


async def exercise(proc, workspace, tmp, state):
    rpc = JsonRpcClient(proc)
    next_id = 0
    async def call(method, params):
        nonlocal next_id
        next_id += 1
        request_id = next_id
        rpc.send({"id": request_id, "method": method, "params": params})
        return await asyncio.to_thread(rpc.response, request_id, 20)
    initialized = await call("initialize", {"clientInfo": {"name": "background-delivery-contract", "version": "0.8.0"},
                                            "capabilities": {"experimentalApi": True}})
    assert not initialized.get("error"), initialized
    rpc.send({"method": "initialized"})
    started = await call("thread/start", {"model": "mock-model", "cwd": str(workspace),
                                          "approvalPolicy": "never", "sandbox": "danger-full-access", "ephemeral": True})
    assert not started.get("error"), started
    thread_id = started["result"]["thread"]["id"]
    turn = await call("turn/start", {"threadId": thread_id, "input": [{"type": "text",
        "text": "Delegate the implementation, then complete independent parent work. Receive compact background events."}]})
    assert not turn.get("error"), turn
    await asyncio.to_thread(rpc.wait_for, lambda event: event.get("method") == "turn/completed", 20)
    assert (tmp / "parent-work").exists(), "Parent did not continue working"
    assert not (tmp / "release").exists(), "Worker already completed"
    assert len(state["requests"]) == 3, "Unexpected root model status sampling"
    receipt = json.loads((tmp / "receipt").read_text())
    adapter = AppServerDelivery(call, thread_id)
    relay = asyncio.create_task(adapter.relay(receipt["delegation_id"]))
    assert await asyncio.to_thread(state["decision_seen"].wait, 10), rpc.diagnostics()
    fourth = json.dumps(state["requests"][3].get("input", []))
    assert "needs_decision" in fourth, "Idle parent did not receive the decision"
    assert "function_call_output" in fourth, "Decision lost tool provenance"
    # Completion arrives while the parent's decision response is still active.
    (tmp / "release").write_text("release result")
    cursor = await asyncio.wait_for(relay, 15)
    assert cursor == 2, "Expected one decision and one terminal event"
    assert len(state["requests"]) == 4, "Queued completion incorrectly spawned concurrent generation"
    state["allow_decision_response"].set()
    assert await asyncio.to_thread(state["completion_seen"].wait, 10), rpc.diagnostics()
    await asyncio.to_thread(rpc.wait_for, lambda event: event.get("method") == "turn/completed", 15)
    fifth = json.dumps(state["requests"][4].get("input", []))
    assert "completed" in fifth and "native turn 1" in fifth, "Active parent missed queued completion"
    assert "function_call_output" in fifth, "Completion lost tool provenance"
    for marker in ("Private planning marker", "step_update", "log_path", '"pid"'):
        assert marker not in fifth, f"Private worker data leaked: {marker}"
    assert (tmp / "planner-count").read_text().splitlines() == ["plan"], "Extra planning inference"
    result = await adapter._gateway("read_delegation_result", {"delegation_id": receipt["delegation_id"]})
    worktree = Path(result["implementation"]["worktree"]["path"])
    assert worktree != workspace and (worktree / "native_result.txt").read_text() == "turn 1\n"
    assert result["antigravity"]["requested_model"] == "gemini-3.8-flash-high"
    assert result["implementation"]["structured_report_valid"] is True
    print(json.dumps({"parent_work_before_completion": True, "root_status_checks": 0,
                      "idle_parent_push": True, "active_parent_queued_push": True,
                      "root_model_requests": len(state["requests"]), "planner_turns": 1,
                      "decision_events": 1, "terminal_events": 1, "isolated_worktree_verified": True}))


def run():
    with tempfile.TemporaryDirectory(prefix="astra-background-contract-") as raw:
        tmp = Path(raw)
        workspace, home = tmp / "workspace", tmp / "home"
        workspace.mkdir()
        home.mkdir()
        subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "--allow-empty", "-qm", "base"], check=True, capture_output=True)
        state = {"lock": threading.Lock(), "requests": [], "workspace": workspace,
                 "decision_seen": threading.Event(), "allow_decision_response": threading.Event(),
                 "completion_seen": threading.Event()}
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http.state = state
        threading.Thread(target=http.serve_forever, daemon=True).start()
        env_names = ["ASTRA_ANTIGRAVITY_STATE_DIR", "ASTRA_ANTIGRAVITY_WORKTREE_ROOT", "ASTRA_FAKE_AGY_RELEASE_FILE",
                     "ASTRA_TEST_PLANNER_COUNT", "ASTRA_TEST_RECEIPT_FILE", "ASTRA_TEST_PARENT_WORK_FILE"]
        config = f'''model = "mock-model"
model_provider = "mock"
[features]
plugins = false
remote_plugin = false
hooks = false
[model_providers.mock]
name = "mock"
base_url = "http://127.0.0.1:{http.server_address[1]}/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
[mcp_servers.luna_gateway]
command = {json.dumps(sys.executable)}
args = [{json.dumps(str(ROOT / 'tests/fixtures/background_gateway_server.py'))}]
env_vars = {json.dumps(env_names)}
tool_timeout_sec = 45
startup_timeout_sec = 15
required = true
'''
        (home / "config.toml").write_text(config, encoding="utf-8")
        env = {**os.environ, "CODEX_HOME": str(home), "OPENAI_API_KEY": "dummy",
               "ASTRA_ANTIGRAVITY_STATE_DIR": str(tmp / "state"), "ASTRA_ANTIGRAVITY_WORKTREE_ROOT": str(tmp / "worktrees"),
               "ASTRA_FAKE_AGY_RELEASE_FILE": str(tmp / "release"), "ASTRA_TEST_PLANNER_COUNT": str(tmp / "planner-count"),
               "ASTRA_TEST_RECEIPT_FILE": str(tmp / "receipt"), "ASTRA_TEST_PARENT_WORK_FILE": str(tmp / "parent-work")}
        proc = subprocess.Popen([discover_codex(), "app-server"], cwd=workspace, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", start_new_session=os.name != "nt")
        try:
            asyncio.run(exercise(proc, workspace, tmp, state))
        finally:
            state["allow_decision_response"].set()
            stop_owned_process(proc)
            http.shutdown()
            http.server_close()


if __name__ == "__main__":
    run()
