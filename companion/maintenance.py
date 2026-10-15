"""Per-user update/removal helper; no service, elevation or arbitrary commands."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

from companion import integration, updates


def validate_completion(value, relay):
    if not isinstance(value, dict) or set(value) != {"url", "token", "expires_at"}:
        raise ValueError("Invalid removal completion capability")
    url = urlsplit(value["url"])
    prefix = relay.rstrip("/") + "/worker/removals/"
    if not isinstance(value["url"], str) or not value["url"].startswith(prefix) or url.query or url.fragment or url.username or url.password:
        raise ValueError("Removal completion must use this connection's service")
    suffix = value["url"][len(prefix):].split("/")
    if len(suffix) != 3 or suffix[-1] != "complete" or not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", part) for part in suffix[:2]):
        raise ValueError("Invalid removal completion path")
    if not isinstance(value["token"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{16,512}", value["token"]):
        raise ValueError("Invalid removal completion capability")
    if type(value["expires_at"]) not in (int, float) or value["expires_at"] <= time.time() * 1000:
        raise ValueError("Removal approval expired; request removal again")


def complete(value, relay, state, summary, *, timeout=15):
    validate_completion(value, relay)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            raise ValueError("Removal completion must not redirect")
    request = urllib.request.Request(value["url"], method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"token": value["token"], "state": state, "summary": summary}).encode())
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise ValueError("Removal receipt has not been acknowledged")
        response.read(4096)


def complete_with_retry(value, relay, state, summary, *, maximum_seconds=300):
    import urllib.error
    deadline = time.monotonic() + maximum_seconds
    for delay in (0, 1, 2, 4, 8, 16, 30, 60, 60, 60, 60):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if delay:
            time.sleep(min(delay, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            complete(value, relay, state, summary, timeout=min(15, remaining))
            return True
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        except ValueError:
            break  # expired/invalid capability cannot recover through retries
    return False


def prepare(state, action, *, operation_id=None, erase_history=False, completion=None):
    if action not in ("update", "uninstall") or type(erase_history) is not bool:
        raise ValueError("Invalid maintenance request")
    state = Path(state).resolve()
    nonce = secrets.token_urlsafe(32)
    helper = state / ("maintenance-helper-" + secrets.token_hex(8) + ".exe")
    source = Path(sys.executable).resolve()
    if not getattr(sys, "frozen", False) or not integration.owned_executable(source):
        raise ValueError("Maintenance requires the installed application")
    if helper.is_symlink():
        raise ValueError("Maintenance helper must not be a link")
    shutil.copyfile(source, helper)
    if hashlib.sha256(helper.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
        raise ValueError("Maintenance helper verification failed")
    integration.atomic_json(state / "maintenance.json", {"schema": 1, "action": action,
        "nonce": nonce, "parent_pid": os.getpid(), "operation_id": operation_id,
        "erase_history": erase_history, "state": "prepared", "completion": completion,
        "helper_name": helper.name, "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest()})
    return integration.launch(helper, ["--state", str(state), "--maintenance", nonce])


def wait_for_parent(pid, timeout=3600):
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        raise ValueError("Invalid maintenance parent")
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            if ctypes.get_last_error() == 87:  # already exited
                return
            raise ValueError("Could not verify the application has exited")
        try:
            result = kernel.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000))
            if result != 0:
                raise ValueError("Application is still finishing work; removal was deferred")
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    else:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(.1)
        raise ValueError("Application is still finishing work")


def remove_files(root):
    """Allow only recognized app versions and bootstrap metadata, never state."""
    supplied = Path(root)
    root = supplied.resolve()
    if integration.is_link(supplied) or root != integration.install_root().resolve():
        raise ValueError("Removal root is not this application's installation")
    allowed = {integration.launcher_executable().name, "current.json", "previous.json", "pending-update.json", "failed-update.json"}
    paths = list(root.iterdir()) if root.exists() else []
    for path in paths:
        if integration.is_link(path) or not path.resolve().is_relative_to(root):
            raise ValueError("An installed path changed; removal needs local review")
        if path.is_dir():
            if path.name == "bootstrap":
                validate_bootstrap(path)
                continue
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.staging)?", path.name):
                raise ValueError("An unknown folder was preserved in the installation")
            for child in path.rglob("*"):
                if integration.is_link(child) or not child.resolve().is_relative_to(root):
                    raise ValueError("An installed file changed; removal needs local review")
                relative = child.relative_to(path)
                if child.is_dir() and relative.parts[0] != "THIRD_PARTY_LICENSES":
                    raise ValueError("An unknown installed folder was preserved for local review")
                if child.is_file() and relative.parts[0] not in (integration.launcher_executable().name, "LICENSE", "THIRD_PARTY_LICENSES", "release.zip"):
                    raise ValueError("An unknown installed file was preserved for local review")
        elif path.name not in allowed:
            raise ValueError("An unknown file was preserved in the installation")
    # All absolute recursive targets have been checked before the first delete.
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    if root.exists():
        root.rmdir()


def validate_bootstrap(folder):
    """Only signed, hash-matching npm installer/proof pairs are ours to erase."""
    trusted = updates.config()
    if not trusted or integration.is_link(folder):
        raise ValueError("Installer cache needs local review")
    children = list(folder.iterdir())
    if len(children) > 256:
        raise ValueError("Installer cache exceeds its cleanup limit")
    proofs = {}
    for child in children:
        if integration.is_link(child) or not child.is_file() or not child.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Unknown installer cache file was preserved")
        if re.fullmatch(r"[a-f0-9]{64}\.manifest\.json", child.name):
            if child.stat().st_size > updates.MAX_MANIFEST:
                raise ValueError("Installer proof exceeds its limit")
            manifest = updates.verify(child.read_bytes(), trusted, require_upgrade=False)
            installer = manifest.get("installer", {})
            digest = child.name.removesuffix(".manifest.json")
            if installer.get("sha256") != digest:
                raise ValueError("Installer proof identity changed")
            proofs[digest] = {**installer, "sequence": manifest["sequence"], "version": manifest["version"]}
        elif child.name != "channel.json" and not re.fullmatch(r"[a-f0-9]{64}\.exe", child.name):
            raise ValueError("Unknown installer cache file was preserved")
    for child in children:
        if child.suffix == ".exe":
            installer = proofs.get(child.stem)
            if not installer or child.stat().st_size != installer["size"] or hashlib.sha256(child.read_bytes()).hexdigest() != installer["sha256"]:
                raise ValueError("An unverified installer was preserved for local review")
        elif child.name == "channel.json":
            if child.stat().st_size > 1024:
                raise ValueError("Installer channel metadata exceeds its limit")
            channel = json.loads(child.read_bytes())
            if set(channel) != {"sequence", "version", "installer_sha256"} or channel.get("installer_sha256") not in proofs:
                raise ValueError("Installer channel metadata needs local review")
            proof = proofs[channel["installer_sha256"]]
            if channel["sequence"] != proof["sequence"] or channel["version"] != proof["version"]:
                raise ValueError("Installer channel metadata changed")


def remove_connection_proofs(state, erase_history=False):
    state = Path(state).resolve()
    paths = [state / name for name in ("worker/auth.json", "worker/pairing.json", "worker/renewal.json", "setup-interface.json")]
    archives = state / "worker-archive"
    if archives.exists():
        if integration.is_link(archives):
            raise ValueError("Connection archives must not be links")
        for archive in archives.iterdir():
            if integration.is_link(archive) or not archive.is_dir():
                raise ValueError("Connection archives need local review")
            paths.extend(archive / name for name in ("auth.json", "pairing.json", "renewal.json"))
    for path in paths:
        if integration.is_link(path) or integration.is_link(path.parent) or not path.resolve().is_relative_to(state):
            raise ValueError("Local connection state changed; removal needs review")
    for path in paths:
        path.unlink(missing_ok=True)
    if erase_history:
        for name in ("worker", "worker-archive"):
            path = state / name
            if path.exists():
                if path.is_symlink() or not path.resolve().is_relative_to(state):
                    raise ValueError("Saved history is outside the application state")
                if any(child.is_symlink() or not child.resolve().is_relative_to(state) for child in path.rglob("*")):
                    raise ValueError("Saved history contains links; erase it manually after review")
                shutil.rmtree(path)


def activate_update(state, *, healthy, start=integration.launch):
    root = integration.install_root().resolve()
    trusted = updates.config()
    if not trusted:
        raise ValueError("Signed automatic updates have not been configured")
    selected = updates.verify_staged(root, trusted)
    current = json.loads((root / "current.json").read_bytes())
    previous_exe = integration.selected_executable()
    integration.atomic_json(root / "previous.json", current)
    integration.atomic_json(root / "current.json", selected)
    process = start(integration.selected_executable(), ["--managed", "--validate-update", "--background", "--state", str(state)])
    if healthy(selected["version"], process):
        (root / "pending-update.json").unlink()
        return "updated"
    # A new process must be gone before fallback acquires its state locks.
    if process.poll() is None:
        if os.name == "nt":
            # This is the exact Popen returned for our validation candidate.
            # One-file PyInstaller has a bootloader child, so close its tree.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            raise ValueError("New version did not stop; rollback needs local review") from None
    integration.atomic_json(root / "current.json", current)
    integration.atomic_json(root / "failed-update.json", {"sequence": selected["sequence"], "version": selected["version"]})
    (root / "pending-update.json").unlink(missing_ok=True)
    start(previous_exe, ["--managed", "--background", "--state", str(state)])
    return "rolled_back"


def cleanup_helper(state):
    """A bounded native shell removes only this exact copied EXE after exit."""
    helper = Path(sys.executable).resolve()
    state = Path(state).resolve()
    if os.name != "nt" or helper.parent != state or not re.fullmatch(r"maintenance-helper-[a-f0-9]{16}\.exe", helper.name) or integration.is_link(helper):
        return False
    import base64
    expected = hashlib.sha256(helper.read_bytes()).hexdigest()
    literal = str(helper).replace("'", "''")
    script = ("$ErrorActionPreference='Stop'; Wait-Process -Id " + str(os.getpid()) + " -Timeout 120 -ErrorAction SilentlyContinue; "
        "$target=[IO.Path]::GetFullPath('" + literal + "'); "
        "if((Test-Path -LiteralPath $target -PathType Leaf) -and ((Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0 "
        "-and (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -eq '" + expected + "'){Remove-Item -LiteralPath $target -Force}")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.Popen([str(shell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
        base64.b64encode(script.encode("utf-16le")).decode("ascii")], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True


def retain_delivery(state, value, relay, result_state):
    root = Path(state) / "removal-receipts"
    if integration.is_link(root):
        raise ValueError("Removal receipts must not be redirected")
    root.mkdir(mode=0o700, exist_ok=True)
    identity = hashlib.sha256(value["operation_id"].encode()).hexdigest()
    record = {"operation_id": value["operation_id"], "completion": value["completion"], "relay": relay,
        "result_state": result_state, "delivery_state": "pending", "helper_name": value.get("helper_name"),
        "helper_sha256": value.get("helper_sha256"), "summary": "Removal finished locally; confirmation delivery is pending. Run the retained verified helper with --retry-removal."}
    record["expires_at"] = value["completion"]["expires_at"]
    if result_state != "succeeded":
        record["summary"] = "Removal could not complete; confirmation delivery is pending. Run the retained verified helper with --retry-removal."
    if record["expires_at"] <= time.time() * 1000:
        record.update(delivery_state="unconfirmed", summary="Removal confirmation expired. The service did not confirm whether local removal finished.")
        record.pop("completion", None)
        value.pop("completion", None)
    integration.atomic_json(root / (identity + ".json"), record)
    publish_recovery(state, record)


def publish_recovery(state, record):
    integration.atomic_json(Path(state) / "removal-recovery.json", {
        "state": record["delivery_state"], "summary": record["summary"],
        "retry_available": record["delivery_state"] == "pending", "helper_name": record.get("helper_name"),
        "expires_at": record.get("expires_at", (record.get("completion") or {}).get("expires_at"))})


def remove_retained_helper(state, record):
    name, digest = record.get("helper_name"), record.get("helper_sha256")
    if not isinstance(name, str) or not re.fullmatch(r"maintenance-helper-[a-f0-9]{16}\.exe", name) or not isinstance(digest, str):
        return
    helper = Path(state) / name
    if integration.is_link(helper) or not helper.is_file() or hashlib.sha256(helper.read_bytes()).hexdigest() != digest:
        return
    if helper.resolve() == Path(sys.executable).resolve():
        cleanup_helper(state)
    else:
        try:
            helper.unlink()
        except OSError:
            pass  # another verified receipt helper may still be exiting


def retry_pending(state, *, maximum_seconds=300):
    """Delivery only: no installation, provider access, or task credentials."""
    state = Path(state).resolve()
    root = state / "removal-receipts"
    if not root.exists():
        return {"status": "not_pending", "message": "No removal confirmation is pending."}
    if integration.is_link(root):
        raise ValueError("Removal receipts must not be redirected")
    paths = list(root.iterdir())
    if len(paths) > 32:
        raise ValueError("Too many removal receipts need local review")
    deadline = time.monotonic() + maximum_seconds
    pending = 0
    for path in paths:
        if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name) or integration.is_link(path) or path.stat().st_size > 16384:
            raise ValueError("Removal receipt needs local review")
        record = json.loads(path.read_bytes())
        if record.get("delivery_state") != "pending":
            continue
        completion = record.get("completion")
        if not isinstance(completion, dict):
            raise ValueError("Removal receipt capability is missing")
        expired = type(completion.get("expires_at")) not in (int, float) or completion["expires_at"] <= time.time() * 1000
        if expired:
            record.update(delivery_state="unconfirmed", summary="Removal confirmation expired. Local removal may have finished; the service did not confirm it.")
        else:
            validate_completion(completion, record["relay"])
            summary = "Companion removed. Provider accounts and workspace files were preserved." if record["result_state"] == "succeeded" else "Removal could not complete; local review is needed."
            remaining = max(0, deadline - time.monotonic())
            if complete_with_retry(completion, record["relay"], record["result_state"], summary, maximum_seconds=remaining):
                record.update(delivery_state="acknowledged", summary="Removal completion was confirmed by the service.")
            else:
                pending += 1
        if record["delivery_state"] != "pending":
            record.pop("completion", None)
            active = state / "maintenance.json"
            if active.is_file() and not integration.is_link(active) and active.stat().st_size <= 16384:
                value = json.loads(active.read_bytes())
                if value.get("operation_id") == record["operation_id"]:
                    value.pop("completion", None)
                    integration.atomic_json(active, value)
            receipt_path = state / "maintenance-receipt.json"
            if receipt_path.is_file() and not integration.is_link(receipt_path) and receipt_path.stat().st_size <= 16384:
                local = json.loads(receipt_path.read_bytes())
                if local.get("operation_id") == record["operation_id"]:
                    local.update(completion_pending=False, delivery_state=record["delivery_state"])
                    integration.atomic_json(receipt_path, local)
            remove_retained_helper(state, record)
        integration.atomic_json(path, record)
        publish_recovery(state, record)
    return {"status": "pending" if pending else "settled", "message": "Removal confirmation remains pending; retry before its expiry." if pending else "Removal receipt delivery settled. Inspect the public status summary."}


def run(state, nonce):
    state = Path(state).resolve()
    request = state / "maintenance.json"
    if request.is_symlink() or request.stat().st_size > 8192:
        raise ValueError("Invalid maintenance request")
    value = json.loads(request.read_bytes())
    if value.get("schema") != 1 or not isinstance(nonce, str) or not secrets.compare_digest(value.get("nonce", ""), nonce):
        raise ValueError("Maintenance request does not match this helper")
    if value.get("state") != "prepared" or value.get("action") not in ("update", "uninstall"):
        raise ValueError("Maintenance request has already settled")
    wait_for_parent(value["parent_pid"])
    relay = None
    try:
        if value["action"] == "update":
            from companion import headless
            def healthy(version, process):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        return False
                    live = headless.status(state)
                    if live.get("status") not in ("unreachable", "not_running") and live.get("companion_version") == version and live.get("validation_pending") is True:
                        return True  # task admission stays closed during rollback eligibility
                    time.sleep(.5)
                return False
            with integration.installation_lock():
                outcome = activate_update(state, healthy=healthy)
            if outcome == "updated":
                headless.submit(state, "accept_update")
        else:
            connection = json.loads((state / "worker/worker.json").read_bytes()) if (state / "worker/worker.json").is_file() else {}
            relay = connection.get("relay", "")
            if value.get("completion"):
                validate_completion(value["completion"], relay)
            with integration.installation_lock():
                integration.remove_integrations()
                remove_files(integration.install_root())
                remove_connection_proofs(state, value.get("erase_history", False))
            outcome = "removed"
        receipt = {"state": "succeeded", "outcome": outcome, "operation_id": value.get("operation_id"),
            "history_preserved": not value.get("erase_history", False), "helper_retained": True}
        integration.atomic_json(state / "maintenance-receipt.json", receipt)
        if value.get("completion"):
            if complete_with_retry(value["completion"], relay, "succeeded", "Companion removed. Provider accounts and workspace files were preserved."):
                value.pop("completion", None)
            else:
                receipt["completion_pending"] = True
                integration.atomic_json(state / "maintenance-receipt.json", receipt)
                retain_delivery(state, value, relay, "succeeded")
        value.update(state="succeeded")
    except Exception as error:
        integration.atomic_json(state / "maintenance-receipt.json", {"state": "failed", "operation_id": value.get("operation_id"),
            "summary": "Maintenance could not complete; local review is needed.", "error": type(error).__name__})
        value.update(state="failed")
        if value.get("completion") and relay:
            if complete_with_retry(value["completion"], relay, "failed", "Removal could not complete; local review is needed. Workspace files were preserved."):
                value.pop("completion", None)
            else:
                retain_delivery(state, value, relay, "failed")
        raise
    finally:
        value.pop("nonce", None)
        integration.atomic_json(request, value)
        controls_path = state / "companion-controls.json"
        if value.get("operation_id") and controls_path.is_file() and not integration.is_link(controls_path):
            controls = json.loads(controls_path.read_bytes())
            item = controls.get(value["operation_id"])
            if item:
                item["state"] = value["state"]
                item["parameters"].pop("completion", None)
                item["summary"] = "Companion removal finished." if value["state"] == "succeeded" else "Removal needs local review."
                integration.atomic_json(controls_path, controls)
        if not value.get("completion"):
            try:
                cleanup_helper(state)
            except OSError:
                pass
