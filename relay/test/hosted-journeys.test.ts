import { createExecutionContext, env, SELF } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";
import { authPage, authorizationServer } from "../src/auth";
import { digest, SCOPES } from "../src/contracts";
import { seal } from "../src/security";
import relay from "../src/index";

declare module "cloudflare:test" { interface ProvidedEnv extends Env {} }
const ORIGIN = "https://relay.example.test";
const ENTRA: Env = { ...env, ENTRA_TENANT_ID: "11111111-1111-4111-8111-111111111111",
  ENTRA_AUTHORITY: "https://synthetictenant.ciamlogin.com/11111111-1111-4111-8111-111111111111",
  ENTRA_CLIENT_ID: "22222222-2222-4222-8222-222222222222", ENTRA_CLIENT_SECRET: "synthetic-client-secret" };
afterEach(() => vi.restoreAllMocks());
async function account(provider = "entra") {
  const key = await digest(crypto.randomUUID()), userId = provider + "-" + key, csrf = "synthetic-browser-consent";
  const cookie = await seal({ userId, ownerKey: key, csrf, expires: Date.now() + 60_000 }, env.SESSION_SECRET);
  return { key, userId, csrf, stub: env.OWNERS.getByName(key), headers: { Cookie: "__Host-astra-session=" + cookie, Origin: ORIGIN, Accept: "text/html" } };
}

describe("hosted account and consent journeys", () => {
  it("serves the reviewed legal notices publicly and resolves their internal links", async () => {
    const expected = {
      privacy: ["Delegate to Antigravity relay privacy notice", "Digipsykologia Oy", "The relay is not end-to-end encrypted against its operator", "72 hours", "LinkedIn profile"],
      terms: ["Delegate to Antigravity remote use conditions", "Cancellation is not rollback", "Provider terms, model access, subscriptions, quotas and permitted uses apply"],
      license: ["MIT license", "Permission is hereby granted", "THE SOFTWARE IS PROVIDED"]
    };
    for (const [path, statements] of Object.entries(expected)) {
      const response = await SELF.fetch(ORIGIN + "/" + path), html = await response.text();
      expect(response.status).toBe(200);
      expect(response.headers.get("Content-Type")).toContain("text/html");
      expect(response.headers.get("Set-Cookie")).toBeNull();
      expect(response.headers.get("Location")).toBeNull();
      for (const statement of statements) expect(html).toContain(statement);
      expect(html.match(/<h1(?:\s[^>]*)?>/g)).toHaveLength(1);
    }
    const terms = await (await SELF.fetch(ORIGIN + "/terms")).text();
    expect(terms).toContain('href="/privacy"');
    expect(terms).toContain('href="/license"');
    expect(terms).not.toMatch(/href="(?:PRIVACY\.md|\.\.\/LICENSE)"/);
  });

  it("keeps public legal pages read-only", async () => {
    for (const path of ["privacy", "terms", "license"]) {
      const response = await SELF.fetch(ORIGIN + "/" + path, { method: "POST", body: "ignored" });
      expect(response.status).toBe(405);
      expect(response.headers.get("Allow")).toBe("GET");
      expect(response.headers.get("Set-Cookie")).toBeNull();
    }
  });

  it("redirects public support to the fixed publisher profile without forwarding request data", async () => {
    for (const method of ["GET", "HEAD"]) {
      const response = await relay.fetch(new Request(ORIGIN + "/support?next=https://other.example/&task=private", { method }), env, createExecutionContext());
      expect(response.status).toBe(302);
      expect(response.headers.get("Location")).toBe("https://www.linkedin.com/in/radwan-rami/");
      expect(response.headers.get("Referrer-Policy")).toBe("no-referrer");
      expect(response.headers.get("Set-Cookie")).toBeNull();
      expect(await response.text()).toBe("");
    }
    const response = await relay.fetch(new Request(ORIGIN + "/support", { method: "POST", body: "ignored" }), env, createExecutionContext());
    expect(response.status).toBe(405);
    expect(response.headers.get("Location")).toBeNull();
  });

  it("explains the empty account and hides destructive controls without exposing identity identifiers", async () => {
    const a = await account(), response = await SELF.fetch(ORIGIN + "/account", { headers: a.headers }), html = await response.text();
    expect(response.status).toBe(200);
    expect(html).toContain("<title>Connected computers · Delegate to Antigravity</title>");
    expect(html).toContain('aria-label="Delegate to Antigravity home"');
    expect(html).toContain('<span class="brand-name">Delegate to Antigravity</span>');
    expect(html).toContain("<h1>Connected computers</h1>");
    expect(html).toContain("Signed in using <strong>Email or Google</strong>");
    expect(html).toContain('class="panel empty-state"');
    expect(html).toContain('href="/auth/login?next=%2Faccount"');
    expect(html).toContain("Switching methods");
    expect(html).toContain('<details class="danger-zone"><summary>Erase relay records</summary>');
    expect(html).toContain('name="confirm" required');
    expect(html).toContain("Open Computers in ChatGPT");
    expect(html).toContain("no window, tray or local web page");
    expect(html).not.toContain('href="/account/export"');
    expect(html).not.toContain(a.userId);
    expect(html).not.toContain(a.key);
    expect((await a.stub.exportData()).workers).toEqual([]);
  });

  it("keeps sign-in separate from headless setup and file delivery in ChatGPT", async () => {
    const helpers = authorizationServer(ENTRA).getOAuthApi(ENTRA);
    const response = await authPage(new Request(ORIGIN + "/auth/login"), ENTRA, helpers), html = await response!.text();
    expect(response!.status).toBe(200);
    expect(html).toContain("Continue setup in ChatGPT");
    expect(html).toContain("check provider accounts and approve folders");
    expect(html).toContain("no window, tray or local web page");
    expect(html).toContain("provider's own browser page");
    expect(html).toContain("Email or Google");
    expect(html).toContain("Continue with GitHub");
    expect(html).not.toMatch(/releases\/download\/|\.exe(?:["?\s<])|href="\/downloads\/|\bdownload=/);
    expect(response!.headers.get("Content-Disposition")).toBeNull();
    expect(response!.headers.get("Location")).toBeNull();
  });

  it("keeps a long first task expandable and reuses its receipt through workspace selection", async () => {
    const a = await account(), task = "Review the approved supplied material. ".repeat(100), requestId = "saved-task";
    const prepared = await a.stub.tool("prepare_delegation", { request_id: requestId, task, isolation: "auto" });
    const worker = await a.stub.registerWorker({ label: "My computer", credential_hash: await digest("x".repeat(64)), workspaces: [{
      id: "project", label: "My project", capabilities: ["local_files"], isolation: ["worktree"], task_types: ["implementation"]
    }] }, a.key, crypto.randomUUID());
    const response = await SELF.fetch(ORIGIN + "/connect?request_id=" + requestId, { headers: a.headers }), html = await response.text();
    expect(response.status).toBe(200);
    expect(html).toContain('<details><summary>Read the full task</summary><pre>');
    expect(html).toContain(task.trim());
    expect(html).toContain("Your task has not started");
    expect(html).toContain("<legend>Use an already approved workspace</legend>");
    expect(html).toContain(`name="workspace" required value="${worker.worker_id}/project"`);
    expect(html).toContain('href="astra-antigravity://connect?request_id=saved-task"');
    expect(html).toContain("Using a phone or tablet?");
    expect(await a.stub.tool("inspect_delegation", { request_id: requestId })).toMatchObject({ delegation_id: prepared.delegation_id, status: "awaiting_setup" });
    expect((await a.stub.exportData()).tasks).toHaveLength(1);
  });

  it("describes actual pairing permissions plainly and keeps approval and CSRF contracts", async () => {
    const a = await account(), pair = await SELF.fetch(ORIGIN + "/worker/pair", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: "<img src=x onerror=alert(1)>", credential_hash: await digest("x".repeat(64)), workspaces: [{
        id: "project", label: "Approved project", capabilities: ["local_files", "shell"], isolation: ["worktree", "shared"], task_types: ["implementation", "analysis"]
      }] }) });
    const receipt = await pair.json<{ code: string }>(), path = "/pair?code=" + receipt.code;
    const response = await SELF.fetch(ORIGIN + path, { headers: a.headers }), html = await response.text();
    expect(response.status).toBe(200);
    for (const statement of ["Isolated Git worktree", "Original folder (shared access)", "Run commands", "Read and change approved files", "Code changes", "Analysis"])
      expect(html).toContain(statement);
    expect(html).toContain('name="csrf" value="' + a.csrf + '"');
    expect(html).toContain('name="decision" value="approve"');
    expect(html).toContain('name="decision" value="deny"');
    expect(html).not.toContain("<img src=x");
    expect((await a.stub.exportData()).workers).toEqual([]);
    const denied = await SELF.fetch(ORIGIN + path, { method: "POST", headers: a.headers, body: new URLSearchParams({ csrf: "wrong", decision: "approve" }) });
    expect(denied.status).toBe(403);
    const recovery = await denied.text();
    expect(recovery).toContain('href="' + path + '"');
    expect(recovery).not.toContain('<form method="post"');
    expect((await a.stub.exportData()).workers).toEqual([]);
  });

  it.each(["entra", "github"])("preselects only the authenticated %s provider while preserving explicit scopes and approval", async provider => {
    const a = await account(provider), helpers = authorizationServer(ENTRA).getOAuthApi(ENTRA);
    const client = await helpers.createClient({ clientName: "Consent fixture", redirectUris: ["http://127.0.0.1:12345/callback"], tokenEndpointAuthMethod: "none", grantTypes: ["authorization_code"], responseTypes: ["code"] });
    const query = new URLSearchParams({ client_id: client.clientId, redirect_uri: client.redirectUris[0], response_type: "code", scope: SCOPES.join(" "),
      resource: ORIGIN + "/mcp", state: "synthetic-state", code_challenge: "a".repeat(43), code_challenge_method: "S256" });
    const response = await authPage(new Request(ORIGIN + "/authorize?" + query, { headers: a.headers }), ENTRA, helpers), html = await response!.text();
    expect(response!.status).toBe(200);
    expect(html).toContain("<h1>Connect application</h1>");
    expect(html).toContain("Tasks can consume your provider quotas and modify approved workspaces. Provider credentials stay on your computer.");
    expect(html).toContain(`name="provider" value="${provider}" checked`);
    expect(html).not.toContain(`name="provider" value="${provider === "entra" ? "github" : "entra"}" checked`);
    expect(html).toContain("Email or Google");
    expect(html).not.toContain("Microsoft");
    expect([...html.matchAll(/name="scope" value="([^"]+)" checked/g)].map(match => match[1])).toEqual([...SCOPES]);
    expect(html).toContain('name="decision" value="approve"');
    expect(html).toContain('name="handle"');
    expect(response!.headers.get("Location")).toBeNull();
    expect((await helpers.listUserGrants(a.userId, { limit: 100 })).items).toEqual([]);
  });

  it("preserves a validated management destination after upstream failure using only a fresh GET", async () => {
    const helpers = authorizationServer(env).getOAuthApi(env), next = "/connect?request_id=saved-task";
    const started = (await authPage(new Request(ORIGIN + "/auth/login?next=" + encodeURIComponent(next)), env, helpers))!;
    const state = new URL(started.headers.get("Location")!).searchParams.get("state")!;
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Synthetic provider detail must never be shown"));
    const response = await relay.fetch(new Request(ORIGIN + "/auth/github/callback?" + new URLSearchParams({ state, code: "synthetic-code" }),
      { headers: { Cookie: started.headers.get("Set-Cookie")!.split(";")[0], Accept: "text/html" } }), env, createExecutionContext());
    expect(response.status).toBe(502);
    const html = await response.text();
    expect(html).toContain('href="/auth/login?next=' + encodeURIComponent(next) + '"');
    expect(html).toContain("It does not repeat the previous action");
    expect(html).not.toContain('<form method="post"');
    expect(html).not.toContain("Synthetic provider detail");
    expect(html).not.toContain(state);
    expect(html).not.toContain("synthetic-code");
  });
});
