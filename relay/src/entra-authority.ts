const GUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const CIAM_HOST = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ciamlogin\.com$/;
const DNS_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

export function isCiamHost(hostname: string): boolean { return CIAM_HOST.test(hostname); }

/** Only an explicit operator setting can authorize an additional login host. */
export function entraCustomDomain(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  const labels = value.split(".");
  if (value.length > 253 || /[^a-z0-9.-]/.test(value) || labels.length < 2 || labels.some(label => !DNS_LABEL.test(label))
    || !/^[a-z]{2,63}$/.test(labels.at(-1) ?? "")
    || ["localhost", "local", "internal", "invalid"].includes(labels.at(-1) ?? ""))
    throw new Error("Invalid custom login domain");
  return value;
}

export function canonicalEntraAuthority(tenantId: string): string {
  if (!GUID.test(tenantId)) throw new Error("Invalid tenant");
  const tenant = tenantId.toLowerCase();
  return `https://${tenant}.ciamlogin.com/${tenant}`;
}

export function entraAuthority(tenantId: string, raw: string, customDomain: string | undefined): string {
  if (!GUID.test(tenantId)) throw new Error("Invalid tenant");
  const pin = entraCustomDomain(customDomain), url = new URL(raw), tenant = tenantId.toLowerCase();
  const rawHost = /^https:\/\/([^/?#]+)/i.exec(raw)?.[1];
  if (url.protocol !== "https:" || url.username || url.password || url.port || url.search || url.hash
    || !rawHost || rawHost.includes(":")
    || (!isCiamHost(url.hostname) && (url.hostname !== pin || rawHost !== pin
      || ![`https://${pin}/${tenant}`, `https://${pin}/${tenant}/`].includes(raw.toLowerCase())))
    || !["/" + tenant, "/" + tenant + "/"].includes(url.pathname.toLowerCase()))
    throw new Error("Invalid tenant authority");
  return url.origin + "/" + tenant;
}
