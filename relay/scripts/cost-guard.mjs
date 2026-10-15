import {basename,dirname,isAbsolute,resolve} from "node:path";
import {tmpdir} from "node:os";
import {validateDirectorySettings} from "./directory-config.mjs";
import {validatedSetupRelease} from "../src/companion-setup.ts";

/** Fail closed before any deployment or resource creation. No billing mutations. */
const ACCOUNT = /^[a-f0-9]{32}$/;
const TENANT = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;

function validCustomDomain(value) {
  if (typeof value !== "string" || value.length > 253 || /[^a-z0-9.-]/.test(value)) return false;
  const labels=value.split("."),suffix=labels.at(-1);
  return labels.length >= 2 && labels.every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))
    && /^[a-z]{2,63}$/.test(suffix) && !["localhost","local","internal","invalid"].includes(suffix);
}

/** Nonsecret release pins only; verification evidence is reviewed before configuration. */
export function validateCompanionSetupSettings(settings) {
  if (settings === undefined) return undefined;
  if (!settings || typeof settings !== "object" || Array.isArray(settings)
    || Object.keys(settings).sort().join(",") !== "bootstrap_sha256,bootstrap_url"
    || !validatedSetupRelease(settings.bootstrap_url, settings.bootstrap_sha256))
    throw new Error("Companion setup requires only a pinned public release tarball URL and its verified lowercase SHA-256");
  return settings;
}

/** Public Entra configuration only; client credentials belong in Worker secrets. */
export function validateEntraSettings(settings) {
  if (settings === undefined) return undefined;
  if (!settings || typeof settings !== "object" || Array.isArray(settings)
    || Object.keys(settings).some(key => !["authority", "custom_domain", "required_role", "tenant_id"].includes(key))
    || typeof settings.tenant_id !== "string" || settings.tenant_id.length !== 36 || !TENANT.test(settings.tenant_id)
    || (Object.hasOwn(settings,"custom_domain") && !validCustomDomain(settings.custom_domain))
    || (Object.hasOwn(settings,"required_role") && (typeof settings.required_role !== "string"
      || settings.required_role.trim() !== settings.required_role || !/^[A-Za-z][A-Za-z0-9._-]{0,63}$/.test(settings.required_role))))
    throw new Error("Entra settings require the tenant, exact authority, an optional public custom domain and optional valid app role; credentials must stay in Worker secrets");
  if (typeof settings.authority !== "string" || settings.authority.trim() !== settings.authority
    || (!new RegExp("^https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.ciamlogin\\.com/" + settings.tenant_id + "$").test(settings.authority)
      && !(settings.custom_domain && settings.authority === `https://${settings.custom_domain}/${settings.tenant_id}`)))
    throw new Error("Entra authority must be the exact tenant-specific CIAM or explicitly pinned custom-domain HTTPS authority");
  return settings;
}
// These separately billed products do not select the Workers plan. The audited
// relay has no R2/cache/Zero Trust binding, route or client for them. All other
// account subscriptions still require explicit Workers Free identification.
const UNRELATED_PLANS = new Map([
  ["r2_paid","R2 Paid"], ["cache_reserve_paid","Cache Reserve"], ["teams_free","Teams Free Base"]
]);

export function validateFreeConfig(config) {
  const keys = new Set(["$schema", "name", "main", "compatibility_date", "compatibility_flags", "workers_dev", "preview_urls", "vars",
    "kv_namespaces", "durable_objects", "migrations", "observability", "account_id", "rules", "assets"]);
  if (Object.keys(config).some(key => !keys.has(key))) throw new Error("Unknown or billable deployment option refused");
  if (!ACCOUNT.test(config.account_id ?? "")) throw new Error("An explicit Cloudflare account ID is required");
  if (JSON.stringify(config.rules) !== JSON.stringify([{type:"Text",globs:["**/*.html","**/astra.css","**/PRIVACY.md","**/TERMS.md","**/LICENSE"],fallthrough:true}]))
    throw new Error("Only bundled HTML, Astra styling and approved legal text modules are allowed");
  if (config.name !== "astra-antigravity-relay" || config.main !== "src/index.ts" || config.workers_dev !== true || config.preview_urls !== false)
    throw new Error("Unexpected relay deployment target");
  if (!config.compatibility_flags?.includes("global_fetch_strictly_public") || config.compatibility_flags.some(flag => !["nodejs_compat", "global_fetch_strictly_public"].includes(flag)))
    throw new Error("Public-only outbound networking must be enabled");
  const origin = new URL(config.vars?.PUBLIC_ORIGIN);
  if (origin.protocol !== "https:" || origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash || !origin.hostname.endsWith(".workers.dev"))
    throw new Error("Use the existing account's free workers.dev HTTPS origin");
  const vars = config.vars;
  if (vars.DEPLOYMENT_MODE !== "verified-free" || vars.PUBLISHER_COST_POLICY !== "free-only"
    || !Number.isSafeInteger(Number(vars.RETENTION_HOURS)) || Number(vars.RETENTION_HOURS) < 1 || Number(vars.RETENTION_HOURS) > 72
    || !Number.isSafeInteger(Number(vars.MAX_TASKS_PER_OWNER)) || Number(vars.MAX_TASKS_PER_OWNER) < 1 || Number(vars.MAX_TASKS_PER_OWNER) > 64)
    throw new Error("Free-only retention and task limits must be retained");
  const allowedVars = new Set(["PUBLIC_ORIGIN", "OPENAI_APPS_CHALLENGE", "DEPLOYMENT_MODE", "PUBLISHER_COST_POLICY", "CALLBACK_HOSTS", "MAX_TASKS_PER_OWNER", "RETENTION_HOURS", "ENTRA_TENANT_ID", "ENTRA_AUTHORITY", "ENTRA_CUSTOM_DOMAIN", "ENTRA_REQUIRED_ROLE", "DIRECTORY_HOOK_CONFIG", "COMPANION_BOOTSTRAP_URL", "COMPANION_BOOTSTRAP_SHA256"]);
  if (Object.keys(vars).some(key => !allowedVars.has(key))) throw new Error("Unexpected variable; keep secrets in Wrangler's secret store");
  const bootstrapUrl=vars.COMPANION_BOOTSTRAP_URL,bootstrapSha256=vars.COMPANION_BOOTSTRAP_SHA256;
  const setupDisabled=(bootstrapUrl===undefined || bootstrapUrl==="") && (bootstrapSha256===undefined || bootstrapSha256==="");
  if (!setupDisabled && !validatedSetupRelease(bootstrapUrl,bootstrapSha256))
    throw new Error("Companion setup variables must be disabled or contain both verified release pins");
  if (["ENTRA_TENANT_ID", "ENTRA_AUTHORITY", "ENTRA_CUSTOM_DOMAIN", "ENTRA_REQUIRED_ROLE"].some(key => Object.hasOwn(vars, key)))
    validateEntraSettings({tenant_id:vars.ENTRA_TENANT_ID,authority:vars.ENTRA_AUTHORITY,
      ...(Object.hasOwn(vars,"ENTRA_CUSTOM_DOMAIN") ? {custom_domain:vars.ENTRA_CUSTOM_DOMAIN} : {}),
      ...(Object.hasOwn(vars,"ENTRA_REQUIRED_ROLE") ? {required_role:vars.ENTRA_REQUIRED_ROLE} : {})});
  if (Object.hasOwn(vars,"DIRECTORY_HOOK_CONFIG")) {
    let directory;
    try { directory = JSON.parse(vars.DIRECTORY_HOOK_CONFIG); } catch { throw new Error("Directory hook configuration must be valid JSON"); }
    validateDirectorySettings(directory, vars.ENTRA_TENANT_ID);
  }
  if (vars.OPENAI_APPS_CHALLENGE !== undefined && (typeof vars.OPENAI_APPS_CHALLENGE !== "string"
    || (vars.OPENAI_APPS_CHALLENGE !== "" && !/^[A-Za-z0-9_-]{43}$/.test(vars.OPENAI_APPS_CHALLENGE))))
    throw new Error("OpenAI domain challenge must be empty or one 43-character base64url token");
  if (config.observability?.enabled !== false || Object.keys(config.observability).some(key => key !== "enabled")) throw new Error("Payload logging must stay disabled");
  const namespaces = config.kv_namespaces;
  if (!Array.isArray(namespaces) || namespaces.length !== 1 || namespaces[0].binding !== "OAUTH_KV" || !ACCOUNT.test(namespaces[0].id ?? "")
    || Object.keys(namespaces[0]).some(key => !["binding", "id"].includes(key))) throw new Error("Only the explicit OAuth KV binding is allowed");
  const objects = config.durable_objects;
  if (!objects || Object.keys(objects).some(key => key !== "bindings") || JSON.stringify(objects.bindings) !== JSON.stringify([
    {name:"OWNERS",class_name:"OwnerRelay"},{name:"PAIRINGS",class_name:"WorkerPairing"},
    {name:"DIRECTORY_MEMBERSHIP",class_name:"DirectoryMembership"}])) throw new Error("Only the owned SQLite mailboxes and directory membership outbox are allowed");
  if (JSON.stringify(config.migrations) !== JSON.stringify([{tag:"v1",new_sqlite_classes:["OwnerRelay","WorkerPairing"]},
    {tag:"v2-directory-membership",new_sqlite_classes:["DirectoryMembership"]}])) throw new Error("Only SQLite Durable Objects are free-plan compatible");
  if (config.assets !== undefined) {
    const assets=config.assets;
    if (!assets || typeof assets!=="object" || Array.isArray(assets)
      || Object.keys(assets).sort().join(",")!=="directory,html_handling,not_found_handling,run_worker_first"
      || typeof assets.directory!=="string" || !isAbsolute(assets.directory)
      || basename(assets.directory)!=="public-walkthrough"
      || !basename(dirname(assets.directory)).startsWith("astra-free-deploy-")
      || resolve(dirname(dirname(assets.directory)))!==resolve(tmpdir())
      || assets.run_worker_first!==false || assets.not_found_handling!=="none" || assets.html_handling!=="none")
      throw new Error("Only reviewed, temporary, asset-first walkthrough files without SPA fallback are allowed");
  }
  return origin.origin;
}

export function proveFreeSubscriptions(snapshot) {
  if (!snapshot || snapshot.success !== true || !Array.isArray(snapshot.result) || (snapshot.errors?.length ?? 0) !== 0)
    throw new Error("Cloudflare billing state could not be verified");
  const count = snapshot.result_info?.total_count;
  if (count !== undefined && count !== snapshot.result.length) throw new Error("Incomplete billing response refused");
  if (snapshot.result_info?.next_page || Number(snapshot.result_info?.total_pages) > 1
    || (snapshot.result_info?.count !== undefined && snapshot.result_info.count !== snapshot.result.length))
    throw new Error("Incomplete billing response refused");
  let free = 0;
  const unrelated = [];
  for (const subscription of snapshot.result) {
    const plan = subscription?.rate_plan;
    if (!plan || typeof plan.id !== "string" || typeof plan.public_name !== "string" || typeof plan.scope !== "string" || typeof subscription.state !== "string")
      throw new Error("Unknown subscription state refused");
    // Zone subscriptions do not change Workers billing. Only the explicitly
    // identified, already-existing independent products may be disregarded.
    if (plan.scope === "zone") continue;
    if (subscription.state === "Expired" && Date.parse(subscription.current_period_end) <= Date.now()) continue;
    if (plan.scope === "account" && UNRELATED_PLANS.get(plan.id) === plan.public_name
      && Number.isFinite(Number(subscription.price)) && Number(subscription.price) >= 0
      && plan.externally_managed !== true && plan.is_contract !== true
      && (plan.sets == null || (Array.isArray(plan.sets) && plan.sets.length === 0))
      && ["Provisioned","Paid"].includes(subscription.state)) {
      unrelated.push(plan.id);
      continue;
    }
    if (plan.scope !== "account" || !["free", "workers_free"].includes(plan.id.toLowerCase())
      || !/^workers(?: free| free plan)?$/i.test(plan.public_name.trim()) || Number(subscription.price) !== 0
      || plan.externally_managed === true || plan.is_contract === true || !["Provisioned", "Paid"].includes(subscription.state))
      throw new Error("A paid Workers, trial, managed or unrecognized account subscription was found; deployment refused");
    free++;
  }
  // Free Workers may be implicit: a successful, complete account subscription
  // response with no active Workers subscription has no metered Workers plan.
  return {plan:"free",evidence:free ? "explicit Workers Free subscription" : "no active Workers subscription",
    unrelated_existing_products:unrelated};
}

export async function checkFreeAccount(accountId, token, fetcher = fetch) {
  if (!ACCOUNT.test(accountId ?? "") || !token) throw new Error("Cloudflare sign-in and account ID are required");
  let response;
  try {
    response = await fetcher(`https://api.cloudflare.com/client/v4/accounts/${accountId}/subscriptions`, {
      method:"GET",headers:{Authorization:"Bearer "+token},redirect:"error",signal:AbortSignal.timeout(15_000)});
  } catch { throw new Error("Billing verification was unavailable; no deployment was attempted"); }
  if (!response.ok) throw new Error("Billing verification requires account billing read access; no deployment was attempted");
  const text = await response.text();
  if (Buffer.byteLength(text) > 262_144) throw new Error("Billing verification response exceeded its limit");
  let snapshot;
  try { snapshot=JSON.parse(text); } catch { throw new Error("Billing verification returned invalid data"); }
  return proveFreeSubscriptions(snapshot);
}
