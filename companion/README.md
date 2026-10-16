# Connect your computer

Delegate to Antigravity uses a headless Windows companion to run authorized tasks
with your Codex and Antigravity accounts. ChatGPT is the main interface for setup,
tasks, files, and computer settings. The companion has no dashboard, tray icon,
or local network listener.

Start setup on the Windows computer that will run your tasks:

```sh
npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.2/delegate-to-antigravity-0.4.2.tgz setup
```

Use the version-pinned command shown in Computers in ChatGPT. It is enabled only
after the public bootstrap and signed Windows release have been verified. Node.js 20
or later is only needed
for this setup convenience; the installed companion includes its own runtime and
can run without Node.js, Python, uv, or a source checkout.

The setup command verifies the publisher's signed release, installs for the
current Windows user, starts the background agent, and returns a public pairing
code to use in ChatGPT. It does not approve the computer or run a task. If ChatGPT
already saved your task, use its supplied setup command with `--request-id ID` to
retain that receipt. Do not resubmit the task under a new ID.

In ChatGPT:

1. Approve the displayed computer. Approval remains until you revoke it; device
   credentials rotate without asking you to approve the same computer again.
2. Check the installed provider tools and existing sign-ins. Install a missing
   tool or request its normal sign-in when needed. Provider sign-in windows open
   only after an explicit sign-in request; passwords and tokens never go in chat.
3. Approve a workspace folder and either an isolated Git worktree or direct
   changes. A new computer initially has no workspace access. Tasks can execute
   commands and modify files in the approved scope, using your provider quotas.
4. Choose whether to start at Windows sign-in and install verified updates
   automatically when idle. Windows startup is off until you enable it.

An existing workspace alias cannot be changed to another folder or isolation
mode. Add a different alias to approve another workspace. Saved tasks keep their
original workspace and provider handles.

After setup, keep the computer awake, signed in to Windows, and connected to the
internet. Closing the setup terminal does not stop the agent. ChatGPT shows the
last observed state; an offline computer cannot receive a command immediately.

**Pause** prevents new tasks while the agent stays reachable for management and
existing task delivery. **Resume** allows new tasks again. **Stop tasks** requests
cancellation and waits for confirmation; a request receipt is not proof that the
provider has stopped. Full **Quit** is a local action and waits for existing work
and delivery. It does not cancel tasks or erase their saved state.

Automatic updates download a signed Windows release, verify its signature and
checksum, and wait until tasks, setup actions, and delivery are idle. A stable
per-user launcher selects the installed version. Startup validation runs without
accepting tasks; failure restores the previous verified version. Task journals,
workspace approvals, and pause state are retained. A failed candidate is not
retried indefinitely. An unpublished or unreachable release feed is reported as
unavailable; it does not prevent current tasks from running.

Provider sessions can expire or be revoked. A provider may require interactive
sign-in, consent, or account recovery. Model access and quota are checked when a
task runs; a successful tool check does not prove inference access. Setup does not
switch API billing modes, buy credits, or change your subscriptions.

## Local fallback commands

The stable entry point is `%LOCALAPPDATA%\AstraAntigravity\AstraAntigravity.exe`.
These commands do not open a companion interface:

```powershell
& "$env:LOCALAPPDATA\AstraAntigravity\AstraAntigravity.exe" --status
& "$env:LOCALAPPDATA\AstraAntigravity\AstraAntigravity.exe" --stop
& "$env:LOCALAPPDATA\AstraAntigravity\AstraAntigravity.exe" --uninstall
```

`--status` returns bounded public JSON. `--stop` queues a graceful Quit. Windows
sign-in startup can launch the agent again; disable that setting in ChatGPT if you
want it to remain off at the next sign-in.

Removal can also be requested in ChatGPT or Windows Installed apps. Remote
removal remains queued until this computer receives it. The agent stops new work,
finishes current tasks and delivery, then removes its installed application,
startup entry, and connection handler. A success is reported only after local
cleanup; a lost completion connection remains explicitly unconfirmed.

Removal clears the companion's connection proofs. It preserves workspace files,
Git worktrees, provider installations and sign-ins, and saved task history by
default. Erasing companion task history and connection archives is a separate
explicit choice. Unknown files or redirected installation paths are preserved
for local review rather than deleted. Local private state is under
`~/.local/state/astra-antigravity-companion`; provider credentials remain in the
providers' normal stores. A temporary cleanup helper can briefly remain while
sending the final receipt and deleting itself.

If network access is lost after local removal, the helper retries confirmation
for up to five minutes, then retains only its receipt capability and recovery
journal. No task agent, service, or startup entry remains active. The public
local status identifies the retained `maintenance-helper-…exe`. Run that verified
helper with `--retry-removal` to resend the receipt; add `--state` only if you
originally chose a custom state directory. This command cannot run tasks, install
software, or access provider accounts. Confirmation expiry becomes **unconfirmed**,
not a claim that the application is installed or removed; the expired capability
cannot authorize further requests. Retrying clears the expired local capability.

Running setup again after a verified successful removal archives the preserved
old connection and creates a new management-only pairing. Old tasks remain
archived and are not restarted. A damaged connection without a successful
removal receipt is preserved for review. To deliberately start a new connection,
quit the companion, then run its executable with `--install --new-connection`.
This archives the prior identity for reference and requires fresh approval.

## Source builds

Windows is the packaged target. Build and verify on each native platform before
distributing Linux or macOS packages. Browser and mobile ChatGPT users still need
a running agent on a computer they own.

```sh
uv run --python 3.13 --no-project --with-requirements companion/build-requirements.lock python scripts/build_companion.py
```

The build writes an executable, a ZIP with vendor notices, and checksums under
`dist/companion/`. The standalone executable also contains the notices and copies
them into its versioned installation. Builds embed only the public release feed
and verification key. The publisher's private signing key is never included.

For an independently managed worker, see the [CLI instructions](../relay/README.md).
