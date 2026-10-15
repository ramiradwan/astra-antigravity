"""Credential-free FastMCP fixture for the await_task wire contract."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import worktree_server


class FakeBase:
    def __init__(self):
        self.calls = 0

    def wait_for_update(self, task_id, after_sequence, timeout_seconds):
        self.calls += 1
        time.sleep(0.03)
        if self.calls == 1:
            return {
                "task_id": task_id,
                "status": "running",
                "terminal": False,
                "timed_out": False,
                "sequence": 1,
                "activity": "Inspecting repository",
                "tests": {"last_command": None, "state": None, "passed": None},
            }
        if self.calls == 2:
            return {
                "task_id": task_id,
                "status": "running",
                "terminal": False,
                "timed_out": False,
                "sequence": 2,
                "activity": "Running pytest -q",
                "tests": {"last_command": "pytest -q", "state": "running", "passed": None},
            }
        return {
            "task_id": task_id,
            "status": "completed",
            "terminal": True,
            "timed_out": False,
            "sequence": 2,
            "activity": "Running pytest -q",
            "tests": {"last_command": "pytest -q", "state": "passed", "passed": True},
        }


class FakeCoordinator:
    def __init__(self):
        self.base = FakeBase()

    def wait(self, task_id, timeout_seconds):
        return {"task_id": task_id, "status": "completed", "finished": True}

    def compact_result(self, task_id):
        return {
            "task_id": task_id,
            "status": "completed",
            "current_turn": 1,
            "implementation": {
                "summary": "wire contract complete",
                "files_changed": ["src/example.py"],
                "tests_run": ["pytest -q"],
                "tests_passed": True,
                "remaining_work": [],
                "risks": [],
                "commit_sha": None,
                "worktree": {"path": "/tmp/fake"},
                "structured_report_valid": True,
            },
            "quota": {"available": True, "blocked": False},
        }


worktree_server.coordinator = FakeCoordinator()

if __name__ == "__main__":
    worktree_server.mcp.run()
