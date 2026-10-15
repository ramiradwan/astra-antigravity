# Installation and support

## Requirements

- Python 3.10 or later and uv, or the locked Python dependencies preinstalled.
- Authenticated Codex CLI and Antigravity CLI (`agy`) with the requested models.
- Git for worktree isolation; a clean repository with a committed HEAD.
- A supported local Codex host. Background delivery needs the host relay described
  in the delegation skill; other hosts use foreground delivery at a dependency boundary.

The Python dependency set is in `requirements.lock`. Normal startup uses an
isolated uv environment; its first start needs network access to download packages.
Without uv, from the installed plugin root run:

```text
python -m pip install --require-hashes -r requirements.lock
python launcher.py --version
```

The portable `mcp.json` invokes `python`, so ensure that executable is on the host's
PATH and meets the Python requirement. `ASTRA_PYTHON_PATH` selects the interpreter
used by the launcher after that initial Python invocation. `CODEX_PATH`, `AGY_PATH`
and `UV_PATH` can select executable files outside PATH.

The portable MCP schema does not include host-specific environment forwarding
or tool timeouts. In Codex, configure these in the plugin's server settings in
`config.toml` when using environment overrides or long foreground calls:

```toml
[plugins."astra-antigravity@astra-antigravity-local".mcp_servers.luna_gateway]
startup_timeout_sec = 60
tool_timeout_sec = 3600
env_vars = ["CODEX_HOME", "CODEX_PATH", "AGY_PATH", "UV_PATH", "ASTRA_PYTHON_PATH",
  "ASTRA_ANTIGRAVITY_GIT_PATH", "ASTRA_ANTIGRAVITY_PLANNER_MODEL",
  "ASTRA_ANTIGRAVITY_LIGHT_MODEL", "ASTRA_ANTIGRAVITY_HEAVY_MODEL",
  "ASTRA_ANTIGRAVITY_SANDBOX", "ASTRA_ANTIGRAVITY_STATE_DIR",
  "ASTRA_ANTIGRAVITY_QUOTA_WARN_FRACTION", "ASTRA_ANTIGRAVITY_QUOTA_MIN_START_FRACTION",
  "ASTRA_ANTIGRAVITY_WORKTREE_ALLOW_DIRTY_SOURCE", "ASTRA_ANTIGRAVITY_WORKTREE_ROOT"]
```

These are variable names, not credentials or values. Keep authentication with
the provider CLIs. Other hosts use their own server configuration controls.

## GitHub installation

Install the local edition from the public marketplace. Use an available release
tag in place of `main` to pin a version.

```text
codex plugin marketplace add ramiradwan/astra-antigravity --ref main
codex plugin add astra-antigravity@astra-antigravity-local
```

For a checkout, use its absolute path in the marketplace-add command. The
`astra-antigravity-local` marketplace name is stable for existing installations.
Restart the desktop host or start a fresh Codex task after updating.

Invoke `setup-antigravity` with a workspace to run `check_integration`. This
read-only check reports dependencies, models and worktree eligibility without
model inference. Authenticate with each CLI's normal flow; do not put tokens in
plugin configuration or support reports. A first delegated task validates planner
model access and consumes provider quota.

## Model and task limits

Light tasks prefer Sonnet 5.5 Thinking, with Gemini 3.8 Flash High when Sonnet is
absent. Heavy tasks prefer Gemini 4 Argon High. If Argon is unavailable, the plugin
explicitly offers Opus 5.5 Thinking and waits for the user's choice; it does not
silently substitute a model. Your actual CLI catalog determines availability.

Select `implementation`, `analysis`, `research` or `document`. Research in this
edition uses supplied local sources. Declare capability requirements; unavailable
web, browser or connector requirements stop before inference. Non-Git folders
require explicit shared isolation. Analysis and research instructions restrict
changes, but shared mode is not a filesystem read-only boundary.

## Verification and support

The parent independently verifies changed files, evidence or document artifacts.
Saved handoffs are bounded; use local source files when more evidence is needed.
Cancel owned tasks before shutting down or removing local history. Review
[privacy](PRIVACY.md) and [use conditions](TERMS.md).

Report reproducible issues through the
[support page](https://github.com/ramiradwan/astra-antigravity/issues).
Include plugin/CLI versions, operating system, sanitized `check_integration`
findings and a minimal reproduction. Remove workspace paths, tokens, raw prompts
and private data. For a security issue, follow the repository's `SECURITY.md`
rather than opening a public issue with exploit details or secrets.
