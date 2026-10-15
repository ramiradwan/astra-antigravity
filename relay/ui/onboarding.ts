import { mayOperate, type ControlComputer, type ControlSnapshot } from "./control-controller";

interface SetupControls {
  field(label: string, input: HTMLInputElement | HTMLSelectElement, key: string): HTMLElement;
  button(label: string, key: string, callback: () => void, disabled?: boolean): HTMLButtonElement;
  prepare(action: string, computerId: string, parameters?: Record<string, unknown>): void;
  acknowledged: boolean;
  acknowledge(value: boolean): void;
  invalid(message: string): void;
}
const node = <K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] => {
  const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element;
};

/** User-entered paths use app-only setup arguments, outside model summaries. */
export function computerSetup(snapshot: ControlSnapshot, computer: ControlComputer, controls: SetupControls): HTMLElement {
  const section = node("section", undefined, "section"); section.append(node("h3", "Account and workspace setup"));
  if (computer.setup_only) section.append(node("p", "Computer approved for setup. Delegated tasks cannot start until a workspace is approved.", "panel-hint"));
  const statusLabels: Record<string, string> = { ready: "Tool check passed", sign_in_required: "Sign-in needed", missing: "Not installed", unknown: "Not checked" };
  for (const provider of ["codex", "agy", "git"] as const) {
    const account = computer.accounts?.[provider];
    if (provider === "git" && !account) continue;
    const title = provider === "codex" ? "Codex" : provider === "agy" ? "Antigravity" : "Git";
    section.append(node("p", `${title} · ${statusLabels[account?.status || "unknown"] || "Not checked"}`, "panel-hint"));
    if (account?.summary) section.append(node("p", account.summary, "panel-hint panel-muted"));
    const actions = node("div", undefined, "actions");
    if ((!account || !account.installed) && mayOperate(snapshot, computer, "install_provider")) actions.append(controls.button(`Install ${title}`, `install:${computer.id}:${provider}`, () => controls.prepare("install_provider", computer.id, { provider })));
    if (provider !== "git" && account?.installed && account.status !== "ready" && mayOperate(snapshot, computer, "sign_in")) actions.append(controls.button(`Sign in to ${title}`, `signin:${computer.id}:${provider}`, () => controls.prepare("sign_in", computer.id, { provider })));
    if (actions.childElementCount) section.append(actions);
  }
  if (mayOperate(snapshot, computer, "check_accounts")) section.append(controls.button("Check accounts", `prepare:${computer.id}:check_accounts`, () => controls.prepare("check_accounts", computer.id)));
  section.append(node("p", "Existing sign-ins are reused. Provider sign-in opens on this computer. Available models and quota are checked when a task runs.", "panel-hint panel-muted"));
  if (mayOperate(snapshot, computer, "configure_workspace")) {
    const details = node("details", undefined, "disclosure"); details.open = computer.setup_only;
    const summary = node("summary", "Workspace access"); summary.dataset.focusKey = `workspace-access:${computer.id}`; details.append(summary);
    const form = node("form");
    const path = node("input"); path.type = "text"; path.maxLength = 1024; path.autocomplete = "off"; path.spellcheck = false; path.placeholder = "C:\\Projects\\my-project";
    const alias = node("input"); alias.type = "text"; alias.maxLength = 128; alias.autocomplete = "off"; alias.spellcheck = false; alias.placeholder = "my-project";
    const isolation = node("select"); isolation.append(new Option("Isolated Git worktree", "worktree"), new Option("Direct folder changes", "shared"));
    form.append(controls.field("Workspace folder on this computer", path, `workspace-path:${computer.id}`), controls.field("Workspace name", alias, `workspace-alias:${computer.id}`), node("p", "Use letters, numbers, dots, dashes or underscores for the workspace name. Use the full path to a folder on this computer.", "panel-hint panel-muted"), controls.field("Task isolation", isolation, `workspace-isolation:${computer.id}`));
    const acknowledge = node("label", undefined, "panel-checkbox"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = controls.acknowledged; checkbox.dataset.focusKey = `workspace-approval:${computer.id}`;
    checkbox.addEventListener("change", () => controls.acknowledge(checkbox.checked));
    acknowledge.append(checkbox, node("span", "Allow authorized tasks in this workspace to read and change files, run commands and use my Codex and Antigravity quotas. Direct folder mode changes the folder itself.")); form.append(acknowledge);
    const review = () => {
      if (!path.value.trim() || !/^[a-zA-Z0-9_.-]{1,128}$/.test(alias.value.trim())) { controls.invalid("Enter the workspace folder and a valid workspace name before reviewing access."); return; }
      if (controls.acknowledged) controls.prepare("configure_workspace", computer.id, { path: path.value.trim(), alias: alias.value.trim(), isolation: isolation.value });
    };
    form.append(controls.button("Review workspace access", `prepare:${computer.id}:configure_workspace`, review, !controls.acknowledged));
    form.addEventListener("submit", event => { event.preventDefault(); review(); }); details.append(form); section.append(details);
  }
  if (!computer.capabilities.includes("setup_control_v1")) section.append(node("p", "Run the current setup command on this computer to enable setup from ChatGPT.", "panel-hint panel-muted"));
  return section;
}
