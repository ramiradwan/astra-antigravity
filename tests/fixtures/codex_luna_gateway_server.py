"""Credential-free slow root Luna gateway fixture for Codex context-firewall tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("Astra Luna Gateway Compatibility")


def _marker(env_name: str, filename: str) -> Path:
    configured = os.environ.get(env_name)
    if configured:
        return Path(configured)
    candidates = sorted(
        Path(tempfile.gettempdir()).glob("astra-root-firewall-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("Root context-firewall temp directory was not found")
    return candidates[0] / filename


STARTED = _marker("ASTRA_TEST_GATEWAY_STARTED_FILE", "gateway-started")
COMPLETED = _marker("ASTRA_TEST_GATEWAY_COMPLETED_FILE", "gateway-completed")
STEP_DELAY = float(os.environ.get("ASTRA_TEST_GATEWAY_STEP_DELAY", "0.7"))


@mcp.tool()
async def delegate_via_luna(
    task: str,
    workspace: str,
    ctx: Context,
    isolation: str = "worktree",
    quota_override: bool = False,
) -> dict:
    STARTED.write_text("started\n", encoding="utf-8")
    await asyncio.sleep(STEP_DELAY)
    await ctx.report_progress(progress=1, message="Inspecting repository")
    await asyncio.sleep(STEP_DELAY)
    await ctx.report_progress(progress=2, message="Running pytest -q")
    await asyncio.sleep(STEP_DELAY)
    COMPLETED.write_text("completed\n", encoding="utf-8")
    return {
        "task_id": "task-root-firewall",
        "status": "completed",
        "current_turn": 1,
        "implementation": {
            "summary": "gateway fixture complete",
            "files_changed": ["src/example.py"],
            "tests_run": ["pytest -q"],
            "tests_passed": True,
            "remaining_work": [],
            "risks": [],
            "commit_sha": None,
            "worktree": {"path": "/tmp/fake-worktree"},
            "structured_report_valid": True,
        },
        "quota": {"available": True, "blocked": False},
        "luna": {
            "model": "gpt-6-luna",
            "reasoning_effort": "low",
            "phase": "delegation",
            "coordination": {
                "coordination_brief": "Keep the implementation scoped to the requested feature.",
                "acceptance_focus": ["Run pytest -q", "Inspect the final diff"],
                "risks": ["Preserve compatibility"],
            },
        },
        "planner": {
            "model": "gpt-6-luna",
            "reasoning_effort": "low",
            "phase": "delegation",
            "coordination": {
                "coordination_brief": "Keep the implementation scoped to the requested feature.",
                "acceptance_focus": ["Run pytest -q", "Inspect the final diff"],
                "risks": ["Preserve compatibility"],
            },
        },
        "antigravity": {
            "requested_model": "gemini-4-argon-high",
            "requested_effort": "high",
        },
    }


@mcp.tool()
async def continue_via_luna(task_id: str, findings: str, ctx: Context) -> dict:
    return {
        "task_id": task_id,
        "status": "completed",
        "luna": {
            "model": "gpt-6-luna",
            "reasoning_effort": "low",
            "phase": "continuation",
            "coordination": {
                "coordination_brief": findings[:80],
                "acceptance_focus": [],
                "risks": [],
            },
        },
        "planner": {
            "model": "gpt-6-luna",
            "reasoning_effort": "low",
            "phase": "continuation",
            "coordination": {
                "coordination_brief": findings[:80],
                "acceptance_focus": [],
                "risks": [],
            },
        },
        "antigravity": {
            "requested_model": "gemini-4-argon-high",
            "requested_effort": "high",
        },
    }


@mcp.tool()
def cancel_delegation(task_id: str) -> dict:
    return {"task_id": task_id, "status": "cancelled"}


@mcp.tool()
def cleanup_task_worktree(task_id: str) -> dict:
    return {"task_id": task_id, "cleaned": True}


@mcp.tool()
def check_integration(workspace: str) -> dict:
    return {"ready": True, "workspace": workspace}


@mcp.tool()
def inspect_delegation(workspace: str, task_id: str | None = None) -> dict:
    return {"workspace": workspace, "tasks": []}


if __name__ == "__main__":
    mcp.run()
