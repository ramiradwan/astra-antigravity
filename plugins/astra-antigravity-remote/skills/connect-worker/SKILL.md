---
name: connect-worker
description: Guide first-use account and workspace connection for Delegate to Antigravity, preserving a saved first task through setup.
---

# Connect your worker

Use the relay's offered account sign-in flow. Read `list_workspaces` once.
If an approved workspace exists, use its alias and read cached `check_integration`
once. An offline computer needs its background companion running and network access.

If the user already supplied a task and setup is needed, call `prepare_delegation`
once with a stable `request_id`, the authorized brief, task type and required
capabilities. Omit `workspace_id` until a workspace is chosen. Use `isolation="auto"`
only when the user will choose isolation during workspace approval; preserve any
explicit isolation requirement. Keep the returned `request_id` and `delegation_id`
with the original brief. Keep its setup URL as a recovery option.
On MCP Apps hosts, call `show_delegation` once with that delegation ID to present
the connection action and later task state in one card. For connection without
a task, omit the ID. Its component refreshes itself; never call its app-only
watch or export tools from the parent model.

Call `show_computers` once for setup and management in ChatGPT. The component
handles computer approval, provider checks/install/sign-in, workspace permission,
startup, updates and removal. Use only the version-pinned setup command the
server offers. If unavailable, report that state; do not invent a command or
open a browser download route.
The headless Windows companion prints a connection code; that code can be entered
in the card. The companion has no default GUI or local web listener. Provider
authentication uses its normal browser/device flow. Computer approval grants
management first; task execution requires separate workspace approval and provider
readiness. The card retains the original task request through these steps.
After approval, the same saved task queues once. Never resubmit it with another ID.

Use the same offered sign-in method and account. GitHub and Microsoft Entra
identities remain separate even when email addresses match. Plugin sign-in does
not authenticate the computer's Codex or Antigravity subscriptions.

Browser/mobile installation needs a one-time companion installation on a computer
the user owns. Do not promise that installing a web plugin installs local software.
Do not ask for provider passwords, tokens, pairing claims or CLI output in chat.
The companion runs independently of the setup terminal. Startup is an explicit
ChatGPT setting. New computer approval can last until revoked with rotating
credentials. Existing 30-day approvals require explicit conversion. Pause keeps
management reachable; Stop tasks requires worker confirmation. Offline removal
stays pending. Revocation is immediate server access removal and does not prove
local work stopped or software was uninstalled. Recover uncertain tasks using
their original receipt; sleep, expiry or revocation never authorizes a new launch.

Setup mechanics and connection checks belong in deterministic code. The parent
receives one useful setup action and compact decisions/completion. Use the host's
supported event subscription for the saved delegation; never poll setup or task
status in a model loop. If the host lacks background delivery, disclose it and
read the original receipt at the dependency boundary.

The worker uses an outbound connection and an in-process gateway; it needs no
local MCP listener or inbound network port. Keep private state outside task workspaces.
Workspace permission is validated on the computer against an existing directory;
only explicit user-approved configuration may register its alias.
Readiness is a non-inference probe and is cached, not a guarantee of model access.

The packaged companion supports Windows. This plugin does not provision cloud
containers; provider sign-in and task lifetime on other hosts require independent
verification. Do not promise an always-on cloud computer or put authentication
or pairing state into shared environment snapshots.
