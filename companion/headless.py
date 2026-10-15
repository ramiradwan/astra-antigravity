"""Headless companion heartbeat and finite per-user CLI fallback commands.

There is no local network listener. Files stay inside the user's private state;
neither heartbeat nor command replies contain device/provider credentials.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from companion import integration

SAFE_FIELDS = ("status", "worker_running", "busy", "companion_version", "setup_request_id", "pairing_code",
    "startup_enabled", "automatic_updates_enabled", "update_status", "uninstall_status", "paused", "active_tasks", "message", "validation_pending")


def status(state):
    path = Path(state) / "headless-status.json"
    if not path.is_file():
        return with_removal_status(state, {"status": "not_running", "worker_running": False, "companion_version": integration.VERSION})
    if integration.is_link(path) or path.stat().st_size > 16384:
        raise ValueError("Computer status needs local review")
    value = json.loads(path.read_bytes())
    result = {key: value.get(key) for key in SAFE_FIELDS if key in value}
    result["observed_at"] = value.get("observed_at")
    if type(value.get("observed_at")) not in (int, float) or time.time() * 1000 - value["observed_at"] > 5000:
        result.update(status="unreachable", worker_running=False, message="The companion is not responding. Start it to reconnect.")
    return with_removal_status(state, result)


def with_removal_status(state, result):
    path = Path(state) / "removal-recovery.json"
    if path.is_file() and not integration.is_link(path) and path.stat().st_size <= 4096:
        recovery = json.loads(path.read_bytes())
        public = {key: recovery.get(key) for key in ("state", "summary", "retry_available", "expires_at", "helper_name")}
        if public.get("state") == "pending" and type(public.get("expires_at")) in (int, float) and public["expires_at"] <= time.time() * 1000:
            public.update(state="unconfirmed", retry_available=False, summary="Removal confirmation expired. Local removal was not confirmed by the service.")
        result["removal_receipt"] = public
    return result


def submit(state, action, parameters=None):
    if action not in ("quit", "uninstall", "accept_update", "attach_request") or not isinstance(parameters or {}, dict):
        raise ValueError("Unsupported local maintenance action")
    parameters = parameters or {}
    allowed = {"erase_history"} if action == "uninstall" else {"request_id"} if action == "attach_request" else set()
    if set(parameters) - allowed or ("erase_history" in parameters and type(parameters["erase_history"]) is not bool):
        raise ValueError("Invalid local maintenance parameters")
    if action == "attach_request" and (not isinstance(parameters.get("request_id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", parameters["request_id"])):
        raise ValueError("Invalid saved task reference")
    root = Path(state) / "local-commands"
    if integration.is_link(root):
        raise ValueError("Local command folder must not be a link")
    root.mkdir(mode=0o700, exist_ok=True)
    if len(list(root.iterdir())) >= 32:
        raise ValueError("Too many local commands are pending")
    identity = secrets.token_hex(16)
    integration.atomic_json(root / (identity + ".json"), {"action": action, "parameters": parameters,
        "operation_id": identity, "created_at": time.time() * 1000})
    return {"status": "pending", "operation_id": identity, "message": "Local command queued. Current work will finish first."}


def consume(controller):
    root = controller.state / "local-commands"
    if not root.exists():
        return
    if integration.is_link(root):
        raise ValueError("Local command folder must not be a link")
    for path in sorted(root.iterdir())[:32]:
        if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name) or integration.is_link(path) or path.stat().st_size > 4096:
            raise ValueError("Local command needs review")
        command = json.loads(path.read_bytes())
        try:
            if command.get("operation_id") != path.stem or time.time() * 1000 - command.get("created_at", 0) > 300000:
                raise ValueError("Local command expired; request it again")
            action, parameters = command.get("action"), command.get("parameters")
            if action == "quit" and parameters == {}:
                controller.request_quit()
            elif action == "uninstall" and isinstance(parameters, dict) and not set(parameters) - {"erase_history"}:
                controller.local_uninstall(approved=True, erase_history=parameters.get("erase_history", False))
            elif action == "accept_update" and parameters == {} and controller.validation_mode:
                controller.validation_mode = False
                if controller.installed:
                    integration.register_application(controller.installed)
                if controller.snapshot()["status"] == "paired":
                    controller.resume_saved()
            elif action == "attach_request" and isinstance(parameters, dict) and set(parameters) == {"request_id"}:
                controller.attach_request(parameters["request_id"])
            else:
                raise ValueError("Unsupported local command")
        except (ValueError, OSError, TypeError):
            controller.update({"message": "A local control could not complete. Review this computer in ChatGPT.", "error": True})
        finally:
            path.unlink()


def write_status(controller):
    value = controller.snapshot()
    public = {key: value.get(key) for key in SAFE_FIELDS if key in value}
    public.update(observed_at=time.time() * 1000, pid=os.getpid(), validation_pending=controller.validation_mode)
    integration.atomic_json(controller.state / "headless-status.json", public)


def wait_status(state, timeout=20):
    deadline = time.monotonic() + timeout
    value = {"status": "pending", "worker_running": False, "companion_version": integration.VERSION}
    while time.monotonic() < deadline:
        value = status(state)
        if value.get("pairing_code") or value.get("worker_running") or value.get("status") in ("connection_invalid", "connection_error"):
            return value
        time.sleep(.1)
    return value


def run(controller, *, validation=False, uninstall=False):
    stopped = threading.Event()
    controller.validation_mode = validation
    controller.start_lifecycle(stopped.set)
    if uninstall:
        controller.local_uninstall(approved=True)
    elif not validation:
        value = controller.snapshot()
        if value["status"] == "paired":
            controller.resume_saved()
        elif value["status"] in ("setup_required", "approval_required"):
            # Initial pairing is management-only. Provider availability and
            # workspace permission are handled later through ChatGPT.
            controller.task(controller.pair, message="Waiting for computer approval in ChatGPT…")
    try:
        while not stopped.is_set():
            write_status(controller)
            consume(controller)
            stopped.wait(.5)
    finally:
        controller.close()
        value = {"status": "not_running", "worker_running": False, "companion_version": integration.VERSION,
            "observed_at": time.time() * 1000}
        integration.atomic_json(controller.state / "headless-status.json", value)
    return 0
