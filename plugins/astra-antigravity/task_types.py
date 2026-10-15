"""Task contracts shared by launch, correction, results and saved inspection."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from runtime_assets import plugin_directory

TASK_TYPES = ("implementation", "analysis", "research", "document")
LOCAL_CAPABILITIES = ("local_files", "shell")

CONTRACTS = {
    "implementation": {
        "instructions": "Complete the requested implementation task. Run appropriate tests where meaningful. "
                        "Report the implementation summary, tests run and whether they passed, remaining work, "
                        "and risks accurately in the required structured result.",
        "verification_focus": ["Inspect the actual changes against acceptance criteria", "Run appropriate tests"],
        "capabilities": ["local_files", "shell"],
    },
    "analysis": {
        "instructions": "Answer the requested analysis question using supplied material and local evidence. "
                        "Separate findings from assumptions and describe limitations. Preserve input files; "
                        "create only artifacts the parent requested. Report the answer, findings, evidence, "
                        "checks performed, limitations, artifacts, remaining work and risks. "
                        "Do not invent implementation changes or software tests for an analytical task.",
        "verification_focus": ["Recheck findings against the supplied evidence", "Check assumptions and calculations"],
        "capabilities": ["local_files"],
    },
    "research": {
        "instructions": "Research the parent's question using supplied material and accessible local sources. "
                        "Preserve inputs; create only requested artifacts. Give an answer and findings with "
                        "source_refs matching IDs in sources. Use a source's actual location, or provided:<label> "
                        "for material supplied in the task. Separate source-backed claims from inferences and "
                        "state limitations. Do not claim live web access or invent citations. If the question "
                        "needs unavailable external access, report that in remaining_work and checks_passed=false. "
                        "Report checks performed, artifacts and risks; software tests are not required.",
        "verification_focus": ["Verify citations and that sources support the claims", "Check source dates, scope and limitations"],
        "capabilities": ["local_files"],
    },
    "document": {
        "instructions": "Create or revise the requested document artifacts in the effective workspace. "
                        "Preserve source material and unrelated files. Report relative artifact paths, review "
                        "notes, checks performed and whether they passed, remaining work and risks. "
                        "Check document content and requested format; inspect rendered output where relevant. "
                        "Do not substitute a prose completion claim for an actual document artifact.",
        "verification_focus": ["Open the artifacts and check content against the request", "Check format and rendered layout where relevant"],
        "capabilities": ["local_files"],
    },
}


def validate_task_type(task_type):
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unsupported task_type: {task_type}. Use {', '.join(TASK_TYPES)}")
    return task_type


def saved_task_type(record):
    """Old records are implementation tasks; unknown new types never downgrade."""
    return validate_task_type(getattr(record, "task_type", "implementation"))


def schema_path(task_type="implementation"):
    validate_task_type(task_type)
    return plugin_directory() / "schemas" / f"{task_type}_result.schema.json"


@lru_cache(maxsize=4)
def result_schema(task_type="implementation"):
    return json.loads(schema_path(task_type).read_text(encoding="utf-8"))


def result_key(task_type):
    return "implementation" if task_type == "implementation" else "result"


def semantic_result(report):
    return report.get(result_key(report.get("task_type", "implementation"))) or {}


def capability_requirements(task_type, required=None):
    validate_task_type(task_type)
    if required is not None and (not isinstance(required, list) or len(required) > 12
                                or any(not isinstance(item, str) or not item.strip()
                                       or len(item) > 64 for item in required)):
        raise ValueError("required_capabilities must be a list of at most 12 nonempty names (64 characters each)")
    return sorted(set(CONTRACTS[task_type]["capabilities"] + (required or [])))


def check_capabilities(task_type, required=None):
    needed = capability_requirements(task_type, required)
    unsupported = sorted(set(needed) - set(LOCAL_CAPABILITIES))
    return {
        "available": not unsupported,
        "required": needed,
        "supported": list(LOCAL_CAPABILITIES),
        "unavailable": unsupported,
        "scope": "Local workspace files and terminal execution. Codex connectors and browser tools are not inherited.",
    }


def task_instructions(task_type="implementation", required=None):
    validate_task_type(task_type)
    return (f"Task type: {task_type}.\n" + CONTRACTS[task_type]["instructions"]
            + "\nAvailable gateway capabilities: local_files, shell. "
            "External web, browser and connector capabilities have not been verified by this gateway. "
            "Do not assume access to the parent's tools."
            + "\nRequired capabilities: " + ", ".join(capability_requirements(task_type, required))
            + "\nArtifact paths must be relative to the effective workspace and refer to existing files. "
            "Use an artifact for a substantial answer; the parent handoff is bounded.")


def inspect_artifacts(paths, workspace):
    """Observe reported files without reading content or trusting model paths."""
    root = Path(workspace).resolve()
    evidence, issues = [], []
    for path in paths:
        if not isinstance(path, str):
            issues.append("An artifact path was not a string.")
            continue
        row = {"path": path, "exists": False}
        try:
            relative = Path(path)
            if relative.is_absolute() or relative.drive or ".." in relative.parts or "\\" in path or ":" in path:
                raise ValueError("Artifact paths must use relative forward-slash paths without traversal")
            target = (root / relative).resolve()
            target.relative_to(root)
            if target.is_file():
                row.update(exists=True, size_bytes=target.stat().st_size)
            else:
                issues.append("A reported artifact does not exist as a file in the workspace.")
        except (OSError, ValueError, RuntimeError):
            issues.append("A reported artifact path is invalid or leaves the workspace.")
        evidence.append(row)
    return evidence, list(dict.fromkeys(issues))
