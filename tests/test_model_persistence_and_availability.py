"""Targeted regression tests for Gemini model persistence, legacy pins, and exact slug availability."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import luna_gateway
import model_availability
from model_availability import check_model_availability, parse_model_slugs
from server import DEFAULT_EFFORT, DEFAULT_MODEL, LEGACY_DEFAULT_EFFORT, LEGACY_DEFAULT_MODEL, TaskRecord


class ModelPersistenceAndAvailabilityTests(unittest.TestCase):
    def test_centralized_argon_defaults(self):
        self.assertEqual(DEFAULT_MODEL, "gemini-4-argon-high")
        self.assertEqual(DEFAULT_EFFORT, "high")
        self.assertEqual(LEGACY_DEFAULT_MODEL, "gemini-3.8-flash-high")
        self.assertEqual(LEGACY_DEFAULT_EFFORT, "high")

    def test_task_record_persistence_and_legacy_retention(self):
        # 1. New record with default model
        rec = TaskRecord(
            task_id="task-new",
            workspace="/test",
            status="running",
            created_at=100.0,
            updated_at=100.0,
            server_instance_id="inst-1",
            log_path="/test/log",
            meta_path="/test/meta",
            model=DEFAULT_MODEL,
            effort=DEFAULT_EFFORT,
        )
        d = rec.to_dict()
        self.assertEqual(d["model"], "gemini-4-argon-high")
        self.assertEqual(d["effort"], "high")

        # 2. Deserializing legacy record without model/effort fields retains Flash High
        legacy_dict = {
            "task_id": "task-legacy",
            "workspace": "/test",
            "status": "completed",
            "created_at": 50.0,
            "updated_at": 50.0,
            "server_instance_id": "inst-1",
            "log_path": "/test/log",
            "meta_path": "/test/meta",
        }
        loaded_legacy = TaskRecord.from_dict(legacy_dict)
        self.assertEqual(loaded_legacy.model, "gemini-3.8-flash-high")
        self.assertEqual(loaded_legacy.effort, "high")

        # 3. Explicit model is preserved across round-trip
        custom_dict = {
            "task_id": "task-custom",
            "workspace": "/test",
            "status": "completed",
            "created_at": 60.0,
            "updated_at": 60.0,
            "server_instance_id": "inst-1",
            "log_path": "/test/log",
            "meta_path": "/test/meta",
            "model": "gemini-4-pro",
            "effort": "low",
        }
        loaded_custom = TaskRecord.from_dict(custom_dict)
        self.assertEqual(loaded_custom.model, "gemini-4-pro")
        self.assertEqual(loaded_custom.effort, "low")
        self.assertEqual(loaded_custom.to_dict()["model"], "gemini-4-pro")
        self.assertEqual(loaded_custom.to_dict()["effort"], "low")

    def test_parse_model_slugs_json_and_tabular(self):
        # JSON list
        json_list = json.dumps(["gemini-4-argon-high", "gemini-3.8-flash-high", "claude-3-5-sonnet"])
        slugs = parse_model_slugs(json_list)
        self.assertIn("gemini-4-argon-high", slugs)
        self.assertIn("gemini-3.8-flash-high", slugs)

        # JSON dictionary with items
        json_dict = json.dumps({
            "models": [
                {"id": "gemini-4-argon-high", "name": "Gemini 4 Argon High"},
                {"slug": "gemini-4-pro", "name": "Gemini 4 Pro"},
            ]
        })
        slugs_dict = parse_model_slugs(json_dict)
        self.assertIn("gemini-4-argon-high", slugs_dict)
        self.assertIn("gemini-4-pro", slugs_dict)

        # Tabular / plain text output
        tabular = """
        AVAILABLE MODELS
        ------------------------------------
        gemini-4-argon-high   active   Google
        gemini-3.8-flash-high active   Google
        claude-sonnet-4       active   Anthropic
        """
        slugs_tab = parse_model_slugs(tabular)
        self.assertIn("gemini-4-argon-high", slugs_tab)
        self.assertIn("gemini-3.8-flash-high", slugs_tab)
        self.assertIn("claude-sonnet-4", slugs_tab)
        self.assertNotIn("AVAILABLE", slugs_tab)

    def test_check_model_availability_exact_match(self):
        def fake_find_agy():
            return "fake-agy"

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "gemini-4-argon-high\ngemini-3.8-flash-high\n"
        fake_proc.stderr = ""

        with patch.object(model_availability, "run_probe", return_value=fake_proc):
            self.assertTrue(check_model_availability("gemini-4-argon-high", fake_find_agy))
            self.assertFalse(check_model_availability("gemini-nonexistent", fake_find_agy))

    def test_check_model_availability_failure_raises(self):
        def fake_find_agy():
            return "fake-agy"

        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stdout = ""
        fake_proc.stderr = "Error: daemon disconnected"

        with patch.object(model_availability, "run_probe", return_value=fake_proc):
            with self.assertRaisesRegex(RuntimeError, "'agy models' exited with code 1"):
                check_model_availability("gemini-4-argon-high", fake_find_agy)


class DelegationAvailabilityEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_model_fails_before_worktree_and_planner_no_fallback(self):
        coord, planner = MagicMock(), AsyncMock()
        with patch.object(luna_gateway, "coordinator", coord), \
             patch.object(luna_gateway, "_run_luna_plan", planner), \
             patch.object(luna_gateway, "get_available_models", return_value={"claude-opus-5-5-thinking"}):
            result = await luna_gateway.delegate_via_luna("Implement", "/repo", MagicMock(), reasoning="heavy", delivery="foreground")
        self.assertEqual(result["status"], "model_unavailable")
        self.assertIn("Argon", result["message"])
        self.assertEqual(result["model_selection"]["retry"]["model"], "opus-5.5")
        coord.delegate.assert_not_called()
        planner.assert_not_called()

    async def test_continuation_uses_pinned_task_model_availability(self):
        coord, planner = MagicMock(), AsyncMock()
        record = MagicMock(model="gemini-3.8-flash-high", task_type="implementation", required_capabilities=[])
        coord.base.get_task.return_value = record
        coord.status.return_value = {"effective_workspace": "/repo"}
        with patch.object(luna_gateway, "coordinator", coord), \
             patch.object(luna_gateway, "_run_luna_plan", planner), \
             patch.object(luna_gateway, "get_available_models", return_value=set()):
            result = await luna_gateway.continue_via_luna("task-legacy", "Fix", MagicMock(), delivery="foreground")
        self.assertEqual(result["status"], "model_unavailable")
        self.assertIn("gemini-3.8-flash-high", result["message"])
        coord.steer.assert_not_called()
        planner.assert_not_called()
