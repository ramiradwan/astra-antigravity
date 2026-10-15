"""Slow credential-free MCP server for Codex no-resampling compatibility tests."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import worktree_server


def _marker(env_name: str, filename: str) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured)
    # Codex intentionally sanitizes the environment inherited by stdio MCP
    # servers. The harness owns exactly one uniquely prefixed temporary
    # directory, so discover it when explicit marker variables are unavailable.
    candidates = sorted(
        Path(tempfile.gettempdir()).glob("astra-codex-compat-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("Codex compatibility temp directory was not found")
    return candidates[0] / filename


STARTED = _marker("ASTRA_TEST_AWAIT_STARTED_FILE", "await-started")
COMPLETED = _marker("ASTRA_TEST_AWAIT_COMPLETED_FILE", "await-completed")
STEP_DELAY = float(os.environ.get("ASTRA_TEST_AWAIT_STEP_DELAY", "0.7"))


class SlowFakeBase:
    def __init__(self):
        self.calls = 0

    def wait_for_update(self, task_id, after_sequence, timeout_seconds):
        self.calls += 1
        if self.calls == 1:
            STARTED.write_text("started\n", encoding="utf-8")
            time.sleep(STEP_DELAY)
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
            time.sleep(STEP_DELAY)
            return {
                "task_id": task_id,
                "status": "running",
                "terminal": False,
                "timed_out": False,
                "sequence": 2,
                "activity": "Running pytest -q",
                "tests": {"last_command": "pytest -q", "state": "running", "passed": None},
            }

        time.sleep(STEP_DELAY)
        COMPLETED.write_text("completed\n", encoding="utf-8")
        return {
            "task_id": task_id,
            "status": "completed",
            "terminal": True,
            "timed_out": False,
            "sequence": 2,
            "activity": "Running pytest -q",
            "tests": {"last_command": "pytest -q", "state": "passed", "passed": True},
        }


class SlowFakeCoordinator:
    def __init__(self):
        self.base = SlowFakeBase()

    def wait(self, task_id, timeout_seconds):
        return {"task_id": task_id, "status": "completed", "finished": True}

    def compact_result(self, task_id):
        return {
            "task_id": task_id,
            "status": "completed",
            "current_turn": 1,
            "implementation": {
                "summary": "codex compatibility await complete",
                "files_changed": [],
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


worktree_server.coordinator = SlowFakeCoordinator()

if __name__ == "__main__":
    worktree_server.mcp.run()
