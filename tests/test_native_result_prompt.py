import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
TEST_DIR = ROOT / "tests"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(TEST_DIR))

from advanced_coordinator import HANDOFF_MARKER, AdvancedWorktreeCoordinator
from test_roadmap import FakeQuota


class NativeResultPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.quota = FakeQuota(self.root / "state")
        self.coordinator = AdvancedWorktreeCoordinator(self.quota)

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_native_prompt(self, prompt):
        self.assertNotIn(HANDOFF_MARKER, prompt)
        self.assertNotIn("quota_impact", prompt)
        self.assertNotIn("commit_sha", prompt)
        self.assertNotIn("exactly one JSON object", prompt)
        self.assertIn("required structured result", prompt)
        self.assertIn("Run appropriate tests", prompt)

    def test_delegate_uses_semantic_native_result_instruction(self):
        self.coordinator.delegate("Implement feature", str(self.workspace), "shared")
        self._assert_native_prompt(self.quota.prompts[-1])

    def test_steer_uses_semantic_native_result_instruction(self):
        delegated = self.coordinator.delegate("Implement feature", str(self.workspace), "shared")
        task_id = delegated["task_id"]
        self.quota.base.records[task_id].status = "completed"
        self.coordinator.steer(task_id, "Fix the regression")
        self._assert_native_prompt(self.quota.prompts[-1])


if __name__ == "__main__":
    unittest.main()
