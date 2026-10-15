import type { UiControlSnapshot } from "../src/ui-contracts";

/** Synthetic data only. No live computers, sessions, provider calls or credentials. */
const base: UiControlSnapshot = {
  version: 1, revision: "control-1", updated_at: "2026-10-15T12:30:00Z",
  permissions: { can_manage: true, can_delete_account: true, can_write_tasks: true },
  computers: [
    { id: "desktop", label: "Office computer", state: "online", paused: false, setup_only: false, approval_mode: "until_revoked", companion_version: "0.4.0",
      capabilities: ["computer_control_v1"], allowed_actions: ["pause", "stop_tasks", "rename", "set_startup", "set_automatic_updates", "uninstall", "revoke"],
      active_task_count: 1, pending_task_count: 2, workspaces: [{ id: "reports", label: "Quarterly reports" }], startup_enabled: true, automatic_updates_enabled: false, update_status: "Up to date" },
    { id: "laptop", label: "Research laptop", state: "offline", paused: false, setup_only: false, approval_mode: "until_revoked", companion_version: "0.4.0",
      capabilities: ["computer_control_v1"], allowed_actions: ["pause", "rename", "set_startup", "set_automatic_updates", "uninstall", "revoke"],
      active_task_count: 0, pending_task_count: 1, workspaces: [{ id: "research", label: "Research notes" }], automatic_updates_enabled: true, update_status: "Update pending" },
    { id: "previous", label: "Previous computer", state: "expired", paused: false, setup_only: false, approval_mode: "legacy_30_day", companion_version: "0.3.1", expires_at: "2026-10-14T12:30:00Z",
      capabilities: [], allowed_actions: ["rename", "revoke"], active_task_count: 0, pending_task_count: 0, workspaces: [{ id: "archive", label: "Archive" }] },
  ],
  tasks: [{ delegation_id: "gallery-report", task_summary: "Create the quarterly report", status: "running", terminal: false, workspace_label: "Quarterly reports", computer_id: "desktop", updated_at: "2026-10-15T12:30:00Z" },
    { delegation_id: "saved-report", task_summary: "Review the saved report", status: "completed", terminal: true, workspace_label: "Research notes", computer_id: "laptop", updated_at: "2026-10-14T12:30:00Z" }],
  settings: { preferred_workspace_id: "reports", setup_url: "https://relay.example.test/connect", installer_url: "https://relay.example.test/companion/download", install_unavailable_message: "A verified public setup release is not available. No setup command is enabled." },
  receipts: [{ operation_id: "offline-pause", computer_id: "laptop", action: "pause", state: "pending", summary: "Pause requested. Waiting for Research laptop to reconnect and confirm.", updated_at: "2026-10-15T12:30:00Z" }],
  account: { export_available: true, deletion_requires_confirmation: true },
};
export const controlFixtures: Record<string, UiControlSnapshot> = {
  computers: base,
  "view-only": { ...base, permissions: { can_manage: false, can_delete_account: false, can_write_tasks: false } },
  empty: { ...base, computers: [], tasks: [], receipts: [] },
  "setup-command": { ...base, settings: { ...base.settings, setup_command: "npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.1/delegate-to-antigravity-0.4.1.tgz setup", install_unavailable_message: undefined } },
  "cancelled-before-setup": { ...base, computers: [], tasks: [{ delegation_id: "cancelled-before-setup", task_summary: "Cancelled saved task",
    status: "cancelled", terminal: true, computer_id: "", updated_at: "2026-10-15T12:30:00Z" }], receipts: [] },
  paused: { ...base, computers: [{ ...base.computers[0], state: "paused", paused: true, allowed_actions: ["resume", "rename", "revoke"] }] },
  onboarding: { ...base, computers: [{ ...base.computers[0], label: "New Windows computer", setup_only: true, workspaces: [], active_task_count: 0, pending_task_count: 0,
    capabilities: ["computer_control_v1", "setup_control_v1"], allowed_actions: ["check_accounts", "install_provider", "sign_in", "configure_workspace", "rename", "revoke"],
    accounts: { codex: { installed: false, status: "missing" }, agy: { installed: true, status: "sign_in_required" }, git: { installed: true, status: "ready" } } }],
    tasks: [], receipts: [] },
  "removal-unconfirmed": { ...base, receipts: [{ operation_id: "expired-removal", computer_id: "laptop", action: "uninstall", state: "unconfirmed",
    summary: "Removal confirmation expired. The companion may have been removed locally; its completion was not confirmed. Check that computer; revocation is separate.", updated_at: "2026-10-15T12:30:00Z" }] },
};
