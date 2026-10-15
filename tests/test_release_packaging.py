import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_builder", ROOT / "scripts/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
ENTRIES = json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))


class ReleasePackagingTests(unittest.TestCase):
    def test_archive_is_reproducible_and_excludes_runtime_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = root / "plugin"
            shutil.copytree(release.PLUGIN, plugin)
            (plugin / "unexpected.log").write_text("private history", encoding="utf-8")
            (plugin / ".env").write_text("PRIVATE=not-packaged", encoding="utf-8")
            first = release.build(plugin, root / "first", ENTRIES)
            # Different source mtime and CRLF must not alter distributed bytes.
            readme = plugin / "README.md"
            readme.write_bytes(readme.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            os.utime(readme, (1234567890, 1234567890))
            second = release.build(plugin, root / "second", ENTRIES)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(Path(first["archive"]).read_bytes(), Path(second["archive"]).read_bytes())
            with zipfile.ZipFile(first["archive"]) as zipped:
                self.assertEqual(set(zipped.namelist()), set(ENTRIES))
                self.assertNotIn("unexpected.log", zipped.namelist())
                self.assertNotIn(".env", zipped.namelist())
                self.assertEqual(zipped.read("LICENSE"), (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n"))
            self.assertTrue(first["package_valid"])
            self.assertFalse(first["official_submission_ready"])
            self.assertTrue(first["external_gates"])

    def test_traversal_duplicates_and_secret_files_are_rejected(self):
        for name in ("../LICENSE", "/LICENSE", "C:/secret", "docs\\secret", ".env", "state/history.log", "docs/./TERMS.md", 123):
            with self.subTest(name=name), self.assertRaises(release.ReleaseError):
                release.read_package(release.PLUGIN, [name])
        with self.assertRaises(release.ReleaseError):
            release.read_package(release.PLUGIN, ["LICENSE", "LICENSE"])

    def test_secret_scan_rejects_without_printing_value(self):
        with tempfile.TemporaryDirectory() as raw:
            plugin = Path(raw)
            secret = "gh" + "p_" + "x" * 40
            (plugin / "test.txt").write_text(secret, encoding="utf-8")
            with self.assertRaises(release.ReleaseError) as caught:
                release.read_package(plugin, ["test.txt"])
            self.assertNotIn(secret, str(caught.exception))

    def test_omitted_runtime_and_active_icon_are_rejected(self):
        files = release.read_package(release.PLUGIN, ENTRIES)
        missing = dict(files)
        del missing["luna_gateway.py"]
        with self.assertRaises(release.ReleaseError):
            release.validate(missing)
        changed = dict(files)
        changed["assets/icon.svg"] = files["assets/icon.svg"].replace(b"</svg>", b"<script>unsafe</script></svg>")
        with self.assertRaises(release.ReleaseError):
            release.validate(changed)

    def test_inline_event_handlers_and_invisible_brand_colors_are_rejected(self):
        files = release.read_package(release.PLUGIN, ENTRIES)
        changed = dict(files)
        changed["assets/icon.svg"] = files["assets/icon.svg"].replace(b"width=", b'onload="unsafe" width=', 1)
        with self.assertRaises(release.ReleaseError):
            release.validate(changed)
        changed = dict(files)
        manifest = json.loads(files["plugin.json"])
        manifest["extensions"]["com.openai"]["interface"]["brandColor"] = "#FFFFFF"
        changed["plugin.json"] = json.dumps(manifest).encode()
        with self.assertRaises(release.ReleaseError):
            release.validate(changed)

    def test_allowlist_covers_runtime_modules_and_referenced_resources(self):
        files = release.read_package(release.PLUGIN, ENTRIES)
        release.validate(files)
        for module in release.PLUGIN.glob("*.py"):
            self.assertIn(module.name, files)
        for schema in (release.PLUGIN / "schemas").glob("*.json"):
            self.assertIn("schemas/" + schema.name, files)


if __name__ == "__main__":
    unittest.main()
