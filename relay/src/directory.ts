import { DurableObject } from "cloudflare:workers";
import { createLocalJWKSet, jwtVerify, type JSONWebKeySet } from "jose";
import { z } from "zod";
import { boundedJson, digest, object, RelayError, type ObjectValue } from "./contracts";
import { seal, unseal } from "./security";
import { canonicalEntraAuthority, entraAuthority } from "./entra-authority";

const GUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const guid = z.string().regex(GUID).transform(value => value.toLowerCase());
// Microsoft's custom-authentication-extension service, not an operator identity.
const EXTENSION_CALLER = "99045fe1-7639-4a75-9d4a-577b6ca3810f";
const listener = z.object({ listenerId: guid, extensionId: guid }).strict();
const configuration = z.object({ tenantId: guid, audience: guid,
  originAttribute: z.string().regex(/^extension_[a-f0-9]{32}_registrationSource$/),
  attributeCollection: listener, tokenIssuance: listener,
  applications: z.record(guid, z.object({ registrationSource: z.string().regex(/^[a-z][a-z0-9-]{0,63}$/),
    groupIds: z.array(guid).length(1) }).strict()) }).strict().superRefine((value, context) => {
  const applications = Object.values(value.applications);
  if (!applications.length || applications.length > 8
    || new Set(applications.map(app => app.registrationSource)).size !== applications.length
    || new Set(applications.flatMap(app => app.groupIds)).size !== applications.length
    || new Set([value.attributeCollection.listenerId, value.attributeCollection.extensionId,
      value.tokenIssuance.listenerId, value.tokenIssuance.extensionId]).size !== 4)
    context.addIssue({ code: "custom", message: "Use one unique source and product group per application" });
});
export type DirectoryHookConfig = z.infer<typeof configuration>;
type Config = DirectoryHookConfig & { authority: string };
const ROUTES = { "/directory/attribute-collection-submit": "attributeCollection",
  "/directory/token-issuance-start": "tokenIssuance" } as const;
type Stage = typeof ROUTES[keyof typeof ROUTES];
const EVENT = { attributeCollection: { type: "attributeCollectionSubmit", callout: "onAttributeCollectionSubmit" },
  tokenIssuance: { type: "tokenIssuanceStart", callout: "onTokenIssuanceStart" } } as const;
const RETENTION_MS = 72 * 3_600_000;
const MAX_ATTEMPTS = 8;
const ENROLLMENT_RETRY_COOLDOWN_MS = 3_600_000;

function unavailable(): RelayError { return new RelayError("directory_config", "Directory hooks are not configured", 503); }
export function directoryConfig(env: Env): Config | null {
  if (!env.DIRECTORY_HOOK_CONFIG?.trim()) return null;
  try {
    if (new TextEncoder().encode(env.DIRECTORY_HOOK_CONFIG).length > 8192) throw new Error("oversize config");
    const config = configuration.parse(JSON.parse(env.DIRECTORY_HOOK_CONFIG));
    if (env.ENTRA_TENANT_ID?.toLowerCase() !== config.tenantId) throw new Error("tenant");
    entraAuthority(config.tenantId, env.ENTRA_AUTHORITY ?? "", env.ENTRA_CUSTOM_DOMAIN);
    // Event caller verification does not depend on the browser's custom-domain
    // proxy, its availability, or metadata selected by that frontend.
    return { ...config, authority: canonicalEntraAuthority(config.tenantId) };
  } catch { throw unavailable(); }
}
function configured(env: Env): Config { const config = directoryConfig(env); if (!config) throw unavailable(); return config; }
function graphWriter(env: Env, config: Config): { id: string; secret: string } {
  const id = env.DIRECTORY_GRAPH_CLIENT_ID?.toLowerCase(), secret = env.DIRECTORY_GRAPH_CLIENT_SECRET;
  if (!id || !GUID.test(id) || !secret?.trim() || secret.length > 4096 || id === config.audience
    || id === env.ENTRA_CLIENT_ID?.toLowerCase() || Object.hasOwn(config.applications, id)
    || env.SESSION_SECRET.length < 32) throw unavailable();
  return { id, secret };
}
async function providerJson(url: string, init?: RequestInit): Promise<ObjectValue> {
  const response = await fetch(url, { ...init, redirect: "manual", signal: AbortSignal.timeout(1500) });
  if (!response.ok) throw new Error("directory provider unavailable");
  return object(await boundedJson(new Request("https://bounded.invalid", { method: "POST", body: response.body })));
}
// Only public, resolved provider keys are cached. No request, token, user data,
// credential, or in-flight promise is shared between request contexts.
let publicKeys: { authority: string; expires: number; issuer: string; jwks: JSONWebKeySet } | undefined;
async function verificationKeys(config: Config, refresh = false) {
  if (!refresh && publicKeys?.authority === config.authority && publicKeys.expires > Date.now()) return publicKeys;
  const jwksUrl = config.authority + "/discovery/v2.0/keys";
  const [metadata, keys] = await Promise.all([
    providerJson(config.authority + "/v2.0/.well-known/openid-configuration"), providerJson(jwksUrl) ]);
  if (metadata.issuer !== config.authority + "/v2.0"
    || metadata.jwks_uri !== jwksUrl || !Array.isArray(keys.keys) || !keys.keys.length || keys.keys.length > 16)
    throw new Error("untrusted directory metadata");
  const jwks: JSONWebKeySet = { keys: keys.keys.map(key => object(key)) };
  const resolved = { authority: config.authority, expires: Date.now() + 300_000, issuer: String(metadata.issuer), jwks };
  publicKeys = resolved;
  return resolved;
}
async function authenticate(request: Request, config: Config): Promise<void> {
  const bearer = /^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$/.exec(request.headers.get("Authorization") ?? "");
  if (!bearer || bearer[1].length > 16_384) throw new RelayError("directory_auth", "Event authentication required", 401);
  let keys: Awaited<ReturnType<typeof verificationKeys>>;
  try { keys = await verificationKeys(config); }
  catch { throw new RelayError("directory_unavailable", "Event authentication is unavailable", 503); }
  try {
    const verify = (candidate: typeof keys) => jwtVerify(bearer[1], createLocalJWKSet(candidate.jwks), { algorithms: ["RS256"],
      issuer: candidate.issuer, audience: config.audience, requiredClaims: ["exp", "iat", "nbf", "tid", "azp", "ver"], clockTolerance: 30 });
    let verified;
    try { verified = await verify(keys); }
    catch (error) {
      // Retry a key rotation only for an unknown key, not bad signatures/claims.
      if (!(error instanceof Error) || !("code" in error) || error.code !== "ERR_JWKS_NO_MATCHING_KEY") throw error;
      keys = await verificationKeys(config, true);
      verified = await verify(keys);
    }
    const claims = verified.payload;
    if (claims.ver !== "2.0" || claims.tid !== config.tenantId || claims.aud !== config.audience
      || claims.azp !== EXTENSION_CALLER || claims.scp !== undefined
      || (claims.appid !== undefined && claims.appid !== EXTENSION_CALLER)
      || typeof claims.iat !== "number" || claims.iat > Date.now() / 1000 + 30
      || typeof claims.exp !== "number" || claims.exp <= claims.iat) throw new Error("unauthorized caller");
  } catch { throw new RelayError("directory_auth", "Event authentication could not be verified", 401); }
}
function eventData(body: unknown, config: Config, stage: Stage): { data: ObjectValue; context: ObjectValue; clientId: string } {
  const event = object(body), data = object(event.data), context = object(data.authenticationContext);
  const source = /^\/tenants\/([a-f0-9-]{36})\/applications\/([a-f0-9-]{36})$/i.exec(String(event.source));
  const clientId = guid.parse(object(context.clientServicePrincipal).appId);
  const expected = config[stage];
  if (event.type !== "microsoft.graph.authenticationEvent." + EVENT[stage].type
    || data["@odata.type"] !== "microsoft.graph." + EVENT[stage].callout + "CalloutData"
    || data.tenantId !== config.tenantId || data.authenticationEventListenerId !== expected.listenerId
    || data.customAuthenticationExtensionId !== expected.extensionId || !source || source[1].toLowerCase() !== config.tenantId
    // These first-party web apps request their own ID tokens. Distinct API
    // resource flows require an explicit resource allowlist before support.
    || source[2] !== clientId || !Object.hasOwn(config.applications, clientId))
    throw new RelayError("directory_event", "Event context is not allowed", 403);
  return { data, context, clientId };
}

/** Authenticated Entra events are independent of browser login and MCP grants. */
export async function directoryHook(request: Request, env: Env): Promise<Response | null> {
  const path = new URL(request.url).pathname;
  if (!Object.hasOwn(ROUTES, path)) return null;
  const config = directoryConfig(env);
  if (!config) return new Response("Not found", { status: 404 });
  if (request.method !== "POST") return new Response(null, { status: 405, headers: { Allow: "POST" } });
  await authenticate(request, config);
  if (request.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "application/json")
    throw new RelayError("directory_content_type", "JSON event required", 415);
  const stage = ROUTES[path as keyof typeof ROUTES];
  const { data, context, clientId } = eventData(await boundedJson(request), config, stage);
  const product = config.applications[clientId];
  let action: ObjectValue;
  if (stage === "attributeCollection") {
    const attributes = object(object(data.userSignUpInfo).attributes);
    const attribute = object(attributes[config.originAttribute]);
    if (attribute["@odata.type"] !== "microsoft.graph.stringDirectoryAttributeValue" || attribute.attributeType !== "directorySchemaExtension")
      throw new RelayError("directory_origin_attribute", "Registration source must be selected for collection", 400);
    // Ignore the submitted value: only the authenticated event's mapped app can
    // select the writeToDirectory attribute. No existing-user PATCH exists.
    action = { "@odata.type": "microsoft.graph.attributeCollectionSubmit.modifyAttributeValues",
      attributes: { [config.originAttribute]: product.registrationSource } };
  } else {
    graphWriter(env, config);
    const userId = guid.parse(object(context.user).id);
    const key = await digest("directory:" + config.tenantId + ":" + userId);
    await env.DIRECTORY_MEMBERSHIP.getByName(key).enqueue(config.tenantId, userId, clientId);
    action = { "@odata.type": "microsoft.graph.tokenIssuanceStart.provideClaimsForToken", claims: {} };
  }
  return Response.json({ data: { "@odata.type": "microsoft.graph." + EVENT[stage].callout + "ResponseData", actions: [action] } },
    { headers: { "Cache-Control": "no-store" } });
}

type MembershipRow = { group_id: string; client_id: string; state: string; attempts: number; next_attempt: number; expires: number };
type SubjectRow = { subject_hash: string; encrypted_subject: string | null };
type Outcome = { kind: "complete" | "retry" | "failed"; retryAfter?: number };
function retryAfter(response: Response): number | undefined {
  const value = response.headers.get("Retry-After");
  if (!value) return undefined;
  const delay = /^\d+$/.test(value) ? Number(value) * 1000 : Date.parse(value) - Date.now();
  return Number.isFinite(delay) && delay > 0 ? Math.min(delay, 3_600_000) : undefined;
}
async function graphOutcome(response: Response): Promise<Outcome> {
  if (response.status === 204) return { kind: "complete" };
  // A user returned by signup can still be absent from another Graph replica.
  // The fixed tenant/user/group scope and attempt limit also bound real 404s.
  if (response.status === 404 || response.status === 429 || response.status >= 500)
    return { kind: "retry", retryAfter: retryAfter(response) };
  if (response.status === 400 || response.status === 409) {
    try {
      const error = object(object(await boundedJson(new Request("https://bounded.invalid", { method: "POST", body: response.body }))).error);
      // Graph also uses 400 for malformed requests and unsupported object types.
      // Neither a generic conflict nor a broad substring is proof of membership.
      if (error.code === "Request_BadRequest" && error.message === "One or more added object references already exist for the following modified properties: 'members'.")
        return { kind: "complete" };
      if (response.status === 400 && error.code === "Request_BadRequest"
        && error.message === "The source resource object or one of the objects being referenced don't exist.") return { kind: "retry" };
    } catch { /* Malformed or oversized errors never turn into success. */ }
  }
  return { kind: "failed" };
}

/** One bounded, add-only outbox per pseudonymous tenant/user identity. */
export class DirectoryMembership extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS directory_subject(id INTEGER PRIMARY KEY CHECK(id=1),subject_hash TEXT NOT NULL,encrypted_subject TEXT);
      CREATE TABLE IF NOT EXISTS directory_memberships(group_id TEXT PRIMARY KEY,client_id TEXT NOT NULL,state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL,expires INTEGER NOT NULL);
    `);
  }
  async enqueue(tenantId: string, userId: string, clientId: string): Promise<void> {
    const config = configured(this.env);
    graphWriter(this.env, config);
    if (tenantId !== config.tenantId || !GUID.test(userId) || !Object.hasOwn(config.applications, clientId))
      throw new RelayError("directory_scope", "Directory membership scope is not allowed", 403);
    userId = userId.toLowerCase();
    const subjectHash = await digest("directory:" + tenantId + ":" + userId);
    const encrypted = await seal({ tenantId, userId }, this.env.SESSION_SECRET + ":directory-membership");
    const now = Date.now();
    this.ctx.storage.transactionSync(() => {
      const subject = this.ctx.storage.sql.exec<SubjectRow>("SELECT subject_hash,encrypted_subject FROM directory_subject WHERE id=1").toArray()[0];
      if (subject && subject.subject_hash !== subjectHash) throw new RelayError("directory_subject", "Directory subject does not match", 403);
      this.ctx.storage.sql.exec("DELETE FROM directory_memberships WHERE expires<=?", now);
      const allowedGroups = Object.values(config.applications).flatMap(app => app.groupIds);
      this.ctx.storage.sql.exec("DELETE FROM directory_memberships WHERE group_id NOT IN (" + allowedGroups.map(() => "?").join(",") + ")", ...allowedGroups);
      this.ctx.storage.sql.exec("INSERT INTO directory_subject(id,subject_hash,encrypted_subject) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET encrypted_subject=excluded.encrypted_subject", subjectHash, encrypted);
      for (const groupId of config.applications[clientId].groupIds) {
        this.ctx.storage.sql.exec("INSERT OR IGNORE INTO directory_memberships(group_id,client_id,state,next_attempt,expires) VALUES(?,?,'pending',?,?)", groupId, clientId, now + 1000, now + RETENTION_MS);
        // Exhaustion and permanent failures are dormant until a fresh verified
        // enrollment arrives after a cooldown. Repeated token events cannot
        // reset an active attempt budget or continuously retry missing grants.
        this.ctx.storage.sql.exec("UPDATE directory_memberships SET state='pending',attempts=0,next_attempt=?,expires=? WHERE group_id=? AND state IN ('failed','retry_exhausted') AND next_attempt<=?",
          now + 1000, now + RETENTION_MS, groupId, now);
      }
      if (!this.ctx.storage.sql.exec<{ total: number }>("SELECT COUNT(*) AS total FROM directory_memberships WHERE state='pending'").one().total)
        this.ctx.storage.sql.exec("UPDATE directory_subject SET encrypted_subject=NULL WHERE id=1");
    });
    await this.schedule();
  }
  private async schedule(): Promise<void> {
    const next = this.ctx.storage.sql.exec<{ next: number | null }>("SELECT MIN(CASE WHEN state='pending' THEN MIN(next_attempt,expires) ELSE expires END) AS next FROM directory_memberships").one().next;
    if (next === null) {
      this.ctx.storage.sql.exec("DELETE FROM directory_subject");
      await this.ctx.storage.deleteAlarm();
      return;
    }
    await this.ctx.storage.setAlarm(Math.max(Date.now() + 1000, next));
  }
  async alarm(): Promise<void> {
    const now = Date.now();
    this.ctx.storage.sql.exec("DELETE FROM directory_memberships WHERE expires<=?", now);
    const rows = this.ctx.storage.sql.exec<MembershipRow>("SELECT * FROM directory_memberships WHERE state='pending' AND next_attempt<=? ORDER BY group_id LIMIT 8", now).toArray();
    if (rows.length) {
      let config: Config | undefined, accessToken: string | undefined, userId: string | undefined;
      try {
        config = configured(this.env);
        const writer = graphWriter(this.env, config);
        const subject = this.ctx.storage.sql.exec<SubjectRow>("SELECT subject_hash,encrypted_subject FROM directory_subject WHERE id=1").one();
        const plain = await unseal(subject.encrypted_subject ?? "", this.env.SESSION_SECRET + ":directory-membership");
        if (plain.tenantId !== config.tenantId || typeof plain.userId !== "string" || !GUID.test(plain.userId)
          || subject.subject_hash !== await digest("directory:" + config.tenantId + ":" + plain.userId)) throw new Error("subject scope");
        userId = plain.userId;
        const token = await providerJson("https://login.microsoftonline.com/" + config.tenantId + "/oauth2/v2.0/token", { method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ client_id: writer.id,
            client_secret: writer.secret, grant_type: "client_credentials", scope: "https://graph.microsoft.com/.default" }) });
        if (token.token_type !== "Bearer" || typeof token.access_token !== "string" || !token.access_token || token.access_token.length > 16_384)
          throw new Error("invalid writer token");
        accessToken = token.access_token;
      } catch { /* Persist a bounded retry without retaining provider responses. */ }
      for (const row of rows) {
        let outcome: Outcome = { kind: "retry" };
        if (config && !config.applications[row.client_id]?.groupIds.includes(row.group_id)) outcome = { kind: "failed" };
        else if (accessToken && userId) {
          try {
            const response = await fetch("https://graph.microsoft.com/v1.0/groups/" + row.group_id + "/members/$ref", { method: "POST",
              redirect: "manual", signal: AbortSignal.timeout(10_000), headers: { Authorization: "Bearer " + accessToken, "Content-Type": "application/json" },
              body: JSON.stringify({ "@odata.id": "https://graph.microsoft.com/v1.0/directoryObjects/" + userId }) });
            outcome = await graphOutcome(response);
          } catch { /* A timeout may have succeeded remotely; retrying the add is idempotent. */ }
        }
        const attempts = row.attempts + 1;
        const state = outcome.kind === "retry" ? attempts < MAX_ATTEMPTS ? "pending" : "retry_exhausted"
          : outcome.kind === "complete" ? "complete" : "failed";
        const delay = Math.max(Math.min(15_000 * 2 ** (attempts - 1), 3_600_000), outcome.retryAfter ?? 0,
          state === "retry_exhausted" || state === "failed" ? ENROLLMENT_RETRY_COOLDOWN_MS : 0);
        this.ctx.storage.sql.exec("UPDATE directory_memberships SET state=?,attempts=?,next_attempt=? WHERE group_id=?", state, attempts, Date.now() + delay, row.group_id);
      }
    }
    if (!this.ctx.storage.sql.exec<{ total: number }>("SELECT COUNT(*) AS total FROM directory_memberships WHERE state='pending'").one().total)
      this.ctx.storage.sql.exec("UPDATE directory_subject SET encrypted_subject=NULL WHERE id=1");
    await this.schedule();
  }
}
