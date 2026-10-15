import { createLocalJWKSet, jwtVerify, type JSONWebKeySet, type JWTPayload } from "jose";
import { boundedJson, digest, object, RelayError, type ObjectValue } from "./contracts";
import { equal, publicOrigin } from "./security";
import { canonicalEntraAuthority, entraAuthority, isCiamHost } from "./entra-authority";

const GUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
export type EntraConfig = { tenantId: string; clientId: string; clientSecret: string; authority: string; requiredRole?: string };
type Metadata = { issuer: string; authorize: string; token: string; jwks: string };

// Configuration is tenant-specific. Neither callback input nor an unverified
// token may select an authority, tenant, key URL or application audience.
export function entraConfig(env: Env): EntraConfig | null {
  const values = [env.ENTRA_TENANT_ID, env.ENTRA_AUTHORITY, env.ENTRA_CLIENT_ID, env.ENTRA_CLIENT_SECRET];
  if (!values.some(value => value?.trim()) && !env.ENTRA_REQUIRED_ROLE?.trim() && env.ENTRA_CUSTOM_DOMAIN === undefined) return null;
  const [tenant, rawAuthority, client, secret] = values;
  let authority: string;
  try { authority = entraAuthority(tenant ?? "", rawAuthority ?? "", env.ENTRA_CUSTOM_DOMAIN); }
  catch { throw unavailable(); }
  if (!tenant || !GUID.test(tenant) || !client || !GUID.test(client) || !secret?.trim() || secret.length > 4096) throw unavailable();
  const requiredRole = env.ENTRA_REQUIRED_ROLE?.trim() || undefined;
  if (requiredRole && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(requiredRole)) throw unavailable();
  return { tenantId: tenant.toLowerCase(), clientId: client.toLowerCase(), clientSecret: secret,
    authority, requiredRole };
}
export function entraConfigured(env: Env): boolean {
  try { return entraConfig(env) !== null; } catch { return false; }
}
function unavailable(): RelayError { return new RelayError("entra_config", "Microsoft sign-in is not configured", 503); }
function configured(env: Env): EntraConfig { const config = entraConfig(env); if (!config) throw unavailable(); return config; }

async function providerJson(url: string, init?: RequestInit): Promise<ObjectValue> {
  try {
    const reply = await fetch(url, { ...init, redirect: "manual", signal: AbortSignal.timeout(10_000) });
    if (!reply.ok || reply.status >= 300) throw new Error("upstream status");
    return object(await boundedJson(new Request("https://bounded.invalid", { method: "POST", body: reply.body, duplex: "half" } as RequestInit)));
  } catch { throw new RelayError("entra_unavailable", "Microsoft sign-in could not be completed. Restart sign-in from your application.", 502); }
}
function endpoint(raw: unknown, config: EntraConfig, path: string): string {
  if (typeof raw !== "string") throw new Error("missing endpoint");
  const url = new URL(raw), authority = new URL(config.authority);
  if (url.protocol !== "https:" || url.username || url.password || url.port || url.search || url.hash
    || ![authority.hostname, config.tenantId + ".ciamlogin.com"].includes(url.hostname)
    || url.pathname !== "/" + config.tenantId + path) throw new Error("unexpected endpoint");
  return url.href;
}
async function metadata(config: EntraConfig): Promise<Metadata> {
  const data = await providerJson(config.authority + "/v2.0/.well-known/openid-configuration");
  try {
    const issuer = endpoint(data.issuer, config, "/v2.0");
    const custom = !isCiamHost(new URL(config.authority).hostname);
    // Custom URL domains brand the transport endpoints; Microsoft retains the
    // canonical tenant GUID CIAM issuer. Never trust a custom-host issuer.
    if (custom && issuer !== canonicalEntraAuthority(config.tenantId) + "/v2.0") throw new Error("unexpected issuer");
    const advertisedKeys = endpoint(data.jwks_uri, config, "/discovery/v2.0/keys");
    return { issuer,
      authorize: endpoint(data.authorization_endpoint, config, "/oauth2/v2.0/authorize"),
      token: endpoint(data.token_endpoint, config, "/oauth2/v2.0/token"),
      // The custom frontend transports codes/tokens but cannot choose the keys
      // trusted to sign a token claiming Microsoft's canonical tenant issuer.
      jwks: custom ? canonicalEntraAuthority(config.tenantId) + "/discovery/v2.0/keys" : advertisedKeys };
  } catch { throw new RelayError("entra_metadata", "Microsoft sign-in configuration could not be verified", 502); }
}
export async function entraAuthorizationUrl(state: string, verifier: string, nonce: string, env: Env): Promise<string> {
  const config = configured(env), endpoints = await metadata(config);
  const challenge = btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)))))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const url = new URL(endpoints.authorize);
  url.search = new URLSearchParams({ client_id: config.clientId, response_type: "code", response_mode: "query",
    redirect_uri: publicOrigin(env) + "/auth/entra/callback", scope: "openid profile", state, nonce,
    code_challenge: challenge, code_challenge_method: "S256" }).toString();
  return url.href;
}
export async function entraIdentity(code: string | null, verifier: string, nonce: string, env: Env): Promise<{userId: string; ownerKey: string}> {
  const config = configured(env);
  if (!code || code.length > 8192 || !/^[a-f0-9]{64}$/.test(verifier) || !/^[a-f0-9]{64}$/.test(nonce))
    throw new RelayError("entra_sign_in", "Microsoft sign-in could not be verified", 401);
  const endpoints = await metadata(config);
  const exchanged = await providerJson(endpoints.token, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: config.clientId, client_secret: config.clientSecret, grant_type: "authorization_code", code,
      redirect_uri: publicOrigin(env) + "/auth/entra/callback", code_verifier: verifier, scope: "openid profile" }) });
  if (typeof exchanged.id_token !== "string" || exchanged.id_token.length > 16384)
    throw new RelayError("entra_sign_in", "Microsoft sign-in could not be verified", 401);
  const keys = await providerJson(endpoints.jwks);
  let claims: JWTPayload;
  try {
    if (!Array.isArray(keys.keys) || !keys.keys.length || keys.keys.length > 16) throw new Error("invalid keys");
    const jwks: JSONWebKeySet = { keys: keys.keys.map(value => object(value)) };
    const { payload } = await jwtVerify(exchanged.id_token, createLocalJWKSet(jwks), { algorithms: ["RS256"],
      issuer: endpoints.issuer, audience: config.clientId, requiredClaims: ["exp", "iat", "nbf", "sub", "tid", "oid", "nonce"], clockTolerance: 30 });
    if (payload.tid !== config.tenantId || typeof payload.oid !== "string" || !GUID.test(payload.oid)
      || typeof payload.nonce !== "string" || !equal(payload.nonce, nonce) || payload.ver !== "2.0"
      || payload.aud !== config.clientId || (payload.azp !== undefined && payload.azp !== config.clientId)
      || typeof payload.sub !== "string" || !payload.sub || payload.sub.length > 255
      || typeof payload.iat !== "number" || !Number.isFinite(payload.iat) || payload.iat > Date.now() / 1000 + 30
      || typeof payload.exp !== "number" || payload.exp <= payload.iat) throw new Error("invalid identity");
    claims = payload;
  } catch { throw new RelayError("entra_sign_in", "Microsoft sign-in could not be verified", 401); }
  if (config.requiredRole && (!Array.isArray(claims.roles) || !claims.roles.includes(config.requiredRole)))
    throw new RelayError("entra_access", "This account has not been granted access to Astra", 403);
  // Email, names and upstream tokens are deliberately excluded. This namespace
  // cannot merge with an existing GitHub owner or a different Entra tenant.
  const ownerKey = await digest("entra:" + config.tenantId + ":" + String(claims.oid).toLowerCase());
  return { userId: "entra-" + ownerKey, ownerKey };
}
