/** Public setup release configuration. No network, provider credentials or inference. */
export interface SetupReleaseConfiguration {
  COMPANION_BOOTSTRAP_URL?: string;
  COMPANION_BOOTSTRAP_SHA256?: string;
}

const RELEASE = /^https:\/\/github\.com\/ramiradwan\/astra-antigravity\/releases\/download\/companion-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\/delegate-to-antigravity-\1\.\2\.\3\.tgz$/;
const PREFIX = "npx --yes ";
const SUFFIX = " setup";
const fullMatch = (expression: RegExp, value: string) => expression.exec(value)?.[0] === value;
export const SETUP_RELEASE_UNAVAILABLE = "A verified public setup release is not available. No setup command is enabled.";

/** Shared by the Worker and the Node 24 guarded publisher command. */
export function validatedSetupRelease(url: unknown, sha256: unknown): { url: string; sha256: string } | undefined {
  if (typeof url !== "string" || !fullMatch(RELEASE, url) || typeof sha256 !== "string" || !fullMatch(/^[a-f0-9]{64}$/, sha256)) return;
  return { url, sha256 };
}

/** Never accept an arbitrary shell command, floating release, query or URL override. */
export function validSetupCommand(value: unknown): value is string {
  return typeof value === "string" && value.startsWith(PREFIX) && value.endsWith(SUFFIX) &&
    fullMatch(RELEASE, value.slice(PREFIX.length, -SUFFIX.length));
}

export function setupCommandForRequest(value: unknown, requestId?: string): string | undefined {
  if (!validSetupCommand(value)) return;
  if (requestId === undefined) return value;
  if (!fullMatch(/^[A-Za-z0-9_.-]{1,128}$/, requestId)) return;
  return value + " --request-id " + requestId;
}

export function companionSetupSettings(config: SetupReleaseConfiguration): { setup_command: string } | { install_unavailable_message: string } {
  const release = validatedSetupRelease(config.COMPANION_BOOTSTRAP_URL, config.COMPANION_BOOTSTRAP_SHA256);
  if (!release) return { install_unavailable_message: SETUP_RELEASE_UNAVAILABLE };
  // The configured digest records the operator's anonymous asset verification.
  // npx fetches this pinned HTTPS tarball; the bootstrap separately verifies the
  // installer signature/hash. npx does not verify a detached release signature.
  return { setup_command: PREFIX + release.url + SUFFIX };
}
