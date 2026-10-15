import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import server
from server import TaskManager, TaskRecord


class AntigravityNativeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.manager = TaskManager(self.root / "state")
        self.record = TaskRecord(
            task_id="task-native-cli",
            workspace=str(self.workspace),
            status="started",
            created_at=time.time(),
            updated_at=time.time(),
            server_instance_id=server.SERVER_INSTANCE_ID,
            log_path=str(self.root / "state" / "task.log"),
            meta_path=str(self.root / "state" / "task.json"),
            conversation_id="conversation-native-cli",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _assert_schema_args(self, cmd):
        self.assertIn("--json-schema", cmd)
        schema_arg = Path(cmd[cmd.index("--json-schema") + 1])
        self.assertTrue(schema_arg.is_absolute())
        self.assertEqual(
            schema_arg,
            PLUGIN_DIR / "schemas" / "implementation_result.schema.json",
        )
        self.assertTrue(schema_arg.is_file())
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")

    def test_persistent_session_uses_native_structured_result_schema(self):
        sentinel = object()
        with patch("server.find_agy", return_value="agy"), patch(
            "server.subprocess.Popen", return_value=sentinel
        ) as popen:
            got = self.manager._launch_session(self.record)

        self.assertIs(got, sentinel)
        cmd = popen.call_args.args[0]
        self._assert_schema_args(cmd)
        self.assertIn("--input-format", cmd)
        self.assertEqual(cmd[cmd.index("--input-format") + 1], "stream-json")
        self.assertNotIn("-p", cmd)

    def test_fallback_turn_uses_same_native_structured_result_schema(self):
        sentinel = object()
        with patch("server.find_agy", return_value="agy"), patch(
            "server.subprocess.Popen", return_value=sentinel
        ) as popen:
            got = self.manager._launch_fallback_turn(self.record, "continue")

        self.assertIs(got, sentinel)
        cmd = popen.call_args.args[0]
        self._assert_schema_args(cmd)
        self.assertIn("-p", cmd)
        self.assertIn("--conversation", cmd)
        self.assertEqual(
            cmd[cmd.index("--conversation") + 1], "conversation-native-cli"
        )

    def test_each_non_coding_schema_is_used_by_persistent_and_cold_turns(self):
        for kind in ("analysis", "research", "document"):
            self.record.task_type = kind
            for launch in (lambda: self.manager._launch_session(self.record),
                           lambda: self.manager._launch_fallback_turn(self.record, "Continue")):
                with patch("server.find_agy", return_value="agy"), patch("server.subprocess.Popen") as popen:
                    launch()
                command = popen.call_args.args[0]
                self.assertEqual(Path(command[command.index("--json-schema") + 1]),
                                 PLUGIN_DIR / "schemas" / f"{kind}_result.schema.json")

    def test_unknown_saved_type_fails_before_launch_instead_of_using_implementation_schema(self):
        self.record.task_type = "unknown"
        with patch("server.find_agy", return_value="agy"), patch("server.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "task_type"):
                self.manager._launch_session(self.record)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
