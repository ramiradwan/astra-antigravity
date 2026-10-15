"""Real stdio protocol fixture: task schema determines work and native output."""

import argparse
import csv
import json
from pathlib import Path
import sys

from jsonschema import validate


def emit(value):
    print(json.dumps(value), flush=True)


def report(kind, workspace, label):
    common = {"summary": f"{kind} {label}", "remaining_work": [], "risks": []}
    if kind == "implementation":
        (workspace / "implementation.txt").write_text(label, encoding="utf-8")
        return {**common, "tests_run": [], "tests_passed": None}
    common.update(checks_performed=["Checked supplied source"], checks_passed=True, artifacts=[])
    if kind == "analysis":
        with (workspace / "data.csv").open(newline="", encoding="utf-8") as handle:
            values = [int(row["value"]) for row in csv.DictReader(handle)]
        answer = str(sum(values) / len(values))
        return {**common, "answer": answer, "findings": ["Mean is " + answer],
                "evidence": ["data.csv value column"], "limitations": []}
    source = (workspace / "source.txt").read_text(encoding="utf-8").strip()
    if kind == "research":
        return {**common, "answer": source, "findings": [{"claim": source, "source_refs": ["notes"]}],
                "sources": [{"id": "notes", "location": "source.txt", "title": "Supplied notes"}], "limitations": []}
    target = workspace / "draft.md"
    target.write_text(f"# Draft\n\n{source}\n\n{label}\n", encoding="utf-8")
    return {**common, "artifacts": ["draft.md"], "review_notes": ["Reviewed the supplied text"]}


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--json-schema", required=True)
    parser.add_argument("--add-dir", required=True)
    parser.add_argument("--input-format")
    parser.add_argument("--conversation")
    parser.add_argument("-p")
    args, _ = parser.parse_known_args()
    path = Path(args.json_schema)
    schema = json.loads(path.read_text(encoding="utf-8"))
    kind = path.name.split("_result")[0]
    workspace = Path(args.add_dir)
    conversation = args.conversation or "typed-conversation"

    def complete(label, turn):
        value = report(kind, workspace, label)
        validate(value, schema)
        emit({"event": "result", "result": {"conversation_id": conversation, "status": "SUCCESS",
              "response": "SECRET raw transcript", "structured_output": value, "num_turns": turn}})

    if args.input_format == "stream-json":
        emit({"event": "init", "conversation_id": conversation})
        turn = 0
        for line in sys.stdin:
            if json.loads(line).get("event") == "user":
                turn += 1
                complete(f"turn {turn}", turn)
    else:
        complete("resumed", 2)


if __name__ == "__main__":
    main()
