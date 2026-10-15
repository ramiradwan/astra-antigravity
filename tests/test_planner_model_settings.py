"""Targeted regression tests for Luna/App Server planner model settings and validation."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN_DIR))

import luna_gateway


class PlannerModelSettingsTests(unittest.TestCase):
    def test_default_planner_model_and_effort(self):
        with patch.dict(os.environ, {}, clear=True):
            model, effort = luna_gateway.get_planner_model_settings()
            self.assertEqual(model, "gpt-6-luna")
            self.assertEqual(effort, "low")

    def test_valid_planner_model_override_gpt_6_1_sol(self):
        with patch.dict(os.environ, {"ASTRA_ANTIGRAVITY_PLANNER_MODEL": "gpt-6.1-sol"}, clear=True):
            model, effort = luna_gateway.get_planner_model_settings()
            self.assertEqual(model, "gpt-6.1-sol")
            self.assertEqual(effort, "low")

    def test_valid_planner_model_override_gpt_6_luna(self):
        with patch.dict(os.environ, {"ASTRA_ANTIGRAVITY_PLANNER_MODEL": "gpt-6-luna"}, clear=True):
            model, effort = luna_gateway.get_planner_model_settings()
            self.assertEqual(model, "gpt-6-luna")
            self.assertEqual(effort, "low")

    def test_invalid_planner_model_fails_closed(self):
        for invalid_model in (
            "gpt-5.6-luna",
            "gpt-5.6-sol",
            "gpt-4o",
            "gemini-4-argon-high",
            "claude-3-opus",
            "random-model",
            "",
        ):
            with patch.dict(
                os.environ,
                {"ASTRA_ANTIGRAVITY_PLANNER_MODEL": invalid_model} if invalid_model else {"ASTRA_ANTIGRAVITY_PLANNER_MODEL": ""},
                clear=True,
            ):
                with self.assertRaises(luna_gateway.GatewayError) as ctx:
                    luna_gateway.get_planner_model_settings()
                self.assertIn("Invalid ASTRA_ANTIGRAVITY_PLANNER_MODEL", str(ctx.exception))

    def test_effort_is_always_low_and_rejects_other_values(self):
        with patch.dict(
            os.environ,
            {
                "ASTRA_ANTIGRAVITY_PLANNER_MODEL": "gpt-6-luna",
                "ASTRA_ANTIGRAVITY_PLANNER_EFFORT": "medium",
            },
            clear=True,
        ):
            with self.assertRaises(luna_gateway.GatewayError) as ctx:
                luna_gateway.get_planner_model_settings()
            self.assertIn("must be 'low'", str(ctx.exception))

        with patch.dict(
            os.environ,
            {
                "ASTRA_ANTIGRAVITY_PLANNER_MODEL": "gpt-6-luna",
                "ASTRA_ANTIGRAVITY_PLANNER_EFFORT": "high",
            },
            clear=True,
        ):
            with self.assertRaises(luna_gateway.GatewayError) as ctx:
                luna_gateway.get_planner_model_settings()
            self.assertIn("must be 'low'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
