import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "astra-antigravity"
CLI = ROOT / "tests" / "fixtures" / "readiness_cli.py"


class ReadinessMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="astra readiness ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        for args in (("init", "--quiet", str(self.workspace)),
                     ("-C", str(self.workspace), "-c", "user.name=Test",
                      "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "base")):
            subprocess.run([shutil.which("git"), *args], stdin=subprocess.DEVNULL,
                           capture_output=True, check=True, timeout=10)
        self.env = {**os.environ,
            "CODEX_PATH": str(self.make_cli("codex")),
            "AGY_PATH": str(self.make_cli("agy")),
            "ASTRA_ANTIGRAVITY_STATE_DIR": str(self.root / "state"),
            "ASTRA_PROBE_CALLS": str(self.root / "calls"),
            "ASTRA_PROBE_STARTED": str(self.root / "started"),
            "ASTRA_ANTIGRAVITY_PLANNER_MODEL": "gpt-6-luna",
            "ASTRA_ANTIGRAVITY_LIGHT_MODEL": "sonnet-5.5",
            "ASTRA_ANTIGRAVITY_HEAVY_MODEL": "argon-high",
            "OPENAI_API_KEY": "dummy"}

    def make_cli(self, role):
        path = self.root / (role + (".cmd" if os.name == "nt" else ""))
        if os.name == "nt":
            code = f'@"{sys.executable}" "{CLI}" {role} %*\n'
        else:
            code = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(CLI))} {role} "$@"\n'
        path.write_text(code, encoding="utf-8")
        path.chmod(0o700)
        return path

    async def call_readiness(self, *, slow=False, oversized=False, arguments=None):
        env = {**self.env}
        if slow:
            env["ASTRA_PROBE_SLOW"] = "1"
        if oversized:
            env["ASTRA_PROBE_OVERSIZED"] = "1"
        params = StdioServerParameters(command=sys.executable,
            args=[str(PLUGIN / "luna_gateway.py")], cwd=str(self.root), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write,
                    read_timeout_seconds=timedelta(seconds=12)) as session:
                await session.initialize()
                pending = asyncio.create_task(session.call_tool("check_integration",
                    {"workspace": str(self.workspace), **(arguments or {})}))
                if slow:
                    async def wait_for_probe():
                        while not (self.root / "started").exists():
                            await asyncio.sleep(0.01)
                    await asyncio.wait_for(wait_for_probe(), timeout=5)
                    # Production MCP must serve another request while a probe waits.
                    await asyncio.wait_for(session.send_ping(), timeout=1)
                result = await pending
                self.assertFalse(result.isError, result.content)
                self.assertIsNone(result.structuredContent)
                self.assertEqual(len(result.content), 1)
                self.assertLessEqual(len(result.content[0].text), 8000)
                return json.loads(result.content[0].text)

    async def test_production_stdio_probes_closed_input_without_inference(self):
        report = await self.call_readiness()
        self.assertTrue(report["ready"])
        self.assertEqual(report["ready_for"], {"light": True, "heavy": False})
        self.assertTrue(report["executables"]["git"]["ready"])
        self.assertEqual((self.root / "calls").read_text().splitlines(),
                         ["codex:--version", "agy:--version", "agy:models"])
        self.assertFalse(any((self.root / "state").glob("task-*.json")))

    async def test_stdio_remains_responsive_during_blocking_probe(self):
        report = await self.call_readiness(slow=True)
        self.assertTrue(report["ready"])

    async def test_oversized_catalog_cannot_advertise_partial_models(self):
        report = await self.call_readiness(oversized=True)
        self.assertFalse(report["ready"])
        self.assertEqual(report["model_availability"]["profiles"], {})
        self.assertIn("output exceeded", report["model_availability"]["error"])
        self.assertFalse(report["ready_for"]["heavy"])

    async def test_shared_non_git_workspace_is_ready_for_document_tasks(self):
        folder = self.root / "documents"
        folder.mkdir()
        report = await self.call_readiness(arguments={"workspace": str(folder), "isolation": "shared", "task_type": "document"})
        self.assertTrue(report["ready"])
        self.assertFalse(report["workspace"]["git_repository"])
        self.assertEqual(report["task_type"], "document")
        self.assertEqual(report["isolation"], "shared")
        self.assertEqual(set(report["task_types"]), {"implementation", "analysis", "research", "document"})

    async def test_external_requirements_are_unready_without_running_inference(self):
        report = await self.call_readiness(arguments={"task_type": "research", "required_capabilities": ["web"]})
        self.assertFalse(report["ready"])
        self.assertEqual(report["capabilities"]["unavailable"], ["web"])
        self.assertTrue(any("capabilities" in item for item in report["remediation"]))
        self.assertEqual((self.root / "calls").read_text().splitlines(),
                         ["codex:--version", "agy:--version", "agy:models"])


if __name__ == "__main__":
    unittest.main()
