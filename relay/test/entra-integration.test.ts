import { env, SELF, createExecutionContext } from "cloudflare:test";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { authPage, authorizationServer, managementSession } from "../src/auth";
import { digest, SCOPES } from "../src/contracts";
import relay from "../src/index";

declare module "cloudflare:test" { interface ProvidedEnv extends Env {} }
const ORIGIN = "https://relay.example.test";
const TENANT = "11111111-1111-4111-8111-111111111111";
const CLIENT = "22222222-2222-4222-8222-222222222222";
const OBJECT = "33333333-3333-4333-8333-333333333333";
const AUTHORITY = `https://synthetictenant.ciamlogin.com/${TENANT}`;
const DISCOVERY = AUTHORITY + "/v2.0/.well-known/openid-configuration";
const TOKEN = AUTHORITY + "/oauth2/v2.0/token";
const AUTHORIZE = AUTHORITY + "/oauth2/v2.0/authorize";
const JWKS = AUTHORITY + "/discovery/v2.0/keys";
const entraEnv: Env = { ...env, ENTRA_AUTHORITY: AUTHORITY, ENTRA_TENANT_ID: TENANT,
  ENTRA_CLIENT_ID: CLIENT, ENTRA_CLIENT_SECRET: "synthetic-entra-client-secret", ENTRA_REQUIRED_ROLE: "Astra.User" };
const cookies = (headers: Headers) => headers.getSetCookie().map(value => value.split(";")[0]).join("; ");
const base64url = (data: Uint8Array) => btoa(String.fromCharCode(...data)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const json = (value: unknown) => base64url(new TextEncoder().encode(JSON.stringify(value)));
let key: CryptoKeyPair, publicJwk: JsonWebKey;
beforeAll(async () => {
  key = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  publicJwk = await crypto.subtle.exportKey("jwk", key.publicKey);
});
afterEach(() => vi.restoreAllMocks());

async function signedIdentity(nonce: string, overrides: Record<string, unknown> = {}) {
  const now = Math.floor(Date.now() / 1_000);
  const payload = { iss: AUTHORITY + "/v2.0", aud: CLIENT, tid: TENANT, oid: OBJECT,
    sub: "synthetic-pairwise-subject", ver: "2.0", iat: now, nbf: now - 5, exp: now + 600,
    nonce, roles: ["Astra.User"], email: "same-email@example.test", ...overrides };
  const unsigned = json({ alg: "RS256", kid: "integration-key", typ: "JWT" }) + "." + json(payload);
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key.privateKey, new TextEncoder().encode(unsigned));
  return unsigned + "." + base64url(new Uint8Array(signature));
}

function upstream(claims: Record<string, unknown> = {}, authorize = AUTHORIZE) {
  let nonce = "", challenge = "";
  const requests: string[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const request = new Request(input, init);
    requests.push(request.url);
    expect(request.redirect).toBe("manual");
    if (request.url === DISCOVERY) return Response.json({ issuer: AUTHORITY + "/v2.0",
      authorization_endpoint: authorize, token_endpoint: TOKEN, jwks_uri: JWKS,
      response_types_supported: ["code"], subject_types_supported: ["pairwise"],
      id_token_signing_alg_values_supported: ["RS256"], token_endpoint_auth_methods_supported: ["client_secret_post"] });
    if (request.url === JWKS) return Response.json({ keys: [{ ...publicJwk, kid: "integration-key", alg: "RS256", use: "sig" }] });
    if (request.url === TOKEN) {
      expect(request.method).toBe("POST");
      const body = await request.formData();
      expect(body.get("grant_type")).toBe("authorization_code");
      expect(body.get("code")).toBe("synthetic-entra-code");
      expect(body.get("client_id")).toBe(CLIENT);
      expect(body.get("client_secret")).toBe("synthetic-entra-client-secret");
      expect(body.get("redirect_uri")).toBe(ORIGIN + "/auth/entra/callback");
      const verifier = String(body.get("code_verifier"));
      expect(verifier.length).toBeGreaterThanOrEqual(43);
      expect(base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))))).toBe(challenge);
      return Response.json({ token_type: "Bearer", access_token: "synthetic-entra-access-token",
        refresh_token: "synthetic-entra-refresh-token", id_token: await signedIdentity(nonce, claims), expires_in: 600 });
    }
    throw new Error("Unexpected synthetic identity request: " + request.url);
  });
  return { spy, requests, capture(location: string) {
    const locationUrl = new URL(location);
    expect(locationUrl.origin + locationUrl.pathname).toBe(authorize);
    expect(locationUrl.searchParams.get("response_type")).toBe("code");
    expect(locationUrl.searchParams.get("response_mode")).toBe("query");
    expect(locationUrl.searchParams.get("scope")!.split(" ")).toContain("openid");
    expect(locationUrl.searchParams.get("scope")!).not.toMatch(/offline_access|User\.Read/);
    expect(locationUrl.searchParams.get("code_challenge_method")).toBe("S256");
    nonce = locationUrl.searchParams.get("nonce")!;
    challenge = locationUrl.searchParams.get("code_challenge")!;
    expect(nonce.length).toBeGreaterThanOrEqual(32);
    expect(challenge).toHaveLength(43);
    return locationUrl.searchParams.get("state")!;
  } };
}

async function managementLogin(provider = "entra", next = "/account", actual = entraEnv) {
  return (await authPage(new Request(ORIGIN + "/auth/login?" + new URLSearchParams({ provider, next })),
    actual, authorizationServer(actual).getOAuthApi(actual)))!;
}
async function callback(start: Response, state: string, provider = "entra", actual = entraEnv, browserCookies = cookies(start.headers)) {
  return authPage(new Request(ORIGIN + `/auth/${provider}/callback?` + new URLSearchParams({ state, code: "synthetic-entra-code" }),
    { headers: { Cookie: browserCookies } }), actual, authorizationServer(actual).getOAuthApi(actual));
}
async function mcpConsent() {
  const server = authorizationServer(entraEnv), helpers = server.getOAuthApi(entraEnv);
  const redirectUri = "http://127.0.0.1:12345/callback";
  const client = await helpers.createClient({ clientName: "Entra integration fixture", redirectUris: [redirectUri],
    tokenEndpointAuthMethod: "none", grantTypes: ["authorization_code", "refresh_token"], responseTypes: ["code"] });
  const verifier = "v".repeat(64), challenge = base64url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  const query = new URLSearchParams({ client_id: client.clientId, redirect_uri: redirectUri, response_type: "code",
    scope: SCOPES.join(" "), resource: ORIGIN + "/mcp", state: "synthetic-client-state", code_challenge: challenge, code_challenge_method: "S256" });
  const consent = (await authPage(new Request(ORIGIN + "/authorize?" + query), entraEnv, helpers))!;
  expect(consent.status).toBe(200);
  const consentHtml = await consent.text(), handle = /name="handle" value="([^"]+)"/.exec(consentHtml)![1];
  const approved = (await authPage(new Request(ORIGIN + "/authorize", { method: "POST",
    headers: { Origin: ORIGIN, Cookie: cookies(consent.headers) }, body: new URLSearchParams([
      ["handle", handle], ["decision", "approve"], ["provider", "entra"], ...SCOPES.map(scope => ["scope", scope]) ]) }), entraEnv, helpers))!;
  return { server, helpers, client, redirectUri, verifier, consent, consentHtml, approved };
}

describe("Entra browser and MCP integration", () => {
  it("offers a provider choice preserving the destination and keeps GitHub-only deployment behavior", async () => {
    const next = "/pair?code=" + "a".repeat(24);
    const picker = await authPage(new Request(ORIGIN + "/auth/login?next=" + encodeURIComponent(next)), entraEnv,
      authorizationServer(entraEnv).getOAuthApi(entraEnv));
    expect(picker!.status).toBe(200);
    const html = await picker!.text();
    expect(html).toContain("GitHub");
    expect(html).toContain("Email or Google");
    expect(html).not.toContain("Microsoft");
    expect(html).toContain("provider=entra");
    expect(html).toContain("provider=github");
    expect(picker!.headers.get("Content-Security-Policy")).toContain("form-action 'self';");
    const githubOnly = await authPage(new Request(ORIGIN + "/auth/login?next=" + encodeURIComponent(next)), env,
      authorizationServer(env).getOAuthApi(env));
    expect(githubOnly!.status).toBe(302);
    expect(new URL(githubOnly!.headers.get("Location")!).hostname).toBe("github.com");
    const explicitGithub = await managementLogin("github", next);
    expect(explicitGithub.status).toBe(302);
    expect(new URL(explicitGithub.headers.get("Location")!).pathname).toBe("/login/oauth/authorize");
  });

  it("finishes Entra management PKCE sign-in, pairs the owned worker, and keeps another provider's owner isolated", async () => {
    const fixture = upstream(), login = await managementLogin("entra", "/connect?request_id=synthetic-task");
    const state = fixture.capture(login.headers.get("Location")!);
    const completed = (await callback(login, state))!;
    expect(completed.status).toBe(302);
    expect(completed.headers.get("Location")).toBe(ORIGIN + "/connect?request_id=synthetic-task");
    const browserCookies = cookies(completed.headers);
    const session = (await managementSession(new Request(ORIGIN + "/account", { headers: { Cookie: browserCookies } }), entraEnv))!;
    const expectedOwner = await digest(`entra:${TENANT}:${OBJECT}`);
    expect(session).toMatchObject({ userId: "entra-" + expectedOwner, ownerKey: expectedOwner });
    expect(JSON.stringify(session)).not.toMatch(/synthetic-entra-(?:access|refresh)-token|same-email@example\.test|synthetic-entra-client-secret/);
    const pair = await SELF.fetch(ORIGIN + "/worker/pair", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: "Entra fixture worker", credential_hash: await digest("x".repeat(64)),
        workspaces: [{ id: "project", label: "Synthetic project", capabilities: ["local_files"], isolation: ["worktree"], task_types: ["analysis"] }] }) });
    expect(pair.status).toBe(201);
    const receipt = await pair.json<{ code: string; claim: string }>();
    const approval = await relay.fetch(new Request(ORIGIN + "/pair?code=" + receipt.code,
      { method: "POST", headers: { Cookie: browserCookies, Origin: ORIGIN },
        body: new URLSearchParams({ csrf: String(session.csrf), decision: "approve" }) }), entraEnv, createExecutionContext());
    expect(approval.status).toBe(200);
    const owned = await env.OWNERS.getByName(expectedOwner).workspaces();
    expect(JSON.stringify(owned)).toContain("Entra fixture worker");
    expect(JSON.stringify(await env.OWNERS.getByName(await digest("github:12345")).workspaces())).not.toContain("Entra fixture worker");
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(1);
    const replay = await relay.fetch(new Request(ORIGIN + "/auth/entra/callback?" + new URLSearchParams({ state, code: "synthetic-entra-code" }),
      { headers: { Cookie: cookies(login.headers) } }), entraEnv, createExecutionContext());
    expect(replay.status).toBe(401);
    expect(replay.headers.get("Set-Cookie")).toBeNull();
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(1);
  });

  it("completes Entra consent and binds the resulting MCP authorization to the same provider owner", async () => {
    const fixture = upstream(), { server, client, redirectUri, verifier, consent, consentHtml, approved } = await mcpConsent();
    expect(consentHtml).toContain("Email or Google");
    expect(consentHtml).not.toContain("Microsoft");
    const policy = consent.headers.get("Content-Security-Policy")!;
    expect(policy).toContain("https://synthetictenant.ciamlogin.com");
    expect(policy).not.toContain("*");
    const upstreamState = fixture.capture(approved.headers.get("Location")!);
    const completed = (await callback(approved, upstreamState))!;
    expect(completed.status).toBe(302);
    const destination = new URL(completed.headers.get("Location")!);
    expect(destination.origin + destination.pathname).toBe(redirectUri);
    expect(destination.searchParams.get("state")).toBe("synthetic-client-state");
    expect(destination.searchParams.get("iss")).toBe(ORIGIN);
    const exchange = await SELF.fetch(ORIGIN + "/oauth/token", { method: "POST", body: new URLSearchParams({ grant_type: "authorization_code",
      client_id: client.clientId, code: destination.searchParams.get("code")!, code_verifier: verifier, redirect_uri: redirectUri, resource: ORIGIN + "/mcp" }) });
    expect(exchange.status).toBe(200);
    const issued = await exchange.json<{ access_token: string; refresh_token: string }>();
    const authenticated = await server.validateToken<{ userId: string; ownerKey: string }>(ORIGIN + "/mcp", issued.access_token, entraEnv);
    const expectedOwner = await digest(`entra:${TENANT}:${OBJECT}`);
    expect(authenticated!.props).toMatchObject({ userId: "entra-" + expectedOwner, ownerKey: expectedOwner });
    expect(authenticated!.scope).toEqual([...SCOPES]);
    expect(JSON.stringify(authenticated)).not.toMatch(/synthetic-entra-(?:access|refresh)-token|same-email@example\.test/);
    expect(await managementSession(new Request(ORIGIN + "/account", { headers: { Cookie: cookies(completed.headers) } }), entraEnv))
      .toMatchObject({ userId: "entra-" + expectedOwner, ownerKey: expectedOwner });
    // The completed upstream transaction and the relay authorization code are
    // independently single-use; neither replay may exchange an Entra token.
    await expect(callback(approved, upstreamState)).rejects.toThrow();
    const replay = await SELF.fetch(ORIGIN + "/oauth/token", { method: "POST", body: new URLSearchParams({ grant_type: "authorization_code",
      client_id: client.clientId, code: destination.searchParams.get("code")!, code_verifier: verifier, redirect_uri: redirectUri, resource: ORIGIN + "/mcp" }) });
    expect(replay.status).toBe(400);
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(1);
  });

  it("rejects a cross-provider management callback and an unrelated browser before contacting the token endpoint", async () => {
    const fixture = upstream(), login = await managementLogin(), state = fixture.capture(login.headers.get("Location")!);
    for (const [provider, browserCookies] of [["github", cookies(login.headers)], ["entra", "__Host-astra-login=unrelated-browser"]]) {
      const reply = await relay.fetch(new Request(ORIGIN + `/auth/${provider}/callback?` + new URLSearchParams({ state, code: "synthetic-entra-code" }),
        { headers: { Cookie: browserCookies } }), entraEnv, createExecutionContext());
      expect(reply.status).toBe(401);
      expect(reply.headers.get("Set-Cookie")).toBeNull();
    }
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(0);
    expect(fixture.requests.some(url => url.includes("github.com"))).toBe(false);
    expect((await callback(login, state))!.status).toBe(302);
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(1);
  });

  it("rejects MCP upstream state at another provider without exchanging or granting credentials", async () => {
    const isolatedObject = crypto.randomUUID(), fixture = upstream({ oid: isolatedObject }), { approved, helpers } = await mcpConsent();
    const state = fixture.capture(approved.headers.get("Location")!);
    await expect(callback(approved, state, "github")).rejects.toMatchObject({ code: "invalid_state", status: 401 });
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(0);
    expect(fixture.requests.some(url => url.includes("github.com"))).toBe(false);
    expect((await helpers.listUserGrants("entra-" + await digest(`entra:${TENANT}:${isolatedObject}`), { limit: 100 })).items).toEqual([]);
  });

  it("allows the tenant UUID authorization host through consent CSP without broadening to other tenants", async () => {
    const tenantOrigin = `https://${TENANT}.ciamlogin.com`;
    const fixture = upstream({}, `${tenantOrigin}/${TENANT}/oauth2/v2.0/authorize`), { consent, approved } = await mcpConsent();
    const policy = consent.headers.get("Content-Security-Policy")!;
    expect(policy).toContain(tenantOrigin);
    expect(policy).toContain("https://synthetictenant.ciamlogin.com");
    expect(policy).not.toMatch(/\*|https:\/\/ciamlogin\.com(?:\s|;)/);
    const state = fixture.capture(approved.headers.get("Location")!);
    expect((await callback(approved, state))!.status).toBe(302);
  });

  it("returns upstream consent denial to the validated MCP client without exposing provider details or minting a session", async () => {
    const fixture = upstream(), { approved, redirectUri, helpers } = await mcpConsent();
    const state = fixture.capture(approved.headers.get("Location")!);
    const denied = (await authPage(new Request(ORIGIN + "/auth/entra/callback?" + new URLSearchParams({ state,
      error: "access_denied", error_description: "synthetic-private-provider-detail" }), { headers: { Cookie: cookies(approved.headers) } }), entraEnv, helpers))!;
    expect(denied.status).toBe(302);
    const destination = new URL(denied.headers.get("Location")!);
    expect(destination.origin + destination.pathname).toBe(redirectUri);
    expect(destination.searchParams.get("state")).toBe("synthetic-client-state");
    expect(destination.searchParams.get("error")).toBe("access_denied");
    expect(destination.href).not.toContain("synthetic-private-provider-detail");
    expect(denied.headers.getSetCookie().some(value => value.startsWith("__Host-astra-session="))).toBe(false);
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(0);
  });

  it("expires management state before exchanging an authorization code", async () => {
    const fixture = upstream(), login = await managementLogin(), state = fixture.capture(login.headers.get("Location")!);
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 601_000);
    await expect(callback(login, state)).rejects.toMatchObject({ code: "invalid_state", status: 401 });
    expect(fixture.requests.filter(url => url === TOKEN)).toHaveLength(0);
  });

  it("fails closed for explicit Entra selection with missing configuration and rejects callback POSTs", async () => {
    const outbound = vi.spyOn(globalThis, "fetch").mockImplementation(async () => { throw new Error("No provider should be contacted"); });
    for (const actual of [env, { ...env, ENTRA_TENANT_ID: TENANT }]) {
      const response = await relay.fetch(new Request(ORIGIN + "/auth/login?provider=entra&next=%2Faccount"), actual, createExecutionContext());
      expect(response.status).toBe(503);
      expect(response.headers.get("Location")).toBeNull();
      expect(response.headers.get("Set-Cookie")).toBeNull();
    }
    const denied = await relay.fetch(new Request(ORIGIN + "/auth/entra/callback", { method: "POST", body: "synthetic-code" }), entraEnv, createExecutionContext());
    expect(denied.status).toBe(405);
    expect(outbound).not.toHaveBeenCalled();
  });

  it("does not create a management session for an identity missing a configured Astra role", async () => {
    const fixture = upstream({ roles: ["Analytics.Customer"] }), login = await managementLogin();
    const state = fixture.capture(login.headers.get("Location")!);
    const reply = await relay.fetch(new Request(ORIGIN + "/auth/entra/callback?" + new URLSearchParams({ state, code: "synthetic-entra-code" }),
      { headers: { Cookie: cookies(login.headers) } }), entraEnv, createExecutionContext());
    expect(reply.status).toBe(403);
    expect(reply.headers.get("Set-Cookie")).toBeNull();
    const responseText = await reply.text();
    expect(responseText).not.toMatch(/synthetic-entra-(?:code|access-token|refresh-token|client-secret)|same-email@example\.test/);
  });

  it("supports self-service sign-in without a role gate while retaining tenant and provider ownership boundaries", async () => {
    const actual = { ...entraEnv, ENTRA_REQUIRED_ROLE: undefined }, isolatedObject = crypto.randomUUID();
    const fixture = upstream({ oid: isolatedObject, roles: undefined });
    const login = await managementLogin("entra", "/account", actual), state = fixture.capture(login.headers.get("Location")!);
    const completed = (await callback(login, state, "entra", actual))!;
    expect(completed.status).toBe(302);
    const session = await managementSession(new Request(ORIGIN + "/account", { headers: { Cookie: cookies(completed.headers) } }), actual);
    expect(session).toMatchObject({ ownerKey: await digest(`entra:${TENANT}:${isolatedObject}`) });
    expect(session!.ownerKey).not.toBe(await digest("github:12345"));
  });
});
