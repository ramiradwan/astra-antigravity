import json
from pathlib import Path
import unittest

REPO_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_DIR / "plugins" / "astra-antigravity"
SKILL_PATH = PLUGIN_DIR / "skills" / "delegate-to-antigravity" / "SKILL.md"
CI_PATH = REPO_DIR / ".github" / "workflows" / "ci.yml"
ROLE_PROBE_PATH = REPO_DIR / "tests" / "codex_luna_role_conformance.py"
PROGRESS_CANARY_PATH = REPO_DIR / "tests" / "codex_mcp_progress_canary.py"
README_PATH = REPO_DIR / "README.md"


class TestPluginPackaging(unittest.TestCase):
    def test_manifest_and_portable_mcp_entrypoint(self):
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text())
        self.assertEqual(manifest["$schema"], "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json")
        self.assertFalse((PLUGIN_DIR / ".codex-plugin" / "plugin.json").exists())
        self.assertFalse((PLUGIN_DIR / ".mcp.json").exists())
        self.assertEqual(manifest["name"], "astra-antigravity")
        self.assertEqual(manifest["version"], "0.9.2")
        long_description = manifest["extensions"]["com.openai"]["interface"]["longDescription"]
        self.assertIn("Codex plans the task and checks the result", long_description)
        self.assertNotIn("optional lightweight Luna proxying", long_description)
        self.assertTrue((PLUGIN_DIR / "skills").exists())
        mcp_path = PLUGIN_DIR / "mcp.json"
        self.assertTrue(mcp_path.exists())
        mcp = json.loads(mcp_path.read_text())
        self.assertEqual(set(mcp["mcpServers"]), {"luna_gateway"})
        server = mcp["mcpServers"]["luna_gateway"]
        self.assertEqual(server["command"], "python")
        self.assertEqual(server["args"], ["${PLUGIN_ROOT}/launcher.py", "luna_gateway.py"])
        self.assertEqual(server.get("cwd"), "${PLUGIN_ROOT}")
        self.assertNotIn("tool_timeout_sec", server)
        self.assertNotIn("uv.exe", mcp_path.read_text().lower())
        for filename in (
            "launcher.py",
            "luna_gateway.py",
            "worktree_server.py",
            "worktree_coordinator.py",
            "advanced_coordinator.py",
            "advanced_coordinator_legacy.py",
            "git_worktrees.py",
            "quota_server.py",
            "server.py",
            "server_legacy.py",
            "task_progress.py",
            "proxy_policy.py",
            "proxy_runtime_guard.py",
        ):
            self.assertTrue((PLUGIN_DIR / filename).exists(), filename)

    def test_skill_keeps_normal_path_gateway_driven_and_concise(self):
        content = SKILL_PATH.read_text(encoding="utf-8")
        for required in (
            'delegate_via_luna(task, workspace, isolation="worktree")',
            "continue_via_luna(task_id, findings)",
            "Luna coordination is mandatory",
            "Do not fall back to direct parent Antigravity delegation",
            'model="gpt-6-luna"',
            'reasoning_effort="low"',
            "Parent verification",
            "quota_override=True",
            "cancel_delegation(task_id)",
            "cleanup_task_worktree(task_id)",
        ):
            self.assertIn(required, content)

        normal = content.split("## Normal workflow", 1)[1].split(
            "## Corrective continuation", 1
        )[0]
        for raw_tool in (
            "delegate_to_gemini",
            "await_task(task_id)",
            "wait_for_task",
            "get_task_progress_summary",
            "get_task_result",
            "steer_gemini",
        ):
            self.assertNotIn(raw_tool, normal)

        for forbidden in (
            "Inert bootstrap",
            "Attest the exact child",
            "Verify `followup_task` delivery",
            "attest_luna_proxy",
            "build_luna_activation_probe",
            "verify_luna_activation",
            "activation nonce",
            "If Luna cannot be started, use direct parent MCP delegation",
        ):
            self.assertNotIn(forbidden, content)

        self.assertLess(len(content.splitlines()), 140)
        self.assertNotIn("#32831", content)
        self.assertNotIn("#33268", content)
        self.assertNotIn("Process ownership lost across server restart", content)
        self.assertNotIn("proxy_runtime_guard.", content)

    def test_internal_server_retains_event_driven_lifecycle_and_diagnostics(self):
        content = (PLUGIN_DIR / "worktree_server.py").read_text(encoding="utf-8")
        for required in (
            "def check_antigravity_health",
            "def recommend_delegation",
            "def get_gemini_quota",
            "def delegate_to_gemini",
            "async def await_task",
            "def wait_for_task",
            "def get_task_progress_summary",
            "def get_task_result",
            "def steer_gemini",
            "def cancel_task",
            "def cleanup_task_worktree",
        ):
            self.assertIn(required, content)

        for hidden in (
            "def attest_luna_proxy",
            "def build_luna_activation_probe",
            "def verify_luna_activation",
        ):
            self.assertNotIn(hidden, content)

    def test_codex_ci_uses_supported_sandbox_prerequisite_and_truthful_role_probe(self):
        workflow = CI_PATH.read_text(encoding="utf-8")
        pinned = workflow.split("  codex-compat-pinned:", 1)[1].split(
            "  codex-compat-latest:", 1
        )[0]
        latest = workflow.split("  codex-compat-latest:", 1)[1]

        for job in (pinned, latest):
            self.assertIn("sudo apt-get install --yes bubblewrap", job)
            self.assertIn("apparmor-profiles", job)
            self.assertIn("bwrap-userns-restrict", job)
            self.assertIn("apparmor_parser -r", job)
            self.assertIn("bwrap --version", job)

        self.assertNotIn("continue-on-error: true", pinned)
        self.assertNotIn("codex_luna_role_conformance.py", pinned)
        self.assertNotIn("codex_mcp_progress_canary.py", pinned)
        self.assertIn("codex_luna_role_conformance.py", latest)
        self.assertIn("codex_mcp_progress_canary.py", latest)
        self.assertEqual(latest.count("continue-on-error: true"), 1)

        probe = ROLE_PROBE_PATH.read_text(encoding="utf-8")
        self.assertIn('"native_custom_role_supported": supported', probe)
        self.assertIn('"spawn_agent_not_exposed"', probe)
        self.assertNotIn("Pinned Codex did not expose spawn_agent", probe)

        progress_canary = PROGRESS_CANARY_PATH.read_text(encoding="utf-8")
        self.assertIn('"mcp_progress_forwarding_supported"', progress_canary)
        self.assertIn('"progress_events_observed"', progress_canary)
        self.assertIn('"model_resampled_while_pending"', progress_canary)

        readme = README_PATH.read_text(encoding="utf-8")
        self.assertIn("Native `spawn_agent` custom-role selection is **not** a production dependency", readme)
        self.assertIn("`native_custom_role_supported: true|false`", readme)
        self.assertIn("`mcp_progress_forwarding_supported: true|false`", readme)
        self.assertIn("`bwrap-userns-restrict` AppArmor profile", readme)
        self.assertIn("Plugin release: **0.9.2**", readme)
        self.assertIn("sandbox mode **on by default**", readme)
        self.assertIn("ASTRA_ANTIGRAVITY_SANDBOX=0", readme)
        self.assertIn("Windows sharing/access failures", readme)
        self.assertIn("Planning delegation", readme)
        self.assertIn("Codex clients may not yet render", readme)
        self.assertIn("No polling fallback", readme)


if __name__ == "__main__":
    unittest.main()
