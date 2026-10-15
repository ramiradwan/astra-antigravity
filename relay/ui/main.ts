import { App } from "@modelcontextprotocol/ext-apps";
import { applyDocumentTheme, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import type { UiArtifact, UiSnapshot } from "../src/ui-contracts";
import { canWriteTasks, cardStatus, cardView, downloadRoute, formatUpdatedAt, outcomePresentation, SnapshotWatch, taskHeading, type CardAction, type StatusIcon } from "./controller";
import { ControlPanel } from "./control-panel";
import { downloadGrantFrom } from "./control-controller";
import { nativeFileSupport, transferNativeFile, type NativeFileMode, type NativeFileRequest } from "./native-files";

const app = new App({ name: "Delegate to Antigravity", version: "0.17.0" }, {}, { autoResize: true });
const root = document.getElementById("root")!;
const announcement = document.getElementById("announcement")!;
let ready = false;
let busy = "";
let notice = "";
let noticeTone = "info";
let emptyTitle = "Waiting for task details";
let lastAnnounced = "";
let cardVisible = true;
const pendingDownloads = new Set<string>();
const disclosureState = new Map<string, boolean>();
const watch = new SnapshotWatch({
  callTool: (name, args) => app.callServerTool({ name, arguments: args }, { timeout: 30_000 }),
}, render);
watch.setVisible(false);
const panel = new ControlPanel(root, announcement, {
  callTool: (name, args) => app.callServerTool({ name, arguments: args }, { timeout: 30_000 }),
  async openTask(id, current) {
    const response = await app.callServerTool({ name: "show_delegation", arguments: { delegation_id: id } });
    if (!current()) return;
    if (!watch.accept(response)) throw new Error("Task details could not be loaded. No task was restarted.");
  },
  transferFile,
  fileMode,
  context: () => app.getHostContext() || {},
  modeChanged: () => { syncVisibility(); render(); },
});
const syncVisibility = () => {
  const visible = ready && cardVisible && document.visibilityState === "visible";
  watch.setVisible(visible && !panel.active);
  panel.setVisible(visible);
};
const visibilityObserver = typeof IntersectionObserver === "undefined" ? undefined : new IntersectionObserver(entries => {
  cardVisible = entries.some(entry => entry.isIntersecting);
  syncVisibility();
});
visibilityObserver?.observe(root);

function element<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function paragraph(text: string, className?: string): HTMLParagraphElement { return element("p", text, className); }
function icon(name: StatusIcon): SVGSVGElement {
  const paths: Record<StatusIcon, string> = {
    check: "M5 12l4 4L19 6",
    clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18M12 7v5l3 2",
    activity: "M3 12h4l3-7 4 14 3-7h4",
    alert: "M12 3 2 21h20L12 3M12 9v5M12 17v.1",
    close: "M6 6l12 12M18 6 6 18",
    computer: "M3 4h18v13H3zM8 21h8M12 17v4",
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("status-icon");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", paths[name]);
  svg.append(path);
  return svg;
}
function feedback(text: string, tone: string): HTMLElement {
  const aside = element("aside", undefined, `feedback ${tone}`);
  aside.append(icon(tone === "success" ? "check" : tone === "info" ? "computer" : "alert"), paragraph(text));
  return aside;
}
class ActionFeedback extends Error {
  constructor(message: string, readonly tone: "warning" | "danger" = "danger") { super(message); }
}
function disclosure(snapshot: UiSnapshot, key: string, label: string, content: HTMLElement): HTMLDetailsElement {
  const details = element("details", undefined, "disclosure");
  const disclosureKey = `${snapshot.delegation_id || "connection"}:${key}`;
  details.dataset.disclosureKey = disclosureKey;
  details.open = disclosureState.get(disclosureKey) === true;
  details.addEventListener("toggle", () => disclosureState.set(disclosureKey, details.open));
  const summary = element("summary", label);
  summary.dataset.focusKey = key;
  details.append(summary, content);
  return details;
}
function button(label: string, action: string, primary = false): HTMLButtonElement {
  const node = element("button", label, "action" + (primary ? " primary" : ""));
  node.type = "button";
  node.dataset.action = action;
  node.disabled = !ready;
  // Retain keyboard focus during an action. The click handler also enforces
  // this disabled state, including host tool requests still in flight.
  if (busy) node.setAttribute("aria-disabled", "true");
  return node;
}
const busyLabels: Record<CardAction["id"], string> = {
  connect: "Opening…", refresh: "Checking…", cancel: "Requesting cancellation…",
  respond: "Sending…", opus: "Sending…", review: "Sending…",
};
function displayBytes(value?: number): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  return value < 1024 ? `${value} B` : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
type ChatGptFileHost = { uploadFile?: (file: File, options: { library: true }) => Promise<{ fileId: string }> };
function fileHost(): ChatGptFileHost | undefined { return (window as unknown as { openai?: ChatGptFileHost }).openai; }
function fileMode(): NativeFileMode { return nativeFileSupport({ hostDownload: Boolean(app.getHostCapabilities()?.downloadFile), openai: fileHost() }); }
function transferFile(request: NativeFileRequest, pins?: { expectedOrigin: string; expectedBytes: number; expectedSha256: string }) {
  const extension = fileHost();
  return transferNativeFile(request, {
    ...(app.getHostCapabilities()?.downloadFile ? { downloadFile: (params: NativeFileRequest) => app.downloadFile(params) } : {}),
    ...(typeof extension?.uploadFile === "function" ? { uploadFile: (file: File, options: { library: true }) => extension.uploadFile!(file, options) } : {}),
    ...pins,
  });
}
function render(): void {
  if (panel.active) { panel.render(); return; }
  const snapshot = watch.current;
  if (!snapshot) {
    if (notice) {
      const article = element("article", undefined, "card");
      article.append(element("h1", emptyTitle), feedback(notice, noticeTone));
      root.replaceChildren(article);
    }
    return;
  }
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement.dataset.focusKey || document.activeElement.dataset.action : undefined;
  // Read native state synchronously: a watch response can arrive before the
  // browser dispatches the disclosure's deferred toggle event.
  for (const details of root.querySelectorAll<HTMLDetailsElement>("details[data-disclosure-key]")) {
    disclosureState.set(details.dataset.disclosureKey!, details.open);
  }
  const view = cardView(snapshot);
  const status = cardStatus(snapshot, view.title);
  const pendingDecision = Boolean(snapshot.decision && (!snapshot.terminal || typeof snapshot.decision.opus_available === "boolean"));
  const accessBlocked = ["expired", "revoked"].includes(snapshot.connection.state);
  const article = element("article", undefined, `card ${view.tone}`);
  article.setAttribute("aria-busy", busy ? "true" : "false");
  const header = element("header", undefined, "card-header");
  const heading = element("h1", taskHeading(snapshot));
  heading.tabIndex = -1;
  heading.dataset.focusKey = "task-heading";
  const statusLine = element("div", undefined, "status-line");
  const badge = element("span", undefined, `task-status ${view.tone}`);
  badge.append(icon(status.icon), element("span", status.label));
  statusLine.append(badge);
  if (status.note) statusLine.append(element("span", status.note, "status-note"));
  if (!canWriteTasks(snapshot)) statusLine.append(element("span", "View only", "access-label"));
  header.append(heading, statusLine);
  article.append(header);
  const outcome = snapshot.terminal && !accessBlocked && !pendingDecision ? outcomePresentation(view.detail) : { preview: view.detail };
  if (outcome.preview !== heading.textContent) article.append(paragraph(outcome.preview, "latest-update"));
  if (outcome.full) article.append(disclosure(snapshot, "result-details", "Result details", paragraph(outcome.full)));
  const meta = element("dl", undefined, "meta");
  for (const [label, value] of [["Workspace", snapshot.workspace_label], ["Model", snapshot.selected_model]]) {
    if (!value) continue;
    const pair = element("div");
    pair.append(element("dt", label), element("dd", value));
    meta.append(pair);
  }
  if (meta.childElementCount) article.append(meta);
  if (!accessBlocked && snapshot.terminal && snapshot.worker_checks?.length) {
    const section = element("section", undefined, "section");
    const list = element("ul");
    for (const check of snapshot.worker_checks) list.append(element("li", String(check)));
    const details = disclosure(snapshot, "worker-checks", `Worker-reported checks (${snapshot.worker_checks.length})`, list);
    details.classList.add("checks");
    section.append(details);
    article.append(section);
  }
  if (!accessBlocked && snapshot.terminal && snapshot.artifacts?.length) {
    const section = element("section", undefined, "section");
    section.append(element("h2", "Files"));
    const files = element("ul", undefined, "files");
    for (const artifact of snapshot.artifacts) {
      const row = element("li", undefined, "file");
      const label = element("span", artifact.name, "file-label");
      label.append(element("small", displayBytes(artifact.bytes)));
      row.append(label);
      const mode = fileMode(), route = downloadRoute(artifact, mode !== "unavailable");
      if (artifact.artifact_id && route !== "unavailable") {
        const action = `download:${artifact.artifact_id}`;
        const label = mode === "chatgpt-upload" ? pendingDownloads.has(artifact.artifact_id) ? "Retry adding" : "Add to ChatGPT" : pendingDownloads.has(artifact.artifact_id) ? "Retry download" : "Download";
        const download = button(busy === action ? "Preparing…" : label, action);
        download.className = "text-action";
        download.setAttribute("aria-label", `${mode === "chatgpt-upload" ? "Add to ChatGPT:" : "Download"} ${artifact.name}`);
        row.append(download);
      } else label.append(element("small", mode === "unavailable"
        ? "File transfer is unavailable in this ChatGPT host. The saved file remains on your computer."
        : artifact.reason || "This file cannot be transferred here. It remains on your computer."));
      files.append(row);
    }
    section.append(files);
    article.append(section);
  }
  const actions = element("div", undefined, "actions");
  const footer = element("footer", undefined, "card-footer");
  const computerButton = button("Computer", "computer"); computerButton.className = "text-action"; footer.append(computerButton);
  for (const action of view.actions) {
    const control = button(busy === action.id ? busyLabels[action.id] : action.label, action.id, action.primary);
    if (action.id === "refresh") {
      control.className = "text-action refresh";
      footer.append(control);
    } else actions.append(control);
  }
  if (actions.childElementCount) article.append(actions);
  if (watch.error || notice) article.append(feedback(watch.error || notice, watch.error ? "warning" : noticeTone));
  const timestamp = formatUpdatedAt(snapshot.updated_at, app.getHostContext());
  const stamp = paragraph("", "stamp");
  if (timestamp) {
    const time = element("time", timestamp.text);
    time.dateTime = timestamp.iso;
    time.title = timestamp.full;
    stamp.append("Updated ", time);
  }
  if (!snapshot.terminal) {
    if (timestamp) stamp.append(" · ");
    stamp.append(watch.error ? "Retrying updates" : snapshot.delegation_id ? "Live updates" : "Automatic connection checks");
  }
  if (stamp.textContent) footer.prepend(stamp);
  if (footer.childElementCount) article.append(footer);
  root.replaceChildren(article);
  if (focused) {
    let restored = false;
    for (const node of root.querySelectorAll<HTMLElement>("[data-focus-key], button[data-action]")) {
      if ((node.dataset.focusKey || node.dataset.action) === focused && !(node instanceof HTMLButtonElement && node.disabled)) { node.focus({ preventScroll: true }); restored = true; break; }
    }
    if (!restored) heading.focus({ preventScroll: true });
  }
  // Announce meaningful transitions, not routine timestamps or identical reads.
  const feedbackText = watch.error || notice;
  const meaningful = `${status.label}\n${status.note || ""}\n${pendingDecision ? snapshot.decision?.summary || "" : ""}\n${snapshot.terminal ? snapshot.outcome || "" : ""}\n${feedbackText}`;
  if (meaningful !== lastAnnounced) {
    announcement.textContent = status.label + (status.note ? `. ${status.note}` : "") + (pendingDecision ? `. ${snapshot.decision!.summary}` : "") + (feedbackText ? `. ${feedbackText}` : "");
    lastAnnounced = meaningful;
  }
}

async function send(text: string): Promise<void> {
  const result = await app.sendMessage({ role: "user", content: [{ type: "text", text }] });
  if (result.isError) throw new Error("The message was not sent. Please continue in chat.");
  notice = "Continue in the conversation.";
}

async function download(snapshot: UiSnapshot, artifact: UiArtifact): Promise<void> {
  const id = artifact.artifact_id!;
  const mode = fileMode();
  if (downloadRoute(artifact, mode !== "unavailable") !== "native")
    throw new ActionFeedback("File transfer is unavailable in this ChatGPT host. Your saved result is unchanged; no task was restarted.", "warning");
  const response = await app.callServerTool({ name: "prepare_artifact_download_ui", arguments: { delegation_id: snapshot.delegation_id, artifact_id: id } }, { timeout: 30_000 });
  const grant = downloadGrantFrom(response, artifact);
  if (!grant) {
    const pending = response._meta?.status === "transfer_requested";
    if (pending) pendingDownloads.add(id);
    throw new ActionFeedback(pending ? `The file is being prepared on your computer. Keep it connected, then choose ${mode === "chatgpt-upload" ? "Retry adding" : "Retry download"} here.` : "The file could not be prepared or its details changed. Refresh the task and try again. No file was sent.", pending ? "warning" : "danger");
  }
  pendingDownloads.delete(id);
  let responseDownload: Awaited<ReturnType<typeof transferFile>>;
  try { responseDownload = await transferFile({ contents: [{ type: "resource_link", uri: grant.url, name: grant.name, mimeType: grant.mime_type, size: grant.bytes }] }, { expectedOrigin: grant.origin, expectedBytes: grant.bytes, expectedSha256: grant.sha256 }); }
  catch { throw new ActionFeedback(mode === "chatgpt-upload" ? "The file transfer could not be confirmed. Check ChatGPT before trying again." : "The download could not be confirmed. Check your downloads before requesting it again.", "warning"); }
  if (responseDownload.isError) {
    throw new ActionFeedback("The download was not approved. You can choose Download again when you are ready.", "warning");
  }
  notice = responseDownload.kind === "chatgpt-upload" ? "File sent to ChatGPT." : "Download requested. Check your downloads.";
  noticeTone = "success";
}

root.addEventListener("click", event => {
  const control = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-action]");
  if (!control || control.disabled || busy || !ready || !watch.current) return;
  const action = control.dataset.action!;
  const snapshot = watch.current;
  if (action === "refresh") { notice = ""; watch.refresh(); return; }
  busy = action; notice = ""; noticeTone = "info"; render();
  void (async () => {
    const task = snapshot.delegation_id;
    if (action === "computer" || action === "connect") await panel.open("computers", undefined, snapshot.request_id);
    else if (action === "cancel" && task && canWriteTasks(snapshot)) {
      const result = await app.callServerTool({ name: "cancel_delegation", arguments: { delegation_id: task } });
      if (result.isError) throw new Error("Cancellation could not be confirmed. Check status before trying again.");
      notice = "Cancellation requested. Waiting for the worker to confirm.";
      watch.refresh();
    } else if (action === "respond" && task) await send(canWriteTasks(snapshot)
      ? `I want to respond to the decision for delegation ${task}. Please show the required question and help me continue the same task.`
      : `Explain the pending decision for delegation ${task}. This plugin connection is view-only; do not start or continue work through it.`);
    else if (action === "opus" && task && canWriteTasks(snapshot) && snapshot.decision?.opus_available) await send(`I explicitly choose Opus 5.5 as the fallback for unavailable Gemini 4 Argon for delegation ${task}. Verify availability and continue the original task without duplicating work.`);
    else if (action === "review" && task) await send(`Review the saved result for delegation ${task}. Read the bounded handoff once and independently verify the relevant artifacts and checks before describing the work as verified.`);
    else if (action.startsWith("download:")) {
      const artifact = snapshot.artifacts?.find(item => item.artifact_id === action.slice(9));
      if (artifact) await download(snapshot, artifact);
    }
  })().catch(error => {
    notice = error instanceof Error ? error.message : "The action could not be completed. Your task has not been restarted.";
    noticeTone = error instanceof ActionFeedback ? error.tone : "danger";
  })
    .finally(() => { busy = ""; render(); });
});

// Approval-gated tool input may arrive after initialization. Rendering waits for
// the owned server snapshot rather than assuming preloaded window globals.
app.ontoolinput = () => { /* The subsequent tool-result contains authoritative state. */ };
app.ontoolresult = result => {
  if (panel.accept(result)) return;
  panel.hide();
  if (!watch.accept(result)) {
    emptyTitle = "Task status unavailable";
    notice = "The task details could not be loaded. Continue in chat to check the connection. No task was restarted.";
    noticeTone = "warning";
    render();
  }
};
app.ontoolcancelled = () => { emptyTitle = "Card request cancelled"; notice = "The card request was cancelled. Your task has not been restarted."; noticeTone = "info"; render(); };
app.onteardown = async () => { watch.dispose(); panel.dispose(); return {}; };
app.onhostcontextchanged = context => {
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  if (context.locale || context.timeZone) render();
};
document.addEventListener("visibilitychange", syncVisibility);
window.addEventListener("pagehide", () => { visibilityObserver?.disconnect(); watch.dispose(); panel.dispose(); }, { once: true });
void app.connect().then(() => {
  ready = true;
  const context = app.getHostContext();
  if (context?.theme) applyDocumentTheme(context.theme);
  if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
  syncVisibility();
  render();
}).catch(() => {
  root.replaceChildren(paragraph("This card could not connect to ChatGPT. Your task has not been restarted. Continue in the conversation to check its status.", "card"));
});
