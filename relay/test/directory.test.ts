import { env, evictDurableObject, runInDurableObject } from "cloudflare:test";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { exportJWK, generateKeyPair, SignJWT, type JWTPayload } from "jose";
import { directoryConfig, directoryHook, DirectoryMembership, type DirectoryHookConfig } from "../src/directory";
import { digest } from "../src/contracts";

const TENANT = "11111111-1111-4111-8111-111111111111", AUDIENCE = "22222222-2222-4222-8222-222222222222";
const USER = "33333333-3333-4333-8333-333333333333", OTHER_USER = "33333333-3333-4333-8333-444444444444";
const ASTRA = "44444444-4444-4444-8444-444444444444", ANALYTICS = "55555555-5555-4555-8555-555555555555";
const GROUP_A = "66666666-6666-4666-8666-666666666666", GROUP_B = "77777777-7777-4777-8777-777777777777";
const WRITER = "88888888-8888-4888-8888-888888888888", UNKNOWN_GROUP = "99999999-9999-4999-8999-999999999999";
const ATTRIBUTE = "extension_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_registrationSource";
const CALLER = "99045fe1-7639-4a75-9d4a-577b6ca3810f";
const POLICY: DirectoryHookConfig = { tenantId: TENANT, audience: AUDIENCE, originAttribute: ATTRIBUTE,
  attributeCollection: { listenerId: "a1111111-1111-4111-8111-111111111111", extensionId: "a2222222-2222-4222-8222-222222222222" },
  tokenIssuance: { listenerId: "b1111111-1111-4111-8111-111111111111", extensionId: "b2222222-2222-4222-8222-222222222222" },
  applications: { [ASTRA]: { registrationSource: "astra-antigravity", groupIds: [GROUP_A] },
    [ANALYTICS]: { registrationSource: "conversational-analytics", groupIds: [GROUP_B] } } };
const ISSUER = `https://${TENANT}.ciamlogin.com/${TENANT}/v2.0`;
const CANONICAL = `https://${TENANT}.ciamlogin.com/${TENANT}`;
const TOKEN = `https://login.microsoftonline.com/${TENANT}/oauth2/v2.0/token`;
const groupUrl = (group: string) => `https://graph.microsoft.com/v1.0/groups/${group}/members/$ref`;
let serial = 0;
function configured(policy: DirectoryHookConfig = POLICY): Env {
  return { ...env, ENTRA_TENANT_ID: TENANT, ENTRA_AUTHORITY: `https://directory-test-${++serial}.ciamlogin.com/${TENANT}`,
    ENTRA_CLIENT_ID: ASTRA, DIRECTORY_HOOK_CONFIG: JSON.stringify(policy), DIRECTORY_GRAPH_CLIENT_ID: WRITER,
    DIRECTORY_GRAPH_CLIENT_SECRET: "synthetic-writer-secret" };
}
let signing: Awaited<ReturnType<typeof generateKeyPair>>, wrong: Awaited<ReturnType<typeof generateKeyPair>>;
let jwk: Awaited<ReturnType<typeof exportJWK>>;
let clock = Date.now();
beforeEach(() => {
  // Expire cached public verification keys between tests without reloading the
  // Worker's module and invalidating unrelated Durable Object instances.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(clock += 301_000);
});
beforeAll(async () => {
  signing = await generateKeyPair("RS256", { extractable: true });
  wrong = await generateKeyPair("RS256", { extractable: true });
  jwk = { ...await exportJWK(signing.publicKey), kid: "directory-key", alg: "RS256", use: "sig" };
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });
async function signed(patch: JWTPayload = {}, badKey = false) {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ iss: ISSUER, aud: AUDIENCE, tid: TENANT, azp: CALLER, ver: "2.0",
    iat: now, nbf: now - 5, exp: now + 600, ...patch }).setProtectedHeader({ alg: "RS256", kid: "directory-key" })
    .sign((badKey ? wrong : signing).privateKey);
}
function upstream(actual: Env, metadata: Record<string, unknown> = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const request = new Request(input, init);
    expect(request.redirect).toBe("manual");
    if (request.url === CANONICAL + "/v2.0/.well-known/openid-configuration")
      return Response.json({ issuer: ISSUER, jwks_uri: CANONICAL + "/discovery/v2.0/keys", ...metadata });
    if (request.url === CANONICAL + "/discovery/v2.0/keys") return Response.json({ keys: [jwk] });
    throw new Error("Unexpected outbound directory request");
  });
}
type Stage = "attributeCollection" | "tokenIssuance";
function event(stage: Stage, client = ASTRA) {
  const submit = stage === "attributeCollection";
  return { type: "microsoft.graph.authenticationEvent." + (submit ? "attributeCollectionSubmit" : "tokenIssuanceStart"),
    source: `/tenants/${TENANT}/applications/${client}`, data: {
      "@odata.type": "microsoft.graph." + (submit ? "onAttributeCollectionSubmit" : "onTokenIssuanceStart") + "CalloutData",
      tenantId: TENANT, authenticationEventListenerId: POLICY[stage].listenerId, customAuthenticationExtensionId: POLICY[stage].extensionId,
      authenticationContext: { clientServicePrincipal: { appId: client }, user: { id: USER, createdDateTime: "2020-01-01T00:00:00Z",
        [ATTRIBUTE]: "astra-antigravity", mail: "private@example.test" } },
      userSignUpInfo: { attributes: { [ATTRIBUTE]: { "@odata.type": "microsoft.graph.stringDirectoryAttributeValue",
        attributeType: "directorySchemaExtension", value: "spoofed-product" } } } } };
}
async function request(stage: Stage, body: unknown = event(stage), claims: JWTPayload = {}, badKey = false) {
  const path = stage === "attributeCollection" ? "attribute-collection-submit" : "token-issuance-start";
  return new Request("https://relay.example.test/directory/" + path, { method: "POST", headers: {
    Authorization: "Bearer " + await signed(claims, badKey), "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
function fixture(actual = configured()) {
  const stub = env.DIRECTORY_MEMBERSHIP.getByName("directory-test-" + crypto.randomUUID());
  return { actual, stub,
    enqueue: (client = ASTRA, user = USER) => runInDurableObject(stub, async (_instance, state) =>
      new DirectoryMembership(state, actual).enqueue(TENANT, user, client)),
    alarm: () => runInDurableObject(stub, async (_instance, state) => new DirectoryMembership(state, actual).alarm()),
    due: () => runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec("UPDATE directory_memberships SET next_attempt=0 WHERE state='pending'");
    }),
    rows: () => runInDurableObject(stub, async (_instance, state) => state.storage.sql.exec("SELECT * FROM directory_memberships ORDER BY group_id").toArray()),
    subjects: () => runInDurableObject(stub, async (_instance, state) => state.storage.sql.exec("SELECT * FROM directory_subject").toArray()) };
}
function graph(responses: Array<() => Response>) {
  const requests: Request[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const request = new Request(input, init);
    requests.push(request);
    expect(request.redirect).toBe("manual");
    if (request.url === TOKEN) {
      const form = await request.formData();
      expect(form.get("client_id")).toBe(WRITER);
      expect(form.get("client_secret")).toBe("synthetic-writer-secret");
      expect(form.get("grant_type")).toBe("client_credentials");
      expect(form.get("scope")).toBe("https://graph.microsoft.com/.default");
      return Response.json({ token_type: "Bearer", access_token: "synthetic-graph-access-token" });
    }
    expect(request.method).toBe("POST");
    expect([groupUrl(GROUP_A), groupUrl(GROUP_B)]).toContain(request.url);
    expect(request.headers.get("Authorization")).toBe("Bearer synthetic-graph-access-token");
    expect(await request.clone().json()).toEqual({ "@odata.id": `https://graph.microsoft.com/v1.0/directoryObjects/${USER}` });
    const response = responses.shift();
    if (!response) throw new Error("Missing synthetic Graph result");
    return response();
  });
  return { requests, spy, membershipRequests: () => requests.filter(value => value.url.startsWith("https://graph.microsoft.com/")) };
}
const duplicate = (status = 400) => Response.json({ error: { code: "Request_BadRequest",
  message: "One or more added object references already exist for the following modified properties: 'members'." } }, { status });

describe("Entra directory event boundary", () => {
  it("verifies directory callbacks through canonical CIAM when browser login uses a custom domain", async () => {
    const actual = { ...configured(), ENTRA_AUTHORITY: `https://login.example.test/${TENANT}`, ENTRA_CUSTOM_DOMAIN: "login.example.test" };
    expect(directoryConfig(actual)?.authority).toBe(CANONICAL);
    const network = upstream(actual);
    expect((await directoryHook(await request("attributeCollection"), actual))!.status).toBe(200);
    expect(network.mock.calls.map(([url]) => String(url)).sort()).toEqual([
      CANONICAL + "/discovery/v2.0/keys", CANONICAL + "/v2.0/.well-known/openid-configuration" ].sort());
    expect(network.mock.calls.every(([url]) => !String(url).includes("login.example.test"))).toBe(true);
    expect(() => directoryConfig({ ...actual, ENTRA_CUSTOM_DOMAIN: undefined })).toThrow();
  });
  it("keeps unconfigured deployments disabled and requires bounded, distinct app/group settings", async () => {
    expect(directoryConfig(env)).toBeNull();
    expect((await directoryHook(new Request("https://relay.example.test/directory/token-issuance-start"), env))!.status).toBe(404);
    expect(await directoryHook(new Request("https://relay.example.test/other"), env)).toBeNull();
    const actual = configured();
    expect(directoryConfig(actual)?.applications[ASTRA].groupIds).toEqual([GROUP_A]);
    for (const policy of [ { ...POLICY, audience: "https://arbitrary.example" },
      { ...POLICY, audience: "AAAA2222-2222-4222-8222-222222222222" },
      { ...POLICY, originAttribute: "extension_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA_registrationSource" },
      { ...POLICY, tokenIssuance: { ...POLICY.tokenIssuance, listenerId: POLICY.attributeCollection.listenerId } },
      { ...POLICY, applications: { [ASTRA]: { registrationSource: "1astra", groupIds: [GROUP_A] } } },
      { ...POLICY, applications: { [ASTRA]: POLICY.applications[ASTRA], [ANALYTICS]: POLICY.applications[ASTRA] } },
      { ...POLICY, applications: { [ASTRA]: { ...POLICY.applications[ASTRA], groupIds: [GROUP_A, GROUP_B] } } } ])
      expect(() => directoryConfig({ ...actual, DIRECTORY_HOOK_CONFIG: JSON.stringify(policy) })).toThrow();
    expect(() => directoryConfig({ ...actual, ENTRA_AUTHORITY: "https://evil.example/" + TENANT })).toThrow();
    expect(() => directoryConfig({ ...actual, ENTRA_TENANT_ID: OTHER_USER })).toThrow();
  });
  it.each([
    ["caller", { azp: ASTRA }], ["audience", { aud: ASTRA }], ["tenant", { tid: OTHER_USER }],
    ["issuer", { iss: `https://login.microsoftonline.com/${TENANT}/v2.0` }],
    ["v1", { ver: "1.0", appid: CALLER, azp: undefined }], ["delegated scope", { scp: "access_as_user" }],
    ["expired", { exp: 1000 }], ["future", { nbf: Math.floor(Date.now() / 1000) + 3600 }],
    ["array audience", { aud: [AUDIENCE, ASTRA] }], ["conflicting appid", { appid: ASTRA }]
  ])("rejects signed events with invalid %s before using the user or client payload", async (_label, claims) => {
    const actual = configured(), network = upstream(actual);
    await expect(directoryHook(await request("tokenIssuance", event("tokenIssuance"), claims), actual)).rejects.toMatchObject({ code: "directory_auth", status: 401 });
    expect(network.mock.calls.every(([url]) => String(url).includes("ciamlogin.com"))).toBe(true);
  });
  it("rejects a forged signature and does not accept payload-only authentication", async () => {
    const actual = configured(); upstream(actual);
    await expect(directoryHook(await request("tokenIssuance", event("tokenIssuance"), {}, true), actual)).rejects.toMatchObject({ code: "directory_auth" });
    const unsigned = new Request("https://relay.example.test/directory/token-issuance-start", { method: "POST", body: JSON.stringify(event("tokenIssuance")) });
    await expect(directoryHook(unsigned, actual)).rejects.toMatchObject({ code: "directory_auth" });
  });
  it.each([{ issuer: "https://evil.example/" }, { jwks_uri: "https://evil.example/keys" },
    { issuer: `https://${OTHER_USER}.ciamlogin.com/${OTHER_USER}/v2.0` }])("rejects metadata that escapes its pinned tenant", async metadata => {
    const actual = configured(), network = upstream(actual, metadata);
    await expect(directoryHook(await request("attributeCollection"), actual)).rejects.toMatchObject({ code: "directory_unavailable" });
    expect(network.mock.calls.some(([url]) => String(url).includes("evil.example"))).toBe(false);
  });
  it("overwrites a spoofed source from the trusted client map and caches only public verification data", async () => {
    const actual = configured(), network = upstream(actual);
    const first = await directoryHook(await request("attributeCollection"), actual);
    expect(await first!.json()).toEqual({ data: { "@odata.type": "microsoft.graph.onAttributeCollectionSubmitResponseData",
      actions: [{ "@odata.type": "microsoft.graph.attributeCollectionSubmit.modifyAttributeValues", attributes: { [ATTRIBUTE]: "astra-antigravity" } }] } });
    const second = await directoryHook(await request("attributeCollection", event("attributeCollection", ANALYTICS)), actual);
    expect(JSON.stringify(await second!.json())).toContain("conversational-analytics");
    expect(network).toHaveBeenCalledTimes(2);
  });
  it.each(["client", "tenant", "listener", "extension", "type", "source", "different resource", "missing attribute"])("rejects disallowed or misconfigured %s", async scenario => {
    const actual = configured(); upstream(actual);
    const body = event("attributeCollection");
    if (scenario === "client") body.data.authenticationContext.clientServicePrincipal.appId = WRITER;
    if (scenario === "tenant") body.data.tenantId = OTHER_USER;
    if (scenario === "listener") body.data.authenticationEventListenerId = POLICY.tokenIssuance.listenerId;
    if (scenario === "extension") body.data.customAuthenticationExtensionId = POLICY.tokenIssuance.extensionId;
    if (scenario === "type") body.type = "microsoft.graph.authenticationEvent.tokenIssuanceStart";
    if (scenario === "source") body.source = `/tenants/${OTHER_USER}/applications/${ASTRA}`;
    if (scenario === "different resource") body.source = `/tenants/${TENANT}/applications/${ANALYTICS}`;
    if (scenario === "missing attribute") body.data.userSignUpInfo.attributes = {};
    await expect(directoryHook(await request("attributeCollection", body), actual)).rejects.toThrow();
  });
  it("preserves existing-user origin on a secondary product sign-in and awaits only durable enqueue", async () => {
    const f = fixture(), network = upstream(f.actual), expectedKey = await digest(`directory:${TENANT}:${USER}`);
    const queued: string[][] = [];
    // Replace just the internal routing boundary; execute the real object with
    // the same storage and the synthetic bindings used by this request.
    const actual: Env = { ...f.actual, DIRECTORY_MEMBERSHIP: new Proxy(env.DIRECTORY_MEMBERSHIP, { get(target, key) {
      if (key === "getByName") return (name: string) => {
        expect(name).toBe(expectedKey);
        return { enqueue: async (tenant: string, user: string, client: string) => {
          queued.push([tenant, user, client]); await f.enqueue(client, user);
        } };
      };
      return Reflect.get(target, key);
    } }) };
    const response = await directoryHook(await request("tokenIssuance", event("tokenIssuance", ANALYTICS)), actual);
    expect(await response!.json()).toEqual({ data: { "@odata.type": "microsoft.graph.onTokenIssuanceStartResponseData",
      actions: [{ "@odata.type": "microsoft.graph.tokenIssuanceStart.provideClaimsForToken", claims: {} }] } });
    expect(queued).toEqual([[TENANT, USER, ANALYTICS]]);
    expect(await f.rows()).toMatchObject([{ group_id: GROUP_B, state: "pending", attempts: 0 }]);
    expect(network.mock.calls.every(([url]) => String(url).includes("ciamlogin.com"))).toBe(true);
    expect(JSON.stringify(await f.subjects())).not.toMatch(/33333333|private@example|synthetic-graph|Bearer/);
  });
});

describe("durable product membership outbox", () => {
  it("deduplicates repeated sign-ins across eviction and removes the encrypted pending subject on success", async () => {
    const f = fixture(); await f.enqueue(); await f.enqueue();
    await evictDurableObject(f.stub);
    expect(await f.rows()).toHaveLength(1);
    const network = graph([() => new Response(null, { status: 204 })]);
    await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ group_id: GROUP_A, state: "complete", attempts: 1 }]);
    expect(await f.subjects()).toMatchObject([{ encrypted_subject: null }]);
    await f.enqueue(); await evictDurableObject(f.stub); await f.alarm();
    expect(network.membershipRequests()).toHaveLength(1);
    expect(await f.subjects()).toMatchObject([{ encrypted_subject: null }]);
  });
  it.each([400, 409])("recognizes only the exact already-member Graph result at HTTP %i", async status => {
    const f = fixture(); await f.enqueue();
    graph([() => duplicate(status)]); await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ state: "complete", attempts: 1 }]);
  });
  it.each([
    () => Response.json({ error: { code: "Request_BadRequest", message: "unsupported member" } }, { status: 400 }),
    () => Response.json({ error: { code: "Conflict", message: "One or more added object references already exist for the following modified properties: 'members'." } }, { status: 409 }),
    () => Response.json({ error: { code: "Request_BadRequest", message: "One or more added object references already exist for the following modified properties: 'owners'." } }, { status: 400 }),
    () => new Response(null, { status: 403 })
  ])("does not treat other Graph client errors as duplicate success", async response => {
    const f = fixture(); await f.enqueue(); graph([response]); await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ state: "failed", attempts: 1 }]);
    expect(await f.subjects()).toMatchObject([{ encrypted_subject: null }]);
  });
  it("persists throttled retries across eviction without duplicating the row or resetting its backoff", async () => {
    const f = fixture(); await f.enqueue();
    const network = graph([() => new Response(null, { status: 429, headers: { "Retry-After": "120" } }), () => duplicate()]);
    await f.due(); const before = Date.now(); await f.alarm();
    const rows = await f.rows();
    expect(rows).toMatchObject([{ state: "pending", attempts: 1 }]);
    expect(rows[0].next_attempt).toBeGreaterThanOrEqual(before + 120_000);
    expect(JSON.stringify(await f.subjects())).not.toContain(USER);
    await evictDurableObject(f.stub); await f.enqueue();
    expect(await f.rows()).toEqual(rows);
    await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ state: "complete", attempts: 2 }]);
    expect(network.membershipRequests()).toHaveLength(2);
  });
  it("retries a newly registered user missing from a Graph replica across eviction", async () => {
    const f = fixture(); await f.enqueue();
    const network = graph([
      () => Response.json({ error: { code: "Request_ResourceNotFound", message: "Resource not found" } }, { status: 404 }),
      () => new Response(null, { status: 204 })
    ]);
    await f.due(); const before = Date.now(); await f.alarm();
    const pending = await f.rows();
    expect(pending).toMatchObject([{ group_id: GROUP_A, state: "pending", attempts: 1 }]);
    expect(pending[0].next_attempt).toBeGreaterThanOrEqual(before + 15_000);
    expect((await f.subjects())[0].encrypted_subject).not.toBeNull();
    await evictDurableObject(f.stub);
    expect(await f.rows()).toEqual(pending);
    await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ group_id: GROUP_A, state: "complete", attempts: 2 }]);
    expect(await f.subjects()).toMatchObject([{ encrypted_subject: null }]);
    expect(network.membershipRequests().map(request => request.url)).toEqual([groupUrl(GROUP_A), groupUrl(GROUP_A)]);
  });
  it("bounds retry attempts and expires all persisted subject/tombstone state", async () => {
    const f = fixture(); await f.enqueue(); const network = graph(Array.from({ length: 8 }, () => () => new Response(null, { status: 503 })));
    for (let attempt = 0; attempt < 8; attempt++) { await f.due(); await f.alarm(); }
    expect(await f.rows()).toMatchObject([{ state: "retry_exhausted", attempts: 8 }]);
    expect(network.membershipRequests()).toHaveLength(8);
    await runInDurableObject(f.stub, async (_instance, state) => state.storage.sql.exec("UPDATE directory_memberships SET expires=0").toArray());
    await f.alarm(); expect(await f.rows()).toEqual([]); expect(await f.subjects()).toEqual([]);
    // Expiry must leave the same active object's schema usable for a new visit.
    await f.enqueue(); expect(await f.rows()).toMatchObject([{ state: "pending", attempts: 0 }]);
  });
  it("recovers an exhausted outage only after a fresh authenticated enrollment and cooldown", async () => {
    const f = fixture(); await f.enqueue();
    const network = graph([...Array.from({ length: 8 }, () => () => new Response(null, { status: 503 })),
      () => new Response(null, { status: 204 })]);
    for (let attempt = 0; attempt < 8; attempt++) { await f.due(); await f.alarm(); }
    const exhausted = await f.rows();
    expect(exhausted).toMatchObject([{ state: "retry_exhausted", attempts: 8 }]);
    await f.enqueue(); await f.alarm();
    expect(await f.rows()).toEqual(exhausted);
    expect(network.membershipRequests()).toHaveLength(8);
    await runInDurableObject(f.stub, async (_instance, state) => state.storage.sql.exec("UPDATE directory_memberships SET next_attempt=0").toArray());
    await f.alarm(); expect(network.membershipRequests()).toHaveLength(8);
    await f.enqueue();
    expect(await f.rows()).toMatchObject([{ state: "pending", attempts: 0 }]);
    await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ state: "complete", attempts: 1 }]);
    expect(network.membershipRequests()).toHaveLength(9);
  });
  it("keeps nonretryable permission failures dormant until a new enrollment after cooldown", async () => {
    const f = fixture(); await f.enqueue();
    const network = graph([() => new Response(null, { status: 403 }), () => new Response(null, { status: 204 })]);
    await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ state: "failed", attempts: 1 }]);
    await f.enqueue(); await f.alarm(); expect(network.membershipRequests()).toHaveLength(1);
    await runInDurableObject(f.stub, async (_instance, state) => state.storage.sql.exec("UPDATE directory_memberships SET next_attempt=0").toArray());
    await f.alarm(); expect(network.membershipRequests()).toHaveLength(1);
    await f.enqueue(); await f.due(); await f.alarm();
    expect(await f.rows()).toMatchObject([{ state: "complete", attempts: 1 }]);
  });
  it("scopes each add to a configured product group and refuses switching an object's user", async () => {
    const f = fixture(); await f.enqueue(ASTRA); await f.enqueue(ANALYTICS);
    await expect(f.enqueue(ASTRA, OTHER_USER)).rejects.toThrow();
    await expect(f.enqueue(WRITER)).rejects.toThrow();
    const network = graph([() => new Response(null, { status: 204 }), () => new Response(null, { status: 204 })]);
    await f.due(); await f.alarm();
    expect(network.membershipRequests().map(request => request.url)).toEqual([groupUrl(GROUP_A), groupUrl(GROUP_B)]);
    expect(network.requests.every(request => !request.url.includes("/users/") && request.method === "POST")).toBe(true);
  });
  it("never processes a previously queued group that is outside the current allowlist", async () => {
    const f = fixture(); await f.enqueue();
    await runInDurableObject(f.stub, async (_instance, state) => state.storage.sql.exec("UPDATE directory_memberships SET group_id=?,next_attempt=0", UNKNOWN_GROUP).toArray());
    const network = graph([]); await f.alarm();
    expect(await f.rows()).toMatchObject([{ group_id: UNKNOWN_GROUP, state: "failed" }]);
    expect(network.membershipRequests()).toEqual([]);
  });
});
