import asyncio

from mcp.server.fastmcp import Context, FastMCP

import quota_server
from git_worktrees import ISOLATION_SHARED
from advanced_coordinator import AdvancedWorktreeCoordinator

mcp = FastMCP("Antigravity")
coordinator = AdvancedWorktreeCoordinator()


@mcp.tool()
def check_antigravity_health(workspace: str | None = None) -> dict:
    """Run one cached Antigravity/Git/quota/workspace preflight."""
    return coordinator.check_health(workspace)


@mcp.tool()
def recommend_delegation(task: str, workspace: str) -> dict:
    """Return advisory delegate/keep guidance plus suggested isolation."""
    return coordinator.recommend_delegation(task, workspace)


@mcp.tool()
def get_gemini_quota(workspace: str | None = None) -> dict:
    """Refresh Gemini quota without starting an agent turn."""
    return coordinator.quota.get_quota_snapshot(workspace)


@mcp.tool()
def delegate_to_gemini(
    task: str,
    workspace: str,
    isolation: str = ISOLATION_SHARED,
    quota_override: bool = False,
) -> dict:
    """Start Gemini in the shared checkout or an isolated Git worktree.

    Set quota_override=True only when explicitly authorized by the user to proceed
    below the minimum quota start floor without mutating environment variables.
    """
    return coordinator.delegate(
        task, workspace, isolation, quota_override=quota_override
    )


@mcp.tool()
def wait_for_task(task_id: str, timeout_seconds: float = 30.0) -> dict:
    """Diagnostic bounded wait for compatibility; normal orchestration uses await_task."""
    return coordinator.wait(task_id, timeout_seconds)


async def _await_task(
    task_id: str,
    ctx: Context,
    *,
    coordinator_obj=None,
    wait_slice_seconds: float = 30.0,
) -> dict:
    """Keep one MCP call in flight and stream condition-driven task progress."""
    coord = coordinator_obj or coordinator
    sequence = 0

    while True:
        update = await asyncio.to_thread(
            coord.base.wait_for_update,
            task_id,
            sequence,
            wait_slice_seconds,
        )

        new_sequence = int(update.get("sequence") or 0)
        if new_sequence > sequence:
            sequence = new_sequence
            await ctx.report_progress(
                progress=sequence,
                message=update.get("activity") or "Gemini task progressed",
            )

        if not update.get("terminal"):
            continue

        settled = await asyncio.to_thread(coord.wait, task_id, 0.1)
        if settled.get("finished"):
            if hasattr(coord, "compact_result"):
                return await asyncio.to_thread(coord.compact_result, task_id)
            return await asyncio.to_thread(coord.result, task_id)


@mcp.tool()
async def await_task(task_id: str, ctx: Context) -> dict:
    """Await Gemini in one MCP call while streaming routine progress notifications."""
    return await _await_task(task_id, ctx)


@mcp.tool()
def get_task_status(task_id: str) -> dict:
    """Inspect task, quota, session, worktree, and compact progress state."""
    return coordinator.status(task_id)


@mcp.tool()
def get_task_progress_summary(task_id: str) -> dict:
    """Return compact progress for diagnostics; normal orchestration uses await_task."""
    base = coordinator.base
    if hasattr(base, "progress_state"):
        return base.progress_state(task_id)
    return coordinator.progress_summary(task_id)


@mcp.tool()
def read_task_progress(
    task_id: str,
    offset: int = 0,
    limit: int = quota_server.server.DEFAULT_PROGRESS_LIMIT,
) -> dict:
    """Read bounded raw Antigravity NDJSON progress events for debugging."""
    return coordinator.progress(task_id, offset, limit)


@mcp.tool()
def get_task_result(
    task_id: str,
    max_chars: int = quota_server.server.DEFAULT_MAX_RESULT_CHARS,
) -> dict:
    """Return normalized implementation, quota metrics, and authoritative Git metadata."""
    return coordinator.result(task_id, max_chars)


@mcp.tool()
def steer_gemini(task_id: str, instruction: str, quota_override: bool = False) -> dict:
    """Quota-preflight and follow up in the same logical task/worktree."""
    return coordinator.steer(task_id, instruction, quota_override=quota_override)


@mcp.tool()
def follow_up_gemini(task_id: str, instruction: str, quota_override: bool = False) -> dict:
    """Alias for steer_gemini."""
    return coordinator.steer(task_id, instruction, quota_override=quota_override)


@mcp.tool()
def cancel_task(task_id: str) -> dict:
    """Cancel the running Gemini turn without deleting its worktree."""
    return coordinator.cancel(task_id)


@mcp.tool()
def cleanup_task_worktree(
    task_id: str, force: bool = False, delete_branch: bool = False
) -> dict:
    """Remove an isolated worktree after review/integration.

    Dirty worktrees require force=True. Branch deletion is separate and opt-in.
    """
    return coordinator.cleanup(task_id, force=force, delete_branch=delete_branch)


if __name__ == "__main__":
    mcp.run()
