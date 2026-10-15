#!/usr/bin/env python3
"""Build a self-contained user companion, without credentials or publisher notes."""
from __future__ import annotations

import hashlib
import base64
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from companion.integration import VERSION


def license_files():
    """Preserve vendor notices from the exact locked build environment."""
    files = {"LICENSE": (ROOT / "LICENSE").read_bytes()}
    names = re.findall(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==", (ROOT / "companion/build-requirements.lock").read_text(), re.MULTILINE)
    for name in names:
        distribution = importlib.metadata.distribution(name)
        for entry in distribution.files or []:
            path = PurePosixPath(str(entry).replace("\\", "/"))
            if not any(part.endswith(".dist-info") for part in path.parts):
                continue
            if path.name != "METADATA" and not any(word in str(path).lower() for word in ("license", "copying", "notice")):
                continue
            if path.is_absolute() or any(part in ("", ".", "..") or ":" in part for part in path.parts):
                raise ValueError("Invalid dependency notice path")
            data = Path(distribution.locate_file(entry)).read_bytes()
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("Dependency notice exceeded its limit")
            files["THIRD_PARTY_LICENSES/" + str(path)] = data
    python_license = next((path for path in (Path(sys.base_prefix) / "LICENSE.txt", Path(os.__file__).parent / "LICENSE.txt") if path.is_file()), None)
    if not python_license:
        raise ValueError("The standalone interpreter's license must accompany the build")
    files["THIRD_PARTY_LICENSES/Python-LICENSE.txt"] = python_license.read_bytes()
    for path in (Path(sys.base_prefix) / "tcl").glob("*/license*"):
        if path.is_file():
            files["THIRD_PARTY_LICENSES/" + path.parent.name + "-" + path.name] = path.read_bytes()
    return files


def build():
    if sys.platform not in ("win32", "linux", "darwin"):
        raise ValueError("Build on the same operating system as the intended companion")
    output = (ROOT / "dist/companion" / VERSION).resolve()
    staging = (ROOT / "build/companion" / VERSION).resolve()
    if not output.is_relative_to(ROOT / "dist") or not staging.is_relative_to(ROOT / "build"):
        raise ValueError("Companion build paths escaped the repository")
    output.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    notices = license_files()
    bundled_notices = staging / "notices"
    bundled_notices.mkdir(exist_ok=True)
    for name, content in notices.items():
        target = bundled_notices / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    command = [sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--noconfirm",
        "--name", "AstraAntigravity", "--distpath", str(output), "--workpath", str(staging), "--specpath", str(staging),
        "--paths", str(ROOT), "--paths", str(ROOT / "plugins/astra-antigravity"),
        "--collect-submodules", "mcp", "--collect-submodules", "websockets", "--copy-metadata", "mcp",
        "--hidden-import", "sqlite3", "--hidden-import", "luna_gateway",
        "--hidden-import", "cryptography.hazmat.primitives.asymmetric.ed25519",
        "--add-data", str(ROOT / "relay/worker.py") + ":relay",
        "--add-data", str(bundled_notices) + ":companion/notices",
        ]
    defaults = json.loads((ROOT / "companion/update-config.json").read_bytes())
    feed = os.environ.get("COMPANION_UPDATE_URL", defaults["url"])
    public_key = os.environ.get("COMPANION_UPDATE_PUBLIC_KEY", defaults["public_key"])
    if bool(feed) != bool(public_key):
        raise ValueError("Configure both the signed update feed and its public verification key")
    if feed:
        from urllib.parse import urlsplit
        parsed = urlsplit(feed)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Update feed must be public HTTPS")
        if len(base64.b64decode(public_key, validate=True)) != 32:
            raise ValueError("Update verification key must be a base64 Ed25519 public key")
        repository = os.environ.get("COMPANION_UPDATE_REPOSITORY", defaults["repository"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid update repository")
        update_config = staging / "update-config.json"
        update_config.write_text(json.dumps({"url": feed, "public_key": public_key, "repository": repository}), encoding="utf-8")
        command += ["--add-data", str(update_config) + ":companion"]
    if sys.platform == "win32":
        command += ["--hide-console", "hide-early"]
    # Only maintained runtime files from the explicit release inventory.
    entries = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))
    for entry in entries:
        if entry.endswith(".py") or entry == "plugin.json" or entry == "LICENSE" or entry.startswith("schemas/"):
            source = ROOT / "plugins/astra-antigravity" / entry
            command += ["--add-data", str(source) + ":plugins/astra-antigravity/" + str(Path(entry).parent).replace("\\", "/")]
    command.append(str(ROOT / "companion/app.py"))
    environment = {key:value for key,value in os.environ.items() if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    executable = output / ("AstraAntigravity.exe" if sys.platform == "win32" else "AstraAntigravity")
    metadata = {"version": VERSION, "platform": sys.platform, "filename": executable.name,
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "bytes": executable.stat().st_size,
                "requires_python": False, "contains_credentials": False, "publication": "private candidate", "signed_updates_configured": bool(feed)}
    archive = output / ("astra-antigravity-companion-" + VERSION + "-" + sys.platform + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.write(executable, executable.name)
        for name, data in sorted(notices.items()):
            package.writestr(name, data)
    metadata.update({"archive":archive.name, "archive_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
                     "license_files":len(notices)})
    (output / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text(metadata["sha256"] + "  " + executable.name + "\n" + metadata["archive_sha256"] + "  " + archive.name + "\n", encoding="utf-8")
    print(json.dumps(metadata))


if __name__ == "__main__":
    build()
