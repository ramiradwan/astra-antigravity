"""Bounded signed release verification. Downloads never choose commands or paths."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import urllib.request
from urllib.parse import urlsplit
import zipfile

from companion import integration, providers

MAX_RELEASE = 256 * 1024 * 1024
MAX_MANIFEST = 16 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate release field")
        result[key] = value
    return result


def config():
    path = providers.ROOT / "companion/update-config.json"
    if not path.is_file():
        return None
    if path.stat().st_size > 4096:
        raise ValueError("Invalid update configuration")
    value = json.loads(path.read_bytes(), object_pairs_hook=unique_object)
    if set(value) != {"url", "public_key", "repository"}:
        raise ValueError("Invalid update configuration")
    parsed = urlsplit(value["url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Updates require a public HTTPS feed")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise ValueError("Invalid release repository")
    if len(base64.b64decode(value["public_key"], validate=True)) != 32:
        raise ValueError("Invalid update verification key")
    return value


def verify(envelope, trusted, *, minimum_sequence=0, platform=None, require_upgrade=True):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if len(envelope) > MAX_MANIFEST:
        raise ValueError("Release manifest exceeds its limit")
    packet = json.loads(envelope, object_pairs_hook=unique_object)
    if set(packet) != {"manifest", "signature"}:
        raise ValueError("Invalid signed release")
    raw = base64.b64decode(packet["manifest"], validate=True)
    signature = base64.b64decode(packet["signature"], validate=True)
    Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted["public_key"], validate=True)).verify(signature, raw)
    value = json.loads(raw, object_pairs_hook=unique_object)
    fields = {"schema", "sequence", "version", "platform", "url", "size", "sha256", "state_schema"}
    if set(value) not in (fields, fields | {"installer"}):
        raise ValueError("Unexpected release fields")
    if value["schema"] != 1 or value["state_schema"] != 1 or value["platform"] != (platform or sys.platform):
        raise ValueError("Release is incompatible with this computer")
    if type(value["sequence"]) is not int or value["sequence"] <= minimum_sequence:
        raise ValueError("Release is stale or already installed")
    if not isinstance(value["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["version"]):
        raise ValueError("Invalid release version")
    if require_upgrade and tuple(map(int, value["version"].split("."))) <= tuple(map(int, integration.VERSION.split("."))):
        raise ValueError("Automatic updates cannot downgrade the application")
    if type(value["size"]) is not int or not 0 < value["size"] <= MAX_RELEASE:
        raise ValueError("Release exceeds its size limit")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
        raise ValueError("Invalid release checksum")
    url = urlsplit(value["url"])
    prefix = "/" + trusted["repository"] + "/releases/download/"
    if url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment or not url.path.startswith(prefix):
        raise ValueError("Release asset is outside the configured publisher")
    parts = url.path[len(prefix):].split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts) or not parts[1].endswith(".zip"):
        raise ValueError("Invalid release asset path")
    if "installer" in value:
        installer = value["installer"]
        if not isinstance(installer, dict) or set(installer) != {"url", "size", "sha256"}:
            raise ValueError("Invalid standalone installer")
        installer_url = urlsplit(installer["url"])
        asset_parts = installer_url.path[len(prefix):].split("/")
        if installer_url.scheme != "https" or installer_url.netloc != "github.com" or installer_url.query or installer_url.fragment or not installer_url.path.startswith(prefix) or len(asset_parts) != 2 or asset_parts[0] != parts[0] or asset_parts[1] != "AstraAntigravity.exe":
            raise ValueError("Invalid standalone installer URL")
        if type(installer["size"]) is not int or not 0 < installer["size"] <= MAX_RELEASE or not isinstance(installer["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", installer["sha256"]):
            raise ValueError("Invalid standalone installer checksum")
    return value


def fetch_feed(trusted):
    url = trusted["url"]
    allowed = {urlsplit(url).hostname}
    if urlsplit(url).hostname == "github.com":
        allowed |= {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    return providers.download(url, allowed, MAX_MANIFEST)


def stage(envelope, trusted, root, *, minimum_sequence=0, download=providers.download):
    value = verify(envelope, trusted, minimum_sequence=minimum_sequence)
    blob = download(value["url"], {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}, value["size"])
    if len(blob) != value["size"] or hashlib.sha256(blob).hexdigest() != value["sha256"]:
        raise ValueError("Release download did not match its signed checksum")
    root = Path(root).resolve()
    destination = root / value["version"]
    if destination.exists() or destination.is_symlink():
        raise ValueError("This version already exists; it needs local review")
    temporary = root / (value["version"] + ".staging")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("An unfinished update needs local review")
    contents = {}
    size = 0
    executable = "AstraAntigravity.exe" if value["platform"] == "win32" else "AstraAntigravity"
    installed_name = integration.launcher_executable().name
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if len(archive.infolist()) > 1024:
            raise ValueError("Release archive has too many files")
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            if item.is_dir():
                continue
            if path.is_absolute() or "\\" in item.filename or any(part in ("", ".", "..") or ":" in part for part in path.parts):
                raise ValueError("Unsafe release archive path")
            if item.filename in contents or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Release archive has duplicate files or links")
            if item.filename not in (executable, "LICENSE") and not item.filename.startswith("THIRD_PARTY_LICENSES/"):
                raise ValueError("Unexpected release archive file")
            size += item.file_size
            if size > MAX_RELEASE * 2:
                raise ValueError("Expanded release exceeds its limit")
            contents[item.filename] = archive.read(item)
    if executable not in contents or "LICENSE" not in contents:
        raise ValueError("Release executable or notices are missing")
    temporary.mkdir(mode=0o700)
    try:
        for name, data in contents.items():
            # Release ZIPs use the build's display name; installed entry points
            # use the platform's stable filename (lowercase on Unix).
            path = temporary / (installed_name if name == executable else name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (temporary / "release.zip").write_bytes(blob)
        (temporary / installed_name).chmod(0o700)
        os.replace(temporary, destination)
    except Exception:
        # Keep bounded staging evidence rather than recursively deleting a path
        # after an unexpected filesystem change.
        raise
    selected = {"version": value["version"], "sha256": hashlib.sha256(contents[executable]).hexdigest(),
                "sequence": value["sequence"]}
    integration.atomic_json(root / "pending-update.json", {"selection": selected,
        "envelope": base64.b64encode(envelope).decode("ascii")})
    return selected


def verify_staged(root, trusted):
    root = Path(root).resolve()
    pending = root / "pending-update.json"
    if pending.is_symlink() or pending.stat().st_size > MAX_MANIFEST * 2:
        raise ValueError("Invalid pending update")
    record = json.loads(pending.read_bytes(), object_pairs_hook=unique_object)
    envelope = base64.b64decode(record["envelope"], validate=True)
    manifest = verify(envelope, trusted)
    selected = record["selection"]
    if selected.get("version") != manifest["version"] or selected.get("sequence") != manifest["sequence"]:
        raise ValueError("Pending update identity changed")
    folder = root / manifest["version"]
    asset = folder / "release.zip"
    if folder.is_symlink() or asset.is_symlink() or asset.stat().st_size != manifest["size"]:
        raise ValueError("Pending update asset changed")
    blob = asset.read_bytes()
    if hashlib.sha256(blob).hexdigest() != manifest["sha256"]:
        raise ValueError("Pending update checksum changed")
    executable = folder / integration.launcher_executable().name
    archive_name = "AstraAntigravity.exe" if manifest["platform"] == "win32" else "AstraAntigravity"
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        expected = hashlib.sha256(archive.read(archive_name)).hexdigest()
    if executable.is_symlink() or hashlib.sha256(executable.read_bytes()).hexdigest() != expected or selected.get("sha256") != expected:
        raise ValueError("Pending application verification failed")
    return selected
