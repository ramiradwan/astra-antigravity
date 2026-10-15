const GUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const fields = (value, names) => value && typeof value === "object" && !Array.isArray(value)
  && Object.keys(value).length === names.length && Object.keys(value).every(key => names.includes(key));
const guid = value => typeof value === "string" && value.length === 36 && GUID.test(value);
const invalid = () => { throw new Error("Directory hooks require complete tenant-pinned public configuration; credentials belong in Worker secrets"); };

/** Only public identifiers and fixed product mappings; never credentials or arbitrary URLs. */
export function validateDirectorySettings(value, tenantId) {
  if (value === undefined) return undefined;
  if (!fields(value, ["tenantId", "audience", "originAttribute", "attributeCollection", "tokenIssuance", "applications"])
    || !guid(value.tenantId) || value.tenantId !== tenantId || !guid(value.audience)
    || typeof value.originAttribute !== "string" || !/^extension_[a-f0-9]{32}_registrationSource$/.test(value.originAttribute)
    || JSON.stringify(value).length > 8192) invalid();
  const ids = [];
  for (const stage of [value.attributeCollection, value.tokenIssuance]) {
    if (!fields(stage, ["listenerId", "extensionId"]) || !guid(stage.listenerId) || !guid(stage.extensionId)) invalid();
    ids.push(stage.listenerId, stage.extensionId);
  }
  if (new Set(ids).size !== ids.length) invalid();
  const apps = value.applications;
  if (!apps || typeof apps !== "object" || Array.isArray(apps) || Object.keys(apps).length < 1 || Object.keys(apps).length > 8) invalid();
  const groups = new Set(), sources = new Set();
  for (const [clientId, app] of Object.entries(apps)) {
    if (!guid(clientId) || !fields(app, ["registrationSource", "groupIds"])
      || typeof app.registrationSource !== "string" || !/^[a-z][a-z0-9-]{0,63}$/.test(app.registrationSource)
      || !Array.isArray(app.groupIds) || app.groupIds.length !== 1 || !guid(app.groupIds[0])
      || groups.has(app.groupIds[0]) || sources.has(app.registrationSource)) invalid();
    groups.add(app.groupIds[0]); sources.add(app.registrationSource);
  }
  return value;
}
