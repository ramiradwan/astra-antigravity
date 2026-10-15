import asyncio
import copy
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "astra-antigravity"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(ROOT / "tests"))

import luna_gateway as gateway
import server
from advanced_coordinator import AdvancedWorktreeCoordinator
from background_delegations import BackgroundDelegations, DelegationStore, inspect_saved_receipt
from communication import bound_report, json_size
from recovery import inspect_saved_tasks
from semantic_report import validate_semantic_report
from task_types import TASK_TYPES, check_capabilities, inspect_artifacts, result_key, schema_path
from test_antigravity_native_process import DirectQuotaAdapter
from test_luna_gateway_runtime import FakeContext, PLAN


def sample(kind):
    common = {"summary": "Useful summary", "remaining_work": [], "risks": []}
    if kind == "implementation":
        return {**common, "tests_run": [], "tests_passed": None}
    common.update(checks_performed=[], checks_passed=None, artifacts=[])
    if kind == "analysis":
        return {**common, "answer": "4", "findings": ["Mean is 4"], "evidence": ["data.csv"], "limitations": []}
    if kind == "research":
        return {**common, "answer": "A", "findings": [{"claim": "A", "source_refs": ["a"]}],
                "sources": [{"id": "a", "location": "source.txt", "title": "Source"}], "limitations": []}
    return {**common, "artifacts": ["draft.md"], "review_notes": []}


class TaskContractTests(unittest.TestCase):
    def test_reports_cannot_be_reinterpreted_as_another_task_type(self):
        for kind in TASK_TYPES:
            with self.subTest(kind=kind):
                self.assertIsNone(validate_semantic_report(sample(kind), kind)[1])
                for other in set(TASK_TYPES) - {kind}:
                    self.assertIsNotNone(validate_semantic_report(sample(kind), other)[1])

    def test_research_citations_must_resolve_and_source_ids_must_be_unique(self):
        report = sample("research")
        report["findings"][0]["source_refs"] = ["invented"]
        self.assertTrue(validate_semantic_report(report, "research")[1])
        report = sample("research")
        report["sources"].append(dict(report["sources"][0]))
        self.assertTrue(validate_semantic_report(report, "research")[1])

    def test_invalid_values_do_not_become_model_claimed_success(self):
        for kind in ("analysis", "research", "document"):
            report = sample(kind)
            report["checks_passed"] = 1
            value, error = validate_semantic_report(report, kind)
            self.assertEqual(value, {})
            self.assertTrue(error)

    def test_artifacts_are_observed_and_paths_must_stay_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "draft.md").write_text("draft", encoding="utf-8")
            (root / "outside.txt").write_text("outside", encoding="utf-8")
            observed, issues = inspect_artifacts(["draft.md"], workspace)
            self.assertEqual(observed, [{"path": "draft.md", "exists": True, "size_bytes": 5}])
            self.assertFalse(issues)
            for path in ("../outside.txt", str(root / "outside.txt"), "C:/outside.txt", "folder\\draft.md", "missing.md"):
                observed, issues = inspect_artifacts([path], workspace)
                self.assertFalse(observed[0]["exists"])
                self.assertTrue(issues)

    def test_file_symlink_cannot_escape_artifact_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outside.txt").write_text("outside", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            try:
                (workspace / "link.txt").symlink_to(root / "outside.txt")
            except OSError:
                self.skipTest("This host does not permit symlink creation")
            observed, issues = inspect_artifacts(["link.txt"], workspace)
            self.assertFalse(observed[0]["exists"])
            self.assertTrue(issues)

    def test_capabilities_are_explicit_and_parent_connectors_are_not_assumed(self):
        self.assertTrue(check_capabilities("research")["available"])
        for capability in ("web", "browser", "connectors", "gmail"):
            report = check_capabilities("research", [capability])
            self.assertFalse(report["available"])
            self.assertEqual(report["unavailable"], [capability])

    def test_legacy_task_records_default_to_implementation_and_new_types_survive_roundtrip(self):
        fields = dict(task_id="task-old", workspace="/work", status="completed", created_at=1,
                      updated_at=1, server_instance_id="old", log_path="/log", meta_path="/meta")
        self.assertEqual(server.TaskRecord.from_dict(fields).task_type, "implementation")
        record = server.TaskRecord.from_dict({**fields, "task_type": "research", "required_capabilities": ["local_files"]})
        restored = server.TaskRecord.from_dict(record.to_dict())
        self.assertEqual(restored.task_type, "research")
        self.assertEqual(restored.required_capabilities, ["local_files"])

    def test_existing_outbox_is_migrated_without_losing_result_or_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delegations.sqlite3"
            store = DelegationStore(path)
            job, _ = store.create("owner", "/work", "delegation", "fp", "request")
            store.publish(job["id"], "completed", {"status": "completed"}, result={"status": "completed"})
            store.close()
            with sqlite3.connect(path) as db:
                # Recreate the pre-0.9 schema without the new column.
                columns = [row[1] for row in db.execute("PRAGMA table_info(delegations)") if row[1] != "task_type"]
                db.execute("ALTER TABLE delegations RENAME TO old_delegations")
                db.execute("CREATE TABLE delegations AS SELECT " + ",".join(columns) + " FROM old_delegations")
                db.execute("DROP TABLE old_delegations")
            db.close()
            readonly = inspect_saved_receipt(directory, "request")
            self.assertEqual(readonly["task_type"], "implementation")
            store = DelegationStore(path)
            try:
                self.assertEqual(store.get(job["id"])["task_type"], "implementation")
                self.assertEqual(len(store.events(job["id"], 0)), 1)
                self.assertEqual(json.loads(store.get(job["id"])["result"])["status"], "completed")
            finally:
                store.close()

    def test_large_non_coding_handoffs_are_bounded_with_explicit_omissions(self):
        for kind in ("analysis", "research", "document"):
            report = sample(kind)
            report["summary"] = "資料" * 5000
            report["remaining_work"] = ["未完了" * 5000] * 30
            report["risks"] = ["risk" * 5000] * 30
            bounded = gateway._result_with_luna({"task_type": kind, "status": "completed", "task_id": "task-large",
                "result": report}, PLAN, phase="delegation")
            self.assertLessEqual(json_size(bounded), 8000)
            self.assertTrue(bounded["result"]["remaining_work"])
            self.assertTrue(bounded["truncation"])
            self.assertNotIn("implementation", bounded)


class TypedQuotaAdapter(DirectQuotaAdapter):
    def delegate(self, task, workspace, quota_override=False, model=None,
                 task_type="implementation", required_capabilities=None):
        record = self.base.start_task(task, workspace, model=model, task_type=task_type,
                                      required_capabilities=required_capabilities)
        return {"task_id": record.task_id, "status": record.status, "workspace": record.workspace,
                "quota_before": self._quota(0.8)}

    def status(self, task_id):
        record = self.base.get_task(task_id)
        return {"task_id": task_id, "status": record.status, "workspace": record.workspace}


class TaskTypeGatewayProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="astra types ")
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "data.csv").write_text("value\n2\n4\n6\n", encoding="utf-8")
        (self.workspace / "source.txt").write_text("Supplied source supports claim A.\n", encoding="utf-8")
        self.manager = server.TaskManager(self.root / "state")
        self.manager.initial_wait_seconds = 0
        self.manager.session_idle_ttl_seconds = 60
        production_command = server.TaskManager._base_command

        def fixture_command(workspace=None, model=None, effort=None, task_type="implementation"):
            command = production_command(self.manager, workspace, model, effort, task_type)
            return [sys.executable, str(ROOT / "tests/fixtures/fake_agy_task_types.py")] + command[1:]

        self.manager._base_command = fixture_command
        self.coordinator = AdvancedWorktreeCoordinator(TypedQuotaAdapter(self.manager))
        self.background = BackgroundDelegations(self.root / "state")
        self.planner = AsyncMock(return_value=copy.deepcopy(PLAN))
        self.patches = [patch.object(server, "find_agy", return_value=sys.executable),
                        patch.object(gateway, "coordinator", self.coordinator),
                        patch.object(gateway, "_background", return_value=self.background),
                        patch.object(gateway, "get_available_models", return_value={"gemini-3.8-flash-high"}),
                        patch.object(gateway, "_run_luna_plan", self.planner)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await self.background.close()
        for task_id in list(self.manager._tasks):
            self.manager.cancel_task(task_id)
        for thread in list(self.manager._session_threads.values()):
            await asyncio.to_thread(thread.join, 3)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def delegate(self, kind, **kwargs):
        return await gateway.delegate_via_luna("Use supplied data and notes", str(self.workspace), FakeContext(),
            task_type=kind, isolation="shared", reasoning="light", delivery="foreground", **kwargs)

    async def test_all_task_types_use_the_native_schema_from_launch_to_parent_handoff(self):
        inputs = {name: (self.workspace / name).read_bytes() for name in ("source.txt", "data.csv")}
        for kind in TASK_TYPES:
            with self.subTest(kind=kind):
                result = await self.delegate(kind)
                key = result_key(kind)
                self.assertEqual(result["task_type"], kind)
                self.assertEqual(result["status"], "completed")
                self.assertTrue(result[key]["structured_report_valid"])
                self.assertIn(kind, self.planner.await_args.args[0])
                self.assertNotIn("SECRET", json.dumps(result))
                record = self.manager.get_task(result["task_id"])
                self.assertEqual(record.task_type, kind)
                self.assertEqual(record.turns[-1]["task_type"], kind)
                if kind != "implementation":
                    self.assertNotIn("implementation", result)
                    self.assertNotIn("tests_run", result[key])
                    self.assertTrue(result["verification"]["structural_checks_passed"])
                    self.assertTrue(result["verification"]["required"])
                if kind == "analysis":
                    self.assertEqual(result[key]["answer"], "4.0")
                if kind == "research":
                    self.assertEqual(result[key]["findings"][0]["source_refs"], ["notes"])
                if kind == "document":
                    self.assertTrue(result[key]["artifact_evidence"][0]["exists"])
                    self.assertIn("claim A", (self.workspace / "draft.md").read_text())
                self.manager.cancel_task(result["task_id"])
        self.assertEqual({name: (self.workspace / name).read_bytes() for name in inputs}, inputs)

    async def test_analysis_corrections_keep_type_schema_and_exact_task_on_hot_and_cold_resume(self):
        first = await self.delegate("analysis")
        task_id = first["task_id"]
        second = await gateway.continue_via_luna(task_id, "Recheck the mean", FakeContext(), delivery="foreground")
        self.assertEqual(second["result"]["summary"], "analysis turn 2")
        self.manager.cancel_task(task_id)
        thread = self.manager._session_threads.get(task_id)
        if thread:
            await asyncio.to_thread(thread.join, 3)
        third = await gateway.continue_via_luna(task_id, "Recheck once more", FakeContext(), delivery="foreground")
        self.assertEqual(third["task_id"], task_id)
        self.assertEqual(third["task_type"], "analysis")
        self.assertEqual(third["result"]["summary"], "analysis resumed")
        self.assertNotIn("implementation", third)
        self.assertIn("Task type: analysis", self.planner.await_args.args[0])

    async def test_non_git_saved_inspection_and_task_manager_restart_preserve_research_evidence(self):
        result = await self.delegate("research")
        tid = result["task_id"]
        self.manager.cancel_task(tid)
        thread = self.manager._session_threads.get(tid)
        if thread:
            await asyncio.to_thread(thread.join, 3)
        restored = server.TaskManager(self.root / "state")
        saved = json.loads((self.root / "state" / f"{tid}.json").read_text(encoding="utf-8"))
        self.assertEqual(server.TaskRecord.from_dict(saved).task_type, "research")
        self.assertEqual(restored.get_task(tid).task_type, "research")
        self.assertEqual(restored.structured_output(tid)["sources"][0]["location"], "source.txt")
        view = inspect_saved_tasks(self.coordinator, self.workspace, tid)["tasks"][0]
        self.assertEqual(view["task_type"], "research")
        self.assertTrue(view["result"]["structured_report_valid"])
        self.assertEqual(view["result"]["sources"][0]["location"], "source.txt")
        self.assertNotIn("implementation", view)

    async def test_missing_document_artifact_fails_observed_verification_even_with_worker_success(self):
        result = await self.delegate("document")
        (self.workspace / "draft.md").unlink()
        observed = self.coordinator.compact_result(result["task_id"])
        self.assertTrue(observed["result"]["structured_report_valid"])
        self.assertFalse(observed["verification"]["structural_checks_passed"])
        self.assertFalse(observed["result"]["artifact_evidence"][0]["exists"])
        self.assertTrue(observed["verification"]["issues"])

    async def test_unavailable_external_capability_stops_before_planner_or_backend_launch(self):
        result = await self.delegate("research", required_capabilities=["web"])
        self.assertEqual(result["status"], "capability_unavailable")
        self.assertFalse(result["task_started"])
        self.planner.assert_not_awaited()
        gateway.get_available_models.assert_not_called()
        self.assertFalse(list((self.root / "state").glob("task-*.json")))

    async def test_background_capability_failure_is_a_compact_pushed_decision(self):
        receipt = await gateway.delegate_via_luna("Find current news", str(self.workspace), FakeContext(),
            task_type="research", isolation="shared", required_capabilities=["web"])
        batch = await asyncio.wait_for(self.background.receive(receipt["delegation_id"]), 3)
        event = batch["events"][0]
        self.assertEqual(event["task_type"], "research")
        self.assertEqual(event["status"], "capability_unavailable")
        self.assertEqual(event["next_action"], "resolve_capabilities")
        self.assertTrue(event["terminal"])
        self.assertLessEqual(json_size(event), 1600)
        self.planner.assert_not_awaited()

    async def test_background_document_push_omits_answers_sources_and_artifact_content(self):
        receipt = await gateway.delegate_via_luna("Write draft", str(self.workspace), FakeContext(),
            task_type="document", isolation="shared", reasoning="light")
        batch = await asyncio.wait_for(self.background.receive(receipt["delegation_id"]), 5)
        event = batch["events"][0]
        self.assertEqual(receipt["task_type"], "document")
        self.assertEqual(event["task_type"], "document")
        self.assertEqual(event["artifacts_count"], 1)
        self.assertEqual(event["next_action"], "verify_result")
        for forbidden in ("answer", "sources", "artifacts", "planner", "luna", "response", "tests_passed"):
            self.assertNotIn(forbidden, event)
        self.assertLessEqual(json_size(event), 1600)
        stored = self.background.result(receipt["delegation_id"])
        self.assertEqual(stored["result"]["artifacts"], ["draft.md"])
        self.assertEqual(inspect_saved_receipt(self.root / "state", self.background.store.get(receipt["delegation_id"])["request_id"])["task_type"], "document")

    async def test_idempotent_request_cannot_change_task_type_or_capability_requirements(self):
        receipt = await gateway.delegate_via_luna("Read notes", str(self.workspace), FakeContext(),
            task_type="research", isolation="shared", request_id="same-request")
        for overrides in ({"task_type": "analysis"}, {"task_type": "research", "required_capabilities": ["web"]}):
            with self.assertRaisesRegex(ValueError, "different delegation"):
                await gateway.delegate_via_luna("Read notes", str(self.workspace), FakeContext(),
                    isolation="shared", request_id="same-request", **overrides)
        self.assertEqual(receipt["task_type"], "research")

    async def test_invalid_task_type_is_rejected_without_creating_a_background_job(self):
        with self.assertRaisesRegex(ValueError, "task_type"):
            await gateway.delegate_via_luna("Read", str(self.workspace), FakeContext(), task_type="unknown")
        self.planner.assert_not_awaited()
        self.assertFalse(self.background.workers)


if __name__ == "__main__":
    unittest.main()
