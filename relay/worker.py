#!/usr/bin/env python3
"""User-owned outbound relay worker. No local HTTP or MCP listener.

Run through the repository's locked Python requirements. Codex/Antigravity use
normal CLI authentication on this host. Provider transcripts never enter the relay.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import importlib
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
TOKEN = re.compile(r"^[a-f0-9]{64}\.[a-f0-9]{32}\.[a-f0-9]{64}$")
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
TASK_TYPES = ("implementation", "analysis", "research", "document")
MAX_FRAME = 32_768
MAX_ARTIFACT_BYTES = 12_000
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
ARTIFACT_CHUNK_BYTES = 16 * 1024
ARTIFACT_IDLE_SECONDS = 20
ARTIFACT_TOTAL_SECONDS = 300
MAX_RESULT_CHARS = 8_000


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def safe_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Invalid relay identifier")
    return value


def relay_origin(value, local=False):
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Relay URL must be a canonical origin")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
        raise ValueError("Relay requires HTTPS outside explicit local testing")
    return value.rstrip("/")


def private_directory(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("Private state directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path.resolve()


def private_json(path, data):
    path = Path(path)
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Private configuration must not be a symlink")
    temporary = path.with_name(path.name + ".new")
    if temporary.is_symlink():
        raise ValueError("Private configuration temporary file must not be a symlink")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
        stream.write(canonical(data) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def single_worker(state, lock_name="worker.lock"):
    """One daemon per private journal, including two shells on the same host."""
    path = Path(state) / lock_name
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Another worker already owns this journal") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Another worker already owns this journal") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Journal:
    def __init__(self, state):
        self.path = Path(state) / "worker.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,job TEXT NOT NULL,
            operation TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,local_id TEXT,cursor INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS artifacts(job TEXT NOT NULL,id TEXT NOT NULL,path TEXT NOT NULL,root TEXT NOT NULL,
            bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(job,id));
          CREATE TABLE IF NOT EXISTS retired_delivery(job TEXT PRIMARY KEY,reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1),worker TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS computer_state(id INTEGER PRIMARY KEY CHECK(id=1),paused INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0);
          INSERT OR IGNORE INTO computer_state(id) VALUES(1);
          CREATE TABLE IF NOT EXISTS computer_operations(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,action TEXT NOT NULL,result TEXT NOT NULL);
        """)
        if os.name != "nt":
            self.path.chmod(0o600)
        self.changed = asyncio.Event()

    def bind_identity(self, credential):
        identity = ".".join(credential.split(".")[:2])
        previous = self.db.execute("SELECT worker FROM identity WHERE id=1").fetchone()
        if previous and previous["worker"] != identity:
            raise ValueError("A different worker cannot resume this local journal")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO identity(id,worker) VALUES(1,?)", (identity,))

    def command(self, command):
        key = safe_id(command["command_id"])
        job = safe_id(command["delegation_id"])
        operation = command["operation"]
        if operation not in ("delegate", "continue", "cancel", "cleanup", "artifact"):
            raise ValueError("Unsupported worker operation")
        # allow_start is an ephemeral, single-delivery permit. The command's
        # durable identity excludes it, so reconnect can replay the same command.
        body = {"job": job, "operation": operation, "payload": command["payload"]}
        fingerprint = hashlib.sha256(canonical(body).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM commands WHERE id=?", (key,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise ValueError("Command identity was reused for different work")
            return dict(row), False
        with self.db:
            self.db.execute("INSERT INTO commands(id,fingerprint,job,operation,payload,state) VALUES(?,?,?,?,?,'prepared')",
                            (key, fingerprint, job, operation, canonical(command["payload"])))
        return dict(self.db.execute("SELECT * FROM commands WHERE id=?", (key,)).fetchone()), True

    def launch(self, command_id, local_id):
        with self.db:
            self.db.execute("UPDATE commands SET state='launched',local_id=? WHERE id=?", (safe_id(local_id), command_id))

    def job(self, job):
        row = self.db.execute("SELECT * FROM commands WHERE job=? AND operation IN ('delegate','continue')", (safe_id(job),)).fetchone()
        if not row:
            raise ValueError("No matching local delegation journal")
        return dict(row)

    def queue(self, message_id, payload, *, command_id=None, cursor=None):
        encoded = canonical({**payload, "message_id": safe_id(message_id)})
        if len(encoded.encode()) > MAX_FRAME:
            raise ValueError("Worker packet exceeds transport limit")
        with self.db:
            if payload.get("delegation_id") and self.db.execute("SELECT 1 FROM retired_delivery WHERE job=?", (payload["delegation_id"],)).fetchone():
                if command_id and cursor is not None:
                    self.db.execute("UPDATE commands SET cursor=? WHERE id=?", (cursor, command_id))
                return
            previous = self.db.execute("SELECT payload FROM outbox WHERE id=?", (message_id,)).fetchone()
            if previous and previous["payload"] != encoded:
                raise ValueError("Outbox identity was reused")
            self.db.execute("INSERT OR IGNORE INTO outbox(id,payload) VALUES(?,?)", (message_id, encoded))
            if command_id and cursor is not None:
                self.db.execute("UPDATE commands SET cursor=? WHERE id=?", (cursor, command_id))
        self.changed.set()

    def pending(self):
        return self.db.execute("SELECT id,payload FROM outbox ORDER BY rowid LIMIT 1").fetchone()

    def acknowledge(self, key, disposition=None):
        with self.db:
            if disposition == "task_expired":
                queued = self.db.execute("SELECT payload FROM outbox WHERE id=?", (key,)).fetchone()
                if queued:
                    job = json.loads(queued["payload"]).get("delegation_id")
                    if job:
                        self.db.execute("INSERT OR IGNORE INTO retired_delivery(job,reason) VALUES(?,'task_expired')", (safe_id(job),))
                        # Only retire this explicitly expired job, never unrelated delivery.
                        for row in self.db.execute("SELECT id,payload FROM outbox").fetchall():
                            if json.loads(row["payload"]).get("delegation_id") == job:
                                self.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            self.db.execute("DELETE FROM outbox WHERE id=?", (key,))

    def artifact(self, job, path, root):
        root = Path(root).resolve()
        supplied = root / str(path)
        actual = supplied.resolve()
        if supplied.is_symlink() or not actual.is_relative_to(root) or not actual.is_file():
            raise ValueError("Artifact is outside the task workspace")
        # Never export credential/configuration-shaped files, even if a model
        # lists one as a generated artifact.
        if any(part in (".git", ".codex", ".config", ".ssh") or part.startswith(".env") for part in actual.relative_to(root).parts):
            raise ValueError("Artifact is a protected configuration file")
        size = actual.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            return {"path": str(actual.relative_to(root)), "bytes": size, "export_available": False,
                    "download_available": False, "reason": "Artifact exceeds the 20 MiB download limit; inspect it on your worker"}
        digest = hashlib.sha256()
        counted = 0
        with actual.open("rb") as stream:
            for chunk in iter(lambda: stream.read(ARTIFACT_CHUNK_BYTES), b""):
                counted += len(chunk)
                if counted > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Artifact grew beyond the download limit")
                digest.update(chunk)
        if counted != size or actual.stat().st_size != size:
            raise ValueError("Artifact changed during registration")
        checksum = digest.hexdigest()
        identifier = hashlib.sha256((job + ":" + str(actual.relative_to(root))).encode()).hexdigest()[:32]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO artifacts(job,id,path,root,bytes,sha256) VALUES(?,?,?,?,?,?)",
                            (job, identifier, str(actual), str(root), size, checksum))
        return {"artifact_id": identifier, "path": str(actual.relative_to(root)), "bytes": size, "sha256": checksum,
                "file_name": actual.name[:240], "mime_type": mimetypes.guess_type(actual.name)[0] or "application/octet-stream",
                "export_available": size <= MAX_ARTIFACT_BYTES, "download_available": True}

    def snapshot_artifact(self, job, artifact_id, maximum=MAX_DOWNLOAD_BYTES):
        """Copy only a registered artifact into bounded, process-local memory."""
        row = self.db.execute("SELECT * FROM artifacts WHERE job=? AND id=?", (safe_id(job), safe_id(artifact_id))).fetchone()
        if not row:
            raise ValueError("Artifact was not authorized in this task's handoff")
        supplied, root = Path(row["path"]), Path(row["root"]).resolve()
        path = supplied.resolve(strict=True)
        if supplied.is_symlink() or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Artifact path changed after verification")
        if row["bytes"] > maximum or path.stat().st_size != row["bytes"]:
            raise ValueError("Artifact size changed or exceeds the download limit")
        snapshot, checksum, size = io.BytesIO(), hashlib.sha256(), 0
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(ARTIFACT_CHUNK_BYTES), b""):
                    size += len(chunk)
                    if size > maximum or size > row["bytes"]:
                        raise ValueError("Artifact changed during snapshot")
                    snapshot.write(chunk)
                    checksum.update(chunk)
            if size != row["bytes"] or checksum.hexdigest() != row["sha256"]:
                raise ValueError("Artifact content changed after verification")
            snapshot.seek(0)
            return snapshot, {"artifact_id": artifact_id, "file_name": path.name[:240],
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "size": size, "sha256": row["sha256"]}
        except BaseException:
            snapshot.close()
            raise

    def read_artifact(self, job, artifact_id):
        row = self.db.execute("SELECT * FROM artifacts WHERE job=? AND id=?", (safe_id(job), safe_id(artifact_id))).fetchone()
        if not row:
            raise ValueError("Artifact was not authorized in this task's handoff")
        supplied = Path(row["path"])
        path = supplied.resolve()
        if supplied.is_symlink() or not path.is_relative_to(Path(row["root"])) or not path.is_file():
            raise ValueError("Artifact path changed after verification")
        if path.stat().st_size != row["bytes"] or row["bytes"] > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact size changed after verification")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("Artifact content changed after verification")
        return {"id": artifact_id, "bytes": len(content), "sha256": row["sha256"], "encoding": "base64", "content": base64.b64encode(content).decode()}

    def close(self):
        self.db.close()

    def finish_command(self, command_id, response):
        with self.db:
            self.db.execute("UPDATE commands SET state=? WHERE id=?", ("done:" + canonical(response), command_id))


class Gateway:
    def __init__(self, state):
        provider_state = Path(state) / "provider-tasks"
        os.environ["ASTRA_ANTIGRAVITY_STATE_DIR"] = str(provider_state)
        sys.path.insert(0, str(ROOT / "plugins" / "astra-antigravity"))
        self.module = importlib.import_module("luna_gateway")
        if self.module._background_manager is not None:
            raise ValueError("Another worker still owns the private provider gateway")
        # The companion can archive a connection and create another in the same
        # process. Imported module globals must never carry old provider handles
        # or cursors into that newly approved worker identity.
        provider = importlib.import_module("server")
        quota = importlib.import_module("quota_server")
        provider.manager = provider.TaskManager(provider_state)
        quota.coordinator = quota.QuotaCoordinator(provider.manager)
        self.module.coordinator = self.module.AdvancedWorktreeCoordinator(quota.coordinator)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def launch(self, command, workspace):
        payload = command["payload"]
        return await self.module.delegate_via_luna(
            payload["task"], str(workspace), None, request_id="relay." + command["command_id"], delivery="background",
            isolation=payload["isolation"], reasoning=payload["reasoning"], model=payload.get("model"),
            task_type=payload["task_type"], required_capabilities=payload["required_capabilities"])

    async def continue_task(self, command, local_id):
        saved = self.module._background().store.get(local_id)
        if not saved["terminal"] or not saved["task_id"]:
            raise ValueError("Correction has no completed provider task")
        return await self.module.continue_via_luna(saved["task_id"], command["payload"]["findings"], None,
                                                  request_id="relay." + command["command_id"], delivery="background")

    async def events(self, local_id, cursor):
        return await self.module.receive_delegation_events(local_id, cursor)

    def acknowledge(self, local_id, cursor):
        return self.module.ack_delegation_events(local_id, cursor)

    def result(self, local_id):
        return self.module.read_delegation_result(local_id)

    async def cancel(self, local_id):
        return await self.module.cancel_delegation(delegation_id=local_id)

    def cleanup(self, local_id, payload):
        task = self.module._background().store.get(local_id)
        if not task["terminal"] or not task["task_id"]:
            raise ValueError("Cleanup requires a completed provider task")
        return self.module.cleanup_task_worktree(task["task_id"], bool(payload.get("force")), bool(payload.get("delete_branch")))

    async def readiness(self, workspace, task_type, isolation):
        return await self.module.check_integration(str(workspace), isolation, task_type)

    async def close(self):
        if self.module._background_manager:
            try:
                await self.module._background_manager.close()
            finally:
                self.module._background_manager = None
        base = self.module.coordinator.base
        # Completed provider conversations remain resumable by their saved IDs;
        # close their idle processes/timers before the companion archives state.
        for task_id, record in list(base._tasks.items()):
            if record.status == "completed":
                base.cancel_task(task_id)


def scrub(value, replacements):
    if isinstance(value, dict):
        return {key: scrub(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, replacements) for item in value]
    if isinstance(value, str):
        for path, alias in sorted(replacements.items(), key=lambda p: len(p[0]), reverse=True):
            value = value.replace(path, alias).replace(path.replace("\\", "/"), alias)
        return value
    return value


class ArtifactSender:
    """One credit-driven download per socket; file bytes never enter the outbox."""
    def __init__(self, journal, send):
        self.journal, self.send, self.active = journal, send, None

    def close(self):
        if self.active:
            self.active["snapshot"].close()
            self.active = None

    async def fail(self, transfer_id, code="artifact_unavailable"):
        await self.send({"kind": "artifact_error", "transfer_id": transfer_id, "code": code,
                         "message": "The approved artifact could not be downloaded. Retry or inspect it on this computer."})

    async def expire(self):
        if self.active and (time.monotonic() - self.active["touched"] >= ARTIFACT_IDLE_SECONDS or
                            time.monotonic() - self.active["started"] >= ARTIFACT_TOTAL_SECONDS):
            identifier = self.active["id"]
            self.close()
            await self.fail(identifier, "artifact_timeout")

    async def handle(self, packet):
        try:
            identifier = safe_id(packet.get("transfer_id"))
        except ValueError:
            # A malformed download request must not shut down provider work.
            return
        await self.expire()
        kind = packet.get("kind")
        if kind == "artifact_abort":
            if self.active and self.active["id"] == identifier:
                self.close()
            return
        if kind == "artifact_open":
            if self.active:
                await self.fail(identifier, "artifact_busy")
                return
            try:
                maximum, chunk = packet.get("max_bytes"), packet.get("chunk_bytes")
                if type(maximum) is not int or not 0 < maximum <= MAX_DOWNLOAD_BYTES or chunk != ARTIFACT_CHUNK_BYTES:
                    raise ValueError("Invalid artifact bounds")
                snapshot, manifest = self.journal.snapshot_artifact(packet.get("delegation_id"), packet.get("artifact_id"), maximum)
                self.active = {"id": identifier, "snapshot": snapshot, "manifest": manifest, "offset": 0,
                               "started": time.monotonic(), "touched": time.monotonic()}
                await self.send({"kind": "artifact_manifest", "transfer_id": identifier, **manifest})
                if not manifest["size"]:
                    await self.send({"kind": "artifact_end", "transfer_id": identifier, "bytes": 0, "sha256": manifest["sha256"]})
                    self.close()
            except (ValueError, OSError):
                self.close()
                await self.fail(identifier)
            return
        if kind != "artifact_credit" or not self.active or self.active["id"] != identifier:
            await self.fail(identifier, "artifact_transfer_missing")
            return
        active = self.active
        if type(packet.get("credits")) is not int or packet["credits"] != 1 or type(packet.get("offset")) is not int or packet["offset"] != active["offset"]:
            self.close()
            await self.fail(identifier, "artifact_invalid_credit")
            return
        content = active["snapshot"].read(ARTIFACT_CHUNK_BYTES)
        await self.send({"kind": "artifact_chunk", "transfer_id": identifier, "offset": active["offset"],
                         "data": base64.b64encode(content).decode("ascii")})
        active["offset"] += len(content)
        active["touched"] = time.monotonic()
        if active["offset"] == active["manifest"]["size"]:
            await self.send({"kind": "artifact_end", "transfer_id": identifier, "bytes": active["offset"], "sha256": active["manifest"]["sha256"]})
            self.close()


class Worker:
    def __init__(self, config, state, gateway=None, *, controls=None):
        self.config = config
        self.state = Path(state).resolve()
        self.monitors = {}
        self.commands = {}
        self.stopping = asyncio.Event()
        self.last_work = time.monotonic()
        self.controls = controls
        self.external_stop = None
        self.refresh_requested = False
        self.access_expires_at = None
        self.approval_mode = "legacy_30_day"
        self._last_status = None
        self._last_status_at = 0
        self.workspaces = {}
        for alias, item in config["workspaces"].items():
            safe_id(alias)
            supplied = Path(item["path"]).expanduser()
            path = supplied.resolve(strict=True)
            if not path.is_dir() or supplied.is_symlink():
                raise ValueError("Workspace must be an existing directory without a symlink alias")
            if self.state.is_relative_to(path):
                raise ValueError("Private worker state must be outside every delegated workspace")
            self.workspaces[alias] = {**item, "path": path}
        self.journal = Journal(state)
        self.gateway = gateway or (None if config.get("setup_only") and not self.workspaces else Gateway(state))

    def admission(self):
        row = self.journal.db.execute("SELECT paused,revision FROM computer_state WHERE id=1").fetchone()
        return bool(row["paused"]), row["revision"]

    def set_admission(self, paused, revision=None):
        if type(paused) is not bool or (revision is not None and (type(revision) is not int or revision < 0)):
            raise ValueError("Invalid computer admission state")
        previous, current = self.admission()
        if revision is not None and revision < current:
            return False
        next_revision = current + 1 if revision is None else revision
        with self.journal.db:
            self.journal.db.execute("UPDATE computer_state SET paused=?,revision=? WHERE id=1", (int(paused), next_revision))
        return True

    def computer_snapshot(self):
        paused, revision = self.admission()
        active = len(self.monitors) + len(self.commands)
        snapshot = {"paused": paused, "active_tasks": active,
                    "pending_delivery": self.journal.pending() is not None}
        callback = getattr(self.controls, "control_snapshot", None)
        if callback:
            allowed = {"companion_version", "startup_enabled", "automatic_updates_enabled", "update_status", "uninstall_status", "installed"}
            supplied = callback()
            if isinstance(supplied, dict):
                snapshot.update({key: value for key, value in supplied.items() if key in allowed and
                                 (type(value) is bool or isinstance(value, str) and len(value) <= 160)})
                accounts = supplied.get("accounts")
                if isinstance(accounts, dict):
                    projected = {}
                    for name in ("codex", "agy", "git"):
                        item = accounts.get(name)
                        if isinstance(item, dict) and type(item.get("installed")) is bool and item.get("status") in ("ready", "sign_in_required", "missing", "unknown"):
                            projected[name] = {"installed": item["installed"], "status": item["status"],
                                               **({"summary": str(item["summary"])[:240]} if item.get("summary") else {})}
                    snapshot["accounts"] = projected
        snapshot["setup_only"] = not bool(self.workspaces)
        return snapshot

    async def accept_workspace(self, metadata):
        if not isinstance(metadata, dict):
            raise ValueError("Workspace approval is missing")
        alias = safe_id(metadata.get("id"))
        saved = json.loads((self.state / "worker.json").read_text(encoding="utf-8"))
        if saved.get("worker_identity") != self.config.get("worker_identity") or saved.get("relay") != self.config["relay"]:
            raise ValueError("Workspace approval cannot replace computer ownership")
        item = saved.get("workspaces", {}).get(alias)
        if not isinstance(item, dict) or item.get("isolation") not in (["worktree"], ["shared"]):
            raise ValueError("Workspace permission is invalid")
        supplied = Path(item["path"]).expanduser()
        actual = supplied.resolve(strict=True)
        if not actual.is_dir() or supplied.is_symlink() or self.state.is_relative_to(actual):
            raise ValueError("Workspace approval is outside its local permission")
        if alias in self.workspaces and (actual != self.workspaces[alias]["path"] or item["isolation"] != self.workspaces[alias]["isolation"]):
            raise ValueError("An existing workspace alias cannot change permission or location")
        public = {"id": alias, "label": str(item.get("label", alias))[:160], "isolation": item["isolation"],
                  "task_types": list(TASK_TYPES), "capabilities": ["local_files", "shell"]}
        self.config["workspaces"][alias] = {**item, "task_types": list(TASK_TYPES), "capabilities": public["capabilities"]}
        self.config["setup_only"] = False
        self.workspaces[alias] = {**self.config["workspaces"][alias], "path": actual}
        if self.gateway is None:
            self.gateway = Gateway(self.state)
        await self.readiness()
        return public

    def publish_computer_status(self, *, force=False):
        snapshot = self.computer_snapshot()
        # Delivery bookkeeping is not a status transition: sending status must
        # not keep generating another status packet forever.
        compare = {key: value for key, value in snapshot.items() if key != "pending_delivery"}
        now = time.monotonic()
        if force or compare != self._last_status or now - self._last_status_at >= 30:
            self.journal.queue("computer.status." + secrets.token_hex(8), {"kind": "computer_status",
                "revision": self.admission()[1], "snapshot": snapshot})
            self._last_status, self._last_status_at = compare, now

    async def computer_control(self, packet, *, local=False):
        identifier = safe_id(packet.get("operation_id"))
        action, parameters = packet.get("action"), packet.get("parameters", {})
        revision = packet.get("revision")
        if not isinstance(parameters, dict) or len(canonical(parameters).encode()) > 4096 or (not local and (type(revision) is not int or revision < 0)):
            raise ValueError("Invalid computer control")
        schemas = {"pause": set(), "resume": set(), "stop_tasks": set(), "keep_approved": set(),
                   "set_startup": {"enabled"}, "set_automatic_updates": {"enabled"}, "uninstall": {"erase_history"},
                   "check_accounts": set(), "install_provider": {"provider"}, "sign_in": {"provider"},
                   "configure_workspace": {"path", "alias", "isolation"}}
        if action not in schemas or set(parameters) - schemas[action]:
            raise ValueError("Unsupported computer control")
        if action in ("set_startup", "set_automatic_updates") and type(parameters.get("enabled")) is not bool:
            raise ValueError("A boolean setting is required")
        if action == "uninstall" and "erase_history" in parameters and type(parameters["erase_history"]) is not bool:
            raise ValueError("Invalid uninstall preference")
        if action in ("install_provider", "sign_in") and parameters.get("provider") not in (("codex", "agy", "git") if action == "install_provider" else ("codex", "agy")):
            raise ValueError("Invalid provider")
        if action == "configure_workspace" and (not isinstance(parameters.get("path"), str) or not 1 <= len(parameters["path"]) <= 1024 or
            not isinstance(parameters.get("alias"), str) or not ID.fullmatch(parameters["alias"]) or parameters.get("isolation") not in ("worktree", "shared")):
            raise ValueError("Invalid workspace approval")
        fingerprint = hashlib.sha256(canonical({"action": action, "parameters": parameters, "revision": revision}).encode()).hexdigest()
        previous = self.journal.db.execute("SELECT * FROM computer_operations WHERE id=?", (identifier,)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("Computer operation identity was reused")
            result = json.loads(previous["result"])
            if not local:
                self.journal.queue("computer.result." + identifier + "." + result["state"], result)
            return result
        state, summary, approved_workspace = "succeeded", "Computer setting updated.", None
        current = self.admission()[1]
        if not local and action in ("pause", "resume", "stop_tasks", "uninstall") and revision < current:
            state, summary = "rejected", "A newer computer setting is already active. Check status before trying again."
        elif action in ("pause", "resume", "stop_tasks"):
            self.set_admission(action != "resume", None if local else revision)
            if action == "stop_tasks":
                state, summary = "pending", "New tasks are paused. Waiting for current tasks to confirm cancellation."
                for row in self.journal.db.execute("SELECT DISTINCT local_id FROM commands WHERE state='launched' AND local_id IS NOT NULL").fetchall():
                    try:
                        await self.gateway.cancel(row["local_id"])
                    except Exception:
                        # Do not turn a provider's unconfirmed cancellation
                        # into a successful stop receipt.
                        summary = "New tasks are paused. Some cancellations need confirmation."
            else:
                summary = "New tasks are paused. Current tasks can finish." if action == "pause" else "This computer can receive tasks again."
        elif action == "keep_approved":
            self.approval_mode = "until_revoked"
            summary = "Computer approval updated. Credentials will rotate without changing saved tasks."
        else:
            if action == "uninstall":
                self.set_admission(True, None if local else revision)
            handler = getattr(self.controls, "control_handler", None)
            if not handler:
                state, summary = "rejected", "Open a compatible installed companion to use this control."
            else:
                try:
                    supplied = dict(parameters)
                    if action == "uninstall" and not local:
                        completion = packet.get("completion")
                        if not isinstance(completion, dict) or not isinstance(completion.get("token"), str) or not re.fullmatch(r"[a-f0-9]{32,128}", completion["token"]):
                            raise ValueError("Removal confirmation is missing")
                        owner = self.config.get("worker_identity", "").split(".")[0]
                        if not re.fullmatch(r"[a-f0-9]{64}", owner):
                            raise ValueError("Computer identity is missing")
                        supplied["completion"] = {"url": self.config["relay"] + "/worker/removals/" + owner + "/" + identifier + "/complete",
                            "token": completion["token"], "expires_at": completion.get("expires_at")}
                    reply = await asyncio.to_thread(handler, action, supplied, identifier)
                    if isinstance(reply, dict):
                        state = reply.get("state", "pending")
                        summary = str(reply.get("summary", "Computer action accepted."))[:240]
                        if action == "configure_workspace" and state == "succeeded":
                            approved_workspace = await self.accept_workspace(reply.get("workspace_approved"))
                    else:
                        state, summary = "failed", "The companion did not confirm this action."
                except Exception:
                    state, summary = "failed", "The companion could not complete this action. Existing task files are preserved."
        if state not in ("pending", "succeeded", "failed", "rejected"):
            state, summary = "failed", "The companion returned an invalid action response."
        result = {"kind": "computer_control_result", "operation_id": identifier, "state": state,
                  "summary": summary, "revision": self.admission()[1] if local else revision, "snapshot": self.computer_snapshot()}
        if approved_workspace:
            result["workspace_approved"] = approved_workspace
        with self.journal.db:
            self.journal.db.execute("INSERT INTO computer_operations(id,fingerprint,action,result) VALUES(?,?,?,?)",
                                    (identifier, fingerprint, action, canonical(result)))
        if not local:
            self.journal.queue("computer.result." + identifier + "." + state, result)
        self.publish_computer_status(force=True)
        return result

    def acknowledge_packet(self, key, disposition=None):
        row = self.journal.db.execute("SELECT payload FROM outbox WHERE id=?", (key,)).fetchone()
        payload = json.loads(row["payload"]) if row else None
        self.journal.acknowledge(key, disposition)
        if disposition == "operation_expired" and payload and payload.get("kind") == "computer_control_result":
            identifier = payload["operation_id"]
            operation = self.journal.db.execute("SELECT result FROM computer_operations WHERE id=?", (identifier,)).fetchone()
            if operation:
                result = json.loads(operation["result"])
                if result.get("state") == "pending":
                    result.update(state="unconfirmed", summary="The approval expired before completion could be confirmed. Check this computer before retrying.")
                    with self.journal.db:
                        self.journal.db.execute("UPDATE computer_operations SET result=? WHERE id=?", (canonical(result), identifier))
            callback = getattr(self.controls, "control_expired", None)
            if callback:
                callback(identifier)
        elif payload and payload.get("kind") == "computer_control_result" and payload.get("state") in ("pending", "succeeded"):
            operation = self.journal.db.execute("SELECT action FROM computer_operations WHERE id=?", (payload["operation_id"],)).fetchone()
            if operation and operation["action"] == "keep_approved":
                auth_path = self.state / "auth.json"
                auth = json.loads(auth_path.read_text(encoding="utf-8"))
                auth.update(approval_mode="until_revoked", refresh_credential=auth.get("refresh_credential", auth["credential"]))
                private_json(auth_path, auth)
                self.refresh_requested = True
            if operation and operation["action"] == "uninstall":
                callback = getattr(self.controls, "control_committed", None)
                if callback:
                    callback(payload["operation_id"])

    async def maintain_controls(self, progress=None, *, connected=True):
        queue = getattr(self.controls, "local_controls", None)
        if queue:
            import queue as queue_module
            for _ in range(16):
                try:
                    packet = queue.get_nowait()
                except queue_module.Empty:
                    break
                await self.computer_control(packet, local=True)
        callback = getattr(self.controls, "control_result", None)
        if callback:
            for row in self.journal.db.execute("SELECT id,action,result FROM computer_operations WHERE action NOT IN ('stop_tasks','uninstall')").fetchall():
                previous = json.loads(row["result"])
                if previous["state"] != "pending":
                    continue
                reply = callback(row["id"])
                if not isinstance(reply, dict) or reply.get("state") not in ("succeeded", "failed", "rejected"):
                    continue
                result = {**previous, "state": reply["state"], "summary": str(reply.get("summary", "Computer action finished."))[:240]}
                if row["action"] == "configure_workspace" and result["state"] == "succeeded":
                    try:
                        result["workspace_approved"] = await self.accept_workspace(reply.get("workspace_approved"))
                    except (ValueError, OSError):
                        result.update(state="failed", summary="Workspace approval could not be verified on this computer.")
                result["snapshot"] = self.computer_snapshot()
                with self.journal.db:
                    self.journal.db.execute("UPDATE computer_operations SET result=? WHERE id=?", (canonical(result), row["id"]))
                self.journal.queue("computer.result." + row["id"] + "." + result["state"], result)
        unconfirmed = self.journal.db.execute("SELECT 1 FROM commands WHERE operation IN ('delegate','continue') AND state IN ('prepared','launched','recovery_required') LIMIT 1").fetchone()
        if not self.monitors and not self.commands and not unconfirmed:
            for row in self.journal.db.execute("SELECT id,result FROM computer_operations WHERE action='stop_tasks'").fetchall():
                result = json.loads(row["result"])
                if result["state"] == "pending":
                    result.update(state="succeeded", summary="Tasks have stopped. New tasks remain paused.", snapshot=self.computer_snapshot())
                    with self.journal.db:
                        self.journal.db.execute("UPDATE computer_operations SET result=? WHERE id=?", (canonical(result), row["id"]))
                    if not row["id"].startswith("local."):
                        self.journal.queue("computer.result." + row["id"] + ".succeeded", result)
        if progress:
            progress({"status": "draining" if self.external_stop and self.external_stop.is_set() else "connected" if connected else "reconnecting",
                      "connected": connected, **self.computer_snapshot()})
        # Quiet idle companions must not emit a never-ending self-generated
        # queue while a full local Quit is draining delivery.
        if not (self.external_stop and self.external_stop.is_set()):
            self.publish_computer_status()

    def approved_workspace(self, payload):
        alias = safe_id(payload["workspace_id"])
        item = self.workspaces.get(alias)
        if not item or payload["isolation"] not in item["isolation"] or payload["task_type"] not in item["task_types"]:
            raise ValueError("Workspace, isolation or task type is not approved on this worker")
        if not set(payload["required_capabilities"]).issubset(item["capabilities"]):
            raise ValueError("Required capability is unavailable")
        path = Path(item["path"]).resolve(strict=True)
        if path != item["path"]:
            raise ValueError("Workspace alias changed after pairing")
        return path

    def recovery(self, job):
        self.journal.queue("recovery." + job, {"kind": "recovery_required", "delegation_id": job})

    async def command(self, command):
        self.last_work = time.monotonic()
        if command.get("operation") in ("delegate", "continue"):
            prior = self.journal.db.execute("SELECT local_id FROM commands WHERE id=?", (safe_id(command["command_id"]),)).fetchone()
            if not (prior and prior["local_id"]) and (self.admission()[0] or self.external_stop and self.external_stop.is_set()):
                if prior:
                    self.recovery(safe_id(command["delegation_id"]))
                    return
                self.journal.queue("deferred." + command["command_id"] + "." + str(self.admission()[1]), {
                    "kind": "command_deferred", "delegation_id": safe_id(command["delegation_id"]),
                    "command_id": command["command_id"], "reason": "computer_paused", "revision": self.admission()[1]})
                return
        row, created = self.journal.command(command)
        job = row["job"]
        operation = row["operation"]
        if row["state"].startswith("done:"):
            self.journal.queue("command." + row["id"], json.loads(row["state"][5:]))
            return
        if operation in ("delegate", "continue"):
            if row["state"] == "recovery_required":
                self.recovery(job)
                return
            if created and not command.get("allow_start"):
                # A delivery timeout is not evidence that another host didn't
                # launch. Missing local evidence means stop for reconciliation.
                self.recovery(job)
                with self.journal.db:
                    self.journal.db.execute("UPDATE commands SET state='recovery_required' WHERE id=?", (row["id"],))
                return
            try:
                if row["local_id"]:
                    local_id = row["local_id"]
                else:
                    if operation == "delegate":
                        path = self.approved_workspace(command["payload"])
                        receipt = await self.gateway.launch(command, path)
                    else:
                        source = self.journal.job(command["payload"]["source_id"])
                        if not source["local_id"]:
                            self.recovery(job)
                            return
                        receipt = await self.gateway.continue_task(command, source["local_id"])
                    local_id = safe_id(receipt["delegation_id"])
                    self.journal.launch(command["command_id"], local_id)
                self.journal.queue("receipt." + job, {"kind": "receipt", "delegation_id": job, "local_id": local_id})
                self.resume_monitor(job, local_id)
                # Stop may arrive while the provider creates its durable
                # receipt. Cancel that exact ID as soon as it becomes known.
                if self.journal.db.execute("SELECT 1 FROM computer_operations WHERE action='stop_tasks' AND json_extract(result,'$.state')='pending' LIMIT 1").fetchone():
                    try:
                        await self.gateway.cancel(local_id)
                    except Exception:
                        pass  # stop remains pending until provider evidence
            except Exception:
                # Some failures occur after a gateway's durable receipt. Never
                # synthesize a terminal failure or retry inference from an error.
                self.recovery(job)
            return
        source = self.journal.job(job)
        if not source["local_id"]:
            self.recovery(job)
            return
        response = {"kind": "command_result", "delegation_id": job,"command_id": row["id"]}
        try:
            if operation == "cancel":
                await self.gateway.cancel(source["local_id"])
                result = {"status": "cancellation_requested"}
            elif operation == "cleanup":
                result = await asyncio.to_thread(self.gateway.cleanup, source["local_id"], command["payload"])
            else:
                response["artifact"] = self.journal.read_artifact(job, command["payload"]["artifact_id"])
                result = None
            if result is not None:
                response["result"] = scrub(result, {str(Path.home()): "<worker-home>",str(self.state):"<worker-state>",
                    **{str(item["path"]):"workspace://"+alias for alias,item in self.workspaces.items()}})
        except Exception:
            response["error"] = {"code":"operation_failed","message":"The requested control or artifact could not be completed. Inspect the saved work locally; no task was restarted."}
        self.journal.finish_command(row["id"], response)
        self.journal.queue("command." + row["id"], response)

    def resume_monitor(self, job, local_id):
        if job not in self.monitors:
            task = asyncio.create_task(self.monitor(job, local_id))
            self.monitors[job] = task
            task.add_done_callback(lambda done, key=job: self.monitor_done(key, done))

    def resume_saved(self):
        # Previously acknowledged launch commands won't be re-sent by the relay.
        # Resume their saved event cursor without another inference call.
        for row in self.journal.db.execute("SELECT job,local_id FROM commands WHERE state='launched' AND local_id IS NOT NULL"):
            self.resume_monitor(row["job"], row["local_id"])

    def monitor_done(self, job, future):
        self.monitors.pop(job, None)
        if not future.cancelled() and future.exception():
            self.recovery(job)

    def handoff(self, job, raw):
        task_type = raw.get("task_type", "implementation")
        key = "implementation" if task_type == "implementation" else "result"
        semantic = raw.get(key) or {}
        source = self.journal.job(job)
        payload = json.loads(source["payload"])
        if source["operation"] == "continue":
            parent = self.journal.job(payload["source_id"])
            while parent["operation"] == "continue":
                parent = self.journal.job(json.loads(parent["payload"])["source_id"])
            payload = json.loads(parent["payload"])
        alias = payload["workspace_id"]
        worktree=semantic.get("worktree") or {}
        effective = worktree.get("path") or worktree.get("workspace") or str(self.workspaces[alias]["path"])
        replacements = {str(item["path"]): "workspace://" + name for name, item in self.workspaces.items()}
        replacements[str(Path.home())] = "<worker-home>"
        if effective:
            replacements[str(Path(effective).resolve())] = "workspace://" + alias + "/task-output"
        projected = {field: raw[field] for field in ("status", "task_type", "message", "model", "model_selection", "verification") if field in raw}
        if isinstance(raw.get("implementation_started"), bool):
            projected["implementation_started"] = raw["implementation_started"]
        selected_model = raw.get("model") or (raw.get("model_selection") or {}).get("model") or (raw.get("antigravity") or {}).get("requested_model")
        if isinstance(selected_model, str):
            projected["model"] = selected_model
        projected[key] = semantic
        projected = scrub(projected, replacements)
        artifacts = []
        if effective:
            outputs = semantic.get("artifacts") or (semantic.get("files_changed") if task_type == "implementation" else []) or []
            for artifact in outputs[:20]:
                name = artifact.get("path") if isinstance(artifact, dict) else artifact
                if isinstance(name, str):
                    if any(part in ("__pycache__", ".pytest_cache", "node_modules") for part in Path(name).parts):
                        continue
                    try:
                        artifacts.append(self.journal.artifact(job, name, effective))
                    except ValueError:
                        artifacts.append({"path": "<unexportable>", "export_available": False, "reason": "Artifact scope validation failed"})
        if artifacts:
            projected["artifact_refs"] = artifacts[:20]
        # Reuse the established bounded-report algorithm without importing the
        # gateway in credential-free worker tests.
        if len(canonical(projected)) > MAX_RESULT_CHARS:
            sys.path.insert(0, str(ROOT / "plugins" / "astra-antigravity"))
            from communication import bound_report
            projected = bound_report(projected, MAX_RESULT_CHARS)
        return projected

    async def monitor(self, job, local_id):
        row = self.journal.job(job)
        cursor = row["cursor"]
        while not self.stopping.is_set():
            batch = await self.gateway.events(local_id, cursor)
            for original in batch.get("events") or []:
                terminal = bool(original.get("terminal"))
                event = {"sequence": original["sequence"], "kind": original.get("kind", "decision"), "terminal": terminal,
                    "summary": str(original.get("summary") or original.get("message") or "Delegation update")[:1_600 if terminal else 600],
                    "task_type": original.get("task_type", "implementation"), "verification_required": bool(original.get("verification_required"))}
                if event["kind"] not in ("decision", "needs_decision", "completed", "failed", "cancelled", "recovery_required"):
                    event["kind"] = "needs_decision"
                packet = {"kind": "event", "delegation_id": job, "event": event}
                if terminal:
                    report = self.gateway.result(local_id)
                    packet["result"] = self.handoff(job, report)
                    selected_model = report.get("model") or (report.get("model_selection") or {}).get("model") or (report.get("antigravity") or {}).get("requested_model")
                    if isinstance(selected_model, str):
                        event["model"] = selected_model[:100]
                    event["checks_count"] = int(original.get("checks_performed_count") or original.get("tests_run_count") or 0)
                    event["artifacts_count"] = int(original.get("artifacts_count") or 0)
                event["summary"] = scrub(event["summary"], {str(Path.home()): "<worker-home>", **{str(item["path"]): "workspace://" + alias for alias,item in self.workspaces.items()}})
                cursor = original["sequence"]
                self.journal.queue("event." + job + "." + str(cursor), packet, command_id=row["id"], cursor=cursor)
                # The local outbox owns delivery from this point, including after
                # a network crash; upstream ACK does not depend on a live socket.
                self.gateway.acknowledge(local_id, cursor)
            if batch.get("delivery_suspended"):
                self.recovery(job)
                return
            if batch.get("closed"):
                with self.journal.db:
                    self.journal.db.execute("UPDATE commands SET state='completed' WHERE id=?", (row["id"],))
                return

    async def readiness(self):
        reports = {}
        for alias, item in self.workspaces.items():
            report = await self.gateway.readiness(item["path"], item["task_types"][0], item["isolation"][0])
            routing = (report.get("model_availability") or {}).get("routing") or {}
            reports[alias] = scrub({"ready": report.get("ready"), "ready_for": report.get("ready_for"),
                "capabilities": item["capabilities"], "observed_at": int(time.time()),
                "models": {key: {field: value for field, value in choice.items() if field in
                    ("available", "profile", "model", "offered_fallback", "argon_unavailable", "opus_available", "message")}
                    for key, choice in routing.items()}, "remediation": [str(value)[:240] for value in (report.get("remediation") or [])[:2]]},
                {str(item["path"]): "workspace://" + alias, str(Path.home()): "<worker-home>"})
        self.journal.queue("readiness." + secrets.token_hex(8), {"kind": "readiness", "readiness": reports,
            "transport_capabilities": {"artifact_stream_v1": True, "max_artifact_bytes": MAX_DOWNLOAD_BYTES,
                                       "artifact_chunk_bytes": ARTIFACT_CHUNK_BYTES, "computer_control_v1": True,
                                       "device_refresh_v1": True, "setup_control_v1": True}})

    def starting(self):
        self.journal.queue("readiness."+secrets.token_hex(8),{"kind":"readiness","readiness":{
            alias:{"ready":None,"status":"checking","observed_at":int(time.time())} for alias in self.workspaces}})

    async def close(self):
        # Provider shutdown can produce a final cancelled event. Preserve it in
        # the local outbox before stopping the monitors and closing the journal.
        if self.gateway is not None:
            await self.gateway.close()
        self.stopping.set()
        self.journal.changed.set()
        for task in self.commands.values():
            task.cancel()
        tasks = list(self.monitors.values())
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.commands:
            await asyncio.gather(*list(self.commands.values()), return_exceptions=True)
        self.journal.close()


class TransportError(RuntimeError):
    def __init__(self, status=0, code=None):
        self.status = status
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else None
        super().__init__("Relay transport unavailable" if status not in (401,403) else "Worker access expired or was revoked")


class HttpTransport:
    def __init__(self, origin, credential, *, client=None):
        import httpx
        self.origin = origin
        self.credential = credential
        self.client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=True,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))

    async def request(self, method, path, payload=None, *, credential=None):
        import httpx
        supplied = credential if credential is not None else self.credential
        # Initial pairing is public and has no worker credential yet. An empty
        # Bearer header is invalid on HTTP/1 and prevents the request reaching
        # the relay; claim and paired-worker requests still authenticate.
        headers = {"Authorization": "Bearer " + supplied} if supplied else {}
        try:
            async with self.client.stream(method, self.origin + path, headers=headers, json=payload) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 196_608:
                        raise TransportError()
                if response.status_code not in (200,201):
                    try:
                        failure = json.loads(content)
                    except (ValueError, UnicodeDecodeError):
                        failure = {}
                    raise TransportError(response.status_code, failure.get("error") if isinstance(failure, dict) else None)
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise TransportError()
                return data
        except (httpx.RequestError, json.JSONDecodeError, UnicodeDecodeError):
            raise TransportError() from None

    async def pull(self):
        return await self.request("POST", "/worker/commands")

    async def send(self, packet):
        reply = await self.request("POST", "/worker/message", packet)
        if reply.get("kind") != "ack" or reply.get("message_id") != packet["message_id"]:
            raise TransportError()
        return reply

    async def close(self):
        await self.client.aclose()


async def outbox_http(worker, transport):
    delay = 1
    while not worker.stopping.is_set():
        worker.journal.changed.clear()
        row = worker.journal.pending()
        if row is None:
            await worker.journal.changed.wait()
            continue
        try:
            reply = await transport.send(json.loads(row["payload"]))
            worker.acknowledge_packet(row["id"], reply.get("disposition"))
            delay = 1
        except TransportError as error:
            if error.status in (401,403):
                raise
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)
        except (OSError, TimeoutError):
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)


async def run_http(worker, credential, idle_timeout=600, *, progress=None, stopped=None):
    worker.external_stop = stopped
    transport = HttpTransport(worker.config["relay"], credential)
    sender = asyncio.create_task(outbox_http(worker, transport))
    async def maintain():
        while not worker.stopping.is_set():
            await worker.maintain_controls(progress)
            if worker.refresh_requested or (worker.approval_mode == "until_revoked" and worker.access_expires_at and time.time() * 1000 >= worker.access_expires_at - 3_600_000):
                raise TransportError(401, "credential_refresh_required")
            await asyncio.sleep(1)
    maintainer = asyncio.create_task(maintain())
    delay = 1
    try:
        worker.starting()
        await worker.readiness()
        while not worker.stopping.is_set():
            if stopped and stopped.is_set():
                if not worker.commands and not worker.monitors and worker.journal.pending() is None:
                    return
                if progress:
                    progress({"status": "draining", "active_tasks": len(worker.monitors) + len(worker.commands)})
                await asyncio.sleep(0.25)
                continue
            if sender.done():
                await sender
            if maintainer.done():
                await maintainer
            try:
                batch = await transport.pull()
                if isinstance(batch.get("admission_revision"), int) and type(batch.get("paused")) is bool:
                    worker.set_admission(batch["paused"], batch["admission_revision"])
                for control in batch.get("controls") or []:
                    await worker.computer_control(control)
                for command in batch.get("commands") or []:
                    key = safe_id(command["command_id"])
                    if key in worker.commands:
                        continue
                    task = asyncio.create_task(worker.command(command))
                    worker.commands[key] = task
                    def settled(done, command_id=key, job=command["delegation_id"]):
                        worker.commands.pop(command_id, None)
                        if not done.cancelled() and done.exception():
                            worker.recovery(job)
                    task.add_done_callback(settled)
                delay = 1
                if idle_timeout and not worker.commands and not worker.monitors and worker.journal.pending() is None and time.monotonic() - worker.last_work > idle_timeout:
                    return
            except TransportError as error:
                if error.status in (401,403):
                    raise
                if progress:
                    progress({"status": "reconnecting", "active_tasks": len(worker.monitors) + len(worker.commands)})
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
            except (OSError, TimeoutError):
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
    finally:
        sender.cancel()
        maintainer.cancel()
        await asyncio.gather(sender, maintainer, return_exceptions=True)
        await transport.close()


async def run_websocket(worker, credential, idle_timeout=600, *, progress=None, stopped=None):
    # Optional transport; HTTPS is sufficient in cloud environments that only
    # expose a configured HTTP proxy. No publisher-side incoming worker port.
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    origin = worker.config["relay"]
    worker.external_stop = stopped
    address = ("wss" if origin.startswith("https") else "ws") + origin[origin.index(":"): ] + "/worker/connect"
    try:
        async with connect(address, additional_headers={"Authorization": "Bearer " + credential}, max_size=MAX_FRAME,
                           ping_interval=20, ping_timeout=20, open_timeout=15, close_timeout=5) as socket:
            send_lock = asyncio.Lock()
            async def send_raw(value):
                if len(value.encode("utf-8")) > MAX_FRAME:
                    raise ValueError("Worker packet exceeds transport limit")
                async with send_lock:
                    await socket.send(value)
            async def send_packet(value):
                await send_raw(canonical(value))
            artifacts = ArtifactSender(worker.journal, send_packet)
            await worker.readiness()
            if progress:
                progress({"status": "connected", "transport": "websocket", "active_tasks": len(worker.monitors) + len(worker.commands)})
            async def send():
                while not worker.stopping.is_set():
                    worker.journal.changed.clear()
                    row = worker.journal.pending()
                    if row is None:
                        await worker.journal.changed.wait()
                        continue
                    await send_raw(row["payload"])
                    # Wait for durable ACK. Check before clearing/waiting so a
                    # receiver ACK between send and this loop can't be lost.
                    while True:
                        worker.journal.changed.clear()
                        pending = worker.journal.pending()
                        if pending is None or pending["id"] != row["id"]:
                            break
                        await worker.journal.changed.wait()

            async def receive():
                async for message in socket:
                    packet = json.loads(message)
                    if not isinstance(packet, dict):
                        raise TransportError()
                    if packet.get("kind") == "ack":
                        worker.acknowledge_packet(packet["message_id"], packet.get("disposition"))
                        worker.journal.changed.set()
                    elif packet.get("kind") == "computer_control":
                        await worker.computer_control(packet)
                    elif packet.get("kind") in ("connected", "worker_connected", "ready"):
                        if type(packet.get("paused")) is bool and type(packet.get("admission_revision")) is int:
                            worker.set_admission(packet["paused"], packet["admission_revision"])
                    elif packet.get("kind") in ("artifact_open", "artifact_credit", "artifact_abort"):
                        await artifacts.handle(packet)
                    elif packet.get("kind") == "command":
                        key = safe_id(packet["command_id"])
                        if key not in worker.commands:
                            task = asyncio.create_task(worker.command(packet))
                            worker.commands[key] = task
                            def settled(done, command_id=key, job=packet["delegation_id"]):
                                worker.commands.pop(command_id, None)
                                if not done.cancelled() and done.exception():
                                    worker.recovery(job)
                            task.add_done_callback(settled)
                    elif packet.get("kind") == "error":
                        raise TransportError()
                # Even a normal server close is transport loss, not task exit.
                raise TransportError()

            async def idle():
                while not worker.stopping.is_set():
                    await asyncio.sleep(1)
                    await worker.maintain_controls(progress)
                    if worker.refresh_requested or (worker.approval_mode == "until_revoked" and worker.access_expires_at and time.time() * 1000 >= worker.access_expires_at - 3_600_000):
                        raise TransportError(401, "credential_refresh_required")
                    if stopped and stopped.is_set():
                        if not worker.commands and not worker.monitors and worker.journal.pending() is None:
                            return
                        if progress:
                            progress({"status": "draining", "active_tasks": len(worker.monitors) + len(worker.commands)})
                    if idle_timeout and not worker.commands and not worker.monitors and worker.journal.pending() is None and time.monotonic() - worker.last_work > idle_timeout:
                        return

            async def expire_artifacts():
                while not worker.stopping.is_set():
                    await asyncio.sleep(1)
                    await artifacts.expire()

            tasks = [asyncio.create_task(send()), asyncio.create_task(receive()), asyncio.create_task(idle()), asyncio.create_task(expire_artifacts())]
            try:
                done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                artifacts.close()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except InvalidStatus as error:
        raise TransportError(error.response.status_code) from None
    except ConnectionClosed as error:
        code = error.rcvd.code if error.rcvd else 0
        raise TransportError(403 if code == 4003 else 409 if code == 4001 else 0) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise TransportError() from None


def workspace_arguments(values, shared):
    output = {}
    for value, isolation in [(item,"worktree") for item in values] + [(item,"shared") for item in shared]:
        alias, separator, supplied = value.partition("=")
        if not separator or alias in output:
            raise ValueError("Use unique WORKSPACE_ID=PATH aliases")
        safe_id(alias)
        path = Path(supplied).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("Workspace is not a directory")
        output[alias] = {"path": str(path), "label": alias, "capabilities": ["local_files","shell"],
                         "isolation": [isolation], "task_types": list(TASK_TYPES)}
    if not output or len(output) > 16:
        raise ValueError("Register between one and sixteen workspace aliases")
    return output


async def initialise(args, state, *, progress=None, stopped=None):
    """Pair through normal browser consent; the companion can display progress.

    A private pending record allows a closed setup window to resume the same
    approval. No credential or pairing claim is passed to the UI callback.
    """
    pending_path = Path(state) / "pairing.json"
    if (Path(state) / "worker.sqlite3").exists() or (not pending_path.exists() and
            any((Path(state) / name).exists() for name in ("worker.json", "auth.json"))):
        raise ValueError("Pairing state already exists. Use a new private state directory for a new pairing")
    relay = relay_origin(args.relay, args.local)
    setup_only = bool(getattr(args, "setup_only", False))
    workspaces = {} if setup_only and not args.workspace and not args.shared_workspace else workspace_arguments(args.workspace, args.shared_workspace)
    if args.codex_cloud and not args.cloud_task_id:
        raise ValueError("Codex Cloud workers require the current cloud task ID")
    if any(Path(state).resolve().is_relative_to(Path(item["path"])) for item in workspaces.values()):
        raise ValueError("Private worker state must be outside every delegated workspace")
    config = {"relay": relay, "workspaces": workspaces, "cloud_task_id": args.cloud_task_id if args.codex_cloud else None}
    if setup_only:
        config["setup_only"] = True
    pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else None
    setup_request_id = getattr(args, "setup_request_id", None)
    if setup_request_id:
        safe_id(setup_request_id)
    if pending and (pending["config"] != config or pending["label"] != args.label or pending.get("setup_request_id") != setup_request_id):
        raise ValueError("Pending pairing belongs to another workspace selection. Finish or cancel it before changing folders")
    transport = HttpTransport(relay, "")
    try:
        if not pending or time.time() * 1_000 >= pending["paired"]["expires"]:
            secret = secrets.token_hex(32)
            request = {"label": args.label, "credential_hash": hashlib.sha256(secret.encode()).hexdigest(),
                "workspaces": [{"id": alias, **{key:value for key,value in item.items() if key != "path"}} for alias,item in workspaces.items()]}
            if getattr(args, "approval_mode", "legacy_30_day") == "until_revoked":
                request["approval_mode"] = "until_revoked"
            if setup_only:
                request["setup_only"] = True
            if setup_request_id:
                request["setup_request_id"] = setup_request_id
            paired = await transport.request("POST", "/worker/pair", request)
            verification = urlsplit(paired["verification_url"])
            if verification.scheme + "://" + verification.netloc != relay or verification.path != "/pair" or verification.query != "code=" + paired["code"]:
                raise ValueError("Pairing returned an unexpected browser destination")
            pending = {"config": config, "label": args.label, "secret": secret, "paired": paired, "setup_request_id": setup_request_id}
            private_json(pending_path, pending)
        paired, secret = pending["paired"], pending["secret"]
        if progress:
            progress({"status": "approval_required", "verification_url": paired["verification_url"], "pairing_code": paired["code"]})
        else:
            print("Open this URL in your own browser to approve the worker:\n" + paired["verification_url"], flush=True)
        approved = None
        while time.time() * 1_000 < paired["expires"]:
            if stopped and stopped.is_set():
                raise ValueError("Setup stopped. The pending approval can be resumed without creating another worker")
            status = await transport.request("GET", "/worker/pair/" + paired["code"], credential=paired["claim"])
            if status.get("status") == "declined":
                pending_path.unlink()
                raise ValueError("Worker pairing was declined. No access was granted")
            if status.get("status") == "approved":
                approved = status
                break
            await asyncio.sleep(2)
        if approved is None:
            raise ValueError("Worker pairing expired without approval")
        credential = approved["owner_key"] + "." + approved["worker_id"] + "." + secret
        config["worker_identity"] = approved["owner_key"] + "." + approved["worker_id"]
        private_json(Path(state) / "worker.json", config)
        mode = approved.get("approval_mode", "legacy_30_day")
        private_json(Path(state) / "auth.json", {"credential": credential,
            **({"expires": approved["expires"]} if "expires" in approved else {}),
            **({"approval_mode": mode, "refresh_credential": credential, "access_expires_at": approved.get("access_expires_at")} if mode == "until_revoked" else {})})
        pending_path.unlink()
        if progress:
            progress({"status": "paired", "workspace_ids": list(workspaces)})
        else:
            print("Worker paired. Credentials were saved in private worker state.", flush=True)
    finally:
        await transport.close()


async def renew(args, state, *, progress=None, stopped=None):
    """Explicit same-worker browser approval; no journal/provider operation."""
    state = Path(state)
    with single_worker(state, "renewal.lock"):
        config = json.loads((state / "worker.json").read_text(encoding="utf-8"))
        relay = relay_origin(config["relay"], getattr(args, "local", False))
        try:
            auth = json.loads((state / "auth.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            raise ValueError("The old worker proof is missing. Approve a new connection for future tasks; old saved work stays archived") from None
        current = auth.get("credential")
        if not isinstance(current, str) or not TOKEN.fullmatch(current):
            raise ValueError("The old worker proof is missing. Approve a new connection for future tasks; old saved work stays archived")
        owner, worker_id, _old_secret = current.split(".")
        pending_path = state / "renewal.json"
        pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else None
        if pending and pending.get("old_credential") != current:
            old_identity = str(pending.get("old_credential", "")).split(".")[:2]
            if TOKEN.fullmatch(str(pending.get("old_credential", ""))) and current == ".".join([*old_identity, str(pending.get("secret", ""))]):
                # The approved credential was atomically saved before a crash,
                # but removing the private pending record did not finish.
                pending_path.unlink()
                if progress:
                    progress({"status": "renewed", "workspace_ids": list(config["workspaces"])})
                return
            raise ValueError("Worker credentials changed during renewal. Review the existing connection before retrying")
        transport = HttpTransport(relay, current)
        try:
            if not pending:
                pending = {"old_credential": current, "secret": secrets.token_hex(32), "receipt": None}
                # A lost begin response must retry the same proof/hash, rather
                # than orphaning a pending browser approval on the relay.
                private_json(pending_path, pending)
            recovered = None
            if pending.get("receipt") and time.time() * 1000 >= pending["receipt"]["expires"]:
                try:
                    recovered = await transport.request("POST", "/worker/renew/" + safe_id(pending["receipt"]["renewal_id"]))
                except TransportError as error:
                    if error.status not in (403,404,410):
                        raise
                if not recovered or recovered.get("status") != "approved":
                    pending["receipt"] = None
            if not pending.get("receipt"):
                receipt = await transport.request("POST", "/worker/renew", {"credential_hash": hashlib.sha256(pending["secret"].encode()).hexdigest()})
                renewal_id = safe_id(receipt["renewal_id"])
                verification = urlsplit(receipt["verification_url"])
                if verification.scheme + "://" + verification.netloc != relay or verification.path != "/renew" or verification.query != "code=" + renewal_id:
                    raise ValueError("Renewal returned an unexpected browser destination")
                pending["receipt"] = receipt
                private_json(pending_path, pending)
            receipt = pending["receipt"]
            if progress and not recovered:
                progress({"status": "renewal_approval_required", "verification_url": receipt["verification_url"]})
            elif not progress and not recovered:
                print("Approve renewal of this same worker in your own browser:\n" + receipt["verification_url"], flush=True)
            while True:
                if stopped and stopped.is_set():
                    raise ValueError("Renewal stopped. The same browser approval can be resumed")
                status = recovered or await transport.request("POST", "/worker/renew/" + safe_id(receipt["renewal_id"]))
                recovered = None
                if status.get("status") == "approved":
                    if status.get("owner_key") != owner or status.get("worker_id") != worker_id:
                        raise ValueError("Renewal attempted to change the saved worker identity")
                    if json.loads((state / "auth.json").read_text(encoding="utf-8")).get("credential") != current:
                        raise ValueError("Worker credentials changed before renewal completed")
                    private_json(state / "auth.json", {"credential": owner + "." + worker_id + "." + pending["secret"],
                                                        "expires": status.get("expires")})
                    pending_path.unlink()
                    if progress:
                        progress({"status": "renewed", "workspace_ids": list(config["workspaces"])})
                    return
                if status.get("status") in ("declined", "expired"):
                    pending_path.unlink()
                    raise ValueError("Worker renewal was not approved. Saved local work has not changed")
                if status.get("status") != "pending":
                    raise ValueError("Unexpected renewal response; keep the saved connection and retry")
                if time.time() * 1000 >= receipt["expires"]:
                    raise ValueError("Worker renewal expired without approval. Retry renewal for this same worker")
                await asyncio.sleep(2)
        finally:
            await transport.close()


async def rotate_device_credentials(state, config, *, transport=None, discover_approved_conversion=False):
    """Crash-safe client-generated access/refresh rotation for one approved device.

    A lost response retries the identical rotation. If its short replay window
    passed, the saved candidate refresh proof can establish a fresh rotation.
    Neither credential nor provider authentication is returned to UI callbacks.
    """
    state = Path(state)
    auth_path, pending_path = state / "auth.json", state / "credentials-rotation.json"
    with single_worker(state, "credential-rotation.lock"):
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        if (auth.get("approval_mode") != "until_revoked" and not discover_approved_conversion) or not TOKEN.fullmatch(auth.get("credential", "")):
            raise ValueError("This computer has no continuing approval for credential rotation")
        prefix = ".".join(auth["credential"].split(".")[:2])
        if config.get("worker_identity") != prefix:
            raise ValueError("Credential rotation cannot change computer ownership")
        proof = auth.get("refresh_credential", auth["credential"])
        if not isinstance(proof, str) or not TOKEN.fullmatch(proof) or ".".join(proof.split(".")[:2]) != prefix:
            raise ValueError("Saved device refresh proof is invalid")
        def proposal(credential):
            return {"worker_identity": prefix, "rotation_id": secrets.token_hex(16), "proof": credential,
                    "origin_hash": hashlib.sha256(auth["credential"].encode()).hexdigest(),
                    "access_secret": secrets.token_hex(32), "refresh_secret": secrets.token_hex(32)}
        pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else proposal(proof)
        if (pending.get("worker_identity") != prefix or not ID.fullmatch(pending.get("rotation_id", "")) or
            not TOKEN.fullmatch(pending.get("proof", "")) or any(not isinstance(pending.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", pending[key]) for key in ("access_secret", "refresh_secret"))):
            raise ValueError("Pending device rotation is invalid; saved work is preserved")
        if pending.get("origin_hash") != hashlib.sha256(auth["credential"].encode()).hexdigest():
            # Explicit renewal can replace the same device's proof. An older
            # failed proposal must not strand that freshly approved credential.
            pending = proposal(proof)
        private_json(pending_path, pending)
        own_transport = transport is None
        channel = transport or HttpTransport(config["relay"], "")
        try:
            for attempt in range(2):
                body = {"rotation_id": pending["rotation_id"],
                        "credential_hash": hashlib.sha256(pending["access_secret"].encode()).hexdigest(),
                        "refresh_hash": hashlib.sha256(pending["refresh_secret"].encode()).hexdigest()}
                try:
                    reply = await channel.request("POST", "/worker/credentials/rotate", body, credential=pending["proof"])
                    break
                except TransportError as error:
                    if (error.status not in (401,403) and error.code != "rotation_conflict") or attempt:
                        if attempt and error.status in (401,403):
                            pending_path.unlink(missing_ok=True)
                        raise
                    # The first proposal may have been committed before a crash.
                    # This proof has only device-refresh authority at the relay.
                    pending = proposal(prefix + "." + pending["refresh_secret"])
                    private_json(pending_path, pending)
            if (reply.get("rotation_id") != pending["rotation_id"] or not isinstance(reply.get("owner_key"), str) or not isinstance(reply.get("worker_id"), str) or reply["owner_key"] + "." + reply["worker_id"] != prefix or
                reply.get("approval_mode") != "until_revoked" or type(reply.get("expires_at")) is not int or
                not time.time() * 1000 < reply["expires_at"] <= time.time() * 1000 + 2 * 86_400_000):
                raise ValueError("Device rotation returned an unexpected identity or lifetime")
            current = json.loads(auth_path.read_text(encoding="utf-8"))
            if current["credential"] != auth["credential"]:
                raise ValueError("Computer credentials changed during rotation")
            result = {**auth, "credential": prefix + "." + pending["access_secret"],
                      "refresh_credential": prefix + "." + pending["refresh_secret"],
                      "access_expires_at": reply["expires_at"], "approval_mode": "until_revoked"}
            private_json(auth_path, result)
            pending_path.unlink()
            return result
        finally:
            if own_transport:
                await channel.close()


async def main_async(args, state, *, progress=None, stopped=None):
    if (Path(state) / "archive-intent.json").exists():
        raise ValueError("This connection is archived for local review. Approve a new connection for future tasks")
    if args.command == "init":
        return await initialise(args, state, progress=progress, stopped=stopped)
    if args.command == "renew":
        return await renew(args, state, progress=progress, stopped=stopped)
    config = json.loads((Path(state) / "worker.json").read_text(encoding="utf-8"))
    config["relay"] = relay_origin(config["relay"], args.local)
    if config.get("cloud_task_id") and config["cloud_task_id"] != args.cloud_task_id:
        raise ValueError("This pairing belongs to another Codex Cloud task. Create a new task-specific worker pairing")
    auth = {} if args.credential_env else json.loads((Path(state) / "auth.json").read_text(encoding="utf-8"))
    credential = os.environ.get(args.credential_env) if args.credential_env else auth["credential"]
    if not credential or not TOKEN.fullmatch(credential):
        raise ValueError("Valid worker credentials are missing")
    if config.get("worker_identity") and config["worker_identity"] != ".".join(credential.split(".")[:2]):
        raise ValueError("A different worker cannot resume this local journal")
    worker = Worker(config, state, controls=args)
    worker.approval_mode = auth.get("approval_mode", "legacy_30_day")
    worker.access_expires_at = auth.get("access_expires_at")
    try:
        worker.journal.bind_identity(credential)
        if args.command == "doctor":
            await worker.readiness()
            row = worker.journal.pending()
            print(canonical(json.loads(row["payload"])["readiness"]) if row else "{}")
            return
        worker.resume_saved()
        mode = args.transport or ("http" if config.get("cloud_task_id") else "websocket")
        delay = 1
        while True:
            try:
                if not args.credential_env and auth.get("approval_mode") == "until_revoked" and (worker.refresh_requested or (Path(state) / "credentials-rotation.json").exists() or
                    not worker.access_expires_at or time.time() * 1000 >= worker.access_expires_at - 3_600_000):
                    auth = await rotate_device_credentials(state, config)
                    credential = auth["credential"]
                    worker.access_expires_at = auth["access_expires_at"]
                    worker.approval_mode = auth["approval_mode"]
                    worker.refresh_requested = False
                if mode == "websocket":
                    await run_websocket(worker, credential, args.idle_timeout, progress=progress, stopped=stopped)
                else:
                    await run_http(worker, credential, args.idle_timeout, progress=progress, stopped=stopped)
                break
            except TransportError as error:
                if not args.credential_env and error.status == 401:
                    auth = json.loads((Path(state) / "auth.json").read_text(encoding="utf-8"))
                    # A conversion may have been approved while this computer
                    # was offline. Only the server can grant ongoing refresh;
                    # unconverted legacy proof is rejected without extending it.
                    if auth:
                        try:
                            auth = await rotate_device_credentials(state, config, discover_approved_conversion=True)
                            credential = auth["credential"]
                            worker.approval_mode, worker.access_expires_at = auth["approval_mode"], auth["access_expires_at"]
                            worker.refresh_requested = False
                            delay = 1
                            continue
                        except TransportError as rotation_error:
                            if rotation_error.status not in (401,403):
                                if progress:
                                    progress({"status": "reconnecting", "connected": False, **worker.computer_snapshot()})
                                await asyncio.sleep(delay)
                                delay = min(60, delay * 2)
                                continue
                if error.status in (401,403,409):
                    print("Relay access stopped. Finishing existing work locally before exiting; inspect results on this worker.", file=sys.stderr, flush=True)
                    # Revocation/replacement isn't authorization to cancel an
                    # existing provider task. Keep its journal and saved result.
                    if progress:
                        progress({"status": "access_expired", "active_tasks": len(worker.monitors) + len(worker.commands)})
                    rotated = False
                    while worker.commands or worker.monitors:
                        if not args.credential_env and not (stopped and stopped.is_set()):
                            saved = json.loads((Path(state) / "auth.json").read_text(encoding="utf-8")).get("credential")
                            if isinstance(saved, str) and TOKEN.fullmatch(saved) and saved != credential:
                                # Only explicit renewal writes a new proof. Provider handles,
                                # command identity and delivery cursors remain unchanged.
                                if saved.split(".")[:2] != credential.split(".")[:2]:
                                    raise ValueError("A different worker cannot resume this local journal")
                                credential, rotated = saved, True
                                break
                        await asyncio.sleep(.25)
                    if rotated:
                        delay = 1
                        continue
                    raise
                if stopped and stopped.is_set() and not worker.commands and not worker.monitors:
                    break
                if progress:
                    progress({"status": "reconnecting", "active_tasks": len(worker.monitors) + len(worker.commands)})
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
            except (OSError, TimeoutError):
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)
    finally:
        await worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "run", "doctor", "renew"))
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/astra-antigravity-relay")
    parser.add_argument("--relay")
    parser.add_argument("--workspace", action="append", default=[])
    parser.add_argument("--shared-workspace", action="append", default=[])
    parser.add_argument("--label", default="My Antigravity worker")
    parser.add_argument("--setup-request-id")
    parser.add_argument("--approval-mode", choices=("legacy_30_day", "until_revoked"), default="legacy_30_day")
    parser.add_argument("--setup-only", action="store_true", help="Enroll management only; approve accounts and a workspace in ChatGPT")
    parser.add_argument("--codex-cloud", action="store_true")
    parser.add_argument("--cloud-task-id")
    parser.add_argument("--transport", choices=("websocket", "http"))
    parser.add_argument("--idle-timeout", type=int, default=600)
    parser.add_argument("--credential-env")
    parser.add_argument("--local", action="store_true", help="Allow loopback HTTP for credential-free local transport tests")
    args = parser.parse_args(argv)
    if args.command == "init" and not args.relay:
        parser.error("init requires --relay")
    if not 0 <= args.idle_timeout <= 3_600:
        parser.error("idle timeout must be between 0 and 3600 seconds")
    try:
        state = private_directory(args.state)
        with single_worker(state, "renewal-cli.lock" if args.command == "renew" else "worker.lock"):
            asyncio.run(main_async(args, state))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Do not print requests, auth headers, private paths or provider traces.
        print("Worker stopped: " + (str(error) if isinstance(error, (ValueError, TransportError)) else type(error).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
