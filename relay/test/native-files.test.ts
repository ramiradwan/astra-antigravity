import { afterEach, describe, expect, it, vi } from "vitest";
import { MAX_NATIVE_FILE_BYTES, nativeFileSupport, transferNativeFile, type NativeFileRequest } from "../ui/native-files";

const origin = "https://relay.example.test", url = origin + "/artifact-download/owned.capability";
const text = "Verified artifact: λ\n", bytes = new TextEncoder().encode(text);
const hash = async (value: Uint8Array<ArrayBuffer>) => Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", value)), part => part.toString(16).padStart(2, "0")).join("");
const request = (): NativeFileRequest => ({ contents: [{ type: "resource_link", uri: url, name: "result.txt", mimeType: "text/plain", size: bytes.length }] });
const pins = async () => ({ expectedOrigin: origin, expectedBytes: bytes.length, expectedSha256: await hash(bytes) });
const embedded = (): NativeFileRequest => ({ contents: [{ type: "resource", resource: { uri: "file:///records.json", mimeType: "application/json", text: '{"verified":true}' } }] });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

describe("native file delivery", () => {
  it("detects actual optional capabilities and prefers the standard host transfer", () => {
    expect(nativeFileSupport({ hostDownload: true, openai: { uploadFile: () => {} } })).toBe("host-download");
    expect(nativeFileSupport({ hostDownload: false, openai: { uploadFile: () => {} } })).toBe("chatgpt-upload");
    expect(nativeFileSupport({ hostDownload: false })).toBe("unavailable");
    expect(nativeFileSupport({ hostDownload: false, openai: { uploadFile: true } })).toBe("unavailable");
  });
  it("fails without a native capability and does not fetch an artifact", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected network request"));
    await expect(transferNativeFile(request(), await pins())).rejects.toThrow("unavailable");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("sends a pinned resource link through the advertised standard host without fetching or uploading", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected network request")), downloadFile = vi.fn(async () => ({})), uploadFile = vi.fn(async () => ({ fileId: "unused" }));
    const contents = request();
    expect(await transferNativeFile(contents, { ...await pins(), downloadFile, uploadFile })).toEqual({ kind: "host-download" });
    expect(downloadFile).toHaveBeenCalledExactlyOnceWith(contents);
    expect(fetch).not.toHaveBeenCalled(); expect(uploadFile).not.toHaveBeenCalled();
  });
  it("never uploads after a denied or failed standard host request", async () => {
    const uploadFile = vi.fn(async () => ({ fileId: "unused" })), fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected network request"));
    expect(await transferNativeFile(embedded(), { downloadFile: async () => ({ isError: true }), uploadFile })).toEqual({ kind: "host-download", isError: true });
    await expect(transferNativeFile(embedded(), { downloadFile: async () => { throw new Error("private host detail"); }, uploadFile })).rejects.toThrow("host did not confirm");
    expect(fetch).not.toHaveBeenCalled(); expect(uploadFile).not.toHaveBeenCalled();
  });
  it("uploads only verified artifact bytes with safe fetch options and the documented library flag", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new ReadableStream({ start(controller) {
      controller.enqueue(bytes.slice(0, 4)); controller.enqueue(bytes.slice(4)); controller.close();
    } }), { headers: { "Content-Length": String(bytes.length) } }));
    const uploadFile = vi.fn(async (_file: File, _options: { library: true }) => ({ fileId: "host-provided-file-id" }));
    expect(await transferNativeFile(request(), { ...await pins(), uploadFile })).toEqual({ kind: "chatgpt-upload", fileId: "host-provided-file-id" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe(url);
    expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: "omit", redirect: "error", cache: "no-store" });
    expect(fetch.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
    const [file, options] = uploadFile.mock.calls[0];
    expect(file).toBeInstanceOf(File); expect(file.name).toBe("result.txt"); expect(file.type).toBe("text/plain");
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(bytes); expect(options).toEqual({ library: true });
  });
  it("rejects noncanonical or unpinned links before any host, network, or upload action", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected network request")), uploadFile = vi.fn(async () => ({ fileId: "unused" }));
    const expected = await pins();
    for (const invalidUrl of ["http://relay.example.test/artifact-download/token", "https://other.example.test/artifact-download/token",
      "https://user:secret@relay.example.test/artifact-download/token", url + "?alternate=1", url + "#fragment", url + "/extra",
      origin + "/downloads/task/file", origin + "/artifact-download/encoded%2Fsegment", "https://RELAY.example.test/artifact-download/token"]) {
      const value = request(); value.contents[0] = { type: "resource_link", uri: invalidUrl, name: "result.txt", size: bytes.length };
      await expect(transferNativeFile(value, { ...expected, uploadFile })).rejects.toThrow("could not be verified");
    }
    for (const changes of [{ expectedOrigin: origin + "/" }, { expectedOrigin: undefined }, { expectedBytes: undefined },
      { expectedBytes: MAX_NATIVE_FILE_BYTES + 1 }, { expectedSha256: undefined }, { expectedSha256: "wrong" }, { expectedBytes: bytes.length + 1 }])
      await expect(transferNativeFile(request(), { ...expected, ...changes, uploadFile })).rejects.toThrow("could not be verified");
    expect(fetch).not.toHaveBeenCalled(); expect(uploadFile).not.toHaveBeenCalled();
  });
  it("rejects a failed response, changed destination, or changed content length before upload", async () => {
    const fetch = vi.spyOn(globalThis, "fetch"), uploadFile = vi.fn(async () => ({ fileId: "unused" }));
    for (const response of [new Response("denied", { status: 403 }), new Response(bytes, { headers: { "Content-Length": String(bytes.length + 1) } }),
      { status: 200, url: "https://other.example.test/file", headers: new Headers(), body: null } as Response]) {
      fetch.mockResolvedValueOnce(response);
      await expect(transferNativeFile(request(), { ...await pins(), uploadFile })).rejects.toThrow("could not be verified");
    }
    expect(uploadFile).not.toHaveBeenCalled();
  });
  it("stops and cancels a stream that exceeds its exact expected byte count without trusting headers", async () => {
    let reads = 0;
    const cancelled = vi.fn(), uploadFile = vi.fn(async () => ({ fileId: "unused" }));
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new ReadableStream({ pull(controller) {
      controller.enqueue(reads++ === 0 ? bytes : new Uint8Array([1]));
    }, cancel: cancelled })));
    await expect(transferNativeFile(request(), { ...await pins(), uploadFile })).rejects.toThrow("could not be verified");
    expect(cancelled).toHaveBeenCalledTimes(1); expect(uploadFile).not.toHaveBeenCalled();
  });
  it("rejects truncated or hash-mismatched bytes and never uploads them", async () => {
    const fetch = vi.spyOn(globalThis, "fetch"), uploadFile = vi.fn(async () => ({ fileId: "unused" }));
    fetch.mockResolvedValueOnce(new Response(bytes.slice(0, -1)));
    await expect(transferNativeFile(request(), { ...await pins(), uploadFile })).rejects.toThrow("could not be verified");
    fetch.mockResolvedValueOnce(new Response(bytes));
    await expect(transferNativeFile(request(), { ...await pins(), expectedSha256: "0".repeat(64), uploadFile })).rejects.toThrow("could not be verified");
    expect(uploadFile).not.toHaveBeenCalled();
  });
  it("supports embedded account exports as text or base64 without any network request", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Unexpected network request")), uploadFile = vi.fn(async (_file: File, _options: { library: true }) => ({ fileId: "private-export-id" }));
    for (const content of [{ text }, { blob: btoa(String.fromCharCode(...bytes)) }]) {
      expect(await transferNativeFile({ contents: [{ type: "resource", resource: { uri: "file:///records.json", mimeType: "application/json", ...content } }] },
        { expectedBytes: bytes.length, expectedSha256: await hash(bytes), uploadFile })).toEqual({ kind: "chatgpt-upload", fileId: "private-export-id" });
    }
    for (const [file, options] of uploadFile.mock.calls) { expect(await file.text()).toBe(text); expect(file.name).toBe("records.json"); expect(options).toEqual({ library: true }); }
    expect(fetch).not.toHaveBeenCalled();
  });
  it("rejects oversized, malformed, or ambiguous embedded content before native transfer", async () => {
    const uploadFile = vi.fn(async () => ({ fileId: "unused" })), downloadFile = vi.fn(async () => ({}));
    for (const content of [{ text: "x".repeat(MAX_NATIVE_FILE_BYTES + 1) }, { blob: "A".repeat(Math.ceil(MAX_NATIVE_FILE_BYTES / 3) * 4 + 4) },
      { blob: "not base64" }, { text: "text", blob: "dGV4dA==" }]) {
      const value = { contents: [{ type: "resource", resource: { uri: "file:///records.json", ...content } }] } as NativeFileRequest;
      await expect(transferNativeFile(value, { uploadFile, downloadFile })).rejects.toThrow("could not be verified");
    }
    await expect(transferNativeFile({ contents: [...embedded().contents, ...embedded().contents] }, { uploadFile })).rejects.toThrow("could not be verified");
    expect(uploadFile).not.toHaveBeenCalled(); expect(downloadFile).not.toHaveBeenCalled();
  });
  it("bounds a stalled response by time, aborts and cancels it, and never uploads", async () => {
    vi.useFakeTimers();
    const cancelled = vi.fn(), uploadFile = vi.fn(async () => ({ fileId: "unused" }));
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new ReadableStream({ cancel: cancelled })));
    const pending = transferNativeFile(request(), { ...await pins(), uploadFile });
    const rejected = expect(pending).rejects.toThrow("could not be verified");
    await vi.advanceTimersByTimeAsync(30_000); await rejected;
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true); expect(cancelled).toHaveBeenCalledTimes(1); expect(uploadFile).not.toHaveBeenCalled();
  });
  it("requires a genuine host file ID and suppresses raw upload errors", async () => {
    await expect(transferNativeFile(embedded(), { uploadFile: async () => ({ fileId: "" }) })).rejects.toThrow("ChatGPT did not confirm");
    try {
      await transferNativeFile(embedded(), { uploadFile: async () => { throw new Error("private capability and upstream error"); } });
      throw new Error("unexpected upload success");
    } catch (error) { expect(String(error)).toBe("Error: ChatGPT did not confirm the file upload. Check your files before trying again."); }
  });
});
