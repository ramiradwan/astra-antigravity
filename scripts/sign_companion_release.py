"""Sign a reviewed companion ZIP with a publisher-owned Ed25519 PEM key.

The key is read only inside this process and is never printed or copied.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def sign(archive, key_path, *, version, sequence, url, platform="win32", installer=None, installer_url=None):
    key = load_pem_private_key(Path(key_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The release signing key must be Ed25519")
    blob = Path(archive).read_bytes()
    value = {"schema": 1, "sequence": sequence, "version": version, "platform": platform,
        "url": url, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest(), "state_schema": 1}
    if bool(installer) != bool(installer_url):
        raise ValueError("Provide both the standalone installer and its public URL")
    if installer:
        executable = Path(installer).read_bytes()
        value["installer"] = {"url": installer_url, "size": len(executable), "sha256": hashlib.sha256(executable).hexdigest()}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps({"manifest": base64.b64encode(raw).decode(),
        "signature": base64.b64encode(key.sign(raw)).decode()}, sort_keys=True).encode() + b"\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--platform", default="win32")
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--installer-url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to replace an existing signed manifest")
    args.output.write_bytes(sign(args.archive, args.private_key, version=args.version,
        sequence=args.sequence, url=args.url, platform=args.platform, installer=args.installer, installer_url=args.installer_url))
