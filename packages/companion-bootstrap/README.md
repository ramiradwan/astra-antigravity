# Set up the computer companion

Run the version-pinned command shown in **Computers** in ChatGPT on the Windows
computer that will perform your tasks. Node.js 20 or later is needed for setup:

```sh
npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.1/delegate-to-antigravity-0.4.1.tgz setup
```

The command downloads this bootstrap through GitHub HTTPS. The bootstrap verifies
the publisher's signed companion release and installer checksum, then installs
the headless companion for your Windows user. The bootstrap itself is trusted
through GitHub HTTPS; npx does not verify a detached bootstrap signature.
An unavailable release or a failed signature check stops setup before installation.

Setup prints a computer connection code. Return to ChatGPT to approve the
computer, connect your provider accounts and approve a workspace. Installing
does not grant workspace access or run a task. A setup card can add
`--request-id ID` to preserve your original saved task.

The companion runs independently, without a dashboard, tray icon or local web
listener. You can close the terminal after setup. Node.js is not needed afterward.
Provider authentication uses the providers' normal sign-in flows on that computer;
passwords and account tokens do not go in chat. Provider access remains subject
to subscription availability, quotas and terms.

Manage tasks, Pause/Resume, startup, updates and removal in ChatGPT. Startup at
Windows sign-in is off until enabled. The installed command and Windows
Installed apps provide local recovery. Uninstall preserves project files,
provider installations and provider sign-ins. See the
[computer guide](https://github.com/ramiradwan/astra-antigravity/blob/main/companion/README.md).

The setup command accepts no relay override, shell commands, account tokens or
workspace paths. It does not run an MCP server in the terminal.
