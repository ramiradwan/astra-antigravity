# Delegate to Antigravity

Delegate coding, analysis, research from supplied sources, and document tasks to
Antigravity using your own accounts. Review the result in ChatGPT or Codex.

Remote edition **0.17.0** Â· Windows companion **0.4.1**

Plugin release: **0.9.2** (local edition).

This independent project is licensed under [MIT](LICENSE). It is not an OpenAI,
Google or Anthropic product. Your provider subscriptions, model access, quotas and
terms apply; the plugin does not supply or pool provider accounts.

## Use it from ChatGPT

1. Install the remote plugin and connect your plugin account.
2. Open Computers in ChatGPT. Run its version-pinned setup command on the Windows
   computer that will do the work:

   ```sh
   npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.1/delegate-to-antigravity-0.4.1.tgz setup
   ```

3. Enter the connection code in ChatGPT and approve computer management. Check
   Codex and Antigravity, then use their normal sign-in flows on that computer.
4. Approve a workspace folder and either Git worktree isolation or direct changes.
   Describe the task and review its result in the conversation.

Use the command shown in ChatGPT when setup is available. If ChatGPT has already
saved a task, its command includes `--request-id ID`; preserve that command so the
same task continues once after workspace approval. Do not submit it again.
Node.js 20+ is needed only for npx setup; the installed companion includes its
own runtime.

The companion has no GUI, tray or local web listener. It runs independently after
the setup terminal closes. Keep that computer awake, signed in to Windows and
connected while tasks and result delivery run. Management approval grants no
workspace access until you separately approve a folder.

Use Tasks, Computers and Settings in ChatGPT for task results, provider checks,
workspace preferences, Pause/Resume, Stop tasks, startup, updates, access
revocation and uninstall. Windows startup is off until you enable it. Pause
blocks new tasks while management and existing delivery remain available. Stop
tasks and offline controls stay pending until the computer confirms them.
Removal preserves project files, provider installations and provider sign-ins.
Windows Installed apps and the installed command provide local recovery.
See [computer setup](companion/README.md) and
[connection and recovery](docs/CONNECTIONS.md).

New computer approval can remain until revoked. Short-lived device credentials
rotate without changing task identities or journals. Existing 30-day approvals
keep their expiry unless you explicitly convert that same computer.

## Receive files in ChatGPT

Published files up to **20 MiB** transfer through supported ChatGPT file APIs.
The standard host file API is preferred; otherwise an explicit **Add to ChatGPT**
action uses the documented upload API after checking file size and SHA-256. If
neither API is available, the card explains this and keeps the saved result.
No browser download page or external file download opens. A refused transfer does
not restart the task or silently switch methods. File bytes and host file IDs
stay outside the conversation model's transcript.

Files stream through the relay without a retained relay file copy. The legacy
model-readable export is limited to **12,000 bytes**. These are plugin limits,
not a stated universal ChatGPT file limit. Sending a file to ChatGPT does not
promise permanent Library storage.

## Use the local Codex edition

The separate local edition requires Python 3.10+, authenticated Codex and
Antigravity CLIs, and Git for isolated worktrees. Follow the
[installation guide](plugins/astra-antigravity/docs/INSTALLATION.md):

```text
codex plugin marketplace add ramiradwan/astra-antigravity --ref main
codex plugin add astra-antigravity@astra-antigravity-local
```

Use a published tag instead of `main` to pin a release, or use the absolute path
of a source checkout as the marketplace. Restart the desktop host or start a new
Codex task after installation. Run the setup skill with your workspace to check
dependencies without starting inference.

The local entry point is:

```text
delegate_via_luna(task, workspace, isolation="worktree")
```

Both editions support `implementation`, `analysis`, `research` and `document`.
Research uses supplied sources; delegated work does not inherit your conversation's
browser, web or connector access. Non-Git work needs explicit shared-folder approval.
Codex plans the task; an available Antigravity model performs it. Model access and
quota are checked on your computer. Missing Argon produces an explicit Opus offer.

Compatible hosts push compact decisions and results while the parent continues
independent work. Other hosts wait at a dependency boundary. The conversation
model must not poll. Keep the saved receipt and independently review reported
checks, source support, changed files and document output.

Tasks can run commands and change files using your provider quotas. Antigravity
tool approval is automatic, with terminal sandbox mode **on by default**;
`ASTRA_ANTIGRAVITY_SANDBOX=0` explicitly opts out. A Git worktree separates changes,
not all filesystem access. Disconnection or revocation does not prove local work
stopped. Cancellation does not undo changes. Deleting relay records does not
delete local task files or provider history.

Read the [remote privacy notice](plugins/astra-antigravity-remote/docs/PRIVACY.md)
and [remote use conditions](plugins/astra-antigravity-remote/docs/TERMS.md), or the
[local privacy notice](plugins/astra-antigravity/docs/PRIVACY.md) and
[local use conditions](plugins/astra-antigravity/docs/TERMS.md).

## Support and development

For remote support, contact [Rami Radwan on LinkedIn](https://astra-antigravity-relay.dipsys.workers.dev/support)
with a sanitized description. Use [SECURITY.md](SECURITY.md) for confidential
security reports. Do not send credentials, authentication files, raw task logs or
private source material in a public issue or initial contact.

This repository contains runtime source, dependency locks, build scripts and
tests. The [synthetic local gallery](relay/dev/README.md) runs without provider
commands. Build the current relay UI with `npm run build:ui` in `relay`; committed
compatibility HTML preserves previously shipped resource URIs. See the
[changelog](CHANGELOG.md), [remote edition](plugins/astra-antigravity-remote/README.md)
and [source-managed worker instructions](relay/README.md).

## Host compatibility for source users

Native `spawn_agent` custom-role selection is **not** a production dependency.
The private Codex App Server planning turn works without that role selector.
Optional compatibility probes report `native_custom_role_supported: true|false`
and `mcp_progress_forwarding_supported: true|false` rather than treating an absent
host feature as working. Codex clients may not yet render every MCP progress
event. Milestones such as `Planning delegation` still belong to the bounded
gateway sequence. No polling fallback is used by the conversation model.

Linux compatibility checks need the `bwrap-userns-restrict` AppArmor profile when
the system restricts unprivileged user namespaces.
Windows sharing/access failures are reported for recovery; inspect the saved task before retrying a
cleanup that could discard work. These host checks do not establish provider
model access or quota.
