# Delegate to Antigravity remote edition

Remote edition **0.17.0** connects ChatGPT or Codex to your computer through an
authenticated HTTPS relay. Your computer runs the tasks using your own Codex and
Antigravity accounts. It needs no local MCP listener or inbound port.

## First use

Install the plugin, connect your plugin account, and open Computers in ChatGPT.
On the Windows computer that will do the work, run the version-pinned setup command
shown there:

```sh
npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.2/delegate-to-antigravity-0.4.2.tgz setup
```

The GitHub tarball needs no npm publishing account. npx obtains the bootstrap
through HTTPS; the bootstrap verifies the signed companion manifest and installer
hash. The installed companion includes its runtime and runs without Node.js,
Python, uv, a GUI, tray or local web listener. See
[computer setup](https://github.com/ramiradwan/astra-antigravity/blob/main/companion/README.md).

Enter the connection code in Computers and approve management access. Check or
install Codex and Antigravity, complete their normal sign-in on that computer,
then approve a workspace folder. Management approval alone allows no task access.
Keep the same plugin sign-in method and account on each device. Different sign-in
providers create separate identities even when their email addresses match.

If you supplied a task before setup, the card saves its receipt and adds
`--request-id ID` to its setup command. Keep that command. Workspace approval
queues the same request once, without repeating your prompt. Reconnection keeps
task identity and does not repeat inference.

## Tasks and files

The ChatGPT card shows connection, task status, decisions and results. It
refreshes while visible without asking the conversation model to poll.
Background notifications show as connected only after the host has established
an authorized subscription; support varies by host. Other supported hosts retain
bounded tools for saved results.

Use the result card to review worker-reported checks, or request independent
review in the conversation. Published files up to **20 MiB** transfer through
supported ChatGPT file APIs while the computer is connected. The host file API
is preferred; compatible hosts can instead accept an explicit **Add to ChatGPT**
action. If neither API is available, the card explains this and the saved file
stays on your computer. No browser download page opens. Transfers stream through
the relay without a retained file copy. The model-readable legacy export remains
limited to **12,000 bytes**. A transfer or review does not rerun the task and does
not promise permanent ChatGPT Library storage.

## Computer controls

Use Computers and Settings to Pause/Resume, Stop tasks, choose Windows startup,
review updates, revoke access or uninstall. Startup is off until enabled. Pause
keeps management reachable. Stop tasks and offline controls remain pending until
the computer acknowledges them.

New computer approval and plugin sign-in can remain until revoked. Existing
30-day approvals need explicit conversion. Credential rotation preserves task
identities and journals. Verified updates install when idle. Windows Installed
apps and the installed command provide local recovery and removal. Uninstall
preserves project files, provider installations and provider sign-ins.

Review permissions when connecting the plugin. Computer management enables setup
and controls; account deletion is a separate permission. Older connections stay
view-only for computer settings until reconnected with management permission.
This does not change an existing computer's approval.

## Scope and installation formats

Your provider terms, subscription, model availability and quota apply. Relay
sign-in does not sign your computer into Codex or Antigravity or provide model
access. Light tasks use an available Sonnet 5.5 or Gemini 3.8 High profile. Heavy
tasks use Argon High or explicitly selected Opus 5.5. Missing Argon is disclosed
with an Opus offer; it is not silently substituted. Research uses supplied
sources. Tasks do not inherit the parent's web, browser or connector access.

Keep the computer awake and connected until work and delivery finish. A worktree
separates changes; it does not provide an OS sandbox. Revocation does not prove
local cancellation, and cancellation does not undo changes.

This source uses Agent Plugins 1.0.0. The GitHub marketplace's remote entry uses
the same hosted HTTPS endpoint as the packaged remote edition. The separate local
**0.9.2** edition is in `plugins/astra-antigravity`. See the
[source-managed worker guide](../../relay/README.md),
[privacy notice](docs/PRIVACY.md) and [use conditions](docs/TERMS.md).

For support, contact [Rami Radwan on LinkedIn](https://astra-antigravity-relay.dipsys.workers.dev/support)
with a sanitized description.
