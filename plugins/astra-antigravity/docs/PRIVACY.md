# Delegate to Antigravity privacy notice

Release scope: 0.9.2, reviewed 14 October 2026.

Rami Radwan maintains this independent, open-source plugin. This notice describes
the local CLI edition. It does not describe a hosted relay service: this release
has no publisher-operated task server or telemetry endpoint.
The plugin is intended for worldwide distribution. Availability of provider
accounts, subscriptions and models remains subject to each provider's terms.

## Content used for delegation

The plugin passes your task, workspace path, task type and selected controls to
local processes. It sends the task and bounded planning or correction context to
Codex for a lightweight planning turn. It then sends the authorized task and plan
to your Antigravity CLI. That agent can inspect workspace content, execute shell
commands and create or change files. Outputs can contain supplied personal data,
source content, paths, command output and model-generated text.

Codex, Antigravity and the selected model provider handle requests through your
authenticated CLI accounts. Their terms, settings and privacy policies govern
that processing. The plugin does not establish their retention periods, training
use, processing locations or account protections. Your task may also instruct
the agent or a terminal command to contact other services. The plugin does not
inherit the parent session's browser, live web tools or connector credentials.

Only delegate content you are authorized to share. Worktree isolation separates
Git changes; it does not prevent every read of files accessible to the CLI user.
Antigravity terminal sandboxing is requested by default, while Antigravity tool
approval is automatic. Planning is read-only and network-disabled at the tool
policy level; inference itself still contacts the Codex provider.

## Local history and credentials

The plugin saves task prompts and results, raw execution logs, conversation and
task identifiers, model choices, paths, quota snapshots, worktree metadata and
compact delivery events locally. The default directory is
`astra-antigravity-tasks` under your operating system's temporary directory.
`ASTRA_ANTIGRAVITY_STATE_DIR` can select another directory. Worktrees normally live
under that state directory; `ASTRA_ANTIGRAVITY_WORKTREE_ROOT` can change their root.

These records support recovery, verification and delivery replay. They are not
encrypted by the plugin and have no automatic expiry. They remain until you or
your operating system remove them. Canceling a task stops execution; it does not
erase history or undo file changes. Worktree cleanup removes the selected
worktree according to its controls, not all associated history. For deletion,
stop active tasks and the plugin, review the selected state directory and remove
the records you own; also review worktrees, CLI history, backups and provider
account controls separately. Uninstalling the plugin does not remove those records.

Authentication remains with your installed CLIs. The plugin does not provide a
login service or package your credentials. Its child processes inherit the
local process environment for CLI configuration and authentication. The launcher
does not read project `.env` files. The source package uses uv, which can download
Python dependencies and keep them in its own cache. The bundled-runtime package
loads its included SDK dependencies without a startup download. Installing Python
and the provider CLIs separately can still contact their distributors.

## Maintainer and support

The plugin does not send task history or local files to the maintainer. If you
choose to report an issue, GitHub processes the account information and content
you submit, and repository maintainers can read it. Public issue contents can be
read by others. Share a sanitized reproduction rather than task logs, tokens or
private source data. This notice does not set GitHub's storage or deletion rules.
Support is handled in GitHub; the maintainer does not export issue records into
a separate support system. Issues remain as troubleshooting history until removed.
Closing an issue does not delete it. You can edit your own contributions or ask
the repository owner to remove an issue. GitHub's own storage, notifications,
backups and account controls remain governed by its policies.

Support and privacy questions can be directed through
[GitHub issues](https://github.com/ramiradwan/astra-antigravity/issues).
This is a public contact channel. For a question about information already in an
issue, link to it without repeating the information. Do not submit credentials,
identity documents, confidential task content or raw logs. For provider-held data,
use the relevant provider's controls.

Changes to task hosting, telemetry, providers, credential handling or retention
will require an updated notice before that release is published.
