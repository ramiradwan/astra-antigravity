#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { runSetup } from "./bootstrap.mjs";

const args = process.argv.slice(2);
if (args.includes("--help") || args.includes("-h")) {
  process.stdout.write("Usage: delegate-to-antigravity setup [--request-id ID]\n\nUse the pinned npx command shown in Computers in ChatGPT. Installs the headless Windows companion. Complete setup in ChatGPT.\nNode.js is not needed afterward. Manage tasks and removal in ChatGPT or Windows Installed apps.\n");
} else {
  try {
    const config = JSON.parse(await readFile(new URL("./release-config.json", import.meta.url), "utf8"));
    process.stdout.write("Installing the headless companion. Complete computer approval, provider sign-ins and workspace permissions in ChatGPT.\n");
    const result = await runSetup({ args, config });
    if (result.enrollment?.pairing_code) process.stdout.write("In ChatGPT, connect this computer using code: " + result.enrollment.pairing_code + "\n");
    else process.stdout.write("The companion is installed. Return to Computers in ChatGPT to continue setup.\n");
    process.stdout.write("The background companion runs independently; this terminal can be closed.\n");
  } catch (error) {
    // URLs can contain temporary release credentials. Never echo fetch errors,
    // request headers, signed envelopes, or local private paths.
    process.stderr.write((error?.publicMessage || "Setup could not finish. No computer access was approved. Retry setup or use the official installer.") + "\n");
    process.exitCode = 1;
  }
}
