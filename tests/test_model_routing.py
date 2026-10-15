import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "astra-antigravity"))
import luna_gateway as gateway
import model_routing
import quota_server
from test_luna_gateway_runtime import FakeCoordinator, FakeContext, PLAN

ALL = {"claude-sonnet-5-5", "gemini-3.8-flash-high", "gemini-4-argon-high", "claude-opus-5-5-thinking"}


class RoutingTests(unittest.TestCase):
    def test_light_prefers_sonnet_then_advertised_gemini_high(self):
        self.assertEqual(model_routing.select_model("light", ALL)["model"], "claude-sonnet-5-5")
        choice = model_routing.select_model("light", {"gemini-3.8-flash-high"})
        self.assertEqual(choice["model"], "gemini-3.8-flash-high")
        self.assertIn("Sonnet 5.5", choice["notice"])

    def test_missing_argon_offers_opus_without_selecting_it(self):
        choice = model_routing.select_model("heavy", ALL - {"gemini-4-argon-high"})
        self.assertFalse(choice["available"])
        self.assertIn("Argon", choice["message"])
        self.assertIn("Opus 5.5", choice["message"])
        self.assertEqual(choice["offered_fallback"]["model"], "claude-opus-5-5-thinking")
        self.assertEqual(choice["retry"], {"model": "opus-5.5", "reasoning": "heavy"})

    def test_both_heavy_models_missing_and_old_claude_is_not_a_substitute(self):
        choice = model_routing.select_model("heavy", {"claude-opus-4-6-thinking"})
        self.assertFalse(choice["offered_fallback"]["available"])
        self.assertIsNone(choice["retry"])
        self.assertIn("does not advertise Opus 5.5", choice["message"])

    def test_explicit_choices_and_settings_never_drift(self):
        self.assertEqual(model_routing.select_model("heavy", ALL, model="opus-5.5")["model"], "claude-opus-5-5-thinking")
        self.assertFalse(model_routing.select_model("light", {"gemini-3.8-flash-high"}, model="sonnet-5.5")["available"])
        with patch.dict(os.environ, {"ASTRA_ANTIGRAVITY_HEAVY_MODEL": "opus-5.5"}):
            self.assertEqual(model_routing.select_model("heavy", ALL)["profile"], "opus-5.5")
        with patch.dict(os.environ, {"ASTRA_ANTIGRAVITY_LIGHT_MODEL": "opus-5.5"}):
            with self.assertRaises(ValueError):
                model_routing.preferences()

    def test_only_exact_advertised_new_version_slug_is_launched(self):
        choice = model_routing.select_model("light", {"claude-sonnet-5.5-thinking"})
        self.assertEqual(choice["model"], "claude-sonnet-5.5-thinking")
        self.assertFalse(model_routing.select_model("light", {"claude-sonnet-4-6"})["available"])

    def test_claude_quota_uses_its_pool_and_excludes_other_models(self):
        buckets = {
            "gemini-5h": {"remaining_fraction": 0.01},
            "shared-5h": {"group": "Claude and GPT models", "remaining_fraction": 0.8},
            "claude-sonnet-5-5-5h": {"remaining_fraction": 0.4},
            "claude-opus-5-5-weekly": {"remaining_fraction": 0.5},
            "claude-opus-4-6-weekly": {"remaining_fraction": 0.01},
            "gpt-oss-5h": {"remaining_fraction": 0.01}}
        self.assertEqual(set(quota_server._relevant_buckets(buckets, "claude-opus-5-5-thinking")),
                         {"shared-5h", "claude-opus-5-5-weekly"})


class GatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_heavy_uses_existing_planner_once_and_returns_offer_without_start(self):
        coord, ctx = FakeCoordinator(), FakeContext()
        planner = AsyncMock(return_value={**PLAN, "reasoning_requirement": "heavy"})
        with patch.object(gateway, "coordinator", coord), patch.object(gateway, "_run_luna_plan", planner), \
             patch.object(gateway, "get_available_models", return_value=ALL - {"gemini-4-argon-high"}) as catalog:
            result = await gateway.delegate_via_luna("Resolve concurrency design", "/repo", ctx, delivery="foreground")
        self.assertEqual(result["status"], "model_unavailable")
        self.assertFalse(result["implementation_started"])
        self.assertEqual(result["model_selection"]["offered_fallback"]["profile"], "opus-5.5")
        self.assertEqual(coord.delegate_calls, [])
        planner.assert_awaited_once()
        catalog.assert_called_once()
        self.assertLessEqual(len(json.dumps(result, separators=(",", ":"))), 8000)

    async def test_explicit_opus_runs_one_plan_and_pins_the_actual_model(self):
        coord = FakeCoordinator()
        with patch.object(gateway, "coordinator", coord), \
             patch.object(gateway, "get_available_models", return_value=ALL), \
             patch.object(gateway, "_run_luna_plan", AsyncMock(return_value={**PLAN, "reasoning_requirement": "heavy"})) as planner:
            result = await gateway.delegate_via_luna("Implement", "/repo", FakeContext(), model="opus-5.5", delivery="foreground")
        self.assertEqual(coord.delegate_calls[0][-1], "claude-opus-5-5-thinking")
        self.assertEqual(result["antigravity"]["requested_model"], "claude-opus-5-5-thinking")
        self.assertEqual(result["model_selection"]["reasoning"], "heavy")
        planner.assert_awaited_once()

    async def test_reasoning_model_mismatch_rejected_before_catalog_or_planning(self):
        with patch.object(gateway, "get_available_models") as catalog:
            with self.assertRaises(ValueError):
                await gateway.delegate_via_luna("Implement", "/repo", FakeContext(), reasoning="heavy", model="sonnet-5.5", delivery="foreground")
        catalog.assert_not_called()


class CoordinatorModelTests(unittest.TestCase):
    def test_advanced_coordinator_forwards_model_through_all_layers(self):
        from advanced_coordinator import AdvancedWorktreeCoordinator
        from test_roadmap import FakeQuota
        with tempfile.TemporaryDirectory() as raw:
            quota = FakeQuota(Path(raw) / "state")
            original = quota.delegate
            def delegate(task, workspace, quota_override=False, model=None):
                result = original(task, workspace, quota_override)
                quota.base.records[result["task_id"]].model = model
                return result
            quota.delegate = delegate
            coord = AdvancedWorktreeCoordinator(quota)
            result = coord.delegate("Implement", raw, "shared", model="claude-opus-5-5-thinking")
            self.assertEqual(quota.base.get_task(result["task_id"]).model, "claude-opus-5-5-thinking")
