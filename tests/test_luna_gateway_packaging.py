import json
from pathlib import Path
import unittest

REPO_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_DIR / "plugins" / "astra-antigravity"
SKILL_PATH = PLUGIN_DIR / "skills" / "delegate-to-antigravity" / "SKILL.md"


class LunaGatewayPackagingTests(unittest.TestCase):
    def test_root_plugin_exposes_only_luna_gateway_transport(self):
        mcp = json.loads((PLUGIN_DIR / "mcp.json").read_text(encoding="utf-8"))
        servers = mcp.get("mcpServers") or {}
        self.assertEqual(set(servers), {"luna_gateway"})

        gateway = servers["luna_gateway"]
        self.assertEqual(gateway.get("command"), "python")
        self.assertEqual(gateway.get("args"), ["${PLUGIN_ROOT}/launcher.py", "luna_gateway.py"])
        self.assertEqual(gateway.get("cwd"), "${PLUGIN_ROOT}")
        self.assertEqual(mcp["$schema"], "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json")
        self.assertNotIn("tool_timeout_sec", gateway)
        self.assertNotIn("env_vars", gateway)

        # The raw Antigravity lifecycle server remains an internal implementation
        # module; it must not be a root-visible plugin MCP transport.
        self.assertNotIn("antigravity", servers)
        self.assertTrue((PLUGIN_DIR / "worktree_server.py").is_file())
        self.assertTrue((PLUGIN_DIR / "luna_gateway.py").is_file())

    def test_gateway_surface_is_high_level_and_context_bounded(self):
        content = (PLUGIN_DIR / "luna_gateway.py").read_text(encoding="utf-8")
        for required in (
            "async def delegate_via_luna",
            "async def continue_via_luna",
            "def cancel_delegation",
            "def cleanup_task_worktree",
            "def check_integration",
            "def inspect_delegation",
            'DEFAULT_PLANNER_MODEL = "gpt-6-luna"',
            'DEFAULT_PLANNER_EFFORT = "low"',
        ):
            self.assertIn(required, content)

        # Root-visible tools must not recreate the old raw orchestration surface.
        for forbidden in (
            "def delegate_to_gemini(",
            "async def await_task(",
            "def wait_for_task(",
            "def get_task_progress_summary(",
            "def read_task_progress(",
            "def get_task_result(",
            "def steer_gemini(",
        ):
            self.assertNotIn(forbidden, content)

    def test_sandbox_is_deployment_policy_not_model_visible_argument(self):
        gateway = (PLUGIN_DIR / "luna_gateway.py").read_text(encoding="utf-8")
        delegate_signature = gateway.split("async def delegate_via_luna(", 1)[1].split(
            ") -> dict:", 1
        )[0]
        continue_signature = gateway.split("async def continue_via_luna(", 1)[1].split(
            ") -> dict:", 1
        )[0]
        self.assertNotIn("sandbox", delegate_signature.lower())
        self.assertNotIn("sandbox", continue_signature.lower())

        internal_server = (PLUGIN_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn("ASTRA_ANTIGRAVITY_SANDBOX", internal_server)
        self.assertIn('cmd.append("--sandbox")', internal_server)

    def test_skill_makes_luna_mandatory_and_fails_closed(self):
        content = SKILL_PATH.read_text(encoding="utf-8")
        normal = content.split("## Normal workflow", 1)[1].split("## Optional controls", 1)[0]

        self.assertIn(
            'delegate_via_luna(task, workspace, isolation="worktree")', normal
        )
        self.assertNotIn("delegate_to_gemini", normal)
        self.assertNotIn("await_task", normal)
        self.assertNotIn("get_task_progress_summary", normal)
        self.assertNotIn("get_task_result", normal)

        self.assertIn("Luna coordination is mandatory", content)
        self.assertIn("Do not fall back to direct parent Antigravity delegation", content)
        self.assertIn("continue_via_luna(task_id, findings)", content)
        self.assertIn("Parent verification", content)

        for forbidden in (
            "A lifecycle proxy is optional",
            "If Luna cannot be started, use direct parent MCP delegation",
        ):
            self.assertNotIn(forbidden, content)


if __name__ == "__main__":
    unittest.main()
