import { env, SELF, runInDurableObject } from "cloudflare:test";
import { describe, expect, it, vi } from "vitest";
import { digest } from "../src/contracts";
import type { OwnerRelay } from "../src/broker";
import { authorizationServer } from "../src/auth";

declare module "cloudflare:test" { interface ProvidedEnv extends Env {} }
const ORIGIN = "https://relay.example.test";
const SECRET = "a".repeat(64);
async function bounded<T>(promise: Promise<T>, stage: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  try { return await Promise.race([promise, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("Timed out: " + stage)), 3_000); })]); }
  finally { clearTimeout(timer!); }
}
async function owner() {
  const key = await digest(crypto.randomUUID()), stub = env.OWNERS.getByName(key);
  const worker = await stub.registerWorker({ label: "My computer", credential_hash: await digest(SECRET), workspaces: [{ id: "project", label: "My project",
    capabilities: ["local_files", "shell"], isolation: ["worktree"], task_types: ["implementation", "document"] }] }, key, crypto.randomUUID());
  const receipt = await stub.tool("delegate_via_luna", { workspace_id: "project", request_id: "saved", task: "Create the authorized report" });
  return { key, stub, workerId: worker.worker_id, jobId: String(receipt.delegation_id) };
}
async function failure(stub: DurableObjectStub<OwnerRelay>, action: (instance: OwnerRelay) => Promise<unknown>) {
  return runInDurableObject(stub, async instance => {
    try { await action(instance); return "NO ERROR"; } catch (error) { return error instanceof Error ? error.message : String(error); }
  });
}
async function complete(a: Awaited<ReturnType<typeof owner>>, artifact?: Record<string, unknown>) {
  await a.stub.workerHttp(a.workerId, SECRET, { kind: "receipt", delegation_id: a.jobId, local_id: "provider-task" });
  await a.stub.workerHttp(a.workerId, SECRET, { kind: "event", delegation_id: a.jobId, event: {
    sequence: 1, kind: "completed", terminal: true, summary: "Worker finished", task_type: "implementation", verification_required: true },
    result: { status: "completed", implementation: { summary: "Saved result", tests_run: ["Worker check passed"] }, ...(artifact ? { artifact_refs: [artifact] } : {}) } });
}
async function socketFor(a: Awaited<ReturnType<typeof owner>>) {
  const response = await SELF.fetch(ORIGIN + "/worker/connect", { headers: { Upgrade: "websocket", Authorization: `Bearer ${a.key}.${a.workerId}.${SECRET}` } });
  expect(response.status).toBe(101);
  const socket = response.webSocket!; socket.accept();
  const packets: Record<string, unknown>[] = [], waiters: Array<{ kind: string; resolve: (packet: Record<string, unknown>) => void }> = [];
  socket.addEventListener("message", event => {
    const packet = JSON.parse(String(event.data));
    const at = waiters.findIndex(item => item.kind === packet.kind);
    if (at >= 0) waiters.splice(at, 1)[0].resolve(packet); else packets.push(packet);
  });
  const next = (kind: string): Promise<Record<string, unknown>> => {
    const at = packets.findIndex(item => item.kind === kind);
    return at >= 0 ? Promise.resolve(packets.splice(at, 1)[0]) : new Promise(resolve => waiters.push({ kind, resolve }));
  };
  await a.stub.workerHttp(a.workerId, SECRET, { kind: "readiness", readiness: {}, transport_capabilities: {
    artifact_stream_v1: true, max_artifact_bytes: 20 * 1024 * 1024, artifact_chunk_bytes: 16 * 1024 } });
  return { socket, next };
}
async function issuedToken(ownerKey: string) {
  const userId = crypto.randomUUID(), server = authorizationServer(env), helpers = server.getOAuthApi(env);
  const client = await helpers.createClient({ clientName: "Owned UI fixture", redirectUris: ["http://localhost:12345/callback"], tokenEndpointAuthMethod: "none", grantTypes: ["authorization_code"], responseTypes: ["code"] });
  const verifier = "s".repeat(64), hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(hash))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const grant = await helpers.completeAuthorization({ request: { responseType: "code", clientId: client.clientId, redirectUri: client.redirectUris[0], scope: ["tasks:read"], state: "fixture", resource: ORIGIN + "/mcp", issuer: ORIGIN, codeChallenge: challenge, codeChallengeMethod: "S256" }, userId, scope: ["tasks:read"], metadata: {}, props: { userId, ownerKey } });
  const response = await SELF.fetch(ORIGIN + "/oauth/token", { method: "POST", body: new URLSearchParams({ grant_type: "authorization_code", client_id: client.clientId, code: new URL(grant.redirectTo).searchParams.get("code")!, code_verifier: verifier, redirect_uri: client.redirectUris[0], resource: ORIGIN + "/mcp" }) });
  expect(response.status).toBe(200);
  return { token: (await response.json<{ access_token: string }>()).access_token, userId, helpers };
}

describe("component snapshots and owned watchers", () => {
  it("keeps revisions stable, never acknowledges parent events, and isolates accounts", async () => {
    const a = await owner(), b = await owner();
    await complete(a);
    const first = await a.stub.uiSnapshot(a.jobId), again = await a.stub.watchUi(a.jobId, first.revision, 0);
    expect(again.revision).toBe(first.revision);
    expect(first).toMatchObject({ terminal: true, outcome: "Saved result", worker_checks: ["Worker check passed"], notifications: { active: false } });
    expect(first.task_summary).toBe("Create the authorized report");
    expect(first).not.toHaveProperty("review_state");
    expect((await a.stub.tool("inspect_delegation", { delegation_id: a.jobId })).acknowledged_sequence).toBe(0);
    expect(await failure(b.stub, instance => instance.uiSnapshot(a.jobId))).toContain("Owned delegation not found");
  });
  it("reports a subscription only while its actual OAuth grant is valid, without changing delivery", async () => {
    const a = await owner(), auth = await issuedToken(a.key);
    const callback = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url, options) => Response.json({ challenge: JSON.parse(String(options?.body)).challenge }));
    try {
      await a.stub.subscribe({ name: "astra.delegation.updated", arguments: { delegation_id: a.jobId }, delivery: { mode: "webhook", url: "https://chatgpt.com/callback-test", secret: "whsec_" + btoa("s".repeat(32)) } }, auth.token, Math.floor(Date.now() / 1000) + 3600);
      const before = await a.stub.uiSnapshot(a.jobId);
      expect(before.notifications).toMatchObject({ active: true, message: "An authorized notification subscription exists for this task." });
      const grants = await auth.helpers.listUserGrants(auth.userId);
      for (const grant of grants.items) await auth.helpers.revokeGrant(grant.id, auth.userId);
      const after = await a.stub.uiSnapshot(a.jobId);
      expect(after.notifications.active).toBe(false);
      expect(after.revision).not.toBe(before.revision);
      const stored = await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec("SELECT state,cursor FROM subscriptions").toArray());
      expect(stored).toEqual([{ state: "active", cursor: 0 }]);
      expect(callback).toHaveBeenCalledTimes(1);
    } finally { callback.mockRestore(); }
  });
  it("wakes an existing card on a worker receipt without consuming a delivery cursor", async () => {
    const a = await owner(), initial = await a.stub.uiSnapshot(a.jobId);
    const waiting = a.stub.watchUi(a.jobId, initial.revision, 20_000);
    await a.stub.workerHttp(a.workerId, SECRET, { kind: "receipt", delegation_id: a.jobId, local_id: "provider-task" });
    expect(await waiting).toMatchObject({ status: "running" });
    expect((await a.stub.tool("inspect_delegation", { delegation_id: a.jobId })).acknowledged_sequence).toBe(0);
  });
  it("allows retained terminal reads after expiry, hides revoked output, and preserves the renewal identity", async () => {
    const a = await owner(); await complete(a);
    await runInDurableObject(a.stub, async (instance, state) => {
      state.storage.sql.exec("UPDATE workers SET expires=? WHERE id=?", Date.now() - 1, a.workerId);
      await instance.alarm();
    });
    expect((await a.stub.uiSnapshot(a.jobId)).connection.state).toBe("expired");
    expect((await a.stub.tool("read_delegation_result", { delegation_id: a.jobId })).handoff).toBeDefined();
    const nextHash = await digest("b".repeat(64));
    const renewal = await a.stub.beginWorkerRenewal(a.workerId, SECRET, nextHash);
    expect((await a.stub.beginWorkerRenewal(a.workerId, SECRET, nextHash)).renewal_id).toBe(renewal.renewal_id);
    expect(await failure(a.stub, instance => instance.approveWorkerRenewal(renewal.renewal_id, "wrong-owner"))).toContain("account that connected this computer");
    await a.stub.approveWorkerRenewal(renewal.renewal_id, a.key);
    expect(await a.stub.claimWorkerRenewal(renewal.renewal_id, SECRET, a.workerId)).toMatchObject({ status: "approved", worker_id: a.workerId });
    expect((await a.stub.uiSnapshot(a.jobId)).delegation_id).toBe(a.jobId);
    expect(await failure(a.stub, instance => instance.workerHttp(a.workerId, SECRET, { kind: "readiness", readiness: {} }))).toContain("authentication failed");
    await a.stub.revokeWorker(a.workerId);
    expect(await failure(a.stub, instance => instance.beginWorkerRenewal(a.workerId, "b".repeat(64), nextHash))).toContain("cannot be renewed");
    expect(await a.stub.uiSnapshot(a.jobId)).not.toHaveProperty("outcome");
  });
  it("declines renewal without credential rotation and rejects renewal outside 72 hours", async () => {
    const a = await owner(), renewal = await a.stub.beginWorkerRenewal(a.workerId, SECRET, await digest("b".repeat(64)));
    await a.stub.declineWorkerRenewal(renewal.renewal_id);
    expect(await a.stub.claimWorkerRenewal(renewal.renewal_id, SECRET)).toMatchObject({ status: "declined" });
    await a.stub.workerHttp(a.workerId, SECRET, { kind: "readiness", readiness: {} });
    await runInDurableObject(a.stub, async (_instance, state) => { state.storage.sql.exec("UPDATE workers SET expires=? WHERE id=?", Date.now() - 72 * 3_600_000 - 1, a.workerId); });
    expect(await failure(a.stub, instance => instance.beginWorkerRenewal(a.workerId, SECRET, "c".repeat(64)))).toContain("cannot be renewed");
  });
  it("retains approved rotation recovery for 72 hours and invalidates superseded claims", async () => {
    const a = await owner(), nextSecret = "b".repeat(64);
    const first = await a.stub.beginWorkerRenewal(a.workerId, SECRET, await digest(nextSecret));
    await a.stub.approveWorkerRenewal(first.renewal_id, a.key);
    const retained = await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec<{ expires: number }>("SELECT expires FROM worker_renewals WHERE id=?", first.renewal_id).toArray()[0]);
    expect(retained.expires).toBeGreaterThan(Date.now() + 71 * 3_600_000);
    const second = await a.stub.beginWorkerRenewal(a.workerId, nextSecret, await digest("c".repeat(64)));
    await a.stub.approveWorkerRenewal(second.renewal_id, a.key);
    expect(await failure(a.stub, instance => instance.claimWorkerRenewal(first.renewal_id, SECRET))).toContain("later renewal");
  });
  it("revokes an expired owned identity and removes its ability to renew", async () => {
    const a = await owner();
    await runInDurableObject(a.stub, async (_instance, state) => { state.storage.sql.exec("UPDATE workers SET expires=? WHERE id=?", Date.now() - 1, a.workerId); });
    expect(await a.stub.revokeWorker(a.workerId)).toMatchObject({ revoked: true, worker_id: a.workerId });
    expect(await a.stub.revokeWorker(a.workerId)).toMatchObject({ revoked: true });
    const retained = await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec("SELECT credential_hash,revoked FROM workers WHERE id=?", a.workerId).toArray()[0]);
    expect(retained).toEqual({ credential_hash: "", revoked: 1 });
    expect(await failure(a.stub, instance => instance.beginWorkerRenewal(a.workerId, SECRET, "b".repeat(64)))).toContain("cannot be renewed");
  });
  it("invalidates an old authenticated long poll before it can lease a new task", async () => {
    const a = await owner(); await complete(a);
    const pending = failure(a.stub, instance => instance.pullCommands(a.workerId, SECRET));
    await runInDurableObject(a.stub, async instance => {
      expect((instance as unknown as { waiters: Map<string, unknown> }).waiters.has("worker:" + a.workerId)).toBe(true);
    });
    const renewal = await a.stub.beginWorkerRenewal(a.workerId, SECRET, await digest("b".repeat(64)));
    await a.stub.approveWorkerRenewal(renewal.renewal_id, a.key);
    const next = await a.stub.tool("delegate_via_luna", { workspace_id: "project", request_id: "next", task: "Create another authorized report" });
    expect(await pending).toContain("authentication failed");
    const status = await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec("SELECT state FROM commands WHERE job_id=?", String(next.delegation_id)).toArray());
    expect(status).toEqual([{ state: "queued" }]);
  });
  it("offers Opus only from the explicit published availability result", async () => {
    const a = await owner();
    await a.stub.workerHttp(a.workerId, SECRET, { kind: "event", delegation_id: a.jobId, event: {
      sequence: 1, kind: "failed", terminal: true, summary: "Model unavailable", task_type: "implementation", verification_required: false },
      result: { status: "model_unavailable", model_selection: { preferred_profile: "argon-high", available: false,
        message: "Argon is unavailable", offered_fallback: { profile: "opus-5.5", available: true } } } });
    expect(await a.stub.uiSnapshot(a.jobId)).toMatchObject({ status: "model_unavailable", decision: { summary: "Argon is unavailable", opus_available: true } });
    await runInDurableObject(a.stub, async (_instance, state) => {
      state.storage.sql.exec("UPDATE jobs SET result=? WHERE id=?", JSON.stringify({ status: "model_unavailable", model_selection: { preferred_profile: "argon-high", available: false, message: "Ask about Opus", offered_fallback: { profile: "opus-5.5" } } }), a.jobId);
    });
    expect((await a.stub.uiSnapshot(a.jobId)).decision?.opus_available).toBe(false);
  });
  it("retires only explicitly retained expired task delivery and never recreates inference", async () => {
    const a = await owner();
    await runInDurableObject(a.stub, async (instance, state) => {
      state.storage.sql.exec("UPDATE jobs SET expires=? WHERE id=?", Date.now() - 1, a.jobId); await instance.alarm();
    });
    expect(await a.stub.workerHttp(a.workerId, SECRET, { kind: "event", delegation_id: a.jobId, message_id: "saved-event" }))
      .toEqual({ kind: "ack", message_id: "saved-event", disposition: "task_expired" });
    expect(await failure(a.stub, instance => instance.workerHttp(a.workerId, SECRET, { kind: "event", delegation_id: "unknown", message_id: "other" }))).toContain("Owned delegation not found");
    expect(await failure(a.stub, instance => instance.tool("delegate_via_luna", { workspace_id: "project", request_id: "saved", task: "Create the authorized report" }))).toContain("Saved task expired");
  });
});

describe("bounded artifact response streams", () => {
  it("preserves private host transfer for a cached legacy small artifact", async () => {
    const a = await owner(), sha256 = await digest("ok");
    await complete(a, { artifact_id: "report", path: "report.txt", bytes: 2, sha256, export_available: true });
    await a.stub.workerHttp(a.workerId, SECRET, { kind: "readiness", readiness: {} });
    await runInDurableObject(a.stub, async (_instance, state) => {
      state.storage.sql.exec("INSERT INTO artifacts(job_id,id,payload) VALUES(?,?,?)", a.jobId, "report", JSON.stringify({ id: "report", bytes: 2, sha256, encoding: "base64", content: btoa("ok") }));
    });
    expect(await a.stub.downloadDetailsByArtifact(a.jobId, "report")).toMatchObject({ eligible: true, stream_available: false });
    const grant = await a.stub.createDownloadRequest(a.jobId, "report");
    expect(await new Response(await a.stub.approveDownload(grant.download_token)).text()).toBe("ok");
  });
  it("streams an artifact larger than 12 KB with explicit credits and no byte persistence", async () => {
    const a = await owner(), bytes = new Uint8Array(20_000).fill(42);
    const sha256 = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(v => v.toString(16).padStart(2, "0")).join("");
    await complete(a, { artifact_id: "report", file_name: "report.pdf", mime_type: "application/pdf", bytes: bytes.length, sha256, export_available: false, download_available: true });
    const { socket, next } = await socketFor(a);
    try {
      expect(await a.stub.downloadDetailsByArtifact(a.jobId, "report")).toMatchObject({ eligible: true, size: 20_000 });
      const grant = await a.stub.createDownloadRequest(a.jobId, "report");
      const opening = a.stub.approveDownload(grant.download_token), open = await bounded(next("artifact_open"), "open");
      socket.send(JSON.stringify({ kind: "artifact_manifest", transfer_id: open.transfer_id, artifact_id: "report", file_name: "report.pdf", mime_type: "application/pdf", size: bytes.length, sha256 }));
      const stream = await bounded(opening, "manifest");
      // RPC byte streams may split a worker chunk across reader boundaries.
      const consuming = new Response(stream).arrayBuffer();
      for (let offset = 0; offset < bytes.length; offset += 16_384) {
        const credit = await bounded(next("artifact_credit"), "credit " + offset);
        expect(credit).toMatchObject({ offset, credits: 1 });
        const chunk = bytes.subarray(offset, Math.min(offset + 16_384, bytes.length));
        socket.send(JSON.stringify({ kind: "artifact_chunk", transfer_id: open.transfer_id, offset, data: btoa(String.fromCharCode(...chunk)) }));
      }
      socket.send(JSON.stringify({ kind: "artifact_end", transfer_id: open.transfer_id, bytes: bytes.length, sha256 }));
      expect(new Uint8Array(await bounded(consuming, "end"))).toEqual(bytes);
      const persisted = await runInDurableObject(a.stub, async (_instance, state) => ({ artifacts: state.storage.sql.exec("SELECT * FROM artifacts").toArray(), grants: state.storage.sql.exec("SELECT state FROM download_grants").toArray() }));
      expect(persisted).toEqual({ artifacts: [], grants: [{ state: "done" }] });
      expect(await failure(a.stub, instance => instance.approveDownload(grant.download_token))).toContain("already used");
    } finally { socket.close(1000); }
  });
  it("rejects changed metadata and artifacts outside the owned published manifest", async () => {
    const a = await owner(); await complete(a, { artifact_id: "report", path: "report.txt", bytes: 2, sha256: "a".repeat(64), download_available: true });
    const { socket, next } = await socketFor(a);
    try {
      expect(await failure(a.stub, instance => instance.downloadDetailsByArtifact(a.jobId, "other"))).toContain("Published owned artifact not found");
      const grant = await a.stub.createDownloadRequest(a.jobId, "report");
      const opening = failure(a.stub, instance => instance.approveDownload(grant.download_token));
      const open = await next("artifact_open");
      socket.send(JSON.stringify({ kind: "artifact_manifest", transfer_id: open.transfer_id, artifact_id: "report", size: 3, sha256: "a".repeat(64) }));
      expect(await opening).toContain("metadata changed");
      expect(await next("artifact_abort")).toMatchObject({ transfer_id: open.transfer_id });
    } finally { socket.close(1000); }
  });
  it("rejects corrupt content before releasing final bytes and releases its owner slot", async () => {
    const a = await owner(), sha256 = await digest("ok");
    await complete(a, { artifact_id: "report", path: "report.txt", bytes: 2, sha256, download_available: true });
    const { socket, next } = await socketFor(a);
    try {
      const grant = await a.stub.createDownloadRequest(a.jobId, "report"), opening = a.stub.approveDownload(grant.download_token), open = await next("artifact_open");
      socket.send(JSON.stringify({ kind: "artifact_manifest", transfer_id: open.transfer_id, artifact_id: "report", size: 2, sha256 }));
      const stream = await opening;
      const consuming = new Response(stream).arrayBuffer().then(() => "UNEXPECTED SUCCESS", () => "rejected");
      await next("artifact_credit");
      expect(await failure(a.stub, instance => instance.approveDownload(grant.download_token))).toContain("current download");
      socket.send(JSON.stringify({ kind: "artifact_chunk", transfer_id: open.transfer_id, offset: 0, data: btoa("no") }));
      await next("artifact_abort");
      expect(await consuming).toBe("rejected");
      expect((await a.stub.createDownloadRequest(a.jobId, "report")).download_token).toBeTruthy();
    } finally { socket.close(1000); }
  });
  it("rejects an over-limit export before worker dispatch", async () => {
    const a = await owner();
    await complete(a, { artifact_id: "report", path: "report.pdf", bytes: 20 * 1024 * 1024 + 1, sha256: "a".repeat(64), download_available: true });
    const { socket } = await socketFor(a);
    try { expect(await failure(a.stub, instance => instance.createDownloadRequest(a.jobId, "report"))).toContain("compatible paired worker"); }
    finally { socket.close(1000); }
  });
});
