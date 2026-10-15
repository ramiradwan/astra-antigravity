import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
TEST_DIR = ROOT / "tests"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(TEST_DIR))

from advanced_coordinator import HANDOFF_MARKER, AdvancedWorktreeCoordinator
from test_roadmap import FakeQuota, git


class TruncatingQuota(FakeQuota):
    def result(self, task_id, max_chars):
        raw = super().result(task_id, max_chars)
        full = raw["response"]
        truncated = len(full) > max_chars
        if truncated:
            raw["response"] = full[:max_chars] + f"\n... [Truncated: {len(full) - max_chars} additional characters]"
        raw["truncated"] = truncated
        return raw


class ResultContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "app.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "app.txt")
        git(self.repo, "commit", "-q", "-m", "base")
        self.quota = TruncatingQuota(self.root / "state")
        self.coordinator = AdvancedWorktreeCoordinator(self.quota)

    def tearDown(self):
        self.tmp.cleanup()

    def _completed_task_with_long_handoff(self):
        delegated = self.coordinator.delegate("Implement result contract", str(self.repo), "shared")
        task_id = delegated["task_id"]
        (self.repo / "app.txt").write_text("changed\n", encoding="utf-8")
        report = {
            "summary": "implemented after a very long response",
            "files_changed": ["claimed.txt"],
            "tests_run": ["pytest -q"],
            "tests_passed": True,
            "remaining_work": [],
            "risks": ["review diff"],
            "commit_sha": None,
            "worktree": str(self.repo),
            "quota_impact": {},
        }
        full = "analysis-prefix\n" + ("x" * 5000) + "\n" + HANDOFF_MARKER + "\n" + json.dumps(report)
        self.quota.responses[task_id] = full
        record = self.quota.base.records[task_id]
        record.latest_response = full
        record.status = "completed"
        return task_id

    def test_handoff_parses_from_full_response_before_raw_truncation(self):
        task_id = self._completed_task_with_long_handoff()
        result = self.coordinator.result(task_id, max_chars=100)
        implementation = result["implementation"]

        self.assertTrue(result["truncated"])
        self.assertTrue(implementation["structured_report_valid"])
        self.assertEqual(implementation["summary"], "implemented after a very long response")
        self.assertEqual(implementation["tests_run"], ["pytest -q"])
        self.assertTrue(implementation["tests_passed"])
        self.assertFalse(any("parseable structured" in risk for risk in implementation["risks"]))

    def test_compact_result_omits_diagnostic_payloads(self):
        task_id = self._completed_task_with_long_handoff()
        result = self.coordinator.compact_result(task_id)
        serialized = json.dumps(result)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["implementation"]["summary"], "implemented after a very long response")
        for forbidden in (
            "response",
            "log_path",
            "pid",
            "usage_cumulative",
            "duration_cumulative",
            "quota_before",
            "quota_after",
        ):
            self.assertNotIn(forbidden, result)
        self.assertLess(len(serialized), 12000)


if __name__ == "__main__":
    unittest.main()
