"""Validate the model-owned part of a handoff independently of diagnostics."""

from jsonschema import Draft202012Validator

from task_types import result_schema, validate_task_type

FIELDS = frozenset({"summary", "tests_run", "tests_passed", "remaining_work", "risks"})


def validate_semantic_report(value, task_type="implementation"):
    validate_task_type(task_type)
    if not isinstance(value, dict):
        return {}, f"No structured {task_type} report was available."
    if not Draft202012Validator(result_schema(task_type)).is_valid(value):
        # Preserve the legacy implementation parsing surface; new task kinds
        # cannot promote malformed output into an analytical/document answer.
        return value if task_type == "implementation" else {}, f"The structured {task_type} report did not match its schema."
    if task_type == "research":
        ids = [source["id"] for source in value["sources"]]
        if len(set(ids)) != len(ids) or any(ref not in ids for finding in value["findings"]
                                          for ref in finding["source_refs"]):
            return {}, "Research source IDs must be unique and all finding references must resolve."
    return value, None
