# Task contracts

Use this reference for analysis, supplied-source research, and document work. The normal background relay applies to every type; task types do not add coordinator turns or progress traffic.

## Dispatch

```text
delegate_via_luna(
  task="Calculate the mean in data.csv, explain the result and cite the input column.",
  workspace="/absolute/existing/folder",
  isolation="shared",
  task_type="analysis"
)
```

For research, supply local source files or excerpts and ask for findings with matching source IDs. To draft or revise a document, choose `task_type="document"` and specify the output file(s) and format. The worker must produce actual artifacts. Request an artifact for a long answer so the complete content stays on disk rather than filling the parent context.

Default `isolation="worktree"` requires a clean Git repository with a committed HEAD. `"shared"` accepts non-Git directories and performs requested changes there. No silent isolation fallback occurs. Analysis/research instructions preserve inputs; the CLI terminal sandbox is not a filesystem read-only guarantee. Use copied inputs or a worktree when source isolation matters.

## Capabilities

`local_files` and `shell` describe the current gateway's supported execution surface. Requirements are checked deterministically before the private planner or Antigravity execution. Explicit web, browser, connector, or other unsupported requirements return `capability_unavailable` with the unavailable names. No extra model request checks capabilities, and no configuration override fabricates access.

Research defaults to supplied/local material. If the original task needs fresh internet or connector evidence, declare that requirement; do not omit it to get past preflight. The parent can gather authorized source material using its own tools, then delegate analysis of that material. Source URLs in a supplied excerpt are citations, not proof that the worker browsed them.

Use `check_integration(workspace, isolation="shared", task_type="research")` to check a non-Git workspace without inference. Dependency readiness does not prove authentication or model access.

## Saved result

The receipt, saved task record, events, and handoff identify `task_type`. Corrections and cold resumes retain that type and its native schema. Records predating task types remain implementation tasks.

Implementation retains the `implementation` result object. Other types use `result`:

| Type | Specific fields |
| --- | --- |
| `analysis` | `answer`, `findings`, `evidence`, `limitations` |
| `research` | `answer`, `findings[{claim, source_refs}]`, `sources[{id, location, title}]`, `limitations` |
| `document` | `artifacts`, `review_notes` |

Non-implementation reports also contain `summary`, `checks_performed`, nullable `checks_passed`, `artifacts`, `remaining_work`, and `risks`. Empty software-test arrays are not required. Research source IDs must be unique, and each finding reference must resolve to a reported source; unsupported inferences can use an empty reference list and must be described as inferences.

Artifact paths are relative forward-slash paths within the effective workspace. The gateway observes file existence and size as `artifact_evidence`; absolute paths, traversal and symlink escapes fail structural verification. Git-derived workspace/change information remains authoritative where available. Non-Git change lists cannot enumerate all filesystem activity.

## Parent verification

Retrieve the saved handoff once after a pushed terminal event. Analysis: reproduce calculations and compare findings with the actual input. Research: open the cited sources and check claim support, dates, and limitations. Document: open artifacts and check content, requested format, and rendered layout where relevant. Do not equate the worker's `checks_passed` or the gateway's `verification.structural_checks_passed` with acceptance.

The parent handoff remains bounded to 8,000 escaped characters with explicit truncation. If evidence is clipped, inspect the requested artifact or archived task metadata before accepting the result. Pushed events stay within 1,600 characters and contain type, short summary, check/artifact counts, verification flags, and `result_ref`; answers, findings, citations and file contents stay out of the event stream.

If verification finds a defect, use `continue_via_luna(task_id, findings)` on that exact task. Changing task type or capabilities requires a new explicit delegation rather than repurposing a correction.
