"""Verify a reviewed public release on a fresh standard GitHub Windows runner.

This is deliberately not a local install test. It never approves the generated
pairing, supplies provider credentials, selects a workspace or submits a task.
All child output and inspected state remain in memory. Only fixed check names,
booleans, reviewed versions and hashes may reach the Actions log.

The checked-in pins must be completed and reviewed before dispatch. A missing
pin fails before a download or command. The real public npx package executes
from its verified downloaded tarball, not an unpinned registry dist-tag.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = "ramiradwan/astra-antigravity"
RELEASE = f"https://github.com/{REPOSITORY}/releases/download/"
MANIFEST_URL = f"https://github.com/{REPOSITORY}/releases/latest/download/companion-win32-stable.json"
PUBLIC_KEY = "/C3cEwwi+glFDiTYOtAwyJG5JlZuy5YnFGveUli6Nug="
PINS_PATH = Path(__file__).with_name("public-bootstrap-first-use-pins.json")
PROTOCOL = r"Software\Classes\astra-antigravity"
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AstraAntigravityCompanion"
RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_ID = "AstraAntigravityCompanion"
HASH = re.compile(r"[a-f0-9]{64}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
MAX_OUTPUT = 262_144
MONITOR_DIAGNOSTICS = ("monitor_started", "windows_sample_ready", "tcp_sample_ready", "udp_sample_ready",
                       "monitor_permission_denied", "monitor_command_unavailable", "monitor_cim_error", "monitor_other_error")


class CheckFailed(Exception):
    """Contains a fixed, non-sensitive check identifier only."""


def require(condition, name):
    if not condition:
        raise CheckFailed(name)


def json_file(path, maximum=65_536):
    path = Path(path)
    require(not path.is_symlink() and not (hasattr(path, "is_junction") and path.is_junction()), "regular_state_file")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "bounded_state_file")
    value = json.loads(raw)
    require(type(value) is dict, "object_state_file")
    return value


def validate_pins(value):
    keys = {"schema", "repository", "bootstrap_version", "bootstrap_url", "bootstrap_sha256", "companion_version",
            "manifest_url", "manifest_sha256", "installer_url", "installer_sha256", "installer_size", "sequence"}
    require(type(value) is dict and set(value) == keys, "pins_shape")
    require(type(value["schema"]) is int and value["schema"] == 1 and value["repository"] == REPOSITORY, "pins_repository")
    for key in ("bootstrap_version", "companion_version"):
        require(type(value[key]) is str and VERSION.fullmatch(value[key]) is not None, "pins_versions")
    tag = f"companion-v{value['companion_version']}/"
    require(value["bootstrap_url"] == RELEASE + tag + f"delegate-to-antigravity-{value['bootstrap_version']}.tgz"
            and value["installer_url"] == RELEASE + tag + "AstraAntigravity.exe"
            and value["manifest_url"] == MANIFEST_URL, "pins_urls")
    for key in ("bootstrap_sha256", "manifest_sha256", "installer_sha256"):
        # No placeholder, empty digest, latest tag or computed-on-download pin.
        require(type(value[key]) is str and HASH.fullmatch(value[key]) is not None
                and value[key] != "0" * 64, "pins_complete")
    require(type(value["installer_size"]) is int and 1 <= value["installer_size"] <= 256 * 1024 * 1024
            and type(value["sequence"]) is int and value["sequence"] > 0, "pins_limits")
    return value


def runner_environment_guard(environment, platform=os.name):
    expected = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows",
                "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"}
    require(platform == "nt" and all(environment.get(key) == val for key, val in expected.items()), "public_windows_runner")


def runner_guard(environment, event, platform=os.name):
    runner_environment_guard(environment, platform)
    require(event.get("repository", {}).get("private") is False
            and event.get("repository", {}).get("full_name") == REPOSITORY, "public_repository_event")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def approved_redirect(value):
    url = urllib.parse.urlsplit(value)
    return (url.scheme == "https" and not url.username and not url.password and not url.fragment
            and url.port in (None, 443) and
            (url.hostname in ("release-assets.githubusercontent.com", "objects.githubusercontent.com")
             or url.hostname == "github.com" and url.path.startswith(f"/{REPOSITORY}/releases/download/") and not url.query))


def download(url, digest, maximum):
    """No auth, cookie jar, automatic redirects, URL logging or unbounded body."""
    opener = urllib.request.build_opener(NoRedirect())
    destination = url
    for _ in range(5):
        request = urllib.request.Request(destination, headers={"User-Agent": "Public-bootstrap-verification", "Accept": "application/octet-stream"})
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 303, 307, 308):
                error.close()
                raise CheckFailed("public_asset_download") from None
            location = error.headers.get("Location", "")
            error.close()
            destination = urllib.parse.urljoin(destination, location)
            require(bool(location) and approved_redirect(destination), "approved_asset_redirect")
            continue
        with response:
            require(response.status == 200, "public_asset_download")
            raw = response.read(maximum + 1)
        require(0 < len(raw) <= maximum, "public_asset_size")
        require(hashlib.sha256(raw).hexdigest() == digest, "public_asset_hash")
        return raw
    raise CheckFailed("asset_redirect_limit")


def validate_package(raw, pins):
    expected = {"package/" + name for name in ("package.json", "bin.mjs", "bootstrap.mjs", "release-config.json", "LICENSE", "README.md")}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        members = archive.getmembers()
        require(len(members) == len(expected) and {item.name for item in members} == expected
                and all(item.isfile() and 0 <= item.size <= 131_072 for item in members), "bootstrap_inventory")
        # Never extract an archive to disk or execute any of its inspection code.
        package = json.loads(archive.extractfile("package/package.json").read())
        config = json.loads(archive.extractfile("package/release-config.json").read())
    require(package.get("name") == "delegate-to-antigravity" and package.get("version") == pins["bootstrap_version"]
            and package.get("bin") == {"delegate-to-antigravity": "bin.mjs"}
            and not any(package.get(key) for key in ("dependencies", "optionalDependencies", "peerDependencies", "bundledDependencies", "bundleDependencies"))
            and set(package.get("scripts", {})) <= {"test"}, "bootstrap_package")
    require(config == {"manifest_url": pins["manifest_url"], "public_key": PUBLIC_KEY}, "bootstrap_trust_config")


def validate_manifest(raw, pins):
    envelope = json.loads(raw)
    manifest = json.loads(base64.b64decode(envelope["manifest"], validate=True))
    # The envelope itself is SHA-pinned here. The real bootstrap additionally
    # verifies its Ed25519 signature before it downloads or runs the installer.
    require(manifest.get("schema") == 1 and manifest.get("platform") == "win32" and manifest.get("state_schema") == 1
            and manifest.get("version") == pins["companion_version"] and manifest.get("sequence") == pins["sequence"]
            and manifest.get("installer") == {"url": pins["installer_url"], "sha256": pins["installer_sha256"],
                                               "size": pins["installer_size"]}, "reviewed_manifest")


def child_environment(environment, temporary):
    # Do not forward Actions credentials, a user npmrc, provider keys or NODE_OPTIONS.
    allowed = {"appdata", "localappdata", "userprofile", "homedrive", "homepath", "path", "pathext", "systemroot", "windir",
               "systemdrive", "comspec", "temp", "tmp", "programfiles", "programfiles(x86)", "programw6432", "allusersprofile",
               "programdata", "username", "userdomain", "computername", "processor_architecture", "number_of_processors"}
    result = {key: val for key, val in environment.items() if key.casefold() in allowed}
    result.update(NPM_CONFIG_CACHE=str(temporary / "npm-cache"), NPM_CONFIG_USERCONFIG=str(temporary / "empty.npmrc"),
                  NPM_CONFIG_AUDIT="false", NPM_CONFIG_FUND="false", NPM_CONFIG_UPDATE_NOTIFIER="false", NPM_CONFIG_LOGLEVEL="error")
    (temporary / "empty.npmrc").touch(exist_ok=False)
    return result


def captured(arguments, environment, timeout=40):
    """Bound each private stdout/stderr buffer; never return it in an exception."""
    child = subprocess.Popen([str(arg) for arg in arguments], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()

    def consume(stream, target):
        while chunk := stream.read(8192):
            remaining = MAX_OUTPUT - len(target)
            target.extend(chunk[:max(remaining, 0)])
            if len(chunk) > remaining:
                exceeded.set()

    readers = [threading.Thread(target=consume, args=(stream, target), daemon=True)
               for stream, target in zip((child.stdout, child.stderr), buffers)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    while child.poll() is None and time.monotonic() < deadline and not exceeded.is_set():
        time.sleep(.1)
    if child.poll() is None:
        # Only the command subtree started above, never an image-name kill.
        taskkill = Path(environment["SYSTEMROOT"]) / "System32/taskkill.exe"
        subprocess.run([str(taskkill), "/PID", str(child.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        child.wait(timeout=10)
        raise CheckFailed("bounded_command_execution")
    for reader in readers:
        reader.join(timeout=5)
    require(not exceeded.is_set() and not any(reader.is_alive() for reader in readers), "bounded_command_output")
    require(child.returncode == 0, "command_exit")
    return bytes(buffers[0])


# Only process names/IDs and sampled window/listener owner IDs enter the pipe.
# Neither process command lines nor files, account names or connection URLs are
# observed. Process-start events cover short-lived providers between samples.
MONITOR = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::Out.WriteLine('{"monitor_started":true}')
try {
  Register-CimIndicationEvent -Query 'SELECT * FROM Win32_ProcessStartTrace' -SourceIdentifier 'BootstrapProcessTrace' | Out-Null
  [Console]::Out.WriteLine('{"ready":true}')
  $first = $true
  $until = [DateTime]::UtcNow.AddSeconds(480)
  while ([DateTime]::UtcNow -lt $until) {
    foreach ($event in @(Get-Event -SourceIdentifier 'BootstrapProcessTrace' -ErrorAction SilentlyContinue)) {
      $process = $event.SourceEventArgs.NewEvent
      [Console]::Out.WriteLine((@{pid=[int]$process.ProcessID; parent=[int]$process.ParentProcessID; name=[string]$process.ProcessName} | ConvertTo-Json -Compress))
      Remove-Event -EventIdentifier $event.EventIdentifier
    }
    $processes = @(Get-Process)
    $windows = @($processes | Where-Object { $_.MainWindowHandle -ne 0 } | ForEach-Object { $_.Id })
    if ($first) { [Console]::Out.WriteLine('{"windows_sample_ready":true}') }
    $tcp = @(Get-NetTCPConnection -State Listen | ForEach-Object { $_.OwningProcess })
    if ($first) { [Console]::Out.WriteLine('{"tcp_sample_ready":true}') }
    $udp = @(Get-NetUDPEndpoint | ForEach-Object { $_.OwningProcess })
    if ($first) { [Console]::Out.WriteLine('{"udp_sample_ready":true}') }
    [Console]::Out.WriteLine((@{sample=$true; windows=$windows; listeners=@($tcp + $udp); running=@($processes | ForEach-Object { $_.Id })} | ConvertTo-Json -Compress))
    $first = $false
    Start-Sleep -Milliseconds 200
  }
} catch {
  # Map runtime errors to a strict set of booleans. Never emit the exception,
  # message, target object, module name, stack, path or command text.
  $kind = $_.Exception.GetType().FullName
  if ($_.CategoryInfo.Category -eq 'PermissionDenied' -or $kind -eq 'System.UnauthorizedAccessException') {
    [Console]::Out.WriteLine('{"error":true,"monitor_permission_denied":true}')
  } elseif ($kind -eq 'System.Management.Automation.CommandNotFoundException') {
    [Console]::Out.WriteLine('{"error":true,"monitor_command_unavailable":true}')
  } elseif ($kind -eq 'Microsoft.Management.Infrastructure.CimException') {
    [Console]::Out.WriteLine('{"error":true,"monitor_cim_error":true}')
  } else { [Console]::Out.WriteLine('{"error":true,"monitor_other_error":true}') }
}
"""


class Observation:
    def __init__(self, environment):
        self.records = []
        self.ready = threading.Event()
        self.failed = False
        self.reader_failed = False
        self.process = None
        self.environment = environment

    def start(self):
        shell = Path(self.environment["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        encoded = base64.b64encode(MONITOR.encode("utf-16le")).decode("ascii")
        self.process = subprocess.Popen([str(shell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        require(self.ready.wait(25) and not self.failed, "process_trace_ready")
        # Require a successful listener/window sample as well as trace startup.
        try:
            wait_for(lambda: self.has_sample() or self.failed, 25)
        except CheckFailed:
            raise CheckFailed("observation_sample_ready") from None
        require(not self.failed and any(record.get("sample") for record in self.records), "observation_ready")

    def has_sample(self):
        return any(record.get("sample") is True for record in self.records)

    def diagnostics(self, report):
        # Only explicitly allowlisted boolean fields cross the logging boundary.
        report["process_trace_ready"] = self.ready.is_set()
        report["observation_sample_available"] = self.has_sample()
        report["monitor_read_failed"] = self.reader_failed
        report["monitor_exited"] = self.process is not None and self.process.poll() is not None
        for key in MONITOR_DIAGNOSTICS:
            report[key] = any(record.get(key) is True for record in self.records)

    def _read(self):
        try:
            while raw := self.process.stdout.readline(65_537):
                if len(raw) > 65_536 or len(self.records) >= 10_000:
                    self.failed = True
                    self.reader_failed = True
                    break
                value = json.loads(raw)
                if value.get("error"):
                    self.failed = True
                if value.get("ready"):
                    self.ready.set()
                self.records.append(value)
        except Exception:
            self.failed = True
            self.reader_failed = True

    def close(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.reader.join(timeout=5)

    def verify(self, report):
        require(not self.failed and self.process.poll() is None, "observation_healthy")
        records = list(self.records)
        owned = descendants(records, os.getpid())
        names = [record["name"].casefold() for record in records if record.get("pid") in owned]
        allowed = re.compile(r"(?:node|cmd|conhost|powershell|taskkill|astraantigravity|maintenance-helper-[a-f0-9]{16})\.exe")
        require(bool(names) and all(allowed.fullmatch(name) for name in names), "expected_processes_only")
        providers = re.compile(r"(?:codex|agy|antigravity|claude|gemini|python|python3)(?:\.exe)?", re.I)
        require(not any(providers.fullmatch(record.get("name", "")) for record in records), "no_provider_process_observed")
        samples = [record for record in records if record.get("sample")]
        require(len(samples) >= 3, "observation_samples")
        require(not any(owned.intersection(sample.get("windows", [])) for sample in samples), "no_window_observed")
        require(not any(owned.intersection(sample.get("listeners", [])) for sample in samples), "no_listener_observed")
        for key in ("expected_processes_only", "no_provider_process_observed", "no_window_observed", "no_listener_observed"):
            report[key] = True

    def wait_stopped(self):
        previous = sum(bool(record.get("sample")) for record in self.records)
        def stopped():
            records = list(self.records)
            samples = [record for record in records if record.get("sample")]
            owned = descendants(records, os.getpid())
            return len(samples) >= previous + 2 and all(not owned.intersection(sample["running"]) for sample in samples[-2:])
        wait_for(stopped, 20)


def descendants(records, parent):
    owned = {parent}
    while True:
        expanded = owned | {record["pid"] for record in records if record.get("parent") in owned}
        if expanded == owned:
            return owned - {parent}
        owned = expanded


def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.25)
    raise CheckFailed("bounded_state_wait")


def registry_value(key, name=""):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as opened:
            return winreg.QueryValueEx(opened, name)[0]
    except FileNotFoundError:
        return None


def registry_exists(key):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
            return True
    except FileNotFoundError:
        return False


def paths(environment):
    root = Path(environment["LOCALAPPDATA"]) / "AstraAntigravity"
    state = Path(environment["USERPROFILE"]) / ".local/state/astra-antigravity-companion"
    shortcut = Path(environment["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Delegate to Antigravity.url"
    require(all(item.is_absolute() for item in (root, state, shortcut)), "absolute_install_paths")
    return root, state, shortcut


def fresh_preflight(root, state, shortcut):
    require(not any(item.exists() or item.is_symlink() for item in (root, state, shortcut))
            and not registry_exists(PROTOCOL) and not registry_exists(UNINSTALL)
            and registry_value(RUN, APP_ID) is None, "fresh_profile")


def verified_executable(path, pins):
    return (path.is_file() and not path.is_symlink() and path.stat().st_size == pins["installer_size"]
            and hashlib.sha256(path.read_bytes()).hexdigest() == pins["installer_sha256"])


def wait_pid_exit(pid):
    """Wait for only the original heartbeat PID, with a read-only kernel handle."""
    import ctypes
    require(type(pid) is int and pid > 0 and pid != os.getpid(), "owned_heartbeat_pid")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        require(ctypes.get_last_error() == 87, "companion_process_exit")  # already exited
        return
    try:
        require(kernel.WaitForSingleObject(handle, 30_000) == 0, "companion_process_exit")
    finally:
        kernel.CloseHandle(handle)


def verify_install(root, shortcut, pins):
    launcher = root / "AstraAntigravity.exe"
    require(verified_executable(launcher, pins) and verified_executable(root / pins["companion_version"] / launcher.name, pins), "installed_binary_hashes")
    require(json_file(root / "current.json") == {"version": pins["companion_version"], "sha256": pins["installer_sha256"]}, "selected_version")
    require(registry_value(PROTOCOL + r"\shell\open\command") == f'"{launcher}" --connect-uri "%1"', "protocol_registration")
    require(registry_value(UNINSTALL, "DisplayVersion") == pins["companion_version"]
            and registry_value(UNINSTALL, "InstallLocation") == str(root)
            and registry_value(UNINSTALL, "UninstallString") == f'"{launcher}" --uninstall', "uninstall_registration")
    require(registry_value(RUN, APP_ID) is None, "startup_off")
    require(shortcut.is_file() and shortcut.read_text(encoding="utf-8") == f"[InternetShortcut]\nURL={launcher.resolve().as_uri()}\n", "launcher_shortcut")


def verify_pending(state, pins, now=None):
    live = json_file(state / "headless-status.json")
    pending = json_file(state / "worker/pairing.json")
    now = time.time() * 1000 if now is None else now
    require(type(live.get("observed_at")) in (int, float) and 0 <= now - live["observed_at"] <= 5000, "fresh_heartbeat")
    require(live.get("status") == "approval_required" and live.get("worker_running") is False
            and live.get("companion_version") == pins["companion_version"] and live.get("startup_enabled") is False
            and live.get("validation_pending") is False and type(live.get("pid")) is int, "pending_headless_setup")
    config = pending.get("config", {})
    require(config.get("setup_only") is True and config.get("workspaces") == {} and config.get("cloud_task_id") is None
            and pending.get("setup_request_id") is None, "management_only_pairing")
    require(not any((state / "worker" / name).exists() for name in ("auth.json", "worker.json", "worker.sqlite3", "provider-tasks")), "no_authorized_worker_or_tasks")


def verify_pairing_output(output, state):
    code = json_file(state / "headless-status.json").get("pairing_code")
    require(type(code) is str and re.fullmatch(r"[a-f0-9]{24}", code) is not None, "pairing_code_returned")
    expected = ("In ChatGPT, connect this computer using code: " + code).encode("ascii")
    require(output.splitlines().count(expected) == 1, "pairing_code_returned")


def cleanup(root, state, shortcut, pins, environment):
    launcher = root / "AstraAntigravity.exe"
    require(verified_executable(launcher, pins), "owned_cleanup_executable")
    heartbeat = state / "headless-status.json"
    pid = json_file(heartbeat).get("pid") if heartbeat.is_file() else None
    # Pending pairing must finish stopping before maintenance can remove files.
    captured([launcher, "--stop"], environment)
    wait_for(lambda: heartbeat.is_file() and json_file(heartbeat).get("status") == "not_running", 35)
    if pid is not None:
        wait_pid_exit(pid)
    captured([launcher, "--uninstall"], environment)
    receipt = state / "maintenance-receipt.json"
    wait_for(lambda: receipt.is_file() and json_file(receipt).get("state") in ("succeeded", "failed"), 60)
    require(json_file(receipt).get("state") == "succeeded" and json_file(receipt).get("outcome") == "removed", "supported_uninstall")
    wait_for(lambda: not root.exists() and not list(state.glob("maintenance-helper-*.exe")), 25)
    require(not shortcut.exists() and not registry_exists(PROTOCOL) and not registry_exists(UNINSTALL)
            and registry_value(RUN, APP_ID) is None and not (state / "worker/pairing.json").exists()
            and not (state / "worker/auth.json").exists(), "install_and_connection_removed")
    # Do not call --status after removal: that command intentionally creates its
    # state folder. Harmless local maintenance history stays on this ephemeral
    # runner. The unapproved relay pairing expires; it is never approved here.


def verify(report):
    pins = validate_pins(json_file(PINS_PATH))  # must precede network/processes
    report.update({key: pins[key] for key in ("bootstrap_version", "companion_version", "bootstrap_sha256", "manifest_sha256", "installer_sha256")})
    runner_environment_guard(os.environ)
    runner_guard(os.environ, json_file(os.environ["GITHUB_EVENT_PATH"]))
    report["public_windows_runner"] = True
    root, state, shortcut = paths(os.environ)
    fresh_preflight(root, state, shortcut)
    report["fresh_profile"] = True
    raw = download(pins["bootstrap_url"], pins["bootstrap_sha256"], 131_072)
    validate_package(raw, pins)
    validate_manifest(download(pins["manifest_url"], pins["manifest_sha256"], 16_384), pins)
    report["public_release_pins_verified"] = True
    temporary_parent = Path(os.environ["RUNNER_TEMP"]).resolve()
    require(temporary_parent.is_dir(), "runner_temporary_directory")
    with tempfile.TemporaryDirectory(prefix="public-first-use-", dir=temporary_parent) as directory:
        temporary = Path(directory).resolve()
        require(temporary.parent == temporary_parent, "owned_temporary_directory")
        tarball = temporary / f"delegate-to-antigravity-{pins['bootstrap_version']}.tgz"
        tarball.write_bytes(raw)
        environment = child_environment(os.environ, temporary)
        node = Path(shutil.which("node", path=environment["PATH"]) or "")
        npx = node.parent / "node_modules/npm/bin/npx-cli.js"
        require(node.is_absolute() and node.is_file() and npx.is_file(), "node_and_npx_available")
        node_version = captured([node, "--version"], environment).decode().strip()
        require(re.fullmatch(r"v24\.[0-9]+\.[0-9]+", node_version) is not None, "node_version")
        report["node_version"] = node_version
        observer = Observation(environment)
        observer_started = False
        install_attempted = False
        primary_failed = False
        try:
            observer.start()
            observer_started = True
            install_attempted = True
            setup_output = captured([node, npx, "--yes", "--package", tarball, "delegate-to-antigravity", "setup"], environment, timeout=180)
            report["npx_setup_exited"] = True
            verify_install(root, shortcut, pins)
            report["installed_and_registered"] = True
            wait_for(lambda: (state / "worker/pairing.json").is_file(), 45)
            verify_pending(state, pins)
            report["management_only_pairing"] = True
            verify_pairing_output(setup_output, state)
            report["pairing_code_returned"] = True
            public_status = json.loads(captured([root / "AstraAntigravity.exe", "--status"], environment))
            require(public_status.get("status") == "approval_required" and public_status.get("worker_running") is False
                    and public_status.get("companion_version") == pins["companion_version"], "public_status_command")
            report["public_status_command"] = True
            # Observe the actual detached companion after the terminal exits.
            for _ in range(12):
                verify_pending(state, pins)
                require(registry_value(RUN, APP_ID) is None, "startup_off")
                time.sleep(.5)
            report["startup_off"] = True
        except CheckFailed as error:
            report[str(error)] = False
            primary_failed = True
            raise
        except Exception:
            primary_failed = True
            raise
        finally:
            try:
                if install_attempted:
                    try:
                        cleanup(root, state, shortcut, pins, environment)
                        observer.wait_stopped()
                        report["supported_stop_and_uninstall"] = True
                    except Exception:
                        report["supported_stop_and_uninstall"] = False
                        raise
                if observer_started:
                    require(observer.has_sample(), "observation_available")
                    observer.verify(report)
            except CheckFailed as error:
                report[str(error)] = False
                if not primary_failed:
                    raise
            except Exception:
                report["observation_finalization_failed"] = True
                if not primary_failed:
                    raise
            finally:
                observer.diagnostics(report)
                try:
                    observer.close()
                except Exception:
                    report["observation_closed"] = False
                    if not primary_failed:
                        raise CheckFailed("observation_closed") from None


def main():
    report = {"passed": False}
    try:
        verify(report)
        report["passed"] = True
    except CheckFailed as error:
        # Only our fixed identifiers, never captured stderr, paths, URLs, state
        # or exception text. Unexpected errors also get no traceback or repr.
        report[str(error)] = False
    except Exception:
        report["unexpected_error"] = True
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
