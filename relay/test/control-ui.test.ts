import { describe, expect, it } from "vitest";
import { controlFixtures } from "../dev/control-fixtures";
import { controlFrom, downloadGrantFrom, mayConfirm, mayOperate, operationFrom, operationState } from "../ui/control-controller";

const snapshot = () => structuredClone(controlFixtures.computers);
const envelope = (value: unknown) => ({ content: [], _meta: { astra_control: value } });
const future = "2035-01-01T00:00:00Z";
const operation = () => ({ operation_id: "operation-1", computer_id: "desktop", action: "pause", state: "prepared" as const,
  summary: "Pause new tasks. Work already running continues.", updated_at: "2026-10-15T12:30:00Z", confirmation_token: "a".repeat(64), expires_at: future });

describe("private ChatGPT control presentation", () => {
  it("accepts only the private metadata envelope and validates nested computer/task/receipt data", () => {
    expect(controlFrom(envelope(snapshot()))?.computers[0].label).toBe("Office computer");
    expect(controlFrom({ structuredContent: snapshot() })).toBeUndefined();
    const bad = snapshot(); bad.computers[0].allowed_actions = "pause" as never;
    expect(controlFrom(envelope(bad))).toBeUndefined();
    const missingPermission = snapshot(); delete (missingPermission.permissions as Partial<typeof missingPermission.permissions>).can_manage;
    expect(controlFrom(envelope(missingPermission))).toBeUndefined();
    const task = snapshot(); task.tasks[0].computer_id = "";
    expect(controlFrom(envelope(task))).toBeUndefined();
    task.tasks[0].status = "awaiting_setup";
    expect(controlFrom(envelope(task))?.tasks[0].computer_id).toBe("");
    task.tasks[0].computer_id = "../other-owner";
    expect(controlFrom(envelope(task))).toBeUndefined();
    const receipt = snapshot(); receipt.receipts[0].state = "saved" as never;
    expect(controlFrom(envelope(receipt))).toBeUndefined();
    const account = snapshot(); account.computers[0].accounts = { codex: { installed: true, status: "available" as never } };
    expect(controlFrom(envelope(account))).toBeUndefined();
  });
  it("keeps terminal tasks cancelled before pairing without accepting an unassigned running task", () => {
    const value = snapshot();
    value.tasks[0] = { ...value.tasks[0], status: "cancelled", terminal: true, computer_id: "" };
    expect(controlFrom(envelope(value))?.tasks[0]).toMatchObject({ status: "cancelled", terminal: true, computer_id: "" });
    value.tasks[0].terminal = false;
    expect(controlFrom(envelope(value))).toBeUndefined();
    value.tasks[0].status = "running";
    expect(controlFrom(envelope(value))).toBeUndefined();
    value.tasks[0].terminal = true;
    value.tasks[0].computer_id = "../other-owner";
    expect(controlFrom(envelope(value))).toBeUndefined();
  });
  it("accepts only a pinned setup command and rejects executable text from malformed metadata", () => {
    const value = snapshot();
    value.settings.setup_command = "npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.1/delegate-to-antigravity-0.4.1.tgz setup";
    expect(controlFrom(envelope(value))?.settings.setup_command).toBe(value.settings.setup_command);
    for (const command of ["npx --yes delegate-to-antigravity@latest setup", value.settings.setup_command + "; calc", value.settings.setup_command + " --request-id another-task", "powershell -Command anything"]) {
      value.settings.setup_command = command;
      expect(controlFrom(envelope(value))).toBeUndefined();
    }
  });
  it("requires both the current management grant and server-advertised action", () => {
    const value = snapshot(), computer = value.computers[0];
    expect(mayOperate(value, computer, "pause")).toBe(true);
    expect(mayOperate(value, computer, "restart")).toBe(false);
    expect(mayOperate({ ...value, permissions: { ...value.permissions, can_manage: false } }, computer, "pause")).toBe(false);
    expect(mayOperate(value, value.computers[2], "keep_approved")).toBe(false);
  });
  it("requires a fresh exact prepared confirmation and distinguishes pending from completed", () => {
    const value = operation();
    expect(operationFrom({ _meta: { astra_operation: value } })).toEqual(value);
    expect(mayConfirm(value)).toBe(true);
    expect(mayConfirm({ ...value, state: "pending" })).toBe(false);
    expect(mayConfirm({ ...value, confirmation_token: undefined })).toBe(false);
    expect(mayConfirm({ ...value, confirmation_token: "invalid" })).toBe(false);
    expect(mayConfirm({ ...value, expires_at: "2020-01-01T00:00:00Z" })).toBe(false);
    expect(operationState("pending")).toBe("Pending on computer");
    expect(operationState("completed")).toBe("Completed");
    expect(operationState("unconfirmed")).toBe("Completion unconfirmed");
    const unconfirmed = { ...value, state: "unconfirmed" as const, confirmation_token: undefined };
    expect(operationFrom({ _meta: { astra_operation: unconfirmed } })?.state).toBe("unconfirmed");
    expect(mayConfirm(unconfirmed)).toBe(false);
  });
  it("accepts a scoped 20MiB native grant and rejects changed details, expired or cross-origin links", () => {
    const artifact = { name: "report.zip", bytes: 20 * 1024 * 1024, sha256: "a".repeat(64), download_origin: "https://relay.example.test", download_url: "https://ignored.example.test/downloads/task/file" };
    const grant = { url: "https://relay.example.test/artifact-download/token", name: "report.zip", bytes: artifact.bytes,
      sha256: artifact.sha256, mime_type: "application/zip", expires_at: future };
    const wrap = (value: unknown) => ({ _meta: { astra_download: value } });
    expect(downloadGrantFrom(wrap(grant), artifact)?.bytes).toBe(20 * 1024 * 1024);
    for (const change of [{ bytes: artifact.bytes + 1 }, { sha256: "b".repeat(64) }, { expires_at: "invalid" },
      { expires_at: "2020-01-01T00:00:00Z" }, { url: "https://other.example.test/artifact-download/token" }, { url: "https://secret:token@relay.example.test/artifact-download/token" },
      { url: "javascript:alert(1)" }, { url: grant.url + "#fragment" }, { url: grant.url + "?token=alternate" },
      { url: "https://relay.example.test/downloads/task/file" }, { url: grant.url + "/extra" }, { url: "https://relay.example.test/artifact-download/encoded%2Fpath" }]) expect(downloadGrantFrom(wrap({ ...grant, ...change }), artifact)).toBeUndefined();
    for (const download_origin of [undefined, "", "http://relay.example.test", "https://relay.example.test/", "https://relay.example.test/path", "https://relay.example.test?query", "https://relay.example.test#fragment", "https://secret:token@relay.example.test"])
      expect(downloadGrantFrom(wrap(grant), { ...artifact, download_origin })).toBeUndefined();
  });
});
