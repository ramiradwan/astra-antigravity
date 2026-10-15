"""Focused compatibility layer over the proven advanced coordinator.

The legacy coordinator remains in ``advanced_coordinator_legacy`` with model-pin forwarding.
This module fixes result parsing order, exposes structured live progress from the
new task manager, and provides a compact terminal result for normal orchestration.
"""

from __future__ import annotations

import advanced_coordinator_legacy as legacy
import quota_server
from advanced_coordinator_legacy import *  # re-export public compatibility symbols
from git_worktrees import ISOLATION_SHARED, ISOLATION_WORKTREE, probe_git_workspace
from worktree_coordinator import WorktreeCoordinator
from semantic_report import validate_semantic_report
from task_types import CONTRACTS, inspect_artifacts, result_key, result_schema, saved_task_type, task_instructions

NATIVE_RESULT_INSTRUCTIONS = CONTRACTS["implementation"]["instructions"]


def _legacy_probe_git_workspace_proxy(*args, **kwargs):
    """Preserve the historical advanced_coordinator probe patch seam."""
    return probe_git_workspace(*args, **kwargs)


def _native_result_prompt(prompt, task_type="implementation", required_capabilities=None):
    prompt = str(prompt or "").strip()
    instructions = task_instructions(task_type, required_capabilities)
    if not prompt:
        return instructions
    return f"{prompt}\n\n{instructions}"


legacy.probe_git_workspace = _legacy_probe_git_workspace_proxy
# Inherited delegate/steer methods resolve this helper in the legacy module.
# Replace only the prompt contract; keep the legacy parser for old persisted tasks.
legacy._with_contract = _native_result_prompt


class AdvancedWorktreeCoordinator(legacy.AdvancedWorktreeCoordinator):
    def progress_summary(self, task_id):
        if hasattr(self.base, "progress_state"):
            state = self.base.progress_state(task_id)
            status = state.get("status")
            phase = (
                "complete"
                if status == "completed"
                else status
                if status in ("failed", "cancelled", "unknown")
                else "working"
            )
            tests = state.get("tests") or {}
            return {
                "task_id": task_id,
                "status": status,
                "current_turn": state.get("current_turn"),
                "phase": phase,
                "activity": state.get("activity"),
                "current_file": state.get("current_file"),
                "tests": {
                    "last_command": tests.get("last_command"),
                    "state": tests.get("state"),
                    "passed": tests.get("passed"),
                },
                "last_change": state.get("last_change"),
                "sequence": state.get("sequence", 0),
                "updated_at": state.get("updated_at"),
            }
        return super().progress_summary(task_id)

    def result(
        self,
        task_id,
        max_chars=quota_server.server.DEFAULT_MAX_RESULT_CHARS,
    ):
        record = self.base.get_task(task_id)
        task_type = saved_task_type(record)
        full_response = getattr(record, "latest_response", "") if record else ""
        raw = WorktreeCoordinator.result(self, task_id, max_chars)

        native_report = None
        if hasattr(self.base, "structured_output"):
            native_report = self.base.structured_output(task_id)
        if isinstance(native_report, dict):
            report, parse_error = validate_semantic_report(dict(native_report), task_type)
        elif task_type == "implementation":
            # Compatibility only for tasks persisted before native --json-schema
            # output was introduced. New Antigravity runs should never depend on
            # response text or the historical ASTRA_IMPLEMENTATION_RESULT marker.
            report, parse_error = legacy._parse_handoff(
                full_response or raw.get("response") or ""
            )
            report = report or {}
        else:
            report, parse_error = validate_semantic_report(None, task_type)

        meta = self._metadata(task_id, True)
        if meta.get("isolation") == ISOLATION_SHARED and meta.get(
            "effective_workspace"
        ):
            snap = self._git_snapshot(
                meta["effective_workspace"], meta.get("base_commit")
            )
            meta.update(
                {
                    "source_repo_root": snap["repo_root"],
                    "head_commit": snap["head_commit"],
                    "branch": snap["branch"],
                    "changed_files": snap["changed_files"],
                    "diff_stat": snap["diff_stat"],
                    "commits_ahead": snap["commits_ahead"],
                    "dirty": snap["dirty"],
                }
            )
            self.store.save(task_id, meta)
            raw = self._attach(task_id, raw, refresh=False)

        tests_passed = (
            report.get("tests_passed")
            if type(report.get("tests_passed")) is bool or report.get("tests_passed") is None
            else None
        )
        risks = legacy._list(report.get("risks"))
        if parse_error:
            risks.append(parse_error)
        if meta.get("isolation") == ISOLATION_SHARED and meta.get("source_dirty"):
            risks.append(
                "Shared workspace was already dirty at delegation time; "
                "Git-derived changes may include pre-existing edits."
            )

        ahead = meta.get("commits_ahead")
        commit_sha = (
            meta.get("head_commit")
            if isinstance(ahead, int) and ahead > 0
            else None
        )
        worktree = {
            "workspace": meta.get("effective_workspace"),
            "branch": meta.get("branch"),
            "base_commit": meta.get("base_commit"),
            "head_commit": meta.get("head_commit"),
            "dirty": meta.get("dirty"),
            "diff_stat": meta.get("diff_stat"),
            "commits_ahead": ahead,
        }
        if meta.get("isolation") == ISOLATION_WORKTREE:
            worktree["path"] = meta.get("worktree_root")
        else:
            worktree.update(
                path=meta.get("source_repo_root") or meta.get("effective_workspace"), isolation=ISOLATION_SHARED
            )

        # A missing semantic handoff must never promote transcript prose into
        # the root's report, even when the process completed successfully.
        summary = str(report.get("summary") or "").strip() or f"No structured {task_type} summary was available."
        raw["task_type"] = task_type
        if task_type != "implementation":
            artifacts, issues = inspect_artifacts(report.get("artifacts") or [], meta.get("effective_workspace"))
            if parse_error:
                issues.append(parse_error)
            if report.get("checks_passed") is False:
                issues.append("The worker reports that one or more checks failed.")
            raw["result"] = {
                **report, "summary": summary, "risks": risks,
                "structured_report_valid": parse_error is None,
                "artifact_evidence": artifacts,
                "files_changed": legacy._list(meta.get("changed_files")),
                "worktree": worktree,
            }
            raw["verification"] = {
                "required": True, "structural_checks_passed": not issues,
                "focus": CONTRACTS[task_type]["verification_focus"], "issues": issues,
            }
            return raw
        raw["implementation"] = {
            "summary": summary,
            "files_changed": legacy._list(meta.get("changed_files")),
            "tests_run": legacy._list(report.get("tests_run")),
            "tests_passed": tests_passed,
            "remaining_work": legacy._list(report.get("remaining_work")),
            "risks": risks,
            "commit_sha": commit_sha,
            "worktree": worktree,
            "quota_impact": {
                "before": raw.get("quota_before"),
                "after": raw.get("quota_after"),
                "delta": raw.get("quota_delta"),
                "blocked_next_turn": raw.get("quota_blocked_next_turn", False),
                "blocked_reason": raw.get("quota_blocked_reason"),
                "quota_override_used": raw.get("quota_override_used", False),
            },
            "structured_report_valid": parse_error is None,
            "reported": report or None,
        }
        return raw

    def compact_result(self, task_id):
        """Return the normal orchestration handoff without diagnostic bulk."""
        raw = self.result(task_id, max_chars=1000)
        task_type = raw["task_type"]
        key = result_key(task_type)
        implementation = raw.get(key) or {}
        fields = (
            "summary", "files_changed", "tests_run", "tests_passed",
            "remaining_work", "risks", "commit_sha", "worktree", "structured_report_valid",
        ) if task_type == "implementation" else tuple(result_schema(task_type)["properties"]) + (
            "artifact_evidence", "files_changed", "worktree", "structured_report_valid",
        )
        compact_implementation = {
            key: implementation.get(key)
            for key in fields
        }

        quota_source = raw.get("quota_after") or raw.get("quota_before") or {}
        quota = {
            key: quota_source.get(key)
            for key in (
                "available",
                "min_remaining_fraction",
                "warning",
                "blocked",
                "limiting_bucket",
            )
            if key in quota_source
        }
        if raw.get("quota_blocked_next_turn"):
            quota["blocked"] = True
            quota["blocked_reason"] = raw.get("quota_blocked_reason")

        result = {
            "task_id": raw.get("task_id", task_id),
            "status": raw.get("status"),
            "current_turn": raw.get("num_turns") or raw.get("current_turn"),
            "task_type": task_type,
            key: compact_implementation,
            "quota": quota,
        }
        if "verification" in raw:
            result["verification"] = raw["verification"]
        if raw.get("error"):
            result["error"] = raw["error"]
        return result
