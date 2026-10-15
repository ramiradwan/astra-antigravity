"""Prepare one GitHub draft from six checksum-verified release artifacts.

Read-only by default. --apply creates a draft or updates its target/assets while
preserving reviewed notes and extra media. Published or ambiguous tags fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


class DraftError(ValueError):
    pass


def core_artifacts(folder, version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise DraftError("Expected a numeric release version")
    source = f"astra-antigravity-{version}"
    bundled = source + "-bundled-runtime"
    names = [source+".zip", source+".inventory.json", "SHA256SUMS",
             bundled+".zip", bundled+".inventory.json", "SHA256SUMS.runtime"]
    if any((folder/name).is_symlink() or not (folder/name).is_file() for name in names):
        raise DraftError("Expected six regular release artifact files")
    for checksum, prefix in (("SHA256SUMS", source), ("SHA256SUMS.runtime", bundled)):
        expected = {prefix+".zip", prefix+".inventory.json"}
        seen = set()
        for row in (folder/checksum).read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", row)
            if not match or match[2] not in expected or match[2] in seen:
                raise DraftError("Unexpected checksum path or duplicate")
            path = folder/match[2]
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != match[1]:
                raise DraftError("Release artifact checksum mismatch")
            seen.add(match[2])
        if seen != expected:
            raise DraftError("Incomplete release checksums")
    for name in (source+".zip", bundled+".zip"):
        with zipfile.ZipFile(folder/name) as archive:
            manifest = json.loads(archive.read("plugin.json"))
            if manifest.get("$schema") != "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json":
                raise DraftError("Release archive must use the portable Agent Plugins manifest")
            if manifest.get("version") != version:
                raise DraftError("Archive version differs from the release tag")
    return {name: hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in names}


def gh_api(endpoint, *, method="GET", body=None, paginate=False):
    command = ["gh", "api", endpoint, "--method", method]
    if paginate:
        command += ["--paginate", "--slurp"]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    if result.returncode:
        raise DraftError("GitHub API operation failed; inspect the authenticated CLI privately")
    return json.loads(result.stdout)


def upload_assets(repo, tag, paths):
    result = subprocess.run(["gh", "release", "upload", tag, *map(str, paths),
                             "--repo", repo, "--clobber"], capture_output=True,
                            text=True, encoding="utf-8", timeout=300)
    if result.returncode:
        raise DraftError("Draft asset upload failed; the release remains a draft")


def require_draft(release, tag):
    if release.get("draft") is not True or release.get("tag_name") != tag:
        raise DraftError("Refusing to mutate a published or changed release")


def prepare(repo, commit, version, folder, notes, *, apply=False, api=gh_api, upload=upload_assets):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not re.fullmatch(r"[a-f0-9]{40}", commit)):
        raise DraftError("Provide an explicit repository and full commit SHA")
    hashes = core_artifacts(folder, version)
    tag = "v"+version
    endpoint = f"repos/{repo}/releases"
    pages = api(endpoint, paginate=True)
    matches = [release for page in pages for release in page if release.get("tag_name") == tag]
    if len(matches) > 1:
        raise DraftError("Multiple releases share this tag; resolve the ambiguity manually")
    release = matches[0] if matches else None
    if release:
        require_draft(release, tag)
    uploaded = {item["name"]: item.get("digest") for item in (release or {}).get("assets", [])}
    changed = [name for name, digest in hashes.items() if uploaded.get(name) != "sha256:"+digest]
    plan = {"tag": tag, "action": "update draft" if release else "create draft",
            "release_id": (release or {}).get("id"), "core_assets_to_upload": changed,
            "applied": False, "preserves_extra_assets_and_reviewed_notes": True}
    if not apply:
        return plan
    if release:
        identity = endpoint+"/"+str(release["id"])
        release = api(identity)
        require_draft(release, tag)
        if release.get("target_commitish") != commit:
            release = api(identity, method="PATCH", body={"tag_name": tag, "target_commitish": commit})
    else:
        release = api(endpoint, method="POST", body={"tag_name": tag, "target_commitish": commit,
            "name": "Delegate to Antigravity "+version, "body": notes.read_text(encoding="utf-8"), "draft": True})
    require_draft(release, tag)
    uploaded = {item["name"]: item.get("digest") for item in release.get("assets", [])}
    changed = [name for name, digest in hashes.items() if uploaded.get(name) != "sha256:"+digest]
    if changed:
        require_draft(api(endpoint+"/"+str(release["id"])), tag)
        upload(repo, tag, [folder/name for name in changed])
    return {**plan, "core_assets_to_upload": changed, "release_id": release["id"], "applied": True, "draft": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--notes-file", type=Path, default=Path("CHANGELOG.md"), help="Used only for a new draft")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.repo, args.commit, args.version, args.artifacts, args.notes_file,
                                 apply=args.apply), indent=2))
    except (DraftError, OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"Draft preparation failed: {exc}")
        raise SystemExit(1)
