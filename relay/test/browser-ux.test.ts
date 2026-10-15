import { env, SELF, runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { authorizationServer } from "../src/auth";
import { digest, SCOPES } from "../src/contracts";
import { page, seal } from "../src/security";

declare module "cloudflare:test" { interface ProvidedEnv extends Env {} }
const ORIGIN = "https://relay.example.test";
const SECRET = "a".repeat(64);
async function account() {
  const key = await digest(crypto.randomUUID()), stub = env.OWNERS.getByName(key);
  const session = await seal({ userId: crypto.randomUUID(), ownerKey: key, csrf: "browser-consent", expires: Date.now() + 60_000 }, env.SESSION_SECRET);
  return { key, stub, headers: { Cookie: "__Host-astra-session=" + session, Origin: ORIGIN, Accept: "text/html" } };
}
async function completedArtifact() {
  const a = await account(), worker = await a.stub.registerWorker({ label: "My computer", credential_hash: await digest(SECRET), workspaces: [{
    id: "project", label: "My project", capabilities: ["local_files", "shell"], isolation: ["worktree"], task_types: ["implementation"]
  }] }, a.key, crypto.randomUUID());
  const receipt = await a.stub.tool("delegate_via_luna", { workspace_id: "project", request_id: "file", task: "Create my report" });
  const delegationId = String(receipt.delegation_id);
  await a.stub.workerHttp(worker.worker_id, SECRET, { kind: "readiness", readiness: {} });
  await a.stub.workerHttp(worker.worker_id, SECRET, { kind: "receipt", delegation_id: delegationId, local_id: "local-task" });
  await a.stub.workerHttp(worker.worker_id, SECRET, { kind: "event", delegation_id: delegationId, event: {
    sequence: 1, kind: "completed", terminal: true, summary: "Report ready", task_type: "implementation", verification_required: true
  }, result: { status: "completed", artifact_refs: [{ artifact_id: "report", path: "report&review.txt", bytes: 2, sha256: await digest("42"), export_available: true }] } });
  return { ...a, workerId: worker.worker_id, delegationId, path: "/downloads/" + delegationId + "/report" };
}

describe("browser journey polish", () => {
  it("keeps responsive styling nonce-protected and management forms restricted to the relay", async () => {
    const rendered = page("A readable page", "<p>Example</p>"), html = await rendered.text();
    const nonce = /<style nonce="([^"]+)">/.exec(html)?.[1];
    expect(nonce).toBeTruthy();
    expect(rendered.headers.get("Content-Security-Policy")).toContain("style-src 'nonce-" + nonce + "'");
    expect(rendered.headers.get("Content-Security-Policy")).toContain("form-action 'self';");
    expect(rendered.headers.get("Content-Security-Policy")).toContain("frame-ancestors 'none'");
    expect(rendered.headers.get("Cache-Control")).toBe("no-store");
    const compact = html.replace(/\s+/g, "");
    for (const hook of ["font:1rem/1.55", "min-height:44px", "overflow-wrap:anywhere", ":focus-visible", "prefers-color-scheme:dark", "prefers-reduced-motion:reduce", "max-width:600px", 'data-surface="hosted"'])
      expect(compact).toContain(hook);
    expect(html).not.toContain("<script");
  });

  it("uses readable permission labels while preserving every OAuth checkbox value", async () => {
    const client = await authorizationServer(env).getOAuthApi(env).createClient({ clientName: "Browser UX fixture", redirectUris: ["http://127.0.0.1:12345/callback"], tokenEndpointAuthMethod: "none", grantTypes: ["authorization_code"], responseTypes: ["code"] });
    const query = new URLSearchParams({ client_id: client.clientId, redirect_uri: client.redirectUris[0], response_type: "code", scope: SCOPES.join(" "), resource: ORIGIN + "/mcp", state: "synthetic-state", code_challenge: "a".repeat(43), code_challenge_method: "S256" });
    const response = await SELF.fetch(ORIGIN + "/authorize?" + query), html = await response.text();
    expect(response.status).toBe(200);
    expect([...html.matchAll(/name="scope" value="([^"]+)" checked/g)].map(match => match[1])).toEqual([...SCOPES]);
    for (const label of ["See my approved computers and workspaces", "Read my task status and results", "Send and manage my authorized tasks"])
      expect(html).toContain(label);
    expect(html).toContain('class="secondary" name="decision" value="deny"');
    expect(response.headers.get("Content-Security-Policy")).toContain("https://github.com");
  });

  it("explains headless setup in ChatGPT without advertising an unpublished installer or starting a task", async () => {
    const a = await account(), response = await SELF.fetch(ORIGIN + "/connect", { headers: a.headers }), html = await response.text();
    expect(response.status).toBe(200);
    expect(html).toContain("Already installed?");
    expect(html).toContain('href="astra-antigravity://connect"');
    expect(html).toContain("Start background companion");
    expect(html).toContain("no local setup window");
    expect(html).toContain("pairing code");
    expect(html).toContain("Computers in ChatGPT");
    expect(html).toContain("A verified public setup release is not available");
    expect(html).toContain("No setup command is enabled");
    expect(html).not.toContain("Download the Windows companion");
    expect(html).not.toContain("paste this link into");
    expect(html).not.toContain("npx --yes");
    expect(html).not.toMatch(/releases\/download\//);
    expect(html).toContain("Using a phone or tablet?");
    expect(html).toContain("your own Windows computer");
    expect(html).toContain("phone or tablet");
    const jobs = await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec("SELECT id FROM jobs").toArray());
    expect(jobs).toEqual([]);
  });

  it("offers a canonical setup GET retry after owned form rejection without queuing the task", async () => {
    const a = await account();
    await a.stub.tool("prepare_delegation", { request_id: "saved-task", task: "Create my report", isolation: "auto" });
    const response = await SELF.fetch(ORIGIN + "/connect?request_id=saved-task&ignored=extra", { method: "POST", headers: a.headers, body: new URLSearchParams({ csrf: "wrong", workspace: "wrong/project" }) });
    const html = await response.text();
    expect(response.status).toBe(403);
    expect(html).toContain('href="/connect?request_id=saved-task"');
    expect(html).not.toContain("ignored=extra");
    expect(html).toContain("It does not repeat the previous action");
    expect(html).not.toContain('method="post"');
    expect((await a.stub.tool("inspect_delegation", { request_id: "saved-task" })).status).toBe("awaiting_setup");
    const commands = await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec("SELECT id FROM commands").toArray());
    expect(commands).toEqual([]);
  });
  it("keeps management-only browser pairing distinct from folder approval and task launch",async()=>{
    const a=await account();await a.stub.tool("prepare_delegation",{request_id:"headless-fallback",task:"Create my report",isolation:"auto"});
    const response=await SELF.fetch(ORIGIN+"/worker/pair",{method:"POST",body:JSON.stringify({label:"Headless computer",credential_hash:await digest("a".repeat(64)),setup_only:true,workspaces:[],setup_request_id:"headless-fallback"})});
    const pair=await response.json<{code:string}>(),html=await (await SELF.fetch(ORIGIN+"/pair?code="+pair.code,{headers:a.headers})).text();
    expect(html).toContain("No folder access is approved");expect(html).toContain("Connect computer management");expect(html).not.toContain("Connect workspace and delegate");
    expect(html).toContain("Management approval alone does not start work");
    expect((await a.stub.tool("inspect_delegation",{request_id:"headless-fallback"})).status).toBe("awaiting_setup");
  });

  it("returns old owned browser file links to ChatGPT without starting a transfer", async () => {
    const a = await completedArtifact(), preview = await SELF.fetch(ORIGIN + a.path, { headers: a.headers }), html = await preview.text();
    expect(preview.status).toBe(200);
    expect(html).toContain("Open your file in ChatGPT");
    expect(html).toContain("This page does not start a file transfer");
    expect(html).not.toMatch(/Save file|<form|\bdownload=|Your file has been saved/);
    expect(preview.headers.get("Content-Disposition")).toBeNull();
    const rejected = await SELF.fetch(ORIGIN + a.path, { method: "POST", headers: a.headers, body: new URLSearchParams({ csrf: "wrong" }) });
    expect(rejected.status).toBe(403);
    expect(await rejected.text()).toContain('href="' + a.path + '"');
    const accepted = await SELF.fetch(ORIGIN + a.path, { method: "POST", headers: a.headers, body: new URLSearchParams({ csrf: "browser-consent" }) });
    expect(accepted.status).toBe(200);
    expect(await accepted.text()).toContain("This page does not start a file transfer");
    expect(accepted.headers.get("Content-Disposition")).toBeNull();
    const grants = await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec("SELECT id FROM download_grants").toArray());
    expect(grants).toEqual([]);
    const other = await account(), wrongOwner = await SELF.fetch(ORIGIN + a.path, { headers: other.headers });
    expect(wrongOwner.status).toBe(404);
    expect(await wrongOwner.text()).not.toContain("Try this step again");
    // A later OAuth/renewal error cannot inherit another request's file retry.
    const approvalError = await SELF.fetch(ORIGIN + "/renew?code=invalid", { headers: a.headers });
    expect(await approvalError.text()).not.toContain("Try this step again");
  });

  it("keeps an offline file on its computer without offering a browser fallback", async () => {
    const a = await completedArtifact();
    await runInDurableObject(a.stub, async (_instance, state) => { state.storage.sql.exec("UPDATE workers SET last_seen=0 WHERE id=?", a.workerId); });
    const response = await SELF.fetch(ORIGIN + a.path, { headers: a.headers }), html = await response.text();
    expect(response.status).toBe(200);
    expect(html).toContain("If file delivery is unavailable in ChatGPT, the file remains on that computer");
    expect(html).toContain("Use Computers in ChatGPT to check the connection");
    expect(html).not.toContain('href="' + a.path + '">Check again</a>');
    expect(html).not.toContain("<button>Save file</button>");
    expect(html).not.toContain("Your file has been saved");
  });

  it("allows cookie-free sandbox file fetches but rejects document navigation before consuming the capability", async () => {
    const a = await completedArtifact(), sha256 = await digest("42");
    await runInDurableObject(a.stub, async (_instance, state) => {
      state.storage.sql.exec("INSERT INTO artifacts(job_id,id,payload) VALUES(?,?,?)", a.delegationId, "report",
        JSON.stringify({ id: "report", encoding: "base64", content: "NDI=", bytes: 2, sha256 }));
    });
    const grant = await a.stub.tool("prepare_artifact_download_ui", { delegation_id: a.delegationId, artifact_id: "report" });
    const url = String(grant.url);
    for (const headers of [{ "Sec-Fetch-Mode": "navigate" }, { "Sec-Fetch-Dest": "document" }]) {
      const navigation = await SELF.fetch(url, { headers: { ...headers, Accept: "text/html" } });
      expect(navigation.status).toBe(403);
      expect(await navigation.text()).toContain("Return to your task in ChatGPT");
      expect(navigation.headers.get("Content-Disposition")).toBeNull();
      expect(await runInDurableObject(a.stub, async (_instance, state) => state.storage.sql.exec("SELECT state FROM download_grants").toArray()))
        .toEqual([{ state: "pending" }]);
    }
    const response = await SELF.fetch(url, { credentials: "omit", headers: { Origin: "null", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty" } });
    expect(response.status).toBe(200);
    expect(new TextDecoder().decode(await response.arrayBuffer())).toBe("42");
    expect(response.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(response.headers.get("Access-Control-Allow-Credentials")).toBeNull();
    expect(response.headers.get("Set-Cookie")).toBeNull();
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect((await SELF.fetch(url, { headers: { Origin: "null" } })).status).toBe(410);
    for (const path of ["/account", "/artifact-download/not/a/capability"]) {
      const denied = await SELF.fetch(ORIGIN + path, { headers: { Origin: "null" } });
      expect(denied.status).toBe(403);
      expect(denied.headers.get("Access-Control-Allow-Origin")).toBeNull();
    }
    const deniedPost = await SELF.fetch(url, { method: "POST", headers: { Origin: "null" } });
    expect(deniedPost.status).toBe(403);
    expect(deniedPost.headers.get("Access-Control-Allow-Origin")).toBeNull();
  });

  it("finishes workspace selection with a clear return step and the same saved task", async () => {
    const a = await account();
    await a.stub.tool("prepare_delegation", { request_id: "saved-task", task: "Create my report", isolation: "auto" });
    const worker = await a.stub.registerWorker({ label: "My computer", credential_hash: await digest(SECRET), workspaces: [{ id: "project", label: "My project", capabilities: ["local_files"], isolation: ["worktree"], task_types: ["implementation"] }] }, a.key, crypto.randomUUID());
    const response = await SELF.fetch(ORIGIN + "/connect?request_id=saved-task", { method: "POST", headers: a.headers, body: new URLSearchParams({ csrf: "browser-consent", workspace: worker.worker_id + "/project" }) });
    const html = await response.text();
    expect(response.status).toBe(200);
    expect(html).toContain("You do not need to submit the task again");
    expect(html).toContain("close this tab and switch back to your original ChatGPT or Codex conversation");
    expect((await a.stub.tool("inspect_delegation", { request_id: "saved-task" })).status).toBe("queued");
  });
});
