# Connect and recover

The local and remote editions use your own Codex and Antigravity accounts.
Plugin sign-in is separate from the provider sessions on the computer doing work.
Your provider subscriptions, availability, quotas and terms continue to apply.

## Connect in ChatGPT

Follow [computer setup](../companion/README.md). Use the same plugin sign-in
method and account across devices; matching email addresses do not link separate
identities from different sign-in providers.

On the Windows computer that will run tasks, run the version-pinned command shown
in ChatGPT's Computers panel:

```sh
npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.2/delegate-to-antigravity-0.4.2.tgz setup
```

Use the card's `--request-id ID` when it has saved a first task. Setup downloads
the bootstrap from GitHub HTTPS; no npm publishing account is needed. The
bootstrap verifies the signed companion release manifest and installer hash.
npx itself does not verify a detached signature for the bootstrap tarball.

Paste the connection code into Computers and approve management access. This
grants no workspace access. Check or install provider tools, request their normal
sign-in, then approve the specific folder and isolation mode in ChatGPT. The
folder path passes through the relay as private component-only operation data;
it is not sent to the conversation model. Provider credentials stay in the
providers' normal stores on your computer.

Workspace approval queues the saved task once. Keep that saved request rather
than repeating its prompt. The headless companion runs independently without a
GUI, tray or local listener; Node.js is not required after installation.

## Manage the computer

Use Tasks and Computers to inspect saved status before retrying uncertain work.
The same computer reconnects after network interruption without restarting it.
Pause keeps management and existing task delivery running while blocking new
tasks. Stop tasks requests cancellation; it stays pending until the computer
confirms. Offline changes also stay pending until received and acknowledged.

New approval can remain until revoked, with rotating short-lived device
credentials. Existing 30-day approvals retain their original expiry unless
explicitly converted for the same computer. Conversion keeps saved identities
and workspaces. Revoked or removed identities need fresh approval for future work.

Verified automatic updates wait for idle, preserve private state and retain a
prior verified version for startup rollback. An unreachable release feed does
not stop current work. Windows startup is opt-in. A local Quit drains work and
requires relaunch or a later enabled Windows sign-in to reconnect.

## Remove access or uninstall

Request removal in ChatGPT or Windows Installed apps. Project files, provider
accounts and local history are preserved by default. Local cleanup and delivery
of its receipt are separate: a lost or expired completion remains **unconfirmed**,
not proof that the application is installed or removed. Follow the
[local recovery commands](../companion/README.md#local-fallback-commands) if needed.

Revocation blocks future relay access but does not prove provider cancellation.
Account export and deletion are in Settings. Deletion revokes application access
and removes retained relay records; local files and provider history remain.

## Other hosts and support

The [local installation guide](../plugins/astra-antigravity/docs/INSTALLATION.md)
covers the separate stdio edition. Background delivery requires a compatible
host. Other hosts disclose the limit and inspect saved results at a dependency
boundary without model polling. For a source-managed remote computer, see
[CLI setup](../relay/README.md).

Contact [Rami Radwan on LinkedIn](https://astra-antigravity-relay.dipsys.workers.dev/support)
for remote support. Start with a sanitized description; do not send provider
credentials, pairing proofs, raw task logs or private source material.
