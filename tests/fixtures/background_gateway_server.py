"""Production background gateway + real process/Git; only inference/quota are fake."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins/astra-antigravity"))
sys.path.insert(0, str(ROOT / "tests"))

import luna_gateway as gateway
import server
from advanced_coordinator import AdvancedWorktreeCoordinator
from test_antigravity_native_process import DirectQuotaAdapter


class FixtureQuota(DirectQuotaAdapter):
    def delegate(self, task, workspace, quota_override=False, model=None):
        record = self.base.start_task(task, workspace, model=model, effort="high")
        return {"task_id": record.task_id, "status": record.status, "workspace": record.workspace,
                "quota_before": self._quota(0.8)}

    def status(self, task_id):
        record = self.base.get_task(task_id)
        return {"task_id": task_id, "status": record.status, "workspace": record.workspace}


base = server.TaskManager()
base.initial_wait_seconds = 0.01
server.find_agy = lambda: sys.executable


def command(workspace=None, model=None, effort=None):
    return [sys.executable, str(ROOT / "tests/fixtures/fake_agy_native_result.py"),
            "--model", model or "gemini-3.8-flash-high", "--effort", effort or "high",
            "--sandbox", "--output-format", "stream-json", "--json-schema", str(server.IMPLEMENTATION_SCHEMA_PATH),
            "--add-dir", workspace]


base._base_command = command
gateway.coordinator = AdvancedWorktreeCoordinator(FixtureQuota(base))
gateway.coordinator._runtime_preflight_enabled = False
gateway.get_available_models = lambda *args: {"gemini-3.8-flash-high"}


async def planner(*args, **kwargs):
    path = Path(os.environ["ASTRA_TEST_PLANNER_COUNT"])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("plan\n")
    return {"reasoning_requirement": "light", "coordination_brief": "Private planning marker",
            "acceptance_focus": ["Inspect the actual changed file"], "risks": []}


gateway._run_luna_plan = planner
original_background = gateway._background


def background():
    manager = original_background()
    if not getattr(manager, "fixture_receipt_wrapped", False):
        original_start = manager.start
        def start(**kwargs):
            receipt, created = original_start(**kwargs)
            Path(os.environ["ASTRA_TEST_RECEIPT_FILE"]).write_text(json.dumps(receipt), encoding="utf-8")
            return receipt, created
        manager.start = start
        manager.fixture_receipt_wrapped = True
    return manager


gateway._background = background


@gateway.gateway_tool()
def record_parent_work() -> dict:
    release = Path(os.environ["ASTRA_FAKE_AGY_RELEASE_FILE"])
    assert not release.exists(), "Worker was released before independent parent work"
    Path(os.environ["ASTRA_TEST_PARENT_WORK_FILE"]).write_text("parent completed independent work", encoding="utf-8")
    return {"parent_independent_work_completed": True, "delegation_still_pending": True}


if __name__ == "__main__":
    gateway.mcp.run()
