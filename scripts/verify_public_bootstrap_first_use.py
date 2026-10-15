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
import ctypes
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


# Native metadata snapshots are supplementary evidence, not a process-start
# trace. A 100 ms sample can miss short-lived processes, windows or endpoints;
# a missed parent can leave a grandchild unlinked, and Windows can reuse PIDs.
# No command line, title, address or port is retained or printed. The mandatory
# setup-only/no-workspace/no-auth/no-task and lifecycle checks remain separate.
# SDK layouts: tlhelp32/PROCESSENTRY32W; tcpmib/MIB_TCP{,6}ROW_OWNER_PID;
# udpmib/MIB_UDP{,6}ROW_OWNER_PID. Derive table alignment from ctypes, not offsets.
# https://learn.microsoft.com/windows/win32/api/tlhelp32/ns-tlhelp32-processentry32w
# https://learn.microsoft.com/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable
# https://learn.microsoft.com/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedudptable
# https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-enumwindows
U32 = ctypes.c_uint32
MAX_NATIVE_TABLE = 4 * 1024 * 1024
MAX_OBSERVATION_RECORDS = 10_000


class ProcessEntry32W(ctypes.Structure):
    _fields_ = [("size", U32), ("usage", U32), ("pid", U32), ("heap", ctypes.c_size_t),
                ("module", U32), ("threads", U32), ("parent", U32), ("priority", ctypes.c_int32),
                ("flags", U32), ("name", ctypes.c_uint16 * 260)]


class Tcp4Row(ctypes.Structure):
    _fields_ = [(key, U32) for key in ("state", "local_address", "local_port", "remote_address", "remote_port", "pid")]


class Tcp6Row(ctypes.Structure):
    _fields_ = [("local_address", ctypes.c_ubyte * 16), ("local_scope", U32), ("local_port", U32),
                ("remote_address", ctypes.c_ubyte * 16), ("remote_scope", U32), ("remote_port", U32), ("state", U32), ("pid", U32)]


class Udp4Row(ctypes.Structure):
    _fields_ = [(key, U32) for key in ("local_address", "local_port", "pid")]


class Udp6Row(ctypes.Structure):
    _fields_ = [("local_address", ctypes.c_ubyte * 16), ("local_scope", U32), ("local_port", U32), ("pid", U32)]


def table_offset(row_type):
    class Table(ctypes.Structure):
        _fields_ = [("count", U32), ("table", row_type * 1)]
    return Table.table.offset


def table_owner_pids(raw, row_type):
    offset, row_size = table_offset(row_type), ctypes.sizeof(row_type)
    require(len(raw) >= offset, "native_table_shape")
    count = U32.from_buffer_copy(raw[:4]).value
    require(count <= MAX_OBSERVATION_RECORDS and offset + count * row_size <= len(raw), "native_table_shape")
    return {row_type.from_buffer_copy(raw, offset + index * row_size).pid for index in range(count)} - {0}


def native_table(function, family, table_class, row_type, check):
    size = U32(0)
    result = function(None, ctypes.byref(size), False, family, table_class, 0)
    require(result in (0, 122), check)  # API return value, not GetLastError
    for _ in range(3):
        require(4 <= size.value <= MAX_NATIVE_TABLE, "native_table_limit")
        buffer = ctypes.create_string_buffer(size.value)
        allocated = size.value
        result = function(buffer, ctypes.byref(size), False, family, table_class, 0)
        if result == 122:  # endpoints can grow between the two calls
            continue
        require(result == 0 and size.value <= allocated, check)
        return table_owner_pids(buffer.raw[:size.value], row_type)
    raise CheckFailed(check)


class NativeWindowsSnapshots:
    def __init__(self):
        require(os.name == "nt", "native_windows_snapshot")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.ip = ctypes.WinDLL("iphlpapi")
        self.window_callback = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
        self.kernel.CreateToolhelp32Snapshot.argtypes = [U32, U32]
        self.kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        for name in ("Process32FirstW", "Process32NextW"):
            function = getattr(self.kernel, name)
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
            function.restype = ctypes.c_int
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int
        self.user.EnumWindows.argtypes = [self.window_callback, ctypes.c_ssize_t]
        self.user.EnumWindows.restype = ctypes.c_int
        self.user.IsWindowVisible.argtypes = [ctypes.c_void_p]
        self.user.IsWindowVisible.restype = ctypes.c_int
        self.user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(U32)]
        self.user.GetWindowThreadProcessId.restype = U32
        for name in ("GetExtendedTcpTable", "GetExtendedUdpTable"):
            function = getattr(self.ip, name)
            function.argtypes = [ctypes.c_void_p, ctypes.POINTER(U32), ctypes.c_int, U32, ctypes.c_int, U32]
            function.restype = U32
        self.windows_ready = self.tcp_ready = self.udp_ready = False

    def processes(self):
        handle = self.kernel.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        require(handle not in (None, ctypes.c_void_p(-1).value), "native_process_snapshot")
        try:
            entry = ProcessEntry32W()
            entry.size = ctypes.sizeof(entry)
            require(bool(self.kernel.Process32FirstW(handle, ctypes.byref(entry))), "native_process_snapshot")
            records = []
            while True:
                require(len(records) < MAX_OBSERVATION_RECORDS, "native_process_limit")
                name = bytes(entry.name).decode("utf-16le", errors="replace").split("\0", 1)[0]
                records.append({"pid": entry.pid, "parent": entry.parent, "name": name})
                if not self.kernel.Process32NextW(handle, ctypes.byref(entry)):
                    require(ctypes.get_last_error() == 18, "native_process_snapshot")  # ERROR_NO_MORE_FILES
                    return records
        finally:
            self.kernel.CloseHandle(handle)

    def visible_windows(self):
        owners, failed = set(), []
        def callback(window, _parameter):
            try:
                if self.user.IsWindowVisible(window):
                    pid = U32(0)
                    thread = self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
                    if thread and pid.value:  # HWND may disappear during enumeration
                        owners.add(pid.value)
                return 1
            except Exception:
                failed.append(True)
                return 0  # never let ctypes print a callback exception/traceback
        retained = self.window_callback(callback)
        result = self.user.EnumWindows(retained, 0)
        require(bool(result) and not failed, "native_visible_windows")
        self.windows_ready = True
        return owners

    def snapshot(self):
        processes = self.processes()
        windows = self.visible_windows()
        # AF_INET6 is 23 on Windows, independently of the host running unit tests.
        tcp = native_table(self.ip.GetExtendedTcpTable, 2, 3, Tcp4Row, "native_tcp_snapshot")
        tcp |= native_table(self.ip.GetExtendedTcpTable, 23, 3, Tcp6Row, "native_tcp_snapshot")
        self.tcp_ready = True
        udp = native_table(self.ip.GetExtendedUdpTable, 2, 1, Udp4Row, "native_udp_snapshot")
        udp |= native_table(self.ip.GetExtendedUdpTable, 23, 1, Udp6Row, "native_udp_snapshot")
        self.udp_ready = True
        return {"processes": processes, "windows": windows, "listeners": tcp | udp}


class Observation:
    def __init__(self, _environment=None):
        self.records = []
        self.ready = threading.Event()
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.reader = None
        self.api = None
        self.failed = False
        self.failure_check = None
        self.seen = set()

    def start(self):
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        available = self.ready.wait(25)
        if self.failure_check:
            raise CheckFailed(self.failure_check)
        require(available and self.has_sample(), "observation_sample_ready")

    def has_sample(self):
        with self.lock:
            return any(record.get("sample") is True for record in self.records)

    def diagnostics(self, report):
        report.update(process_snapshots_used=self.has_sample(), process_start_trace_used=False,
                      sampling_can_miss_short_lived_activity=True, monitor_started=self.started.is_set(),
                      monitor_read_failed=self.failed, monitor_exited=self.reader is not None and not self.reader.is_alive(),
                      observation_sample_available=self.has_sample())
        for key in ("windows", "tcp", "udp"):
            report[key + "_sample_ready"] = bool(self.api and getattr(self.api, key + "_ready", False))

    def _capture(self, snapshot, *, baseline=False):
        with self.lock:
            for record in snapshot["processes"]:
                identity = (record["pid"], record["parent"], record["name"].casefold())
                if identity not in self.seen:
                    self.seen.add(identity)
                    if not baseline:
                        self.records.append(record)
            require(len(self.seen) <= MAX_OBSERVATION_RECORDS, "observation_record_limit")
            owned = descendants(self.records, os.getpid())
            # Only owned PID sets are retained for repeated samples. Unrelated
            # baseline process names never enter the event records or report.
            self.records.append({"sample": True, "windows": sorted(owned.intersection(snapshot["windows"])),
                                 "listeners": sorted(owned.intersection(snapshot["listeners"])),
                                 "running": sorted(owned.intersection(record["pid"] for record in snapshot["processes"]))})
            require(len(self.records) <= MAX_OBSERVATION_RECORDS, "observation_record_limit")

    def _read(self):
        self.started.set()
        try:
            self.api = self.api or NativeWindowsSnapshots()
            self._capture(self.api.snapshot(), baseline=True)
            self.ready.set()
            deadline = time.monotonic() + 480
            while not self.stopped.wait(.1):
                require(time.monotonic() < deadline, "observation_time_limit")
                self._capture(self.api.snapshot())
        except CheckFailed as error:
            self.failed = True
            self.failure_check = str(error)  # only fixed local check identifiers
        except Exception:
            self.failed = True
            self.failure_check = "native_snapshot_failed"
        finally:
            self.ready.set()  # wake start immediately on a native API failure

    def close(self):
        self.stopped.set()
        if self.reader:
            self.reader.join(timeout=5)
            require(not self.reader.is_alive(), "observation_closed")
        require(not self.failed, self.failure_check or "observation_healthy")

    def verify(self, report):
        require(not self.failed and self.stopped.is_set() and self.reader is not None
                and not self.reader.is_alive(), "observation_healthy")
        with self.lock:
            records = list(self.records)
        owned = descendants(records, os.getpid())
        names = [record["name"].casefold() for record in records if record.get("pid") in owned]
        providers = re.compile(r"(?:codex|agy|antigravity|claude|gemini|python|python3)(?:\.exe)?", re.I)
        require(not any(providers.fullmatch(name) for name in names), "no_provider_process_observed")
        allowed = re.compile(r"(?:node|cmd|conhost|powershell|taskkill|astraantigravity|maintenance-helper-[a-f0-9]{16})\.exe")
        require(bool(names) and all(allowed.fullmatch(name) for name in names), "expected_processes_only")
        samples = [record for record in records if record.get("sample")]
        require(len(samples) >= 3, "observation_samples")
        require(not any(owned.intersection(sample.get("windows", [])) for sample in samples), "no_window_observed")
        require(not any(owned.intersection(sample.get("listeners", [])) for sample in samples), "no_listener_observed")
        for key in ("expected_processes_only", "no_provider_process_observed", "no_window_observed", "no_listener_observed"):
            report[key] = True

    def wait_stopped(self):
        with self.lock:
            previous = sum(bool(record.get("sample")) for record in self.records)
        def stopped():
            require(not self.failed, self.failure_check or "observation_healthy")
            with self.lock:
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
            except CheckFailed as error:
                report[str(error)] = False
                if not primary_failed:
                    primary_failed = True
                    raise
            except Exception:
                report["observation_finalization_failed"] = True
                if not primary_failed:
                    primary_failed = True
                    raise
            finally:
                try:
                    observer.close()
                    report["observation_closed"] = True
                    if observer_started:
                        require(observer.has_sample(), "observation_available")
                        observer.verify(report)
                except CheckFailed as error:
                    report.setdefault("observation_closed", False)
                    report[str(error)] = False
                    if not primary_failed:
                        raise
                except Exception:
                    report.setdefault("observation_closed", False)
                    report["observation_finalization_failed"] = True
                    if not primary_failed:
                        raise CheckFailed("observation_finalization_failed") from None
                finally:
                    observer.diagnostics(report)


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
