"""Read-only, repository-scoped inspection of persisted delegation state."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from communication import bound_report
from git_worktrees import run_git_bounded
from server import LEGACY_DEFAULT_MODEL, LEGACY_DEFAULT_EFFORT, SERVER_INSTANCE_ID
from semantic_report import validate_semantic_report
from task_types import CONTRACTS, result_key, result_schema, validate_task_type


def repository_identity(path):
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        return None
    try:
        result = run_git_bounded(
            ["-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            timeout=5, check=False)
    except (RuntimeError, OSError):
        return None
    if result.returncode:
        return None
    return os.path.normcase(str(Path(result.stdout.strip()).resolve()))


def inspect_saved_tasks(coordinator, workspace, task_id=None):
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Workspace does not exist: {workspace}")
    if task_id and not re.fullmatch(r"task-[A-Za-z0-9-]+", task_id):
        raise ValueError("Invalid task ID")
    identity = repository_identity(workspace)
    identities = {}

    def belongs(path):
        if not isinstance(path, str):
            return False
        resolved = Path(path).expanduser().resolve()
        if os.path.normcase(str(resolved)) == os.path.normcase(str(workspace)):
            return True
        if identity is None:
            return False
        key = str(resolved)
        if key not in identities:
            identities[key] = repository_identity(resolved)
        return identities[key] == identity

    state = Path(coordinator.base.state_dir)
    rows = []
    files = [state / (task_id + ".json")] if task_id else state.glob("task-*.json")
    for file in files:
        # Worktree/quota sidecars are not TaskRecords.
        if not re.fullmatch(r"task-[A-Za-z0-9-]+\.json", file.name):
            continue
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
            tid = record["task_id"]
            if not re.fullmatch(r"task-[A-Za-z0-9-]+", tid):
                continue
            sidecar = state / (tid + ".worktree.json")
            wt = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
        except (ValueError, OSError, KeyError, TypeError):
            continue
        paths = (record.get("workspace"), wt.get("source_repo_root"),
                 wt.get("source_workspace"), wt.get("effective_workspace"))
        if not any(belongs(path) for path in paths):
            continue
        status = record.get("status")
        if status in ("started", "running"):
            proc = coordinator.base._session_processes.get(tid)
            if record.get("server_instance_id") != SERVER_INSTANCE_ID or not proc or proc.poll() is not None:
                status = "unknown"
        current = record.get("current_turn_index", 1)
        turn = next((item for item in reversed(record.get("turns") or [])
                     if item.get("turn_index") == current), None)
        report = (turn or {}).get("structured_output")
        task_type = validate_task_type(record.get("task_type", "implementation"))
        # Never use response prose as a summary or substitute an older turn.
        report, report_error = validate_semantic_report(report, task_type)
        summary = report.get("summary") if isinstance(report.get("summary"), str) else ""
        item = {
            "task_id": tid, "status": status, "current_turn": current,
            "task_type": task_type,
            "model": record.get("model") or LEGACY_DEFAULT_MODEL,
            "effort": record.get("effort") or LEGACY_DEFAULT_EFFORT,
            "summary": summary[:160], "tests_passed": report.get("tests_passed"),
            "updated_at": record.get("updated_at", 0),
            "correction_supported": status == "completed",
        }
        if task_id:
            key = result_key(task_type)
            item[key] = {name: report.get(name) for name in result_schema(task_type)["properties"]}
            item[key]["structured_report_valid"] = report_error is None
            item[key]["files_changed"] = wt.get("changed_files") or []
            item[key]["worktree"] = {
                "path": wt.get("worktree_root") or record.get("workspace"),
                "branch": wt.get("branch"), "base_commit": wt.get("base_commit"),
                "head_commit": wt.get("head_commit"), "dirty": wt.get("dirty")}
            if task_type != "implementation":
                from task_types import inspect_artifacts
                evidence, issues = inspect_artifacts(report.get("artifacts") or [], record["workspace"])
                item[key]["artifact_evidence"] = evidence
                if report_error:
                    issues.append(report_error)
                if report.get("checks_passed") is False:
                    issues.append("The worker reports that one or more checks failed.")
                item["verification"] = {"required": True, "structural_checks_passed": not issues,
                                        "focus": CONTRACTS[task_type]["verification_focus"], "issues": issues}
        if task_type != "implementation":
            item.pop("tests_passed", None)
            item["checks_passed"] = report.get("checks_passed")
        if record.get("error"):
            item["error"] = str(record["error"])[:200]
        rows.append(item)
    if task_id and not rows:
        raise ValueError(f"Task '{task_id}' not found in workspace '{workspace}'")
    rows.sort(key=lambda row: (row["updated_at"], row["task_id"]), reverse=True)
    result = {"workspace": str(workspace), "total_matching": len(rows),
              "tasks": rows[:20]}
    if len(rows) > 20:
        result["tasks_truncated_count"] = len(rows) - 20
    # Aggregate list responses need a bound too; retain the newest rows.
    from communication import json_size
    while len(result["tasks"]) > 1 and json_size(result) > 7800:
        result["tasks"].pop()
        result["tasks_truncated_count"] = len(rows) - len(result["tasks"])
    return bound_report(result)
