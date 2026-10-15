"""Deterministic first-use and worker lifecycle; no model drives setup."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import queue
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from types import SimpleNamespace
import webbrowser
import urllib.error

from companion import providers
from companion import integration
from companion import maintenance, updates

ROOT = providers.ROOT
spec = importlib.util.spec_from_file_location("astra_relay_worker", ROOT / "relay/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
DEFAULT_RELAY = "https://astra-antigravity-relay.dipsys.workers.dev"


class Controller:
    def __init__(self, state, *, relay=DEFAULT_RELAY, suggested_workspace=None, request_id=None, local=False, browser=webbrowser.open,
                 provider_api=providers, worker_api=worker, portable=False):
        if type(portable) is not bool:
            raise ValueError("Choose an explicit portable application mode")
        self.portable = portable
        self.state = worker_api.private_directory(state)
        self.connection = worker_api.private_directory(self.state / "worker")
        self.private_bin = self.state / "bin"
        self.relay = worker_api.relay_origin(relay, local)
        self.local = local
        self.worker_api = worker_api
        self.providers = provider_api
        self.browser = browser
        self.lock = threading.RLock()
        self.operation = None
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.setup_stop = threading.Event()
        self.auth_processes = {}
        self.local_controls = queue.Queue(maxsize=32)
        self.shutdown = None
        self.quit_requested = False
        self.validation_mode = False
        self.lifecycle_closed = threading.Event()
        self.lifecycle_thread = None
        self.maintenance_started = False
        self.last_update_check = 0
        preference_path = self.state / "preferences.json"
        self.preferences = json.loads(preference_path.read_text(encoding="utf-8")) if preference_path.is_file() else {"automatic_updates_enabled": False}
        if type(self.preferences.get("automatic_updates_enabled")) is not bool:
            raise ValueError("Saved update preference needs review")
        control_path = self.state / "companion-controls.json"
        self.controls = json.loads(control_path.read_text(encoding="utf-8")) if control_path.is_file() else {}
        if not isinstance(self.controls, dict) or len(self.controls) > 128:
            raise ValueError("Saved computer controls need review")
        interrupted = False
        for item in self.controls.values():
            if item.get("state") == "pending" and item.get("action") in ("check_accounts", "install_provider", "sign_in", "configure_workspace"):
                item.update(state="failed", summary="Setup was interrupted. Check this computer in ChatGPT and request the action again; no task was restarted.")
                interrupted = True
        if interrupted:
            self.worker_api.private_json(control_path, self.controls)
        self.value = {"status": "setup_required", "accounts": {}, "workspace": None, "message": "Connect your accounts, then choose a workspace.",
                      "setup_inference_started": False}
        self.suggestion = str(Path(suggested_workspace).expanduser().resolve()) if suggested_workspace else ""
        saved_request = self.state / "setup-request.json"
        retained_request = json.loads(saved_request.read_text(encoding="utf-8")).get("request_id") if saved_request.exists() else None
        self.request_id = worker_api.safe_id(request_id or retained_request) if request_id or retained_request else None
        # A portable/source instance has its own connection state, but must not
        # inherit maintenance authority over this user's installed application.
        installed_runtime = not portable and getattr(sys, "frozen", False) and integration.owned_executable(sys.executable)
        self.installed = integration.launcher_executable() if installed_runtime else None
        self.value["startup_available"] = self.maintenance_available()
        self.value["startup_enabled"] = integration.startup_enabled(self.installed) if self.value["startup_available"] else False
        self.value.update({"automatic_updates_enabled": self.preferences["automatic_updates_enabled"] if self.maintenance_available() else False,
            "update_status": ("not_configured" if not updates.config() else "idle") if self.maintenance_available() else "not_installed", "uninstall_status": None})
        receipt = self.state / "maintenance-receipt.json"
        if self.maintenance_available() and receipt.is_file():
            previous_maintenance = json.loads(receipt.read_bytes())
            if previous_maintenance.get("outcome") == "rolled_back":
                self.value["update_status"] = "rolled_back"
        if (self.connection / "worker.json").exists() and (self.connection / "auth.json").exists():
            config = json.loads((self.connection / "worker.json").read_text(encoding="utf-8"))
            self.relay = worker_api.relay_origin(config["relay"], local)
            self.value["workspace"] = [{"id": alias, "path": item["path"], "isolation": item["isolation"][0]} for alias, item in config["workspaces"].items()]
            self.value["status"] = "paired"
        elif (self.connection / "pairing.json").exists():
            pending = json.loads((self.connection / "pairing.json").read_text(encoding="utf-8"))
            unbound_setup = pending.get("setup_request_id") is None and pending["config"].get("setup_only") is True
            if request_id and request_id != pending.get("setup_request_id") and not unbound_setup:
                raise ValueError("Finish the current browser approval before connecting another task")
            self.request_id = pending.get("setup_request_id") or (self.request_id if unbound_setup else None)
            config = pending["config"]
            self.value["workspace"] = [{"id": alias, "path": item["path"], "isolation": item["isolation"][0]} for alias, item in config["workspaces"].items()]
            self.value["status"] = "approval_required"
        elif any((self.connection / name).exists() for name in ("worker.json", "auth.json", "worker.sqlite3")):
            self.value.update({"status": "connection_invalid", "fresh_approval_required": True, "recovery_action": "fresh",
                "message": "Saved connection details are missing. Archive this connection and approve a new one for future tasks."})
        self.value["suggested_workspace"] = self.suggestion
        if self.request_id:
            worker_api.private_json(saved_request, {"request_id": self.request_id})

    def snapshot(self):
        with self.lock:
            return {**self.value, "busy": bool(self.operation and self.operation.is_alive()),
                    "worker_running": bool(self.worker_thread and self.worker_thread.is_alive()), "relay": self.relay,
                    "setup_request_id": self.request_id, "companion_version": integration.VERSION}

    def update(self, value):
        # Callback values contain status and public URLs only. Provider output,
        # credentials, pairing proofs and task reasoning never enter this state.
        with self.lock:
            self.value.update(value)

    def task(self, action, *, message="Preparing browser approval…"):
        with self.lock:
            if self.operation and self.operation.is_alive():
                raise ValueError("A setup step is already running. Wait for it to finish")
            def run():
                try:
                    action()
                except Exception as error:
                    message = str(error) if isinstance(error, ValueError) else "This step could not finish. Check your connection and retry."
                    self.update({"message": message[:240], "error": True})
            self.update({"error": False, "message": message})
            self.operation = threading.Thread(target=run, daemon=True, name="astra-setup")
            self.operation.start()

    def check_accounts(self):
        def check():
            self.probe_accounts()
        self.task(check, message="Checking provider sign-ins…")

    def publish_provider_paths(self):
        for name, variable in (("codex", "CODEX_PATH"), ("agy", "AGY_PATH"), ("git", "ASTRA_ANTIGRAVITY_GIT_PATH")):
            try:
                executable = self.providers.find_program(name, self.private_bin)
            except (OSError, ValueError):
                continue  # unavailable provider must not disable management
            if executable:
                os.environ[variable] = executable

    def probe_accounts(self):
        result = self.providers.accounts(self.private_bin)
        self.publish_provider_paths()
        self.update({"accounts": result, "message": "Existing sign-ins are reused. Model access and quota are checked when a task runs."})

    def resume_saved(self):
        def resume():
            try:
                self.probe_accounts()
            except Exception:
                self.update({"accounts": {name: {"installed": False, "status": "check_failed"} for name in ("codex", "agy", "git")},
                    "message": "Provider checks need attention. Management remains available in ChatGPT."})
            self.start()
        self.task(resume, message="Checking the saved provider sign-ins before reconnecting…")

    def install_provider(self, name):
        if name not in ("codex", "agy", "git"):
            raise ValueError("Unknown provider")
        def install():
            self.providers.install(name, self.private_bin)
            self.publish_provider_paths()
            self.update({"accounts": self.providers.accounts(self.private_bin), "message": "Installed. Connect this account if sign-in is needed."})
        self.task(install, message="Installing " + {"codex": "Codex", "agy": "Antigravity", "git": "Git"}[name] + "…")

    def sign_in(self, name):
        if name not in ("codex", "agy"):
            raise ValueError("Unknown account")
        previous = self.auth_processes.get(name)
        if previous and previous.poll() is None:
            self.update({"message": "Sign-in is already open. Finish it, then check the connection."})
            return
        self.auth_processes[name] = self.providers.sign_in(name, self.private_bin, str(self.state))
        self.update({"message": "Finish the provider's sign-in, then select Check connections. No task has been submitted."})

    def validate_workspace(self, path, isolation, alias="project"):
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Choose a workspace folder before connecting")
        supplied = Path(path).expanduser()
        resolved = supplied.resolve(strict=True)
        if not resolved.is_dir() or supplied.is_symlink():
            raise ValueError("Choose an existing folder without a symlink alias")
        if self.state.is_relative_to(resolved):
            raise ValueError("Choose a project folder that does not contain this application's private state")
        self.worker_api.safe_id(alias)
        if isolation not in ("worktree", "shared"):
            raise ValueError("Choose isolated Git work or explicitly approve direct folder changes")
        if isolation == "worktree":
            git = self.providers.find_program("git", self.private_bin)
            if not git:
                raise ValueError("Install Git before using an isolated workspace")
            for arguments, expected in ((["rev-parse", "--show-toplevel"], str(resolved)), (["rev-parse", "--verify", "HEAD"], None), (["status", "--porcelain"], "")):
                reply = self.providers.probe([git, "-C", str(resolved), *arguments])
                if reply.returncode != 0 or (expected is not None and os.path.normcase(reply.stdout.strip()) != os.path.normcase(expected)):
                    raise ValueError("Choose a clean Git repository root with a committed HEAD, or explicitly approve direct folder changes")
        return {"id": alias, "path": str(resolved), "isolation": isolation}

    def approve_workspace(self, path, isolation, *, approved, alias="project", startup_enabled=None, automatic_updates_enabled=None):
        if approved is not True:
            raise ValueError("Workspace approval is required before connecting")
        if any((self.connection / name).exists() for name in ("worker.json", "auth.json", "worker.sqlite3")):
            raise ValueError("This connection already has saved connection details. Renew it or explicitly archive it before approving future work")
        workspace = self.validate_workspace(path, isolation, alias)
        observed = self.snapshot()["accounts"]
        if observed.get("codex", {}).get("status") != "connected" or observed.get("agy", {}).get("status") != "available":
            raise ValueError("Connect Codex with ChatGPT and check Antigravity before approving the workspace")
        if startup_enabled is not None:
            if type(startup_enabled) is not bool:
                raise ValueError("Choose a Windows startup preference")
            if self.value["startup_available"]:
                self.startup(startup_enabled)
        if automatic_updates_enabled is not None:
            self.automatic_updates(automatic_updates_enabled)
        with self.lock:
            if self.operation and self.operation.is_alive():
                raise ValueError("A setup step is already running. Wait for it to finish")
            self.update({"workspace": [workspace], "status": "pairing"})
            self.task(self.pair)

    def arguments(self, command):
        values = self.snapshot()["workspace"] or []
        setup_request_id = self.request_id
        pending_path = self.connection / "pairing.json"
        if command == "init" and pending_path.is_file():
            pending = json.loads(pending_path.read_bytes())
            if pending.get("setup_request_id") is None and pending["config"].get("setup_only") is True:
                setup_request_id = None  # owner approval binds the public code server-side
        return SimpleNamespace(command=command, relay=self.relay, local=self.local, label="My Antigravity computer",
            workspace=[item["id"] + "=" + item["path"] for item in values if item["isolation"] == "worktree"],
            shared_workspace=[item["id"] + "=" + item["path"] for item in values if item["isolation"] == "shared"],
            codex_cloud=False, cloud_task_id=None, transport="websocket", idle_timeout=0, credential_env=None,
            setup_request_id=setup_request_id, approval_mode="until_revoked", setup_only=not bool(values), open_browser=False,
            control_handler=self.control_handler, control_result=self.control_result, control_expired=self.control_expired,
            control_snapshot=self.control_snapshot, control_committed=self.control_committed,
            local_controls=self.local_controls)

    def pair(self):
        self.setup_stop.clear()
        def progress(value):
            self.update(value)
            if value["status"] == "approval_required":
                from urllib.parse import urlsplit, parse_qs
                code = value.get("pairing_code") or parse_qs(urlsplit(value.get("verification_url", "")).query).get("code", [None])[0]
                self.update({"pairing_code": code, "message": "Approve this computer in ChatGPT. Provider sign-ins and workspace permission come next."})
        try:
            with self.worker_api.single_worker(self.connection):
                asyncio.run(self.worker_api.initialise(self.arguments("init"), self.connection, progress=progress, stopped=self.setup_stop))
        except Exception:
            if not (self.connection / "pairing.json").exists() and not (self.connection / "auth.json").exists():
                self.update({"status": "setup_required", "verification_url": None})
            raise
        self.start()

    def start(self):
        with self.lock:
            if self.validation_mode or self.quit_requested or self.maintenance_started:
                raise ValueError("This application is finishing maintenance")
            if self.worker_thread and self.worker_thread.is_alive():
                return
            if not (self.connection / "worker.json").exists() or not (self.connection / "auth.json").exists():
                if any((self.connection / name).exists() for name in ("worker.json", "auth.json", "worker.sqlite3")):
                    self.update({"status": "connection_invalid", "fresh_approval_required": True, "recovery_action": "fresh",
                                 "message": "Saved connection details are missing. Keep existing work archived and approve a new connection for future tasks."})
                raise ValueError("Connect accounts and approve a workspace first")
            self.publish_provider_paths()
            self.stop_event.clear()
            self.update({"status": "connecting", "message": "Connecting this computer…", "error": False,
                         "recovery_action": None, "fresh_approval_required": False})
            def run():
                try:
                    with self.worker_api.single_worker(self.connection):
                        asyncio.run(self.worker_api.main_async(self.arguments("run"), self.connection, progress=self.connection_progress, stopped=self.stop_event))
                    self.update({"status": "stopped", "message": "Disconnected. Saved tasks and credentials remain on this computer."})
                except Exception as error:
                    self.update(self.connection_failure(error))
            self.worker_thread = threading.Thread(target=run, daemon=True, name="astra-worker")
            self.worker_thread.start()

    def connection_failure(self, error=None):
        code = getattr(error, "code", None)
        saved_state = any((self.connection / name).exists() for name in ("worker.json", "auth.json", "worker.sqlite3"))
        missing_proof = saved_state and not all((self.connection / name).exists() for name in ("worker.json", "auth.json"))
        if self.snapshot().get("fresh_approval_required") or missing_proof or code in ("worker_revoked", "worker_missing", "worker_not_found"):
            return {"status": "connection_invalid", "fresh_approval_required": True, "recovery_action": "fresh",
                    "message": "This connection is no longer authorized. Finish existing local work before choosing a new connection for future tasks."}
        access_stopped = error is None or (isinstance(error, self.worker_api.TransportError) and error.status in (401,403,409))
        expired = False
        if access_stopped:
            try:
                expiry = json.loads((self.connection / "auth.json").read_text(encoding="utf-8")).get("expires")
                expired = type(expiry) in (int, float) and expiry <= time.time() * 1000
            except (OSError, ValueError, TypeError):
                pass
        if code == "worker_expired" or expired:
            return {"status": "access_expired", "fresh_approval_required": False, "recovery_action": "renew",
                    "message": "This connection expired. Renew this connection within 72 hours of expiry to preserve its saved tasks and workspace approval."}
        return {"status": "connection_error", "fresh_approval_required": False, "recovery_action": "reconnect",
                "message": "Connection interrupted. Check your internet connection, then select Reconnect. Saved work stays on this computer."}

    def connection_progress(self, value):
        # Worker status contains the public account DTO; retain the local probe
        # representation separately so it cannot oscillate ready/unknown.
        value = {key: item for key, item in value.items() if key != "accounts"}
        # Older workers use this label for expiry, revocation and replacement.
        # Only a specific error or our saved expiry is evidence for renewal.
        if value.get("status") == "access_expired":
            recovery = self.connection_failure()
            if recovery["recovery_action"] == "reconnect" and value.get("active_tasks", 0):
                recovery["message"] = "Connection stopped. Existing work continues on this computer. Reconnect after current work finishes."
            self.update({**value, **recovery})
            return
        ready = "Connected. Your saved task continues in its original conversation; you do not need to submit it again." if self.request_id else "Connected. Return to ChatGPT or Codex to delegate a task."
        messages = {"connected": ready, "reconnecting": "Reconnecting. Existing work stays on this computer.",
                    "draining": "Finishing current work before disconnecting…"}
        if value.get("paused"):
            messages["connected"] = "New tasks are paused. This computer stays reachable from ChatGPT."
        self.update({**value, "message": messages.get(value.get("status"), "Connecting…"),
                     "recovery_action": None, "fresh_approval_required": False})

    def renew_connection(self):
        if not (self.connection / "worker.json").exists() or not (self.connection / "auth.json").exists():
            self.update({"status": "connection_invalid", "fresh_approval_required": True, "recovery_action": "fresh",
                         "message": "Saved connection details are missing. Archive this connection and approve a new one for future tasks."})
            raise ValueError("Saved connection details are missing. Archive this connection and approve a new one for future tasks")
        def renew():
            self.setup_stop.clear()
            def progress(value):
                self.update(value)
                if value["status"] == "renewal_approval_required":
                    self.update({"message": "Approve renewal of this same computer in your browser. Saved tasks and workspace approval stay unchanged."})
            try:
                asyncio.run(self.worker_api.renew(self.arguments("renew"), self.connection, progress=progress, stopped=self.setup_stop))
            except self.worker_api.TransportError as error:
                if error.code == "renewal_denied" or error.status in (401,403,404):
                    self.update({"status": "connection_invalid", "fresh_approval_required": True, "recovery_action": "fresh", "verification_url": None})
                    raise ValueError("This connection cannot be renewed. Keep its saved work archived and approve a new connection for future tasks") from None
                if error.code == "renewal_expired":
                    self.update({"status": "access_expired", "fresh_approval_required": False, "recovery_action": "renew", "verification_url": None})
                    raise ValueError("Browser renewal approval expired. Select Renew this connection to retry for the same connection") from None
                raise
            self.update({"fresh_approval_required": False, "verification_url": None})
            self.start()
        self.task(renew, message="Preparing renewal approval…")

    def fresh_connection(self, *, approved):
        if approved is not True:
            raise ValueError("Confirm that a new connection is for future tasks only and old saved work will remain archived")
        with self.lock:
            if self.worker_thread and self.worker_thread.is_alive():
                raise ValueError("Finish existing local tasks and disconnect before archiving this connection")
            if self.operation and self.operation.is_alive():
                raise ValueError("Finish the current browser approval before archiving this connection")
            archive_root = self.worker_api.private_directory(self.state / "worker-archive")
            archive = archive_root / (str(time.time_ns()) + "-" + secrets.token_hex(4))
            # Resolve both ends before moving a private directory on Windows.
            if self.connection.resolve().parent != self.state or not archive.resolve().is_relative_to(archive_root):
                raise ValueError("The saved connection is outside this application's private state")
            if any(self.connection.iterdir()):
                with self.worker_api.single_worker(self.connection):
                    # Release the Windows lock handle before moving its parent,
                    # while preventing another daemon from starting in the gap.
                    self.worker_api.private_json(self.connection / "archive-intent.json", {"future_tasks_only": True})
                os.replace(self.connection, archive)
                self.worker_api.private_json(archive / "archive.json", {"reason": "fresh_approval_for_future_tasks", "setup_request_id": self.request_id})
            self.connection = self.worker_api.private_directory(self.state / "worker")
            self.request_id = None
            saved_request = self.state / "setup-request.json"
            if saved_request.exists():
                saved_request.unlink()
            self.update({"status": "setup_required", "workspace": None, "verification_url": None, "fresh_approval_required": False, "recovery_action": None,
                "message": "Previous work stays archived on this computer. Choose and approve a workspace for future tasks. Old tasks will not move to this connection or restart."})

    def prepare_reinstall(self, *, new_request_id=None, explicit_recovery=False):
        """Consume a verified local removal receipt once; never guess corruption.

        The caller is the explicit local --install path under the app singleton.
        New request intent is attached only after the prior identity is archived.
        """
        if new_request_id is not None:
            self.worker_api.safe_id(new_request_id)
        value = self.snapshot()
        if value["worker_running"] or value["busy"]:
            raise ValueError("Quit the current companion before creating a new connection")
        if not explicit_recovery:
            if value["status"] != "connection_invalid" or (self.connection / "auth.json").exists():
                return False
            receipt_path = self.state / "maintenance-receipt.json"
            request_path = self.state / "maintenance.json"
            if any(not path.is_file() or integration.is_link(path) or path.stat().st_size > 16384 for path in (receipt_path, request_path)):
                return False
            try:
                receipt = json.loads(receipt_path.read_bytes())
                request = json.loads(request_path.read_bytes())
            except (OSError, ValueError):
                return False
            identifier = receipt.get("operation_id")
            recorded = self.controls.get(identifier, {}) if isinstance(identifier, str) else {}
            if (receipt.get("state") != "succeeded" or receipt.get("outcome") != "removed" or receipt.get("reinstall_consumed")
                    or request.get("state") != "succeeded" or request.get("action") != "uninstall" or request.get("operation_id") != identifier
                    or recorded.get("action") != "uninstall" or recorded.get("state") != "succeeded"):
                return False
            # Mark first so a crash cannot reuse old removal authority to
            # archive a later damaged connection. History is never discarded.
            receipt["reinstall_consumed"] = True
            self.worker_api.private_json(receipt_path, receipt)
        self.fresh_connection(approved=True)
        if new_request_id is not None:
            self.attach_request(new_request_id)
        return True

    def stop(self):
        self.stop_event.set()
        self.update({"status": "draining", "message": "Finishing current work before disconnecting…"})

    def maintenance_available(self):
        return bool(not self.portable and self.installed and self.installed.is_file())

    def startup(self, enabled):
        if type(enabled) is not bool or not self.maintenance_available():
            raise ValueError("Automatic startup is available in the installed Antigravity setup application")
        integration.set_startup(self.installed, enabled)
        self.update({"startup_enabled": enabled})

    def automatic_updates(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("Choose an automatic update preference")
        if not self.maintenance_available():
            raise ValueError("Automatic updates require the installed application")
        with self.lock:
            self.preferences["automatic_updates_enabled"] = enabled
            self.worker_api.private_json(self.state / "preferences.json", self.preferences)
            self.update({"automatic_updates_enabled": enabled})

    def control_snapshot(self):
        value = self.snapshot()
        result = {key: value.get(key) for key in ("companion_version", "startup_enabled",
            "automatic_updates_enabled", "update_status", "uninstall_status")}
        result["installed"] = self.maintenance_available()
        if not result["installed"]:
            result.update(startup_enabled=False, automatic_updates_enabled=False, update_status="not_installed", uninstall_status=None)
        statuses = {"connected": "ready", "available": "ready", "missing": "missing", "sign_in_required": "sign_in_required"}
        result["accounts"] = {name: {"installed": bool(item.get("installed")), "status": statuses.get(item.get("status"), "unknown"),
            "summary": {"connected": "Provider sign-in is available.", "available": "Tool responded; model access and quota are checked when a task runs.",
                "missing": "Provider tool is not installed.", "sign_in_required": "Complete this provider's normal sign-in on the computer."}.get(item.get("status"), "Check this provider on the computer.")}
            for name, item in value.get("accounts", {}).items() if name in ("codex", "agy", "git")}
        result["setup_only"] = not bool(value.get("workspace"))
        return result

    def control_result(self, operation_id):
        with self.lock:
            item = self.controls.get(operation_id)
            if not item:
                return None
            return {"state": item["state"], "summary": item["summary"],
                **({"workspace_approved": item["workspace_approved"]} if item.get("workspace_approved") else {})}

    def setup_control(self, action, parameters, operation_id):
        if self.operation and self.operation.is_alive():
            raise ValueError("A setup action is already running")
        if action == "check_accounts" and parameters:
            raise ValueError("Account detection does not accept parameters")
        if action in ("install_provider", "sign_in") and parameters.get("provider") not in (("codex", "agy", "git") if action == "install_provider" else ("codex", "agy")):
            raise ValueError("Unknown provider")
        self.controls[operation_id] = {"action": action, "parameters": parameters, "state": "pending", "summary": "Computer setup action received.", "committed": False}
        self.worker_api.private_json(self.state / "companion-controls.json", self.controls)
        def perform():
            try:
                approved = None
                if action == "configure_workspace":
                    workspace = self.validate_workspace(parameters.get("path", ""), parameters.get("isolation"), parameters.get("alias", "project"))
                    path = self.connection / "worker.json"
                    config = json.loads(path.read_bytes())
                    existing = config["workspaces"].get(workspace["id"])
                    if existing and (existing["path"] != workspace["path"] or existing["isolation"] != [workspace["isolation"]]):
                        raise ValueError("Use a new alias for a different workspace; saved tasks keep their original workspace")
                    if not existing and len(config["workspaces"]) >= 16:
                        raise ValueError("This computer already has sixteen approved workspaces")
                    approved = {"id": workspace["id"], "label": workspace["id"], "capabilities": ["local_files", "shell"],
                        "isolation": [workspace["isolation"]], "task_types": list(worker.TASK_TYPES)}
                    config["workspaces"][workspace["id"]] = {key: value for key, value in approved.items() if key != "id"}
                    config["workspaces"][workspace["id"]]["path"] = workspace["path"]
                    config["setup_only"] = False
                    self.worker_api.private_json(path, config)
                    self.update({"workspace": [{"id": alias, "path": item["path"], "isolation": item["isolation"][0]} for alias, item in config["workspaces"].items()]})
                    summary = "Workspace approved on this computer."
                elif action == "sign_in":
                    self.sign_in(parameters["provider"])
                    summary = "Provider sign-in opened on this computer. Complete it there, then check accounts in ChatGPT."
                else:
                    if action == "install_provider":
                        self.providers.install(parameters["provider"], self.private_bin)
                        self.publish_provider_paths()
                    self.probe_accounts()
                    summary = "Provider tools checked. Model access and quota are checked when a task runs."
                with self.lock:
                    self.controls[operation_id].update(state="succeeded", summary=summary)
                    if approved:
                        self.controls[operation_id]["workspace_approved"] = approved
            except Exception:
                with self.lock:
                    self.controls[operation_id].update(state="failed", summary="Setup could not complete. Check the provider sign-in or workspace on this computer; no task was restarted.")
            finally:
                with self.lock:
                    self.worker_api.private_json(self.state / "companion-controls.json", self.controls)
        self.task(perform, message="Completing the setup action requested in ChatGPT…")
        return {"state": "pending", "summary": "Computer setup action received.", "snapshot": self.control_snapshot()}

    def control_handler(self, action, parameters, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", operation_id) or not isinstance(parameters, dict):
            raise ValueError("Invalid computer operation")
        allowed = {"set_startup": {"enabled"}, "set_automatic_updates": {"enabled"}, "uninstall": {"erase_history", "completion"},
            "check_accounts": set(), "install_provider": {"provider"}, "sign_in": {"provider"}, "configure_workspace": {"path", "alias", "isolation"}}
        if action not in allowed or set(parameters) - allowed[action]:
            raise ValueError("Unsupported computer setting")
        if action in ("set_startup", "set_automatic_updates", "uninstall") and not self.maintenance_available():
            raise ValueError("Application maintenance requires the installed application")
        with self.lock:
            previous = self.controls.get(operation_id)
            if previous:
                if previous["action"] != action or previous["parameters"] != parameters:
                    raise ValueError("Computer operation identity changed")
                return {"state": previous["state"], "summary": previous["summary"], "snapshot": self.control_snapshot(),
                    **({"workspace_approved": previous["workspace_approved"]} if previous.get("workspace_approved") else {})}
            if len(self.controls) >= 128:
                settled = next((key for key, item in self.controls.items() if item["state"] != "pending"), None)
                if settled is None:
                    raise ValueError("Too many pending computer operations")
                del self.controls[settled]
            if action in ("check_accounts", "install_provider", "sign_in", "configure_workspace"):
                return self.setup_control(action, parameters, operation_id)
            if action == "set_startup":
                self.startup(parameters.get("enabled"))
                state, summary = "succeeded", "Windows startup preference saved."
            elif action == "set_automatic_updates":
                self.automatic_updates(parameters.get("enabled"))
                state, summary = "succeeded", "Automatic update preference saved."
            else:
                if type(parameters.get("erase_history", False)) is not bool or not self.maintenance_available():
                    raise ValueError("Removal requires an installed application and a history preference")
                if any(item["action"] == "uninstall" and item["state"] == "pending" for item in self.controls.values()):
                    raise ValueError("Removal is already pending")
                completion = parameters.get("completion")
                if completion is not None:
                    maintenance.validate_completion(completion, self.relay)
                state, summary = "pending", "Removal received. Current tasks and delivery will finish before removal."
                self.update({"uninstall_status": "waiting_for_receipt"})
            self.controls[operation_id] = {"action": action, "parameters": parameters, "state": state,
                "summary": summary, "committed": False}
            self.worker_api.private_json(self.state / "companion-controls.json", self.controls)
            if action == "uninstall" and self.snapshot()["worker_running"]:
                try:
                    self.local_control("pause")
                except ValueError:
                    pass  # remote admission is already paused; quit fences locally
            return {"state": state, "summary": summary, "snapshot": self.control_snapshot()}

    def control_committed(self, operation_id):
        with self.lock:
            item = self.controls.get(operation_id)
            if item and item["action"] == "uninstall" and item["state"] == "pending":
                item["committed"] = True
                self.worker_api.private_json(self.state / "companion-controls.json", self.controls)
                self.update({"uninstall_status": "waiting_for_idle"})

    def control_expired(self, operation_id):
        with self.lock:
            item = self.controls.get(operation_id)
            if item and item["action"] == "uninstall" and item["state"] == "pending":
                item.update(state="unconfirmed", committed=False, summary="Removal approval expired before completion was confirmed.")
                item["parameters"].pop("completion", None)
                self.worker_api.private_json(self.state / "companion-controls.json", self.controls)
                self.update({"uninstall_status": "unconfirmed"})

    def local_control(self, action):
        if action not in ("pause", "resume", "stop_tasks"):
            raise ValueError("Unsupported local computer control")
        if not self.snapshot()["worker_running"]:
            raise ValueError("Connect this computer before changing task admission")
        if action == "resume" and any(item["action"] == "uninstall" and item["state"] == "pending" for item in self.controls.values()):
            raise ValueError("Removal is pending; new tasks cannot resume")
        try:
            self.local_controls.put_nowait({"action": action, "parameters": {}, "operation_id": "local." + secrets.token_hex(12)})
        except queue.Full:
            raise ValueError("A computer control is already pending") from None

    def local_uninstall(self, *, approved, erase_history=False):
        if approved is not True:
            raise ValueError("Confirm removal of this computer application")
        operation_id = "local." + secrets.token_hex(12)
        result = self.control_handler("uninstall", {"erase_history": erase_history}, operation_id)
        self.control_committed(operation_id)
        return result

    def request_quit(self):
        self.quit_requested = True
        self.setup_stop.set()
        self.stop()
        # Keep headless local commands and status available while draining.
        def finish():
            if self.operation:
                self.operation.join()
            if self.worker_thread:
                self.worker_thread.join()
            if self.shutdown:
                self.shutdown()
        threading.Thread(target=finish, daemon=True, name="companion-quit").start()

    def start_lifecycle(self, shutdown):
        self.shutdown = shutdown
        if self.lifecycle_thread:
            return
        def maintain():
            while not self.lifecycle_closed.wait(1):
                try:
                    self.maintenance_tick()
                except urllib.error.HTTPError as error:
                    self.update({"update_status": "unavailable" if error.code == 404 else "download_failed"})
                except Exception:
                    self.update({"update_status": "needs_attention", "message": "Computer maintenance needs attention. Existing tasks were not restarted."})
        self.lifecycle_thread = threading.Thread(target=maintain, daemon=True, name="companion-maintenance")
        self.lifecycle_thread.start()

    def maintenance_tick(self):
        if not self.maintenance_available() or self.validation_mode or self.maintenance_started or self.quit_requested:
            return
        pending = next(((key, item) for key, item in self.controls.items()
            if item["action"] == "uninstall" and item["state"] == "pending" and item["committed"]), None)
        trusted = updates.config()
        if not pending and self.preferences["automatic_updates_enabled"] and trusted and (not self.last_update_check or time.monotonic() - self.last_update_check > 21600):
            self.last_update_check = time.monotonic()
            pointer = integration.install_root() / "pending-update.json"
            if not pointer.exists():
                self.update({"update_status": "checking"})
                current = json.loads((integration.install_root() / "current.json").read_bytes())
                failed_path = integration.install_root() / "failed-update.json"
                failed = json.loads(failed_path.read_bytes()) if failed_path.is_file() else {}
                envelope = updates.fetch_feed(trusted)
                try:
                    updates.stage(envelope, trusted, integration.install_root(), minimum_sequence=max(current.get("sequence", 0), failed.get("sequence", 0)))
                except ValueError as error:
                    if str(error) in ("Release is stale or already installed", "Automatic updates cannot downgrade the application"):
                        self.update({"update_status": "current"})
                        return
                    raise
            self.update({"update_status": "waiting_for_idle"})
        wants_update = bool(trusted and self.preferences["automatic_updates_enabled"] and (integration.install_root() / "pending-update.json").is_file())
        if not pending and not wants_update:
            return
        value = self.snapshot()
        if value["busy"] or any(process.poll() is None for process in self.auth_processes.values()):
            return
        if value["worker_running"] and (type(value.get("active_tasks")) is not int or value["active_tasks"] != 0 or value.get("pending_delivery") is not False):
            return
        action = "uninstall" if pending else "update"
        operation_id, item = pending if pending else (None, {"parameters": {}})
        self.stop_event.set()  # atomically fence intake before the helper starts
        maintenance.prepare(self.state, action, operation_id=operation_id,
            erase_history=item["parameters"].get("erase_history", False), completion=item["parameters"].get("completion"))
        self.maintenance_started = True
        self.update({"uninstall_status": "removing" if pending else None, "update_status": "installing" if not pending else value.get("update_status")})
        self.request_quit()

    def attach_request(self, request_id):
        identifier = self.worker_api.safe_id(request_id)
        with self.lock:
            if self.request_id and self.request_id != identifier:
                if self.value["status"] in ("pairing", "approval_required") or (self.connection / "pairing.json").exists():
                    raise ValueError("Finish the current browser approval before connecting another task")
                raise ValueError("This setup already retains another task reference; keep its original receipt")
            self.request_id = identifier
            self.worker_api.private_json(self.state / "setup-request.json", {"request_id": identifier})
            if (self.connection / "worker.json").exists() and (self.connection / "auth.json").exists():
                self.start()

    def close(self):
        self.lifecycle_closed.set()
        self.setup_stop.set()
        self.stop_event.set()
        # Closing setup is distinct from cancellation. Retained worker handles
        # finish their current tasks; process shutdown waits for their thread.
        if self.worker_thread:
            self.worker_thread.join()
