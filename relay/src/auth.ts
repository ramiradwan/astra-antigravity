import { OAuthAuthorizationServer, AuthorizationError, CimdFetchError, authorizationErrorRedirect,
  type OAuthHelpers, type ConsentDescription } from "@cloudflare/workers-oauth-provider";
import { boundedJson, digest, object, relayError, RelayError, SCOPES, token, type ObjectValue } from "./contracts";
import { equal, html, page, publicOrigin, seal, unseal } from "./security";
import { entraAuthorizationUrl, entraConfig, entraConfigured, entraIdentity } from "./entra";

export type AuthProps = { userId: string; ownerKey: string };
type Provider = "github" | "entra";
export function accountMethod(session: ObjectValue | null): string | undefined {
  const subject = session?.userId;
  if (typeof subject !== "string") return undefined;
  return /^github[-:]/.test(subject) ? "GitHub" : subject.startsWith("entra-") ? "Email or Google" : undefined;
}
function preferredProvider(session: ObjectValue | null, env: Env): Provider {
  return typeof session?.userId === "string" && session.userId.startsWith("entra-") && entraConfigured(env) ? "entra"
    : githubConfigured(env) ? "github" : "entra";
}
function githubConfigured(env: Env): boolean { return Boolean(env.GITHUB_CLIENT_ID?.trim() && env.GITHUB_CLIENT_SECRET?.trim()); }
export function authConfigured(env: Env): boolean {
  return new TextEncoder().encode(env.SESSION_SECRET ?? "").byteLength >= 32 && (githubConfigured(env) || entraConfigured(env));
}
function selectedProvider(raw: unknown, env: Env): Provider {
  const provider = raw ?? (githubConfigured(env) ? "github" : "entra");
  if ((provider === "github" && githubConfigured(env)) || (provider === "entra" && entraConfigured(env))) return provider;
  throw new RelayError("sign_in_provider", "This sign-in provider is not configured. Choose an available provider.", 503);
}
function managementReturnPath(raw: string | null): string | null {
  return raw && /^\/(?:account|connect(?:\?request_id=[A-Za-z0-9_.-]{1,128})?|pair\?code=[a-f0-9]{24}|renew\?code=[a-f0-9]{32}|downloads\/[A-Za-z0-9_.-]{1,128}\/[A-Za-z0-9_.-]{1,128})$/.test(raw) ? raw : null;
}
export function authorizationServer(env: Env): OAuthAuthorizationServer<Env> {
  const origin = publicOrigin(env);
  return new OAuthAuthorizationServer<Env>({ issuer: origin, resources: [origin + "/mcp"],
    authorizeEndpoint: "/authorize", tokenEndpoint: "/oauth/token", clientRegistrationEndpoint: "/oauth/register",
    scopesSupported: [...SCOPES], clientIdMetadataDocumentEnabled: true,
    accessTokenTTL: 3_600, refreshTokenTTL: undefined, clientRegistrationTTL: undefined,
    allowTokenExchangeGrant: false });
}
/** Enumerate before revoking: changing KV while walking a cursor can skip grants. */
export async function allUserGrants(oauth:OAuthHelpers,userId:string) {
  const items:Awaited<ReturnType<OAuthHelpers["listUserGrants"]>>["items"]=[],seen=new Set<string>();
  let cursor:string|undefined;
  do {
    const page=await oauth.listUserGrants(userId,{limit:100,...(cursor?{cursor}:{})});
    items.push(...page.items);cursor=page.cursor;
    if(cursor&&seen.has(cursor))throw new RelayError("grant_pagination","Authorization listing did not advance; deletion was not reported complete",503);
    if(cursor)seen.add(cursor);
  }while(cursor);
  return items;
}
export async function revokeAllUserGrants(oauth:OAuthHelpers,userId:string):Promise<number> {
  const grants=await allUserGrants(oauth,userId);
  for(const grant of grants)await oauth.revokeGrant(grant.id,userId);
  return grants.length;
}
function githubUrl(state: string, verifier: string, env: Env): Promise<string> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)).then(hash => {
    const challenge = btoa(String.fromCharCode(...new Uint8Array(hash))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    const url = new URL("https://github.com/login/oauth/authorize");
    url.search = new URLSearchParams({ client_id: env.GITHUB_CLIENT_ID, redirect_uri: publicOrigin(env) + "/auth/github/callback",
      scope: "", state, code_challenge: challenge, code_challenge_method: "S256" }).toString();
    return url.href;
  });
}
async function githubIdentity(code: string | null, verifier: string, env: Env): Promise<AuthProps> {
  if (!code || code.length > 512 || !env.GITHUB_CLIENT_ID || !env.GITHUB_CLIENT_SECRET) throw new RelayError("github_sign_in", "GitHub sign-in is not configured", 503);
  let reply: Response;
  try { reply = await fetch("https://github.com/login/oauth/access_token", { method: "POST", redirect: "manual", signal: AbortSignal.timeout(10_000),
    headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "Astra-Antigravity-Relay" },
    body: new URLSearchParams({ client_id: env.GITHUB_CLIENT_ID, client_secret: env.GITHUB_CLIENT_SECRET, code,
      redirect_uri: publicOrigin(env) + "/auth/github/callback", code_verifier: verifier }) }); }
  catch { throw new RelayError("github_token_unavailable", "GitHub sign-in could not be contacted. Restart sign-in from your application.", 502); }
  if (reply.status >= 300 && reply.status < 400) throw new RelayError("github_redirect", "GitHub returned an unexpected redirect. Sign-in was stopped.", 502);
  if (!reply.ok) throw new RelayError("github_sign_in", "GitHub could not complete sign-in", 502);
  let exchanged: ObjectValue;
  try { exchanged = object(await boundedJson(new Request("https://bounded.invalid", { method: "POST", body: reply.body, duplex: "half" } as RequestInit))); }
  catch { throw new RelayError("github_token_response", "GitHub sign-in returned an unreadable response. Restart sign-in from your application.", 502); }
  const access = exchanged.access_token;
  if (typeof access !== "string" || access.length > 512) throw new RelayError("github_sign_in", "GitHub authorization was rejected", 401);
  let identity: Response;
  try { identity = await fetch("https://api.github.com/user", { redirect: "manual", signal: AbortSignal.timeout(10_000),
    headers: { Authorization: `Bearer ${access}`, Accept: "application/vnd.github+json", "User-Agent": "Astra-Antigravity-Relay" } }); }
  catch { throw new RelayError("github_identity_unavailable", "GitHub identity could not be contacted. Restart sign-in from your application.", 502); }
  if (identity.status >= 300 && identity.status < 400) throw new RelayError("github_redirect", "GitHub returned an unexpected redirect. Sign-in was stopped.", 502);
  if (!identity.ok) throw new RelayError("github_sign_in", "GitHub identity could not be verified", 401);
  let user: ObjectValue;
  try { user = object(await boundedJson(new Request("https://bounded.invalid", { method: "POST", body: identity.body, duplex: "half" } as RequestInit))); }
  catch { throw new RelayError("github_identity_response", "GitHub identity returned an unreadable response. Restart sign-in from your application.", 502); }
  if (typeof user.id !== "number" || !Number.isSafeInteger(user.id) || user.id <= 0) throw new RelayError("github_sign_in", "GitHub returned an invalid identity", 401);
  // The upstream GitHub token is used only for identity, never stored or sent to a worker.
  // The OAuth provider forbids ':' in grant subjects. Keep the established
  // owner digest so workers paired before this correction keep their identity.
  return { userId: "github-" + user.id, ownerKey: await digest("github:" + user.id) };
}
function consent(details: ConsentDescription, handle: string, env: Env, preferred: Provider): string {
  const scopes = details.scope.filter(scope => (SCOPES as readonly string[]).includes(scope));
  const labels: Record<string,string> = { "workspaces:read": "See my approved computers and workspaces", "tasks:read": "Read my task status and results", "tasks:write": "Send and manage my authorized tasks", "computers:manage":"Manage my connected computers, startup, updates and uninstall", "account:delete":"Delete my relay account records and revoke application access" };
  return `<section class="panel"><p class="eyebrow">Connection request</p><h2>${html(details.clientName)}</h2>
    <p>Connect this application to the computers and workspaces you approve.</p>
    <dl class="facts"><dt>Application domain</dt><dd>${html(details.clientDomain || "Self-declared client")}</dd>
      <dt>Return destination</dt><dd>${html(details.redirectHost)}</dd></dl>
    ${details.redirectIsLoopback ? '<p class="status status--attention">Continue only if you started sign-in from the application on your computer.</p>' : ""}</section>
    <form method="post" class="stack"><input type="hidden" name="handle" value="${html(handle)}">
      <fieldset class="panel"><legend>Choose what this application can do</legend>${scopes.map(scope =>
        `<label class="choice"><input type="checkbox" name="scope" value="${html(scope)}" checked><span>${html(labels[scope])}</span></label>`).join("")}
        <p class="help">Tasks can consume your provider quotas and modify approved workspaces. Provider credentials stay on your computer.</p></fieldset>
      ${entraConfigured(env) ? `<fieldset class="panel"><legend>How you sign in</legend>${githubConfigured(env) ? `<label class="choice"><input type="radio" name="provider" value="github"${preferred === "github" ? " checked" : ""}><span>GitHub</span></label>` : ""}
        <label class="choice"><input type="radio" name="provider" value="entra"${preferred === "entra" ? " checked" : ""}><span>Email or Google</span></label>
        <p class="help">Choose the account connected to your computer. Accounts from different sign-in providers remain separate.</p></fieldset>` : ""}
      <div class="action-group"><button name="decision" value="approve">${entraConfigured(env) ? "Continue" : "Continue with GitHub"}</button><button class="secondary" name="decision" value="deny">Decline</button></div>
      <p class="help">You can revoke computer access from your account. <a href="/privacy">Privacy notice</a></p></form>`;
}
function cookie(request: Request, name: string): string | undefined {
  return request.headers.get("Cookie")?.split(";").map(p => p.trim()).find(p => p.startsWith(name + "="))?.slice(name.length + 1);
}
export async function managementSession(request: Request, env: Env): Promise<ObjectValue | null> {
  const value = cookie(request, "__Host-astra-session");
  if (!value) return null;
  let session: ObjectValue;
  try { session = await unseal(value, env.SESSION_SECRET); }
  catch { return null; }
  if (typeof session.expires !== "number" || session.expires <= Date.now() || typeof session.ownerKey !== "string"
    || typeof session.csrf !== "string") return null;
  const legacy = typeof session.userId === "string" ? /^github:([1-9][0-9]{0,15})$/.exec(session.userId) : null;
  if (legacy) {
    if (!Number.isSafeInteger(Number(legacy[1])) || session.ownerKey !== await digest(String(session.userId))) return null;
    session.userId = "github-" + legacy[1];
  }
  return session;
}
export async function requireManagementPost(request: Request, env: Env, form: FormData): Promise<ObjectValue> {
  const session = await managementSession(request, env);
  if (!session || request.headers.get("Origin") !== publicOrigin(env) || !equal(String(form.get("csrf")), String(session.csrf)))
    throw new RelayError("csrf", "Sign in again from the relay's own page", 403);
  return session;
}
async function managementSignIn(request: Request, env: Env): Promise<Response> {
  const query = new URL(request.url).searchParams;
  const requested = query.get("next") ?? "/account";
  if (!managementReturnPath(requested)) throw new RelayError("invalid_return", "Invalid sign-in destination");
  if (!query.has("provider") && entraConfigured(env)) {
    const session = await managementSession(request, env), method = accountMethod(session), preferred = preferredProvider(session, env);
    const choices = ([...(githubConfigured(env) ? ["github" as const] : []), "entra" as const]).sort((first, second) => first === second ? 0 : first === preferred ? -1 : 1);
    return page("Sign in", `<section class="panel stack"><h2>Choose a sign-in method</h2>
      <p>Use the same sign-in method and account that you used to connect your computer.</p>
      <div class="stack">${choices.map(provider => `<a class="button${provider === preferred ? "" : " secondary"}" href="/auth/login?${html(new URLSearchParams({ next: requested, provider }).toString())}">${provider === "github" ? "Continue with GitHub" : "Email or Google"}</a>`).join("")}</div>
      <p class="help">Accounts from different sign-in providers remain separate. Switching methods does not move connected computers.</p></section>
      <section class="panel"><h2>Continue setup in ChatGPT</h2><p>Open Computers in ChatGPT to connect your computer, check provider accounts and approve folders. Manage tasks, computer settings and files there.</p><p>The companion runs in the background with no window, tray or local web page. Provider sign-in can open the provider's own browser page on your computer.</p><p>Use the same account in ChatGPT or Codex to access the computers you approve. Tasks use your own provider accounts and quotas.</p></section>`,
      new Headers(), [], { description: "Sign in here, then return to ChatGPT for setup and computer management.", accountMethod: method });
  }
  const provider = selectedProvider(query.get("provider"), env);
  const state = "manage-" + token();
  const verifier = token();
  const nonce = token();
  const binding = token();
  const location = provider === "github" ? await githubUrl(state, verifier, env) : await entraAuthorizationUrl(state, verifier, nonce, env);
  await env.OAUTH_KV.put("management:" + await digest(state), await seal({ next: requested, verifier, binding, provider, nonce,
    expires: Date.now() + 600_000 }, env.SESSION_SECRET), { expirationTtl: 600 });
  return new Response(null, { status: 302, headers: { Location: location,
    "Set-Cookie": `__Host-astra-login=${binding}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=600`, "Cache-Control": "no-store" } });
}
async function managementCallback(request: Request, env: Env, provider: Provider, recovery: { retryPath?: string }): Promise<Response> {
  let stage = "signin_state_check";
  try {
    const url = new URL(request.url), state = url.searchParams.get("state") ?? "";
    if (!/^manage-[a-f0-9]{64}$/.test(state)) throw new RelayError("invalid_state", "Sign-in state is invalid", 401);
    const key = "management:" + await digest(state), saved = await env.OAUTH_KV.get(key);
    if (!saved) throw new RelayError("invalid_state", "Sign-in expired", 401);
    const data = await unseal(saved, env.SESSION_SECRET);
    if (Number(data.expires) <= Date.now() || !equal(cookie(request, "__Host-astra-login") ?? "", String(data.binding)))
      throw new RelayError("invalid_state", "Sign-in belongs to another browser", 401);
    if ((data.provider ?? "github") !== provider) throw new RelayError("invalid_state", "Sign-in provider does not match this callback", 401);
    const next = managementReturnPath(typeof data.next === "string" ? data.next : null);
    if (next) recovery.retryPath = "/auth/login?next=" + encodeURIComponent(next);
    stage = "signin_state_cleanup";
    await env.OAUTH_KV.delete(key);
    if (url.searchParams.has("error")) throw new RelayError("sign_in_denied", "Sign-in was declined. Restart sign-in from your application.", 401);
    stage = provider + "_callback";
    const identity = provider === "github" ? await githubIdentity(url.searchParams.get("code"), String(data.verifier), env)
      : await entraIdentity(url.searchParams.get("code"), String(data.verifier), String(data.nonce), env);
    stage = "signin_session";
    const session = await seal({ ...identity, csrf: token(), expires: Date.now() + 3_600_000 }, env.SESSION_SECRET);
    return new Response(null, { status: 302, headers: { Location: publicOrigin(env) + String(data.next),
      "Set-Cookie": `__Host-astra-session=${session}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=3600`, "Cache-Control": "no-store" } });
  } catch (error) {
    if (relayError(error)) throw error;
    // Expose only a fixed stage code, never an upstream exception message,
    // callback URL, credential, cookie or provider response body.
    throw new RelayError(stage, "Sign-in could not be completed. Restart sign-in from your application.", 502);
  }
}
export async function authPage(request: Request, env: Env, oauth: OAuthHelpers, recovery: { retryPath?: string } = {}): Promise<Response | null> {
  const path = new URL(request.url).pathname;
  if (["/auth/login", "/auth/github/callback", "/auth/entra/callback"].includes(path) && request.method !== "GET") return new Response(null, { status: 405 });
  if (path === "/auth/login") {
    const next = managementReturnPath(new URL(request.url).searchParams.get("next") ?? "/account");
    if (next) recovery.retryPath = "/auth/login?next=" + encodeURIComponent(next);
    return managementSignIn(request, env);
  }
  if (path === "/auth/github/callback" || path === "/auth/entra/callback") {
    const provider: Provider = path === "/auth/entra/callback" ? "entra" : "github";
    if (new URL(request.url).searchParams.get("state")?.startsWith("manage-")) return managementCallback(request, env, provider, recovery);
    const resumed = await oauth.finishUpstream<{ verifier: string; provider?: Provider; nonce?: string }>(request);
    if ((resumed.data.provider ?? "github") !== provider) throw new RelayError("invalid_state", "Sign-in provider does not match this callback", 401);
    if (new URL(request.url).searchParams.has("error")) {
      resumed.headers.set("Location", authorizationErrorRedirect(resumed.request, "access_denied"));
      return new Response(null, { status: 302, headers: resumed.headers });
    }
    const identity = provider === "github" ? await githubIdentity(new URL(request.url).searchParams.get("code"), resumed.data.verifier, env)
      : await entraIdentity(new URL(request.url).searchParams.get("code"), resumed.data.verifier, String(resumed.data.nonce), env);
    const completed = await oauth.completeAuthorization({ request: resumed.request, userId: identity.userId, metadata: {},
      scope: resumed.request.scope, props: identity });
    const management = await seal({...identity,csrf:token(),expires:Date.now()+3_600_000},env.SESSION_SECRET);
    resumed.headers.append("Set-Cookie",`__Host-astra-session=${management}; Secure; HttpOnly; SameSite=Lax; Path=/; Max-Age=3600`);
    resumed.headers.set("Location", completed.redirectTo);
    return new Response(null, { status: 302, headers: resumed.headers });
  }
  if (path !== "/authorize") return null;
  try {
    if (request.method === "GET") {
      const parsed = await oauth.parseAuthRequest(request);
      const details = await oauth.describeConsent(parsed);
      const started = await oauth.beginConsent(parsed);
      // Chromium also checks form-action across redirects. Approval navigates
      // to the selected provider; denial returns to the validated client URI.
      // Other relay forms retain the same-origin-only policy.
      const entra = entraConfigured(env) ? entraConfig(env) : null;
      const session = await managementSession(request, env);
      return page("Connect application", consent(details, started.handle, env, preferredProvider(session, env)), started.headers,
        ["https://github.com", ...(entra ? [new URL(entra.authority).origin, `https://${entra.tenantId}.ciamlogin.com`] : []), parsed.redirectUri],
        { eyebrow: "Review connection", description: "Choose an account and review permissions before continuing.", accountMethod: accountMethod(session) });
    }
    if (request.method !== "POST") return new Response(null, { status: 405 });
    const form = await request.formData();
    if (request.headers.get("Origin") !== publicOrigin(env)) throw new RelayError("csrf", "Authorization must be confirmed on the relay page", 403);
    if (form.get("decision") !== "approve") {
      const denied = await oauth.denyConsent(request, String(form.get("handle")));
      return new Response(null, { status: 302, headers: denied.headers });
    }
    const provider = selectedProvider(form.get("provider"), env);
    const approved = await oauth.approveConsent(request, String(form.get("handle")), { scope: form.getAll("scope").map(String) });
    const verifier = token();
    const nonce = token();
    const upstream = await oauth.beginUpstream(approved.request, { data: { verifier, provider, nonce }, headers: approved.headers });
    upstream.headers.set("Location", provider === "github" ? await githubUrl(upstream.state, verifier, env)
      : await entraAuthorizationUrl(upstream.state, verifier, nonce, env));
    return new Response(null, { status: 302, headers: upstream.headers });
  } catch (error) {
    if (error instanceof AuthorizationError && error.redirectTo) return Response.redirect(error.redirectTo, 302);
    if (error instanceof AuthorizationError || error instanceof CimdFetchError) return page("Restart sign-in",
      '<section class="status status--attention"><h2>Start a new connection request</h2><p>The authorization request expired or the client could not be verified. Restart from your application.</p></section><p>Close this tab, return to the ChatGPT or Codex conversation where you started, and choose Connect again. No task has been restarted.</p><div class="action-group"><a class="button" href="/account">Open your account</a></div>',
      new Headers(), [], { eyebrow: "Connection recovery" });
    throw error;
  }
}
