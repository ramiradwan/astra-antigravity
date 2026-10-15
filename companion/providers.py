"""Discover normal CLI sessions and install verified first-party binaries.

Only a user pressing Install/Sign in invokes an installer or authentication.
Provider secrets and raw CLI output never become setup responses.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from urllib.parse import urlsplit
import zipfile

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
GOOGLE_UPDATER = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
MAX_DOWNLOAD = 256 * 1024 * 1024


def find_program(name, private_bin=None):
    if name not in ("codex", "agy", "git"):
        raise ValueError("Unknown provider")
    suffix = ".exe" if os.name == "nt" else ""
    override = os.environ.get({"codex": "CODEX_PATH", "agy": "AGY_PATH", "git": "ASTRA_ANTIGRAVITY_GIT_PATH"}[name])
    candidates = [Path(override).expanduser()] if override else []
    if private_bin:
        candidates += [Path(private_bin) / (name + suffix), Path(private_bin) / "git" / "cmd" / "git.exe"] if name == "git" else [Path(private_bin) / (name + suffix)]
        if name == "codex":
            candidates.append(Path(private_bin) / "codex" / ("codex" + suffix))
    found = shutil.which(name + suffix) or shutil.which(name)
    if found:
        candidates.append(Path(found))
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    if name == "agy":
        candidates += [local / "agy/bin/agy.exe", Path.home() / ".local/bin/agy"]
        if os.name == "nt" and (local / "Packages").is_dir():
            candidates += list((local / "Packages").glob("OpenAI.Codex_*/LocalCache/Local/agy/bin/agy.exe"))
    if name == "codex" and os.name == "nt":
        npm = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "npm"
        triple = "aarch64-pc-windows-msvc" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64-pc-windows-msvc"
        package = "codex-win32-arm64" if triple.startswith("aarch64") else "codex-win32-x64"
        for modules in [npm / "node_modules"] + [candidate.parent / "node_modules" for candidate in candidates]:
            for root in (modules / "@openai" / package, modules / "@openai/codex/node_modules/@openai" / package, modules / "@openai/codex"):
                candidates.append(root / "vendor" / triple / "bin/codex.exe")
    return next((str(path.resolve()) for path in candidates if path.is_file() and (os.name != "nt" or path.suffix.lower() == ".exe")), None)


def probe(command, timeout=12):
    # Reuse the bounded, noninteractive probe; never expose its captured output.
    import sys
    sys.path.insert(0, str(ROOT / "plugins/astra-antigravity"))
    from command_probes import run_probe
    return run_probe(command, timeout=timeout)


def accounts(private_bin):
    result = {}
    for name in ("codex", "agy", "git"):
        executable = find_program(name, private_bin)
        row = {"installed": bool(executable), "status": "missing"}
        if executable:
            try:
                if name == "codex":
                    reply = probe([executable, "login", "status"])
                    mode = (reply.stdout + reply.stderr).lower()
                    row["status"] = "connected" if reply.returncode == 0 and "chatgpt" in mode and "api key" not in mode else "sign_in_required"
                elif name == "agy":
                    if agy_api_mode():
                        row["status"] = "api_mode"
                        result[name] = row
                        continue
                    reply = probe([executable, "models"])
                    # An advertised catalog is not proof that inference access
                    # works. The actual task still verifies subscription access.
                    row["status"] = "available" if reply.returncode == 0 and reply.stdout.strip() else "sign_in_required"
                else:
                    row["status"] = "available" if probe([executable, "--version"]).returncode == 0 else "unavailable"
            except (OSError, RuntimeError, subprocess.SubprocessError):
                row["status"] = "check_failed"
        result[name] = row
    return result


def agy_api_mode():
    settings = Path.home() / ".gemini/antigravity-cli/settings.json"
    if not settings.exists():
        return False
    if settings.stat().st_size > 65536:
        raise ValueError("Review your Antigravity settings before connecting")
    # Read only the provider choice into the status response; settings can hold
    # secrets, so neither their contents nor parse errors are exposed.
    try:
        return json.loads(settings.read_text(encoding="utf-8")).get("modelProvider") == "gemini"
    except (ValueError, OSError, AttributeError):
        raise ValueError("Review your Antigravity settings before connecting") from None


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed):
        self.allowed = set(allowed)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_url(value, allowed):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Installer download destination is not an approved provider")


def download(url, allowed, limit=MAX_DOWNLOAD):
    check_url(url, allowed)
    opener = urllib.request.build_opener(SafeRedirect(allowed))
    request = urllib.request.Request(url, headers={"User-Agent": "Astra-Antigravity-Setup/0.1.0", "Accept": "application/json, application/octet-stream"})
    with opener.open(request, timeout=30) as response:
        if int(response.headers.get("Content-Length", 0)) > limit:
            raise ValueError("Provider download exceeds its size limit")
        content = bytearray()
        while chunk := response.read(65536):
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError("Provider download exceeds its size limit")
        return bytes(content)


def verify(content, digest, algorithm="sha256"):
    size = {"sha256": 64, "sha512": 128}[algorithm]
    if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{" + str(size) + "}", digest):
        raise ValueError("Provider did not publish a valid download checksum")
    if hashlib.new(algorithm, content).hexdigest() != digest.lower():
        raise ValueError("Provider download checksum did not match")


def github_asset(repository, matcher):
    metadata = json.loads(download("https://api.github.com/repos/" + repository + "/releases/latest", {"api.github.com"}, 524288))
    assets = [item for item in metadata.get("assets", []) if matcher(item.get("name", ""))]
    if len(assets) != 1:
        raise ValueError("An unambiguous provider download was not found for this platform")
    asset = assets[0]
    url = asset["browser_download_url"]
    if not url.startswith("https://github.com/" + repository + "/releases/download/"):
        raise ValueError("Provider release belongs to an unexpected repository")
    content = download(url, {"github.com", "release-assets.githubusercontent.com"})
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        raise ValueError("Provider release must publish a SHA-256 digest")
    verify(content, digest.removeprefix("sha256:"))
    return asset["name"], content


def write_executable(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ValueError("Installer will not replace an existing executable")
    with path.open("xb") as stream:
        stream.write(content)
    path.chmod(0o700)
    return str(path)


def install(name, private_bin):
    """Install on an explicit UI action, without shell scripts or profile edits."""
    if find_program(name, private_bin):
        return
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if name == "agy":
        target = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}.get(platform.system())
        if not target:
            raise ValueError("This platform needs the provider's official installer")
        metadata = json.loads(download(GOOGLE_UPDATER + "/manifests/" + target + ("_arm64" if arm else "_amd64") + ".json", {urlsplit(GOOGLE_UPDATER).hostname}, 16384))
        url = metadata["url"]
        if not url.startswith("https://storage.googleapis.com/antigravity-public/antigravity-cli/"):
            raise ValueError("Unexpected Antigravity release location")
        content = download(url, {"storage.googleapis.com"})
        verify(content, metadata["sha512"], "sha512")
        write_executable(Path(private_bin) / ("agy.exe" if os.name == "nt" else "agy"), content)
    elif name == "codex":
        triple = ("aarch64" if arm else "x86_64") + {"Windows": "-pc-windows-msvc", "Darwin": "-apple-darwin", "Linux": "-unknown-linux-musl"}.get(platform.system(), "-unsupported")
        filename, content = github_asset("openai/codex", lambda value: value == "codex-" + triple + (".exe.zip" if os.name == "nt" else ".tar.gz"))
        if filename.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                mains = [entry for entry in entries if entry.filename in ("codex.exe", "codex-" + triple + ".exe")]
                destination = Path(private_bin) / "codex"
                if len(mains) != 1 or sum(entry.file_size for entry in entries) > 1024 * 1024 * 1024:
                    raise ValueError("Unexpected Codex archive contents")
                if destination.exists() or any(not (destination / entry.filename).resolve().is_relative_to(destination.resolve()) for entry in entries):
                    raise ValueError("Codex archive escaped its new installation directory")
                # Preserve the official companion executables, resources and
                # notices. The current distribution is more than one binary.
                archive.extractall(destination)
                main = destination / mains[0].filename
                if main.name != "codex.exe":
                    main.rename(destination / "codex.exe")
                return
        else:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                entries = [entry for entry in archive.getmembers() if entry.isfile() and Path(entry.name).name.startswith("codex")]
                if len(entries) != 1 or entries[0].size > MAX_DOWNLOAD:
                    raise ValueError("Unexpected Codex archive contents")
                binary = archive.extractfile(entries[0]).read(MAX_DOWNLOAD + 1)
        write_executable(Path(private_bin) / ("codex.exe" if os.name == "nt" else "codex"), binary)
    elif name == "git" and os.name == "nt":
        _, content = github_asset("git-for-windows/git", lambda value: bool(re.fullmatch(r"MinGit-[0-9.]+(?:\.windows\.[0-9]+)?-(?:arm64|64-bit)\.zip", value)) and ("arm64" in value) == arm)
        destination = Path(private_bin) / "git"
        if destination.exists():
            raise ValueError("Git installation directory already exists")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if sum(entry.file_size for entry in entries) > MAX_DOWNLOAD or any(not (destination / entry.filename).resolve().is_relative_to(destination.resolve()) for entry in entries):
                raise ValueError("Git archive exceeds its size or path boundary")
            archive.extractall(destination)
    else:
        raise ValueError("Use the provider's installer on this platform")


def sign_in(name, private_bin, private_directory):
    if name not in ("codex", "agy"):
        raise ValueError("Choose Codex or Antigravity")
    executable = find_program(name, private_bin)
    if not executable:
        raise ValueError("Install this provider first")
    if name == "agy" and agy_api_mode():
        raise ValueError("Your Antigravity CLI uses API mode. Choose subscription sign-in in its settings, then check the connection. Setup will not change billing settings")
    command = [executable, "login"] if name == "codex" else [executable]
    # Antigravity's normal interactive first launch opens its browser sign-in.
    # We do not send a prompt, capture its credentials, or type into its TUI.
    return subprocess.Popen(command, cwd=private_directory, stdin=None if name == "agy" else subprocess.DEVNULL,
        stdout=None if name == "agy" else subprocess.DEVNULL, stderr=None if name == "agy" else subprocess.DEVNULL,
        creationflags=(subprocess.CREATE_NEW_CONSOLE if name == "agy" else subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0)
