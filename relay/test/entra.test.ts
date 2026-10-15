import { env } from "cloudflare:test";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { exportJWK, generateKeyPair, SignJWT, type JWTPayload } from "jose";
import { entraAuthorizationUrl, entraConfig, entraConfigured, entraIdentity } from "../src/entra";
import { authConfigured } from "../src/auth";
import { digest } from "../src/contracts";

const TENANT = "11111111-1111-4111-8111-111111111111", CLIENT = "22222222-2222-4222-8222-222222222222";
const OID = "33333333-3333-4333-8333-333333333333", NONCE = "a".repeat(64), VERIFIER = "b".repeat(64);
const BASE = `https://synthetictenant.ciamlogin.com/${TENANT}`, ISSUER = `https://${TENANT}.ciamlogin.com/${TENANT}/v2.0`;
const CUSTOM_HOST = "login.example.test", CUSTOM = `https://${CUSTOM_HOST}/${TENANT}`;
const configured = () => ({ ...env, ENTRA_TENANT_ID: TENANT, ENTRA_AUTHORITY: BASE,
  ENTRA_CLIENT_ID: CLIENT, ENTRA_CLIENT_SECRET: "synthetic-secret", ENTRA_REQUIRED_ROLE: "Astra.User" });
const customConfigured = () => ({ ...configured(), ENTRA_CUSTOM_DOMAIN: CUSTOM_HOST, ENTRA_AUTHORITY: CUSTOM });
let signing: Awaited<ReturnType<typeof generateKeyPair>>, wrong: Awaited<ReturnType<typeof generateKeyPair>>;
let jwk: Awaited<ReturnType<typeof exportJWK>>, wrongJwk: Awaited<ReturnType<typeof exportJWK>>;
beforeAll(async () => {
  signing = await generateKeyPair("RS256", { extractable: true });
  wrong = await generateKeyPair("RS256", { extractable: true });
  jwk = { ...await exportJWK(signing.publicKey), kid: "synthetic-signing-key", use: "sig", alg: "RS256" };
  wrongJwk = { ...await exportJWK(wrong.publicKey), kid: "synthetic-signing-key", use: "sig", alg: "RS256" };
});
afterEach(() => vi.restoreAllMocks());
function claims(patch: JWTPayload = {}): JWTPayload {
  const now = Math.floor(Date.now() / 1000);
  return { iss: ISSUER, aud: CLIENT, sub: "pairwise-subject", tid: TENANT, oid: OID, ver: "2.0", nonce: NONCE,
    iat: now, nbf: now - 1, exp: now + 3600, roles: ["Astra.User"], ...patch };
}
async function signed(payload: JWTPayload, badKey = false): Promise<string> {
  return new SignJWT(payload).setProtectedHeader({ alg: "RS256", kid: jwk.kid }).sign((badKey ? wrong : signing).privateKey);
}
function upstream(idToken = "", patch: Record<string, unknown> = {}, base = BASE, advertisedKeys = [jwk]) {
  const metadata = { issuer: ISSUER, authorization_endpoint: base + "/oauth2/v2.0/authorize",
    token_endpoint: base + "/oauth2/v2.0/token", jwks_uri: base + "/discovery/v2.0/keys", ...patch };
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    new Request(String(input), init);
    expect(init?.redirect).toBe("manual");
    if (String(input) === base + "/v2.0/.well-known/openid-configuration") return Response.json(metadata);
    if (String(input) === base + "/oauth2/v2.0/token") {
      const form = new URLSearchParams(String(init?.body));
      expect(form.get("code_verifier")).toBe(VERIFIER);
      expect(form.get("client_secret")).toBe("synthetic-secret");
      expect(form.get("redirect_uri")).toBe("https://relay.example.test/auth/entra/callback");
      return Response.json({ id_token: idToken, access_token: "synthetic-unused-token" });
    }
    if (String(input) === base + "/discovery/v2.0/keys") return Response.json({ keys: advertisedKeys });
    if (base === CUSTOM && String(input) === `https://${TENANT}.ciamlogin.com/${TENANT}/discovery/v2.0/keys`)
      return Response.json({ keys: [jwk] });
    throw new Error("unexpected network destination");
  });
}

describe("tenant-specific Entra validation", () => {
  it("requires the exact explicit custom-host pin while retaining default CIAM configuration", () => {
    expect(entraConfig(customConfigured())?.authority).toBe(CUSTOM);
    expect(entraConfig({ ...configured(), ENTRA_CUSTOM_DOMAIN: CUSTOM_HOST })?.authority).toBe(BASE);
    for (const changes of [{ ENTRA_CUSTOM_DOMAIN: undefined }, { ENTRA_CUSTOM_DOMAIN: "other.example.test" },
      { ENTRA_AUTHORITY: CUSTOM.replace(CUSTOM_HOST, CUSTOM_HOST + ".evil.test") },
      { ENTRA_AUTHORITY: CUSTOM.replace(TENANT, CLIENT) },
      { ENTRA_AUTHORITY: CUSTOM.replace(CUSTOM_HOST, CUSTOM_HOST + ":443") },
      { ENTRA_AUTHORITY: CUSTOM.replace("/" + TENANT, "/unused/../" + TENANT) },
      { ENTRA_AUTHORITY: CUSTOM.replace(CUSTOM_HOST, CUSTOM_HOST.toUpperCase()) }])
      expect(entraConfigured({ ...customConfigured(), ...changes })).toBe(false);
  });
  it.each(["", "https://login.example.test", "*.example.test", "login.example.test/path", "login.example.test:443",
    "user@login.example.test", "127.0.0.1", "[::1]", "localhost", "login.localhost", "login.local", "login.internal",
    "login.invalid", "LOGIN.example.test", "login.example.test.", " login.example.test", "login..example.test", "-login.example.test", "login\n.example.test"])
  ("rejects non-DNS or normalized custom pin %s", pin => {
    expect(entraConfigured({ ...customConfigured(), ENTRA_CUSTOM_DOMAIN: pin })).toBe(false);
    expect(entraConfigured({ ...configured(), ENTRA_CUSTOM_DOMAIN: pin })).toBe(false);
  });
  it("uses custom transport endpoints with the canonical CIAM issuer and unchanged owner identity", async () => {
    const network = upstream(await signed(claims()), {}, CUSTOM);
    const authorization = new URL(await entraAuthorizationUrl("saved-state", VERIFIER, NONCE, customConfigured()));
    expect(authorization.origin + authorization.pathname).toBe(CUSTOM + "/oauth2/v2.0/authorize");
    expect(authorization.searchParams.get("redirect_uri")).toBe("https://relay.example.test/auth/entra/callback");
    expect(authorization.searchParams.get("nonce")).toBe(NONCE);
    expect(await entraIdentity("synthetic-code", VERIFIER, NONCE, customConfigured()))
      .toEqual({ userId: "entra-" + await digest("entra:" + TENANT + ":" + OID), ownerKey: await digest("entra:" + TENANT + ":" + OID) });
    expect(network.mock.calls.map(([url]) => String(url))).toEqual([
      CUSTOM + "/v2.0/.well-known/openid-configuration", CUSTOM + "/v2.0/.well-known/openid-configuration",
      CUSTOM + "/oauth2/v2.0/token", `https://${TENANT}.ciamlogin.com/${TENANT}/discovery/v2.0/keys` ]);
  });
  it("rejects a custom proxy's forged canonical-issuer token even when it advertises matching attacker keys", async () => {
    const network = upstream(await signed(claims(), true), {}, CUSTOM, [wrongJwk]);
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, customConfigured())).rejects.toMatchObject({ code: "entra_sign_in", status: 401 });
    const destinations = network.mock.calls.map(([url]) => String(url));
    expect(destinations).toContain(`https://${TENANT}.ciamlogin.com/${TENANT}/discovery/v2.0/keys`);
    expect(destinations).not.toContain(CUSTOM + "/discovery/v2.0/keys");
  });
  it.each([
    { issuer: CUSTOM + "/v2.0" }, { issuer: `https://${CLIENT}.ciamlogin.com/${CLIENT}/v2.0` },
    { authorization_endpoint: "https://evil.example.test/" + TENANT + "/oauth2/v2.0/authorize" },
    { token_endpoint: "https://evil.example.test/" + TENANT + "/oauth2/v2.0/token" },
    { jwks_uri: "https://evil.example.test/" + TENANT + "/discovery/v2.0/keys" },
    { token_endpoint: CUSTOM.replace(TENANT, CLIENT) + "/oauth2/v2.0/token" },
  ])("rejects custom discovery that changes issuer, tenant or pinned endpoint host", async patch => {
    const network = upstream("", patch, CUSTOM);
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, customConfigured())).rejects.toMatchObject({ code: "entra_metadata" });
    expect(network).toHaveBeenCalledTimes(1);
  });
  it.each([{ iss: CUSTOM + "/v2.0" }, { tid: CLIENT }])("keeps token tenant and issuer checks under custom login", async patch => {
    upstream(await signed(claims(patch)), {}, CUSTOM);
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, customConfigured())).rejects.toMatchObject({ code: "entra_sign_in", status: 401 });
  });
  it("disables absent or partial configuration while retaining configured GitHub", () => {
    expect(entraConfig(env)).toBeNull();
    expect(entraConfigured({ ...env, ENTRA_TENANT_ID: TENANT })).toBe(false);
    expect(() => entraConfig({ ...env, ENTRA_TENANT_ID: TENANT })).toThrow();
    expect(authConfigured(env)).toBe(true);
    expect(authConfigured({ ...configured(), SESSION_SECRET: "short" })).toBe(false);
  });
  it.each([
    ["ENTRA_AUTHORITY", "http://synthetictenant.ciamlogin.com/" + TENANT],
    ["ENTRA_AUTHORITY", "https://synthetictenant.ciamlogin.com.evil.test/" + TENANT],
    ["ENTRA_AUTHORITY", "https://synthetictenant.ciamlogin.com/common"],
    ["ENTRA_AUTHORITY", BASE + "?redirect=evil"],
    ["ENTRA_AUTHORITY", BASE + "/v2.0"],
    ["ENTRA_TENANT_ID", "common"], ["ENTRA_CLIENT_ID", "not-a-guid"], ["ENTRA_CLIENT_SECRET", "   "], ["ENTRA_REQUIRED_ROLE", "Astra.User *"],
  ])("rejects invalid configuration %s=%s", (key, value) => {
    expect(entraConfigured({ ...configured(), [key]: value })).toBe(false);
  });
  it("uses authorization-code PKCE and nonce without Graph or offline scopes", async () => {
    const request = upstream();
    const url = new URL(await entraAuthorizationUrl("saved-state", VERIFIER, NONCE, configured()));
    expect(url.origin + url.pathname).toBe(BASE + "/oauth2/v2.0/authorize");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("response_mode")).toBe("query");
    expect(url.searchParams.get("scope")).toBe("openid profile");
    expect(url.searchParams.get("nonce")).toBe(NONCE);
    expect(url.searchParams.get("state")).toBe("saved-state");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(VERIFIER)));
    expect(url.searchParams.get("code_challenge")).toBe(btoa(String.fromCharCode(...bytes)).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/, ""));
    expect(request).toHaveBeenCalledTimes(1);
  });
  it("validates a signed ID token and derives only the isolated tenant/object owner", async () => {
    const token = await signed(claims({ email: "synthetic@example.test", name: "Synthetic" }));
    upstream(token);
    const ownerKey = await digest("entra:" + TENANT + ":" + OID);
    const identity = await entraIdentity("synthetic-code", VERIFIER, NONCE, configured());
    expect(identity).toEqual({ userId: "entra-" + ownerKey, ownerKey });
    expect(ownerKey).not.toBe(await digest("github:12345"));
    expect(JSON.stringify(identity)).not.toMatch(/synthetic@example|synthetic-unused-token|Synthetic|eyJ/);
  });
  it.each([
    ["issuer", { iss: `https://login.microsoftonline.com/${TENANT}/v2.0` }],
    ["audience", { aud: TENANT }], ["multiple audiences", { aud: [CLIENT, TENANT] }],
    ["authorized party", { azp: TENANT }], ["tenant", { tid: CLIENT }],
    ["nonce", { nonce: "c".repeat(64) }], ["object ID", { oid: "email@example.test" }],
    ["expired", { exp: Math.floor(Date.now()/1000)-60 }],
    ["future nbf", { nbf: Math.floor(Date.now()/1000)+120 }],
    ["future iat", { iat: Math.floor(Date.now()/1000)+120 }],
    ["empty subject", { sub: "" }], ["wrong version", { ver: "1.0" }],
    ["missing expiry", { exp: undefined }], ["missing object ID", { oid: undefined }],
  ] satisfies [string, JWTPayload][]) ("rejects signed token with %s", async (_name, patch) => {
    upstream(await signed(claims(patch)));
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, configured())).rejects.toMatchObject({ code: "entra_sign_in", status: 401 });
  });
  it("rejects a forged signature", async () => {
    upstream(await signed(claims(), true));
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, configured())).rejects.toMatchObject({ code: "entra_sign_in", status: 401 });
  });
  it("rejects a symmetric algorithm even with otherwise valid claims", async () => {
    const token = await new SignJWT(claims()).setProtectedHeader({ alg: "HS256", kid: jwk.kid }).sign(new Uint8Array(32).fill(1));
    upstream(token);
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, configured())).rejects.toMatchObject({ code: "entra_sign_in", status: 401 });
  });
  it("gates the configured app role, while allowing intentionally configured self-service", async () => {
    upstream(await signed(claims({ roles: [] })));
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, configured())).rejects.toMatchObject({ code: "entra_access", status: 403 });
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, { ...configured(), ENTRA_REQUIRED_ROLE: "" })).resolves.toHaveProperty("ownerKey");
  });
  it.each(["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"])("rejects untrusted discovery %s before credentials leave", async key => {
    const requests = upstream("", { [key]: "https://attacker.example.test/collect" });
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, configured())).rejects.toMatchObject({ code: "entra_metadata", status: 502 });
    expect(requests).toHaveBeenCalledTimes(1);
  });
  it("rejects redirects and bounded-response failures without leaking upstream details", async () => {
    const mock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 302, headers: { Location: "https://attacker.example.test/collect" } }));
    await expect(entraIdentity("synthetic-code", VERIFIER, NONCE, configured())).rejects.toMatchObject({ code: "entra_unavailable", status: 502 });
    mock.mockResolvedValue(Response.json({ body: "private-upstream-value".repeat(2000) }));
    try { await entraIdentity("synthetic-code", VERIFIER, NONCE, configured()); throw new Error("expected failure"); }
    catch (error) { expect(String(error)).not.toContain("private-upstream-value"); expect(error).toMatchObject({ code: "entra_unavailable", status: 502 }); }
  });
});
