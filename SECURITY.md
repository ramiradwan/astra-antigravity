# Security and confidential reports

This plugin runs local programs and delegates work to your authenticated Codex and Antigravity CLIs. Use workspaces you are authorized to share with those providers. Review delegated output before integration.

Antigravity commands use automatic tool approval. Its terminal sandbox is requested by default, but Git worktrees and input-preservation instructions do not provide a filesystem security boundary. Disabling the terminal sandbox is an explicit local configuration choice.

Task state and raw event logs can contain private prompts, paths, file content and model output. Do not attach credentials, authentication files, raw task logs or private source material to public issues. A useful report includes plugin/CLI versions, operating system, delivery mode, a minimal synthetic reproducer and sanitized error text.

Use GitHub's **Report a vulnerability** option when available on the repository. Otherwise use a contact method the [maintainer publishes on their profile](https://github.com/ramiradwan) and request a confidential channel before sharing sensitive details. Do not disclose an exploitable issue in a public issue.

Supported releases: local plugin 0.9.x, remote plugin 0.17.x, and Windows
companion/bootstrap 0.4.x. Use the latest patch release in each line. Support and
fixes are provided on a best-effort basis. There is no promised response deadline.
