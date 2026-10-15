import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
MANIFEST_PATH = PLUGIN_DIR / "plugin.json"
sys.path.insert(0, str(PLUGIN_DIR))

import launcher


def manifest_version() -> str:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["version"]


class LauncherDiscoveryTests(unittest.TestCase):
    def test_explicit_overrides_win_over_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uv = root / ("uv.exe" if os.name == "nt" else "uv")
            agy = root / ("agy.exe" if os.name == "nt" else "agy")
            py = root / ("python.exe" if os.name == "nt" else "python")
            for path in (uv, agy, py):
                path.write_text("x", encoding="utf-8")
            with patch.dict(os.environ, {
                "UV_PATH": str(uv),
                "AGY_PATH": str(agy),
                "ASTRA_PYTHON_PATH": str(py),
            }, clear=False), patch("launcher.shutil.which", return_value="/should/not/win"):
                self.assertEqual(launcher.discover_uv(), str(uv.resolve()))
                self.assertEqual(launcher.discover_agy(), str(agy.resolve()))
                self.assertEqual(launcher.discover_python(), str(py.resolve()))

    def test_invalid_explicit_agy_path_has_useful_error(self):
        with patch.dict(os.environ, {"AGY_PATH": "/definitely/missing/agy"}, clear=False):
            with self.assertRaisesRegex(launcher.DiscoveryError, "AGY_PATH is set"):
                launcher.discover_agy(required=True)

    def test_discover_agy_msix_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            msix_agy = root / "Packages" / "OpenAI.Codex_xyz456" / "LocalCache" / "Local" / "agy" / "bin" / "agy.exe"
            msix_agy.parent.mkdir(parents=True, exist_ok=True)
            msix_agy.write_text("dummy", encoding="utf-8")
            with patch.dict(os.environ, {"AGY_PATH": "", "LOCALAPPDATA": str(root)}), \
                 patch("launcher._path_lookup", return_value=None), \
                 patch("os.name", "nt"):
                self.assertEqual(launcher.discover_agy(required=True), str(msix_agy.resolve()))

    def test_missing_uv_error_explains_fallback_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "luna_gateway.py").write_text("pass\n", encoding="utf-8")
            (root / "requirements.lock").write_text(launcher.MCP_REQUIREMENT, encoding="utf-8")
            with patch("launcher.discover_agy", return_value="/agy"), \
                 patch("launcher.discover_uv", return_value=None), \
                 patch("launcher.discover_python", return_value="/python"), \
                 patch("launcher._python_has_mcp", return_value=False):
                with self.assertRaisesRegex(launcher.DiscoveryError, "uv was not found"):
                    launcher.build_server_command(root)

    def test_uv_command_sets_discovered_agy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "luna_gateway.py").write_text("pass\n", encoding="utf-8")
            (root / "requirements.lock").write_text(launcher.MCP_REQUIREMENT, encoding="utf-8")
            with patch("launcher.discover_agy", return_value="/tools/agy"), \
                 patch("launcher.discover_uv", return_value="/tools/uv"), \
                 patch("launcher.discover_python", return_value="/tools/python"):
                command, env = launcher.build_server_command(root)
            self.assertEqual(command[:6], ["/tools/uv", "run", "--no-project", "--no-config",
                                          "--no-env-file", "--isolated"])
            self.assertEqual(command[6:10], ["--python", "/tools/python", "--with-requirements",
                                            str((root / "requirements.lock").resolve())])
            self.assertEqual(command[-2], "python")
            self.assertEqual(env["AGY_PATH"], "/tools/agy")
            self.assertEqual(env["UV_PATH"], "/tools/uv")

    def test_gateway_server_is_an_explicit_portable_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = root / "luna_gateway.py"
            gateway.write_text("pass\n", encoding="utf-8")
            (root / "requirements.lock").write_text(launcher.MCP_REQUIREMENT, encoding="utf-8")
            with patch("launcher.discover_agy", return_value="/tools/agy"), \
                 patch("launcher.discover_uv", return_value="/tools/uv"), \
                 patch("launcher.discover_python", return_value="/tools/python"):
                command, _ = launcher.build_server_command(root, "luna_gateway.py")
            self.assertEqual(command[-1], str(gateway.resolve()))

    def test_uv_startup_rejects_incomplete_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "luna_gateway.py").write_text("pass\n", encoding="utf-8")
            with patch("launcher.discover_agy", return_value=None), \
                 patch("launcher.discover_uv", return_value="/tools/uv"), \
                 patch("launcher.discover_python", return_value="/tools/python"):
                with self.assertRaisesRegex(launcher.DiscoveryError, "dependency lock was not found"):
                    launcher.build_server_command(root)

    def test_launcher_rejects_arbitrary_server_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "arbitrary.py").write_text("pass\n", encoding="utf-8")
            with self.assertRaisesRegex(launcher.DiscoveryError, "Unsupported MCP server target"):
                launcher.build_server_command(root, "arbitrary.py")


class LauncherVersionTests(unittest.TestCase):
    def test_version_success(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = launcher.main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), manifest_version())
        self.assertEqual(stderr.getvalue(), "")

    def test_version_discovery_bypass(self):
        with patch("launcher.discover_agy") as mock_agy, \
             patch("launcher.discover_uv") as mock_uv, \
             patch("launcher.discover_python") as mock_py, \
             patch("launcher._python_has_mcp") as mock_has_mcp, \
             patch("launcher.build_server_command") as mock_build:
            # Success path bypasses all discovery
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(launcher.main(["--version"]), 0)
            mock_agy.assert_not_called()
            mock_uv.assert_not_called()
            mock_py.assert_not_called()
            mock_has_mcp.assert_not_called()
            mock_build.assert_not_called()

            # Manifest-error path also bypasses all discovery
            with patch("launcher.MANIFEST_PATH", Path("/nonexistent/manifest.json")):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(launcher.main(["--version"]), 2)
            mock_agy.assert_not_called()
            mock_uv.assert_not_called()
            mock_py.assert_not_called()
            mock_has_mcp.assert_not_called()
            mock_build.assert_not_called()

    def test_version_manifest_errors(self):
        cases = [
            (None, "Plugin manifest not found"),
            ("{bad json", "Plugin manifest is not valid JSON"),
            (json.dumps(["not", "dict"]), "root must be a JSON object"),
            (json.dumps({"name": "test"}), "missing 'version' field"),
            (json.dumps({"version": 123}), "must be a non-empty string"),
            (json.dumps({"version": ""}), "must be a non-empty string"),
            (json.dumps({"version": "   "}), "must be a non-empty string"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            manifest_file = Path(tmp) / "plugin.json"
            for content, expected_msg in cases:
                with self.subTest(content=content):
                    if content is None:
                        target_path = Path(tmp) / "missing.json"
                    else:
                        manifest_file.write_text(content, encoding="utf-8")
                        target_path = manifest_file
                    stderr = io.StringIO()
                    with patch("launcher.MANIFEST_PATH", target_path), contextlib.redirect_stderr(stderr):
                        code = launcher.main(["--version"])
                    self.assertEqual(code, 2)
                    err = stderr.getvalue()
                    self.assertIn(expected_msg, err)
                    self.assertNotIn("Traceback", err)

    def test_version_subprocess_integration(self):
        proc = subprocess.run(
            [sys.executable, str(PLUGIN_DIR / "launcher.py"), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), manifest_version())
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
