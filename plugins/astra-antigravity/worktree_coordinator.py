import os
import time
from pathlib import Path

import quota_server
from compatibility import call_supported
from task_types import validate_task_type
from git_worktrees import (
    GitWorktreeStore,
    ISOLATION_SHARED,
    ISOLATION_WORKTREE,
    SUPPORTED_ISOLATION_MODES,
)


WINDOWS_WORKTREE_RETRY_DELAYS = (0.10, 0.25, 0.50, 1.00)
WINDOWS_LOCK_ERROR_MARKERS = (
    "permission denied",
    "access is denied",
    "being used by another process",
    "used by another process",
    "sharing violation",
)


def _is_windows() -> bool:
    return os.name == "nt"


def _is_transient_windows_worktree_lock(message: str | None) -> bool:
    if not message:
        return False
    lowered = str(message).lower()
    return any(marker in lowered for marker in WINDOWS_LOCK_ERROR_MARKERS)


class WorktreeCoordinator:
    def __init__(self, quota_coordinator=None, store=None):
        self.quota = quota_coordinator or quota_server.coordinator
        self.base = self.quota.base
        self.store = store or GitWorktreeStore(self.base.state_dir)

    def _metadata(self, task_id: str, refresh: bool) -> dict:
        record = self.base.get_task(task_id)
        fallback = record.workspace if record else None
        if refresh:
            return self.store.refresh(task_id, fallback)
        metadata = self.store.load(task_id)
        if metadata:
            return dict(metadata)
        if fallback:
            return self.store.shared(task_id, fallback, fallback)
        raise ValueError(f"Task '{task_id}' not found.")

    def _attach(self, task_id: str, result: dict, *, refresh: bool = True) -> dict:
        metadata = self._metadata(task_id, refresh)
        return {
            **result,
            "isolation": metadata.get("isolation", ISOLATION_SHARED),
            "source_workspace": metadata.get("source_workspace"),
            "effective_workspace": metadata.get("effective_workspace"),
            "worktree": metadata if metadata.get("isolation") == ISOLATION_WORKTREE else None,
        }

    def delegate(
        self,
        task: str,
        workspace: str,
        isolation: str = ISOLATION_SHARED,
        quota_override: bool = False,
        model: str | None = None,
        task_type: str = "implementation",
        required_capabilities: list[str] | None = None,
    ) -> dict:
        validate_task_type(task_type)
        mode = (isolation or ISOLATION_SHARED).strip().lower()
        if mode not in SUPPORTED_ISOLATION_MODES:
            raise ValueError(f"Unsupported isolation mode '{isolation}'. Use 'shared' or 'worktree'.")
        if mode == ISOLATION_SHARED:
            result = call_supported(self.quota.delegate, task, workspace,
                                    quota_override=quota_override, model=model,
                                    task_type=task_type, required_capabilities=required_capabilities)
            task_id = result.get("task_id")
            if not task_id:
                return {**result, "isolation": ISOLATION_SHARED}
            effective = result.get("workspace") or workspace
            self.store.shared(task_id, workspace, effective)
            return self._attach(task_id, result, refresh=False)

        metadata = self.store.create(workspace)
        try:
            result = call_supported(self.quota.delegate, task, metadata["worktree_workspace"],
                                    quota_override=quota_override, model=model,
                                    task_type=task_type, required_capabilities=required_capabilities)
        except Exception:
            self.store.discard(metadata)
            raise
        task_id = result.get("task_id")
        if not task_id:
            self.store.discard(metadata)
            return {
                **result,
                "isolation": ISOLATION_WORKTREE,
                "source_workspace": metadata["source_workspace"],
                "effective_workspace": None,
                "worktree": None,
            }
        self.store.register(task_id, metadata)
        return self._attach(task_id, result)

    def wait(self, task_id: str, timeout_seconds: float = 30.0) -> dict:
        result = self.quota.wait(task_id, timeout_seconds)
        return self._attach(task_id, result, refresh=bool(result.get("finished")))

    def status(self, task_id: str) -> dict:
        return self._attach(task_id, self.quota.status(task_id))

    def progress(self, task_id: str, offset: int, limit: int) -> dict:
        result = self.quota.base.read_progress(task_id, offset, limit)
        return self._attach(task_id, result, refresh=False)

    def result(self, task_id: str, max_chars: int = quota_server.server.DEFAULT_MAX_RESULT_CHARS) -> dict:
        return self._attach(task_id, self.quota.result(task_id, max_chars))

    def steer(self, task_id: str, instruction: str, quota_override: bool = False) -> dict:
        try:
            res = self.quota.steer(task_id, instruction, quota_override=quota_override)
        except TypeError:
            res = self.quota.steer(task_id, instruction)
        return self._attach(task_id, res, refresh=False)

    def cancel(self, task_id: str) -> dict:
        return self._attach(task_id, self.quota.cancel(task_id))

    def _close_completed_session(self, task_id: str, timeout_seconds: float = 5.0) -> bool:
        """Release a completed task's hot Antigravity process without changing its result."""
        record = self.base.get_task(task_id)
        if not record or record.status != "completed" or not getattr(record, "session_alive", False):
            return True

        # Route through the quota coordinator so any queued quota-aware follow-up
        # is discarded before the persistent session closes. Production
        # QuotaCoordinator.cancel delegates to base.cancel_task, which preserves
        # completed task/result state while closing the hot session.
        self.quota.cancel(task_id)

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            record = self.base.get_task(task_id)
            if not record or not getattr(record, "session_alive", False):
                return True
            time.sleep(0.05)
        record = self.base.get_task(task_id)
        return not record or not getattr(record, "session_alive", False)

    def _remove_worktree_with_windows_retry(
        self,
        task_id: str,
        *,
        force: bool,
        delete_branch: bool,
    ) -> dict:
        result = self.store.remove(task_id, force=force, delete_branch=delete_branch)
        if result.get("cleaned") or not _is_windows():
            return result

        error = result.get("remove_error")
        if not _is_transient_windows_worktree_lock(error):
            return result

        for delay in WINDOWS_WORKTREE_RETRY_DELAYS:
            time.sleep(delay)
            result = self.store.remove(task_id, force=force, delete_branch=delete_branch)
            if result.get("cleaned"):
                return result
            if not _is_transient_windows_worktree_lock(result.get("remove_error")):
                return result
        return result

    def cleanup(self, task_id: str, force: bool = False, delete_branch: bool = False) -> dict:
        record = self.base.get_task(task_id)
        if not record:
            raise ValueError(f"Task '{task_id}' not found.")
        metadata = self._metadata(task_id, refresh=True)
        if metadata.get("isolation") != ISOLATION_WORKTREE:
            return {
                "task_id": task_id,
                "cleaned": False,
                "isolation": ISOLATION_SHARED,
                "message": "Task does not own an isolated worktree.",
            }
        if record.status in ("started", "running", "unknown"):
            raise ValueError("Cannot clean up a worktree while its Gemini turn is active.")
        if metadata.get("dirty") and not force:
            return {
                "task_id": task_id,
                "cleaned": False,
                "isolation": ISOLATION_WORKTREE,
                "message": "Worktree has uncommitted changes. Review/integrate them first, or use force=True to discard them.",
                "worktree": metadata,
            }
        if not self._close_completed_session(task_id):
            return {
                "task_id": task_id,
                "cleaned": False,
                "isolation": ISOLATION_WORKTREE,
                "message": "Antigravity session is still using the worktree; cleanup was not attempted.",
                "worktree": metadata,
            }
        result = self._remove_worktree_with_windows_retry(
            task_id,
            force=force,
            delete_branch=delete_branch,
        )
        return {**result, "isolation": ISOLATION_WORKTREE}
