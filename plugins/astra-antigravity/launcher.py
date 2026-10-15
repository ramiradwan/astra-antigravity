#!/usr/bin/env python3
"""Portable MCP launcher for the Astra Antigravity plugin."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable
from runtime_assets import plugin_directory

MCP_REQUIREMENT = "mcp[cli]==1.29.1"
REQUIREMENTS_LOCK = "requirements.lock"
DEFAULT_SERVER_TARGET = "luna_gateway.py"
ALLOWED_SERVER_TARGETS = frozenset({DEFAULT_SERVER_TARGET, "worktree_server.py"})


class DiscoveryError(RuntimeError):
    pass


class ManifestError(RuntimeError):
    pass


def _valid_file(value: str | os.PathLike[str] | None) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if path.is_file():
        return str(path.resolve())
    return None


def _first_existing(candidates: Iterable[Path]) -> str | None:
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _path_lookup(names: Iterable[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    return None


def _home() -> Path:
    return Path.home()


def _windows_localappdata() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    return Path(raw) if raw else _home() / "AppData" / "Local"


def discover_uv(*, required: bool = False) -> str | None:
    override = os.environ.get("UV_PATH")
    if override:
        resolved = _valid_file(override)
        if resolved:
            return resolved
        raise DiscoveryError(f"UV_PATH is set but does not point to a file: {override}")
    found = _path_lookup(("uv.exe", "uv") if os.name == "nt" else ("uv",))
    if found:
        return found
    candidates = (
        [_home() / ".local" / "bin" / "uv.exe", _windows_localappdata() / "uv" / "bin" / "uv.exe"]
        if os.name == "nt"
        else [_home() / ".local" / "bin" / "uv", _home() / ".cargo" / "bin" / "uv"]
    )
    fallback = _first_existing(candidates)
    if fallback:
        return fallback
    if required:
        raise DiscoveryError(
            "uv was not found. Set UV_PATH, add uv to PATH, or install uv in a standard user location."
        )
    return None


def discover_python(*, required: bool = True) -> str | None:
    override = os.environ.get("ASTRA_PYTHON_PATH")
    if override:
        resolved = _valid_file(override)
        if resolved:
            return resolved
        raise DiscoveryError(
            f"ASTRA_PYTHON_PATH is set but does not point to a file: {override}"
        )
    names = ("python.exe", "py.exe", "python") if os.name == "nt" else ("python3", "python")
    found = _path_lookup(names)
    if found:
        return found
    current = _valid_file(sys.executable)
    if current:
        return current
    candidates = (
        [
            _windows_localappdata() / "Programs" / "Python" / "Python313" / "python.exe",
            _windows_localappdata() / "Programs" / "Python" / "Python312" / "python.exe",
            _windows_localappdata() / "Programs" / "Python" / "Python311" / "python.exe",
        ]
        if os.name == "nt"
        else [_home() / ".local" / "bin" / "python3", Path("/usr/bin/python3"), Path("/usr/local/bin/python3")]
    )
    fallback = _first_existing(candidates)
    if fallback:
        return fallback
    if required:
        raise DiscoveryError(
            "Python was not found. Add python/python3 to PATH or set ASTRA_PYTHON_PATH."
        )
    return None


def discover_agy(*, required: bool = True) -> str | None:
    override = os.environ.get("AGY_PATH")
    if override:
        resolved = _valid_file(override)
        if resolved:
            return resolved
        raise DiscoveryError(f"AGY_PATH is set but does not point to a file: {override}")
    found = _path_lookup(("agy.exe", "agy") if os.name == "nt" else ("agy",))
    if found:
        return found
    candidates = (
        [_windows_localappdata() / "agy" / "bin" / "agy.exe", _home() / ".local" / "bin" / "agy.exe"]
        if os.name == "nt"
        else [_home() / ".local" / "bin" / "agy", _home() / "bin" / "agy"]
    )
    fallback = _first_existing(candidates)
    if fallback:
        return fallback
    if os.name == "nt":
        packages_dir = _windows_localappdata() / "Packages"
        if packages_dir.is_dir():
            for pkg in packages_dir.glob("OpenAI.Codex_*"):
                msix_candidate = pkg / "LocalCache" / "Local" / "agy" / "bin" / "agy.exe"
                if msix_candidate.is_file():
                    return str(msix_candidate.resolve())
    if required:
        raise DiscoveryError(
            "Antigravity CLI (agy) was not found. Set AGY_PATH, add agy to PATH, or install/authenticate the Antigravity CLI first."
        )
    return None


def _python_has_mcp(python: str) -> bool:
    try:
        completed = subprocess.run(
            [python, "-c", "import importlib.metadata,sys; sys.exit(importlib.metadata.version('mcp') != '1.29.1')"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        return completed.returncode == 0
    except Exception:
        return False


def build_server_command(
    plugin_root: str | os.PathLike[str] | None = None,
    server_target: str = DEFAULT_SERVER_TARGET,
) -> tuple[list[str], dict[str, str]]:
    if server_target not in ALLOWED_SERVER_TARGETS:
        raise DiscoveryError(f"Unsupported MCP server target: {server_target}")

    root = Path(plugin_root or Path(__file__).resolve().parent).resolve()
    server = root / server_target
    if not server.is_file():
        raise DiscoveryError(f"Plugin server was not found: {server}")

    agy_required = (server_target == "worktree_server.py")
    try:
        agy = discover_agy(required=agy_required)
    except DiscoveryError:
        if agy_required:
            raise
        agy = None
    uv = discover_uv(required=False)
    python = discover_python(required=True)
    env = dict(os.environ)
    if agy:
        env["AGY_PATH"] = agy
    env.setdefault("ASTRA_PYTHON_PATH", python)
    if uv:
        env.setdefault("UV_PATH", uv)
        requirements = root / REQUIREMENTS_LOCK
        if not requirements.is_file():
            raise DiscoveryError(f"Plugin dependency lock was not found: {requirements}")
        # Never discover configuration, .env files or a virtual environment from
        # the delegated repository. The shipped lock supplies transitive pins.
        return [uv, "run", "--no-project", "--no-config", "--no-env-file",
                "--isolated", "--python", python, "--with-requirements",
                str(requirements), "python", str(server)], env
    if _python_has_mcp(python):
        return [python, str(server)], env
    raise DiscoveryError(
        "uv was not found and the discovered Python does not have MCP SDK 1.29.1 installed. "
        "Install uv, set UV_PATH, or install the packaged requirements.lock "
        "into the selected Python environment with pip --require-hashes."
    )


MANIFEST_PATH = plugin_directory() / "plugin.json"


def get_plugin_version(manifest_path: Path | None = None) -> str:
    path = manifest_path or MANIFEST_PATH
    if not path.is_file():
        raise ManifestError(f"Plugin manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Plugin manifest is not valid JSON: {exc}")
    except Exception as exc:
        raise ManifestError(f"Failed to read plugin manifest: {exc}")
    if not isinstance(data, dict):
        raise ManifestError(f"Plugin manifest root must be a JSON object: {path}")
    if "version" not in data:
        raise ManifestError(f"Plugin manifest missing 'version' field: {path}")
    version = data["version"]
    if not isinstance(version, str) or not version.strip():
        raise ManifestError(
            f"Plugin manifest 'version' must be a non-empty string: {path}"
        )
    return version.strip()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        try:
            print(get_plugin_version())
            return 0
        except ManifestError as exc:
            print(f"astra-antigravity launcher: {exc}", file=sys.stderr)
            return 2
    if len(args) > 1:
        print("astra-antigravity launcher: expected at most one MCP server target", file=sys.stderr)
        return 2
    target = args[0] if args else DEFAULT_SERVER_TARGET
    try:
        command, env = build_server_command(server_target=target)
    except DiscoveryError as exc:
        print(f"astra-antigravity launcher: {exc}", file=sys.stderr)
        return 2
    try:
        return subprocess.call(command, cwd=str(Path(__file__).resolve().parent), env=env)
    except OSError as exc:
        print(f"astra-antigravity launcher: failed to start MCP server: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
