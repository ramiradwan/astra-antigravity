"""Opt-in live typed delegation canary; uses normal authenticated model quota.

Run with: uv run --with 'mcp[cli]==1.29.1' python tests/live_task_types.py
This is not a readiness probe or part of credential-free CI. Scratch artifacts
and handoffs are retained for independent inspection; no repository is modified.
"""

import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "astra-antigravity"


def emit(kind, value):
    print(json.dumps({kind: value}, separators=(",", ":"), ensure_ascii=True), flush=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def size(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=True))


async def main():
    root = Path(tempfile.mkdtemp(prefix="astra-live-task-types-"))
    emit("workspace", str(root))
    params = StdioServerParameters(command=sys.executable, args=[str(PLUGIN / "luna_gateway.py")],
        cwd=str(root), env={**os.environ, "ASTRA_ANTIGRAVITY_STATE_DIR": str(root / "state")})
    inputs = {}
    folders = {}
    for kind in ("analysis", "research", "document"):
        folder = root / kind
        folder.mkdir()
        (folder / "data.csv").write_text("value\n2\n4\n6\n", encoding="utf-8")
        (folder / "source.txt").write_text("The pilot included 12 participants. Eight completed the four-week pilot.\n", encoding="utf-8")
        folders[kind] = folder
        inputs[kind] = {name: digest(folder / name) for name in ("data.csv", "source.txt")}

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=3600)) as session:
            await session.initialize()

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                if response.isError:
                    raise RuntimeError(f"{name}: {response.content}")
                return json.loads(response.content[0].text)

            readiness = await call("check_integration", {"workspace": str(folders["analysis"]),
                                                       "isolation": "shared", "task_type": "analysis"})
            emit("readiness", {key: readiness[key] for key in ("plugin_version", "ready_for", "task_types", "capabilities")})
            if not readiness["ready_for"]["light"]:
                raise RuntimeError("Live light-model dependencies are unavailable")

            tasks = {
                "analysis": "Read data.csv and calculate the arithmetic mean of its value column. "
                            "In the structured answer include mean=4 (or the actual calculated value if different). "
                            "Include the calculation and local input reference in findings/evidence. Preserve all files; no artifact is needed.",
                "research": "Using only source.txt, report how many participants joined the pilot and how many completed it. "
                            "Return source-linked findings with the source location source.txt. "
                            "State that this is supplied-source research with no live web verification. Preserve all files; no artifact is needed.",
                "document": "Read source.txt and create pilot-summary.md, a brief Markdown report with the title Pilot summary, "
                            "the two participant counts, and a Checklist section with one review bullet. "
                            "Return pilot-summary.md as an artifact and check its content. Preserve both input files. No software tests are needed.",
            }
            metrics = []
            for kind, task in tasks.items():
                started = time.monotonic()
                receipt = await call("delegate_via_luna", {"task": task, "workspace": str(folders[kind]),
                    "task_type": kind, "isolation": "shared", "reasoning": "light", "model": "gemini-3.8-high"})
                launch_seconds = time.monotonic() - started
                emit("receipt", {**receipt, "launch_seconds": round(launch_seconds, 3)})
                cursor = 0
                events = []
                while True:
                    batch = await call("receive_delegation_events", {"delegation_id": receipt["delegation_id"], "after_sequence": cursor})
                    for event in batch["events"]:
                        assert size(event) <= (1600 if event["terminal"] else 600), event
                        assert event["task_type"] == kind, event
                        emit("event", event)
                        events.append(event)
                        cursor = event["sequence"]
                        await call("ack_delegation_events", {"delegation_id": receipt["delegation_id"], "sequence": cursor})
                        if not event["terminal"]:
                            raise RuntimeError("Canary unexpectedly needs a parent decision; inspect its saved state")
                    if batch["closed"]:
                        break
                handoff = await call("read_delegation_result", {"delegation_id": receipt["delegation_id"]})
                (root / f"{kind}-handoff.json").write_text(json.dumps(handoff, indent=2), encoding="utf-8")
                assert handoff["status"] == "completed", handoff
                assert handoff["task_type"] == kind
                result = handoff["result"]
                assert result["structured_report_valid"] and handoff["verification"]["structural_checks_passed"], handoff
                assert result["checks_passed"] is not False and not result["remaining_work"], handoff
                assert size(handoff) <= 8000
                assert "implementation" not in handoff and "tests_run" not in result
                if kind == "analysis":
                    assert re.search(r"mean\s*=\s*4(?:\.0)?\b", result["answer"], re.I), result["answer"]
                if kind == "research":
                    claims = " ".join(item["claim"] for item in result["findings"])
                    assert re.search(r"\b12\b", claims) and re.search(r"\b(8|eight)\b", claims, re.I), claims
                    ids = {source["id"] for source in result["sources"] if source["location"] == "source.txt"}
                    assert ids and all(set(item["source_refs"]) & ids for item in result["findings"])
                if kind == "document":
                    assert "pilot-summary.md" in result["artifacts"]
                    text = (folders[kind] / "pilot-summary.md").read_text(encoding="utf-8")
                    assert "Pilot summary" in text and "Checklist" in text
                    assert re.search(r"\b12\b", text) and re.search(r"\b(8|eight)\b", text, re.I)
                    assert all(row["exists"] for row in result["artifact_evidence"])
                assert {name: digest(folders[kind] / name) for name in inputs[kind]} == inputs[kind]
                await call("cancel_delegation", {"task_id": handoff["task_id"]})
                metric = {"task_type": kind, "delegation_id": receipt["delegation_id"], "task_id": handoff["task_id"],
                          "launch_seconds": round(launch_seconds, 3), "total_seconds": round(time.monotonic() - started, 3),
                          "events": len(events), "event_chars": [size(item) for item in events], "handoff_chars": size(handoff),
                          "model": handoff["antigravity"]["requested_model"], "parent_verification_passed": True}
                metrics.append(metric)
                emit("verified", metric)
            (root / "verification.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    emit("completed", {"workspace": str(root), "verified_types": [item["task_type"] for item in metrics]})


if __name__ == "__main__":
    asyncio.run(main())
