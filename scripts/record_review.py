"""Opt-in live reviewer run from a checksum-pinned bundled release.

Uses the owner's normal CLI accounts and quotas. Produces a public-safe measured
trace and retains raw handoffs/logs privately. This is a developer CLI host, not
a recording of ChatGPT UI or proof of marketplace/reviewer-account eligibility.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import traceback
from urllib.parse import unquote, urlsplit
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from prepare_review import prepare

TASKS = {
    "implementation": "Create answer.py with answer() returning the integer 42. Run a Python assertion. Preserve README.md. Keep this tiny fixture scoped; no dependencies are needed.",
    "analysis": "Read data.csv and calculate the arithmetic mean of value. Include mean=4 (or the actual calculated value), the calculation and the local input reference in the structured answer/evidence. Preserve data.csv; no artifact is needed.",
    "research": "Using only source.txt, report the pilot participant and completion counts. Link findings to source.txt and state that this is supplied-source research without live web verification. Preserve source.txt; no artifact is needed.",
    "document": "Use source.txt to create pilot-summary.md titled Pilot summary. Include both participant counts and a Checklist section with one review bullet. Return the artifact in the document contract. Preserve source.txt; no software tests are needed.",
}
MODEL_LABELS = {"gemini-3.8-flash-high": "Gemini 3.8 Flash High", "claude-sonnet-5-5": "Sonnet 5.5",
                "claude-sonnet-5-5-thinking": "Sonnet 5.5", "claude-sonnet-5.5": "Sonnet 5.5",
                "claude-sonnet-5.5-thinking": "Sonnet 5.5"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wire_size(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=True))


def extract_release(archive, expected_sha, destination):
    if digest(archive) != expected_sha:
        raise ValueError("Release checksum mismatch")
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        if len(infos) > 15000 or sum(item.file_size for item in infos) > 1024**3:
            raise ValueError("Release exceeds bounded extraction limits")
        names = set()
        for item in infos:
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or "\\" in item.filename or ":" in item.filename
                    or any(part in {"", ".", ".."} for part in item.filename.split("/"))
                    or item.filename.casefold() in names
                    or item.external_attr >> 16 & 0o170000 == 0o120000):
                raise ValueError("Unsafe release archive path")
            names.add(item.filename.casefold())
        zipped.extractall(destination)
    config = json.loads((destination / "mcp.json").read_text())
    args = config["mcpServers"]["luna_gateway"]["args"]
    if args != ["-I", "${PLUGIN_ROOT}/runtime_bootstrap.py", "luna_gateway.py"]:
        raise ValueError("Review requires the bundled gateway entry point")


def verify_answer(source):
    """Review a tiny fixture before evaluating only its safe answer function."""
    module = ast.parse(source)
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != "answer":
        raise ValueError("Expected exactly the scoped answer function")
    function = functions[0]
    body = [node for node in function.body if not (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]
    if (function.decorator_list or function.args.args or function.args.posonlyargs
            or function.args.kwonlyargs or function.args.vararg or function.args.kwarg
            or len(body) != 1 or not isinstance(body[0], ast.Return)
            or not isinstance(body[0].value, ast.Constant)
            or type(body[0].value.value) is not int or body[0].value.value != 42):
        raise ValueError("answer must return the integer 42 without side effects")
    for node in ast.walk(module):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Attribute, ast.With, ast.AsyncWith)):
            raise ValueError("Unexpected external behavior in the tiny fixture")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name)
                and node.func.id == "answer" and not node.args and not node.keywords):
            raise ValueError("Unexpected call in the tiny fixture")
    # Execute only the inspected function, without module-level statements or annotations.
    checked = ast.FunctionDef(name="answer", args=function.args, body=body,
                              decorator_list=[], returns=None, type_comment=None)
    scoped = ast.fix_missing_locations(ast.Module(body=[checked], type_ignores=[]))
    namespace = {"__builtins__": {}}
    exec(compile(scoped, "reviewed-answer.py", "exec"), namespace)
    assert namespace["answer"]() == 42


def unchanged_inputs(root, inputs):
    return all((root / name).is_file() and digest(root / name) == expected
               for name, expected in inputs.items())


def matches_supplied_source(location, workspace, filename="source.txt"):
    if not isinstance(location, str):
        return False
    if "://" in location:
        parsed = urlsplit(location)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return False
        location = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", location):
            location = location[1:]
    path = Path(location)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve() == (workspace / filename).resolve() and path.is_file()


class Trace:
    def __init__(self, path):
        self.path = path
        self.started = time.monotonic()
        self.rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        self.offset = self.rows[-1]["at_seconds"] if self.rows else 0

    def emit(self, kind, message, **data):
        # Call sites supply fixed text and whitelisted booleans/numbers/labels.
        # Never serialize a provider response, account, workspace, ID or handoff here.
        row = {"at_seconds": round(self.offset + time.monotonic() - self.started, 3),
               "kind": kind, "message": message, "data": data}
        self.rows.append(row)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n")
        print(json.dumps(row, separators=(",", ":"), ensure_ascii=True), flush=True)


async def run(args):
    root = args.output.resolve()
    if not args.resume:
        root.mkdir(parents=True, exist_ok=False)
    private = root / "private"
    public = root / "public"
    if not args.resume:
        private.mkdir()
        public.mkdir()
    trace = Trace(public / "review-trace.jsonl")
    plugin = root / "plugin"
    if args.resume:
        if (not trace.rows or trace.rows[0]["data"].get("archive_sha256") != args.archive_sha256
                or digest(args.archive) != args.archive_sha256):
            raise ValueError("Resume must retain the exact trusted release")
        trace.emit("resume", "Recording resumed after a verifier fix. Completed jobs are reused; the restart gap is omitted.",
                   timeline_basis="cumulative live recording time", completed_jobs_relaunched=False)
    else:
        trace.emit("intro", "Live developer CLI reviewer run; normal owner CLI accounts and quotas.",
                   archive_sha256=args.archive_sha256)
        extract_release(args.archive, args.archive_sha256, plugin)
    manifest = json.loads((plugin / "plugin.json").read_text())
    fixtures = root / "fixtures"
    if args.resume:
        inputs = json.loads((fixtures / "input-hashes.json").read_text(encoding="utf-8"))
        assert unchanged_inputs(fixtures, inputs)
    else:
        inputs = prepare(fixtures)["input_hashes"]
        trace.emit("installed", "Extracted the checksum-pinned bundled release into a new isolated review folder.",
                   plugin_version=manifest["version"], sdk_version="1.29.1")
    params = StdioServerParameters(command=sys.executable,
        args=["-I", "runtime_bootstrap.py", "luna_gateway.py"], cwd=str(plugin),
        env={**os.environ, "ASTRA_ANTIGRAVITY_STATE_DIR": str(root / "state"),
             "ASTRA_ANTIGRAVITY_WORKTREE_ROOT": str(root / "worktrees"), "UV_OFFLINE": "1"})
    with (private / "gateway-stderr.log").open("a", encoding="utf-8") as stderr:
        async with stdio_client(params, errlog=stderr) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=3600)) as session:
                await session.initialize()

                async def call(name, arguments, *, expect_error=False):
                    response = await session.call_tool(name, arguments)
                    if expect_error:
                        assert response.isError, "Expected schema rejection"
                        return None
                    if response.isError:
                        (private / "tool-error.txt").write_text(str(response.content), encoding="utf-8")
                        raise RuntimeError("Tool failed; private diagnostic retained")
                    return json.loads(response.content[0].text)

                ready = await call("check_integration", {"workspace": str(fixtures / "implementation")})
                (private / "readiness.json").write_text(json.dumps(ready, indent=2), encoding="utf-8")
                profiles = ready["model_availability"]["profiles"]
                availability = {key: bool(profiles[key]["available"])
                                for key in ("sonnet-5.5", "gemini-3.8-high", "argon-high", "opus-5.5")}
                trace.emit("readiness", "Read-only dependency and model preflight; no inference.",
                           light_ready=ready["ready_for"]["light"], **availability)
                if not availability["argon-high"]:
                    notice = ("Gemini 4 Argon High is unavailable; Opus 5.5 is the explicit available fallback offer."
                              if availability["opus-5.5"] else
                              "Gemini 4 Argon High and Opus 5.5 are unavailable in this CLI catalog; heavy delegation cannot start.")
                    trace.emit("availability", notice,
                               opus_advertised=availability["opus-5.5"], heavy_task_started=False)
                if not ready["ready_for"]["light"] or not availability[args.model]:
                    raise RuntimeError("Requested light profile is unavailable; no quota/model bypass")

                no_web = await call("check_integration", {"workspace": str(fixtures / "research"),
                    "isolation": "shared", "task_type": "research", "required_capabilities": ["web"]})
                assert not no_web["capabilities"]["available"]
                trace.emit("negative", "Live-web requirement rejected by capability preflight.", inference_started=False)
                tools = await session.list_tools()
                assert len(tools.tools) == 9 and not any("email" in tool.name for tool in tools.tools)
                trace.emit("negative", "No email tool or inherited connector is exposed; no message is sent.",
                           scope="tool exposure; parent conversational refusal needs review-host evaluation")
                await call("delegate_via_luna", {"task": "Use only the supplied source", "workspace": str(fixtures / "research"),
                    "task_type": "autonomous-email-campaign", "isolation": "shared"}, expect_error=True)
                trace.emit("negative", "Unsupported task type rejected by the tool schema.", inference_started=False)

                metrics = []
                for kind, task in TASKS.items():
                    started = time.monotonic()
                    saved_handoff = private / f"{kind}-handoff.json"
                    saved_receipt = private / f"{kind}-receipt.json"
                    previous_metric = next((row["data"] for row in trace.rows
                        if row["kind"] == "verified" and row["data"].get("task_type") == kind), None)
                    if args.resume and saved_handoff.is_file():
                        handoff = json.loads(saved_handoff.read_text(encoding="utf-8"))
                        original_receipt = next(row for row in trace.rows if row["kind"] == "receipt" and row["data"]["task_type"] == kind)
                        terminal = next(row for row in trace.rows if row["kind"] == "terminal" and row["data"]["task_type"] == kind)
                        elapsed = terminal["at_seconds"] - original_receipt["at_seconds"]
                        event_count = terminal["data"]["events"]
                    else:
                        if saved_receipt.exists():
                            raise RuntimeError("An existing receipt must be recovered explicitly; never relaunch its job")
                        handoff, event_count = await execute_case(call, private, trace, fixtures, inputs, kind, task, args.model)
                        elapsed = time.monotonic() - started
                    assert wire_size(handoff) <= 8000
                    assert handoff["status"] == "completed" and handoff["task_type"] == kind
                    result = handoff["implementation" if kind == "implementation" else "result"]
                    assert result["structured_report_valid"] and not result["remaining_work"]
                    requested = handoff["antigravity"]["requested_model"]
                    assert ((args.model == "gemini-3.8-high" and requested == "gemini-3.8-flash-high")
                            or (args.model == "sonnet-5.5" and MODEL_LABELS.get(requested) == "Sonnet 5.5"))
                    model_label = MODEL_LABELS.get(requested, "Unrecognized model label")
                    verified = verify_case(kind, result, root, fixtures, inputs)
                    assert unchanged_inputs(fixtures, inputs)
                    metric = previous_metric or {"task_type": kind, "total_seconds": round(elapsed, 3),
                              "events": event_count, "handoff_characters": wire_size(handoff),
                              "requested_model": model_label, "parent_verification_passed": True}
                    metrics.append(metric)
                    if not previous_metric:
                        trace.emit("verified", verified, **metric)
                summary = {"plugin_version": manifest["version"], "archive_sha256": args.archive_sha256,
                           "host": "developer Python MCP client", "live_execution": True,
                           "timeline_basis": "cumulative live recording time; restart gaps omitted",
                           "tasks": metrics, "positive_api_cases_verified": 5,
                           "negative_api_boundaries_verified": 3,
                           "original_inputs_unchanged": unchanged_inputs(fixtures, inputs),
                           "review_host_conversational_cases_verified": False,
                           "marketplace_eligibility_verified": False}
                (public / "review-summary.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
                trace.emit("complete", "Four live tasks independently verified. Marketplace access and review-host conversational evaluation remain separate.",
                           verified_task_types=list(TASKS), original_inputs_unchanged=True)


async def execute_case(call, private, trace, fixtures, inputs, kind, task, model):
    started = time.monotonic()
    receipt = await call("delegate_via_luna", {"task": task, "workspace": str(fixtures / kind),
        "task_type": kind, "isolation": "worktree" if kind == "implementation" else "shared",
        "reasoning": "light", "model": model,
        "request_id": "review-" + kind})
    identifier = receipt["delegation_id"]
    (private / f"{kind}-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    trace.emit("receipt", "Background receipt received; the host can continue independent work.",
               task_type=kind, receipt_seconds=round(time.monotonic()-started, 3))
    # A deterministic receiver waits for pushed decisions/results. No status polling.
    receive = asyncio.create_task(call("receive_delegation_events", {"delegation_id": identifier}))
    assert unchanged_inputs(fixtures, inputs)
    trace.emit("parent_work", "While the job runs, the parent independently verifies synthetic input hashes.",
               task_type=kind, original_inputs_unchanged=True)
    events = []
    cursor = 0
    while True:
        batch = await receive
        for event in batch["events"]:
            assert wire_size(event) <= (1600 if event["terminal"] else 600)
            events.append(event)
            cursor = event["sequence"]
            await call("ack_delegation_events", {"delegation_id": identifier, "sequence": cursor})
            if not event["terminal"]:
                (private / f"{kind}-decision.json").write_text(json.dumps(event, indent=2), encoding="utf-8")
                trace.emit("decision", "The job needs a parent decision; inspect the retained private evidence.", task_type=kind)
                raise RuntimeError("Reviewer run requires a parent decision; no automatic retry or override")
        if batch["closed"]:
            break
        receive = asyncio.create_task(call("receive_delegation_events", {
            "delegation_id": identifier, "after_sequence": cursor}))
    trace.emit("terminal", "One bounded terminal event delivered; full handoff is fetched once for verification.",
               task_type=kind, events=len(events), event_characters=[wire_size(item) for item in events])
    handoff = await call("read_delegation_result", {"delegation_id": identifier})
    (private / f"{kind}-handoff.json").write_text(json.dumps(handoff, indent=2), encoding="utf-8")
    return handoff, len(events)


def verify_case(kind, result, root, fixtures, inputs):
    if kind == "implementation":
        workspace = Path(result["worktree"]["workspace"]).resolve()
        assert workspace.is_relative_to(root) and workspace != (fixtures / kind).resolve()
        verify_answer((workspace / "answer.py").read_text(encoding="utf-8"))
        assert digest(workspace / "README.md") == inputs["implementation/README.md"]
        assert not (fixtures / kind / "answer.py").exists()
        assert result["tests_passed"] is True
        verified = "Inspected the isolated function; independently asserted answer() == 42. Source README preserved."
    elif kind == "analysis":
        assert re.search(r"mean\s*=\s*4(?:\.0)?\b", result["answer"], re.I)
        with (fixtures / kind / "data.csv").open(encoding="utf-8", newline="") as stream:
            values = [float(row["value"]) for row in csv.DictReader(stream)]
        assert sum(values) / len(values) == 4
        verified = "Independently recalculated (2 + 4 + 6) / 3 = 4 and checked the analysis answer."
    elif kind == "research":
        claims = " ".join(item["claim"] for item in result["findings"])
        assert re.search(r"\b12\b", claims) and re.search(r"\b(8|eight)\b", claims, re.I)
        ids = {source["id"] for source in result["sources"]
               if matches_supplied_source(source["location"], fixtures / kind)}
        assert ids and all(set(item["source_refs"]) & ids for item in result["findings"])
        verified = "Checked 12 participants and 8 completions against source.txt and resolved each finding's source reference."
    else:
        document = (fixtures / kind / "pilot-summary.md").read_text(encoding="utf-8")
        assert "pilot-summary.md" in result["artifacts"]
        assert "Pilot summary" in document and "Checklist" in document
        assert re.search(r"\b12\b", document) and re.search(r"\b(8|eight)\b", document, re.I)
        assert re.search(r"(?m)^\s*[-*]\s+", document)
        assert all(row["exists"] for row in result["artifact_evidence"])
        verified = "Opened pilot-summary.md and verified the title, both source counts, Checklist and review bullet."
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New folder, or an existing run with explicit --resume")
    parser.add_argument("--model", choices=("gemini-3.8-high", "sonnet-5.5"), default="gemini-3.8-high")
    parser.add_argument("--run-live", action="store_true", help="Use normal authenticated model quota")
    parser.add_argument("--resume", action="store_true", help="Reuse saved completed handoffs; never relaunch an existing receipt")
    args = parser.parse_args()
    if not args.resume and args.output.exists():
        parser.error("Use a new output directory; existing runs require explicit --resume")
    if not args.run_live:
        parser.error("Live inference requires --run-live; normal account quota applies")
    if not re.fullmatch(r"[0-9a-f]{64}", args.archive_sha256):
        parser.error("Provide the trusted bundled release SHA-256")
    try:
        asyncio.run(run(args))
    except Exception as exc:
        private = args.output.resolve() / "private"
        if private.is_dir():
            with (private / "recording-error.log").open("a", encoding="utf-8") as stream:
                stream.write(traceback.format_exc() + "\n")
        print(json.dumps({"kind": "failed", "message": "Reviewer run did not satisfy its checks; inspect retained private evidence.",
                          "error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
