import { object, RelayError, type ObjectValue } from "./contracts";
import astraStyles from "../../ui/astra.css";

const encoder = new TextEncoder();
export function base64(bytes: Uint8Array): string { return btoa(String.fromCharCode(...bytes)); }
export function unbase64(value: string): Uint8Array {
  try { return Uint8Array.from(atob(value), c => c.charCodeAt(0)); }
  catch { throw new RelayError("invalid_encoding", "Invalid base64 value"); }
}
export function equal(first: string, second: string): boolean {
  const a = encoder.encode(first), b = encoder.encode(second);
  return a.length === b.length && crypto.subtle.timingSafeEqual(a, b);
}
async function storageKey(secret: string): Promise<CryptoKey> {
  if (!secret || encoder.encode(secret).length < 32) throw new RelayError("missing_secret", "A 32-byte session secret must be configured", 503);
  const hash = await crypto.subtle.digest("SHA-256", encoder.encode(secret));
  return crypto.subtle.importKey("raw", hash, "AES-GCM", false, ["encrypt", "decrypt"]);
}
export async function seal(data: ObjectValue, secret: string): Promise<string> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const body = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await storageKey(secret), encoder.encode(JSON.stringify(data)));
  return base64(iv) + "." + base64(new Uint8Array(body));
}
export async function unseal(data: string, secret: string): Promise<ObjectValue> {
  try {
    const [iv, body, extra] = data.split(".");
    if (!iv || !body || extra) throw new Error("invalid box");
    const decoded = await crypto.subtle.decrypt({ name: "AES-GCM", iv: unbase64(iv) }, await storageKey(secret), unbase64(body));
    return object(JSON.parse(new TextDecoder().decode(decoded)));
  } catch { throw new RelayError("invalid_session", "Session expired or could not be verified", 401); }
}
function artifactCapabilityPart(part: string): string {
  return part.replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}
/** Artifact URLs use unpadded Base64URL; cookie/session seal formats stay unchanged. */
export async function sealArtifactDownload(data: ObjectValue, secret: string): Promise<string> {
  return (await seal(data, secret)).split(".").map(artifactCapabilityPart).join(".");
}
export async function unsealArtifactDownload(data: string, secret: string): Promise<ObjectValue> {
  const parts = data.split(".");
  if (parts.length === 2 && /^[A-Za-z0-9_-]{16}$/.test(parts[0]) && /^[A-Za-z0-9_-]{22,}$/.test(parts[1])) {
    try {
      const standard = parts.map(part => {
        const value = part.replaceAll("-", "+").replaceAll("_", "/");
        const padded = value + "=".repeat((4 - value.length % 4) % 4);
        if (btoa(atob(padded)) !== padded || artifactCapabilityPart(padded) !== part) throw new Error("invalid box");
        return padded;
      }).join(".");
      return unseal(standard, secret);
    } catch { throw new RelayError("invalid_session", "Session expired or could not be verified", 401); }
  }
  // Honor standard-Base64 grants issued before the artifact codec change.
  // Their AES-GCM authentication and the existing one-use grant checks apply.
  return unseal(data, secret);
}
export function html(value: string): string { return value.replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`); }
export interface PageOptions { eyebrow?: string; description?: string; wide?: boolean; accountMethod?: string }
const displayName = "Delegate to Antigravity";
const brandMark = `<svg class="brand-mark" viewBox="0 0 512 512" aria-hidden="true" focusable="false"><rect x="24" y="24" width="464" height="464" rx="112" fill="#173b58"/><ellipse cx="256" cy="256" rx="176" ry="102" transform="rotate(-35 256 256)" fill="none" stroke="#58cecb" stroke-width="20"/><path d="M256 130 L286 226 L382 256 L286 286 L256 382 L226 286 L130 256 L226 226 Z" fill="#fff"/><circle cx="400" cy="167" r="24" fill="#58cecb"/></svg>`;
export function page(title: string, body: string, headers = new Headers(), formDestinations: readonly string[] = [], options: PageOptions = {}): Response {
  const styleNonce = base64(crypto.getRandomValues(new Uint8Array(18)));
  const formOrigins = [...new Set(formDestinations.map(destination => {
    const url = new URL(destination);
    const loopback = url.hostname === "localhost" || url.hostname === "[::1]" || /^127(?:\.\d{1,3}){3}$/.test(url.hostname);
    if (url.username || url.password || (url.protocol !== "https:" && !(url.protocol === "http:" && loopback))
      || /[';\s]/.test(url.origin)) throw new RelayError("invalid_form_destination", "Invalid form navigation destination");
    return url.origin;
  }))];
  headers.set("Content-Type", "text/html; charset=utf-8");
  headers.set("Cache-Control", "no-store");
  headers.set("Content-Security-Policy", `default-src 'none'; style-src 'nonce-${styleNonce}'; form-action 'self'${formOrigins.length ? " " + formOrigins.join(" ") : ""}; frame-ancestors 'none'; base-uri 'none'`);
  headers.set("X-Frame-Options", "DENY");
  // no-referrer also changes a navigation POST's Origin to null. Keep the
  // origin on our forms for CSRF checks while hiding referrers cross-origin.
  headers.set("Referrer-Policy", "same-origin");
  const styles = astraStyles.replace(/<\/style/gi, "<\\/style");
  const pageTitle = title === displayName ? title : `${title} · ${displayName}`;
  return new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark"><title>${html(pageTitle)}</title><style nonce="${styleNonce}">${styles}</style></head><body data-surface="hosted"><div class="site-frame"><header class="site-header"><a class="brand-lockup" href="/" aria-label="${displayName} home">${brandMark}<span class="brand-name">${displayName}</span></a>${options.accountMethod ? `<span class="badge">${html(options.accountMethod)}</span>` : '<span class="badge">Computer connection</span>'}</header><main class="app-shell${options.wide ? " app-shell--wide" : ""}"><div class="page-heading">${options.eyebrow ? `<p class="eyebrow">${html(options.eyebrow)}</p>` : ""}<h1>${html(title)}</h1>${options.description ? `<p class="lead">${html(options.description)}</p>` : ""}</div><div class="page-body">${body}</div></main><footer class="site-footer"><span>${displayName}</span><nav aria-label="Legal"><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/license">MIT license</a></nav></footer></div></body></html>`, { headers });
}
export function publicOrigin(env: Env): string {
  const url = new URL(env.PUBLIC_ORIGIN);
  if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new RelayError("invalid_origin", "PUBLIC_ORIGIN must be a canonical origin", 503);
  if (url.protocol !== "https:" && !(env.DEPLOYMENT_MODE === "local" && ["localhost", "127.0.0.1"].includes(url.hostname)))
    throw new RelayError("insecure_origin", "Public relay requires HTTPS", 503);
  if (env.PUBLISHER_COST_POLICY !== "free-only" || !["local", "verified-free"].includes(env.DEPLOYMENT_MODE))
    throw new RelayError("cost_policy", "Relay deployment has not passed the free-plan check", 503);
  return url.origin;
}

export function callbackUrl(raw: string, env: Env): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new RelayError("callback_invalid", "Callback must be a valid HTTPS URL"); }
  const hosts = env.CALLBACK_HOSTS.split(",").map(h => h.trim().toLowerCase()).filter(Boolean);
  if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443") || url.hash
    || url.hostname.endsWith(".") || !hosts.includes(url.hostname.toLowerCase()) || url.href.length > 2_048)
    throw new RelayError("callback_invalid", "Callback host or scheme is not approved");
  return url;
}
export function signingKey(secret: string): Uint8Array {
  if (!secret.startsWith("whsec_")) throw new RelayError("callback_secret", "Standard Webhooks signing secret required");
  const bytes = unbase64(secret.slice(6));
  if (bytes.length < 24 || bytes.length > 64) throw new RelayError("callback_secret", "Signing secret must decode to 24–64 bytes");
  return bytes;
}
export async function signedCallback(url: string, secret: string, subscriptionId: string, id: string, body: string, env: Env, previousSecret?: string): Promise<Response> {
  const target = callbackUrl(url, env);
  const stamp = Math.floor(Date.now() / 1_000).toString();
  const signatures: string[] = [];
  for (const value of [secret, ...(previousSecret ? [previousSecret] : [])]) {
    const key = await crypto.subtle.importKey("raw", signingKey(value), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(`${id}.${stamp}.${body}`));
    signatures.push("v1," + base64(new Uint8Array(signature)));
  }
  // global_fetch_strictly_public checks addresses at connection time, including
  // redirects/rebinding. Workers supports manual redirect handling, not error
  // mode. Return 3xx untouched so the protocol's success checks reject it.
  return fetch(target.href, { method: "POST", redirect: "manual", signal: AbortSignal.timeout(10_000),
    headers: { "Content-Type": "application/json", "webhook-id": id, "webhook-timestamp": stamp,
      "webhook-signature": signatures.join(" "), "X-MCP-Subscription-Id": subscriptionId }, body });
}
