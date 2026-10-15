import { z } from "zod";

export const ID = z.string().regex(/^[a-zA-Z0-9_.-]{1,128}$/);
export const TASK_TYPES = ["implementation", "analysis", "research", "document"] as const;
export const MODEL_PROFILES = ["sonnet-5.5", "gemini-3.8-high", "argon-high", "opus-5.5"] as const;
export const SCOPES = ["workspaces:read", "tasks:read", "tasks:write", "computers:manage", "account:delete"] as const;
export const COMPUTER_ACTIONS = ["pause", "resume", "stop_tasks", "rename", "set_startup", "set_automatic_updates", "uninstall", "revoke", "keep_approved", "set_preferred_workspace", "delete_account", "check_accounts", "install_provider", "sign_in", "configure_workspace"] as const;
export const MAX_BODY_BYTES = 32_768;
export const MAX_RESULT_CHARS = 8_000;
export const MAX_DECISIONS = 8;

const workspace = { workspace_id: ID };
const receipt = { delegation_id: ID };
export const inputs = {
  list_workspaces: z.object({}).strict(),
  check_integration: z.object({ ...workspace, task_type: z.enum(TASK_TYPES).default("implementation"),
    isolation: z.enum(["worktree", "shared"]).default("worktree") }).strict(),
  delegate_via_luna: z.object({ ...workspace, request_id: ID, task: z.string().trim().min(1).max(16_000),
    task_type: z.enum(TASK_TYPES).default("implementation"),
    isolation: z.enum(["worktree", "shared"]).default("worktree"),
    reasoning: z.enum(["auto", "light", "heavy"]).default("auto"),
    model: z.enum(MODEL_PROFILES).optional(),
    required_capabilities: z.array(z.string().trim().min(1).max(64)).max(12).default([]) }).strict(),
  prepare_delegation: z.object({ workspace_id: ID.optional(), request_id: ID, task: z.string().trim().min(1).max(16_000),
    task_type: z.enum(TASK_TYPES).default("implementation"), isolation: z.enum(["auto", "worktree", "shared"]).default("auto"),
    reasoning: z.enum(["auto", "light", "heavy"]).default("auto"), model: z.enum(MODEL_PROFILES).optional(),
    required_capabilities: z.array(z.string().trim().min(1).max(64)).max(12).default([]) }).strict(),
  continue_via_luna: z.object({ ...receipt, request_id: ID, findings: z.string().trim().min(1).max(8_000) }).strict(),
  cancel_delegation: z.object(receipt).strict(),
  cleanup_task_worktree: z.object({ ...receipt, force: z.boolean().default(false),
    delete_branch: z.boolean().default(false) }).strict(),
  inspect_delegation: z.object({ delegation_id: ID.optional(), request_id: ID.optional() }).strict()
    .refine(v => Boolean(v.delegation_id) !== Boolean(v.request_id), "Supply one delegation or request ID"),
  read_delegation_result: z.object(receipt).strict(),
  receive_delegation_events: z.object({ ...receipt, after_sequence: z.number().int().min(0).default(0) }).strict(),
  ack_delegation_events: z.object({ ...receipt, sequence: z.number().int().min(1) }).strict(),
  read_artifact: z.object({ ...receipt, artifact_id: ID }).strict(),
  show_delegation: z.object({ delegation_id: ID.optional() }).strict(),
  watch_delegation_ui: z.object({ delegation_id: ID.optional(), after_revision: z.string().max(128).optional(),
    wait_ms: z.union([z.literal(0), z.literal(20_000)]).default(20_000) }).strict(),
  read_artifact_ui: z.object({ ...receipt, artifact_id: ID }).strict(),
  show_computers: z.object({}).strict(),
  watch_computers_ui: z.object({after_revision:z.string().max(128).optional(),wait_ms:z.union([z.literal(0),z.literal(20_000)]).default(20_000)}).strict(),
  prepare_operation: z.object({request_id:ID,action:z.enum(COMPUTER_ACTIONS),computer_id:ID.optional(),parameters:z.object({label:z.string().trim().min(1).max(80).optional(),enabled:z.boolean().optional(),erase_history:z.literal(false).optional(),workspace_id:ID.optional(),provider:z.enum(["codex","agy","git"]).optional(),path:z.string().trim().min(1).max(1024).optional(),alias:ID.optional(),isolation:z.enum(["worktree","shared"]).optional()}).strict().default({})}).strict(),
  confirm_operation: z.object({operation_id:ID,confirmation_token:z.string().regex(/^[a-f0-9]{64}$/)}).strict(),
  inspect_operation: z.object({operation_id:ID}).strict(),
  export_account_ui: z.object({}).strict(),
  prepare_artifact_download_ui: z.object({...receipt,artifact_id:ID}).strict(),
  approve_pairing_ui:z.object({code:z.string().regex(/^[a-f0-9]{24}$/),request_id:ID.optional(),approval_mode:z.literal("until_revoked")}).strict()
} as const;

export type ToolName = keyof typeof inputs;
export const descriptions: Record<ToolName, string> = {
  list_workspaces: "List workspace aliases registered on your paired workers. Worker filesystem paths stay private.",
  check_integration: "Read cached worker readiness for a registered workspace. Does not start model inference.",
  delegate_via_luna: "Queue the authorized task on your paired worker. Uses your Codex and Antigravity quotas; can run commands or modify files. Returns a durable receipt before inference. Preserve request_id for recovery.",
  prepare_delegation: "Save an authorized first task and return its durable receipt plus guided setup URL. Starts only after an owned workspace is explicitly approved and connected. Existing approved workspace aliases can be reused. Preserve this request_id and delegation_id; setup resumes the same task once.",
  continue_via_luna: "Queue a correction of your saved task on the same worker, workspace and model. Uses provider quotas and may modify files. Preserve request_id for recovery.",
  cancel_delegation: "Request cancellation of your queued or running task. Confirmation arrives as an event; existing file changes remain.",
  cleanup_task_worktree: "Request deletion of the owned task worktree after review. Force may discard uncommitted changes; delete_branch removes its local branch.",
  inspect_delegation: "Recover a lost launch receipt or inspect an owned saved task. Never restarts inference.",
  read_delegation_result: "Read the saved bounded terminal handoff for independent verification. Full transcripts and provider credentials stay on the worker.",
  receive_delegation_events: "For deterministic host adapters: wait for compact decisions or completion, bounded to 25 seconds. The parent model must not loop on this tool; prefer a host event subscription.",
  ack_delegation_events: "Acknowledge an event only after the host accepts delivery; an explicit cursor can replay it after reconnect.",
  read_artifact: "Request a bounded owned artifact from the paired worker by its published artifact ID. Only approved task artifacts may be exported.",
  show_delegation: "Show the connection or owned task card once. Component refresh is independent of the parent model. Does not start or retry work.",
  watch_delegation_ui: "Component-only owned status refresh, bounded to 20 seconds. Never acknowledges parent events or starts work.",
  read_artifact_ui: "Legacy component-only retrieval up to 12 KB. Prefer prepare_artifact_download_ui for native ChatGPT file transfer up to 20 MiB. File delivery never opens a browser download page.",
  show_computers:"Open connected computers, approved workspaces, settings and operation receipts. Does not start work or change settings.",
  watch_computers_ui:"Component-only foreground settings refresh. Does not start tasks or change settings.",
  prepare_operation:"Prepare an owned computer or account change for explicit confirmation. Returns an expiring confirmation; does not apply the change.",
  confirm_operation:"Confirm the exact prepared change after the user reviews its summary. Offline machine changes remain pending until acknowledged.",
  inspect_operation:"Read a durable receipt for a previously prepared or confirmed change. Never retries the action.",
  export_account_ui:"Component-only export of owned relay records. Credentials and authentication secrets are excluded.",
  prepare_artifact_download_ui:"Component-only short-lived download grant for an owned published artifact up to 20 MiB. File content is never included in model context.",
  approve_pairing_ui:"Component-only approval of the computer pairing code after explicit user consent to ongoing computer management. An empty computer has no workspace authority until a separate folder approval succeeds."
};

export function scopeFor(name: ToolName): string {
  if (["prepare_operation","confirm_operation","approve_pairing_ui"].includes(name)) return "computers:manage";
  if (name === "show_computers" || name === "watch_computers_ui" || name === "inspect_operation") return "workspaces:read";
  if (name === "list_workspaces" || name === "check_integration") return "workspaces:read";
  return ["delegate_via_luna", "prepare_delegation", "continue_via_luna", "cancel_delegation", "cleanup_task_worktree", "ack_delegation_events"].includes(name)
    ? "tasks:write" : "tasks:read";
}

export class RelayError extends Error {
  constructor(public code: string, message: string, public status = 400) {
    super(message);
    // RPC preserves Error.name, unlike custom prototype and fields.
    this.name = `AstraRelay:${code}:${status}`;
  }
}

export function relayError(error: unknown): { code: string; message: string; status: number } | null {
  if (error instanceof RelayError) return error;
  if (error instanceof Error) {
    const encoded = /^AstraRelay:([a-z_]{1,64}):([45][0-9]{2})$/.exec(error.name);
    if (encoded) return { code: encoded[1], message: error.message.slice(0, 240), status: Number(encoded[2]) };
  }
  // Durable Object RPC reconstructs errors in the caller's isolate.
  if (error instanceof Error && "code" in error && "status" in error
    && typeof error.code === "string" && /^[a-z_]{1,64}$/.test(error.code)
    && typeof error.status === "number" && error.status >= 400 && error.status <= 599)
    return { code: error.code, message: error.message.slice(0, 240), status: error.status };
  return null;
}

// Boundary objects arrive from bounded JSON or a validated schema. Avoid a
// recursive RPC type; Cloudflare's serialization mapping expands it indefinitely.
export type Json = unknown;
export type ObjectValue = Record<string, unknown>;
export function object(value: unknown): ObjectValue {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new RelayError("invalid_object", "Expected an object");
  return value as ObjectValue;
}

export function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value !== null && typeof value === "object") return "{" + Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => JSON.stringify(key) + ":" + canonical(item)).join(",") + "}";
  const encoded = JSON.stringify(value);
  if (encoded === undefined) throw new RelayError("invalid_json", "Value is not JSON");
  return encoded;
}

export async function digest(value: string): Promise<string> {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)))].map(v => v.toString(16).padStart(2, "0")).join("");
}

export function token(bytes = 32): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(bytes)), v => v.toString(16).padStart(2, "0")).join("");
}

export async function boundedJson(request: Request): Promise<unknown> {
  const body = await boundedBytes(request);
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(body)); }
  catch { throw new RelayError("invalid_json", "Valid UTF-8 JSON required"); }
}
export async function boundedBytes(request: Request): Promise<Uint8Array> {
  const declared = request.headers.get("Content-Length");
  if (declared && Number(declared) > MAX_BODY_BYTES) throw new RelayError("payload_too_large", "Request exceeds 32 KiB", 413);
  if (!request.body) throw new RelayError("empty_body", "JSON body required");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > MAX_BODY_BYTES) { await reader.cancel(); throw new RelayError("payload_too_large", "Request exceeds 32 KiB", 413); }
      chunks.push(next.value);
    }
  } finally { reader.releaseLock(); }
  const body = new Uint8Array(size);
  let cursor = 0;
  for (const chunk of chunks) { body.set(chunk, cursor); cursor += chunk.length; }
  return body;
}
