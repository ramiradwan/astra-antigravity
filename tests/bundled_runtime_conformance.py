"""Discover all gateway tools from the extracted native bundle with uv disabled.

No model inference, credentials or user config writes. Run under each Python/OS
profile with the locked client dependencies installed by the test harness.
"""

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {"delegate_via_luna", "continue_via_luna", "cancel_delegation", "cleanup_task_worktree",
            "check_integration", "inspect_delegation", "receive_delegation_events",
            "ack_delegation_events", "read_delegation_result"}


async def run(archive):
    with tempfile.TemporaryDirectory(prefix="astra bundled runtime ") as raw:
        root = Path(raw)
        plugin = root / "plugin with spaces"
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(plugin)
        entry = json.loads((plugin / "mcp.json").read_text())["mcpServers"]["luna_gateway"]
        assert entry["args"] == ["-I", "${PLUGIN_ROOT}/runtime_bootstrap.py", "luna_gateway.py"]
        poison = root / "poison"
        poison.mkdir()
        (poison / "mcp.py").write_text('raise RuntimeError("PYTHONPATH must be ignored")\n')
        params = StdioServerParameters(
            command=sys.executable, args=[arg.replace("${PLUGIN_ROOT}", str(plugin)) for arg in entry["args"]], cwd=str(plugin),
            env={**os.environ, "UV_OFFLINE": "1", "UV_PATH": str(root / "missing-uv"),
                 "PYTHONPATH": str(poison), "AGY_PATH": str(root / "missing-agy"),
                 "ASTRA_ANTIGRAVITY_STATE_DIR": str(root / "state")})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.version == "1.29.1"
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == EXPECTED
                for tool in listed.tools:
                    assert tool.annotations is not None
                    for field in ("readOnlyHint", "destructiveHint", "openWorldHint"):
                        assert type(getattr(tool.annotations, field)) is bool, (tool.name, field)
                    if tool.name in {"delegate_via_luna", "check_integration"}:
                        assert set(tool.inputSchema["properties"]["task_type"]["enum"]) == {
                            "implementation", "analysis", "research", "document"}
                response = await session.call_tool("check_integration", {
                    "workspace": str(root), "isolation": "shared", "task_type": "analysis"})
                assert not response.isError
                readiness = json.loads(response.content[0].text)
                assert readiness["plugin_version"] == "0.9.2"
                assert not readiness["ready_for"]["light"]
        print(json.dumps({"python": f"{sys.version_info.major}.{sys.version_info.minor}",
                          "platform": sys.platform, "gateway_tools": len(EXPECTED),
                          "sdk_version": initialized.serverInfo.version,
                          "uv_disabled": True, "pythonpath_poison_ignored": True,
                          "model_requests": 0, "extracted_bundle_startup_passed": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    asyncio.run(run(parser.parse_args().archive))
