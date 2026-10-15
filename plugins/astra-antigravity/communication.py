"""Bounded state notifications and semantic reports for the root MCP client."""

from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import re
import time
from pathlib import Path

from task_progress import FILE_TOOL_NAMES

MAX_REPORT_BUDGET = 8000
MAX_PROGRESS_MESSAGE_CHARS = 160
ORDINARY_PROGRESS_INTERVAL_SECONDS = 2.0
MAX_ORDINARY_PROGRESS_EVENTS = 30
MAX_PRIORITY_PROGRESS_EVENTS = 12


def json_size(value) -> int:
    # Count escaped wire characters too, including Unicode and both aliases.
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=True))


def bound_report(value: dict, max_budget: int = MAX_REPORT_BUDGET) -> dict:
    """Preserve keys and evidence while explicitly accounting for clipped data."""
    result = json.loads(json.dumps(value))
    clipping: dict[str, int] = dict(result.pop("truncation", {}))

    def count(path, amount):
        # Aggregate per collection; a counter for every long list item would
        # itself bloat the handoff.
        path = re.sub(r"\[\d+\]", "", path)
        clipping[path] = clipping.get(path, 0) + amount

    def visit(item, path="", limit=400):
        if isinstance(item, dict):
            return {key: visit(val, f"{path}.{key}".strip("."), limit)
                    for key, val in item.items()}
        if isinstance(item, list):
            keep = min(len(item), 20)
            if len(item) > keep:
                count(path + ".items", len(item) - keep)
            return [visit(val, f"{path}[{index}]", limit)
                    for index, val in enumerate(item[:keep])]
        if isinstance(item, str) and len(item) > limit:
            count(path + ".characters", len(item) - limit)
            return item[:limit] + "…"
        return item

    # Paths and SHAs are useful for independent Git review. Ordinary summaries
    # are clipped separately; never include raw responses or diagnostic blobs.
    result = visit(result, limit=600)
    if clipping:
        result["truncation"] = dict(clipping)

    # Drop low-priority supporting lists first, then reduce semantic strings.
    # Keep at least one blocker/remaining-work item even in adversarial reports.
    list_paths = [
        ("implementation", "files_changed"), ("implementation", "tests_run"),
        ("result", "files_changed"), ("result", "checks_performed"),
        ("result", "review_notes"), ("result", "evidence"),
        ("result", "findings"), ("result", "sources"),
        ("result", "artifact_evidence"), ("result", "artifacts"),
        ("result", "limitations"), ("result", "risks"), ("result", "remaining_work"),
        ("planner", "coordination", "acceptance_focus"),
        ("planner", "coordination", "risks"),
        ("implementation", "risks"), ("implementation", "remaining_work"),
    ]
    for path in list_paths:
        parent = result
        for key in path[:-1]:
            parent = parent.get(key, {})
        rows = parent.get(path[-1])
        minimum = 1 if path[-1] in ("risks", "remaining_work") else 0
        while isinstance(rows, list) and len(rows) > minimum and json_size(result) > max_budget:
            rows.pop()
            name = ".".join(path) + ".items"
            clipping[name] = clipping.get(name, 0) + 1
            result["truncation"] = dict(clipping)
            if path[0] == "planner" and "luna" in result:
                result["luna"] = json.loads(json.dumps(result["planner"]))

    # A single enormous remaining-work item or escaped Unicode still needs a
    # hard bound. Truncation counts never turn omitted blockers into an empty list.
    def longest_string(item, path=()):
        candidates = []
        if isinstance(item, dict):
            for key, val in item.items():
                if key in ("truncation", "luna"):
                    continue
                candidates.extend(longest_string(val, path + (key,)))
        elif isinstance(item, list):
            for index, val in enumerate(item):
                candidates.extend(longest_string(val, path + (index,)))
        # The shortest replacement is 24 characters plus an ellipsis. Selecting
        # that 25-character replacement again would never shrink it.
        elif isinstance(item, str) and len(item) > 25:
            candidates.append((len(item), path))
        return candidates

    while json_size(result) > max_budget:
        candidates = longest_string(result)
        if not candidates:
            # Finite schemas should never reach this; fail clearly, never emit
            # an unbounded result that claims to have met the contract.
            raise ValueError("Report metadata cannot fit the configured budget")
        _, path = max(candidates, key=lambda row: row[0])
        parent = result
        for key in path[:-1]:
            parent = parent[key]
        previous = parent[path[-1]]
        kept = max(24, len(previous) // 2)
        parent[path[-1]] = previous[:kept] + "…"
        name = ".".join(str(key) for key in path if not isinstance(key, int)) + ".characters"
        count(name, len(previous) - kept)
        if path[0] == "planner" and "luna" in result:
            result["luna"] = json.loads(json.dumps(result["planner"]))
        result["truncation"] = dict(clipping)
    if "planner" in result and "luna" in result:
        # Aliases must remain identical; budget their shared contents together.
        result["luna"] = json.loads(json.dumps(result["planner"]))
        if json_size(result) > max_budget:
            raise ValueError("Report aliases exceed the configured budget")
    return result


class ProgressEmitter:
    """One bounded notification stream; no inference, polling or transcript text."""

    def __init__(self, ctx, send):
        self.ctx = ctx
        self.send = send
        self.sequence = 0
        self.workspace = None
        self.last_ordinary_emit_time = float("-inf")
        self.ordinary_events_count = 0
        self.priority_events_count = 0
        self.coalescing_notice_emitted = False
        self.priority_notice_emitted = False
        self.seen = set()
        self.seen_order = deque()
        self.pending = None
        self.flush_task = None
        self.closed = False
        self.last_tests = None
        try:
            meta = ctx.request_context.meta
            self.enabled = meta is not None and meta.progressToken is not None
        except AttributeError:
            # Test contexts deliberately implement only report_progress.
            self.enabled = True
        except (RuntimeError, ValueError):
            self.enabled = False

    async def _send(self, message):
        if not self.enabled or self.closed:
            return
        self.sequence += 1
        await self.send(self.ctx, self.sequence, message[:MAX_PROGRESS_MESSAGE_CHARS])

    async def emit(self, message: str, *, priority=False, lifecycle=True):
        if not self.enabled or self.closed:
            return
        message = " ".join(str(message).split())[:MAX_PROGRESS_MESSAGE_CHARS]
        if not message or message in self.seen:
            return
        self.seen.add(message)
        self.seen_order.append(message)
        if len(self.seen_order) > 64:
            self.seen.discard(self.seen_order.popleft())
        if lifecycle:
            self.pending = None
            await self._send(message)
            return
        if priority:
            self.pending = None
            if self.priority_events_count >= MAX_PRIORITY_PROGRESS_EVENTS:
                if not self.priority_notice_emitted:
                    self.priority_notice_emitted = True
                    await self._send("Further blocker updates coalesced; see the terminal report.")
                return
            self.priority_events_count += 1
            await self._send(message)
            return
        if self.ordinary_events_count >= MAX_ORDINARY_PROGRESS_EVENTS:
            if not self.coalescing_notice_emitted:
                self.coalescing_notice_emitted = True
                await self._send("Further activity coalesced; see the terminal report.")
            return
        elapsed = time.monotonic() - self.last_ordinary_emit_time
        if elapsed < ORDINARY_PROGRESS_INTERVAL_SECONDS:
            self.pending = message
            if self.flush_task is None or self.flush_task.done():
                self.flush_task = asyncio.create_task(
                    self._flush_after(ORDINARY_PROGRESS_INTERVAL_SECONDS - elapsed))
            return
        # A busy host can resume emit() after the interval but before its older
        # timer runs. The immediate latest update supersedes that pending text.
        self.pending = None
        self.ordinary_events_count += 1
        self.last_ordinary_emit_time = time.monotonic()
        await self._send(message)

    async def _flush_after(self, delay):
        while not self.closed:
            await asyncio.sleep(delay)
            if self.closed or not self.pending:
                return
            # An immediate update may have reset the interval while this timer
            # was delayed. Do not let the obsolete wake flush a newer burst early.
            remaining = ORDINARY_PROGRESS_INTERVAL_SECONDS - (
                time.monotonic() - self.last_ordinary_emit_time)
            if remaining > 0:
                delay = remaining
                continue
            message, self.pending = self.pending, None
            if self.ordinary_events_count >= MAX_ORDINARY_PROGRESS_EVENTS:
                return
            self.ordinary_events_count += 1
            self.last_ordinary_emit_time = time.monotonic()
            await self._send(message)
            if not self.pending:
                return
            # Updates can arrive while the transport is sending. Keep their one
            # coalesced timer alive rather than losing the latest pending item.
            delay = max(0, ORDINARY_PROGRESS_INTERVAL_SECONDS - (
                time.monotonic() - self.last_ordinary_emit_time))

    async def forward(self, source_sequence: int, update):
        del source_sequence
        if not self.enabled or not isinstance(update, dict):
            return
        kind = update.get("source_step_type")
        state = update.get("source_state")
        tool = update.get("source_tool_name")
        if update.get("subagent_state") in ("ACTIVE", "DONE"):
            label = "running" if update["subagent_state"] == "ACTIVE" else "completed"
            await self.emit(f"Antigravity subagent activity {label}", lifecycle=False)
            return
        if kind != "tool":
            return
        tests = update.get("tests") or {}
        signature = (tests.get("last_command"), tests.get("state"))
        if tool == "run_command" and signature != self.last_tests and signature[0]:
            self.last_tests = signature
            if signature[1] in ("running", "passed", "failed"):
                for label in ("Tests running", "Tests passed", "Tests failed"):
                    self.seen.discard(label)
                # The state comes from structured command facts, never activity prose.
                await self.emit("Tests " + signature[1],
                                priority=signature[1] == "failed", lifecycle=False)
                return
        if state == "ERROR" or update.get("source_error"):
            await self.emit("Antigravity tool reported a blocker", priority=True, lifecycle=False)
            return
        if tool in FILE_TOOL_NAMES and update.get("current_file"):
            target = str(update["current_file"]).replace("\\", "/")
            if self.workspace:
                root = str(self.workspace).replace("\\", "/").rstrip("/") + "/"
                if os.path.normcase(target).startswith(os.path.normcase(root)):
                    target = target[len(root):]
                elif Path(target).is_absolute():
                    target = Path(target).name
            action = "Updated" if state in ("DONE", "COMPLETED", "SUCCESS") else "Editing"
            await self.emit(f"{action} {target[:110]}", lifecycle=False)

    async def close(self):
        self.closed = True
        if self.flush_task and not self.flush_task.done():
            self.flush_task.cancel()
            try:
                await self.flush_task
            except asyncio.CancelledError:
                pass
