"""Install and discover the real packaged plugin in an isolated Codex home.

No inference, credentials or user config edits. This exercises compatibility
portable manifest path resolution, runtime startup and tool registration.
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from codex_mcp_progress_canary import JsonRpcClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astra-antigravity"))
from luna_gateway import discover_codex
from task_types import TASK_TYPES, result_schema
sys.path.insert(0, str(ROOT / "scripts"))
from build_release import build

PLUGIN = ROOT / "plugins" / "astra-antigravity"
VERSION = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"]


def startup_diagnostic_text(value, limit):
    """Bound fixture errors without printing any credential-shaped values."""
    text = re.sub(r"\bBearer\s+[^\s\"',;]+", "Bearer [redacted]", str(value), flags=re.IGNORECASE)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text, flags=re.IGNORECASE)
    text = re.sub(r"([\"']?(?:[a-z0-9_]*(?:token|secret|password|api_key)|code_verifier)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}&]+",
                  r"\1[redacted]", text, flags=re.IGNORECASE)
    return text[-limit:]


def finish_owned_process(proc):
    """Finish this test's CLI and any children still writing its isolated home."""
    from process_lifecycle import stop_owned_process
    if proc.stdin and not proc.stdin.closed:
        proc.stdin.close()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        stop_owned_process(proc)
    if os.name != "nt":
        # Every test CLI is launched in its own session. Its leader can exit
        # while an asynchronous marketplace git clone is still writing; the
        # retained process group remains ours even after the leader is reaped.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                os.killpg(proc.pid, 0)
                time.sleep(0.05)
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            pipe.close()


def run():
    codex = discover_codex()
    with tempfile.TemporaryDirectory(prefix="astra plugin installation ") as raw:
        tmp = Path(raw)
        home = tmp / "codex home"
        home.mkdir()
        workspace = tmp / "workspace"
        workspace.mkdir()
        # Exercise the actual standalone archive, not a direct source install.
        entries = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))
        archive = build(PLUGIN, tmp / "release", entries)["archive"]
        marketplace = tmp / "archive marketplace"
        catalog = marketplace / ".agents/plugins/marketplace.json"
        catalog.parent.mkdir(parents=True)
        catalog_data = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        # This fixture extracts only the local package being verified. The
        # shared repository catalog also offers the separate HTTPS edition.
        catalog_data["plugins"] = [entry for entry in catalog_data["plugins"] if entry["name"] == "astra-antigravity"]
        catalog.write_text(json.dumps(catalog_data), encoding="utf-8")
        packaged = marketplace / "plugins/astra-antigravity"
        packaged.mkdir(parents=True)
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(packaged)
        env = {**os.environ, "CODEX_HOME": str(home), "OPENAI_API_KEY": "dummy",
               "ASTRA_ANTIGRAVITY_STATE_DIR": str(tmp / "state")}

        def command(*args):
            proc = subprocess.Popen([codex, *args], cwd=workspace, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    start_new_session=os.name != "nt")
            try:
                stdout, stderr = proc.communicate(timeout=45)
                result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
            finally:
                finish_owned_process(proc)
            if result.returncode:
                raise AssertionError(f"Codex command failed: {args[0]}\n{result.stderr[-1000:]}")
            return result

        command("plugin", "marketplace", "add", str(marketplace))
        command("plugin", "add", "astra-antigravity@astra-antigravity-local", "--json")
        manifests = list((home / "plugins" / "cache").glob("**/plugin.json"))
        installed = [path for path in manifests if json.loads(path.read_text(encoding="utf-8")).get("version") == VERSION
                     and json.loads(path.read_text(encoding="utf-8")).get("name") == "astra-antigravity"]
        if len(installed) != 1:
            raise AssertionError(f"Plugin {VERSION} was not installed from the marketplace")
        for kind in TASK_TYPES:
            cached_schema = installed[0].parent / "schemas" / f"{kind}_result.schema.json"
            if json.loads(cached_schema.read_text(encoding="utf-8")) != result_schema(kind):
                raise AssertionError(f"Packaged {kind} schema differs from the source contract")
        existing_config = (home / "config.toml").read_text(encoding="utf-8")
        # This test never invokes a model. A non-OpenAI provider avoids account
        # catalog refresh/network work competing with cold Windows MCP startup.
        (home / "config.toml").write_text('''model_provider = "installation-test"
[model_providers.installation-test]
name = "installation-test"
base_url = "http://127.0.0.1:1/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
[features]
plugins = true
remote_plugin = false
''' + existing_config + '''
[plugins."astra-antigravity@astra-antigravity-local".mcp_servers.luna_gateway]
startup_timeout_sec = 60
tool_timeout_sec = 3600
env_vars = ["CODEX_HOME", "ASTRA_ANTIGRAVITY_STATE_DIR"]
''', encoding="utf-8")
        proc = subprocess.Popen([codex, "app-server"], cwd=workspace, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                start_new_session=os.name != "nt")
        rpc = JsonRpcClient(proc)
        try:
            rpc.send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "packaged-plugin-test", "version": "1"},
                "capabilities": {"experimentalApi": True}}})
            rpc.response(1)
            rpc.send({"method": "initialized"})
            rpc.send({"id": 2, "method": "thread/start", "params": {
                "model": "gpt-6.1-sol", "cwd": str(workspace), "approvalPolicy": "never",
                "sandbox": "read-only", "ephemeral": True}})
            started = rpc.response(2)
            rpc.send({"id": 3, "method": "mcpServerStatus/list", "params": {
                "threadId": started.get("result", {}).get("thread", {}).get("id")}})
            discovery_started = time.monotonic()
            try:
                status = rpc.response(3, timeout=60)
            except Exception as exc:
                raise AssertionError("Packaged MCP discovery failed: " + json.dumps(rpc.diagnostics())) from exc
            if status.get("error"):
                raise AssertionError(status["error"])
            servers = (status.get("result") or {}).get("data") or []
            matching = [server for server in servers if server.get("name") == "luna_gateway"]
            if len(matching) != 1 or matching[0].get("toolsError"):
                diagnostics = rpc.diagnostics()
                raise AssertionError("Packaged gateway failed to start: " + json.dumps({
                    "discovery_seconds": round(time.monotonic() - discovery_started, 3),
                    "servers": [{"name": startup_diagnostic_text(server.get("name"), 160),
                                 "toolsError": startup_diagnostic_text(server.get("toolsError") or "", 1200)}
                                for server in servers[:32]],
                    "returncode": diagnostics["returncode"],
                    "stderr": startup_diagnostic_text(diagnostics["stderr"], 4000),
                }))
            exposed = set(matching[0].get("tools") or {})
            expected = {"delegate_via_luna", "continue_via_luna", "cancel_delegation",
                        "cleanup_task_worktree", "check_integration", "inspect_delegation",
                        "receive_delegation_events", "ack_delegation_events", "read_delegation_result"}
            if exposed != expected:
                raise AssertionError(f"Unexpected packaged tools: {exposed}")
            for tool in ("delegate_via_luna", "check_integration"):
                properties = matching[0]["tools"][tool]["inputSchema"]["properties"]
                if set(properties["task_type"]["enum"]) != set(TASK_TYPES) or "required_capabilities" not in properties:
                    raise AssertionError(f"Packaged {tool} does not expose the typed capability contract")
            if matching[0].get("serverInfo", {}).get("version") != "1.29.1":
                raise AssertionError("Pinned SDK was not used by the packaged gateway")
            print(json.dumps({"plugin_version": VERSION, "packaged_gateway_tools": sorted(exposed),
                              "task_types": list(TASK_TYPES), "typed_schemas_verified": True,
                              "installed_from_release_zip": True, "portable_manifest": True,
                              "plugin_root_with_spaces": True, "model_requests": 0,
                              "discovery_seconds": round(time.monotonic() - discovery_started, 3)}))
        finally:
            finish_owned_process(proc)


if __name__ == "__main__":
    run()
