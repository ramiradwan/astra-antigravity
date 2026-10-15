"""Server-owned jobs and a durable, bounded outbox for model-facing events.

Receiving an event is a condition wait, not a status request. Disconnecting a
receiver never cancels its worker. Reconnecting replays unacknowledged events;
another server may inspect saved evidence but never takes over a worker.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from communication import bound_report, json_size
from task_types import result_key, semantic_result, validate_task_type

MAX_DECISION_EVENTS = 8
MAX_DECISION_CHARS = 600
MAX_TERMINAL_CHARS = 1600
FINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _terminal_body(report):
    task_type = report.get("task_type", "implementation")
    implementation = semantic_result(report)
    worktree = implementation.get("worktree") or {}
    status = report.get("status") or "failed"
    body = {
        "status": status,
        "task_type": task_type,
        "summary": _text(implementation.get("summary") or report.get("message")
                         or "Delegation " + status + "; inspect saved evidence", 320),
        "tests_passed": implementation.get("tests_passed"),
        "structured_report_valid": implementation.get("structured_report_valid"),
        "files_changed_count": len(implementation.get("files_changed") or []),
        "tests_run_count": len(implementation.get("tests_run") or []),
        "remaining_work_count": len(implementation.get("remaining_work") or []),
        "risks_count": len(implementation.get("risks") or []),
        "remaining_work": [_text(item, 120) for item in (implementation.get("remaining_work") or [])[:1]],
        "risks": [_text(item, 120) for item in (implementation.get("risks") or [])[:1]],
        "worktree": worktree.get("path") or worktree.get("workspace"),
        "branch": worktree.get("branch"),
        "commit_sha": implementation.get("commit_sha"),
        "verification_required": bool(report.get("task_id")),
        "next_action": "verify_changes" if status == "completed" else
                       "choose_model" if status == "model_unavailable" else "inspect_saved_work",
    }
    if task_type != "implementation":
        for field in ("tests_passed", "tests_run_count", "commit_sha", "branch"):
            body.pop(field, None)
        body.update(
            checks_passed=implementation.get("checks_passed"),
            checks_performed_count=len(implementation.get("checks_performed") or []),
            artifacts_count=len(implementation.get("artifacts") or []),
            structural_checks_passed=(report.get("verification") or {}).get("structural_checks_passed"),
            next_action="verify_result" if status == "completed" else body["next_action"],
        )
    if status == "capability_unavailable":
        body["next_action"] = "resolve_capabilities"
        body["unavailable_capabilities"] = (report.get("capabilities") or {}).get("unavailable") or []
    selection = report.get("model_selection") or {}
    if selection.get("offered_fallback"):
        body["model_offer"] = {key: selection["offered_fallback"].get(key)
                               for key in ("profile", "available", "label")}
    if report.get("error"):
        body["error_present"] = True
    if report.get("error_code"):
        body["error_code"] = report["error_code"]
    return body


def _bounded_event(event, limit):
    """Keep identity, outcome and failure flags; omit evidence explicitly."""
    omitted = []
    for field in ("model_offer", "unavailable_capabilities", "branch", "worktree", "risks", "remaining_work"):
        if json_size(event) <= limit:
            break
        if field in event:
            event.pop(field)
            omitted.append(field)
            event["omitted_fields"] = omitted[:]
    while json_size(event) > limit and len(event.get("summary", "")) > 24:
        event["summary"] = event["summary"][:max(24, len(event["summary"]) // 2)]
        if "summary" not in omitted:
            omitted.append("summary")
            event["omitted_fields"] = omitted[:]
    if json_size(event) > limit:
        raise ValueError("Event identity exceeds the delivery budget")
    return event


class DelegationStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS delegations (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                fingerprint TEXT NOT NULL, owner TEXT NOT NULL,
                workspace TEXT NOT NULL, phase TEXT NOT NULL,
                task_id TEXT, status TEXT NOT NULL, terminal INTEGER NOT NULL DEFAULT 0,
                sequence INTEGER NOT NULL DEFAULT 0, acknowledged INTEGER NOT NULL DEFAULT 0,
                suppressed INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, result TEXT
            );
            CREATE TABLE IF NOT EXISTS delegation_events (
                delegation_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                dedup_key TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (delegation_id, sequence),
                UNIQUE (delegation_id, dedup_key)
            );
        """)
        # Multiple Codex hosts can open the same state directory. Serialize the
        # schema check with migration so neither host races an ALTER TABLE.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(delegations)")}
            if "suppressed" not in columns:
                self.db.execute("ALTER TABLE delegations ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0")
            if "task_type" not in columns:
                self.db.execute("ALTER TABLE delegations ADD COLUMN task_type TEXT NOT NULL DEFAULT 'implementation'")

    def create(self, owner, workspace, phase, fingerprint, request_id=None, task_id=None, busy=False,
               task_type="implementation"):
        validate_task_type(task_type)
        request_id = request_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id):
            raise ValueError("request_id must contain 1-128 letters, digits, dots, underscores or hyphens")
        with self.lock, self.db:
            existing = self.db.execute("SELECT * FROM delegations WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("request_id already belongs to a different delegation")
                return dict(existing), False
            if busy or phase == "continuation" and self.db.execute(
                    "SELECT 1 FROM delegations WHERE phase='continuation' AND task_id=? AND terminal=0", (task_id,)).fetchone():
                raise ValueError("A correction of this task is already pending")
            job_id = "delegation-" + uuid.uuid4().hex[:16]
            self.db.execute(
                "INSERT INTO delegations (id,request_id,fingerprint,owner,workspace,phase,task_id,status,created_at,task_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, request_id, fingerprint, owner, workspace, phase, task_id, "queued", time.time(), task_type))
            return self.get(job_id), True

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM delegations WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError(f"Delegation '{job_id}' not found")
            return dict(row)

    def update(self, job_id, *, status=None, task_id=None):
        with self.lock, self.db:
            if status:
                self.db.execute("UPDATE delegations SET status=? WHERE id=? AND terminal=0", (status, job_id))
            if task_id:
                self.db.execute("UPDATE delegations SET task_id=? WHERE id=?", (task_id, job_id))

    def publish(self, job_id, kind, body, *, result=None):
        # Result and final event commit in the SAME transaction. A successful
        # tool response can never exist only in an in-memory callback.
        with self.lock, self.db:
            job = self.get(job_id)
            if job["terminal"]:
                return None
            terminal = result is not None
            dedup = hashlib.sha256(json.dumps([kind, body], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
            if self.db.execute("SELECT 1 FROM delegation_events WHERE delegation_id=? AND dedup_key=?", (job_id, dedup)).fetchone():
                return None
            if not terminal and job["sequence"] >= MAX_DECISION_EVENTS:
                self.db.execute("UPDATE delegations SET suppressed=suppressed+1 WHERE id=?", (job_id,))
                return None
            sequence = job["sequence"] + 1
            event = {"delegation_id": job_id, "event_id": f"{job_id}:{sequence}",
                     "sequence": sequence, "task_id": job["task_id"],
                     "kind": kind, "terminal": terminal, "result_ref": job_id,
                     "task_type": job["task_type"], **body}
            if terminal and job["suppressed"]:
                event["decision_updates_suppressed"] = job["suppressed"]
            event = _bounded_event(event, MAX_TERMINAL_CHARS if terminal else MAX_DECISION_CHARS)
            self.db.execute("INSERT INTO delegation_events VALUES (?,?,?,?)", (job_id, sequence, dedup, json.dumps(event, separators=(",", ":"))))
            self.db.execute("UPDATE delegations SET sequence=? WHERE id=?", (sequence, job_id))
            if terminal:
                self.db.execute("UPDATE delegations SET status=?,terminal=1,result=? WHERE id=?",
                                (result.get("status") or "failed", json.dumps(bound_report(result), separators=(",", ":")), job_id))
            return event

    def events(self, job_id, after_sequence):
        with self.lock:
            rows = self.db.execute("SELECT payload FROM delegation_events WHERE delegation_id=? AND sequence>? ORDER BY sequence LIMIT 1", (job_id, after_sequence)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def acknowledge(self, job_id, sequence):
        with self.lock, self.db:
            job = self.get(job_id)
            if type(sequence) is not int or not 0 <= sequence <= job["sequence"]:
                raise ValueError("Acknowledgement must be an existing, non-negative sequence")
            self.db.execute("UPDATE delegations SET acknowledged=MAX(acknowledged,?) WHERE id=?", (sequence, job_id))

    def close(self):
        self.db.close()


class JobProgress:
    """Ordinary updates stay out of the model outbox. No planner runs here."""
    def __init__(self, manager, job_id, base):
        self.manager, self.job_id, self.base = manager, job_id, base
        self.workspace = None
        self.unsubscribe = None
        self.closed = False

    async def emit(self, message, **kwargs):
        if message.startswith("Planning"):
            self.manager.store.update(self.job_id, status="planning")

    async def started(self, started):
        self.captured_start(started["task_id"])
        self.manager.store.update(self.job_id, status="running")
        if hasattr(self.base, "subscribe_decisions"):
            loop = asyncio.get_running_loop()
            self.unsubscribe = self.base.subscribe_decisions(
                started["task_id"], lambda update: loop.call_soon_threadsafe(self.decision, update))

    def captured_start(self, task_id):
        # Called inside the startup thread too: a late start remains attributable
        # even if cancellation prevents the normal started callback.
        self.manager.store.update(self.job_id, task_id=task_id)
        if hasattr(self.base, "get_task"):
            record = self.base.get_task(task_id)
            self.workspace = getattr(record, "workspace", None) or self.workspace

    def decision(self, update):
        if self.closed or update.get("decision_required") is not True:
            return
        self.manager.publish(self.job_id, "needs_decision", {
            "status": "running", "next_action": "review_blocker",
            "summary": _text(update.get("decision_reason") or "Worker requires parent input", 180)})

    async def forward(self, sequence, update):
        self.decision(update)

    async def close(self):
        self.closed = True
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe = None


class BackgroundDelegations:
    def __init__(self, state_dir):
        self.owner = uuid.uuid4().hex
        self.store = DelegationStore(Path(state_dir) / "delegations.sqlite3")
        self.workers = {}
        self.changed = {}
        self.entered = set()
        self.cancel_requested = set()

    def receipt(self, job):
        worker = self.workers.get(job["id"])
        return {"delegation_id": job["id"], "task_id": job["task_id"],
                "task_type": job["task_type"],
                "status": job["status"] if job["terminal"] or worker is not None and not worker.done() else "unknown",
                "delivery": "background", "terminal": bool(job["terminal"]),
                "sequence": job["sequence"], "acknowledged_sequence": job["acknowledged"]}

    def start(self, *, workspace, phase, inputs, execute, base, request_id=None, task_id=None, finished=None, busy=False,
              task_type="implementation"):
        fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        job, created = self.store.create(self.owner, workspace, phase, fingerprint, request_id, task_id, busy, task_type)
        if created:
            self.changed[job["id"]] = asyncio.Event()
            released = False
            def finish_once():
                nonlocal released
                if not released:
                    released = True
                    if finished:
                        finished()
            worker = asyncio.create_task(self._run(job["id"], execute, base, finish_once))
            self.workers[job["id"]] = worker
            def settled(future):
                # Wake receivers even if persistence itself failed. They must
                # report unavailable ownership rather than wait indefinitely.
                if not future.cancelled():
                    future.exception()
                self.workers.pop(job["id"], None)
                self.entered.discard(job["id"])
                self.cancel_requested.discard(job["id"])
                self.changed[job["id"]].set()
                finish_once()
            worker.add_done_callback(settled)
        return self.receipt(job), created

    async def _run(self, job_id, execute, base, finished):
        self.entered.add(job_id)
        progress = JobProgress(self, job_id, base)
        try:
            if job_id in self.cancel_requested:
                raise asyncio.CancelledError()
            report = await execute(progress)
            if not isinstance(report, dict):
                raise ValueError("Gateway worker returned an invalid terminal report")
        except asyncio.CancelledError:
            report = {"status": "cancelled", "task_id": self.store.get(job_id)["task_id"],
                      "message": "Delegation cancelled; review saved work before cleanup"}
        except Exception as exc:
            report = {"status": "failed", "task_id": self.store.get(job_id)["task_id"],
                      "message": "Gateway operation failed; inspect saved evidence",
                      "error_code": type(exc).__name__,
                      "error": _text(exc, 1000) or type(exc).__name__}
        task_type = self.store.get(job_id)["task_type"]
        if report.get("task_type", task_type) != task_type:
            report = {"status": "failed", "task_id": self.store.get(job_id)["task_id"],
                      "message": "The task result did not preserve its saved task type."}
        report["task_type"] = task_type
        key = result_key(report["task_type"])
        if progress.workspace and key not in report:
            report[key] = {"worktree": {"path": progress.workspace}, "structured_report_valid": False}
        await progress.close()
        try:
            if report.get("task_id"):
                self.store.update(job_id, task_id=report["task_id"])
            kind = report.get("status") if report.get("status") in FINAL_STATUSES else "needs_decision"
            try:
                self.publish(job_id, kind, _terminal_body(report), result=report)
            except (ValueError, TypeError) as exc:
                # A malformed report must still produce a durable failure. I/O
                # failures propagate: never claim delivery without persistence.
                failed = {"status": "failed", "task_id": self.store.get(job_id)["task_id"],
                          "error": "Terminal report could not be encoded: " + _text(exc, 200)}
                self.publish(job_id, "failed", _terminal_body(failed), result=failed)
        finally:
            finished()
            self.workers.pop(job_id, None)
            self.entered.discard(job_id)
            self.cancel_requested.discard(job_id)
            self.changed.setdefault(job_id, asyncio.Event()).set()

    def publish(self, job_id, kind, body, **kwargs):
        event = self.store.publish(job_id, kind, body, **kwargs)
        if event:
            self.changed.setdefault(job_id, asyncio.Event()).set()
        return event

    async def receive(self, job_id, after_sequence=None):
        if after_sequence is not None and (type(after_sequence) is not int or after_sequence < 0):
            raise ValueError("after_sequence must be a non-negative integer")
        signal = self.changed.setdefault(job_id, asyncio.Event())
        while True:
            signal.clear()
            job = self.store.get(job_id)
            if after_sequence is not None and after_sequence > job["sequence"]:
                raise ValueError("after_sequence exceeds the delegation's sequence")
            # Explicit cursors can recover even acknowledged events if a host
            # lost its own context. The default resumes after durable ACKs.
            cursor = job["acknowledged"] if after_sequence is None else after_sequence
            events = self.store.events(job_id, cursor)
            if events or job["terminal"]:
                return {"delegation_id": job_id, "events": events,
                        "closed": bool(job["terminal"] and (not events or events[-1]["terminal"]))}
            worker = self.workers.get(job_id)
            if worker is None or worker.done():
                # A stored PID or owner token is not evidence of process life.
                return {"delegation_id": job_id, "events": [], "closed": True,
                        "status": "unknown", "delivery_suspended": True,
                        "message": "Worker ownership is unavailable; inspect saved work. No task was restarted."}
            await signal.wait()

    def acknowledge(self, job_id, sequence):
        self.store.acknowledge(job_id, sequence)
        return {"delegation_id": job_id, "acknowledged_sequence": self.store.get(job_id)["acknowledged"]}

    def result(self, job_id):
        job = self.store.get(job_id)
        if not job["terminal"]:
            raise ValueError("Delegation has no terminal result; await its event")
        return json.loads(job["result"])

    async def cancel(self, job_id):
        job = self.store.get(job_id)
        if job["terminal"]:
            return self.receipt(job)
        worker = self.workers.get(job_id)
        if worker is None:
            raise ValueError("Cannot cancel a delegation owned by another server; inspect saved work")
        self.cancel_requested.add(job_id)
        if job_id in self.entered:
            worker.cancel()
        # Cancellation is a request. Final confirmation arrives on the outbox.
        return {"delegation_id": job_id, "task_id": job["task_id"], "status": "cancelling"}

    async def close(self):
        pending = list(self.workers.values())
        for job_id, worker in list(self.workers.items()):
            self.cancel_requested.add(job_id)
            if job_id in self.entered:
                worker.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.store.close()


def inspect_saved_receipt(state_dir, request_id, active_workers=None):
    """Recover a lost launch receipt without creating a manager or rewriting data."""
    path = Path(state_dir) / "delegations.sqlite3"
    if not path.is_file():
        raise ValueError("No saved background delegations")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM delegations WHERE request_id=?", (request_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError("No delegation matches the launch request_id")
    active = (active_workers or {}).get(row["id"])
    known = active is not None and not active.done()
    return {"delegation_id": row["id"], "task_id": row["task_id"], "delivery": "background",
            "task_type": row["task_type"] if "task_type" in row.keys() else "implementation",
            "status": row["status"] if row["terminal"] or known else "unknown",
            "terminal": bool(row["terminal"]), "sequence": row["sequence"],
            "acknowledged_sequence": row["acknowledged"]}
