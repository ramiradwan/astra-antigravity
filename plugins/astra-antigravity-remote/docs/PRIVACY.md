# Delegate to Antigravity relay privacy notice

Remote edition 0.17.0. Updated 16 October 2026. This describes the implemented
relay and worker; public service activation is separate from installing the source.

Digipsykologia Oy is responsible for operating the hosted relay. Rami Radwan is
the open-source plugin author. The service is intended for worldwide use where
your providers make the required accounts and models available.

## What the relay processes

Your chosen GitHub or Microsoft Entra sign-in supplies account identifiers used
to bind your workers and tasks to you. Entra sign-in validates the tenant,
application, user object identifier and any configured app-role requirement. The relay derives
an account key from that identity; it does not store Entra email addresses or names
as account identifiers or link accounts by matching email. The relay processes
worker names, approved workspace aliases, capability
and model readiness, pairing records and authorization records. Workspace
registration sends aliases, not discovered absolute paths; handoffs replace known
workspace paths with aliases. If you enter a folder path into ChatGPT's setup
card, that explicit setup input passes through the relay to your computer for
validation. Task content and results can contain other paths. The relay also
processes installation/sign-in readiness, companion version, Pause/Resume,
startup/update preferences and bounded management-operation receipts. It does not
receive provider authentication output or credentials during these checks.

When product enrollment is configured in a shared Entra directory, Microsoft sends
authenticated signup and token-issuance events to the relay. The relay uses the
application and user object identifiers to record the product that created the
account and add the account to that product's informational group. Signing in to
another configured product can add another group without replacing the original
registration source. These labels help the operator administer shared-directory
accounts; they do not grant application roles or access by themselves. Authorized
directory administrators can see the labels. The relay processes the event only
for this purpose and does not retain its full payload.

To deliver your instructions, the relay receives the task brief, task type,
model controls and corrections you submit. It retains queued dispatch content
until the worker confirms acceptance. It also receives compact decision events,
bounded results, verification metadata and artifact references. An artifact's
content reaches the relay only when you request its listed export. Those records
may contain personal data or confidential content supplied in your task or result.
The relay is not end-to-end encrypted against its operator; authorized hosting
administrators can access the service's retained records.

In-chat cards receive connection status, task summaries, selected models,
worker-reported checks and published file metadata. Routine card refreshes do not
add those snapshots to the conversation model's context. Your ChatGPT or Codex
host still processes the card and tool data under its own account settings and
terms. An explicit response or review action can add a message to your conversation.

For transfers through ChatGPT's supported file APIs up to 20 MiB, requested file bytes pass through the relay
as a bounded stream and are not saved in relay artifact storage. The file remains
on your computer. Existing small-file exports up to 12 KB may be cached with the
task records. File-transfer approval metadata expires after five minutes. The
Add to ChatGPT action uploads your chosen file to ChatGPT and requests that it
also be saved in your ChatGPT file library when that feature is available. OpenAI
handles that copy under your ChatGPT account settings and terms. The plugin does
not offer an external browser download page if host file APIs are unavailable.

We use these records to authenticate you, route authorized work, recover lost
receipts, deliver subscribed outcomes and let you inspect your retained results.
Providing the requested service is the purpose for account and task processing.
Security and recovery records support our legitimate interest in preventing
unauthorized access and duplicate execution. Do not submit data you lack authority
to share; use a suitable separate arrangement for regulated or sensitive workloads.

The relay does not receive your Codex or Antigravity auth caches, passwords or
provider tokens. Raw planning reasoning, provider transcripts and ordinary tool
logs stay on your worker. The relay does not invoke model APIs or train models.
Your chosen providers govern their own processing, retention, training settings,
locations and account protections.

## Hosting, recipients and browser storage

Cloudflare hosts the HTTPS relay, OAuth storage and per-account mailboxes. It
handles requests and network metadata under its own service terms and privacy
practices. Application payload logging and application analytics are disabled.
The software has no advertising, tracking pixel or publisher model-training path.

When Microsoft sign-in uses our custom login domain, a separate Cloudflare Worker
forwards authentication requests to Microsoft Entra. Cloudflare terminates HTTPS
on that domain, so sign-in form submissions, verification codes, authentication
cookies and OAuth requests and responses pass through its infrastructure. The
proxy streams this traffic to and from Microsoft without application storage,
payload logging or caching. Microsoft still performs authentication and controls
its sign-in cookies and identity records. This proxy does not handle your Codex
or Antigravity account credentials.

GitHub and Microsoft provide sign-in identity when the corresponding sign-in
method is enabled. LinkedIn provides the support contact channel. GitHub's sign-in
access token and Microsoft's identity response are used transiently to verify
identity; the relay does not retain upstream provider tokens or request GitHub
repository access. Microsoft sign-in requests basic identity (`openid profile`),
not mail, file or directory-reading permissions. Signing in to this plugin does not
sign the worker in to Codex or Antigravity. Necessary encrypted session and sign-in cookies support
authentication and pairing consent. The management session lasts one hour;
pending sign-in and pairing expire after ten minutes.

Product enrollment uses a separate operator-managed Microsoft Graph identity to
add group members. It does not add directory permissions to your sign-in consent.
The relay sends Microsoft the user and product-group identifiers needed for that
update. Cloudflare temporarily holds an encrypted user identifier, a pseudonymous
lookup key and bounded enrollment delivery records while the update is retried.

Your worker sends authorized content to Codex and Antigravity under your own
accounts. The selected model provider and any destinations contacted by your
authorized task apply their own rules. A registered directory and Git worktree
are not complete OS isolation: CLIs can access files permitted to their OS user.
Antigravity tool approval is automatic; terminal sandboxing is requested by default.

Authorized event callbacks receive only bounded decision or terminal summaries.
Callbacks must use approved public HTTPS hosts, are verified and signed, and stop
on revocation or expiry. Provider sign-in, GitHub, Microsoft and Cloudflare can process data
internationally. This software does not select an EU-only hosting location or
establish the providers' contractual transfer safeguards on your behalf.

## Retention and deletion

Product enrollment delivery records expire after 72 hours. The encrypted user
identifier is cleared when no enrollment update remains pending. Entra retains the
registration source with the directory account; product-group memberships remain
until removed or the account is deleted. Erasing relay records does not delete
the Entra account, its labels or the separate enrollment delivery queue. Use the
support route below for a directory-label correction or removal request. A later
authenticated sign-in can enroll the account in that product again.

Queued task briefs and correction content are removed from relay dispatch records
when the worker acknowledges acceptance. Remaining task metadata, compact events,
results and transferred artifacts expire after 72 hours from launch, including
unfinished tasks. Expiration removes their dispatch records as well. Minimal
request-ID recovery tombstones, containing identifiers and request fingerprints,
remain for a further 30 days to prevent an expired retry from starting work again.
New computer management approval can remain until revoked; task execution still
requires separately approved workspace permissions. Device access credentials
last 24 hours and refresh proofs rotate while approval remains valid. The relay
stores hashes of device proofs, not their plaintext values. A previous proof can
recover only its identical rotation for ten minutes. Approval, workspace and
readiness records remain while the computer is approved. Existing 30-day grants
retain their expiry until you explicitly convert them. Expired identity records, authentication
hashes and approved workspace metadata remain for up to 72 further hours to
support explicit renewal by the same owner and computer. Expiry blocks normal
worker access during that window. Pending renewal approvals expire after ten
minutes. After approval, a protected claim record remains for up to 72 hours so
the same computer can recover an interrupted credential rotation. Revocation
immediately invalidates the pairing and clears its
authentication hash, aliases and cached readiness. A revoked identity marker may
remain while associated task records are retained to prevent unauthorized recovery.

Management confirmations expire after ten minutes. Confirmed operations remain
pending while waiting for a computer; settled receipts are retained for 30 days.
Management-message deduplication records expire after one day. Narrow uninstall
completion permissions expire after seven days and cannot authorize other actions.
Device credentials are removed on revocation. Account erasure also removes the
new management, settings and credential-rotation records.

ChatGPT access tokens last up to one hour. New refresh authorizations and client
registrations have no scheduled expiry; disconnect or revoke authorizations to
remove access. Existing authorizations may require reconnection to adopt this
policy. Subscription signing secrets and authorization tokens
are encrypted in relay storage, retained only during their authorized lifetime,
and cleared when revoked, expired or stopped. A callback subscription lasts no
longer than the access authorization used to create it.

Use ChatGPT's Computers and Settings card, or the relay account recovery page,
to export retained records, revoke a computer or erase relay records. Erasure invalidates worker access and
removes recovery identifiers. Review or cancel running tasks first: erasing relay
records does not prove provider termination, undo file changes, erase local
worker evidence or remove data held by your providers. Cloudflare's own platform
records and operational copies remain subject to its terms and applicable law.

Your worker's private journal, task prompts, raw evidence and worktrees have no
automatic local expiry. They remain until you or your host remove them. Keep
private state outside delegated workspaces, stop active processes before removal,
and review provider history, CLI caches and backups separately. Do not put auth
or pairing state in Git, shared images or published cloud environment snapshots.

## Support and your choices

The headless companion stores setup preferences, pending pairing, local command
receipts and a protected status heartbeat on your computer. It has no GUI
or local web listener. Account checks expose bounded installation/sign-in status
to your ChatGPT card through the relay. Provider credentials and raw output do not enter the relay.
Installing a missing CLI contacts its official release service and verifies the
published checksum. Providers receive ordinary download connection information
under their own policies. The publisher receives no companion browsing telemetry
or installation logs. A task requested before setup is retained under the same
task retention period; workspace approval queues that original task once.

The npx setup command downloads its pinned bootstrap package and the signed Windows
installer from GitHub Releases. Your installed npm/npx tool can also contact npm's
infrastructure under its own configuration. Automatic companion updates contact the configured
public release feed and verify its publisher signature and package hash locally.
npm, GitHub and their download infrastructure receive ordinary connection
information when contacted, under their policies. No provider credentials accompany these
downloads. Update/recovery state stays on your computer; only bounded version and
update status is reported through your account's card. Uninstall removes owned
application integrations and device credentials, while preserving project files,
provider installations, provider sign-ins and local task history by default.

Support and privacy questions use [Rami Radwan's LinkedIn profile](https://astra-antigravity-relay.dipsys.workers.dev/support).
The support link redirects to LinkedIn without adding account identifiers, task
content or URL parameters. LinkedIn receives ordinary connection information when
you visit and handles profile and message data under its own
[privacy policy](https://www.linkedin.com/legal/privacy-policy).
Start with a sanitized question; never send identity documents, credentials,
private task content or raw logs. Support correspondence stays in LinkedIn,
with no separate publisher exports, and is retained there until removed.
Use LinkedIn's controls or ask us to remove our copy of correspondence.
LinkedIn's storage, backups and any copies held by other recipients follow its rules.

Where applicable, you can request access, correction, erasure, restriction,
portability or object to processing, and complain to the relevant data-protection
authority. Authenticated relay controls provide access, revocation and erasure
for your relay records. For provider-held information, use that provider's controls.
Avoid sending sensitive material through LinkedIn when raising a question.

Changes to hosting, data flows, providers, logging or retention require an updated
notice before the changed release is enabled.
