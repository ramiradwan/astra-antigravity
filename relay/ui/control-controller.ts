import type { UiControlSnapshot, UiOperation } from "../src/ui-contracts";
import { validSetupCommand } from "../src/companion-setup";
/** All control data stays in the component. Never send snapshots to model context. */
export type ControlSnapshot = UiControlSnapshot;
export type ControlComputer = ControlSnapshot["computers"][number];
export type ControlOperation = UiOperation;
export const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));
const id = (value: unknown): value is string => typeof value === "string" && /^[a-zA-Z0-9_.-]{1,160}$/.test(value);
const text = (value: unknown): value is string => typeof value === "string";
const optionalText = (value: unknown) => value === undefined || text(value);
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.length <= 100 && value.every(text);
const count = (value: unknown) => typeof value === "number" && Number.isInteger(value) && value >= 0;
const boolean = (value: unknown) => typeof value === "boolean";

export function operationFrom(result: unknown): ControlOperation | undefined {
  if (!record(result) || !record(result._meta)) return;
  return validOperation(result._meta.astra_operation) ? result._meta.astra_operation : undefined;
}
function validOperation(value: unknown): value is ControlOperation {
  return record(value) && id(value.operation_id) && text(value.action) && optionalText(value.computer_id) &&
    ["prepared", "pending", "completed", "failed", "revoked", "unconfirmed"].includes(String(value.state)) && text(value.summary) &&
    text(value.updated_at) && optionalText(value.confirmation_token) && optionalText(value.expires_at);
}
export function controlFrom(result: unknown): ControlSnapshot | undefined {
  if (!record(result) || !record(result._meta)) return;
  const value = result._meta.astra_control;
  if (!record(value) || value.version !== 1 || !text(value.revision) || !text(value.updated_at) ||
    !record(value.permissions) || !boolean(value.permissions.can_manage) || !boolean(value.permissions.can_delete_account) || !boolean(value.permissions.can_write_tasks) ||
    !record(value.settings) || !text(value.settings.setup_url) || !["preferred_workspace_id", "setup_command", "installer_url", "install_unavailable_message"].every(key => optionalText(value.settings && (value.settings as Record<string, unknown>)[key])) ||
    (value.settings.setup_command !== undefined && !validSetupCommand(value.settings.setup_command)) ||
    !record(value.account) || !boolean(value.account.export_available) || !boolean(value.account.deletion_requires_confirmation) ||
    !Array.isArray(value.computers) || value.computers.length > 200 || !value.computers.every(computer => record(computer) &&
      id(computer.id) && text(computer.label) && ["online", "offline", "paused", "expired", "revoked"].includes(String(computer.state)) && boolean(computer.paused) && boolean(computer.setup_only) &&
      ["until_revoked", "legacy_30_day"].includes(String(computer.approval_mode)) && optionalText(computer.expires_at) && optionalText(computer.companion_version) &&
      (computer.startup_enabled === undefined || boolean(computer.startup_enabled)) && (computer.automatic_updates_enabled === undefined || boolean(computer.automatic_updates_enabled)) && optionalText(computer.update_status) && optionalText(computer.uninstall_status) &&
      strings(computer.capabilities) && strings(computer.allowed_actions) && count(computer.active_task_count) && count(computer.pending_task_count) &&
      (computer.accounts === undefined || (record(computer.accounts) && Object.entries(computer.accounts).every(([provider, account]) => ["codex", "agy", "git"].includes(provider) && record(account) && boolean(account.installed) && ["ready", "sign_in_required", "missing", "unknown"].includes(String(account.status)) && optionalText(account.summary)))) &&
      Array.isArray(computer.workspaces) && computer.workspaces.length <= 100 && computer.workspaces.every(workspace => record(workspace) && id(workspace.id) && text(workspace.label))) ||
    !Array.isArray(value.tasks) || value.tasks.length > 200 || !value.tasks.every(task => record(task) && id(task.delegation_id) && text(task.task_summary) && text(task.status) &&
      boolean(task.terminal) && optionalText(task.workspace_label) && (id(task.computer_id) || (task.computer_id === "" && (task.status === "awaiting_setup" || task.terminal === true))) && text(task.updated_at)) ||
    !Array.isArray(value.receipts) || value.receipts.length > 200 || !value.receipts.every(validOperation)) return;
  return value as unknown as ControlSnapshot;
}
export const operationState = (state: ControlOperation["state"]): string => ({ prepared: "Awaiting your confirmation", pending: "Pending on computer", completed: "Completed", failed: "Failed", revoked: "Revoked", unconfirmed: "Completion unconfirmed" })[state];
export const computerState = (state: ControlComputer["state"]): string => ({ online: "Online", offline: "Offline", paused: "Paused", expired: "Access expired", revoked: "Access revoked" })[state];
export function mayOperate(snapshot: ControlSnapshot, computer: ControlComputer, action: string): boolean {
  return snapshot.permissions.can_manage && computer.allowed_actions.includes(action);
}
export function mayConfirm(operation: ControlOperation, now = Date.now()): boolean {
  return operation.state === "prepared" && typeof operation.confirmation_token === "string" && /^[a-f0-9]{64}$/.test(operation.confirmation_token) && typeof operation.expires_at === "string" && Date.parse(operation.expires_at) > now;
}
export function downloadGrantFrom(result: unknown, artifact: { name: string; bytes?: number; sha256?: string; download_origin?: string; download_url?: string }, now = Date.now()): { url: string; name: string; mime_type: string; bytes: number; sha256: string; origin: string } | undefined {
  if (!record(result) || !record(result._meta) || !record(result._meta.astra_download)) return;
  const value = result._meta.astra_download;
  if (!text(value.url) || !text(value.name) || !text(value.mime_type) || !count(value.bytes) || Number(value.bytes) > 20 * 1024 * 1024 ||
      !text(value.sha256) || !/^[a-f0-9]{64}$/.test(value.sha256) || !text(value.expires_at) || Date.parse(value.expires_at) <= now || !Number.isFinite(Date.parse(value.expires_at)) ||
      (artifact.bytes !== undefined && value.bytes !== artifact.bytes) || (artifact.sha256 && value.sha256 !== artifact.sha256)) return;
  try {
    if (!text(artifact.download_origin)) return;
    const origin = new URL(artifact.download_origin), url = new URL(value.url);
    // Browser download URLs from older snapshots confer no transfer authority.
    if (origin.protocol !== "https:" || origin.username || origin.password || origin.origin !== artifact.download_origin ||
        url.origin !== origin.origin || url.username || url.password || url.search || url.hash || !/^\/artifact-download\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$/.test(url.pathname)) return;
    return { url: url.href, name: value.name, mime_type: value.mime_type, bytes: Number(value.bytes), sha256: value.sha256, origin: origin.origin };
  } catch { return; }
}
