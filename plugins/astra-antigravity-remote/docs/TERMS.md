# Delegate to Antigravity remote use conditions

Remote edition 0.17.0, prepared 16 October 2026. Digipsykologia Oy operates the
hosted relay; Rami Radwan authors the independent MIT-licensed plugin. No public
service availability or directory approval is implied by this source package.

Use your own Codex and Antigravity accounts on a worker you control. Provider
terms, model access, subscriptions, quotas and permitted uses apply. The service
does not sell, pool or supply provider accounts and is not endorsed by OpenAI,
Google or Anthropic. You are responsible for authorizing task content and the
files and commands available to your worker.

Tasks can run commands and modify or delete files. Antigravity tool approval is
automatic, while terminal sandboxing is requested by default. Git worktrees
separate changes; they do not prevent every read accessible to the CLI's OS user.
Keep private state outside approved workspaces and review output before use or
integration. Cancellation is not rollback. Cleanup can discard changes when
force is selected and must be explicitly requested after needed work is saved.

The companion is a headless background application managed through ChatGPT.
Computer approval and plugin sign-in can continue until revoked. Startup is your
choice; verified automatic updates wait for idle. Pause retains management access.
Stop, offline changes and uninstall require computer confirmation before they are
reported complete. Revocation blocks relay access immediately and does not prove
local work stopped. Uninstall preserves project files and independent provider
accounts; review task results and provider history separately.

The relay uses Cloudflare Workers Free with shared request, duration, storage and
write quotas. Exhaustion or outages can stop task delivery; the service does not
upgrade or switch to paid infrastructure. The worker must stay available.
Disconnection, expired pairing or missing ownership is not permission to start
the same task again. Keep launch IDs and inspect saved evidence before authorizing
new work. See the documented task and recovery retention windows.

Codex Cloud is a candidate task-scoped worker host. Its actual CLI authentication,
network access and task lifetime need validation in that environment; this plugin
does not provision containers or guarantee an always-on endpoint. ChatGPT Work
cloud supports the documented event protocol; generic CLI push is not guaranteed.

The software is provided under the [MIT license](../LICENSE), including its
warranty disclaimer. These conditions do not waive rights that applicable law
does not allow to be waived. Read the [privacy notice](PRIVACY.md) before submitting
tasks. For support, contact [Rami Radwan on LinkedIn](https://astra-antigravity-relay.dipsys.workers.dev/support) with a sanitized description.
