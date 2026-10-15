---
name: setup-antigravity
description: Setup and verify dependencies, authentication, and workspace readiness for Delegate to Antigravity delegation.
---

# Setup Antigravity

Verify readiness and configure dependencies before delegating tasks through Delegate to Antigravity.

## Preflight Readiness Check

Use the read-only integration preflight tool from `luna_gateway`:

```text
check_integration(workspace, isolation="worktree", task_type="implementation")
```

This verifies:
- `codex` CLI availability and version
- Antigravity CLI (`agy`) availability and version
- `git` installation and version
- `python` and `uv` package manager readiness
- Pinned runtime MCP SDK version (`1.29.1`) and plugin manifest version (`0.9.2`)
- Configured planner settings (`gpt-6-luna` or `gpt-6.1-sol`, effort `low`)
- Workspace Git eligibility for isolated worktree creation
- Shared-mode readiness for non-Git folders, supported task types and capability requirements
- Per-profile model availability (Sonnet/Opus 5.5, Gemini 3.8 High, Argon High) via non-inference `agy models`
- Separate readiness for light and heavy routing, plus explicit unavailable-model offers
- Actionable remediation instructions for any unmet readiness requirement

## Troubleshooting & Remediation

1. **Antigravity CLI missing or unauthenticated**:
   - Install the `agy` CLI for your platform.
   - Run `agy` in your terminal to complete initial authentication.
   - If installed in a non-standard path, export `AGY_PATH=/path/to/agy`.
   - Note: The gateway MCP server can start even if `agy` is temporarily absent so that readiness checks and inspection tools remain accessible.

2. **Codex CLI missing or non-executable**:
   - Install the Codex CLI (`@openai/codex`).
   - If needed, set `CODEX_PATH=/path/to/codex` (native executable companions are automatically preferred over `.cmd` wrappers on Windows).

3. **Workspace not Git-eligible**:
   - Initialize a Git repository (`git init`) or ensure the workspace checkout has a clean `HEAD`.
   - Worktree isolation requires a valid Git repository root.
   - For authorized non-Git work, choose `isolation="shared"` explicitly in both preflight and delegation; it performs changes in the supplied folder.

4. **Planner model configuration**:
   - Set `ASTRA_ANTIGRAVITY_PLANNER_MODEL=gpt-6-luna` (default) or `gpt-6.1-sol`.
   - The reasoning effort is always `low`; invalid models fail closed without fallback.

Readiness uses non-inference commands and does not prove model access or authenticate on the user's behalf. The first planner turn checks Codex access. If Argon is absent, report it and offer Opus 5.5 with its actual CLI availability; obtain the user's model choice before retrying. Light tasks can use Sonnet 5.5 or Gemini 3.8 High. Never substitute Claude 4.6 for 5.5. A dirty or non-Git workspace is not eligible for default worktree isolation.

Preferred profiles can be set with `ASTRA_ANTIGRAVITY_LIGHT_MODEL=sonnet-5.5|gemini-3.8-high` and `ASTRA_ANTIGRAVITY_HEAVY_MODEL=argon-high|opus-5.5`. Empty or unsupported settings fail closed. Existing tasks retain their model and effort pins.

Noninteractive probes close stdin and bound execution/output. Readiness runs outside the MCP event loop, so other requests remain responsive during a slow probe.
