"""Build a reproducible, explicitly allowlisted standalone plugin ZIP.

No provider credentials, model inference, network access or repository mutation.
The report separates package validation from external publication prerequisites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "astra-antigravity"
FORBIDDEN_PARTS = {".git", ".env", "__pycache__", ".venv", "node_modules", "state"}
SECRET_PATTERNS = (
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b",
    rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b",
    rb"\bAIza[A-Za-z0-9_-]{35}\b",
)


class ReleaseError(ValueError):
    pass


def read_package(plugin: Path, entries: list[str]) -> dict[str, bytes]:
    plugin = plugin.resolve()
    files = {}
    for name in entries:
        if not isinstance(name, str):
            raise ReleaseError("Release paths must be strings")
        path = PurePosixPath(name)
        if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                or path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/"))
                or any(part in FORBIDDEN_PARTS or part.startswith(".env") for part in path.parts)
                or path.suffix.lower() in {".log", ".sqlite3", ".pyc", ".pem", ".key"}
                or name in files):
            raise ReleaseError("Unsafe or duplicate release path")
        source = plugin.joinpath(*path.parts)
        if any(item.is_symlink() for item in (source, *source.parents) if item != plugin.parent):
            raise ReleaseError(f"Symlink in release path: {name}")
        if not source.resolve().is_relative_to(plugin) or not source.is_file():
            raise ReleaseError(f"Missing or escaped release file: {name}")
        data = source.read_bytes()
        # Every allowed asset is text. Canonical LF makes Windows/Linux archives
        # byte-identical even when Git's autocrlf setting differs.
        try:
            data = data.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReleaseError(f"Non-text release asset: {name}") from exc
        if any(re.search(pattern, data) for pattern in SECRET_PATTERNS):
            # Never echo matched credential values.
            raise ReleaseError(f"Credential-shaped content in release file: {name}")
        files[name] = data
    return dict(sorted(files.items()))


def _relative_asset(value: str, files: dict[str, bytes]) -> bytes:
    if not isinstance(value, str) or not value.startswith("./") or ".." in value.split("/"):
        raise ReleaseError("Manifest asset must be a package-relative path")
    name = value[2:]
    if name not in files:
        raise ReleaseError(f"Manifest asset was omitted from the release: {name}")
    return files[name]


def contrast(first: str, second: str) -> float:
    def luminance(color):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ReleaseError("Brand colors require six-digit hex values")
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    values = sorted((luminance(first), luminance(second)))
    return (values[1] + 0.05) / (values[0] + 0.05)


def validate(files: dict[str, bytes]) -> dict:
    required = {"plugin.json", "mcp.json", "LICENSE", "launcher.py",
                "luna_gateway.py", "requirements.lock", "docs/PRIVACY.md", "docs/TERMS.md"}
    if not required.issubset(files):
        raise ReleaseError("Incomplete plugin package")
    if any(name.startswith(".codex-plugin/") or name == ".mcp.json" for name in files):
        raise ReleaseError("Legacy manifests are not part of this portable package")
    manifest = json.loads(files["plugin.json"])
    if manifest.get("$schema") != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json":
        raise ReleaseError("Agent Plugins 1.0.0 manifest schema is required")
    if set(manifest) - {"$schema", "name", "version", "description", "author", "homepage",
                        "repository", "license", "keywords", "extensions"}:
        raise ReleaseError("Non-portable fields in the root plugin manifest")
    if manifest.get("license") != "MIT" or b"MIT License" not in files["LICENSE"]:
        raise ReleaseError("MIT manifest/license mismatch")
    if not re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")):
        raise ReleaseError("Release version must be numeric semver")
    if not manifest.get("author", {}).get("name") or not manifest.get("description"):
        raise ReleaseError("Publisher identity and description are required")
    openai = manifest["extensions"]["com.openai"]
    if openai.get("requires_local_executor") is not True:
        raise ReleaseError("The current backend requires local execution")
    interface = openai["interface"]
    for field, limit in (("displayName", 30), ("shortDescription", 30),
                         ("longDescription", 4000), ("developerName", 80)):
        if not isinstance(interface.get(field), str) or not 0 < len(interface[field]) <= limit:
            raise ReleaseError(f"Invalid listing field: {field}")
    for field in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        if not interface.get(field, "").startswith("https://"):
            raise ReleaseError(f"Public HTTPS URL required: {field}")
    for field, background in (("brandColor", "#FFFFFF"), ("brandColorDark", "#212121")):
        if contrast(interface[field], background) < 2:
            raise ReleaseError(f"Insufficient listing contrast: {field}")
    for field in ("logo", "composerIcon", "logoDark", "composerIconDark"):
        data = _relative_asset(interface[field], files)
        if len(data) > 5 * 1024 * 1024:
            raise ReleaseError("Icon exceeds 5 MiB")
        svg = ET.fromstring(data)
        width, height = float(svg.attrib["width"]), float(svg.attrib["height"])
        view = [float(v) for v in svg.attrib["viewBox"].split()]
        if (svg.tag != "{http://www.w3.org/2000/svg}svg"
                or not all(math.isfinite(value) for value in (width, height, *view))
                or width != height or width < 48 or len(view) != 4 or view[2] != view[3] or view[2] < 48):
            raise ReleaseError("Icon must be a square SVG at least 48px")
        if any(element.tag.split("}")[-1] in {"script", "foreignObject", "image"}
               or any("href" in key or key.lower().startswith("on") or "url(" in value.lower()
                      or "@import" in value.lower() or value.lower().startswith("http")
                      for key, value in element.attrib.items()) for element in svg.iter()):
            raise ReleaseError("Icon contains active or external content")
    _relative_asset("./skills/setup-antigravity/SKILL.md", files)
    config = json.loads(files["mcp.json"])
    if (config.get("$schema") != "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
            or set(config) != {"$schema", "mcpServers"}):
        raise ReleaseError("Agent Plugins 1.0.0 MCP schema is required")
    if set(config["mcpServers"]) != {"luna_gateway"}:
        raise ReleaseError("Only the parent gateway may be exposed")
    gateway = config["mcpServers"]["luna_gateway"]
    if (gateway.get("type") != "stdio" or gateway.get("command") != "python"
            or gateway.get("args") != ["${PLUGIN_ROOT}/launcher.py", "luna_gateway.py"]
            or gateway.get("cwd") != "${PLUGIN_ROOT}"
            or set(gateway) - {"type", "command", "args", "env", "cwd"}):
        raise ReleaseError("Unexpected portable gateway launch configuration")
    _relative_asset(openai["onboardingSkill"], files)
    if b"mcp==1.29.1" not in files["requirements.lock"] or b"--hash=sha256:" not in files["requirements.lock"]:
        raise ReleaseError("Missing hashed MCP runtime lock")
    return manifest


def build(plugin: Path, output: Path, entries: list[str]) -> dict:
    files = read_package(plugin, entries)
    manifest = validate(files)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"astra-antigravity-{manifest['version']}"
    archive = output / f"{stem}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zipped:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            zipped.writestr(info, data)
    inventory = {"plugin": manifest["name"], "version": manifest["version"], "license": "MIT",
                 "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                           for name, data in files.items()],
                 "runtime_dependency_lock_sha256": hashlib.sha256(files["requirements.lock"]).hexdigest()}
    inventory_path = output / f"{stem}.inventory.json"
    inventory_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8", newline="\n")
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
                                 for path in (archive, inventory_path)), encoding="utf-8", newline="\n")
    return {"version": manifest["version"], "archive": str(archive), "file_count": len(files),
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "package_valid": True, "official_submission_ready": False,
            "external_gates": ["public listing/support/privacy/terms URLs", "publisher identity/access verification",
                               "stable public HTTPS MCP endpoint with authenticated per-user execution",
                               "third-party integration eligibility confirmation",
                               "dedicated reviewer access and real accessible demo recording",
                               "publisher privacy/support operational facts"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args(argv)
    if args.require_clean:
        status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                                text=True, check=True).stdout
        if status:
            raise ReleaseError("Release builds require a clean committed checkout")
    entries = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))
    result = build(PLUGIN, args.output, entries)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, KeyError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"Release validation failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
