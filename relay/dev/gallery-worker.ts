/** DEV ONLY. Never configured by production Wrangler or included in a release. */
import production, { OwnerRelay as ProductionOwnerRelay, WorkerPairing } from "../src/index";
import { authorizationServer } from "../src/auth";
import { digest, object } from "../src/contracts";
import { html, page, seal } from "../src/security";
import cardHtml from "../ui/dist/delegation.html";
import companionHtml from "../../companion/web/index.html";
import companionCss from "../../companion/web/setup.css";
import companionJs from "../../companion/web/setup.js";
import sharedCss from "../../ui/astra.css";
import { cardHost } from "./card-host";
import { cardFixtures, companionFixtures } from "./fixtures";
import { controlFixtures } from "./control-fixtures";

export { WorkerPairing };
const SECRET = "a".repeat(64);
const csrf = "synthetic-gallery-csrf";
const workspaces = [{ id: "reports", label: "Quarterly reports", capabilities: ["local_files", "shell"], isolation: ["worktree"], task_types: ["implementation", "document"] }];
const hosted = ["home", "login", "authorize", "connect", "choose-workspace", "pair", "account", "empty-account", "renew", "download", "download-offline", "error", "privacy", "terms", "license"];

export class GalleryOwnerRelay extends ProductionOwnerRelay {
  async gallerySeed(kind: string, ownerKey: string) {
    let seeded = await this.ctx.storage.get<{ workerId?: string; renewalId?: string }>("gallery-seed");
    if (!seeded) {
      seeded = {};
      await this.ctx.storage.put("gallery-kind", kind);
      if (["connect", "choose-workspace"].includes(kind)) await this.tool("prepare_delegation", { request_id: "gallery-task", task: "Create the quarterly report using the supplied operations and accessibility sources.", isolation: "auto" });
      if (!["connect", "empty-account"].includes(kind)) {
        const worker = await this.registerWorker({ label: "Rami’s Windows computer", credential_hash: await digest(SECRET), workspaces: workspaces as never }, ownerKey, "gallery-main");
        seeded.workerId = worker.worker_id;
        await this.workerHttp(worker.worker_id, SECRET, { kind: "readiness", readiness: {} });
        this.ctx.storage.sql.exec("UPDATE workers SET expires=? WHERE id=?", Date.parse("2030-11-14T12:30:00Z"), worker.worker_id);
        if (kind === "account") {
          const expired = await this.registerWorker({ label: "Previous laptop", credential_hash: await digest(SECRET), workspaces: workspaces.map(workspace => ({ ...workspace, id: "previous-reports" })) as never }, ownerKey, "gallery-expired");
          this.ctx.storage.sql.exec("UPDATE workers SET expires=?,last_seen=0 WHERE id=?", Date.parse("2020-10-14T12:30:00Z"), expired.worker_id);
          const revoked = await this.registerWorker({ label: "Archived research computer", credential_hash: await digest(SECRET), workspaces: workspaces.map(workspace => ({ ...workspace, id: "archived-reports" })) as never }, ownerKey, "gallery-revoked");
          await this.revokeWorker(revoked.worker_id);
        }
      }
      await this.ctx.storage.put("gallery-seed", seeded);
    }
    if (kind === "renew" && seeded.workerId) {
      // Cached gallery state outlives approval proofs. Resolve a valid renewal
      // on each visit so the production lifecycle replaces expired proofs.
      const renewal = await this.beginWorkerRenewal(seeded.workerId, SECRET, await digest("b".repeat(64)));
      seeded.renewalId = renewal.renewal_id;
      await this.ctx.storage.put("gallery-seed", seeded);
    }
    this.ctx.storage.sql.exec("UPDATE workers SET last_seen=? WHERE revoked=0 AND expires>?", Date.now(), Date.now());
    return seeded;
  }
  override async downloadDetailsByArtifact(jobId: string, artifactId: string) {
    if (jobId !== "gallery-download") return super.downloadDetailsByArtifact(jobId, artifactId);
    const kind = await this.ctx.storage.get<string>("gallery-kind");
    return { delegation_id: jobId, artifact_id: artifactId, file_name: "Quarterly accessibility and operations report.pdf", mime_type: "application/pdf", size: 248_320,
      sha256: "73475cb40a568e8da8a045ced110137e159f890ac4da883b6b17dc651b3a8049", eligible: kind !== "download-offline", stream_available: true };
  }
}

const companionSessions = new Map<string, { scenario: string; state: Record<string, unknown>; calls: string[] }>();
function cookie(request: Request, name: string) { return request.headers.get("Cookie")?.split(";").map(part => part.trim()).find(part => part.startsWith(name + "="))?.slice(name.length + 1); }
function textResponse(body: string, mime = "text/html; charset=utf-8", headers: Record<string, string> = {}) { return new Response(body, { headers: { "Content-Type": mime, "Cache-Control": "no-store", ...headers } }); }

async function hostedPage(kind: string, request: Request, env: Env, ctx: ExecutionContext) {
  if (!hosted.includes(kind)) return new Response("Unknown preview", { status: 404 });
  const origin = env.PUBLIC_ORIGIN, key = await digest("astra-ui-gallery:" + kind);
  const owner = env.OWNERS.getByName(key) as unknown as DurableObjectStub<GalleryOwnerRelay>;
  const seeded = await owner.gallerySeed(kind, key);
  const target = new URL(origin + ({ home: "/", login: "/auth/login?next=%2Faccount", "choose-workspace": "/connect?request_id=gallery-task", connect: "/connect?request_id=gallery-task", "empty-account": "/account", download: "/downloads/gallery-download/report", "download-offline": "/downloads/gallery-download/report", renew: "/renew?code=" + seeded.renewalId, error: "/renew?code=invalid" }[kind] ?? "/" + kind));
  if (kind === "pair") {
    // Generate a new local fixture on each visit so a long design review does
    // not retain an expired approval code. No production worker is involved.
    const code = crypto.randomUUID().replace(/-/g, "").slice(0, 24);
    await env.PAIRINGS.getByName(code).create({ label: "Rami’s Windows computer", credential_hash: await digest(SECRET), workspaces: workspaces as never });
    target.search = "code=" + code;
  }
  if (kind === "authorize") {
    const client = await authorizationServer(env).getOAuthApi(env).createClient({ clientName: "ChatGPT", redirectUris: [origin + "/gallery/callback"], tokenEndpointAuthMethod: "none", grantTypes: ["authorization_code"], responseTypes: ["code"] });
    target.search = new URLSearchParams({ client_id: client.clientId, redirect_uri: client.redirectUris[0], response_type: "code", scope: "workspaces:read tasks:read tasks:write", resource: origin + "/mcp", state: "synthetic-gallery-state", code_challenge: "a".repeat(43), code_challenge_method: "S256" }).toString();
  }
  const headers = new Headers({ Accept: "text/html" });
  if (!["home", "login", "authorize", "privacy", "terms", "license"].includes(kind)) headers.set("Cookie", "__Host-astra-session=" + await seal({ userId: "entra-gallery", ownerKey: key, csrf, expires: Date.now() + 3_600_000 }, env.SESSION_SECRET));
  return production.fetch(new Request(target, { headers }), env, ctx);
}

async function companionApi(request: Request, url: URL) {
  const fixture = companionSessions.get(cookie(request, "astra-gallery-session") || "");
  if (!fixture) return Response.json({ message: "Open a companion preview first." }, { status: 409 });
  const action = url.pathname.slice(5); fixture.calls.push(action);
  if (action === "status") return Response.json(fixture.state);
  if (action === "metrics") return Response.json({ calls: fixture.calls });
  if (request.method !== "POST") return new Response(null, { status: 405 });
  if (["folder", "approve"].includes(action) && fixture.scenario === "action-error") return Response.json({ message: "The selected folder is unavailable. Choose another workspace." }, { status: 400 });
  const body = object(await request.json());
  if (action === "folder") return Response.json({ path: "C:\\Astra demo\\Quarterly reports" });
  if (action === "startup") fixture.state.startup_enabled = Boolean(body.enabled);
  if (action === "stop") Object.assign(fixture.state, { status: "draining", message: "Current work will finish before disconnecting." });
  if (action === "start") Object.assign(fixture.state, { status: "connected", worker_running: true, error: false, message: "Your computer is connected." });
  if (action === "approve") Object.assign(fixture.state, { status: "approval_required", workspace: [{ id: "reports", path: String(body.path), isolation: body.isolation }], verification_url: "/gallery/hosted/pair", message: "Review and approve this computer in your browser." });
  if (action === "intent") fixture.state.setup_request_id = "gallery-report";
  if (action === "renew") Object.assign(fixture.state, { status: "renewal_approval_required", verification_url: "/gallery/hosted/renew", message: "Approve renewal in your browser." });
  return Response.json({ ok: true });
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext) {
    const url = new URL(request.url);
    if (!["127.0.0.1", "localhost"].includes(url.hostname)) return new Response("The design gallery is loopback-only.", { status: 403 });
    if (url.pathname === "/__gallery/health") return Response.json({ service: "Astra local UI gallery", synthetic: true });
    if (url.pathname === "/__gallery" || url.pathname === "/") {
      const links = (prefix: string, names: string[]) => `<ul>${names.map(name => `<li><a href="${prefix}/${html(name)}">${html(name.replace(/-/g, " "))}</a></li>`).join("")}</ul>`;
      return page("Astra UI gallery", `<p class="status status--attention">Local synthetic previews. No sign-in, provider commands, production API calls or inference.</p><div class="grid"><section class="panel"><h2>Hosted pages</h2>${links("/gallery/hosted", hosted)}</section><section class="panel"><h2>Desktop companion</h2>${links("/gallery/companion", Object.keys(companionFixtures))}</section><section class="panel"><h2>ChatGPT task card</h2>${links("/gallery/card", Object.keys(cardFixtures))}</section><section class="panel"><h2>ChatGPT controls</h2>${links("/gallery/control", Object.keys(controlFixtures))}</section></div>`, new Headers(), [], { wide: true, eyebrow: "Design and verification" });
    }
    if (url.pathname === "/design-system.css") return textResponse(sharedCss, "text/css; charset=utf-8");
    if (url.pathname === "/setup.css") return textResponse(companionCss, "text/css; charset=utf-8");
    if (url.pathname === "/setup.js") return textResponse(companionJs, "text/javascript; charset=utf-8");
    if (url.pathname.startsWith("/api/")) return companionApi(request, url);
    const companion = /^\/gallery\/companion\/([a-z-]+)$/.exec(url.pathname);
    if (companion && companionFixtures[companion[1]]) {
      const id = crypto.randomUUID(); companionSessions.set(id, { scenario: companion[1], state: structuredClone(companionFixtures[companion[1]]), calls: [] });
      return textResponse(companionHtml, undefined, { "Set-Cookie": "astra-gallery-session=" + id + "; Path=/; SameSite=Lax" });
    }
    if (url.pathname === "/gallery/card/frame") return textResponse(cardHtml, undefined, { "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; frame-ancestors 'self'" });
    const card = /^\/gallery\/card\/([a-z-]+)$/.exec(url.pathname);
    if (card && cardFixtures[card[1]]) return textResponse(cardHost(card[1], url.searchParams.get("theme") || "light"));
    const control = /^\/gallery\/control\/([a-z-]+)$/.exec(url.pathname);
    if (control && controlFixtures[control[1]]) return textResponse(cardHost(control[1], url.searchParams.get("theme") || "light", true));
    const host = /^\/gallery\/hosted\/([a-z-]+)$/.exec(url.pathname);
    if (host && request.method === "GET") return hostedPage(host[1], request, env, ctx);
    // The production renderer is reachable only through the explicit fixture
    // whitelist above. Form posts, callbacks, worker/MCP APIs and all provider
    // operations are deliberately unavailable in this development entry point.
    return new Response("This action is unavailable in the local design preview.", { status: 405 });
  },
} satisfies ExportedHandler<Env>;
