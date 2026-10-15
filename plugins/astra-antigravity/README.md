# Delegate to Antigravity

Independent MIT-licensed Codex plugin for delegating implementation, analysis,
supplied-source research and document tasks through lightweight Luna planning.

This release uses local Python, Codex and Antigravity CLIs with your own accounts.
It can run commands and modify authorized workspaces. Review the setup skill,
[installation guide](docs/INSTALLATION.md), [privacy notice](docs/PRIVACY.md) and
[use conditions](docs/TERMS.md) before delegation.

Repository and maintainer: [ramiradwan/astra-antigravity](https://github.com/ramiradwan/astra-antigravity).

The package does not contain provider accounts, tokens or proprietary CLI binaries.
`requirements.lock` specifies the Python runtime dependencies; the source package
uses uv to download them on first startup. The bundled-runtime package includes
its SDK dependencies. Root `plugin.json` and `mcp.json` use Agent Plugins 1.0.0.
