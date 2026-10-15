import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

from advanced_coordinator import HANDOFF_MARKER, AdvancedWorktreeCoordinator
from git_worktrees import (
    GIT_HEALTH_TIMEOUT_SECONDS,
    probe_git_workspace,
    run_git_bounded,
)
import proxy_policy


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class FakeBase:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.records = {}
        self._tasks = self.records
        self._session_processes = {}

    def get_task(self, task_id):
        return self.records.get(task_id)

    def read_progress(self, task_id, offset, limit):
        return {"task_id": task_id, "offset": offset, "limit": limit, "events": []}


class FakeQuota:
    def __init__(self, state_dir):
        self.base = FakeBase(state_dir)
        self.counter = 0
        self.prompts = []
        self.responses = {}

    def delegate(self, task, workspace):
        self.prompts.append(task)
        self.counter += 1
        task_id = f"task-{self.counter}"
        now = time.time()
        record = SimpleNamespace(
            task_id=task_id,
            workspace=str(Path(workspace).resolve()),
            status="running",
            current_turn_index=1,
            pid=2000 + self.counter,
            log_path=str(self.base.state_dir / f"{task_id}.log"),
            conversation_id=f"cid-{self.counter}",
            session_alive=True,
            error=None,
            exit_code=None,
            created_at=now,
            updated_at=now,
            queued_instruction=None,
        )
        self.base.records[task_id] = record
        self.base._session_processes[task_id] = object()
        return {
            "task_id": task_id,
            "status": "running",
            "workspace": record.workspace,
            "pid": record.pid,
            "quota_before": {"available": True, "min_remaining_fraction": 0.8},
        }

    def result(self, task_id, max_chars=10000):
        record = self.base.records[task_id]
        return {
            "task_id": task_id,
            "status": record.status,
            "workspace": record.workspace,
            "response": self.responses.get(task_id, "done"),
            "quota_before": {"available": True, "min_remaining_fraction": 0.8},
            "quota_after": {"available": True, "min_remaining_fraction": 0.75},
            "quota_delta": {"buckets": {"gemini": {"consumed_fraction": 0.05}}},
            "quota_blocked_next_turn": False,
            "quota_blocked_reason": None,
        }

    def wait(self, task_id, timeout):
        record = self.base.records[task_id]
        return {
            "task_id": task_id,
            "status": record.status,
            "finished": record.status == "completed",
            "current_turn": record.current_turn_index,
        }

    def steer(self, task_id, instruction):
        self.prompts.append(instruction)
        return {"task_id": task_id, "status": "running", "instruction": instruction}

    def cancel(self, task_id):
        self.base.records[task_id].status = "cancelled"
        return {"task_id": task_id, "status": "cancelled", "cancelled": True}

    def get_quota_snapshot(self, workspace=None):
        return {
            "available": True,
            "blocked": False,
            "warning": False,
            "min_remaining_fraction": 0.8,
        }


class TestGitHealthAndAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "init.txt").write_text("initial\n", encoding="utf-8")
        git(self.repo, "add", "init.txt")
        git(self.repo, "commit", "-q", "-m", "init")

        self.quota = FakeQuota(self.root / "state")
        self.coordinator = AdvancedWorktreeCoordinator(self.quota)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_git_bounded_uses_temporary_files_and_succeeds(self):
        completed = run_git_bounded(["-C", str(self.repo), "status", "--porcelain=v1"])
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")

    def test_git_timeout_kills_process_tree_and_does_not_hang(self):
        with patch("git_worktrees.find_git", return_value=sys.executable):
            with patch("git_worktrees._kill_process_tree") as mock_kill:
                t0 = time.monotonic()
                with self.assertRaises(subprocess.TimeoutExpired):
                    # Run a script that sleeps longer than the probe timeout
                    run_git_bounded(
                        ["-c", "import time; time.sleep(10)"],
                        timeout=0.3,
                    )
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 2.0)
                mock_kill.assert_called_once()

    def test_windows_descendant_process_tree_termination(self):
        """Real Windows integration test: prove taskkill /F /T terminates descendants."""
        if os.name != "nt":
            self.skipTest("Windows-only process tree termination integration test")

        pid_file = self.root / "child_pid.txt"
        parent_script = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )

        with patch("git_worktrees.find_git", return_value=sys.executable):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_git_bounded(["-c", parent_script], timeout=0.8)

        self.assertTrue(pid_file.is_file(), "Child process did not write PID file")
        child_pid = int(pid_file.read_text().strip())

        # Verify child process is terminated within a bounded interval
        deadline = time.monotonic() + 3.0
        terminated = False
        while time.monotonic() < deadline:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            if str(child_pid) not in res.stdout:
                terminated = True
                break
            time.sleep(0.1)

        self.assertTrue(
            terminated,
            f"Child process {child_pid} was not terminated by taskkill /F /T process-tree cleanup",
        )

    def test_health_probe_fails_fast_on_rev_parse_timeout(self):
        calls = []

        def fake_run_git(args, *, timeout=GIT_HEALTH_TIMEOUT_SECONDS, check=True):
            calls.append(list(args))
            if "rev-parse" in args:
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("git_worktrees.run_git", side_effect=fake_run_git):
            probe = probe_git_workspace(self.repo, timeout=0.1)

        self.assertTrue(probe["timed_out"])
        self.assertFalse(probe["worktree_eligible"])
        self.assertIn("timed out", probe["error"].lower())
        # Must fail fast: status must NOT be called after rev-parse timed out
        self.assertEqual(len(calls), 1)
        self.assertIn("rev-parse", calls[0])

    def test_check_health_returns_prompt_error_on_git_timeout(self):
        def fake_probe(workspace, *, timeout=GIT_HEALTH_TIMEOUT_SECONDS, allow_dirty=False):
            return {
                "git_repository": False,
                "repo_root": None,
                "worktree_eligible": False,
                "dirty": None,
                "error": f"Git rev-parse timed out after {timeout}s",
                "timed_out": True,
            }

        with patch("advanced_coordinator.probe_git_workspace", side_effect=fake_probe):
            health = self.coordinator.check_health(str(self.repo))

        self.assertFalse(health["ready"])
        ws_check = next(c for c in health["checks"] if c["name"] == "workspace")
        self.assertEqual(ws_check["state"], "error")
        self.assertIn("timed out", ws_check["message"].lower())
        self.assertTrue(health["workspace"]["timed_out"])

    def test_health_checks_only_repo_clean_and_eligible(self):
        probe = probe_git_workspace(self.repo)
        self.assertTrue(probe["git_repository"])
        self.assertFalse(probe["dirty"])
        self.assertTrue(probe["worktree_eligible"])
        self.assertFalse(probe["timed_out"])

        # Dirty workspace
        (self.repo / "untracked.txt").write_text("hello\n", encoding="utf-8")
        probe_dirty = probe_git_workspace(self.repo)
        self.assertTrue(probe_dirty["git_repository"])
        self.assertTrue(probe_dirty["dirty"])
        self.assertFalse(probe_dirty["worktree_eligible"])

        # Dirty workspace with allow_dirty=True
        probe_allowed = probe_git_workspace(self.repo, allow_dirty=True)
        self.assertTrue(probe_allowed["worktree_eligible"])

    def test_recommend_delegation_includes_proxy_spec(self):
        rec = self.coordinator.recommend_delegation(
            "Implement feature with tests and schema migrations " * 5,
            str(self.repo),
        )
        self.assertIn("proxy_spec", rec)
        spec = rec["proxy_spec"]
        self.assertEqual(spec["model"], "gpt-5.6-luna")
        self.assertEqual(spec["reasoning_effort"], "low")
        self.assertEqual(spec["fork_turns"], "none")
        self.assertEqual(spec["fallback"], "direct_mcp")
        self.assertEqual(spec["verifier"], "parent_astra")

    def test_simulated_acceptance_flow(self):
        """Simulated acceptance scenario verifying plugin policies and coordinator mechanics:
        1. Astra evaluates Luna proxy spawn spec.
        2. Policy confirms Luna proxy or falls back to direct MCP (forbidding Sol/Terra).
        3. Gemini delegation in coordinator completes turn.
        4. Parent Astra verifies implementation directly (no second verifier worker).
        5. Windows health check returns promptly.
        (Note: Live end-to-end execution with Codex Desktop subagent spawning is tested manually).
        """
        # 1 & 2 & 3: Route decision
        spawn_spec = proxy_policy.get_proxy_spec()
        self.assertEqual(spawn_spec["model"], "gpt-5.6-luna")
        self.assertEqual(spawn_spec["reasoning_effort"], "low")
        self.assertEqual(spawn_spec["fork_turns"], "none")

        # Verify Sol/Terra/Astra cannot be used as proxy
        for forbidden in ("gpt-5-sol", "gpt-5-terra", "gpt-5-astra"):
            route, _ = proxy_policy.resolve_delegation_route(
                luna_available=True,
                child_model=forbidden,
                spawn_args=spawn_spec,
                runtime_metadata={"model": "gpt-5.6-luna", "effort": "low"},
            )
            self.assertEqual(route, "direct_mcp")

        # Confirmed Luna proxy with Antigravity MCP tools and verified attestation
        route, _ = proxy_policy.resolve_delegation_route(
            luna_available=True,
            child_model="gpt-5.6-luna",
            tools_available=True,
            spawn_args=spawn_spec,
            runtime_metadata={"model": "gpt-5.6-luna", "effort": "low"},
        )
        self.assertEqual(route, "luna_proxy")

        # 4: Gemini delegation & completion
        delegated = self.coordinator.delegate(
            "Implement the widget parser",
            str(self.repo),
            isolation="shared",
        )
        task_id = delegated["task_id"]

        # Gemini writes files and completes handoff
        (self.repo / "widget.py").write_text("class WidgetParser:\n    pass\n", encoding="utf-8")
        self.quota.responses[task_id] = (
            "Done.\n"
            f"{HANDOFF_MARKER}\n"
            + json.dumps({
                "summary": "Implemented WidgetParser",
                "files_changed": ["widget.py"],
                "tests_run": ["python -m unittest tests/test_widget.py"],
                "tests_passed": True,
                "remaining_work": [],
                "risks": [],
                "commit_sha": None,
                "worktree": str(self.repo),
                "quota_impact": {},
            })
        )
        self.quota.base.records[task_id].status = "completed"

        res = self.coordinator.result(task_id)
        impl = res["implementation"]
        self.assertTrue(impl["structured_report_valid"])
        self.assertEqual(impl["files_changed"], ["widget.py"])

        # 5: Parent Astra verifies directly (no second worker)
        verification_policy = proxy_policy.get_verification_policy()
        self.assertEqual(verification_policy["verifier"], "parent_astra")
        self.assertFalse(verification_policy["mandatory_worker"])

        # Parent inspects actual file changes
        self.assertTrue((self.repo / "widget.py").is_file())
        verdict = "accepted"
        self.assertIn(verdict, verification_policy["verdicts"])

        # 6: Windows health check returns promptly
        with patch("quota_server.server.find_agy", return_value="agy"), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="agy 1.2.0 --input-format", stderr="")):
            t0 = time.monotonic()
            health = self.coordinator.check_health(str(self.repo))
            health_duration = time.monotonic() - t0
            self.assertTrue(health["ready"])
            self.assertLess(health_duration, 5.0)
            self.assertTrue(health["workspace"]["dirty"])
            self.assertFalse(health["workspace"]["worktree_eligible"])


if __name__ == "__main__":
    unittest.main()
