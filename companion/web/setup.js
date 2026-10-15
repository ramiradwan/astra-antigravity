"use strict";
const $ = id => document.getElementById(id);
const all = selector => document.querySelectorAll(selector);
const pairedStates = new Set(["paired","connecting","connected","reconnecting","draining","stopped","access_expired","connection_error","connection_invalid","renewed","renewal_approval_required"]);
let checking = false, suggested = false, requesting = false, quitting = false, timer = null;
let lastState = null, requestedStage = null, currentScreen = null, signInReturn = null;
const fieldIds = ["folder", "approved", "setup-link", "fresh-approved"];
function text(id, value) { if ($(id).textContent !== value) $(id).textContent = value; }
function notice(message, error = false, success = false) {
  text("notice", message || "Checking this computer…");
  $("notice").classList.toggle("status--danger", error);
  $("notice").classList.toggle("status--success", !error && success);
  $("notice").classList.toggle("status--attention", !error && !success);
}
function clearError() {
  $("action-error").hidden = true;
  for (const id of fieldIds) { $(id).removeAttribute("aria-invalid"); $(id + "-error").hidden = true; text(id + "-error", ""); }
}
function showError(message, field) {
  text("error-message", message); $("action-error").hidden = false;
  if (field) { $(field).setAttribute("aria-invalid", "true"); text(field + "-error", message); $(field + "-error").hidden = false; $(field).focus(); }
}
async function action(path, body = {}) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body), credentials:"same-origin"});
  const value = await response.json();
  if (!response.ok) throw new Error(value.message || "This step could not finish. Try again.");
  return value;
}
function disableActions(value) {
  all("button").forEach(button => { button.disabled = requesting || quitting || (Boolean(value.busy) && !["stop","quit","dismiss-error"].includes(button.id)); });
  $("accounts-next").disabled ||= !accountsReady(value);
  $("fresh").disabled ||= Boolean(value.worker_running) || !$("fresh-approved").checked;
  $("main").setAttribute("aria-busy", requesting ? "true" : "false");
}
async function perform(callback, field) {
  if (requesting || quitting) return;
  clearError(); requesting = true; if (lastState) disableActions(lastState);
  try { await callback(); }
  catch (error) { showError(error.message || "This step could not finish. Try again.", field); }
  finally { requesting = false; await refresh(); }
}
function accountsReady(value) { return value.accounts?.codex?.status === "connected" && value.accounts?.agy?.status === "available"; }
function renderWorkspaces(id, workspaces) {
  const root = $(id), fingerprint = JSON.stringify(workspaces || []);
  if (root.dataset.workspaces === fingerprint) return;
  root.dataset.workspaces = fingerprint; root.replaceChildren();
  for (const item of workspaces || []) {
    const row = document.createElement("div"), title = document.createElement("strong"), path = document.createElement("span"), mode = document.createElement("span");
    row.className = "workspace-fact"; title.textContent = item.id; path.textContent = item.path; path.className = "workspace-path";
    mode.textContent = item.isolation === "worktree" ? "Isolated Git worktree" : "Direct folder changes";
    row.append(title, path, mode); root.appendChild(row);
  }
}
function render(value) {
  lastState = value;
  const paired = pairedStates.has(value.status), renewal = value.status === "renewal_approval_required";
  const approval = ["pairing","approval_required","renewal_approval_required"].includes(value.status);
  const screen = approval ? "approval" : paired ? "connected" : accountsReady(value) ? requestedStage || "workspace" : "accounts";
  $("accounts").hidden = screen !== "accounts"; $("workspace").hidden = screen !== "workspace";
  $("browser-approval").hidden = screen !== "approval"; $("connected").hidden = screen !== "connected";
  $("diagnostics").hidden = !paired; $("setup-steps").hidden = paired; $("saved-task").hidden = paired || approval;
  $("quitting").hidden = true;
  text("page-title", screen === "connected" ? "Computer connection" : screen === "approval" ? "Approve this connection" : "Connect this computer");
  text("page-intro", paired ? "Manage your connection here. Delegate and follow your tasks in ChatGPT or Codex." : "Connect Codex and Antigravity, then approve a workspace for delegated tasks.");
  ["accounts","workspace","approval"].forEach((stage, index) => {
    const item = $("step-" + stage), active = stage === screen;
    if (active) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
    item.classList.toggle("complete", index < ["accounts","workspace","approval"].indexOf(screen));
  });
  const names = {connected:"Signed in with ChatGPT",available:"Tool check passed",missing:"Not installed",sign_in_required:"Sign-in needed",api_mode:"API mode — subscription sign-in needed",unavailable:"Unavailable",check_failed:"Check connection"};
  for (const name of ["codex","agy","git"]) {
    const account = value.accounts?.[name];
    all(`[data-provider-status="${name}"]`).forEach(node => { const label = account ? names[account.status] || "Check connection" : "Checking…"; if (node.textContent !== label) node.textContent = label; node.classList.toggle("ready", ["connected","available"].includes(account?.status)); });
    all(`[data-install="${name}"]`).forEach(button => { button.hidden = !account || account.installed; });
    all(`[data-sign-in="${name}"]`).forEach(button => { button.hidden = !account?.installed || account.status === "connected" || (name === "agy" && account.status === "available"); });
  }
  $("git-row").hidden = !value.accounts?.git || value.accounts.git.status === "available" || document.querySelector('[name="isolation"]:checked').value !== "worktree";
  if (!suggested && value.suggested_workspace) { $("folder").value = value.suggested_workspace; suggested = true; }
  text("task-summary", value.setup_request_id ? "Your saved task is linked. Review it on the browser approval page; you do not need to submit it again." : "To continue a task from ChatGPT or Codex, open the setup link from that conversation.");
  $("task-recovery").hidden = Boolean(value.setup_request_id);
  renderWorkspaces("workspace-summary", value.workspace); renderWorkspaces("approval-workspace", value.workspace);
  text("approval-step", renewal ? "Renew this connection" : "Step 3 of 3");
  text("approval-title", renewal ? "Approve this same computer again" : "Finish approval in your browser");
  text("approval-description", renewal ? "Renewal preserves this connection, its approved workspace and saved tasks. Complete the browser approval to reconnect." : "Review and approve this computer and workspace in your browser. Setup will continue here automatically.");
  const labels = {connected:"Connected",paired:"Ready to connect",connecting:"Connecting",reconnecting:"Reconnecting",draining:"Finishing current work",stopped:"Disconnected",access_expired:"Access expired",connection_error:"Connection interrupted",connection_invalid:"New connection needed",renewed:"Access renewed",pairing:"Preparing approval",approval_required:"Awaiting approval",renewal_approval_required:"Awaiting renewal"};
  text("connection", labels[value.status] || "Setup");
  const providerAttention = Boolean(value.accounts?.codex && value.accounts?.agy && !accountsReady(value));
  if (paired && providerAttention) $("diagnostics").open = true;
  text("connected-title", value.status === "connected" ? providerAttention ? "Check your provider sign-ins" : "This computer is connected" : value.status === "draining" ? "Finishing current work" : "Your saved connection");
  text("connected-description", value.status === "connected" ? providerAttention ? "This computer is connected. Open the provider sign-ins below to finish signing in." : "Return to ChatGPT or Codex to delegate your next task. Provider access and quota are checked when the task runs." : "Your approved workspace and saved work remain on this computer. Follow the connection status above.");
  $("linked-task").hidden = !value.setup_request_id; $("startup-row").hidden = !value.startup_available;
  if (!requesting) $("startup").checked = Boolean(value.startup_enabled);
  $("stop").hidden = !value.worker_running || value.status === "draining";
  $("start").hidden = value.worker_running || !(value.recovery_action === "reconnect" || ["paired","stopped","renewed"].includes(value.status));
  $("renew").hidden = value.recovery_action !== "renew" || renewal;
  $("fresh-approval").hidden = value.recovery_action !== "fresh" || !value.fresh_approval_required;
  $("resume").hidden = value.status !== "approval_required" || value.busy;
  $("pair-link").hidden = !approval || !value.verification_url;
  if (value.verification_url) $("pair-link").href = value.verification_url;
  text("pair-link", renewal ? "Open renewal approval" : "Open browser approval");
  text("companion-version", "Antigravity setup " + (value.companion_version || "—")); text("relay-address", "Connection service: " + (value.relay || "not connected"));
  disableActions(value);
  notice(value.message, Boolean(value.error), value.status === "connected");
  if (currentScreen && currentScreen !== screen) $(screen === "approval" ? "approval-title" : screen + "-title").focus();
  currentScreen = screen;
}
async function refresh() {
  if (checking || quitting) return;
  checking = true;
  try {
    const response = await fetch("/api/status", {credentials:"same-origin", signal:AbortSignal.timeout(10000)}), value = await response.json();
    if (!response.ok) throw new Error(value.message || "Open setup from the installed application.");
    if (!quitting) render(value);
  } catch (error) { if (!quitting) notice(error.message || "Antigravity setup is offline. Open the installed application to reconnect.", true); }
  finally { checking = false; maybeRecheckSignIn(); }
}
function maybeRecheckSignIn() {
  if (!signInReturn?.returned || !signInReturn.launched || requesting || checking || quitting || lastState?.busy || document.hidden) return;
  signInReturn = null; perform(() => action("/api/check"));
}
function startTimer() { if (timer === null && !quitting) timer = setInterval(refresh, 3000); }
all("[data-install]").forEach(button => button.addEventListener("click", () => perform(() => action("/api/install", {provider:button.dataset.install}))));
all("[data-sign-in]").forEach(button => button.addEventListener("click", () => perform(async () => {
  signInReturn = {away:false,returned:false,launched:false};
  try { await action("/api/sign-in", {provider:button.dataset.signIn}); signInReturn.launched = true; }
  catch (error) { signInReturn = null; throw error; }
})));
window.addEventListener("blur", () => { if (signInReturn) { signInReturn.away = true; signInReturn.returned = false; } });
window.addEventListener("focus", () => { if (signInReturn?.away) signInReturn.returned = true; maybeRecheckSignIn(); });
document.addEventListener("visibilitychange", () => { if (signInReturn) { if (document.hidden) { signInReturn.away = true; signInReturn.returned = false; } else if (signInReturn.away) signInReturn.returned = true; } maybeRecheckSignIn(); });
all("[data-check]").forEach(button => button.addEventListener("click", () => perform(() => { signInReturn = null; return action("/api/check"); })));
$("accounts-next").addEventListener("click", () => { if (accountsReady(lastState || {})) { requestedStage = "workspace"; render(lastState); } });
$("workspace-back").addEventListener("click", () => { requestedStage = "accounts"; render(lastState); });
$("dismiss-error").addEventListener("click", clearError);
fieldIds.forEach(id => $(id).addEventListener("input", clearError));
all('[name="isolation"]').forEach(input => input.addEventListener("change", () => { clearError(); if (lastState) render(lastState); }));
$("fresh-approved").addEventListener("change", () => { if (lastState) disableActions(lastState); });
$("attach-task").addEventListener("click", () => perform(async () => { if (!$("setup-link").value.trim()) throw new Error("Paste the setup link from your conversation."); await action("/api/intent", {uri:$("setup-link").value.trim()}); }, "setup-link"));
$("browse").addEventListener("click", () => perform(async () => { const value = await action("/api/folder"); if (value.path) $("folder").value = value.path; }, "folder"));
$("workspace-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!$("folder").value.trim()) { showError("Choose a workspace folder before connecting.", "folder"); return; }
  if (!$("approved").checked) { showError("Approve the workspace before connecting.", "approved"); return; }
  perform(() => action("/api/approve", {path:$("folder").value,isolation:document.querySelector('[name="isolation"]:checked').value,approved:$("approved").checked}), "folder");
});
$("start").addEventListener("click", () => perform(() => action("/api/start")));
$("stop").addEventListener("click", () => perform(() => action("/api/stop")));
$("resume").addEventListener("click", () => perform(() => action("/api/resume-setup")));
$("renew").addEventListener("click", () => perform(() => action("/api/renew")));
$("fresh").addEventListener("click", () => perform(() => action("/api/fresh-connection", {approved:$("fresh-approved").checked}), "fresh-approved"));
$("startup").addEventListener("change", () => perform(() => action("/api/startup", {enabled:$("startup").checked})));
$("quit").addEventListener("click", async () => {
  if (requesting || quitting) return;
  clearError(); quitting = true; if (timer !== null) { clearInterval(timer); timer = null; }
  try {
    await action("/api/quit");
    for (const id of ["accounts","workspace","browser-approval","connected","diagnostics","setup-steps","saved-task"]) $(id).hidden = true;
    $("quitting").hidden = false; $("quit").disabled = true; text("connection", "Quit requested");
    notice("Quit requested. Current work will finish before Antigravity setup exits."); $("quitting-title").focus();
  } catch (error) { quitting = false; showError(error.message || "Quit could not finish. Try again."); if (lastState) render(lastState); startTimer(); }
});
(async () => {
  try { const capability = location.hash.slice(1); if (capability) { await action("/api/session", {capability}); history.replaceState(null,"", "/"); } await refresh(); startTimer(); }
  catch (error) { showError(error.message || "Open Antigravity setup again to continue."); }
})();
