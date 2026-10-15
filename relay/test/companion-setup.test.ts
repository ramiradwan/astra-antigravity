import { describe, expect, it } from "vitest";
import { companionSetupSettings, SETUP_RELEASE_UNAVAILABLE, setupCommandForRequest, validSetupCommand } from "../src/companion-setup";

const URL = "https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.1/delegate-to-antigravity-0.4.1.tgz";
const command = "npx --yes " + URL + " setup";
const verified = { COMPANION_BOOTSTRAP_URL: URL, COMPANION_BOOTSTRAP_SHA256: "a".repeat(64) };

describe("public companion setup gate", () => {
  it("keeps setup unavailable until both verified release fields are configured", () => {
    for (const config of [{}, { COMPANION_BOOTSTRAP_URL: URL }, { COMPANION_BOOTSTRAP_SHA256: verified.COMPANION_BOOTSTRAP_SHA256 }])
      expect(companionSetupSettings(config)).toEqual({ install_unavailable_message: SETUP_RELEASE_UNAVAILABLE });
  });
  it("offers a recorded verified and version-pinned release without an extra policy flag", () => {
    expect(companionSetupSettings(verified)).toEqual({ setup_command: command });
    for (const sha256 of [undefined, "", "a".repeat(63), "A".repeat(64), "a".repeat(64) + "\n"])
      expect(companionSetupSettings({ ...verified, COMPANION_BOOTSTRAP_SHA256: sha256 })).toEqual({ install_unavailable_message: SETUP_RELEASE_UNAVAILABLE });
  });
  it("rejects floating releases, host substitution, shell syntax and noncanonical URLs", () => {
    for (const url of [URL.replace("github.com", "example.com"), URL.replace("companion-v0.4.1", "latest"), URL.replace("companion-v0.4.1", "companion-v0.4.2"),
      URL.replace("https:", "http:"), URL.replace("github.com", "user@github.com"), URL + "?download=1", URL + "#hash", URL + "\n", URL + "; calc", URL.replace("0.4.1.tgz", "00.4.1.tgz")]) {
      expect(companionSetupSettings({ ...verified, COMPANION_BOOTSTRAP_URL: url })).toEqual({ install_unavailable_message: SETUP_RELEASE_UNAVAILABLE });
      expect(validSetupCommand("npx --yes " + url + " setup")).toBe(false);
    }
    expect(validSetupCommand("npx --yes delegate-to-antigravity@latest setup")).toBe(false);
    expect(validSetupCommand(command + " --request-id preset")).toBe(false);
  });
  it("preserves one inert saved request without accepting additional arguments", () => {
    expect(setupCommandForRequest(command)).toBe(command);
    expect(setupCommandForRequest(command, "saved-task.01")).toBe(command + " --request-id saved-task.01");
    for (const requestId of ["", "a".repeat(129), "x & calc", "valid\n", "--relay=https://example.com"])
      expect(setupCommandForRequest(command, requestId)).toBeUndefined();
    expect(setupCommandForRequest(command + "; calc", "saved-task")).toBeUndefined();
  });
});
