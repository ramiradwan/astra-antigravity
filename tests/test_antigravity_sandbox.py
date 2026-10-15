import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_DIR / "plugins" / "astra-antigravity"


def load_server():
    sys.path.insert(0, str(PLUGIN_DIR))
    try:
        spec = importlib.util.spec_from_file_location("server_sandbox_test", PLUGIN_DIR / "server.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class TestAntigravitySandboxPolicy(unittest.TestCase):
    def setUp(self):
        self.server = load_server()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_sandbox_enabled_by_default_and_for_non_false_values(self):
        for value in (None, "", "1", "true", "TRUE", " yes ", "on", "ON", "anything"):
            with self.subTest(value=value):
                env = {} if value is None else {"ASTRA_ANTIGRAVITY_SANDBOX": value}
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertTrue(self.server.antigravity_sandbox_enabled())

    def test_sandbox_disabled_only_for_explicit_false_values(self):
        for value in ("0", "false", "FALSE", " no ", "off", "OFF"):
            with self.subTest(value=value):
                with mock.patch.dict(
                    os.environ,
                    {"ASTRA_ANTIGRAVITY_SANDBOX": value},
                    clear=True,
                ):
                    self.assertFalse(self.server.antigravity_sandbox_enabled())

    def test_base_command_adds_sandbox_by_default_and_omits_it_on_opt_out(self):
        manager = self.server.TaskManager(state_dir=Path(self.tmp.name) / "state")
        with mock.patch.object(self.server, "find_agy", return_value="agy"):
            with mock.patch.dict(os.environ, {}, clear=True):
                enabled = manager._base_command("/workspace")
            with mock.patch.dict(
                os.environ,
                {"ASTRA_ANTIGRAVITY_SANDBOX": "0"},
                clear=True,
            ):
                disabled = manager._base_command("/workspace")

        self.assertEqual(enabled.count("--sandbox"), 1)
        self.assertNotIn("--sandbox", disabled)
        for required in (
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--json-schema",
        ):
            self.assertIn(required, enabled)
            self.assertIn(required, disabled)


if __name__ == "__main__":
    unittest.main()
