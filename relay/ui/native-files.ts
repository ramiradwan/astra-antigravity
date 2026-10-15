/** File bytes and returned host IDs stay in the component, outside model context. */
export type NativeFileMode = "host-download" | "chatgpt-upload" | "unavailable";
export type NativeFileContent =
  | { type: "resource_link"; uri: string; name: string; mimeType?: string; size?: number }
  | { type: "resource"; resource: { uri: string; mimeType?: string } & ({ text: string } | { blob: string }) };
export interface NativeFileRequest { contents: NativeFileContent[] }
export interface NativeFileTransferOptions {
  /** Supply only when the host advertises downloadFile. A failure never falls back to an upload. */
  downloadFile?: (request: NativeFileRequest) => Promise<{ isError?: boolean }>;
  uploadFile?: (file: File, options: { library: true }) => Promise<{ fileId: string }>;
  expectedOrigin?: string;
  expectedBytes?: number;
  expectedSha256?: string;
}
export interface NativeFileTransferResult { kind: "host-download" | "chatgpt-upload"; fileId?: string; isError?: boolean }
export const MAX_NATIVE_FILE_BYTES = 20 * 1024 * 1024;
const TRANSFER_TIMEOUT_MS = 30_000;
const unavailable = () => new Error("Native file delivery is unavailable in this ChatGPT host.");
const invalid = () => new Error("The file could not be verified. No file was added to ChatGPT.");
const boundedSize = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= MAX_NATIVE_FILE_BYTES;

export function nativeFileSupport(options: { hostDownload: boolean; openai?: { uploadFile?: unknown } }): NativeFileMode {
  return options.hostDownload ? "host-download" : typeof options.openai?.uploadFile === "function" ? "chatgpt-upload" : "unavailable";
}

function fileName(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 255 || value === "." || value === ".." || /[\\/:\u0000-\u001f\u007f]/.test(value)) throw invalid();
  return value;
}
function mimeType(value: unknown): string {
  if (value === undefined) return "application/octet-stream";
  if (typeof value !== "string" || !value || value.length > 200 || /[^\x20-\x7e]/.test(value)) throw invalid();
  return value;
}
function linkDetails(content: Extract<NativeFileContent, { type: "resource_link" }>, options: NativeFileTransferOptions) {
  const { expectedOrigin, expectedBytes, expectedSha256 } = options;
  if (typeof content.uri !== "string" || content.uri.length > 8192 || typeof expectedOrigin !== "string" || !boundedSize(expectedBytes) ||
      typeof expectedSha256 !== "string" || !/^[a-f0-9]{64}$/.test(expectedSha256) ||
      (content.size !== undefined && content.size !== expectedBytes)) throw invalid();
  try {
    const origin = new URL(expectedOrigin), url = new URL(content.uri);
    if (origin.protocol !== "https:" || origin.origin !== expectedOrigin || origin.username || origin.password ||
        url.protocol !== "https:" || url.origin !== expectedOrigin || url.href !== content.uri || url.username || url.password || url.search || url.hash ||
        !/^\/artifact-download\/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$/.test(url.pathname)) throw invalid();
    return { url: url.href, name: fileName(content.name), mime: mimeType(content.mimeType), bytes: expectedBytes };
  } catch { throw invalid(); }
}

async function fetchedBytes(url: string, expected: number): Promise<Uint8Array<ArrayBuffer>> {
  const controller = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined, response: Response | undefined, stopped = false;
  let expire!: () => void;
  const deadline = new Promise<never>((_resolve, reject) => { expire = () => { stopped = true; controller.abort(); reject(invalid()); }; });
  const timer = setTimeout(expire, TRANSFER_TIMEOUT_MS);
  try {
    return await Promise.race([(async () => {
      response = await fetch(url, { credentials: "omit", redirect: "error", cache: "no-store", signal: controller.signal });
      if (stopped || response.status !== 200 || (response.url && response.url !== url)) throw invalid();
      const length = response.headers.get("Content-Length");
      if (length !== null && (!/^(0|[1-9][0-9]*)$/.test(length) || Number(length) !== expected)) throw invalid();
      if (!response.body) { if (expected !== 0) throw invalid(); return new Uint8Array(); }
      reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (stopped) throw invalid();
        if (done) break;
        if (!(value instanceof Uint8Array) || value.byteLength > expected - size) throw invalid();
        size += value.byteLength;
        chunks.push(value);
      }
      if (size !== expected) throw invalid();
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      return bytes;
    })(), deadline]);
  } catch { throw invalid(); }
  finally {
    stopped = true;
    clearTimeout(timer);
    controller.abort();
    // Cancellation does not wait for a server or delay the bounded UI result.
    if (reader) void reader.cancel().catch(() => {});
    else if (response?.body) void response.body.cancel().catch(() => {});
  }
}

function embeddedFile(content: Extract<NativeFileContent, { type: "resource" }>) {
  const resource = content.resource;
  if (!resource || typeof resource.uri !== "string") throw invalid();
  let name: string;
  try {
    const uri = new URL(resource.uri);
    if (uri.protocol !== "file:" || uri.host || uri.search || uri.hash) throw invalid();
    name = fileName(decodeURIComponent(uri.pathname.split("/").at(-1) ?? ""));
  } catch { throw invalid(); }
  let bytes: Uint8Array<ArrayBuffer>;
  if ("text" in resource && !("blob" in resource) && typeof resource.text === "string" && resource.text.length <= MAX_NATIVE_FILE_BYTES) {
    bytes = new TextEncoder().encode(resource.text);
  } else if ("blob" in resource && !("text" in resource) && typeof resource.blob === "string" && resource.blob.length <= Math.ceil(MAX_NATIVE_FILE_BYTES / 3) * 4 &&
             resource.blob.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(resource.blob)) {
    try { bytes = Uint8Array.from(atob(resource.blob), value => value.charCodeAt(0)); }
    catch { throw invalid(); }
  } else throw invalid();
  if (!boundedSize(bytes.byteLength)) throw invalid();
  return { bytes, name, mime: mimeType(resource.mimeType) };
}
async function verifyBytes(bytes: Uint8Array<ArrayBuffer>, options: NativeFileTransferOptions): Promise<void> {
  if (!boundedSize(bytes.byteLength) || (options.expectedBytes !== undefined && (!boundedSize(options.expectedBytes) || bytes.byteLength !== options.expectedBytes))) throw invalid();
  if (options.expectedSha256 !== undefined) {
    if (!/^[a-f0-9]{64}$/.test(options.expectedSha256)) throw invalid();
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), value => value.toString(16).padStart(2, "0")).join("");
    if (digest !== options.expectedSha256) throw invalid();
  }
}

/** Called only after an explicit file action. Transfers exactly one bounded file. */
export async function transferNativeFile(request: NativeFileRequest, options: NativeFileTransferOptions): Promise<NativeFileTransferResult> {
  if (typeof options.downloadFile !== "function" && typeof options.uploadFile !== "function") throw unavailable();
  if (!request || !Array.isArray(request.contents) || request.contents.length !== 1) throw invalid();
  const content = request.contents[0];
  let file: ReturnType<typeof embeddedFile> | undefined;
  let link: ReturnType<typeof linkDetails> | undefined;
  if (content?.type === "resource_link") link = linkDetails(content, options);
  else if (content?.type === "resource") { file = embeddedFile(content); await verifyBytes(file.bytes, options); }
  else throw invalid();
  if (typeof options.downloadFile === "function") {
    try {
      const result = await options.downloadFile(request);
      if (!result || (result.isError !== undefined && typeof result.isError !== "boolean")) throw invalid();
      return { kind: "host-download", ...(result.isError ? { isError: true } : {}) };
    } catch { throw new Error("The host did not confirm file delivery. Check your files before trying again."); }
  }
  if (link) { file = { ...link, bytes: await fetchedBytes(link.url, link.bytes) }; await verifyBytes(file.bytes, options); }
  if (!file) throw invalid();
  try {
    const result = await options.uploadFile!(new File([file.bytes], file.name, { type: file.mime }), { library: true });
    if (!result || typeof result.fileId !== "string" || !result.fileId.trim() || result.fileId.length > 512) throw invalid();
    return { kind: "chatgpt-upload", fileId: result.fileId };
  } catch { throw new Error("ChatGPT did not confirm the file upload. Check your files before trying again."); }
}
