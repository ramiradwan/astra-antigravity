import type { UiSnapshot } from "../src/ui-contracts";

const connection = { state: "online" as const, setup_url: "https://relay.example.test/connect" };
const base: UiSnapshot = {
  version: 1, delegation_id: "gallery-report", request_id: "original-request", revision: "gallery-1", status: "running", terminal: false,
  summary: "Preparing your report in the approved workspace.", task_summary: "Create the quarterly report",
  updated_at: "2026-10-15T12:30:00Z", workspace_label: "Quarterly reports", selected_model: "Gemini 3.8 High",
  connection, notifications: { active: false }, permissions: { can_write_tasks: true },
};
// Only synthetic data. HTTPS links are inert in the mock host and never opened.
const setup = "https://relay.example.test/connect?request_id=gallery-report";
export const cardFixtures: Record<string, UiSnapshot> = {
  setup: { ...base, status: "awaiting_setup", summary: "Your task is saved.", connection: { state: "setup_required", setup_url: setup }, setup_url: setup },
  connected: { ...base, delegation_id: undefined, task_summary: undefined, status: "connection" },
  "view-only": { ...base, delegation_id: undefined, status: "connection", permissions: { can_write_tasks: false } },
  queued: { ...base, status: "queued", summary: "Your task is queued on its approved computer." },
  running: base,
  cancelling: { ...base, status: "cancelling" },
  decision: { ...base, status: "model_unavailable", terminal: true, decision: { summary: "Gemini 4 Argon High is unavailable for this account. Opus 5.5 is available if you choose it.", opus_available: true } },
  "decision-no-fallback": { ...base, status: "model_unavailable", terminal: true, decision: { summary: "Gemini 4 Argon High and Opus 5.5 are unavailable for this account.", opus_available: false } },
  offline: { ...base, connection: { state: "offline", setup_url: setup }, setup_url: setup },
  expired: { ...base, connection: { state: "expired", setup_url: setup }, setup_url: setup },
  revoked: { ...base, connection: { state: "revoked", setup_url: setup }, setup_url: setup },
  recovery: { ...base, status: "recovery_required", connection: { state: "unknown", setup_url: setup }, setup_url: setup },
  completed: { ...base, status: "completed", terminal: true, outcome: "Created the report and saved it in your approved workspace. The totals and source links are ready for review.", worker_checks: ["Validated report totals against the supplied spreadsheet.", "Opened the report and checked its sections."], artifacts: [{ artifact_id: "quarterly-report", name: "Quarterly report.pdf", bytes: 248_320, mime_type: "application/pdf", download_available: true, download_origin: "https://relay.example.test" }] },
  failed: { ...base, status: "failed", terminal: true, outcome: "The source spreadsheet was not available. Saved partial work remains in the approved workspace." },
  cancelled: { ...base, status: "cancelled", terminal: true, outcome: "Cancellation was confirmed. Changes already saved remain on your computer." },
  long: { ...base, task_summary: "Create the quarterly accessibility and operations report for the European customer onboarding programme, including all supplied source references and verification notes", workspace_label: "European customer onboarding programme and quarterly operations", selected_model: "Gemini 4 Argon High — explicitly selected", status: "completed", terminal: true, outcome: "Created the requested report. " + "The worker checked the supplied sources and documented the remaining limitations. ".repeat(12), worker_checks: ["Checked the long source reference in the supplied quarterly accessibility and operations report."], artifacts: [{ artifact_id: "long-report", name: "Quarterly_accessibility_and_operations_report_for_the_European_customer_onboarding_programme_2026.pdf", bytes: 248_320, download_available: true, download_origin: "https://relay.example.test" }] },
  download: { ...base, status: "completed", terminal: true, outcome: "Saved the requested file.", artifacts: [{ artifact_id: "answer", name: "answer.txt", bytes: 2, sha256: "73475cb40a568e8da8a045ced110137e159f890ac4da883b6b17dc651b3a8049", mime_type: "text/plain", export_available: true, download_available: true, download_origin: "https://relay.example.test" }] },
};

const accounts = {
  codex: { installed: true, status: "connected" }, agy: { installed: true, status: "available" }, git: { installed: true, status: "available" },
};
const workspace = [{ id: "reports", path: "C:\\Astra demo\\Quarterly reports", isolation: "worktree" }];
const ready = { status: "connected", worker_running: true, busy: false, error: false, accounts, workspace,
  companion_version: "0.3.0", relay: "https://relay.example.test", setup_request_id: "gallery-report", startup_available: true, startup_enabled: false,
  message: "Your computer is connected. Return to your conversation to delegate a task." };
export const companionFixtures: Record<string, Record<string, unknown>> = {
  accounts: { ...ready, status: "setup", worker_running: false, accounts: { codex: { installed: true, status: "sign_in_required" }, agy: { installed: false, status: "missing" }, git: accounts.git }, message: "Connect your accounts to continue." },
  workspace: { ...ready, status: "ready", worker_running: false, workspace: [], suggested_workspace: "C:\\Astra demo\\Quarterly reports", message: "Choose the workspace this computer may use." },
  approval: { ...ready, status: "approval_required", worker_running: false, verification_url: "/gallery/hosted/pair", message: "Review and approve this computer in your browser." },
  connected: ready,
  reconnecting: { ...ready, status: "reconnecting", message: "Reconnecting to the relay. Your saved task will not be restarted." },
  draining: { ...ready, status: "draining", message: "Current work will finish before disconnecting." },
  expired: { ...ready, status: "access_expired", worker_running: false, recovery_action: "renew", error: true, message: "Access expired. Renew this same connection to keep your approved workspace." },
  renewal: { ...ready, status: "renewal_approval_required", worker_running: false, recovery_action: "renew", verification_url: "/gallery/hosted/renew", message: "Approve renewal for the same computer and workspace." },
  revoked: { ...ready, status: "connection_invalid", worker_running: false, recovery_action: "fresh", fresh_approval_required: true, error: true, message: "This computer is no longer authorized. Saved work remains on this computer." },
  error: { ...ready, status: "connection_error", worker_running: false, recovery_action: "reconnect", error: true, message: "The connection was interrupted. Reconnect to continue." },
  "action-error": { ...ready, status: "ready", worker_running: false, workspace: [], suggested_workspace: "C:\\Astra demo\\Quarterly reports", message: "Choose the workspace this computer may use." },
};
