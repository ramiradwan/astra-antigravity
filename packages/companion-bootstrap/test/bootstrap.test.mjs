import test from "node:test";
import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { installerArgs, runSetup, verifyInstaller } from "../bootstrap.mjs";

const feed = "https://github.com/ramiradwan/astra-antigravity/releases/latest/download/companion-win32-stable.json";
const url = "https://github.com/ramiradwan/astra-antigravity/releases/download/v0.4.0/AstraAntigravity.exe";
function fixture(overrides = {}) {
  const keys = generateKeyPairSync("ed25519"), binary = Buffer.from("Synthetic installer; never executed.");
  const manifest = { schema: 1, platform: "win32", sequence: 1, version: "0.4.0", state_schema: 1,
    installer: { url, size: binary.length, sha256: createHash("sha256").update(binary).digest("hex") }, ...overrides };
  const raw = Buffer.from(JSON.stringify(manifest));
  const envelope = Buffer.from(JSON.stringify({ manifest: raw.toString("base64"), signature: sign(null, raw, keys.privateKey).toString("base64") }));
  const config = { manifest_url: feed, public_key: keys.publicKey.export({ format: "der", type: "spki" }).subarray(-32).toString("base64") };
  const requests = [], launches = [];
  return { config, manifest, binary, envelope, requests, launches,
    request: async (address) => { requests.push(address); return new Response(address === feed ? envelope : binary); },
    launch: async (...args) => { launches.push(args); } };
}
async function temporary(action) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "delegate-bootstrap-test-"));
  try { return await action(directory); }
  finally { await rm(directory, { recursive: true, force: true }); }
}

test("one command verifies, caches and starts the independent installer with a saved task", async () => temporary(async directory => {
  const value = fixture();
  const result = await runSetup({ ...value, localAppData: directory, platform: "win32", args: ["setup", "--request-id", "original-task"] });
  assert.deepEqual(result, { started: true, version: "0.4.0" });
  assert.equal(value.launches.length, 1);
  assert.deepEqual(value.launches[0][1], ["--install", "--request-id", "original-task"]);
  assert.deepEqual(await readFile(value.launches[0][0]), value.binary);
  await runSetup({ ...value, localAppData: directory, platform: "win32" });
  assert.equal(value.requests.filter(address => address === url).length, 1, "cached installer is rehashed rather than downloaded again");
}));

test("signature tampering never creates an installer or launches a process", async () => temporary(async directory => {
  const value = fixture(), other = fixture();
  value.config.public_key = other.config.public_key;
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /signature/);
  assert.equal(value.launches.length, 0);
  assert.deepEqual(await readdir(directory), []);
}));

test("wrong file hash and oversized transfer are rejected and exact temporary download is removed", async () => temporary(async directory => {
  const value = fixture();
  value.request = async address => new Response(address === feed ? value.envelope : Buffer.alloc(value.binary.length + 1));
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /signed size/);
  assert.equal(value.launches.length, 0);
  assert.deepEqual(await readdir(path.join(directory, "AstraAntigravity", "bootstrap")), []);
  value.request = async address => new Response(address === feed ? value.envelope : Buffer.alloc(value.binary.length));
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /signed release/);
  assert.equal(value.launches.length, 0);
}));

test("unapproved release redirect is rejected without sending a second request", async () => temporary(async directory => {
  const value = fixture(); let calls = 0;
  value.request = async address => { calls++; return address === feed ? new Response(value.envelope) : new Response(null, { status: 302, headers: { Location: "https://untrusted.example/install.exe" } }); };
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /unapproved host/);
  assert.equal(calls, 2); assert.equal(value.launches.length, 0);
}));

test("a modified cache cannot be executed", async () => temporary(async directory => {
  const value = fixture(); await runSetup({ ...value, localAppData: directory, platform: "win32" });
  const target = value.launches[0][0]; await writeFile(target, Buffer.alloc(value.binary.length));
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /cache needs local review/);
  assert.equal(value.launches.length, 1);
}));

test("channel replay below a previously verified sequence is blocked", async () => temporary(async directory => {
  const value = fixture(); await runSetup({ ...value, localAppData: directory, platform: "win32" });
  await writeFile(path.join(path.dirname(value.launches[0][0]), "channel.json"), JSON.stringify({ sequence: 2 }));
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /older than/);
}));

test("a slow download cannot overwrite a newer verified channel", async () => temporary(async directory => {
  const value = fixture();
  const channel = path.join(directory, "AstraAntigravity", "bootstrap", "channel.json");
  value.request = async address => {
    if (address === feed) return new Response(value.envelope);
    await writeFile(channel, JSON.stringify({ sequence: 2, version: "0.4.1" }));
    return new Response(value.binary);
  };
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /older than/);
  assert.equal(value.launches.length, 0);
  assert.equal(JSON.parse(await readFile(channel, "utf8")).sequence, 2);
}));

test("unsupported platforms and unpublished releases fail honestly", async () => temporary(async directory => {
  const value = fixture();
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "linux" }), /supports Windows/);
  assert.equal(value.requests.length, 0);
  value.request = async () => new Response(null, { status: 404 });
  await assert.rejects(runSetup({ ...value, localAppData: directory, platform: "win32" }), /not published yet/);
  assert.deepEqual(await readdir(directory), []);
}));

test("CLI accepts only setup and one inert request identifier", () => {
  assert.deepEqual(installerArgs([]), ["--install"]);
  for (const args of [["setup", "--relay", "https://example.com"], ["setup", "--request-id", "x & calc"], ["setup", "--request-id", "valid", "--extra"], ["run"]]) assert.throws(() => installerArgs(args));
  const value = fixture({ platform: "darwin" }); assert.throws(() => verifyInstaller(value.envelope, value.config.public_key), /compatible Windows/);
});
