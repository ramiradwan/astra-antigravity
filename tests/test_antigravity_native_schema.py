import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
SCHEMA_PATH = PLUGIN_DIR / "schemas" / "implementation_result.schema.json"


class AntigravityNativeSchemaTests(unittest.TestCase):
    def test_schema_exists_and_contains_only_model_owned_fields(self):
        self.assertTrue(SCHEMA_PATH.is_file(), f"Missing schema: {SCHEMA_PATH}")
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(schema.get("type"), "object")
        self.assertFalse(schema.get("additionalProperties", True))

        expected = {
            "summary",
            "tests_run",
            "tests_passed",
            "remaining_work",
            "risks",
        }
        self.assertEqual(set(schema.get("properties") or {}), expected)
        self.assertEqual(set(schema.get("required") or []), expected)

        for forbidden in (
            "files_changed",
            "commit_sha",
            "worktree",
            "quota_impact",
        ):
            self.assertNotIn(forbidden, schema.get("properties") or {})

    def test_schema_uses_bounded_container_types(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        properties = schema["properties"]

        self.assertEqual(properties["summary"].get("type"), "string")
        self.assertEqual(properties["tests_run"].get("type"), "array")
        self.assertEqual(properties["tests_run"]["items"].get("type"), "string")
        self.assertEqual(properties["tests_passed"].get("type"), ["boolean", "null"])
        self.assertEqual(properties["remaining_work"].get("type"), "array")
        self.assertEqual(properties["remaining_work"]["items"].get("type"), "string")
        self.assertEqual(properties["risks"].get("type"), "array")
        self.assertEqual(properties["risks"]["items"].get("type"), "string")


if __name__ == "__main__":
    unittest.main()
