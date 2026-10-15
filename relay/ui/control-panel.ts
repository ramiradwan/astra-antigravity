import { controlFrom, computerState, mayConfirm, mayOperate, operationFrom, operationState, record, type ControlComputer, type ControlOperation, type ControlSnapshot } from "./control-controller";
import { formatUpdatedAt } from "./controller";
import { computerSetup } from "./onboarding";
import type { NativeFileMode, NativeFileRequest, NativeFileTransferResult } from "./native-files";
import { setupCommandForRequest } from "../src/companion-setup";

export type PanelView = "tasks" | "computers" | "settings";
interface Bridge {
  callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
  openTask(id: string, current: () => boolean): Promise<void>;
  transferFile(params: NativeFileRequest): Promise<NativeFileTransferResult>;
  fileMode(): NativeFileMode;
  context(): { locale?: string; timeZone?: string };
  modeChanged(active: boolean): void;
}
const node = <K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] => {
  const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element;
};
const actionTitles: Record<string, string> = {
  pause: "Pause new tasks", resume: "Resume new tasks", stop_tasks: "Stop current tasks", rename: "Rename computer",
  set_startup: "Change startup", set_automatic_updates: "Change automatic updates", uninstall: "Uninstall companion",
  revoke: "Revoke access", keep_approved: "Keep computer approved", delete_account: "Delete account records", set_preferred_workspace: "Set preferred workspace",
  check_accounts: "Check accounts", install_provider: "Install provider tool", sign_in: "Sign in to provider", configure_workspace: "Approve workspace", approve_pairing: "Approve computer",
};

export class ControlPanel {
  active = false;
  private snapshot?: ControlSnapshot;
  private view: PanelView = "computers";
  private selectedComputer?: string;
  private prepared?: ControlOperation;
  private latestReceipt?: ControlOperation;
  private uncertainOperations = new Set<string>();
  private drafts = new Map<string, string>();
  private pendingRequestId?: string;
  private pairAcknowledged = false;
  private workspaceAcknowledged = new Set<string>();
  private disclosureState = new Map<string, boolean>();
  private busy = "";
  private notice = "";
  private tone = "info";
  private lastAnnounced = "";
  private deleteAcknowledged = false;
  private accountDeleted = false;
  private visible = true;
  private disposed = false;
  private reading = false;
  private generation = 0;
  private invocation = 0;
  private timer?: ReturnType<typeof setTimeout>;

  constructor(private root: HTMLElement, private announcement: HTMLElement, private bridge: Bridge) {}
  accept(result: unknown): boolean {
    const value = controlFrom(result); if (!value) return false;
    this.invocation++; this.busy = "";
    // An external tool result starts a fresh invocation, possibly for another
    // connected account. Never carry a confirmation or private form into it.
    this.prepared = undefined; this.latestReceipt = undefined; this.uncertainOperations.clear();
    this.drafts.clear(); this.disclosureState.clear(); this.workspaceAcknowledged.clear();
    this.pairAcknowledged = false; this.deleteAcknowledged = false; this.accountDeleted = false;
    this.notice = ""; this.tone = "info"; this.selectedComputer = undefined;
    this.pendingRequestId = undefined;
    this.generation++; this.snapshot = value; this.setActive(true); this.render(); this.plan(); return true;
  }
  setVisible(visible: boolean): void { this.visible = visible; this.plan(); }
  dispose(): void { this.disposed = true; this.generation++; this.clear(); }
  private clear(): void { if (this.timer !== undefined) clearTimeout(this.timer); this.timer = undefined; }
  private setActive(active: boolean): void { this.active = active; this.bridge.modeChanged(active); if (!active) this.clear(); }
  async open(view: PanelView = "computers", computerId?: string, requestId?: string): Promise<void> {
    this.view = view; this.selectedComputer = computerId;
    this.pendingRequestId = requestId && /^[a-zA-Z0-9_.-]{1,128}$/.test(requestId) ? requestId : undefined;
    this.setActive(true);
    await this.read();
  }
  hide(): void { this.setActive(false); }
  private plan(): void {
    this.clear();
    if (this.disposed || this.accountDeleted || !this.active || !this.visible || this.reading || this.busy || this.prepared || !this.snapshot) return;
    this.timer = setTimeout(() => { void this.read(true); }, 20_000);
  }
  private async read(wait = false): Promise<void> {
    if (this.reading || this.disposed || this.accountDeleted) return;
    this.reading = true; this.clear(); const generation = this.generation;
    try {
      const response = await this.bridge.callTool("watch_computers_ui", { ...(this.snapshot ? { after_revision: this.snapshot.revision } : {}), wait_ms: wait ? 20_000 : 0 });
      if (generation !== this.generation || this.disposed || !this.active) return;
      const value = controlFrom(response); if (!value) throw new Error("Computer details could not be loaded. No action was repeated.");
      this.snapshot = value;
      if (this.notice.startsWith("Computer details could not")) this.notice = "";
    } catch { if (generation === this.generation && !this.disposed) { this.notice = "Computer details could not be loaded. Use Refresh to check again. No action was repeated."; this.tone = "warning"; } }
    finally { this.reading = false; if (this.active) this.render(); this.plan(); }
  }
  private control(label: string, key: string, callback: () => void, options: { primary?: boolean; disabled?: boolean; danger?: boolean } = {}): HTMLButtonElement {
    const pendingLabel = key.startsWith("prepare:") ? "Preparing change…" : key === "confirm-operation" ? "Sending change…" : key.startsWith("inspect:") ? "Checking…" : key === "export-account" ? "Preparing export…" : "Opening…";
    const button = node("button", this.busy === key ? pendingLabel : label, `action${options.primary ? " primary" : ""}${options.danger ? " danger" : ""}`);
    button.type = "button"; button.dataset.focusKey = key; button.disabled = Boolean(options.disabled);
    if (this.busy) button.setAttribute("aria-disabled", "true");
    button.addEventListener("click", () => { if (!button.disabled && !this.busy) callback(); }); return button;
  }
  private async run(key: string, action: (current: () => boolean) => Promise<void>): Promise<void> {
    const pending = key === "approve-pairing" ? "Approving computer…" : key.startsWith("prepare:") ? `Preparing ${actionTitles[key.split(":").pop()!] || "change"}…` : key === "confirm-operation" ? "Sending the confirmed change…" : key.startsWith("inspect:") ? "Checking operation status…" : key === "export-account" ? "Preparing account export…" : "Opening…";
    if (this.busy) return; const invocation = this.invocation; const current = () => invocation === this.invocation && !this.disposed;
    this.busy = key; this.notice = pending; this.tone = "info"; this.clear(); this.render();
    try { await action(current); }
    catch (error) { if (current()) { this.notice = error instanceof Error ? error.message : "The action could not be completed. No action was repeated."; this.tone = "danger"; } }
    finally { if (current()) { if (this.notice === pending) this.notice = ""; this.busy = ""; this.render(); this.plan(); } }
  }
  private async prepare(action: string, computerId?: string, parameters?: Record<string, unknown>): Promise<void> {
    const invocation = this.invocation;
    await this.run(`prepare:${computerId || "account"}:${action}`, async current => {
      const result = await this.bridge.callTool("prepare_operation", { request_id: crypto.randomUUID(), action, ...(computerId ? { computer_id: computerId } : {}), ...(parameters ? { parameters } : {}) });
      if (!current()) return;
      const operation = operationFrom(result);
      if (!operation || operation.action !== action || operation.computer_id !== computerId || !mayConfirm(operation)) throw new Error("The change could not be prepared. Refresh before trying again.");
      this.prepared = operation;
    });
    if (invocation === this.invocation) this.root.querySelector<HTMLElement>(".panel-confirm h2")?.focus({ preventScroll: true });
  }
  private async confirm(): Promise<void> {
    const prepared = this.prepared; if (!prepared || !mayConfirm(prepared)) { this.prepared = undefined; this.notice = "This confirmation expired. Prepare the change again."; this.render(); return; }
    // Clear the token before sending. A lost response never causes an automatic retry.
    this.prepared = undefined;
    await this.run("confirm-operation", async current => {
      try {
        const result = await this.bridge.callTool("confirm_operation", { operation_id: prepared.operation_id, confirmation_token: prepared.confirmation_token });
        if (!current()) return;
        const receipt = operationFrom(result);
        if (!receipt || receipt.operation_id !== prepared.operation_id) throw new Error("The response could not be confirmed.");
        this.latestReceipt = receipt; this.notice = receipt.summary; this.tone = receipt.state === "failed" ? "danger" : receipt.state === "pending" ? "warning" : receipt.state === "completed" ? "success" : "info";
        if (receipt.action === "delete_account" && receipt.state === "completed" && this.snapshot) {
          this.accountDeleted = true; this.generation++; this.drafts.clear(); this.deleteAcknowledged = false;
          this.snapshot = { ...this.snapshot, computers: [], tasks: [], receipts: [], permissions: { can_manage: false, can_delete_account: false, can_write_tasks: false }, settings: { setup_url: this.snapshot.settings.setup_url }, account: { export_available: false, deletion_requires_confirmation: true } };
        }
      } catch { if (!current()) return; this.uncertainOperations.add(prepared.operation_id); this.latestReceipt = { ...prepared, confirmation_token: undefined, summary: "The outcome is not confirmed. Check this operation before requesting another change." }; throw new Error("The outcome is not confirmed. Check this operation; the change has not been sent again."); }
      await this.read();
    });
  }
  private async inspect(operation: ControlOperation): Promise<void> {
    await this.run(`inspect:${operation.operation_id}`, async current => {
      const response = await this.bridge.callTool("inspect_operation", { operation_id: operation.operation_id });
      if (!current()) return;
      const receipt = operationFrom(response); if (!receipt || receipt.operation_id !== operation.operation_id) throw new Error("Operation status could not be loaded. No action was repeated.");
      this.uncertainOperations.delete(receipt.operation_id); this.latestReceipt = receipt; this.notice = receipt.summary; this.tone = receipt.state === "failed" ? "danger" : "info"; await this.read();
    });
  }
  private field(label: string, input: HTMLInputElement | HTMLSelectElement, key: string): HTMLElement {
    const field = node("div", undefined, "panel-field"); input.id = key; input.dataset.focusKey = key;
    if (this.drafts.has(key)) input.value = this.drafts.get(key)!;
    input.addEventListener("input", () => this.drafts.set(key, input.value));
    const caption = node("label", label); caption.htmlFor = key; field.append(caption, input); return field;
  }
  private installation(snapshot: ControlSnapshot): HTMLElement {
    const section = node("section", undefined, "panel-section"); section.append(node("h2", "Connect a computer"));
    const command = setupCommandForRequest(snapshot.settings.setup_command, this.pendingRequestId);
    if (command) {
      section.append(node("p", "Run setup on the Windows computer that will do the work. Paste its pairing code here, then connect your accounts and approve a workspace in this panel. Provider sign-in opens the provider’s browser page on that computer.", "panel-muted"));
      section.append(node("p", "Run this command on that computer:", "panel-hint"), node("code", command, "panel-code"));
    }
    if (snapshot.settings.install_unavailable_message) section.append(node("p", snapshot.settings.install_unavailable_message, "panel-hint panel-muted"));
    const form = node("form");
    const code = node("input"); code.type = "text"; code.autocomplete = "one-time-code"; code.spellcheck = false; code.maxLength = 24; code.placeholder = "24-character code from setup";
    form.append(this.field("Pairing code", code, "pairing-code"));
    const acknowledge = node("label", undefined, "panel-checkbox"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = this.pairAcknowledged; checkbox.disabled = !snapshot.permissions.can_manage; checkbox.dataset.focusKey = "pairing-approval";
    checkbox.addEventListener("change", () => { this.pairAcknowledged = checkbox.checked; this.render(); });
    acknowledge.append(checkbox, node("span", "Approve this computer for account setup and management until I revoke access. File and command access requires a separate workspace approval."));
    form.append(acknowledge, this.control("Approve computer", "approve-pairing", () => { void this.approvePairing(code.value); }, { primary: true, disabled: !snapshot.permissions.can_manage || !this.pairAcknowledged }));
    form.addEventListener("submit", event => { event.preventDefault(); if (snapshot.permissions.can_manage && this.pairAcknowledged) void this.approvePairing(code.value); });
    section.append(form);
    return section;
  }
  private async approvePairing(raw: string): Promise<void> {
    const code = raw.trim().toLowerCase();
    if (!/^[a-f0-9]{24}$/.test(code)) { this.notice = "Paste the full 24-character pairing code printed by setup."; this.tone = "warning"; this.render(); this.root.querySelector<HTMLElement>("#pairing-code")?.focus(); return; }
    await this.run("approve-pairing", async current => {
      const result = await this.bridge.callTool("approve_pairing_ui", { code, approval_mode: "until_revoked", ...(this.pendingRequestId ? { request_id: this.pendingRequestId } : {}) });
      if (!current()) return;
      const receipt = operationFrom(result);
      if (!receipt || receipt.state !== "completed") throw new Error("Computer approval could not be confirmed. Refresh before using the pairing code again.");
      this.latestReceipt = receipt; this.notice = receipt.summary; this.tone = "success"; this.pairAcknowledged = false; this.drafts.delete("pairing-code"); this.selectedComputer = receipt.computer_id; this.view = "computers"; await this.read();
    });
  }
  private computerCard(snapshot: ControlSnapshot, computer: ControlComputer): HTMLElement {
    const item = node("li", undefined, "panel-item"); item.dataset.computerId = computer.id;
    const heading = node("h2", computer.label); item.append(heading);
    const status = node("div", undefined, "status-line"); status.append(node("span", computerState(computer.state), "task-status"), node("span", `${computer.active_task_count} active · ${computer.pending_task_count} pending`, "status-note")); item.append(status);
    if (computer.companion_version) item.append(node("p", `Companion ${computer.companion_version}${computer.update_status ? ` · ${computer.update_status}` : ""}`, "panel-hint panel-muted"));
    item.append(node("p", computer.approval_mode === "until_revoked" ? "Approved until you revoke access." : `Legacy approval${computer.expires_at ? ` expires ${formatUpdatedAt(computer.expires_at, this.bridge.context())?.full || computer.expires_at}` : " lasts 30 days"}. Keeping this computer approved requires a separate confirmation.`, "panel-hint panel-muted"));
    if (computer.paused) item.append(node("p", "New tasks are paused. Work already running may continue.", "panel-hint"));
    if (computer.state === "offline") item.append(node("p", "This computer is offline. Machine changes stay pending until it reconnects.", "panel-hint"));
    const workspaces = node("ul", undefined, "panel-workspaces"); for (const workspace of computer.workspaces) workspaces.append(node("li", workspace.label + (snapshot.settings.preferred_workspace_id === workspace.id ? " · Preferred" : ""))); if (workspaces.childElementCount) item.append(workspaces);
    item.append(computerSetup(snapshot, computer, {
      field: (label, input, key) => this.field(label, input, key),
      button: (label, key, callback, disabled) => this.control(label, key, callback, { disabled }),
      prepare: (action, computerId, parameters) => { void this.prepare(action, computerId, parameters); },
      acknowledged: this.workspaceAcknowledged.has(computer.id),
      acknowledge: checked => { if (checked) this.workspaceAcknowledged.add(computer.id); else this.workspaceAcknowledged.delete(computer.id); this.render(); },
      invalid: message => { this.notice = message; this.tone = "warning"; this.render(); },
    }));
    const actions = node("div", undefined, "actions");
    for (const action of [computer.paused ? "resume" : "pause", "stop_tasks"]) if (mayOperate(snapshot, computer, action)) actions.append(this.control(actionTitles[action], `prepare:${computer.id}:${action}`, () => { void this.prepare(action, computer.id); }));
    if (actions.childElementCount) item.append(actions);
    const details = node("details", undefined, "disclosure"); const summary = node("summary", "Computer settings"); summary.dataset.focusKey = `settings:${computer.id}`; details.append(summary);
    const form = node("form"); const name = node("input"); name.value = computer.label; name.maxLength = 80; name.autocomplete = "off"; name.disabled = !mayOperate(snapshot, computer, "rename");
    form.append(this.field("Computer name", name, `label:${computer.id}`), this.control("Review name change", `prepare:${computer.id}:rename`, () => { if (name.value.trim()) void this.prepare("rename", computer.id, { label: name.value.trim() }); }, { disabled: !mayOperate(snapshot, computer, "rename") }));
    form.addEventListener("submit", event => { event.preventDefault(); if (!this.busy && name.value.trim() && mayOperate(snapshot, computer, "rename")) void this.prepare("rename", computer.id, { label: name.value.trim() }); }); details.append(form);
    const advanced = node("div", undefined, "actions");
    for (const [action, label, parameters] of [
      ["set_startup", computer.startup_enabled ? "Turn startup off" : "Start at sign-in", { enabled: !computer.startup_enabled }],
      ["set_automatic_updates", computer.automatic_updates_enabled ? "Turn automatic updates off" : "Turn automatic updates on", { enabled: !computer.automatic_updates_enabled }],
      ["keep_approved", "Keep computer approved", undefined], ["uninstall", "Uninstall companion", { erase_history: false }], ["revoke", "Revoke access", undefined],
    ] as const) if (mayOperate(snapshot, computer, action)) advanced.append(this.control(label, `prepare:${computer.id}:${action}`, () => { void this.prepare(action, computer.id, parameters); }, { danger: action === "revoke" || action === "uninstall" }));
    details.append(advanced);
    if (!computer.capabilities.includes("computer_control_v1")) details.append(node("p", "This companion does not support remote machine controls. Update it on the computer to enable them.", "panel-hint panel-muted"));
    if (computer.approval_mode === "legacy_30_day" && !computer.allowed_actions.includes("keep_approved")) details.append(node("p", "Run the updated setup command on this computer to prepare ongoing approval. The current approval stays unchanged until you confirm here.", "panel-hint panel-muted"));
    if (computer.uninstall_status) details.append(node("p", `Uninstall: ${computer.uninstall_status}`, "panel-hint"));
    item.append(details); return item;
  }
  private tasks(snapshot: ControlSnapshot): HTMLElement {
    const section = node("section", undefined, "panel-section"); section.append(node("h2", "Tasks"));
    if (!snapshot.tasks.length) { section.append(node("p", "No saved tasks. Describe a task in chat to delegate work to an approved workspace.", "panel-empty panel-muted")); return section; }
    const list = node("ul", undefined, "panel-list");
    for (const task of snapshot.tasks) {
      const item = node("li", undefined, "panel-item"); item.append(node("h3", task.task_summary || "Antigravity task"));
      const computer = snapshot.computers.find(item => item.id === task.computer_id);
      item.append(node("p", `${task.status === "awaiting_setup" ? "Awaiting connection" : task.status.replace(/_/g, " ")}${task.status === "completed" ? " · Review pending" : ""}`, "panel-hint"), node("p", [computer?.label, task.workspace_label].filter(Boolean).join(" · "), "panel-hint panel-muted"));
      const actions = node("div", undefined, "actions"); actions.append(this.control("Open task", `task:${task.delegation_id}`, () => { void this.run(`task:${task.delegation_id}`, async current => { await this.bridge.openTask(task.delegation_id, current); if (current()) this.hide(); }); })); item.append(actions); list.append(item);
    }
    section.append(list); return section;
  }
  private settings(snapshot: ControlSnapshot): HTMLElement {
    const section = node("section", undefined, "panel-section"); section.append(node("h2", "Settings"));
    const select = node("select"); select.append(new Option("Choose a workspace", ""));
    for (const computer of snapshot.computers) if (!["revoked", "expired"].includes(computer.state)) for (const workspace of computer.workspaces) select.append(new Option(`${computer.label} · ${workspace.label}`, workspace.id));
    select.value = snapshot.settings.preferred_workspace_id || ""; select.disabled = !snapshot.permissions.can_manage;
    section.append(this.field("Preferred workspace", select, "preferred-workspace"), node("p", "Used when a task does not name a workspace. You can choose a different workspace in chat.", "panel-hint panel-muted"), this.control("Review preference", "prepare:account:set_preferred_workspace", () => { if (select.value) void this.prepare("set_preferred_workspace", undefined, { workspace_id: select.value }); }, { disabled: !snapshot.permissions.can_manage || select.options.length <= 1 }));
    const account = node("section", undefined, "panel-section"); account.append(node("h2", "Account records"), node("p", "Export or delete records held by this relay. This does not delete files, provider accounts or task changes on your computers.", "panel-muted"));
    const actions = node("div", undefined, "actions"); actions.append(this.control(this.bridge.fileMode() === "chatgpt-upload" ? "Add account records to ChatGPT" : "Export account records", "export-account", () => { void this.exportAccount(); }, { disabled: !snapshot.account.export_available || this.bridge.fileMode() === "unavailable" })); account.append(actions);
    if (this.bridge.fileMode() === "unavailable") account.append(node("p", "File transfer is unavailable in this ChatGPT host. Your account records remain saved.", "panel-hint panel-muted"));
    const acknowledge = node("label", undefined, "panel-checkbox"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = this.deleteAcknowledged; checkbox.dataset.focusKey = "delete-acknowledgement";
    checkbox.addEventListener("change", () => { this.deleteAcknowledged = checkbox.checked; this.render(); });
    checkbox.disabled = !snapshot.permissions.can_manage || !snapshot.permissions.can_delete_account;
    acknowledge.append(checkbox, node("span", "I understand deletion revokes computer access and removes relay records. Local files and provider accounts remain.")); account.append(acknowledge);
    account.append(this.control("Review account deletion", "prepare:account:delete_account", () => { void this.prepare("delete_account"); }, { danger: true, disabled: !snapshot.permissions.can_manage || !snapshot.permissions.can_delete_account || !this.deleteAcknowledged }));
    if (!snapshot.permissions.can_delete_account) account.append(node("p", "Account deletion permission is not enabled for this plugin connection. Reconnect with that permission before deleting records.", "panel-hint panel-muted"));
    section.append(account, this.installation(snapshot)); return section;
  }
  private async exportAccount(): Promise<void> {
    if (this.bridge.fileMode() === "unavailable") { this.notice = "File transfer is unavailable in this ChatGPT host. Your account records remain saved."; this.tone = "warning"; this.render(); return; }
    await this.run("export-account", async current => {
      const result = await this.bridge.callTool("export_account_ui", {});
      if (!current()) return;
      if (!record(result) || !record(result._meta) || !record(result._meta.astra_export)) throw new Error("Account export could not be prepared.");
      const file = result._meta.astra_export;
      if (file.encoding !== "base64" || typeof file.content !== "string" || file.content.length > 28_000_000 || typeof file.name !== "string" || typeof file.mime_type !== "string") throw new Error("Account export could not be verified.");
      try { if (atob(file.content).length > 20 * 1024 * 1024) throw new Error(); } catch { throw new Error("Account export could not be verified."); }
      const reply = await this.bridge.transferFile({ contents: [{ type: "resource", resource: { uri: `file:///${encodeURIComponent(file.name.split(/[\\/]/).pop() || "account-records.json")}`, mimeType: file.mime_type, blob: file.content } }] });
      if (!current()) return;
      this.notice = reply.isError ? "The download was not approved. Your account records have not been deleted." : reply.kind === "chatgpt-upload" ? "File sent to ChatGPT." : "Download requested. Check your downloads."; this.tone = reply.isError ? "warning" : "success";
    });
  }
  render(): void {
    if (!this.active) return;
    const focused = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.focusKey : undefined;
    for (const summary of this.root.querySelectorAll<HTMLElement>("details summary[data-focus-key]")) this.disclosureState.set(summary.dataset.focusKey!, (summary.parentElement as HTMLDetailsElement).open);
    const panel = node("article", undefined, "card control-panel"); panel.setAttribute("aria-busy", String(Boolean(this.busy || this.reading)));
    const header = node("header", undefined, "panel-heading"); const title = node("div"); title.append(node("h1", "Delegate to Antigravity"), node("p", "Tasks and computers", "panel-hint")); header.append(title, this.control(this.reading ? "Checking…" : "Refresh", "panel-refresh", () => { void this.read(); })); panel.append(header);
    const nav = node("nav", undefined, "panel-nav"); nav.setAttribute("aria-label", "Plugin controls");
    for (const view of ["tasks", "computers", "settings"] as const) { const button = this.control(view[0].toUpperCase() + view.slice(1), `view:${view}`, () => { this.view = view; this.prepared = undefined; this.render(); this.plan(); }); if (view === this.view) button.setAttribute("aria-current", "page"); nav.append(button); } panel.append(nav);
    if (this.prepared) {
      const confirm = node("section", undefined, "panel-confirm"); confirm.setAttribute("aria-label", "Confirm change"); const heading = node("h2", actionTitles[this.prepared.action] || "Confirm change"); heading.tabIndex = -1;
      confirm.append(heading, node("p", this.prepared.summary)); const actions = node("div", undefined, "actions");
      actions.append(this.control("Confirm change", "confirm-operation", () => { void this.confirm(); }, { primary: true, disabled: !mayConfirm(this.prepared) }), this.control("Keep current settings", "cancel-operation", () => { this.prepared = undefined; this.notice = "No change was sent."; this.tone = "info"; this.render(); this.plan(); })); confirm.append(actions); panel.append(confirm);
    }
    const snapshot = this.snapshot;
    if (snapshot) {
      if (!snapshot.permissions.can_manage) panel.append(node("p", "View only. Reconnect the plugin with computer management permission to change settings.", "panel-hint panel-muted"));
      if (this.view === "tasks") panel.append(this.tasks(snapshot));
      else if (this.view === "computers") { const section = node("section", undefined, "panel-section"); section.append(node("h2", "Computers")); const list = node("ul", undefined, "panel-list"); for (const computer of snapshot.computers) list.append(this.computerCard(snapshot, computer)); section.append(list); if (!snapshot.computers.length) section.append(node("p", "No computers are connected yet.", "panel-empty panel-muted")); section.append(this.installation(snapshot)); panel.append(section); }
      else panel.append(this.settings(snapshot));
      const receipts = [this.latestReceipt, ...snapshot.receipts].filter((receipt): receipt is ControlOperation => Boolean(receipt)).filter((receipt, index, values) => values.findIndex(item => item.operation_id === receipt.operation_id) === index).slice(0, 10);
      if (receipts.length) {
        const section = node("section", undefined, "section panel-receipts"); section.append(node("h2", "Recent changes"));
        for (const receipt of receipts) {
          const uncertain = this.uncertainOperations.has(receipt.operation_id);
          const row = node("div", undefined, "panel-receipt");
          row.append(node("p", `${actionTitles[receipt.action] || "Computer change"} · ${uncertain ? "Outcome not confirmed" : operationState(receipt.state)}`, "panel-hint"), node("p", receipt.summary, "panel-hint panel-muted"));
          if (receipt.state === "pending" || uncertain) row.append(this.control("Check operation", `inspect:${receipt.operation_id}`, () => { void this.inspect(receipt); }));
          if (receipt.state === "unconfirmed" && receipt.computer_id) row.append(this.control("Computer controls", `receipt-computer:${receipt.operation_id}`, () => { this.view = "computers"; this.selectedComputer = receipt.computer_id; this.render(); this.plan(); }));
          section.append(row);
        }
        panel.append(section);
      }
      const updated = formatUpdatedAt(snapshot.updated_at, this.bridge.context()); if (updated) panel.append(node("p", `Updated ${updated.text}`, "stamp"));
    } else panel.append(node("p", "Loading your computers…", "panel-empty panel-muted"));
    if (this.notice) { const feedback = node("aside", undefined, `feedback ${this.tone}`); feedback.append(node("p", this.notice)); panel.append(feedback); }
    this.root.replaceChildren(panel);
    for (const summary of this.root.querySelectorAll<HTMLElement>("details summary[data-focus-key]")) if (this.disclosureState.has(summary.dataset.focusKey!)) (summary.parentElement as HTMLDetailsElement).open = this.disclosureState.get(summary.dataset.focusKey!)!;
    if (focused) for (const element of this.root.querySelectorAll<HTMLElement>("[data-focus-key]")) if (element.dataset.focusKey === focused && !(element instanceof HTMLButtonElement && element.disabled)) { element.focus({ preventScroll: true }); break; }
    if (this.selectedComputer && this.view === "computers") { const selected = [...this.root.querySelectorAll<HTMLElement>("[data-computer-id]")].find(item => item.dataset.computerId === this.selectedComputer); if (selected) { selected.tabIndex = -1; selected.focus({ preventScroll: true }); selected.scrollIntoView({ block: "nearest" }); } this.selectedComputer = undefined; }
    const message = `${this.view}${this.notice}${this.prepared?.summary || ""}`;
    if (message !== this.lastAnnounced) { this.announcement.textContent = `${this.view[0].toUpperCase() + this.view.slice(1)}${this.prepared ? `. Confirm change: ${this.prepared.summary}` : ""}${this.notice ? `. ${this.notice}` : ""}`; this.lastAnnounced = message; }
  }
}
