"""Per-user installation, browser handoff, and opt-in login startup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

VERSION = "0.4.1"
SCHEME = "astra-antigravity"
APP_ID = "AstraAntigravityCompanion"


def parse_connection_uri(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char in value for char in ('"', "\r", "\n", "\x00")):
        raise ValueError("Invalid connection link")
    url = urlsplit(value)
    if url.scheme != SCHEME or url.netloc != "connect" or url.path not in ("", "/") or url.fragment:
        raise ValueError("Invalid connection link")
    values = parse_qs(url.query, strict_parsing=True)
    if set(values) - {"request_id"} or any(len(items) != 1 for items in values.values()):
        raise ValueError("Connection links cannot change account, workspace, relay, or executable settings")
    identifier = values.get("request_id", [None])[0]
    if identifier:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identifier):
            raise ValueError("Invalid setup request identifier")
    return identifier


def install_root():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AstraAntigravity"
    return Path.home() / ".local/lib/astra-antigravity"


def installed_executable():
    return install_root() / VERSION / ("AstraAntigravity.exe" if os.name == "nt" else "astra-antigravity")


def launcher_executable():
    return install_root() / ("AstraAntigravity.exe" if os.name == "nt" else "astra-antigravity")


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink() or path.with_suffix(path.suffix + ".new").is_symlink():
        raise ValueError("Application settings must not be links")
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def owned_executable(value, root=None):
    """Recognize only our stable entry or a numeric version's exact EXE."""
    root = Path(root or install_root()).resolve()
    supplied = Path(value)
    target = supplied.resolve()
    if is_link(supplied) or not target.is_relative_to(root):
        return False
    relative = target.relative_to(root)
    name = "AstraAntigravity.exe" if os.name == "nt" else "astra-antigravity"
    return relative.parts == (name,) or (len(relative.parts) == 2 and relative.name == name
        and __import__("re").fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", relative.parts[0]) is not None
        and not is_link(supplied.parent))


def selected_executable():
    if is_link(install_root()):
        raise ValueError("Application installation must not be redirected")
    root = install_root().resolve()
    pointer = root / "current.json"
    if pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 4096:
        raise ValueError("Installed application selection is missing")
    selected = json.loads(pointer.read_text(encoding="utf-8"))
    version = selected.get("version", "")
    if not __import__("re").fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Installed application selection is invalid")
    executable = root / version / launcher_executable().name
    if not owned_executable(executable, root) or not executable.is_file():
        raise ValueError("Selected application is missing")
    digest = selected.get("sha256")
    if not isinstance(digest, str) or hashlib.sha256(executable.read_bytes()).hexdigest() != digest:
        raise ValueError("Installed application verification failed")
    return executable


def launch(executable, arguments):
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return subprocess.Popen([str(executable), *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)


def install_current_app():
    if not getattr(sys, "frozen", False):
        return None
    with installation_lock():
        return _install_current_app_locked()


@contextmanager
def installation_lock(timeout=20):
    """One installer/update/remover across sessions for this per-user root."""
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        digest = hashlib.sha256(str(install_root().resolve()).casefold().encode()).hexdigest()
        # Global mutexes work across user sessions without a service or the
        # privilege needed for global file mappings. The root binds the user.
        handle = kernel.CreateMutexW(None, False, "Global\\DelegateToAntigravityInstall-" + digest)
        if not handle:
            raise ValueError("Could not acquire the per-user installation lock")
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, int(timeout * 1000))
            if result not in (0, 0x80):
                raise ValueError("Another installation or removal is in progress")
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        root = install_root()
        root.parent.mkdir(parents=True, exist_ok=True)
        path = root.parent / ("." + root.name + ".installation.lock")
        if is_link(path):
            raise ValueError("Installation lock must not be a link")
        with path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _install_current_app_locked():
    source, destination = Path(sys.executable).resolve(), installed_executable()
    if is_link(install_root()):
        raise ValueError("Application installation must not be redirected")
    root = install_root().resolve()
    current = root / "current.json"
    if current.exists():
        selected = selected_executable()
        if tuple(map(int, selected.parent.name.split("."))) > tuple(map(int, VERSION.split("."))):
            raise ValueError("A newer verified application is already installed; downgrade was refused")
    if not destination.resolve().is_relative_to(root) or destination.parent.is_symlink():
        raise ValueError("Invalid per-user installation directory")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source != destination.resolve():
        if destination.exists():
            if destination.is_symlink() or hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                raise ValueError("A different build is already installed at this version. Use a new version or review the existing installation")
        else:
            temporary = destination.with_name(destination.name + ".new")
            if temporary.exists() or temporary.is_symlink():
                raise ValueError("An unfinished installation needs review")
            shutil.copyfile(source, temporary)
            temporary.chmod(0o700)
            os.replace(temporary, destination)
    # Standalone EXE installs carry the same runtime notices as the ZIP.
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "companion/notices"
    if bundled.is_dir():
        for notice in bundled.rglob("*"):
            if notice.is_file():
                relative = notice.relative_to(bundled)
                target = destination.parent / relative
                if is_link(target) or is_link(target.parent):
                    raise ValueError("Installed notices must not be links")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(notice, target)
    launcher = launcher_executable()
    # A stable, already installed bootstrap remains usable across app updates.
    # It is replaced only by a maintenance helper after its process exits.
    if not launcher.exists():
        shutil.copyfile(destination, launcher)
        launcher.chmod(0o700)
    if launcher.is_symlink():
        raise ValueError("Installed launcher must not be a link")
    atomic_json(root / "current.json", {"version": VERSION, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
    migrate_startup(launcher)
    register_protocol(launcher)
    register_application(launcher)
    return destination


def migrate_startup(executable):
    if os.name != "nt":
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Run") as key:
        try:
            current, _ = winreg.QueryValueEx(key, APP_ID)
        except FileNotFoundError:
            return
        if current == startup_definition(executable):
            return
        # Never take over an entry belonging to another application.
        if not isinstance(current, str) or not current.endswith('" --background') or not current.startswith('"'):
            raise ValueError("An existing startup entry needs review")
        prior = current[1:-len('" --background')]
        if not owned_executable(prior):
            raise ValueError("An existing startup entry needs review")
        winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, startup_definition(executable))


def register_application(executable):
    if os.name != "nt":
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + APP_ID) as key:
        for name, value in {"DisplayName": "Delegate to Antigravity", "DisplayVersion": VERSION,
                "InstallLocation": str(install_root()), "UninstallString": '"' + str(executable) + '" --uninstall'}.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
    # A .url uses Windows' built-in file handler and needs no scripting host.
    start = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Microsoft/Windows/Start Menu/Programs"
    start.mkdir(parents=True, exist_ok=True)
    shortcut = start / "Delegate to Antigravity.url"
    expected = "[InternetShortcut]\nURL=" + executable.resolve().as_uri() + "\n"
    if shortcut.is_symlink():
        raise ValueError("Application shortcut must not be a link")
    if shortcut.exists() and shortcut.read_text(encoding="utf-8") != expected:
        raise ValueError("An existing application shortcut needs review")
    shortcut.write_text(expected, encoding="utf-8")


def remove_integrations():
    """Remove only values whose command points at this owned installation."""
    if os.name != "nt":
        return
    import winreg
    launcher = launcher_executable()
    migrate_startup(launcher)
    set_startup(launcher, False)
    protocol = "Software\\Classes\\" + SCHEME
    command = '"' + str(launcher) + '" --connect-uri "%1"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, protocol + "\\shell\\open\\command") as key:
            actual, _ = winreg.QueryValueEx(key, "")
        if actual != command:
            raise ValueError("The connection handler belongs to another installation")
        for suffix in ("\\shell\\open\\command", "\\shell\\open", "\\shell", ""):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, protocol + suffix)
    except FileNotFoundError:
        pass
    uninstall = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + APP_ID
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall) as key:
            actual, _ = winreg.QueryValueEx(key, "UninstallString")
        if actual != '"' + str(launcher) + '" --uninstall':
            raise ValueError("The uninstall entry belongs to another installation")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, uninstall)
    except FileNotFoundError:
        pass
    start = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "Microsoft/Windows/Start Menu/Programs/Delegate to Antigravity.url"
    if start.is_symlink():
        raise ValueError("Application shortcut must not be a link")
    if start.exists() and start.read_text(encoding="utf-8") == "[InternetShortcut]\nURL=" + launcher.resolve().as_uri() + "\n":
        start.unlink()


def register_protocol(executable):
    # Protocol links supply one validated URI, never executable arguments or
    # arbitrary shell commands. Registration is scoped to the current user.
    if os.name != "nt":
        return
    import winreg
    command = '"' + str(executable) + '" --connect-uri "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Classes\\" + SCHEME) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Delegate to Antigravity connection")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Classes\\" + SCHEME + "\\shell\\open\\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def startup_definition(executable, system=None):
    system = system or sys.platform
    if system == "win32":
        return '"' + str(executable) + '" --background'
    if system == "darwin":
        return plistlib.dumps({"Label": "com.astra-antigravity.companion", "ProgramArguments": [str(executable), "--background"],
            "RunAtLoad": True, "KeepAlive": False})
    escaped = str(executable).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '[Desktop Entry]\nType=Application\nName=Delegate to Antigravity\nExec="' + escaped + '" --background\nTerminal=false\n'


def startup_enabled(executable):
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Run") as key:
                value, _ = winreg.QueryValueEx(key, APP_ID)
            return value == startup_definition(executable)
        except FileNotFoundError:
            return False
    target = Path.home() / ("Library/LaunchAgents/com.astra-antigravity.companion.plist" if sys.platform == "darwin" else ".config/autostart/astra-antigravity.desktop")
    return target.is_file()


def set_startup(executable, enabled):
    if type(enabled) is not bool or not Path(executable).is_file():
        raise ValueError("Choose an installed application and a startup preference")
    if os.name == "nt":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Windows\\CurrentVersion\\Run") as key:
            try:
                current, _ = winreg.QueryValueEx(key, APP_ID)
                if current != startup_definition(executable):
                    raise ValueError("An existing startup entry needs review")
            except FileNotFoundError:
                pass
            if enabled:
                winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, startup_definition(executable))
            else:
                try:
                    winreg.DeleteValue(key, APP_ID)
                except FileNotFoundError:
                    pass
        return
    target = Path.home() / ("Library/LaunchAgents/com.astra-antigravity.companion.plist" if sys.platform == "darwin" else ".config/autostart/astra-antigravity.desktop")
    definition = startup_definition(executable)
    encoded = definition if isinstance(definition, bytes) else definition.encode()
    if target.is_symlink() or (target.exists() and target.read_bytes() != encoded):
        raise ValueError("An existing startup entry needs review")
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        target.chmod(0o600)
    elif target.exists():
        target.unlink()
