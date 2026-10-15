import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bundle_runtime as bundle


def snapshot(path, entries=None, hashes=None):
    files = entries or {"site-packages/mcp/__init__.py": b"test fixture\n"}
    info = {"format_version": 1, "tag": "cpython-313-win-amd64", "sdk_version": "1.29.1",
            "lock_sha256": bundle.sha(bundle.LOCK.read_bytes().replace(b"\r\n", b"\n")),
            "packages": [], "files": hashes or {name: bundle.sha(data) for name, data in files.items()}}
    bundle.archive(path, {**files, "runtime-info.json": json.dumps(info).encode()})


class RuntimeBundleTests(unittest.TestCase):
    def test_crypto_format_constant_is_not_key_material(self):
        header = b"-----" + b"BEGIN OPENSSH PRIVATE KEY-----"
        self.assertFalse(bundle.dependency_secret(b'FORMAT = "' + header + b'"'))
        self.assertTrue(bundle.dependency_secret(header + b"\n" + b"A" * 60))

    def test_native_payload_hashes_are_checked(self):
        with tempfile.TemporaryDirectory() as raw:
            archive = Path(raw) / "runtime.zip"
            snapshot(archive)
            info, files = bundle.load_snapshot(archive)
            self.assertEqual(info["sdk_version"], "1.29.1")
            snapshot(archive, hashes={"site-packages/mcp/__init__.py": "0" * 64})
            with self.assertRaises(bundle.ReleaseError):
                bundle.load_snapshot(archive)

    def test_traversal_and_symlinks_cannot_enter_plugin_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            archive = Path(raw) / "runtime.zip"
            snapshot(archive, entries={"site-packages/../../luna_gateway.py": b"bad"})
            with self.assertRaises(bundle.ReleaseError):
                bundle.load_snapshot(archive)
            with zipfile.ZipFile(archive, "w") as zipped:
                info = zipfile.ZipInfo("site-packages/link")
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                zipped.writestr(info, "outside")
            with self.assertRaises(bundle.ReleaseError):
                bundle.load_snapshot(archive)

    def test_partial_profiles_require_an_explicit_test_mode(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtimes = root / "runtimes"
            runtimes.mkdir()
            snapshot(runtimes / "runtime-cpython-313-win-amd64.zip")
            with self.assertRaises(bundle.ReleaseError):
                bundle.bundle(runtimes, root / "release")
            report = bundle.bundle(runtimes, root / "release", require_all=False)
            self.assertFalse(report["official_submission_ready"])
            with zipfile.ZipFile(report["archive"]) as zipped:
                config = json.loads(zipped.read("mcp.json"))
                self.assertEqual(config["mcpServers"]["luna_gateway"]["args"],
                                 ["-I", "${PLUGIN_ROOT}/runtime_bootstrap.py", "luna_gateway.py"])
                self.assertIn("RUNTIME.md", zipped.namelist())
                self.assertIn("runtime/cpython-313-win-amd64/runtime-info.json", zipped.namelist())


if __name__ == "__main__":
    unittest.main()
