import { DurableObject } from "cloudflare:workers";
import { createHash, type Hash } from "node:crypto";
import { z } from "zod";
import { boundedJson, canonical, digest, ID, inputs, MAX_DECISIONS, MAX_RESULT_CHARS, object, relayError, RelayError, token,
  type Json, type ObjectValue, type ToolName } from "./contracts";
import { callbackUrl, equal, publicOrigin, seal, sealArtifactDownload, signedCallback, signingKey, unseal, unbase64 } from "./security";
import { authorizationServer, type AuthProps } from "./auth";
import type { UiSnapshot, UiArtifact, UiControlSnapshot, UiOperation } from "./ui-contracts";
import { companionSetupSettings } from "./companion-setup";

export const workspaceSchema = z.object({ id: ID, label: z.string().trim().min(1).max(80),
  capabilities: z.array(z.enum(["local_files", "shell"])).max(2),
  isolation: z.array(z.enum(["worktree", "shared"])).min(1).max(2),
  task_types: z.array(z.enum(["implementation", "analysis", "research", "document"])).min(1).max(4)
}).strict();
export const pairingSchema = z.object({ label: z.string().trim().min(1).max(80),
  credential_hash: z.string().regex(/^[a-f0-9]{64}$/), workspaces: z.array(workspaceSchema).max(16),
  setup_only:z.boolean().default(false),setup_request_id: ID.optional(), approval_mode:z.enum(["until_revoked","legacy_30_day"]).optional() }).strict().refine(value=>value.workspaces.length>0||value.setup_only,"A task computer requires an approved workspace");
export type Workspace = z.infer<typeof workspaceSchema>;
export type PairingInput = z.input<typeof pairingSchema>;
type WorkerRow = { id: string; label: string; credential_hash: string; expires: number; revoked: number; workspaces: string; readiness: string; last_seen: number; transport_capabilities: string;
  approval_mode:string;access_expires:number;refresh_hash:string;paused:number;admission_revision:number;computer_snapshot:string;setup_request_id:string;setup_managed:number };
type OperationRow={id:string;request_id:string;fingerprint:string;worker_id:string;action:string;parameters:string;state:string;summary:string;confirmation:string;created:number;updated:number;expires:number;revision:number;completion_hash:string;completion_secret:string;completion_expires:number;result_fingerprint:string};
type JobRow = { id: string; request_id: string; fingerprint: string; worker_id: string; workspace_id: string; source_id: string | null;
  operation: string; payload: string; task_type: string; status: string; terminal: number; local_id: string | null;
  sequence: number; worker_sequence: number; model_pin: string | null; acknowledged: number; result: string | null; created: number; expires: number; display_summary: string };
type CommandRow = { id: string; job_id: string; worker_id: string; operation: string; payload: string; state: string; result:string|null };
type EventRow = { sequence: number; payload: string; source_fingerprint:string };
type SubscriptionRow = { id: string; job_id: string; url: string; secret: string; expires: number; cursor: number; state: string; attempts: number; generation: string; next_attempt: number; verified: number };
interface Connection { worker_id: string; generation: string }
type DownloadRow = { id: string; job_id: string; artifact_id: string; expires: number; state: string };
type RenewalRow = { id: string; worker_id: string; old_hash: string; new_hash: string; expires: number; approved: number };
type ArtifactManifest = { delegation_id: string; artifact_id: string; file_name: string; mime_type: string; size: number; sha256: string; eligible: boolean; stream_available: boolean };
interface ActiveDownload {
  id: string; worker_id: string; socket: WebSocket; manifest: ArtifactManifest; offset: number; credit: boolean;
  writer: WritableStreamDefaultWriter<Uint8Array>; ready: () => void; reject: (error: Error) => void;
  idle: ReturnType<typeof setTimeout>; total: ReturnType<typeof setTimeout>; opened: boolean;
  hash: Hash;
}
const MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024;
const DOWNLOAD_CHUNK_BYTES = 16 * 1024;
const RENEWAL_WINDOW_MS = 72 * 3_600_000;
const PERMANENT_APPROVAL = 8_640_000_000_000_000;
const ACCESS_TTL = 24 * 3_600_000;
const providerAccountSchema=z.object({installed:z.boolean(),status:z.enum(["ready","sign_in_required","missing","unknown"]),summary:z.string().max(240).optional()}).strict();
const computerSnapshotSchema=z.object({paused:z.boolean().optional(),setup_only:z.boolean().optional(),installed:z.boolean().optional(),active_tasks:z.number().int().min(0).max(1000).optional(),pending_delivery:z.boolean().optional(),companion_version:z.string().max(64).optional(),startup_enabled:z.boolean().optional(),automatic_updates_enabled:z.boolean().optional(),update_status:z.string().max(240).optional(),uninstall_status:z.string().max(240).optional(),accounts:z.object({codex:providerAccountSchema.optional(),agy:providerAccountSchema.optional(),git:providerAccountSchema.optional()}).strict().optional()}).strict();

const eventSchema = z.object({ sequence: z.number().int().min(1).max(9),
  kind: z.enum(["decision", "needs_decision", "completed", "failed", "cancelled", "recovery_required"]),
  terminal: z.boolean(), summary: z.string().max(1_600),
  task_type: z.enum(["implementation", "analysis", "research", "document"]),
  model: z.string().max(100).nullable().optional(), verification_required: z.boolean(),
  checks_count: z.number().int().min(0).max(1_000_000).optional(),
  artifacts_count: z.number().int().min(0).max(1_000_000).optional()
}).strict();

function eventLength(event: ObjectValue): number {
  // Match the local gateway's escaped-character budget, including Unicode and
  // all metadata. Summary length alone doesn't bound parent context usage.
  return JSON.stringify(event).replace(/[\u007f-\uffff]/g,c=>"\\u"+c.charCodeAt(0).toString(16).padStart(4,"0")).length;
}
function compactEvent(event: ObjectValue): ObjectValue {
  const limit=event.terminal ? 1_600 : 600;
  if(eventLength(event)<=limit) return event;
  const summary=Array.from(String(event.summary));
  let low=0,high=summary.length;
  while(low<high) {
    const middle=Math.ceil((low+high)/2),candidate={...event,summary:summary.slice(0,middle).join("")};
    if(eventLength(candidate)<=limit) low=middle;else high=middle-1;
  }
  const compact={...event,summary:summary.slice(0,low).join("")};
  if(eventLength(compact)>limit) throw new RelayError("event_metadata_too_large","Event metadata exceeds the parent context budget");
  return compact;
}

/** One strongly consistent mailbox per verified account. No global task table. */
export class OwnerRelay extends DurableObject<Env> {
  private waiters = new Map<string, Set<() => void>>();
  private activeDownload?: ActiveDownload;
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`
        CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,label TEXT NOT NULL,credential_hash TEXT NOT NULL,
          expires INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,workspaces TEXT NOT NULL,readiness TEXT NOT NULL DEFAULT '{}',last_seen INTEGER NOT NULL DEFAULT 0,pairing_id TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS identity(owner_key TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,
          worker_id TEXT NOT NULL,workspace_id TEXT NOT NULL,source_id TEXT,operation TEXT NOT NULL,payload TEXT NOT NULL,task_type TEXT NOT NULL,
          status TEXT NOT NULL,terminal INTEGER NOT NULL DEFAULT 0,local_id TEXT,sequence INTEGER NOT NULL DEFAULT 0,worker_sequence INTEGER NOT NULL DEFAULT 0,model_pin TEXT,
          acknowledged INTEGER NOT NULL DEFAULT 0,result TEXT,created INTEGER NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,worker_id TEXT NOT NULL,
          operation TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,result TEXT);
        CREATE INDEX IF NOT EXISTS pending_commands ON commands(worker_id,state);
        CREATE TABLE IF NOT EXISTS events(job_id TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL,occurred INTEGER NOT NULL,source_key TEXT NOT NULL,source_fingerprint TEXT NOT NULL,
          PRIMARY KEY(job_id,sequence),UNIQUE(job_id,source_key));
        CREATE TABLE IF NOT EXISTS suppressed_events(job_id TEXT NOT NULL,worker_sequence INTEGER NOT NULL,fingerprint TEXT NOT NULL,PRIMARY KEY(job_id,worker_sequence));
        CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,url TEXT NOT NULL,
          secret TEXT NOT NULL,expires INTEGER NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
          generation TEXT NOT NULL,next_attempt INTEGER NOT NULL DEFAULT 0,verified INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS subscriptions_by_job ON subscriptions(job_id,state);
        CREATE TABLE IF NOT EXISTS artifacts(job_id TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(job_id,id));
        CREATE TABLE IF NOT EXISTS retired_requests(request_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,fingerprint TEXT NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS download_grants(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,artifact_id TEXT NOT NULL,expires INTEGER NOT NULL,state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_renewals(id TEXT PRIMARY KEY,worker_id TEXT NOT NULL,old_hash TEXT NOT NULL,new_hash TEXT NOT NULL,expires INTEGER NOT NULL,approved INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS computer_operations(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,worker_id TEXT NOT NULL,action TEXT NOT NULL,parameters TEXT NOT NULL,state TEXT NOT NULL,summary TEXT NOT NULL,confirmation TEXT NOT NULL,created INTEGER NOT NULL,updated INTEGER NOT NULL,expires INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0,completion_hash TEXT NOT NULL DEFAULT '',completion_secret TEXT NOT NULL DEFAULT '',completion_expires INTEGER NOT NULL DEFAULT 0,result_fingerprint TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS computer_messages(id TEXT PRIMARY KEY,worker_id TEXT NOT NULL,fingerprint TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS credential_rotations(id TEXT PRIMARY KEY,worker_id TEXT NOT NULL,old_hash TEXT NOT NULL,access_hash TEXT NOT NULL,refresh_hash TEXT NOT NULL,access_expires INTEGER NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS account_settings(id INTEGER PRIMARY KEY,preferred_workspace_id TEXT);
      `);
      if (!this.ctx.storage.sql.exec<{ name: string }>("PRAGMA table_info(workers)").toArray().some(row => row.name === "transport_capabilities"))
        this.ctx.storage.sql.exec("ALTER TABLE workers ADD COLUMN transport_capabilities TEXT NOT NULL DEFAULT '{}'");
      if (!this.ctx.storage.sql.exec<{ name: string }>("PRAGMA table_info(retired_requests)").toArray().some(row => row.name === "worker_id"))
        this.ctx.storage.sql.exec("ALTER TABLE retired_requests ADD COLUMN worker_id TEXT");
      if (!this.ctx.storage.sql.exec<{ name: string }>("PRAGMA table_info(jobs)").toArray().some(row => row.name === "display_summary"))
        this.ctx.storage.sql.exec("ALTER TABLE jobs ADD COLUMN display_summary TEXT NOT NULL DEFAULT ''");
      const columns=new Set(this.ctx.storage.sql.exec<{name:string}>("PRAGMA table_info(workers)").toArray().map(row=>row.name));
      for(const [name,definition] of Object.entries({approval_mode:"TEXT NOT NULL DEFAULT 'legacy_30_day'",access_expires:"INTEGER NOT NULL DEFAULT 0",refresh_hash:"TEXT NOT NULL DEFAULT ''",paused:"INTEGER NOT NULL DEFAULT 0",admission_revision:"INTEGER NOT NULL DEFAULT 0",computer_snapshot:"TEXT NOT NULL DEFAULT '{}'",setup_request_id:"TEXT NOT NULL DEFAULT ''",setup_managed:"INTEGER NOT NULL DEFAULT 0"}))
        if(!columns.has(name)) this.ctx.storage.sql.exec(`ALTER TABLE workers ADD COLUMN ${name} ${definition}`);
      if(!this.ctx.storage.sql.exec<{name:string}>("PRAGMA table_info(download_grants)").toArray().some(row=>row.name==="manifest"))
        this.ctx.storage.sql.exec("ALTER TABLE download_grants ADD COLUMN manifest TEXT NOT NULL DEFAULT ''");
      // A response pipe cannot survive object eviction. Never replay its bytes.
      this.ctx.storage.sql.exec("UPDATE download_grants SET state='interrupted' WHERE state='active'");
    });
  }

  private one<T extends Record<string, SqlStorageValue>>(query: string, ...args: SqlStorageValue[]): T | undefined {
    return this.ctx.storage.sql.exec<T>(query, ...args).toArray()[0];
  }
  private job(id: string): JobRow {
    const row = this.one<JobRow>("SELECT * FROM jobs WHERE id=?", ID.parse(id));
    if (!row || row.expires <= Date.now()) throw new RelayError("not_found", "Owned delegation not found", 404);
    return row;
  }
  private worker(id: string): WorkerRow {
    const row = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", ID.parse(id));
    if (!row || row.revoked || row.expires <= Date.now()) throw new RelayError("worker_unavailable", "Worker pairing expired or was revoked", 409);
    return row;
  }
  private connection(id: string): WebSocket | undefined {
    return this.ctx.getWebSockets(id).find(ws => ws.readyState === WebSocket.OPEN);
  }
  private online(id: string): boolean {
    const grant=this.one<WorkerRow>("SELECT * FROM workers WHERE id=?",id);
    if(!grant||grant.revoked||grant.expires<=Date.now()||(grant.access_expires||grant.expires)<=Date.now())return false;
    return Boolean(this.connection(id)) || (this.one<{ last_seen: number }>("SELECT last_seen FROM workers WHERE id=?", id)?.last_seen ?? 0) > Date.now() - 60_000;
  }
  private receipt(job: JobRow): ObjectValue {
    const cleanup=this.one<CommandRow>("SELECT * FROM commands WHERE job_id=? AND operation='cleanup' ORDER BY rowid DESC LIMIT 1",job.id);
    return { delegation_id: job.id, request_id: job.request_id, workspace_id: job.workspace_id,
      status: job.status, terminal: Boolean(job.terminal), delivery: "background", sequence: job.sequence,
      acknowledged_sequence: job.acknowledged, worker_online: this.online(job.worker_id),
      ...(job.status === "awaiting_setup" || (job.status === "queued" && !this.online(job.worker_id))
        ? { inference_started: false, setup_url: publicOrigin(this.env) + "/connect?request_id=" + encodeURIComponent(job.request_id) } : {}),
      ...(cleanup?{cleanup:{command_id:cleanup.id,status:cleanup.state,...(cleanup.result?{result:JSON.parse(cleanup.result)}:{})}}:{}) };
  }
  private wake(jobId: string): void {
    for (const resolve of this.waiters.get(jobId) ?? []) resolve();
    this.waiters.delete(jobId);
  }
  private async schedule(at: number): Promise<void> {
    const current = await this.ctx.storage.getAlarm();
    if (current === null || at < current) await this.ctx.storage.setAlarm(at);
  }
  private async wait(jobId: string, milliseconds = 25_000): Promise<void> {
    await new Promise<void>(resolve => {
      let timeout: ReturnType<typeof setTimeout>;
      const done = () => { clearTimeout(timeout); this.waiters.get(jobId)?.delete(done); resolve(); };
      const listeners = this.waiters.get(jobId) ?? new Set<() => void>();
      listeners.add(done);
      this.waiters.set(jobId, listeners);
      timeout = setTimeout(done, milliseconds);
    });
  }
  private queue(job: JobRow, operation: string, payload: ObjectValue): string {
    const id = operation === "delegate" || operation === "continue" ? job.id : token(16);
    this.ctx.storage.sql.exec("INSERT INTO commands(id,job_id,worker_id,operation,payload,state) VALUES(?,?,?,?,?,'queued')",
      id, job.id, job.worker_id, operation, canonical(payload));
    this.wake("worker:" + job.worker_id);
    this.dispatch(job.worker_id);
    return id;
  }
  private dispatch(workerId: string): void {
    const ws = this.connection(workerId);
    if (!ws) return;
    const worker=this.worker(workerId);
    if((worker.access_expires||worker.expires)<=Date.now())return;
    // One worker owns a command for its entire lifetime. A reconnect can replay
    // leased commands to that same worker, never assign them to another worker.
    const rows = this.pendingCommands(workerId);
    for (const row of rows) {
      this.ctx.storage.sql.exec("UPDATE commands SET state='leased' WHERE id=?", row.id);
      if (row.operation === "delegate" || row.operation === "continue") this.ctx.storage.sql.exec(
        "UPDATE jobs SET status='leased' WHERE id=? AND status='queued'", row.job_id);
      if (row.state === "queued") this.wakeUi(row.job_id);
      try { ws.send(JSON.stringify({ kind: "command", command_id: row.id, delegation_id: row.job_id,
        operation: row.operation, payload: JSON.parse(row.payload), allow_start: row.state === "queued",admission_revision:worker.admission_revision })); }
      catch { return; }
    }
  }

  async registerWorker(input: PairingInput, ownerKey: string, pairingId: string): Promise<{ worker_id: string }> {
    const parsed = pairingSchema.parse(input);
    const owner = this.one<{ owner_key: string }>("SELECT owner_key FROM identity");
    if (owner && owner.owner_key !== ownerKey) throw new RelayError("owner_conflict", "Worker cannot change account ownership", 409);
    this.ctx.storage.sql.exec("INSERT OR IGNORE INTO identity(owner_key) VALUES(?)", ownerKey);
    const previous = this.one<WorkerRow>("SELECT * FROM workers WHERE pairing_id=?", pairingId);
    if (previous) {
      if (previous.credential_hash !== parsed.credential_hash || previous.workspaces !== canonical(parsed.workspaces)) throw new RelayError("pairing_conflict", "Pairing identity was reused", 409);
      return { worker_id: previous.id };
    }
    if (new Set(parsed.workspaces.map(w => w.id)).size !== parsed.workspaces.length) throw new RelayError("duplicate_workspace", "Workspace aliases must be unique");
    const count = this.one<{ count: number }>("SELECT COUNT(*) AS count FROM workers WHERE revoked=0 AND expires>?", Date.now())?.count ?? 0;
    if (count >= 4) throw new RelayError("worker_limit", "Revoke an old worker before adding another", 429);
    const used = this.ctx.storage.sql.exec<{ workspaces: string }>("SELECT workspaces FROM workers WHERE revoked=0 AND expires>?", Date.now()).toArray();
    const aliases = new Set(used.flatMap(row => (JSON.parse(row.workspaces) as Workspace[]).map(w => w.id)));
    if (parsed.workspaces.some(w => aliases.has(w.id))) throw new RelayError("duplicate_workspace", "Workspace alias already belongs to another worker");
    const id = token(16);
    const persistent=parsed.approval_mode==="until_revoked",expires=persistent?PERMANENT_APPROVAL:Date.now()+30*86_400_000;
    this.ctx.storage.sql.exec("INSERT INTO workers(id,label,credential_hash,expires,workspaces,pairing_id,approval_mode,access_expires,refresh_hash,setup_request_id,setup_managed) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      id, parsed.label, parsed.credential_hash, expires, canonical(parsed.workspaces), pairingId,persistent?"until_revoked":"legacy_30_day",persistent?Date.now()+ACCESS_TTL:expires,persistent?parsed.credential_hash:"",parsed.setup_request_id??"",parsed.setup_only?1:0);
    await this.schedule(persistent?Date.now()+ACCESS_TTL:expires);
    this.wakeUi();
    return { worker_id: id };
  }

  async revokeWorker(workerId: string): Promise<ObjectValue> {
    const worker = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", ID.parse(workerId));
    if (!worker) throw new RelayError("not_found", "Owned worker not found", 404);
    if (worker.revoked) return { worker_id: workerId, revoked: true, running_tasks: "inspect locally; revocation does not prove provider termination" };
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec("UPDATE workers SET revoked=1,credential_hash='',refresh_hash='',paused=1,admission_revision=admission_revision+1,workspaces='[]',readiness='{}' WHERE id=?", workerId);
      this.ctx.storage.sql.exec("DELETE FROM credential_rotations WHERE worker_id=?",workerId);
      this.ctx.storage.sql.exec("UPDATE computer_operations SET state='revoked',summary='Computer access was revoked. This pending change was not confirmed applied.',parameters='{}',updated=? WHERE worker_id=? AND state IN ('prepared','pending')",Date.now(),workerId);
      this.ctx.storage.sql.exec("UPDATE subscriptions SET state='revoked',secret='' WHERE job_id IN (SELECT id FROM jobs WHERE worker_id=?)", workerId);
      this.ctx.storage.sql.exec("UPDATE jobs SET status='unknown' WHERE worker_id=? AND terminal=0", workerId);
    });
    for (const ws of this.ctx.getWebSockets(workerId)) ws.close(4003, "Worker pairing revoked; inspect running tasks locally");
    if (this.activeDownload?.worker_id === workerId) this.finishDownload(new RelayError("worker_unavailable", "Worker access was revoked", 409));
    for (const job of this.ctx.storage.sql.exec<{ id: string }>("SELECT id FROM jobs WHERE worker_id=?", workerId).toArray()) this.wakeUi(job.id);
    this.wakeUi();
    return { worker_id: workerId, revoked: true, running_tasks: "inspect locally; revocation does not prove provider termination" };
  }

  async workspaces(): Promise<ObjectValue> {
    const rows = this.ctx.storage.sql.exec<WorkerRow>("SELECT * FROM workers WHERE revoked=0 AND expires>?", Date.now()).toArray();
    return { workspaces: rows.flatMap(row => (JSON.parse(row.workspaces) as Workspace[]).map(w => ({
      ...w, worker_id: row.id, worker_label: row.label, online: this.online(row.id)
    }))) };
  }

  async workerGrant(workerId:string):Promise<ObjectValue> {
    const worker=this.worker(workerId);
    return {approval_mode:worker.approval_mode,access_expires_at:worker.access_expires||worker.expires};
  }
  async rotateWorkerCredentials(workerId:string,secret:string,raw:unknown):Promise<ObjectValue> {
    const input=z.object({rotation_id:ID,credential_hash:z.string().regex(/^[a-f0-9]{64}$/),refresh_hash:z.string().regex(/^[a-f0-9]{64}$/)}).strict().parse(raw);
    const proof=await digest(secret),worker=this.worker(workerId),now=Date.now();
    if(worker.approval_mode!=="until_revoked") throw new RelayError("rotation_denied","This computer needs explicit ongoing approval first",403);
    const previous=this.one<{worker_id:string;old_hash:string;access_hash:string;refresh_hash:string;access_expires:number;expires:number}>("SELECT * FROM credential_rotations WHERE id=?",input.rotation_id);
    if(previous) {
      if(previous.worker_id!==workerId||previous.expires<=now||!equal(previous.old_hash,proof)||previous.access_hash!==input.credential_hash||previous.refresh_hash!==input.refresh_hash||worker.credential_hash!==previous.access_hash||worker.refresh_hash!==previous.refresh_hash)
        throw new RelayError("rotation_conflict","Credential rotation identity was reused or superseded",409);
      return {rotation_id:input.rotation_id,owner_key:this.one<{owner_key:string}>("SELECT owner_key FROM identity")!.owner_key,worker_id:workerId,expires_at:previous.access_expires,approval_mode:"until_revoked"};
    }
    if(!worker.refresh_hash||!equal(worker.refresh_hash,proof)||input.credential_hash===input.refresh_hash||[input.credential_hash,input.refresh_hash].includes(worker.refresh_hash)) throw new RelayError("rotation_denied","Refresh proof is invalid",403);
    const expires=now+ACCESS_TTL;
    this.ctx.storage.transactionSync(()=>{
      this.ctx.storage.sql.exec("INSERT INTO credential_rotations(id,worker_id,old_hash,access_hash,refresh_hash,access_expires,expires) VALUES(?,?,?,?,?,?,?)",input.rotation_id,workerId,proof,input.credential_hash,input.refresh_hash,expires,now+600_000);
      this.ctx.storage.sql.exec("UPDATE workers SET credential_hash=?,refresh_hash=?,access_expires=? WHERE id=?",input.credential_hash,input.refresh_hash,expires,workerId);
    });
    for(const ws of this.ctx.getWebSockets(workerId)) ws.close(4001,"Computer credentials rotated");
    await this.schedule(now+600_000);
    return {rotation_id:input.rotation_id,owner_key:this.one<{owner_key:string}>("SELECT owner_key FROM identity")!.owner_key,worker_id:workerId,expires_at:expires,approval_mode:"until_revoked"};
  }

  private operationReceipt(row:OperationRow,includeConfirmation=false):UiOperation {
    const deliveryHint=row.action==="uninstall"&&row.state==="pending"&&row.completion_expires>Date.now()?` If removal finished offline, run the retained verified removal helper with --retry-removal on that computer before ${new Date(row.completion_expires).toISOString()} to resend confirmation.`:"";
    return {operation_id:row.id,...(row.worker_id?{computer_id:row.worker_id}:{}),action:row.action,state:row.state as UiOperation["state"],summary:row.summary+deliveryHint,updated_at:new Date(row.updated).toISOString(),...(includeConfirmation&&row.state==="prepared"?{confirmation_token:row.confirmation,expires_at:new Date(row.expires).toISOString()}:{})};
  }
  private expireRemovalOperations(now=Date.now()):void {
    for(const row of this.ctx.storage.sql.exec<OperationRow>("SELECT * FROM computer_operations WHERE action='uninstall' AND state='pending' AND completion_expires>0 AND completion_expires<=?",now).toArray()){
      this.ctx.storage.sql.exec("UPDATE computer_operations SET state='unconfirmed',summary='Removal confirmation expired. The companion may have been removed locally; its completion was not confirmed. Check that computer. Revoking access is a separate action.',parameters='{}',completion_secret='',updated=? WHERE id=? AND state='pending'",now,row.id);
      const worker=this.one<WorkerRow>("SELECT * FROM workers WHERE id=?",row.worker_id);
      if(worker)this.ctx.storage.sql.exec("UPDATE workers SET computer_snapshot=? WHERE id=?",canonical({...object(JSON.parse(worker.computer_snapshot)),uninstall_status:"unconfirmed"}),row.worker_id);
      this.wakeUi();
    }
  }
  async inspectOperation(id:string):Promise<ObjectValue> {
    this.expireRemovalOperations();
    const row=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",ID.parse(id));
    if(!row) throw new RelayError("not_found","Owned operation not found",404);
    return {...this.operationReceipt(row)};
  }
  private computerActions(worker:WorkerRow):string[] {
    if(worker.revoked)return [];
    const actions=["rename","revoke"],caps=object(JSON.parse(worker.transport_capabilities));
    if(worker.expires>Date.now()&&caps.computer_control_v1===true) {
      actions.push(worker.paused?"resume":"pause","stop_tasks");
      if(computerSnapshotSchema.parse(JSON.parse(worker.computer_snapshot)).installed===true)
        actions.push("set_startup","set_automatic_updates","uninstall");
      if(worker.approval_mode==="legacy_30_day"&&this.online(worker.id)) actions.push("keep_approved");
    }
    if(worker.expires>Date.now()&&caps.setup_control_v1===true)actions.push("check_accounts","install_provider","sign_in","configure_workspace");
    if(this.one("SELECT id FROM computer_operations WHERE worker_id=? AND action='uninstall' AND state='pending'",worker.id))return ["revoke"];
    return actions;
  }
  async controlSnapshot():Promise<UiControlSnapshot> {
    this.expireRemovalOperations();
    const workers=this.ctx.storage.sql.exec<WorkerRow>("SELECT * FROM workers ORDER BY rowid").toArray();
    const computers=workers.map(row=>{
      const {paused:_reportedPause,setup_only:_reportedSetup,...snapshot}=computerSnapshotSchema.parse(JSON.parse(row.computer_snapshot)),capabilities=object(JSON.parse(row.transport_capabilities));
      const counts=this.one<{active:number;pending:number}>("SELECT SUM(CASE WHEN terminal=0 AND status NOT IN ('queued','awaiting_setup') THEN 1 ELSE 0 END) AS active,SUM(CASE WHEN terminal=0 AND status IN ('queued','awaiting_setup') THEN 1 ELSE 0 END) AS pending FROM jobs WHERE worker_id=? AND expires>?",row.id,Date.now());
      return {id:row.id,label:row.label,state:(row.revoked?"revoked":row.expires<=Date.now()?"expired":row.paused?"paused":this.online(row.id)?"online":"offline") as UiControlSnapshot["computers"][number]["state"],paused:Boolean(row.paused),approval_mode:(row.approval_mode==="until_revoked"?"until_revoked":"legacy_30_day") as "until_revoked"|"legacy_30_day",setup_only:JSON.parse(row.workspaces).length===0,...(row.approval_mode!=="until_revoked"?{expires_at:new Date(row.expires).toISOString()}:{}),...snapshot,capabilities:Object.keys(capabilities).filter(key=>capabilities[key]===true),allowed_actions:this.computerActions(row),active_task_count:counts?.active??0,pending_task_count:counts?.pending??0,workspaces:(JSON.parse(row.workspaces) as Workspace[]).map(w=>({id:w.id,label:w.label}))};
    });
    const tasks=this.ctx.storage.sql.exec<JobRow>("SELECT * FROM jobs WHERE expires>? ORDER BY created DESC LIMIT 50",Date.now()).toArray().map(row=>({delegation_id:row.id,task_summary:row.display_summary||"Saved task",status:row.status,terminal:Boolean(row.terminal),computer_id:row.worker_id,workspace_label:workers.flatMap(w=>JSON.parse(w.workspaces) as Workspace[]).find(w=>w.id===row.workspace_id)?.label,updated_at:new Date(row.created).toISOString()}));
    const preferred=this.one<{preferred_workspace_id:string}>("SELECT preferred_workspace_id FROM account_settings WHERE id=1")?.preferred_workspace_id;
    const snapshot:UiControlSnapshot={version:1,revision:"",updated_at:new Date().toISOString(),permissions:{can_manage:false,can_delete_account:false,can_write_tasks:false},computers,tasks,settings:{setup_url:publicOrigin(this.env)+"/connect",...(preferred?{preferred_workspace_id:preferred}:{}),...companionSetupSettings(this.env)},receipts:this.ctx.storage.sql.exec<OperationRow>("SELECT * FROM computer_operations ORDER BY updated DESC LIMIT 20").toArray().map(row=>this.operationReceipt(row)),account:{export_available:true,deletion_requires_confirmation:true}};
    snapshot.revision=await digest(canonical({...snapshot,revision:"",updated_at:""}));
    return snapshot;
  }
  private async watchComputers(after:string|undefined,waitMs:number):Promise<UiControlSnapshot> {
    const initial=await this.controlSnapshot();
    if(!after||after!==initial.revision||!waitMs)return initial;
    const waiting=this.wait("ui:computers",20_000),current=await this.controlSnapshot();
    if(current.revision!==after)this.wake("ui:computers");
    await waiting;return this.controlSnapshot();
  }
  private async prepareOperation(input:ObjectValue):Promise<ObjectValue> {
    const fingerprint=await digest(canonical(input));
    const previous=this.one<OperationRow>("SELECT * FROM computer_operations WHERE request_id=?",String(input.request_id));
    if(previous){if(previous.fingerprint!==fingerprint)throw new RelayError("operation_conflict","Request identity already names another change",409);return {...this.operationReceipt(previous,true)};}
    const action=String(input.action),parameters=object(input.parameters),computerId=String(input.computer_id??"");
    const accountAction=["delete_account","set_preferred_workspace"].includes(action);
    if(accountAction&&computerId||!accountAction&&!computerId)throw new RelayError("computer_required","Choose exactly the computer needed for this change");
    const parameterSchemas:Record<string,z.ZodType>={rename:z.object({label:z.string().trim().min(1).max(80)}).strict(),set_startup:z.object({enabled:z.boolean()}).strict(),set_automatic_updates:z.object({enabled:z.boolean()}).strict(),set_preferred_workspace:z.object({workspace_id:ID}).strict(),uninstall:z.object({erase_history:z.literal(false).optional()}).strict(),install_provider:z.object({provider:z.enum(["codex","agy","git"])}).strict(),sign_in:z.object({provider:z.enum(["codex","agy"])}).strict(),configure_workspace:z.object({path:z.string().trim().min(1).max(1024),alias:ID,isolation:z.enum(["worktree","shared"])}).strict()};
    if(!(parameterSchemas[action]??z.object({}).strict()).safeParse(parameters).success)throw new RelayError("invalid_parameters","Parameters do not match the selected action");
    let worker:WorkerRow|undefined;
    if(computerId){worker=["rename","revoke"].includes(action)?this.one<WorkerRow>("SELECT * FROM workers WHERE id=? AND revoked=0",computerId):this.worker(computerId);if(!worker||!this.computerActions(worker).includes(action))throw new RelayError("control_unavailable","This computer does not support that action in its current state",409);}
    if(action==="configure_workspace"&&this.ctx.storage.sql.exec<WorkerRow>("SELECT * FROM workers WHERE revoked=0 AND expires>?",Date.now()).toArray().some(row=>(JSON.parse(row.workspaces) as Workspace[]).some(w=>w.id===parameters.alias)))throw new RelayError("duplicate_workspace","Choose a new alias; an approved folder cannot be silently replaced",409);
    if(action==="set_preferred_workspace"&&!(object(await this.workspaces()).workspaces as ObjectValue[]).some(w=>w.id===parameters.workspace_id))throw new RelayError("not_found","Choose an approved workspace",404);
    const summaries:Record<string,string>={pause:"Pause new tasks. This computer stays connected; work already running continues.",resume:"Allow new tasks on this computer again.",stop_tasks:"Pause new tasks and request cancellation of all unfinished tasks. Completion is confirmed by the computer; existing files remain.",rename:`Rename this computer to ${String(parameters.label??"")}.`,set_startup:`${parameters.enabled?"Enable":"Disable"} companion startup when you sign in.`,set_automatic_updates:`${parameters.enabled?"Enable verified automatic updates when idle":"Disable automatic updates"}.`,uninstall:"Stop accepting new tasks, let work already running and its delivery finish, then uninstall the companion when the computer is reachable. Approved task files and local history remain. Offline removal stays pending until confirmed or its completion capability expires.",revoke:"Revoke this computer's access immediately. This does not prove running work stopped or uninstall software.",keep_approved:"Keep this same computer approved until you revoke it. Short-lived device credentials rotate; existing tasks and workspaces stay the same.",set_preferred_workspace:"Use this approved workspace by default for future task setup.",delete_account:"Delete retained relay records and revoke all application authorizations. This does not cancel running provider work, uninstall the companion, or delete local task files.",check_accounts:"Check installed tools and provider sign-in on this computer. No model task will run.",install_provider:`Install ${String(parameters.provider)} on this computer using the companion's supported installer.`,sign_in:`Start ${String(parameters.provider)} sign-in on your computer. Finish the provider's browser sign-in there; credentials stay on that computer.`,configure_workspace:`Approve folder ${String(parameters.path)} as workspace ${String(parameters.alias)} with ${String(parameters.isolation)} isolation. This allows delegated commands and file changes in that folder.`};
    if((this.one<{count:number}>("SELECT COUNT(*) AS count FROM computer_operations")?.count??0)>=256)throw new RelayError("operation_limit","Operation history is full; wait for old receipts to expire",429);
    const id=token(16),now=Date.now(),confirmation=token();
    this.ctx.storage.sql.exec("INSERT INTO computer_operations(id,request_id,fingerprint,worker_id,action,parameters,state,summary,confirmation,created,updated,expires) VALUES(?,?,?,?,?,?,'prepared',?,?,?,?,?)",id,String(input.request_id),fingerprint,computerId,action,canonical(parameters),summaries[action],confirmation,now,now,now+600_000);
    await this.schedule(now+600_000);this.wakeUi();
    return {...this.operationReceipt(this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",id)!,true)};
  }
  private async confirmOperation(id:string,confirmation:string):Promise<ObjectValue> {
    let operation=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",id);
    if(!operation||!equal(operation.confirmation,confirmation))throw new RelayError("confirmation_denied","Prepared change and confirmation do not match",403);
    if(operation.state!=="prepared")return {...this.operationReceipt(operation)};
    if(operation.expires<=Date.now())throw new RelayError("confirmation_expired","This confirmation expired; prepare the change again",410);
    const parameters=object(JSON.parse(operation.parameters)),worker=operation.worker_id?(["rename","revoke"].includes(operation.action)?this.one<WorkerRow>("SELECT * FROM workers WHERE id=? AND revoked=0",operation.worker_id):this.worker(operation.worker_id)):undefined;
    if(operation.worker_id&&!worker)throw new RelayError("control_unavailable","Computer access changed; inspect the current connection",409);
    if(worker&&!this.computerActions(worker).includes(operation.action))throw new RelayError("control_unavailable","Computer authority or capabilities changed; inspect it before continuing",409);
    if(operation.action==="configure_workspace"&&this.one("SELECT id FROM computer_operations WHERE action='configure_workspace' AND state='pending' AND json_extract(parameters,'$.alias')=? AND id!=?",String(parameters.alias),id))throw new RelayError("workspace_pending","Finish the existing folder approval for this alias first",409);
    if(operation.action==="delete_account")return {...this.operationReceipt(operation),account_delete_confirmed:true};
    if(operation.action==="set_preferred_workspace"){
      if(!(object(await this.workspaces()).workspaces as ObjectValue[]).some(w=>w.id===parameters.workspace_id))throw new RelayError("not_found","Preferred workspace is no longer approved",404);
      this.ctx.storage.sql.exec("INSERT INTO account_settings(id,preferred_workspace_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET preferred_workspace_id=excluded.preferred_workspace_id",String(parameters.workspace_id));
    } else if(operation.action==="rename")this.ctx.storage.sql.exec("UPDATE workers SET label=? WHERE id=?",String(parameters.label),worker!.id);
    else if(operation.action==="revoke")await this.revokeWorker(worker!.id);
    else {
      this.ctx.storage.transactionSync(()=>{
        if(["pause","resume","stop_tasks","uninstall"].includes(operation!.action))this.ctx.storage.sql.exec("UPDATE workers SET paused=?,admission_revision=admission_revision+1 WHERE id=?",operation!.action==="resume"?0:1,worker!.id);
        if(operation!.action==="keep_approved")this.ctx.storage.sql.exec("UPDATE workers SET approval_mode='until_revoked',expires=?,refresh_hash=credential_hash,access_expires=? WHERE id=?",PERMANENT_APPROVAL,Date.now()+ACCESS_TTL,worker!.id);
        const revision=this.one<WorkerRow>("SELECT * FROM workers WHERE id=?",worker!.id)!.admission_revision;
        this.ctx.storage.sql.exec("UPDATE computer_operations SET state='pending',revision=?,updated=? WHERE id=?",revision,Date.now(),id);
      });
      if(["stop_tasks","uninstall"].includes(operation.action))for(const job of this.ctx.storage.sql.exec<JobRow>("SELECT * FROM jobs WHERE worker_id=? AND status='queued' AND terminal=0 AND local_id IS NULL",worker!.id).toArray()){
        this.ctx.storage.sql.exec("UPDATE commands SET state='cancelled' WHERE job_id=? AND state='queued' AND operation IN ('delegate','continue')",job.id);
        await this.acceptEvent(job,{sequence:1,kind:"cancelled",terminal:true,summary:"Cancelled before worker launch by the computer stop request. No provider inference started.",task_type:this.taskType(job),verification_required:false},{status:"cancelled",inference_started:false},"computer-stop-before-launch");
      }
      this.wake("worker:"+worker!.id);await this.dispatchControls(worker!.id);this.dispatch(worker!.id);this.wakeUi();
      return {...this.operationReceipt(this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",id)!)};
    }
    this.ctx.storage.sql.exec("UPDATE computer_operations SET state='completed',summary=?,parameters='{}',updated=? WHERE id=?",operation.action==="revoke"?"Computer access revoked. Running provider work may continue; no uninstall was confirmed.":operation.summary,Date.now(),id);this.wakeUi();
    return {...this.operationReceipt(this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",id)!)};
  }
  private async controlPackets(workerId:string):Promise<ObjectValue[]> {
    this.expireRemovalOperations();
    const rows=this.ctx.storage.sql.exec<OperationRow>("SELECT * FROM computer_operations WHERE worker_id=? AND state='pending' ORDER BY rowid LIMIT 16",workerId).toArray(),packets:ObjectValue[]=[];
    for(const row of rows){
      let completion:ObjectValue|undefined;
      if(row.action==="uninstall"){
        if(!row.completion_hash){const secret=token(),expires=Date.now()+7*86_400_000;this.ctx.storage.sql.exec("UPDATE computer_operations SET completion_hash=?,completion_secret=?,completion_expires=? WHERE id=? AND completion_hash=''",await digest(secret),await seal({token:secret},this.env.SESSION_SECRET),expires,row.id);await this.schedule(expires);}
        const current=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",row.id)!;
        if(current.completion_expires>Date.now())completion={token:(await unseal(current.completion_secret,this.env.SESSION_SECRET)).token,expires_at:current.completion_expires};
        else continue;
      }
      packets.push({kind:"computer_control",operation_id:row.id,action:row.action,parameters:JSON.parse(row.parameters),revision:row.revision,...(completion?{completion}:{} )});
    }return packets;
  }
  private async dispatchControls(workerId:string):Promise<void>{const ws=this.connection(workerId);if(!ws)return;for(const packet of await this.controlPackets(workerId)){try{ws.send(JSON.stringify(packet));}catch{return;}}}
  async completeRemoval(operationId:string,raw:unknown):Promise<ObjectValue>{
    this.expireRemovalOperations();
    const input=z.object({token:z.string().regex(/^[a-f0-9]{64}$/),state:z.enum(["succeeded","failed"]),summary:z.string().max(600)}).strict().parse(raw);
    let row=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",ID.parse(operationId));
    if(!row||row.action!=="uninstall"||!row.completion_hash||row.completion_expires<=Date.now()||!equal(row.completion_hash,await digest(input.token)))throw new RelayError("removal_denied","Removal completion proof is invalid or expired",403);
    const fingerprint=await digest(canonical({state:input.state,summary:input.summary}));
    row=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=?",ID.parse(operationId))!;
    if(!["pending","revoked"].includes(row.state)){if(row.result_fingerprint!==fingerprint)throw new RelayError("operation_conflict","Removal already has a different final receipt",409);return {acknowledged:true,state:row.state};}
    this.ctx.storage.sql.exec("UPDATE computer_operations SET state=?,summary=?,result_fingerprint=?,updated=?,completion_secret='',parameters='{}' WHERE id=?",input.state==="succeeded"?"completed":"failed",input.summary,fingerprint,Date.now(),row.id);
    if(input.state==="succeeded")await this.revokeWorker(row.worker_id);
    this.wakeUi();return {acknowledged:true,state:input.state==="succeeded"?"completed":"failed"};
  }

  private wakeUi(jobId?: string): void {
    if (jobId) this.wake("ui:" + jobId);
    this.wake("ui:connection");
    this.wake("ui:computers");
  }
  private connectionState(worker?: WorkerRow): UiSnapshot["connection"] {
    const setup_url = publicOrigin(this.env) + "/connect";
    if (!worker) return { state: "setup_required", message: "Connect your computer and approve a workspace.", setup_url: publicOrigin(this.env) + "/connect" };
    if (worker.revoked) return { state: "revoked", message: "Worker access was revoked. Inspect existing work on your computer.", setup_url };
    if (worker.expires <= Date.now()) return { state: "expired", message: "Renew this computer's access. Saved work is preserved.", setup_url };
    return this.online(worker.id) ? { state: "online", message: "Your paired computer is connected.", setup_url }
      : { state: "offline", message: "Start the companion on your paired computer. Keep the original task receipt.", setup_url };
  }
  private canReadSavedJob(job: JobRow): void {
    const worker = job.worker_id ? this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", job.worker_id) : undefined;
    if (job.terminal && worker && !worker.revoked) return;
    this.checkJobWorker(job);
  }
  private artifactRefs(job: JobRow): UiArtifact[] {
    if (!job.result) return [];
    const handoff = object(JSON.parse(job.result));
    if (!Array.isArray(handoff.artifact_refs)) return [];
    return handoff.artifact_refs.slice(0, 20).filter(v => v && typeof v === "object" && !Array.isArray(v)).map(raw => {
      const ref = object(raw), id = ID.safeParse(ref.artifact_id), size = ref.bytes;
      const artifact: UiArtifact = { name: String(ref.file_name ?? ref.path ?? "Output").slice(0, 240),
        ...(id.success ? { artifact_id: id.data } : {}),
        ...(typeof size === "number" && Number.isSafeInteger(size) && size >= 0 ? { bytes: size } : {}),
        ...(typeof ref.mime_type === "string" ? { mime_type: ref.mime_type.slice(0, 120) } : {}),
        ...(typeof ref.sha256 === "string" && /^[a-f0-9]{64}$/.test(ref.sha256) ? { sha256: ref.sha256 } : {}),
        export_available: ref.export_available === true, download_available: ref.download_available === true,
        ...(typeof ref.reason === "string" ? { reason: ref.reason.slice(0, 240) } : {}) };
      if (id.success && (artifact.download_available || artifact.export_available) && typeof size === "number" && size <= MAX_DOWNLOAD_BYTES)
        artifact.download_origin = publicOrigin(this.env);
      return artifact;
    });
  }
  private async hasAuthorizedSubscription(jobId: string): Promise<boolean> {
    const candidates = this.ctx.storage.sql.exec<SubscriptionRow>("SELECT * FROM subscriptions WHERE job_id=? AND state='active' AND expires>? AND verified>0 ORDER BY expires DESC LIMIT 4", jobId, Date.now()).toArray();
    for (const candidate of candidates) {
      try {
        const secrets = await unseal(candidate.secret, this.env.SESSION_SECRET);
        await this.subscriptionAuthorization(String(secrets.token));
        const current = this.one<SubscriptionRow>("SELECT * FROM subscriptions WHERE id=?", candidate.id);
        if (current?.state === "active" && current.generation === candidate.generation && current.expires > Date.now() && current.verified > 0) return true;
      } catch { /* A stored subscription is not evidence of current authorization. */ }
    }
    return false;
  }
  async uiSnapshot(delegationId?: string): Promise<UiSnapshot> {
    let snapshot: Omit<UiSnapshot, "revision">;
    if (!delegationId) {
      const workers = this.ctx.storage.sql.exec<WorkerRow>("SELECT * FROM workers ORDER BY revoked,expires DESC LIMIT 4").toArray();
      const worker = workers.find(w => !w.revoked && w.expires > Date.now() && this.online(w.id)) ?? workers.find(w => !w.revoked && w.expires > Date.now()) ?? workers[0];
      const connection = this.connectionState(worker);
      snapshot = { version: 1, status: connection.state, terminal: false, summary: connection.message ?? "Check your worker connection.",
        updated_at: new Date(worker?.last_seen || 0).toISOString(), connection,
        notifications: { active: false, message: "Task notifications are available only after an authorized host subscription is connected." } };
    } else {
      this.job(delegationId);
      const notificationsActive = await this.hasAuthorizedSubscription(delegationId);
      // Re-read owned data after asynchronous OAuth checks; revocation or task
      // retention may have changed while a subscription was being verified.
      const job = this.job(delegationId);
      // UI may describe an expired/revoked connection, but never exposes its result.
      const worker = job.worker_id ? this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", job.worker_id) : undefined;
      const connection: UiSnapshot["connection"] = job.worker_id && !worker
        ? { state: "unknown", message: "This worker identity is no longer recoverable through the relay. Inspect saved work locally.", setup_url: publicOrigin(this.env) + "/connect" }
        : this.connectionState(worker);
      const allowedResult = !job.worker_id || Boolean(worker && !worker.revoked);
      const latest = this.one<{ payload: string; occurred: number }>("SELECT payload,occurred FROM events WHERE job_id=? ORDER BY sequence DESC LIMIT 1", job.id);
      const event = latest ? object(JSON.parse(latest.payload)) : null;
      const handoff = job.terminal && job.result && allowedResult ? object(JSON.parse(job.result)) : null;
      const selection = handoff?.model_selection && typeof handoff.model_selection === "object" && !Array.isArray(handoff.model_selection) ? object(handoff.model_selection) : {};
      const offered = selection.offered_fallback && typeof selection.offered_fallback === "object" && !Array.isArray(selection.offered_fallback) ? object(selection.offered_fallback) : {};
      const argonUnavailable = handoff?.status === "model_unavailable" && selection.preferred_profile === "argon-high" && selection.available === false;
      const semantic = handoff && typeof (handoff.implementation ?? handoff.result) === "object" && (handoff.implementation ?? handoff.result) !== null
        ? object(handoff.implementation ?? handoff.result) : {};
      const payload = object(JSON.parse(job.payload));
      const ws = worker ? (JSON.parse(worker.workspaces) as Workspace[]).find(w => w.id === job.workspace_id) : undefined;
      const statusText: Record<string, string> = { awaiting_setup: "Your task is saved. Connect your computer and approve a workspace to begin.", queued: "Your task is queued on its approved worker.",
        leased: "Your worker has received the saved task.", running: "Your saved task is running on your worker.", cancelling: "Cancellation requested. Waiting for confirmation.", unknown: "Worker recovery is required. Keep the original receipt; no new task was started." };
      const setupUrl = this.receipt(job).setup_url;
      const checks = semantic.checks_performed ?? semantic.tests_run ?? handoff?.verification;
      const checkList = Array.isArray(checks) ? checks.filter(v => typeof v === "string").slice(0, 20).map(v => String(v).slice(0, 240)) : [];
      snapshot = { version: 1, delegation_id: job.id,request_id:job.request_id, status: worker?.revoked ? "access_revoked" : argonUnavailable ? "model_unavailable" : job.status, terminal: Boolean(job.terminal),
        summary: worker?.revoked ? connection.message! : String(allowedResult && event?.summary || statusText[job.status] || "Inspect the saved task on your computer.").slice(0, 1_600),
        updated_at: new Date(latest?.occurred ?? job.created).toISOString(), connection,
        workspace_label: ws?.label ?? job.workspace_id,
        ...(job.display_summary || typeof payload.task === "string" ? { task_summary: job.display_summary || String(payload.task).slice(0, 240) } : {}),
        ...(job.model_pin ? { selected_model: job.model_pin } : {}),
        ...(typeof setupUrl === "string" ? { setup_url: setupUrl } : {}),
        notifications: { active: notificationsActive, message: notificationsActive ? "An authorized notification subscription exists for this task." : "Background delivery is not confirmed. This card can refresh while open." },
        ...(argonUnavailable ? { decision: { summary: String(selection.message ?? handoff?.message ?? "Gemini 4 Argon High is unavailable. Choose how to proceed.").slice(0, 600), opus_available: offered.profile === "opus-5.5" && offered.available === true } }
          : allowedResult && event && ["needs_decision", "decision"].includes(String(event.kind)) && !job.terminal ? { decision: { summary: String(event.summary).slice(0, 600) } } : {}),
        ...(handoff ? { outcome: String(semantic.summary ?? handoff.message ?? event?.summary ?? job.status).slice(0, 1_600), worker_checks: checkList, artifacts: this.artifactRefs(job) } : {}) };
    }
    // Timestamps are presentation, not a reason for a component refresh.
    const { updated_at: _updated, ...revisionData } = snapshot;
    return { ...snapshot, revision: await digest(canonical(revisionData)) };
  }
  async watchUi(delegationId: string | undefined, afterRevision?: string, waitMs = 20_000): Promise<UiSnapshot> {
    const first = await this.uiSnapshot(delegationId);
    if (!afterRevision || first.revision !== afterRevision || waitMs === 0) return first;
    const key = delegationId ? "ui:" + delegationId : "ui:connection";
    const waiting = this.wait(key, 20_000);
    // Install the waiter before rechecking to close the snapshot/subscribe race.
    const current = await this.uiSnapshot(delegationId);
    if (current.revision !== afterRevision) this.wake(key);
    await waiting;
    return this.uiSnapshot(delegationId);
  }

  async downloadDetailsByArtifact(jobId: string, artifactId: string): Promise<ArtifactManifest> {
    const job = this.job(jobId);
    this.canReadSavedJob(job);
    if (!job.terminal) throw new RelayError("not_completed", "Downloads require a completed owned task", 409);
    const ref = this.artifactRefs(job).find(item => item.artifact_id === ID.parse(artifactId));
    if (!ref || !ref.sha256 || ref.bytes === undefined) throw new RelayError("not_found", "Published owned artifact not found", 404);
    const worker = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", job.worker_id);
    const capabilities = worker ? object(JSON.parse(worker.transport_capabilities)) : {};
    const validWorker = Boolean(worker && !worker.revoked && worker.expires > Date.now());
    const streamAvailable = validWorker && Boolean(worker && this.online(worker.id)&&this.connection(worker.id)) && capabilities.artifact_stream_v1 === true && ref.download_available === true;
    return { delegation_id: job.id, artifact_id: artifactId, file_name: ref.name.split(/[\\/]/).pop()!.replace(/[\r\n\x00-\x1f]/g, "_").slice(0, 180) || "output",
      mime_type: ref.mime_type && /^[a-zA-Z0-9.+-]+\/[a-zA-Z0-9.+-]+$/.test(ref.mime_type) ? ref.mime_type : "application/octet-stream",
      size: ref.bytes, sha256: ref.sha256, stream_available: streamAvailable,
      eligible: ref.bytes <= MAX_DOWNLOAD_BYTES && ref.bytes >= 0 && (streamAvailable || Boolean(validWorker && worker && this.online(worker.id) && ref.export_available === true && ref.bytes <= 12_000)) };
  }
  async createDownloadRequest(jobId: string, artifactId: string): Promise<{ download_token: string; expires_at: number }> {
    const manifest = await this.downloadDetailsByArtifact(jobId, artifactId);
    if (!manifest.eligible) throw new RelayError("download_unavailable", "Connect a compatible paired worker to download this file", 409);
    if ((this.one<{ count: number }>("SELECT COUNT(*) AS count FROM download_grants WHERE expires>?", Date.now())?.count ?? 0) >= 32)
      throw new RelayError("download_limit", "Too many recent download requests; wait before trying again", 429);
    const id = token(24), expires = Date.now() + 300_000;
    this.ctx.storage.sql.exec("INSERT INTO download_grants(id,job_id,artifact_id,expires,state,manifest) VALUES(?,?,?,?,'pending',?)", id, jobId, artifactId, expires,canonical({sha256:manifest.sha256,size:manifest.size}));
    await this.schedule(expires);
    return { download_token: id, expires_at: expires };
  }
  async downloadDetails(downloadToken: string): Promise<ArtifactManifest> {
    const grant = this.one<DownloadRow&{manifest:string}>("SELECT * FROM download_grants WHERE id=?", ID.parse(downloadToken));
    if (!grant || grant.expires <= Date.now() || grant.state !== "pending") throw new RelayError("download_expired", "Download approval expired or was already used", 410);
    const manifest=await this.downloadDetailsByArtifact(grant.job_id, grant.artifact_id);
    if(grant.manifest&&grant.manifest!==canonical({sha256:manifest.sha256,size:manifest.size}))throw new RelayError("artifact_changed","Published artifact changed after this download was approved",409);
    return manifest;
  }
  private async artifactDownloadGrant(jobId:string,artifactId:string):Promise<ObjectValue>{
    const details=await this.downloadDetailsByArtifact(jobId,artifactId),grant=await this.createDownloadRequest(jobId,artifactId);
    const owner=this.one<{owner_key:string}>("SELECT owner_key FROM identity")!.owner_key,expires=Math.min(grant.expires_at,Date.now()+120_000);
    const capability=await sealArtifactDownload({purpose:"artifact_download",owner,grant:grant.download_token,job:jobId,artifact:artifactId,sha256:details.sha256,size:details.size,expires},this.env.SESSION_SECRET);
    return {url:publicOrigin(this.env)+"/artifact-download/"+encodeURIComponent(capability),name:details.file_name,mime_type:details.mime_type,bytes:details.size,sha256:details.sha256,expires_at:new Date(expires).toISOString()};
  }
  private finishDownload(error?: Error): void {
    const active = this.activeDownload;
    if (!active) return;
    this.activeDownload = undefined;
    clearTimeout(active.idle); clearTimeout(active.total);
    this.ctx.storage.sql.exec("UPDATE download_grants SET state=? WHERE id=?", error ? "interrupted" : "done", active.id);
    if (error) {
      active.reject(error);
      void active.writer.abort(error).catch(() => {});
      try { active.socket.send(JSON.stringify({ kind: "artifact_abort", transfer_id: active.id })); } catch { /* disconnected */ }
    } else void active.writer.close().catch(() => {});
  }
  private resetDownloadIdle(active: ActiveDownload): void {
    clearTimeout(active.idle);
    active.idle = setTimeout(() => { if (this.activeDownload === active) this.finishDownload(new RelayError("download_timeout", "Download stalled; request the same artifact again", 408)); }, 20_000);
  }
  async approveDownload(downloadToken: string): Promise<ReadableStream<Uint8Array>> {
    if (this.activeDownload || this.one("SELECT id FROM download_grants WHERE state='active' AND expires>?", Date.now())) throw new RelayError("download_busy", "Finish or cancel the current download first", 409);
    const manifest = await this.downloadDetails(downloadToken);
    if (!manifest.eligible) throw new RelayError("download_unavailable", "The paired worker is unavailable", 409);
    if (this.activeDownload || this.one("SELECT id FROM download_grants WHERE state='active' AND expires>?", Date.now())) throw new RelayError("download_busy", "Finish or cancel the current download first", 409);
    if (!manifest.stream_available) {
      this.ctx.storage.sql.exec("UPDATE download_grants SET state='active' WHERE id=? AND state='pending'", downloadToken);
      try {
        const result = await this.tool("read_artifact", { delegation_id: manifest.delegation_id, artifact_id: manifest.artifact_id });
        const artifact = object(result.artifact);
        if (artifact.id !== manifest.artifact_id || artifact.encoding !== "base64" || typeof artifact.content !== "string") throw new RelayError("download_unavailable", "Artifact is not available yet; try the same file again", 409);
        const bytes = unbase64(artifact.content);
        if (bytes.byteLength !== manifest.size || bytes.byteLength > 12_000 || createHash("sha256").update(bytes).digest("hex") !== manifest.sha256)
          throw new RelayError("artifact_changed", "Artifact content changed after publication", 409);
        this.ctx.storage.sql.exec("UPDATE download_grants SET state='done' WHERE id=?", downloadToken);
        return new ReadableStream({ type: "bytes", start(controller) { if (bytes.byteLength) controller.enqueue(bytes); controller.close(); } });
      } catch (error) { this.ctx.storage.sql.exec("UPDATE download_grants SET state='interrupted' WHERE id=?", downloadToken); throw error; }
    }
    const job = this.job(manifest.delegation_id), socket = this.connection(job.worker_id)!;
    let ready!: () => void, reject!: (error: Error) => void;
    const opened = new Promise<void>((resolve, fail) => { ready = resolve; reject = fail; });
    const stream = new FixedLengthStream(manifest.size);
    const active: ActiveDownload = { id: downloadToken, worker_id: job.worker_id, socket, manifest, offset: 0, credit: false,
      writer: stream.writable.getWriter(), ready, reject, idle: setTimeout(() => {}, 0), total: setTimeout(() => {}, 0), opened: false, hash: createHash("sha256") };
    this.activeDownload = active;
    void active.writer.closed.catch(() => { if (this.activeDownload === active) this.finishDownload(new RelayError("download_cancelled", "Download cancelled", 409)); });
    this.ctx.storage.sql.exec("UPDATE download_grants SET state='active' WHERE id=? AND state='pending'", downloadToken);
    this.resetDownloadIdle(active);
    active.total = setTimeout(() => { if (this.activeDownload === active) this.finishDownload(new RelayError("download_timeout", "Download exceeded five minutes", 408)); }, 300_000);
    try {
      socket.send(JSON.stringify({ kind: "artifact_open", delegation_id: job.id, artifact_id: manifest.artifact_id, transfer_id: downloadToken, max_bytes: MAX_DOWNLOAD_BYTES, chunk_bytes: DOWNLOAD_CHUNK_BYTES }));
      await opened;
      return stream.readable;
    } catch (error) { this.finishDownload(error instanceof Error ? error : new Error("Download failed")); throw error; }
  }

  async beginWorkerRenewal(workerId: string, oldSecret: string, newCredentialHash: string): Promise<{ renewal_id: string; verification_url: string; expires: number; expires_at: number }> {
    const proof = await digest(oldSecret);
    const worker = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", ID.parse(workerId));
    if (!worker || worker.revoked || worker.approval_mode!=="legacy_30_day" || worker.expires + RENEWAL_WINDOW_MS <= Date.now() || !/^[a-f0-9]{64}$/.test(newCredentialHash)
      || newCredentialHash === worker.credential_hash || !equal(proof, worker.credential_hash)) throw new RelayError("renewal_denied", "This identity cannot be renewed; inspect saved work locally", 403);
    const previous = this.one<RenewalRow>("SELECT * FROM worker_renewals WHERE worker_id=? AND expires>? AND approved=0", workerId, Date.now());
    if (previous && previous.new_hash !== newCredentialHash) throw new RelayError("renewal_pending", "Finish the pending renewal before starting another", 409);
    const id = previous?.id ?? token(16), expires = previous?.expires ?? Date.now() + 600_000;
    if (!previous) this.ctx.storage.sql.exec("INSERT INTO worker_renewals(id,worker_id,old_hash,new_hash,expires) VALUES(?,?,?,?,?)", id, workerId, worker.credential_hash, newCredentialHash, expires);
    await this.schedule(expires);
    return { renewal_id: id, verification_url: publicOrigin(this.env) + "/renew?code=" + id, expires, expires_at: expires };
  }
  async renewalDetails(renewalId: string): Promise<ObjectValue> {
    const renewal = this.one<RenewalRow>("SELECT * FROM worker_renewals WHERE id=?", ID.parse(renewalId));
    if (!renewal || renewal.expires <= Date.now()) throw new RelayError("renewal_expired", "Renewal approval expired", 410);
    const worker = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", renewal.worker_id);
    if (!worker || worker.revoked || worker.expires + RENEWAL_WINDOW_MS <= Date.now()) throw new RelayError("renewal_denied", "Worker cannot be renewed", 403);
    return { renewal_id: renewal.id, worker_id: worker.id, label: worker.label, workspaces: JSON.parse(worker.workspaces), approved: renewal.approved === 1, declined: renewal.approved === -1, expires: renewal.expires };
  }
  async approveWorkerRenewal(renewalId: string, ownerKey: string): Promise<ObjectValue> {
    if (ownerKey !== this.one<{ owner_key: string }>("SELECT owner_key FROM identity")?.owner_key) throw new RelayError("owner_conflict", "Use the account that connected this computer", 403);
    await this.renewalDetails(renewalId);
    const renewal = this.one<RenewalRow>("SELECT * FROM worker_renewals WHERE id=?", renewalId)!;
    if (renewal.approved === -1) throw new RelayError("renewal_declined", "Renewal was declined", 409);
    if (!renewal.approved) {
      const current = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", renewal.worker_id);
      if (!current || current.revoked || current.expires + RENEWAL_WINDOW_MS <= Date.now() || current.credential_hash !== renewal.old_hash)
        throw new RelayError("renewal_denied", "Worker authorization changed during renewal", 403);
      this.ctx.storage.transactionSync(() => {
        this.ctx.storage.sql.exec("UPDATE workers SET credential_hash=?,expires=?,access_expires=?,last_seen=0 WHERE id=? AND revoked=0", renewal.new_hash, Date.now() + 30 * 86_400_000,Date.now()+30*86_400_000, renewal.worker_id);
        this.ctx.storage.sql.exec("UPDATE worker_renewals SET approved=1,expires=? WHERE id=?", Date.now() + RENEWAL_WINDOW_MS, renewal.id);
      });
      for (const ws of this.ctx.getWebSockets(renewal.worker_id)) ws.close(4001, "Same worker authorization renewed");
      this.wake("worker:" + renewal.worker_id);
      this.wakeUi();
      for (const job of this.ctx.storage.sql.exec<{ id: string }>("SELECT id FROM jobs WHERE worker_id=?", renewal.worker_id).toArray()) this.wakeUi(job.id);
    }
    return { approved: true, worker_id: renewal.worker_id };
  }
  async declineWorkerRenewal(renewalId: string): Promise<ObjectValue> {
    await this.renewalDetails(renewalId);
    const renewal = this.one<RenewalRow>("SELECT * FROM worker_renewals WHERE id=?", renewalId)!;
    if (renewal.approved === 1) throw new RelayError("renewal_used", "An approved renewal cannot be declined", 409);
    this.ctx.storage.sql.exec("UPDATE worker_renewals SET approved=-1 WHERE id=?", renewalId);
    return { declined: true };
  }
  async claimWorkerRenewal(renewalId: string, oldSecret: string, expectedWorkerId?: string): Promise<ObjectValue> {
    const renewal = this.one<RenewalRow>("SELECT * FROM worker_renewals WHERE id=?", ID.parse(renewalId));
    if (!renewal || (expectedWorkerId && renewal.worker_id !== ID.parse(expectedWorkerId)) || renewal.expires <= Date.now() || !equal(await digest(oldSecret), renewal.old_hash))
      throw new RelayError("renewal_denied", "Renewal proof is invalid or expired", 403);
    const workerId = renewal.worker_id;
    const worker = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", workerId);
    if (!worker || worker.revoked) throw new RelayError("renewal_denied", "Worker access was revoked", 403);
    if (renewal.approved === 1 && worker.credential_hash !== renewal.new_hash) throw new RelayError("renewal_superseded", "A later renewal replaced this authorization", 409);
    return renewal.approved === 1 ? { status: "approved", owner_key: this.one<{ owner_key: string }>("SELECT owner_key FROM identity")!.owner_key, worker_id: workerId, expires: worker.expires }
      : { status: renewal.approved === -1 ? "declined" : "pending", expires: renewal.expires };
  }

  async exportData(): Promise<ObjectValue> {
    // User download only. Authentication hashes, browser sessions, OAuth tokens
    // and webhook credentials never enter this export.
    return {
      workers:this.ctx.storage.sql.exec("SELECT id,label,expires,approval_mode,access_expires,paused,admission_revision,revoked,workspaces,readiness,computer_snapshot FROM workers").toArray(),
      tasks:this.ctx.storage.sql.exec("SELECT id,request_id,worker_id,workspace_id,task_type,status,terminal,sequence,payload,result,created,expires FROM jobs").toArray(),
      events:this.ctx.storage.sql.exec("SELECT job_id,sequence,payload FROM events ORDER BY job_id,sequence").toArray(),
      artifacts:this.ctx.storage.sql.exec("SELECT job_id,id,payload FROM artifacts").toArray(),
      settings:this.ctx.storage.sql.exec("SELECT preferred_workspace_id FROM account_settings").toArray(),
      operations:this.ctx.storage.sql.exec("SELECT id,worker_id,action,state,summary,created,updated FROM computer_operations").toArray()
    };
  }
  async eraseData(): Promise<ObjectValue> {
    this.finishDownload(new RelayError("records_erased", "Relay records were erased", 410));
    for(const socket of this.ctx.getWebSockets()) socket.close(4003,"Relay records erased; inspect work locally");
    this.ctx.storage.transactionSync(()=>{
      for(const table of ["download_grants","worker_renewals","credential_rotations","computer_operations","computer_messages","account_settings","subscriptions","artifacts","events","suppressed_events","commands","jobs","workers","retired_requests","identity"])
        this.ctx.storage.sql.exec(`DELETE FROM ${table}`);
    });
    for(const key of this.waiters.keys()) this.wake(key);
    await this.ctx.storage.deleteAlarm();
    return {erased:true,provider_history:"managed on your own worker and provider accounts",running_tasks:"inspect locally; erasure is not cancellation"};
  }

  private async reply(action: () => Promise<ObjectValue>): Promise<ObjectValue> {
    try { return await action(); }
    catch (error) {
      const fault = relayError(error);
      return { relay_error: true, code: fault?.code ?? "request_rejected", status: fault?.status ?? 400,
        message: fault?.message ?? "Request failed validation; inspect the owned receipt." };
    }
  }
  async toolRequest(name: ToolName, input: unknown): Promise<ObjectValue> {
    return this.reply(() => this.tool(name, input));
  }
  async eventRequest(method: "subscribe" | "unsubscribe", input: unknown, bearer?: string, expires?: number): Promise<ObjectValue> {
    return this.reply(() => method === "subscribe" ? this.subscribe(input, bearer!, expires!) : this.unsubscribe(input));
  }
  async workerRequest(method: "commands" | "message", workerId: string, secret: string, input?: unknown): Promise<ObjectValue> {
    return this.reply(() => method === "commands" ? this.pullCommands(workerId, secret) : this.workerHttp(workerId, secret, input));
  }

  async tool(name: ToolName, raw: unknown): Promise<ObjectValue> {
    const input = object(inputs[name].parse(raw));
    if(name==="show_computers")return {...await this.controlSnapshot()};
    if(name==="watch_computers_ui")return {...await this.watchComputers(input.after_revision as string|undefined,Number(input.wait_ms))};
    if(name==="prepare_operation")return this.prepareOperation(input);
    if(name==="confirm_operation")return this.confirmOperation(String(input.operation_id),String(input.confirmation_token));
    if(name==="inspect_operation")return this.inspectOperation(String(input.operation_id));
    if(name==="export_account_ui")return this.exportData();
    if(name==="prepare_artifact_download_ui")return this.artifactDownloadGrant(String(input.delegation_id),String(input.artifact_id));
    if (name === "show_delegation") return { ...await this.uiSnapshot(input.delegation_id as string | undefined) };
    if (name === "watch_delegation_ui") return { ...await this.watchUi(input.delegation_id as string | undefined, input.after_revision as string | undefined, Number(input.wait_ms)) };
    if (name === "read_artifact_ui") return this.tool("read_artifact", input);
    if (name === "list_workspaces") return this.workspaces();
    if (name === "prepare_delegation") return this.prepare(input);
    if (name === "delegate_via_luna" || name === "check_integration") {
      const workers = this.ctx.storage.sql.exec<WorkerRow>("SELECT * FROM workers WHERE revoked=0 AND expires>?", Date.now()).toArray();
      const matches = workers.filter(w => (JSON.parse(w.workspaces) as Workspace[]).some(ws => ws.id === input.workspace_id));
      if (matches.length > 1) throw new RelayError("workspace_ambiguous", "Choose the worker through guided task setup before delegating", 409);
      const row = matches[0];
      if (!row) throw new RelayError("not_found", "Owned workspace not found", 404);
      const ws = (JSON.parse(row.workspaces) as Workspace[]).find(w => w.id === input.workspace_id)!;
      if (!ws.task_types.includes(input.task_type as typeof ws.task_types[number]) || !ws.isolation.includes(input.isolation as typeof ws.isolation[number]))
        throw new RelayError("workspace_policy", "Task type or isolation is not approved for this workspace");
      if (name === "check_integration") {
        const cached = object(JSON.parse(row.readiness));
        return { workspace_id: ws.id, online: this.online(row.id), cached: true,
          readiness: cached[ws.id] ?? null, inference_started: false };
      }
      const caps = input.required_capabilities as string[];
      if (caps.some(cap => !ws.capabilities.includes(cap as "local_files" | "shell"))) throw new RelayError("capability_unavailable", "Requested capability is not available on this worker");
      return this.start(row.id, ws.id, "delegate", input);
    }
    if (name === "inspect_delegation") {
      const row = input.request_id ? this.one<JobRow>("SELECT * FROM jobs WHERE request_id=?", String(input.request_id)) : this.job(String(input.delegation_id));
      if (!row || row.expires <= Date.now()) throw new RelayError("not_found", "Owned launch not found", 404);
      this.canReadSavedJob(row);
      return this.receipt(row);
    }
    const job = this.job(String(input.delegation_id));
    if (name === "read_delegation_result") this.canReadSavedJob(job);
    else this.checkJobWorker(job); // Dispatch requires a live, unrevoked worker.
    if (name === "continue_via_luna") {
      if (!job.terminal || !job.local_id) throw new RelayError("not_completed", "Correction requires a completed saved provider task", 409);
      return this.start(job.worker_id, job.workspace_id, "continue", { ...input, source_id: job.id });
    }
    if (name === "cancel_delegation") {
      if (job.terminal) return this.receipt(job);
      if (["queued", "awaiting_setup"].includes(job.status)) {
        this.ctx.storage.sql.exec("UPDATE commands SET state='cancelled' WHERE job_id=? AND operation IN ('delegate','continue')", job.id);
        await this.acceptEvent(job, { sequence: job.sequence + 1, kind: "cancelled", terminal: true,
          summary: "Cancelled before worker launch. No provider inference was started.",
          task_type: this.taskType(job), verification_required: false }, { status: "cancelled", inference_started: false }, "cancel-before-launch");
      } else {
        this.ctx.storage.sql.exec("UPDATE jobs SET status='cancelling' WHERE id=?", job.id);
        this.queue(job, "cancel", {});
      }
      this.wakeUi(job.id);
      return this.receipt(this.job(job.id));
    }
    if (name === "cleanup_task_worktree") {
      if (!job.terminal || !job.local_id) throw new RelayError("not_completed", "Cleanup requires an owned completed task", 409);
      const commandId=this.queue(job, "cleanup", input);
      return { delegation_id: job.id, cleanup_command_id:commandId,cleanup_requested: true, confirmed: false };
    }
    if (name === "read_delegation_result") {
      if (!job.terminal || !job.result) throw new RelayError("not_completed", "Wait for the task's terminal event before reading its result", 409);
      return { ...this.receipt(job), handoff: JSON.parse(job.result) };
    }
    if (name === "ack_delegation_events") {
      const sequence = Number(input.sequence);
      if (sequence > job.sequence) throw new RelayError("invalid_cursor", "Cannot acknowledge an event that does not exist");
      this.ctx.storage.sql.exec("UPDATE jobs SET acknowledged=MAX(acknowledged,?) WHERE id=?", sequence, job.id);
      return { delegation_id: job.id, acknowledged_sequence: this.job(job.id).acknowledged };
    }
    if (name === "receive_delegation_events") {
      let batch = await this.events(job.id, Number(input.after_sequence));
      if (!(batch.events as Json[]).length && !job.terminal && (this.online(job.worker_id) || job.status === "awaiting_setup" || (job.status === "queued" && !job.local_id))) {
        await this.wait(job.id);
        batch = await this.events(job.id, Number(input.after_sequence));
      }
      const current = this.job(job.id);
      if (current.status === "awaiting_setup") return {...batch,setup_pending:true,inference_started:false};
      if (current.status === "queued" && !current.local_id && !batch.worker_online) return {...batch,waiting_for_worker:true,inference_started:false};
      if (!batch.worker_online && !batch.closed) return { ...batch, closed: true, delivery_suspended: true,
        summary: "Worker is disconnected. Keep the receipt and resume delivery; do not restart inference." };
      return batch;
    }
    if (name === "read_artifact") {
      const cached = this.one<{ payload: string }>("SELECT payload FROM artifacts WHERE job_id=? AND id=?", job.id, String(input.artifact_id));
      if (cached) return { delegation_id: job.id, artifact_id: input.artifact_id!, artifact: JSON.parse(cached.payload) };
      if (!job.terminal) throw new RelayError("not_completed", "Artifact access requires terminal output", 409);
      this.queue(job, "artifact", { artifact_id: input.artifact_id! });
      await this.wait(job.id, 10_000);
      const transferred = this.one<{ payload: string }>("SELECT payload FROM artifacts WHERE job_id=? AND id=?", job.id, String(input.artifact_id));
      return transferred ? { delegation_id: job.id, artifact_id: input.artifact_id!, artifact: JSON.parse(transferred.payload) }
        : { delegation_id: job.id, artifact_id: input.artifact_id!, status: "transfer_requested" };
    }
    throw new RelayError("unknown_tool", "Unsupported operation");
  }

  private taskType(job: JobRow): "implementation" | "analysis" | "research" | "document" {
    return job.task_type as "implementation" | "analysis" | "research" | "document";
  }
  private checkJobWorker(job: JobRow): void {
    if (job.worker_id) this.worker(job.worker_id);
    else if (job.status !== "awaiting_setup" && !job.terminal) throw new RelayError("worker_unavailable", "This task has no approved worker", 409);
  }
  private async prepare(input: ObjectValue): Promise<ObjectValue> {
    const fingerprint = await digest(canonical({ operation: "prepare_delegation", input }));
    const previous = this.one<JobRow>("SELECT * FROM jobs WHERE request_id=?", String(input.request_id));
    if (previous) {
      if (previous.fingerprint !== fingerprint) throw new RelayError("request_conflict", "request_id was already used for different work", 409);
      this.checkJobWorker(previous);
      return this.receipt(this.job(previous.id));
    }
    if (this.one("SELECT request_id FROM retired_requests WHERE request_id=?", String(input.request_id)))
      throw new RelayError("request_expired", "This request has expired; inspect saved evidence before authorizing new work", 410);
    if ((input.required_capabilities as string[]).some(cap => !["local_files","shell"].includes(cap)))
      throw new RelayError("capability_unavailable", "This edition supports local files and shell; live browser, web and connector capabilities are not inherited");
    if ((this.one<{count:number}>("SELECT COUNT(*) AS count FROM retired_requests")?.count ?? 0) >= 2_000)
      throw new RelayError("history_limit", "Recent launch recovery history is full; wait for expiration", 429);
    if ((this.one<{count:number}>("SELECT COUNT(*) AS count FROM jobs")?.count ?? 0) >= Number(this.env.MAX_TASKS_PER_OWNER))
      throw new RelayError("task_limit", "Task retention limit reached", 429);
    if ((this.one<{count:number}>("SELECT COUNT(*) AS count FROM jobs WHERE terminal=0")?.count ?? 0) >= 4)
      throw new RelayError("concurrency_limit", "At most four tasks may await setup or execution", 429);
    // Preference is an owner-confirmed alias, never a newly inferred folder.
    if(!input.workspace_id){const preferred=this.one<{preferred_workspace_id:string}>("SELECT preferred_workspace_id FROM account_settings WHERE id=1")?.preferred_workspace_id;if(preferred)input={...input,workspace_id:preferred};}
    const now = Date.now(), id = token(16);
    this.ctx.storage.sql.exec("INSERT INTO jobs(id,request_id,fingerprint,worker_id,workspace_id,operation,payload,task_type,status,created,expires) VALUES(?,?,?,'',?,'delegate',?,?,'awaiting_setup',?,?)",
      id, String(input.request_id), fingerprint, String(input.workspace_id ?? "project"), canonical({...input, _setup_request: true}),
      String(input.task_type), now, now + Number(this.env.RETENTION_HOURS) * 3_600_000);
    this.ctx.storage.sql.exec("UPDATE jobs SET display_summary=? WHERE id=?", String(input.task).slice(0, 240), id);
    await this.schedule(now + Number(this.env.RETENTION_HOURS) * 3_600_000);
    this.wakeUi(id);
    // Explicit existing aliases already have workspace consent. An absent alias
    // requires the normal setup/pairing page, never an arbitrary folder choice.
    if (typeof input.workspace_id === "string") {
      const candidates = this.ctx.storage.sql.exec<WorkerRow>("SELECT * FROM workers WHERE revoked=0 AND expires>?", now).toArray()
        .filter(row => (JSON.parse(row.workspaces) as Workspace[]).some(ws => ws.id === input.workspace_id));
      if (candidates.length === 1) await this.bindPrepared(String(input.request_id), candidates[0].id);
    }
    return this.receipt(this.job(id));
  }
  async setupDetails(requestId: string): Promise<ObjectValue> {
    const row = this.one<JobRow>("SELECT * FROM jobs WHERE request_id=?", ID.parse(requestId));
    if (!row || row.expires <= Date.now()) throw new RelayError("not_found", "This setup request does not belong to the signed-in account", 404);
    return {...this.receipt(row), task: String(object(JSON.parse(row.payload)).task ?? ""), task_type: row.task_type};
  }
  async bindPrepared(requestId: string, workerId: string, workspaceId?: string): Promise<ObjectValue> {
    const detail = await this.setupDetails(requestId), job = this.job(String(detail.delegation_id));
    if (job.worker_id) {
      if (job.worker_id !== workerId || (workspaceId && job.workspace_id !== workspaceId))
        throw new RelayError("setup_conflict", "This task is already bound to another worker or workspace", 409);
      return this.receipt(job);
    }
    if (job.status !== "awaiting_setup" || job.terminal) throw new RelayError("setup_closed", "This setup request is no longer awaiting approval", 409);
    const saved = object(JSON.parse(job.payload));
    if (saved._setup_request !== true) throw new RelayError("setup_closed", "This task is not a setup request", 409);
    if (workspaceId && saved.workspace_id && workspaceId !== saved.workspace_id)
      throw new RelayError("workspace_policy", "The selected workspace differs from the requested workspace");
    const chosenId = workspaceId ? ID.parse(workspaceId) : saved.workspace_id;
    const row = this.worker(workerId), choices = (JSON.parse(row.workspaces) as Workspace[])
      .filter(ws => !chosenId || ws.id === chosenId);
    if(!this.computerReady(row))return this.receipt(job);
    if (choices.length !== 1) throw new RelayError("workspace_policy", "Approve one unambiguous workspace for this task");
    const selected = choices[0], isolation = saved.isolation === "auto" ? selected.isolation[0] : saved.isolation;
    if (!selected.isolation.includes(isolation as "shared" | "worktree") || !selected.task_types.includes(saved.task_type as typeof selected.task_types[number])
      || (saved.required_capabilities as string[]).some(cap => !selected.capabilities.includes(cap as "local_files" | "shell")))
      throw new RelayError("workspace_policy", "The approved workspace does not support the requested task");
    const delegateInput: ObjectValue = {...saved, workspace_id:selected.id, isolation};
    delete delegateInput._setup_request;
    const payload = object(inputs.delegate_via_luna.parse(delegateInput));
    // SQL state and queue creation are committed together. A browser retry or
    // reconnection sees the same job and command; it cannot start another task.
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec("UPDATE jobs SET worker_id=?,workspace_id=?,payload=?,status='queued' WHERE id=? AND status='awaiting_setup' AND worker_id=''",
        workerId, selected.id, canonical(payload), job.id);
      this.queue(this.job(job.id), "delegate", payload);
    });
    this.wakeUi(job.id);
    return this.receipt(this.job(job.id));
  }
  private async start(workerId: string, workspaceId: string, operation: string, input: ObjectValue): Promise<ObjectValue> {
    const fingerprint = await digest(canonical({ operation, input }));
    const previous = this.one<JobRow>("SELECT * FROM jobs WHERE request_id=?", String(input.request_id));
    if (previous) {
      if (previous.fingerprint !== fingerprint) throw new RelayError("request_conflict", "request_id was already used for different work", 409);
      if (previous.expires <= Date.now()) throw new RelayError("request_expired", "Saved task expired; inspect worker evidence before authorizing any new work", 410);
      return this.receipt(previous);
    }
    const retired = this.one<{ fingerprint: string }>("SELECT fingerprint FROM retired_requests WHERE request_id=?", String(input.request_id));
    if (retired) throw new RelayError("request_expired", "Saved task expired; inspect worker evidence before authorizing any new work", 410);
    const remembered = this.one<{ count: number }>("SELECT COUNT(*) AS count FROM retired_requests")?.count ?? 0;
    if (remembered >= 2_000) throw new RelayError("history_limit", "Recent launch recovery history is full; wait for expiration", 429);
    const worker = this.worker(workerId);
    const count = this.one<{ count: number }>("SELECT COUNT(*) AS count FROM jobs")?.count ?? 0;
    if (count >= Number(this.env.MAX_TASKS_PER_OWNER)) throw new RelayError("task_limit", "Task retention limit reached; delete reviewed records or wait for expiration", 429);
    const active = this.one<{ count: number }>("SELECT COUNT(*) AS count FROM jobs WHERE terminal=0")?.count ?? 0;
    if (active >= 4) throw new RelayError("concurrency_limit", "At most four delegations may be active for this account", 429);
    const now = Date.now();
    const id = token(16);
    const source = input.source_id ? this.job(String(input.source_id)) : null;
    this.ctx.storage.sql.exec("INSERT INTO jobs(id,request_id,fingerprint,worker_id,workspace_id,source_id,operation,payload,task_type,model_pin,status,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
      id, String(input.request_id), fingerprint, worker.id, workspaceId, input.source_id ? String(input.source_id) : null,
      operation, canonical(input), source?.task_type ?? String(input.task_type), source?.model_pin ?? null, now, now + Number(this.env.RETENTION_HOURS) * 3_600_000);
    this.ctx.storage.sql.exec("UPDATE jobs SET display_summary=? WHERE id=?", source?.display_summary || String(input.task ?? "Correction of saved task").slice(0, 240), id);
    this.queue(this.job(id), operation, input);
    this.wakeUi(id);
    await this.schedule(now + Number(this.env.RETENTION_HOURS) * 3_600_000);
    return this.receipt(this.job(id));
  }

  async events(jobId: string, cursor: number): Promise<ObjectValue> {
    const job = this.job(jobId);
    this.checkJobWorker(job);
    if (!Number.isSafeInteger(cursor) || cursor < 0 || cursor > job.sequence) throw new RelayError("invalid_cursor", "Event cursor is outside the saved archive");
    const rows = this.ctx.storage.sql.exec<EventRow>("SELECT sequence,payload FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 9", jobId, cursor).toArray();
    return { delegation_id: jobId, events: rows.map(row => JSON.parse(row.payload)), closed: Boolean(job.terminal),
      sequence: job.sequence, worker_online: this.online(job.worker_id) };
  }

  private async authenticateWorker(workerId: string, secret: string): Promise<WorkerRow> {
    const fingerprint = await digest(secret);
    let worker: WorkerRow;
    try { worker = this.worker(workerId); }
    catch { throw new RelayError("invalid_worker_token", "Worker pairing expired or was revoked", 401); }
    if ((worker.access_expires||worker.expires)<=Date.now()||!equal(fingerprint, worker.credential_hash)) throw new RelayError("invalid_worker_token", "Worker authentication failed or access credential expired", 401);
    return worker;
  }
  async pullCommands(workerId: string, secret: string): Promise<ObjectValue> {
    await this.authenticateWorker(workerId, secret);
    const wasOnline = this.online(workerId);
    this.ctx.storage.sql.exec("UPDATE workers SET last_seen=? WHERE id=?", Date.now(), workerId);
    if (!wasOnline) this.workerUiChanged(workerId);
    let rows = this.pendingCommands(workerId),controls=await this.controlPackets(workerId);
    rows=this.pendingCommands(workerId);
    if (!rows.length&&!controls.length) { await this.wait("worker:" + workerId, 20_000); await this.authenticateWorker(workerId, secret); rows = this.pendingCommands(workerId);controls=await this.controlPackets(workerId); }
    // Control preparation can await cryptography. Recheck both credentials and
    // admission immediately before leasing, after every asynchronous boundary.
    await this.authenticateWorker(workerId,secret);
    rows=this.pendingCommands(workerId);
    this.ctx.storage.sql.exec("UPDATE workers SET last_seen=? WHERE id=?", Date.now(), workerId);
    for (const row of rows) {
      this.ctx.storage.sql.exec("UPDATE commands SET state='leased' WHERE id=?", row.id);
      if (["delegate", "continue"].includes(row.operation)) this.ctx.storage.sql.exec("UPDATE jobs SET status='leased' WHERE id=? AND status='queued'", row.job_id);
      if (row.state === "queued") this.wakeUi(row.job_id);
    }
    return { controls,commands: rows.map(row => ({ kind: "command", command_id: row.id, delegation_id: row.job_id,
      operation: row.operation, payload: JSON.parse(row.payload), allow_start: row.state === "queued",admission_revision:this.worker(workerId).admission_revision })) };
  }
  private pendingCommands(workerId: string): CommandRow[] {
    const ready=this.computerReady(this.worker(workerId));
    return this.ctx.storage.sql.exec<CommandRow>("SELECT * FROM commands WHERE worker_id=? AND state IN ('queued','leased') AND (operation NOT IN ('delegate','continue') OR (?=1 AND NOT EXISTS(SELECT 1 FROM workers WHERE id=commands.worker_id AND paused=1))) ORDER BY rowid LIMIT 8", workerId,ready?1:0).toArray();
  }
  private computerReady(worker:WorkerRow):boolean {
    const caps=object(JSON.parse(worker.transport_capabilities));
    if(worker.setup_managed!==1&&caps.setup_control_v1!==true)return true;
    const accounts=computerSnapshotSchema.parse(JSON.parse(worker.computer_snapshot)).accounts;
    return accounts?.codex?.status==="ready"&&accounts?.agy?.status==="ready"&&JSON.parse(worker.workspaces).length>0;
  }
  private async bindReadySetup(workerId:string):Promise<void>{
    const worker=this.worker(workerId);if(!this.computerReady(worker))return;
    const aliases=new Set((JSON.parse(worker.workspaces) as Workspace[]).map(w=>w.id));
    const jobs=this.ctx.storage.sql.exec<JobRow>("SELECT * FROM jobs WHERE status='awaiting_setup' AND terminal=0 AND expires>?",Date.now()).toArray();
    for(const job of jobs){const input=object(JSON.parse(job.payload));if(typeof input.workspace_id==="string"&&!aliases.has(input.workspace_id))continue;if(worker.setup_request_id===job.request_id||typeof input.workspace_id==="string"&&aliases.has(input.workspace_id)){
      try{await this.bindPrepared(job.request_id,workerId);}catch(error){const failure=relayError(error);if(!failure||!["workspace_policy","not_found","setup_closed"].includes(failure.code))throw error;}
    }}
  }
  async workerHttp(workerId: string, secret: string, message: unknown): Promise<ObjectValue> {
    await this.authenticateWorker(workerId, secret);
    const packet = object(message);
    if (this.retiredPacket(workerId, packet)) return { kind: "ack", message_id: ID.parse(packet.message_id), disposition: "task_expired" };
    if(this.retiredControlPacket(workerId,packet))return {kind:"ack",message_id:ID.parse(packet.message_id),disposition:"operation_expired"};
    await this.consumeWorkerPacket(workerId, packet);
    return { kind: "ack", message_id: typeof packet.message_id === "string" ? ID.parse(packet.message_id) : null };
  }

  async fetch(request: Request): Promise<Response> {
    // WebSocket upgrades use the DO HTTP transport; RPC cannot serialize a
    // WebSocket embedded in a 101 Response.
    const credential = /^Bearer ([a-f0-9]{64})\.([a-f0-9]{32})\.([a-f0-9]{64})$/.exec(request.headers.get("Authorization") ?? "");
    if (request.method !== "GET" || new URL(request.url).pathname !== "/worker/connect" || request.headers.get("Upgrade")?.toLowerCase() !== "websocket") return new Response(null,{status:405});
    if (!credential || credential[1] !== this.one<{owner_key:string}>("SELECT owner_key FROM identity")?.owner_key)
      return Response.json({error:"invalid_worker_token",message:"Worker authentication failed"},{status:401});
    return this.connectWorker(credential[2],credential[3]);
  }

  async connectWorker(workerId: string, secret: string): Promise<Response> {
    try { await this.authenticateWorker(workerId, secret); }
    catch (error) {
      const fault = relayError(error);
      return Response.json({ error: fault?.code ?? "invalid_worker_token", message: "Worker authentication failed" }, { status: fault?.status ?? 401 });
    }
    const pair = new WebSocketPair();
    if (this.activeDownload?.worker_id === workerId) this.finishDownload(new RelayError("download_disconnected", "Worker reconnected; request the same artifact again", 409));
    const connection = { worker_id: workerId, generation: token(16) };
    for (const existing of this.ctx.getWebSockets(workerId)) existing.close(4001, "Replaced by the same authenticated worker");
    this.ctx.acceptWebSocket(pair[1], [workerId]);
    pair[1].serializeAttachment(connection);
    // Protocol ping frames are handled by Cloudflare without waking the object.
    const worker=this.worker(workerId);
    pair[1].send(JSON.stringify({ kind: "connected", worker_id: workerId,paused:Boolean(worker.paused),admission_revision:worker.admission_revision,approval_mode:worker.approval_mode,access_expires_at:worker.access_expires||worker.expires }));
    await this.dispatchControls(workerId);
    this.dispatch(workerId);
    this.workerUiChanged(workerId);
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    try {
      const connection = ws.deserializeAttachment() as Connection;
      const worker=this.worker(connection.worker_id);
      if((worker.access_expires||worker.expires)<=Date.now())throw new RelayError("invalid_worker_token","Access credential expired; rotate credentials",401);
      if (this.connection(connection.worker_id) !== ws) throw new RelayError("invalid_worker_token", "Worker connection was replaced", 401);
      if (typeof message !== "string" || new TextEncoder().encode(message).length > 32_768) throw new RelayError("payload_too_large", "Worker message exceeds limit");
      const packet = object(JSON.parse(message));
      if (String(packet.kind).startsWith("artifact_")) { await this.consumeArtifactPacket(ws, packet); return; }
      const packetId = typeof packet.message_id === "string" ? ID.parse(packet.message_id) : null;
      if (this.retiredPacket(connection.worker_id, packet)) { ws.send(JSON.stringify({ kind: "ack", message_id: packetId, disposition: "task_expired" })); return; }
      if(this.retiredControlPacket(connection.worker_id,packet)){ws.send(JSON.stringify({kind:"ack",message_id:packetId,disposition:"operation_expired"}));return;}
      await this.consumeWorkerPacket(connection.worker_id, packet);
      ws.send(JSON.stringify({ kind: "ack", message_id: packetId }));
      this.dispatch(connection.worker_id);
    } catch (error) {
      ws.send(JSON.stringify({ kind: "error", error: error instanceof RelayError ? error.code : "invalid_worker_message" }));
    }
  }
  private workerUiChanged(workerId: string): void {
    this.wakeUi();
    for (const job of this.ctx.storage.sql.exec<{ id: string }>("SELECT id FROM jobs WHERE worker_id=? AND expires>?", workerId, Date.now()).toArray()) this.wakeUi(job.id);
  }
  private retiredPacket(workerId: string, packet: ObjectValue): boolean {
    if (typeof packet.message_id !== "string" || typeof packet.delegation_id !== "string") return false;
    ID.parse(packet.message_id); ID.parse(packet.delegation_id);
    return Boolean(this.one("SELECT job_id FROM retired_requests WHERE job_id=? AND worker_id=? AND expires>?", packet.delegation_id, workerId, Date.now()));
  }
  private retiredControlPacket(workerId:string,packet:ObjectValue):boolean {
    if(packet.kind!=="computer_control_result"||!["pending","succeeded","failed","rejected"].includes(String(packet.state)))return false;
    ID.parse(packet.message_id);
    this.expireRemovalOperations();
    const operation=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=? AND worker_id=?",ID.parse(packet.operation_id),workerId);
    return operation?.state==="unconfirmed"||!operation&&packet.state!=="pending";
  }
  private async creditDownload(active: ActiveDownload): Promise<void> {
    await active.writer.ready;
    if (this.activeDownload !== active || active.credit || active.offset >= active.manifest.size) return;
    active.credit = true;
    active.socket.send(JSON.stringify({ kind: "artifact_credit", transfer_id: active.id, credits: 1, offset: active.offset }));
  }
  private async consumeArtifactPacket(ws: WebSocket, packet: ObjectValue): Promise<void> {
    const active = this.activeDownload;
    // Old transfer generations are never attached to a new browser response.
    if (!active || active.socket !== ws || packet.transfer_id !== active.id) {
      if (typeof packet.transfer_id === "string" && ID.safeParse(packet.transfer_id).success)
        ws.send(JSON.stringify({ kind: "artifact_abort", transfer_id: packet.transfer_id }));
      return;
    }
    try {
      if (packet.kind === "artifact_error") throw new RelayError("download_worker_error", "The worker could not export this saved artifact; inspect it locally", 409);
      if (packet.kind === "artifact_manifest") {
        if (active.opened || packet.artifact_id !== active.manifest.artifact_id || packet.size !== active.manifest.size || packet.sha256 !== active.manifest.sha256)
          throw new RelayError("artifact_changed", "Artifact metadata changed after publication", 409);
        if (active.manifest.size === 0 && active.hash.copy().digest("hex") !== active.manifest.sha256)
          throw new RelayError("artifact_changed", "Empty artifact digest is invalid", 409);
        active.opened = true; active.ready();
        await this.creditDownload(active);
      } else if (packet.kind === "artifact_chunk") {
        if (!active.opened || !active.credit || packet.offset !== active.offset || typeof packet.data !== "string" || packet.data.length > 21_848)
          throw new RelayError("download_sequence", "Artifact transfer sequence is invalid", 409);
        const bytes = unbase64(packet.data);
        if (!bytes.byteLength || bytes.byteLength > DOWNLOAD_CHUNK_BYTES || active.offset + bytes.byteLength > active.manifest.size)
          throw new RelayError("download_size", "Artifact transfer exceeds its declared size", 409);
        active.credit = false; active.offset += bytes.byteLength;
        active.hash.update(bytes);
        // Withhold the final bytes until our own digest matches. Otherwise a
        // Content-Length download could look successful before end validation.
        if (active.offset === active.manifest.size && active.hash.copy().digest("hex") !== active.manifest.sha256)
          throw new RelayError("artifact_changed", "Artifact content did not match its pinned digest", 409);
        await active.writer.write(bytes);
        await this.creditDownload(active);
      } else if (packet.kind === "artifact_end") {
        if (!active.opened || active.offset !== active.manifest.size || packet.bytes !== active.offset || packet.sha256 !== active.manifest.sha256 || active.hash.digest("hex") !== active.manifest.sha256)
          throw new RelayError("artifact_changed", "Artifact transfer did not match its pinned content", 409);
        this.finishDownload(); return;
      } else throw new RelayError("invalid_packet", "Unsupported artifact packet");
      this.resetDownloadIdle(active);
    } catch (error) { this.finishDownload(error instanceof Error ? error : new Error("Download failed")); }
  }
  private async consumeWorkerPacket(workerId: string, packet: ObjectValue): Promise<void> {
      this.worker(workerId);
      if(["computer_control_result","computer_status","command_deferred"].includes(String(packet.kind))){
        const id=ID.parse(packet.message_id),fingerprint=await digest(canonical(packet));
        const duplicate=this.one<{worker_id:string;fingerprint:string}>("SELECT worker_id,fingerprint FROM computer_messages WHERE id=?",id);
        if(duplicate){if(duplicate.worker_id!==workerId||duplicate.fingerprint!==fingerprint)throw new RelayError("message_conflict","Computer message ID was reused",409);return;}
        const revision=z.number().int().min(0).max(Number.MAX_SAFE_INTEGER).parse(packet.revision),worker=this.worker(workerId);
        if(packet.kind==="command_deferred"){
          if(packet.reason!=="computer_paused")throw new RelayError("invalid_packet","Unsupported command deferral");
          const job=this.job(ID.parse(packet.delegation_id)),command=this.one<CommandRow>("SELECT * FROM commands WHERE id=? AND worker_id=? AND job_id=?",ID.parse(packet.command_id),workerId,job.id);
          if(job.worker_id!==workerId||!command||!["delegate","continue"].includes(command.operation))throw new RelayError("not_found","Owned launch command not found",404);
          if(job.local_id||job.terminal||command.state!=="leased")throw new RelayError("already_started","A started or finished command cannot be returned to the queue",409);
          this.ctx.storage.transactionSync(()=>{
            if(revision>=worker.admission_revision)this.ctx.storage.sql.exec("UPDATE workers SET paused=1,admission_revision=? WHERE id=?",revision,workerId);
            this.ctx.storage.sql.exec("UPDATE commands SET state='queued' WHERE id=? AND state='leased'",command.id);
            this.ctx.storage.sql.exec("UPDATE jobs SET status='queued' WHERE id=? AND local_id IS NULL AND terminal=0",job.id);
          });this.wakeUi(job.id);
          if(this.one("SELECT id FROM computer_operations WHERE worker_id=? AND action IN ('stop_tasks','uninstall') AND (state='pending' OR (state='completed' AND revision>=?)) ORDER BY created DESC LIMIT 1",workerId,revision)){
            this.ctx.storage.sql.exec("UPDATE commands SET state='cancelled' WHERE id=?",command.id);
            await this.acceptEvent(this.job(job.id),{sequence:1,kind:"cancelled",terminal:true,summary:"Cancelled before worker launch by the computer stop request. No provider inference started.",task_type:this.taskType(job),verification_required:false},{status:"cancelled",inference_started:false},"computer-stop-before-launch");
          }
        } else {
          const snapshot=packet.snapshot===undefined?{}:computerSnapshotSchema.parse(packet.snapshot);
          if(packet.kind==="computer_control_result"){
            let operation=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=? AND worker_id=?",ID.parse(packet.operation_id),workerId);
            if(!operation||operation.state==="prepared"||revision!==operation.revision)throw new RelayError("operation_conflict","Computer result does not match its confirmed operation",409);
            const state=z.enum(["pending","succeeded","failed","rejected"]).parse(packet.state),summary=z.string().max(600).parse(packet.summary),resultFingerprint=await digest(canonical({state,summary}));
            const replay=this.one<{worker_id:string;fingerprint:string}>("SELECT worker_id,fingerprint FROM computer_messages WHERE id=?",id);
            if(replay){if(replay.worker_id!==workerId||replay.fingerprint!==fingerprint)throw new RelayError("message_conflict","Computer message ID was reused",409);return;}
            operation=this.one<OperationRow>("SELECT * FROM computer_operations WHERE id=? AND worker_id=?",ID.parse(packet.operation_id),workerId)!;
            if(operation.state!=="pending"){
              if(operation.result_fingerprint!==resultFingerprint)throw new RelayError("operation_conflict","Operation already has a different final receipt",409);
            } else {
              if(operation.action==="uninstall"&&state==="succeeded")throw new RelayError("removal_proof_required","Use the uninstall completion capability for final removal",403);
              if(operation.action==="configure_workspace"&&state==="succeeded"){
                const approved=workspaceSchema.parse(packet.workspace_approved),expected=object(JSON.parse(operation.parameters));
                if(approved.id!==expected.alias||approved.isolation.length!==1||approved.isolation[0]!==expected.isolation)throw new RelayError("workspace_policy","Approved workspace differs from the confirmed folder request",409);
                const current=this.worker(workerId),workspaces=JSON.parse(current.workspaces) as Workspace[];
                if(this.ctx.storage.sql.exec<WorkerRow>("SELECT * FROM workers WHERE id!=? AND revoked=0 AND expires>?",workerId,Date.now()).toArray().some(row=>(JSON.parse(row.workspaces) as Workspace[]).some(w=>w.id===approved.id)))throw new RelayError("duplicate_workspace","Workspace alias already belongs to another computer",409);
                const existing=workspaces.find(w=>w.id===approved.id);
                if(existing&&canonical(existing)!==canonical(approved))throw new RelayError("workspace_conflict","An approved workspace alias cannot be replaced silently",409);
                if(!existing){if(workspaces.length>=16)throw new RelayError("workspace_limit","Computer workspace limit reached",429);workspaces.push(approved);this.ctx.storage.sql.exec("UPDATE workers SET workspaces=? WHERE id=?",canonical(workspaces),workerId);}
              }
              this.ctx.storage.sql.exec("UPDATE computer_operations SET state=?,summary=?,result_fingerprint=?,updated=?,parameters=CASE WHEN ? THEN '{}' ELSE parameters END WHERE id=?",state==="pending"?"pending":state==="succeeded"?"completed":"failed",summary,state==="pending"?"":resultFingerprint,Date.now(),state!=="pending"?1:0,operation.id);
            }
          }
          const noPendingRemoval=!this.one("SELECT id FROM computer_operations WHERE worker_id=? AND action='uninstall' AND state='pending'",workerId);
          const {paused,...metadata}=snapshot;
          const currentWorker=this.worker(workerId);
          this.ctx.storage.sql.exec("UPDATE workers SET computer_snapshot=?,last_seen=? WHERE id=?",canonical({...object(JSON.parse(currentWorker.computer_snapshot)),...(revision>=currentWorker.admission_revision?metadata:{})}),Date.now(),workerId);
          if(typeof paused==="boolean"&&revision>=currentWorker.admission_revision&&noPendingRemoval)this.ctx.storage.sql.exec("UPDATE workers SET paused=?,admission_revision=? WHERE id=?",paused?1:0,revision,workerId);
        }
        this.ctx.storage.sql.exec("INSERT INTO computer_messages(id,worker_id,fingerprint,created) VALUES(?,?,?,?)",id,workerId,fingerprint,Date.now());
        this.workerUiChanged(workerId);this.wake("worker:"+workerId);await this.schedule(Date.now()+86_400_000);await this.bindReadySetup(workerId);return;
      }
      if (packet.kind === "readiness") {
        const readiness = object(packet.readiness);
        if (canonical(readiness).length > 12_000) throw new RelayError("readiness_too_large", "Readiness report exceeds limit");
        const previous = this.one<WorkerRow>("SELECT * FROM workers WHERE id=?", workerId)!;
        const capabilities = packet.transport_capabilities === undefined ? object(JSON.parse(previous.transport_capabilities)) : z.object({ artifact_stream_v1: z.boolean(),
          max_artifact_bytes: z.number().int().min(0).max(MAX_DOWNLOAD_BYTES), artifact_chunk_bytes: z.number().int().min(1).max(DOWNLOAD_CHUNK_BYTES),computer_control_v1:z.boolean().optional(),setup_control_v1:z.boolean().optional(),device_refresh_v1:z.boolean().optional() }).strict().parse(packet.transport_capabilities);
        const changed = previous.readiness !== canonical(readiness) || previous.transport_capabilities !== canonical(capabilities) || !this.online(workerId);
        this.ctx.storage.sql.exec("UPDATE workers SET readiness=?,transport_capabilities=?,last_seen=? WHERE id=?", canonical(readiness), canonical(capabilities), Date.now(), workerId);
        if (changed) this.workerUiChanged(workerId);
        await this.bindReadySetup(workerId);
      } else {
        const job = this.job(String(packet.delegation_id));
        if (job.worker_id !== workerId) throw new RelayError("not_found", "Delegation does not belong to this worker", 404);
        if (packet.kind === "receipt") {
          const localId = ID.parse(packet.local_id);
          if (job.local_id && job.local_id !== localId) throw new RelayError("identity_conflict", "Worker cannot replace a task identity", 409);
          this.ctx.storage.sql.exec("UPDATE jobs SET local_id=?,payload='{}',status=CASE WHEN status='cancelling' THEN status ELSE 'running' END WHERE id=? AND terminal=0", localId, job.id);
          this.ctx.storage.sql.exec("UPDATE commands SET state='done',payload='{}' WHERE id=? AND job_id=?", job.id, job.id);
          this.wakeUi(job.id);
        } else if (packet.kind === "event") {
          await this.acceptEvent(job, packet.event, packet.result);
        } else if (packet.kind === "recovery_required") {
          this.ctx.storage.sql.exec("UPDATE jobs SET status='unknown' WHERE id=? AND terminal=0", job.id);
          this.ctx.storage.sql.exec("UPDATE commands SET state='recovery_required',payload='{}' WHERE id=? AND job_id=?", job.id, job.id);
          if (!job.terminal && job.sequence < MAX_DECISIONS) await this.acceptEvent(job,
            { sequence: 1, kind: "recovery_required", terminal: false, task_type: this.taskType(job), verification_required: false,
              summary: "Worker recovery is required. This leased task has no matching live owner or local journal. Inspect saved work; no inference was restarted." }, undefined, "recovery-required");
        } else if (packet.kind === "command_result") {
          const command = this.one<CommandRow>("SELECT * FROM commands WHERE id=? AND job_id=? AND worker_id=?", ID.parse(packet.command_id), job.id, workerId);
          if (!command) throw new RelayError("not_found", "Owned command not found", 404);
          const failure=packet.error ? z.object({code:z.string().regex(/^[a-z_]{1,32}$/),message:z.string().max(240)}).strict().parse(packet.error) : null;
          const result=failure ? {error:failure} : object(packet.result ?? {});
          if(canonical(result).length>MAX_RESULT_CHARS) throw new RelayError("result_too_large","Command result exceeds its limit");
          if (command.operation === "artifact") {
            const expected=ID.parse(object(JSON.parse(command.payload)).artifact_id);
            const artifact = failure ? {id:expected,error:failure} : object(packet.artifact);
            const id = ID.parse(artifact.id);
            if (id !== expected) throw new RelayError("artifact_identity", "Worker returned a different artifact");
            if (canonical(artifact).length > 24_000) throw new RelayError("artifact_too_large", "Artifact transfer exceeds limit");
            this.ctx.storage.sql.exec("INSERT OR REPLACE INTO artifacts(job_id,id,payload) VALUES(?,?,?)", job.id, id, canonical(artifact));
          }
          this.ctx.storage.sql.exec("UPDATE commands SET state=?,result=?,payload=CASE WHEN operation='artifact' THEN payload ELSE '{}' END WHERE id=?",failure?"failed":"done",canonical(result),command.id);
          this.wake(job.id);
          this.wakeUi(job.id);
        } else throw new RelayError("invalid_packet", "Unsupported worker message");
      }
  }

  private async acceptEvent(job: JobRow, raw: unknown, result?: unknown, controlKey?: string): Promise<void> {
    const parsed = eventSchema.parse(raw);
    if (!parsed.terminal && parsed.summary.length > 600) throw new RelayError("event_too_large", "Decision summary exceeds 600 characters");
    if (parsed.task_type !== this.taskType(job)) throw new RelayError("task_type_conflict", "Worker event changed the task type");
    const sourceFingerprint=await digest(canonical({event:parsed,result:parsed.terminal?object(result):null}));
    job=this.job(job.id);
    this.checkJobWorker(job);
    const sourceKey = controlKey ? "control:" + controlKey : "worker:" + parsed.sequence;
    if (!controlKey && !parsed.terminal) {
      const fingerprint = await digest(canonical(parsed));
      job = this.job(job.id);
      const suppressed = this.one<{fingerprint:string}>("SELECT fingerprint FROM suppressed_events WHERE job_id=? AND worker_sequence=?",job.id,parsed.sequence);
      if (suppressed) {
        if (suppressed.fingerprint !== fingerprint) throw new RelayError("event_conflict","Event ID was reused for a different payload");
        return;
      }
      // A relay recovery notice shares the parent's eight-decision budget. If
      // it consumes a slot, accept/ACK extra native decisions privately so a
      // terminal result can still reach the parent without a stuck outbox.
      if (!job.terminal && parsed.sequence === job.worker_sequence + 1 && parsed.sequence <= MAX_DECISIONS && job.sequence >= MAX_DECISIONS) {
        if(parsed.model && job.model_pin && parsed.model !== job.model_pin) throw new RelayError("model_pin","Worker event changed the pinned model");
        this.ctx.storage.transactionSync(()=>{
          this.ctx.storage.sql.exec("INSERT INTO suppressed_events(job_id,worker_sequence,fingerprint) VALUES(?,?,?)",job.id,parsed.sequence,fingerprint);
          this.ctx.storage.sql.exec("UPDATE jobs SET worker_sequence=?,model_pin=COALESCE(model_pin,?) WHERE id=?",parsed.sequence,parsed.model ?? null,job.id);
        });
        return;
      }
    }
    const previous = this.one<EventRow>("SELECT sequence,payload,source_fingerprint FROM events WHERE job_id=? AND source_key=?", job.id, sourceKey);
    const sequence = previous?.sequence ?? job.sequence + 1;
    const event = compactEvent({ ...parsed, sequence, delegation_id: job.id, event_id: `${job.id}:${sequence}`, workspace_id: job.workspace_id });
    const encoded = canonical(event);
    if (previous) {
      if (previous.source_fingerprint !== sourceFingerprint || previous.payload !== encoded) throw new RelayError("event_conflict", "Event ID was reused for a different payload");
      return;
    }
    if (job.terminal || (!controlKey && parsed.sequence !== job.worker_sequence + 1) || (!parsed.terminal && sequence > MAX_DECISIONS))
      throw new RelayError("event_sequence", "Event violates the decision/terminal sequence budget");
    if (parsed.model && job.model_pin && parsed.model !== job.model_pin) throw new RelayError("model_pin", "Worker event changed the pinned model");
    const handoff = parsed.terminal ? canonical(object(result)) : null;
    if (handoff && handoff.length > MAX_RESULT_CHARS) throw new RelayError("result_too_large", "Saved handoff exceeds 8,000 characters");
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec("INSERT INTO events(job_id,sequence,payload,occurred,source_key,source_fingerprint) VALUES(?,?,?,?,?,?)", job.id, sequence, encoded, Date.now(), sourceKey,sourceFingerprint);
      this.ctx.storage.sql.exec("UPDATE jobs SET sequence=?,status=?,terminal=?,result=COALESCE(?,result),payload=CASE WHEN ? THEN '{}' ELSE payload END WHERE id=?",
        sequence, parsed.kind, parsed.terminal ? 1 : 0, handoff, parsed.terminal ? 1 : 0, job.id);
      if (!controlKey) this.ctx.storage.sql.exec("UPDATE jobs SET worker_sequence=?,model_pin=COALESCE(model_pin,?) WHERE id=?", parsed.sequence, parsed.model ?? null, job.id);
      if (parsed.terminal) this.ctx.storage.sql.exec("UPDATE jobs SET payload=? WHERE id=?", canonical({ task_type: parsed.task_type, model: parsed.model ?? null }), job.id);
    });
    this.wake(job.id);
    this.wakeUi(job.id);
    const subscribed = this.one<{ count: number }>("SELECT COUNT(*) AS count FROM subscriptions WHERE job_id=? AND state='active'", job.id)?.count;
    if (subscribed) await this.schedule(Date.now() + 100);
  }

  async subscribe(raw: unknown, authToken: string, tokenExpiry: number): Promise<ObjectValue> {
    const parsed = z.object({ name: z.literal("astra.delegation.updated"), arguments: z.object({ delegation_id: ID }).strict(),
      delivery: z.object({ mode: z.literal("webhook"), url: z.string().max(2_048), secret: z.string().max(100) }).strict(),
      cursor: z.string().nullable().optional(), ttlMs: z.number().int().min(1_000).max(3_600_000).nullable().optional() }).strict().parse(raw);
    const job = this.job(parsed.arguments.delegation_id);
    this.checkJobWorker(job);
    callbackUrl(parsed.delivery.url, this.env);
    signingKey(parsed.delivery.secret);
    const id = "sub_" + await digest(canonical({ owner: this.ctx.id.toString(), name: parsed.name, job: job.id, url: parsed.delivery.url }));
    const previous = this.one<SubscriptionRow>("SELECT * FROM subscriptions WHERE id=?", id);
    if (!previous && (this.one<{count:number}>("SELECT COUNT(*) AS count FROM subscriptions")?.count ?? 0) >= 64) throw new RelayError("subscription_limit", "Account subscription retention limit reached", 429);
    const previousSecrets = previous?.state === "active" && previous.secret ? await unseal(previous.secret, this.env.SESSION_SECRET) : null;
    let verified = previous?.verified ?? 0;
    if (!previousSecrets || (previous?.expires ?? 0) <= Date.now() || verified < Date.now() - 300_000 || !equal(String(previousSecrets.signing_secret), parsed.delivery.secret)) {
      const challenge = token();
      let response: Response;
      try { response = await signedCallback(parsed.delivery.url, parsed.delivery.secret, id, "verify_" + token(16),
        JSON.stringify({ type: "verification", challenge }), this.env); }
      catch { throw new RelayError("callback_verification","Callback verification timed out or could not connect",400); }
      if (!response.ok) { await response.body?.cancel(); throw new RelayError("callback_verification", "Callback verification failed", 400); }
      const payload = object(await boundedJson(new Request("https://bounded.invalid", { method: "POST", body: response.body })));
      if (typeof payload.challenge !== "string" || !equal(payload.challenge, challenge)) throw new RelayError("callback_verification", "Callback did not return the challenge", 400);
      verified = Date.now();
    }
    // Revalidate access after the network wait; revocation may have occurred.
    this.checkJobWorker(this.job(job.id));
    await this.subscriptionAuthorization(authToken);
    const expires = Math.min(tokenExpiry * 1_000, Date.now() + (parsed.ttlMs ?? 3_600_000));
    if (!Number.isFinite(expires) || expires <= Date.now()) throw new RelayError("token_expired", "Subscription authorization expired", 401);
    const rotation = previousSecrets && !equal(String(previousSecrets.signing_secret), parsed.delivery.secret)
      ? { previous_secret: previousSecrets.signing_secret, rotation_until: Date.now() + 60_000 }
      : previousSecrets && Number(previousSecrets.rotation_until) > Date.now()
        ? { previous_secret: previousSecrets.previous_secret, rotation_until: previousSecrets.rotation_until } : {};
    const saved = await seal({ signing_secret: parsed.delivery.secret, token: authToken, ...rotation }, this.env.SESSION_SECRET);
    this.checkJobWorker(this.job(job.id));
    const current = this.one<SubscriptionRow>("SELECT * FROM subscriptions WHERE id=?",id);
    const cursor = parsed.cursor == null ? current?.cursor ?? 0 : Number(parsed.cursor);
    if (!Number.isSafeInteger(cursor) || cursor < 0 || cursor > this.job(job.id).sequence) throw new RelayError("invalid_cursor", "Subscription cursor is outside the saved archive");
    this.ctx.storage.sql.exec("INSERT INTO subscriptions(id,job_id,url,secret,expires,cursor,state,generation,next_attempt,verified) VALUES(?,?,?,?,?,?,'active',?,?,?) ON CONFLICT(id) DO UPDATE SET secret=excluded.secret,expires=excluded.expires,cursor=excluded.cursor,state='active',attempts=0,generation=excluded.generation,next_attempt=excluded.next_attempt,verified=excluded.verified",
      id, job.id, parsed.delivery.url, saved, expires, cursor, token(16), Date.now(), verified);
    this.wakeUi(job.id);
    await this.schedule(Math.min(Date.now() + 100, expires));
    return { id, refreshBefore: new Date(expires).toISOString(), cursor: String(cursor), truncated: false };
  }
  async unsubscribe(raw: unknown): Promise<ObjectValue> {
    const parsed = z.object({ name: z.literal("astra.delegation.updated"), arguments: z.object({ delegation_id: ID }).strict(),
      delivery: z.object({ mode: z.literal("webhook"), url: z.string().max(2_048) }).strict() }).strict().parse(raw);
    const job = this.job(parsed.arguments.delegation_id);
    this.checkJobWorker(job);
    const id = "sub_" + await digest(canonical({ owner: this.ctx.id.toString(), name: parsed.name, job: job.id, url: parsed.delivery.url }));
    this.ctx.storage.sql.exec("DELETE FROM subscriptions WHERE id=?", id);
    this.wakeUi(job.id);
    return {};
  }
  private async subscriptionAuthorization(authToken: string): Promise<void> {
    const authorized = await authorizationServer(this.env).validateToken<AuthProps>(publicOrigin(this.env) + "/mcp", authToken, this.env);
    if (!authorized || authorized.props.ownerKey !== this.one<{ owner_key: string }>("SELECT owner_key FROM identity")?.owner_key || !authorized.scope?.includes("tasks:read"))
      throw new RelayError("subscription_revoked", "Subscription authorization was revoked", 401);
  }
  private async deliver(subscription: SubscriptionRow): Promise<void> {
    const update = (clause: string, ...args: SqlStorageValue[]) => this.ctx.storage.sql.exec(
      `UPDATE subscriptions SET ${clause} WHERE id=? AND generation=? AND state='active'`, ...args, subscription.id, subscription.generation);
    try {
      if (subscription.expires <= Date.now()) throw new RelayError("subscription_expired", "Subscription authorization expired", 401);
      const job = this.job(subscription.job_id);
      this.checkJobWorker(job);
      const secrets = await unseal(subscription.secret, this.env.SESSION_SECRET);
      await this.subscriptionAuthorization(String(secrets.token));
      // An unsubscribe, refresh or worker revocation can arrive during crypto
      // or token validation. Old delivery attempts cannot mutate the new cursor.
      const current = this.one<SubscriptionRow>("SELECT * FROM subscriptions WHERE id=?", subscription.id);
      if (!current || current.state !== "active" || current.generation !== subscription.generation || current.cursor !== subscription.cursor) return;
      if (current.expires <= Date.now()) throw new RelayError("subscription_expired", "Subscription authorization expired", 401);
      this.checkJobWorker(this.job(job.id));
      const pending = this.one<EventRow & { occurred: number }>("SELECT sequence,payload,occurred FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 1", job.id, subscription.cursor);
      if (!pending) return;
      const event = object(JSON.parse(pending.payload));
      const body = JSON.stringify({ eventId: event.event_id, name: "astra.delegation.updated", timestamp: new Date(pending.occurred).toISOString(),
        data: event, cursor: String(pending.sequence) });
      const response = await signedCallback(subscription.url, String(secrets.signing_secret), subscription.id, String(event.event_id), body, this.env,
        Number(secrets.rotation_until) > Date.now() && typeof secrets.previous_secret === "string" ? secrets.previous_secret : undefined);
      if (response.ok) update("cursor=MAX(cursor,?),attempts=0,next_attempt=?", pending.sequence, Date.now());
      else if ([410, 413].includes(response.status)) update("state='stopped',secret='' ");
      else update("attempts=attempts+1,next_attempt=?", Date.now() + Math.min(60_000, 1_000 * 2 ** subscription.attempts));
      await response.body?.cancel();
    } catch (error) {
      if (error instanceof RelayError && [401, 404, 409].includes(error.status)) update("state='revoked',secret='' ");
      else update("attempts=attempts+1,next_attempt=?", Date.now() + Math.min(60_000, 1_000 * 2 ** subscription.attempts));
    }
    this.ctx.storage.sql.exec("UPDATE subscriptions SET state='suspended',secret='' WHERE id=? AND generation=? AND attempts>=5", subscription.id, subscription.generation);
    if (this.one<SubscriptionRow>("SELECT * FROM subscriptions WHERE id=?", subscription.id)?.state !== subscription.state) this.wakeUi(subscription.job_id);
  }

  async webSocketClose(ws: WebSocket, code: number): Promise<void> {
    ws.close(code);
    if (this.activeDownload?.socket === ws) this.finishDownload(new RelayError("download_disconnected", "Worker disconnected; request the same artifact again", 409));
    const connection = ws.deserializeAttachment() as Connection | null;
    if (connection && !this.connection(connection.worker_id)) this.ctx.storage.sql.exec(
      "UPDATE jobs SET status='unknown' WHERE worker_id=? AND terminal=0 AND status NOT IN ('queued','cancelling')", connection.worker_id);
    if (connection) this.workerUiChanged(connection.worker_id);
  }
  async webSocketError(ws: WebSocket): Promise<void> { await this.webSocketClose(ws, 1011); }

  async alarm(): Promise<void> {
    const now = Date.now();
    this.expireRemovalOperations(now);
    for(const row of this.ctx.storage.sql.exec<{id:string}>("SELECT id FROM workers WHERE approval_mode='until_revoked' AND access_expires<=? AND revoked=0",now).toArray()){
      for(const socket of this.ctx.getWebSockets(row.id))socket.close(4001,"Access credential expired; refresh the same computer grant");
    }
    if (this.activeDownload && (this.one<DownloadRow>("SELECT * FROM download_grants WHERE id=?", this.activeDownload.id)?.expires ?? 0) <= now)
      this.finishDownload(new RelayError("download_expired", "Download expired", 410));
    const expiredWorkers = this.ctx.storage.sql.exec<{ id: string }>("SELECT id FROM workers WHERE expires<=?", now).toArray();
    const expiredJobs = this.ctx.storage.sql.exec<{ id: string }>("SELECT id FROM jobs WHERE expires<=?", now).toArray();
    const expiredSubscriptions = this.ctx.storage.sql.exec<{ job_id: string }>("SELECT job_id FROM subscriptions WHERE state='active' AND expires<=?", now).toArray();
    this.ctx.storage.transactionSync(() => {
      this.ctx.storage.sql.exec("UPDATE subscriptions SET state='expired',secret='' WHERE state='active' AND expires<=?", now);
      this.ctx.storage.sql.exec("UPDATE subscriptions SET state='revoked',secret='' WHERE job_id IN (SELECT j.id FROM jobs j JOIN workers w ON j.worker_id=w.id WHERE w.expires<=?)",now);
      this.ctx.storage.sql.exec("UPDATE jobs SET status='unknown' WHERE terminal=0 AND worker_id IN (SELECT id FROM workers WHERE expires<=?)",now);
      for (const row of this.ctx.storage.sql.exec<{id:string}>("SELECT id FROM workers WHERE expires<=?",now).toArray())
        for (const socket of this.ctx.getWebSockets(row.id)) socket.close(4003,"Worker pairing expired; inspect work locally");
      // Expiry is recoverable; explicit revocation remains recorded during the
      // same bounded window so it can never masquerade as an expired identity.
      this.ctx.storage.sql.exec("DELETE FROM workers WHERE expires+?<=? AND NOT EXISTS(SELECT 1 FROM jobs WHERE jobs.worker_id=workers.id AND jobs.expires>?)", RENEWAL_WINDOW_MS, now, now);
      this.ctx.storage.sql.exec("INSERT OR IGNORE INTO retired_requests(request_id,job_id,fingerprint,expires,worker_id) SELECT request_id,id,fingerprint,?,worker_id FROM jobs WHERE expires<=?", now + 30 * 86_400_000, now);
      for (const table of ["commands", "events", "suppressed_events", "artifacts", "subscriptions"]) this.ctx.storage.sql.exec(
        `DELETE FROM ${table} WHERE job_id IN (SELECT id FROM jobs WHERE expires<=?)`, now);
      this.ctx.storage.sql.exec("DELETE FROM jobs WHERE expires<=?", now);
      this.ctx.storage.sql.exec("DELETE FROM retired_requests WHERE expires<=?", now);
      this.ctx.storage.sql.exec("DELETE FROM download_grants WHERE expires<=? OR job_id NOT IN (SELECT id FROM jobs)", now);
      this.ctx.storage.sql.exec("DELETE FROM worker_renewals WHERE expires<=? OR worker_id NOT IN (SELECT id FROM workers)", now);
      this.ctx.storage.sql.exec("DELETE FROM credential_rotations WHERE expires<=? OR worker_id NOT IN (SELECT id FROM workers)",now);
      this.ctx.storage.sql.exec("DELETE FROM computer_messages WHERE created<?",now-86_400_000);
      this.ctx.storage.sql.exec("UPDATE computer_operations SET state='failed',summary='Confirmation expired. No change was applied.',parameters='{}',updated=? WHERE state='prepared' AND expires<=?",now,now);
      this.ctx.storage.sql.exec("DELETE FROM computer_operations WHERE state IN ('completed','failed','revoked','unconfirmed') AND updated<?",now-30*86_400_000);
    });
    for (const worker of expiredWorkers) this.workerUiChanged(worker.id);
    for (const job of expiredJobs) this.wakeUi(job.id);
    for (const subscription of expiredSubscriptions) this.wakeUi(subscription.job_id);
    this.wake("ui:computers");
    const deliveries = this.ctx.storage.sql.exec<SubscriptionRow>("SELECT s.* FROM subscriptions s JOIN jobs j ON s.job_id=j.id WHERE s.state='active' AND s.cursor<j.sequence AND s.next_attempt<=? ORDER BY s.next_attempt,s.rowid LIMIT 4", now).toArray();
    for (const subscription of deliveries) await this.deliver(subscription);
    // SQLite on this runtime permits at most five compound SELECT terms.
    const deadlines = [
      this.one<{ at: number | null }>("SELECT MIN(expires) AS at FROM jobs")?.at,
      this.one<{ at: number | null }>("SELECT MIN(expires) AS at FROM retired_requests")?.at,
      this.one<{ at: number | null }>("SELECT MIN(CASE WHEN expires>? THEN expires ELSE expires+? END) AS at FROM workers WHERE approval_mode='legacy_30_day'", now, RENEWAL_WINDOW_MS)?.at,
      this.one<{ at: number | null }>("SELECT MIN(expires) AS at FROM download_grants")?.at,
      this.one<{ at: number | null }>("SELECT MIN(expires) AS at FROM worker_renewals")?.at,
      this.one<{ at:number|null }>("SELECT MIN(expires) AS at FROM credential_rotations")?.at,
      this.one<{ at:number|null }>("SELECT MIN(expires) AS at FROM computer_operations WHERE state='prepared'")?.at,
      this.one<{ at:number|null }>("SELECT MIN(access_expires) AS at FROM workers WHERE approval_mode='until_revoked' AND revoked=0 AND access_expires>?",now)?.at,
      this.one<{ at:number|null }>("SELECT MIN(updated+2592000000) AS at FROM computer_operations WHERE state IN ('completed','failed','revoked','unconfirmed')")?.at,
      this.one<{ at:number|null }>("SELECT MIN(created+86400000) AS at FROM computer_messages")?.at,
      this.one<{ at:number|null }>("SELECT MIN(completion_expires) AS at FROM computer_operations WHERE action='uninstall' AND state='pending' AND completion_expires>0")?.at,
      this.one<{ at: number | null }>("SELECT MIN(expires) AS at FROM subscriptions WHERE state='active'")?.at,
      this.one<{ at: number | null }>("SELECT MIN(s.next_attempt) AS at FROM subscriptions s JOIN jobs j ON s.job_id=j.id WHERE s.state='active' AND s.cursor<j.sequence")?.at
    ].filter((at): at is number => typeof at === "number");
    if (deadlines.length) await this.schedule(Math.max(Date.now() + 100, Math.min(...deadlines)));
  }
}

/** A short-lived device pairing; claims are bound to the operator's browser. */
export class WorkerPairing extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ctx.blockConcurrencyWhile(async () => {
      this.ctx.storage.sql.exec(`CREATE TABLE IF NOT EXISTS pairing(
        id INTEGER PRIMARY KEY,input TEXT NOT NULL,claim_hash TEXT NOT NULL,expires INTEGER NOT NULL,owner_key TEXT,worker_id TEXT,decision TEXT)`);
      if (!this.ctx.storage.sql.exec<{name:string}>("PRAGMA table_info(pairing)").toArray().some(row => row.name === "decision"))
        this.ctx.storage.sql.exec("ALTER TABLE pairing ADD COLUMN decision TEXT");
    });
  }
  private saved(): { input: string; claim_hash: string; expires: number; owner_key: string | null; worker_id: string | null; decision: string | null } {
    const row = this.ctx.storage.sql.exec<{ input: string; claim_hash: string; expires: number; owner_key: string | null; worker_id: string | null; decision:string|null }>(
      "SELECT * FROM pairing WHERE id=1").toArray()[0];
    if (!row || row.expires <= Date.now()) throw new RelayError("pairing_expired", "Pairing code expired", 404);
    return row;
  }
  async create(input: PairingInput): Promise<{ claim: string; expires: number }> {
    const claim = token();
    const fingerprint = await digest(claim);
    const expires = Date.now() + 600_000;
    if (this.ctx.storage.sql.exec("SELECT id FROM pairing").toArray().length) throw new RelayError("pairing_conflict", "Pairing already exists", 409);
    this.ctx.storage.sql.exec("INSERT INTO pairing(id,input,claim_hash,expires) VALUES(1,?,?,?)", canonical(pairingSchema.parse(input)), fingerprint, expires);
    await this.ctx.storage.setAlarm(expires);
    return { claim, expires };
  }
  async details(): Promise<PairingInput> {
    return pairingSchema.parse(JSON.parse(this.saved().input));
  }
  async approve(ownerKey: string,untilRevoked=false,requestId?:string): Promise<{worker_id:string}> {
    if (!/^[a-f0-9]{64}$/.test(ownerKey)) throw new RelayError("invalid_owner", "Verified account identity required");
    const saved = this.saved();
    if (saved.decision === "declined") throw new RelayError("pairing_declined", "Pairing was declined", 409);
    if (saved.owner_key && saved.owner_key !== ownerKey) throw new RelayError("pairing_used", "Pairing was claimed by another account", 409);
    const input = {...pairingSchema.parse(JSON.parse(saved.input)),approval_mode:untilRevoked?"until_revoked" as const:"legacy_30_day" as const}, owner = this.env.OWNERS.getByName(ownerKey);
    if(requestId){ID.parse(requestId);if(input.setup_request_id&&input.setup_request_id!==requestId)throw new RelayError("setup_conflict","Pairing code belongs to another saved setup request",409);if(saved.worker_id&&!input.setup_request_id)throw new RelayError("setup_conflict","An approved pairing cannot acquire a new saved task",409);input.setup_request_id=requestId;}
    if (input.setup_request_id) await owner.setupDetails(input.setup_request_id);
    // Check task ownership before reserving the pairing, so a signed-in account
    // cannot claim another account's setup request.
    const current = this.saved();
    if (current.decision === "declined" || (current.owner_key && current.owner_key !== ownerKey))
      throw new RelayError("pairing_used", "Pairing is no longer available for this account", 409);
    const currentInput=pairingSchema.parse(JSON.parse(current.input));
    if(currentInput.setup_request_id&&currentInput.setup_request_id!==input.setup_request_id)throw new RelayError("setup_conflict","Pairing already names a different saved setup request",409);
    this.ctx.storage.sql.exec("UPDATE pairing SET owner_key=?,input=? WHERE id=1", ownerKey,canonical(input));
    const workerId = saved.worker_id ?? (await owner.registerWorker(input, ownerKey, this.ctx.id.toString())).worker_id;
    if (input.setup_request_id&&input.workspaces.length>0) await owner.bindPrepared(input.setup_request_id, workerId);
    this.ctx.storage.sql.exec("UPDATE pairing SET worker_id=? WHERE id=1 AND owner_key=?", workerId, ownerKey);
    return {worker_id:workerId};
  }
  async decline(): Promise<void> {
    const saved = this.saved();
    if (saved.owner_key || saved.worker_id) throw new RelayError("pairing_used", "An approved pairing cannot be declined", 409);
    this.ctx.storage.sql.exec("UPDATE pairing SET decision='declined' WHERE id=1");
  }
  async status(claim: string): Promise<ObjectValue> {
    const hash = await digest(claim);
    const saved = this.saved();
    if (!equal(hash, saved.claim_hash)) throw new RelayError("invalid_pairing_claim", "Pairing proof is invalid", 401);
    return saved.decision === "declined" ? {status:"declined"} : saved.owner_key && saved.worker_id ? { status: "approved", owner_key: saved.owner_key, worker_id: saved.worker_id,...object(await this.env.OWNERS.getByName(saved.owner_key).workerGrant(saved.worker_id)) } : { status: "pending" };
  }
  async alarm(): Promise<void> { await this.ctx.storage.deleteAll(); }
}
