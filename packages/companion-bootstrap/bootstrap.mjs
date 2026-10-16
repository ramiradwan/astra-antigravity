import { createHash, createPublicKey, randomUUID, verify } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import { mkdir, lstat, readFile, rename, rm } from "node:fs/promises";
import path from "node:path";
import { spawn } from "node:child_process";
import { finished } from "node:stream/promises";
import { once } from "node:events";

const REPOSITORY = "/ramiradwan/astra-antigravity/releases/download/";
const MAX_INSTALLER_BYTES = 256 * 1024 * 1024;
class SetupError extends Error {
  constructor(message) { super(message); this.publicMessage = message; }
}
const fail = (message) => { throw new SetupError(message); };

export function installerArgs(args) {
  const values = args.length ? [...args] : ["setup"];
  if (values.shift() !== "setup") fail("Use: delegate-to-antigravity setup [--request-id ID]");
  if (!values.length) return ["--install"];
  if (values.length !== 2 || values[0] !== "--request-id" || !/^[A-Za-z0-9_.-]{1,128}$/.test(values[1])) fail("The setup request ID is invalid. Copy the command from your ChatGPT setup card.");
  return ["--install", "--request-id", values[1]];
}

function secureUrl(value, { release = false, manifest = false, redirect = false } = {}) {
  let url;
  try { url = new URL(value); } catch { fail("The release address is invalid."); }
  if (url.protocol !== "https:" || url.username || url.password || url.port && url.port !== "443" || url.hash) fail("The release address is invalid.");
  const github = url.hostname === "github.com";
  const storage = ["release-assets.githubusercontent.com", "objects.githubusercontent.com"].includes(url.hostname);
  if (release && (!github || !url.pathname.startsWith(REPOSITORY) || url.search)) fail("The installer is not an approved release asset.");
  if (manifest && (!github || url.pathname !== "/ramiradwan/astra-antigravity/releases/latest/download/companion-win32-stable.json" || url.search)) fail("The update channel is invalid.");
  if (redirect && !(storage || github && url.pathname.startsWith(REPOSITORY))) fail("The release redirected to an unapproved host.");
  return url.href;
}

async function getResponse(url, request, maximum) {
  let destination = url;
  for (let hop = 0; hop < 5; hop++) {
    const response = await request(destination, { redirect: "manual", signal: AbortSignal.timeout(120_000), headers: { Accept: "application/octet-stream", "User-Agent": "Delegate-to-Antigravity-Setup/0.4.2" } });
    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const location = response.headers.get("location");
      await response.body?.cancel();
      if (!location) fail("The release download did not provide a destination.");
      destination = secureUrl(new URL(location, destination).href, { redirect: true });
      continue;
    }
    if (response.status === 404) fail("The signed companion release is not published yet. No installer was run.");
    if (!response.ok || !response.body) fail("The official release could not be downloaded. Retry setup later.");
    const declared = Number(response.headers.get("content-length"));
    if (declared && (!Number.isSafeInteger(declared) || declared > maximum)) { await response.body.cancel(); fail("The release exceeded its download limit."); }
    return response;
  }
  fail("The release redirected too many times.");
}

async function bytes(response, maximum) {
  const chunks = []; let length = 0;
  for await (const chunk of response.body) {
    length += chunk.length;
    if (length > maximum) { fail("The release exceeded its download limit."); }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export function verifyInstaller(envelopeBytes, publicKey) {
  let envelope, raw, signature, key, manifest;
  try {
    envelope = JSON.parse(envelopeBytes.toString("utf8"));
    if (typeof envelope.manifest !== "string" || typeof envelope.signature !== "string") throw Error();
    raw = Buffer.from(envelope.manifest, "base64"); signature = Buffer.from(envelope.signature, "base64");
    const publicBytes = Buffer.from(publicKey, "base64");
    if (publicBytes.length !== 32 || signature.length !== 64 || raw.length > 32_768) throw Error();
    key = createPublicKey({ key: Buffer.concat([Buffer.from("302a300506032b6570032100", "hex"), publicBytes]), format: "der", type: "spki" });
    if (!verify(null, raw, key, signature)) throw Error();
    manifest = JSON.parse(raw.toString("utf8"));
  } catch { fail("The companion release signature could not be verified. No installer was run."); }
  const installer = manifest.installer;
  if (manifest.schema !== 1 || manifest.platform !== "win32" || !/^\d+\.\d+\.\d+$/.test(manifest.version) ||
      !Number.isSafeInteger(manifest.sequence) || manifest.sequence < 1 || manifest.state_schema !== 1 || !installer ||
      !Number.isSafeInteger(installer.size) || installer.size < 1 || installer.size > MAX_INSTALLER_BYTES || !/^[a-f0-9]{64}$/.test(installer.sha256)) fail("The signed release does not contain a compatible Windows installer.");
  secureUrl(installer.url, { release: true });
  return { ...installer, version: manifest.version, sequence: manifest.sequence };
}

async function checkChannel(channelPath, installer) {
  try {
    const info = await lstat(channelPath);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 1024) fail("The installer channel state needs local review.");
    const previous = JSON.parse(await readFile(channelPath, "utf8"));
    if (!Number.isSafeInteger(previous.sequence) || previous.sequence > installer.sequence) fail("The release is older than one already verified on this computer.");
  } catch (error) { if (error.code !== "ENOENT") throw error; }
}

export async function runSetup({ args = [], config, platform = process.platform, localAppData = process.env.LOCALAPPDATA, request = fetch, launch } = {}) {
  const launchArgs = installerArgs(args);
  if (platform !== "win32") fail("The packaged companion currently supports Windows. Run setup on the Windows computer that will perform your tasks.");
  if (!localAppData || !path.isAbsolute(localAppData)) fail("Windows did not provide a valid per-user installation directory.");
  const manifestUrl = secureUrl(config?.manifest_url, { manifest: true });
  const envelope = await bytes(await getResponse(manifestUrl, request, 16_384), 16_384);
  const installer = verifyInstaller(envelope, config.public_key);
  const directory = path.resolve(localAppData, "AstraAntigravity", "bootstrap");
  const installRoot = path.resolve(localAppData, "AstraAntigravity");
  if (path.dirname(directory) !== installRoot) fail("The installer directory is invalid.");
  // Refuse junction/symlink redirection anywhere below the per-user parent.
  for (const candidate of [localAppData, installRoot, directory]) {
    try { if ((await lstat(candidate)).isSymbolicLink()) fail("The installer directory needs local review."); }
    catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  await mkdir(directory, { recursive: true });
  const channelPath = path.join(directory, "channel.json");
  await checkChannel(channelPath, installer);
  const executable = path.join(directory, installer.sha256 + ".exe");
  let present = false;
  try {
    const info = await lstat(executable);
    if (!info.isFile() || info.isSymbolicLink() || info.size !== installer.size) fail("An existing installer cache needs local review.");
    const checksum = createHash("sha256"); let counted = 0;
    for await (const chunk of createReadStream(executable)) { counted += chunk.length; if (counted > installer.size) fail("An existing installer cache needs local review."); checksum.update(chunk); }
    if (counted !== installer.size || checksum.digest("hex") !== installer.sha256) fail("An existing installer cache needs local review.");
    present = true;
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  if (!present) {
    const temporary = executable + "." + randomUUID() + ".download";
    const stream = createWriteStream(temporary, { flags: "wx", mode: 0o600 });
    const completion = finished(stream);
    // Attach immediately so an I/O failure cannot become an unhandled promise.
    completion.catch(() => {});
    try {
      const response = await getResponse(installer.url, request, installer.size);
      const digest = createHash("sha256"); let total = 0;
      for await (const chunk of response.body) {
        total += chunk.length;
        if (total > installer.size) fail("The installer exceeded its signed size.");
        digest.update(chunk);
        if (!stream.write(chunk)) await once(stream, "drain");
      }
      stream.end(); await completion;
      if (total !== installer.size || digest.digest("hex") !== installer.sha256) fail("The installer did not match its signed release. No installer was run.");
      await rename(temporary, executable);
    } catch (error) {
      stream.destroy(); await completion.catch(() => {});
      // One exact file created by this invocation; never recursive cleanup.
      await rm(temporary, { force: true });
      throw error;
    }
  }
  const proof = path.join(directory, installer.sha256 + ".manifest.json");
  try { if ((await lstat(proof)).isSymbolicLink()) fail("The installer proof needs local review."); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  const proofTemporary = proof + "." + randomUUID() + ".new";
  const proofStream = createWriteStream(proofTemporary, { flags: "wx", mode: 0o600 });
  const proofDone = finished(proofStream);
  proofStream.end(envelope); await proofDone;
  await rename(proofTemporary, proof);
  const marker = channelPath + "." + randomUUID() + ".new";
  const markerStream = createWriteStream(marker, { flags: "wx", mode: 0o600 });
  const markerDone = finished(markerStream);
  markerStream.end(JSON.stringify({ sequence: installer.sequence, version: installer.version, installer_sha256: installer.sha256 }) + "\n");
  await markerDone;
  // A download may finish after a newer setup invocation. Recheck immediately
  // before publishing; the native install mutex also enforces no downgrade.
  try { await checkChannel(channelPath, installer); }
  catch (error) { await rm(marker, { force: true }); throw error; }
  await rename(marker, channelPath);
  const enrollment = launch ? await launch(executable, launchArgs) : await new Promise((resolve, reject) => {
    // The installer starts its detached managed agent and exits after returning
    // a bounded public enrollment receipt. The terminal is never the worker.
    const child = spawn(executable, launchArgs, { windowsHide: true, shell: false, stdio: ["ignore", "pipe", "ignore"] });
    let output = "", exceeded = false;
    const timer = setTimeout(() => { child.kill(); reject(new SetupError("The installer did not return setup status. Retry the setup command; existing work will not restart.")); }, 35_000);
    child.stdout.on("data", chunk => { output += chunk.toString("utf8"); if (Buffer.byteLength(output) > 32_768) { exceeded = true; child.kill(); } });
    child.once("error", error => { clearTimeout(timer); reject(error); });
    child.once("close", code => {
      clearTimeout(timer);
      if (code !== 0 || exceeded) { reject(new SetupError("The installer could not finish. No task was started.")); return; }
      try { resolve(JSON.parse(output.trim())); }
      catch { reject(new SetupError("The installer did not return setup status. Retry the setup command.")); }
    });
  });
  let publicEnrollment;
  if (enrollment && typeof enrollment === "object") {
    publicEnrollment = { status: typeof enrollment.status === "string" ? enrollment.status.slice(0, 64) : "pending" };
    if (typeof enrollment.pairing_code === "string" && /^[a-f0-9]{24}$/.test(enrollment.pairing_code)) publicEnrollment.pairing_code = enrollment.pairing_code;
  }
  return { started: true, version: installer.version, ...(publicEnrollment ? { enrollment: publicEnrollment } : {}) };
}
