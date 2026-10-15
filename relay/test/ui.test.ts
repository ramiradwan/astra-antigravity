import { describe, expect, it } from "vitest";
import type { UiSnapshot } from "../src/ui-contracts";
import { canWriteTasks, cardStatus, cardView, downloadRoute, formatUpdatedAt, httpsUrl, outcomePresentation, snapshotFrom, SnapshotWatch, taskHeading, verifiedArtifact, type WatchClock } from "../ui/controller";

const snapshot = (overrides: Partial<UiSnapshot> = {}): UiSnapshot => ({
  version: 1, delegation_id: "task-1", revision: "revision-1", status: "running", terminal: false,
  summary: "Worker is running the saved task.", updated_at: "2026-10-15T00:00:00Z",
  connection: { state: "online" }, notifications: { active: false }, permissions: { can_write_tasks: true }, ...overrides,
});
const result = (value: UiSnapshot) => ({ content: [], _meta: { astra_ui: value } });
function timers() {
  let next = 0;
  const pending = new Map<number, { callback: () => void; milliseconds: number }>();
  return {
    clock: { schedule(callback, milliseconds) { const id = ++next; pending.set(id, { callback, milliseconds }); return id; }, cancel(handle) { pending.delete(handle as number); } } satisfies WatchClock,
    pending,
    run() { const entry = pending.entries().next().value; if (!entry) throw new Error("No scheduled read"); pending.delete(entry[0]); entry[1].callback(); },
  };
}
const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

describe("ChatGPT task card", () => {
  it("keeps task identity stable while status and progress updates change", () => {
    const running = snapshot({ task_summary: "Create the quarterly report" });
    const completed = { ...running, status: "completed", terminal: true };
    expect(taskHeading(running)).toBe("Create the quarterly report");
    expect(taskHeading(completed)).toBe(taskHeading(running));
    expect(taskHeading(snapshot({ task_summary: "  " }))).toBe("Antigravity task");
    expect(taskHeading(snapshot({ delegation_id: undefined }))).toBe("Your computer");
    expect(cardStatus(completed, cardView(completed).title)).toEqual({ icon: "check", label: "Work finished", note: "Review pending" });
    expect(cardStatus(running, cardView(running).title)).toEqual({ icon: "activity", label: "Task in progress" });
  });

  it("keeps refresh subordinate and gives every state an explicit icon and label", () => {
    for (const state of [snapshot(), snapshot({ status: "queued" }), snapshot({ status: "cancelling" }), snapshot({ connection: { state: "offline" } }), snapshot({ connection: { state: "revoked" } })]) {
      const view = cardView(state);
      expect(view.actions.find(action => action.id === "refresh")?.primary).not.toBe(true);
      expect(cardStatus(state, view.title).label).toBeTruthy();
      expect(cardStatus(state, view.title).icon).toBeTruthy();
      expect(view.actions.filter(action => action.primary).length).toBeLessThanOrEqual(1);
    }
    const completed = snapshot({ status: "completed", terminal: true, connection: { state: "offline" } });
    expect(cardStatus(completed, cardView(completed).title).note).toBe("Review pending");
    const revoked = { ...completed, connection: { state: "revoked" as const } };
    expect(cardStatus(revoked, cardView(revoked).title)).toEqual({ icon: "alert", label: "Computer access was revoked" });
  });

  it("offers explicit Opus selection when model availability stops a terminal attempt", () => {
    const view = cardView(snapshot({status:"failed",terminal:true,decision:{summary:"Gemini 4 Argon High is unavailable. Opus 5.5 is offered.",opus_available:true}}));
    expect(view.title).toBe("Your decision is needed");
    expect(view.actions.map(action=>action.id)).toEqual(["respond","opus"]);
    expect(view.actions.find(action=>action.id==="opus")?.label).toBe("Approve Opus 5.5");
    const unavailable=cardView(snapshot({status:"failed",terminal:true,decision:{summary:"Argon and Opus are unavailable.",opus_available:false}}));
    expect(unavailable.actions.map(action=>action.id)).toEqual(["respond"]);
  });
  it("waits for an owned metadata snapshot rather than assuming initial approved input", () => {
    expect(snapshotFrom(undefined)).toBeUndefined();
    expect(snapshotFrom({ structuredContent: { delegation_id: "task-1" } })).toBeUndefined();
    expect(snapshotFrom(result(snapshot()))?.delegation_id).toBe("task-1");
    expect(snapshotFrom(result(snapshot({ delegation_id: "../other-owner" })))).toBeUndefined();
    expect(snapshotFrom(result(snapshot({ decision: { summary: 42 } as never })))).toBeUndefined();
    expect(snapshotFrom(result(snapshot({ worker_checks: "not an array" as never })))).toBeUndefined();
    expect(snapshotFrom(result(snapshot({ artifacts: [{ name: null }] as never })))).toBeUndefined();
    expect(snapshotFrom(result(snapshot({ permissions: { can_write_tasks: "yes" } as never })))).toBeUndefined();
    expect(snapshotFrom(result(snapshot({ permissions: undefined })))).toBeDefined();
  });

  it("keeps one foreground watch and an independent revision cursor without launch or acknowledgement", async () => {
    const time = timers(), calls: { name: string; args: Record<string, unknown> }[] = [];
    let resolve!: (value: unknown) => void;
    const watch = new SnapshotWatch({ callTool(name, args) { calls.push({ name, args }); return new Promise(done => { resolve = done; }); } }, () => {}, time.clock);
    watch.accept(result(snapshot())); time.run();
    watch.refresh(); watch.refresh();
    expect(calls).toEqual([{ name: "watch_delegation_ui", args: { delegation_id: "task-1", after_revision: "revision-1", wait_ms: 20_000 } }]);
    resolve(result(snapshot({ revision: "revision-2" }))); await flush();
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual({ name: "watch_delegation_ui", args: { delegation_id: "task-1", after_revision: "revision-2", wait_ms: 0 } });
    watch.dispose(); resolve(result(snapshot())); await flush();
    expect(time.pending.size).toBe(0);
  });

  it("pauses hidden cards, resumes one read, and stops automatically at terminal state", async () => {
    const time = timers(); let calls = 0;
    let resolve!: (value: unknown) => void;
    const watch = new SnapshotWatch({ callTool() { calls++; return new Promise(done => { resolve = done; }); } }, () => {}, time.clock);
    watch.setVisible(false); watch.accept(result(snapshot())); expect(time.pending.size).toBe(0);
    watch.setVisible(true); time.run(); watch.setVisible(false);
    resolve(result(snapshot({ revision: "revision-2" }))); await flush();
    expect(time.pending.size).toBe(0); expect(calls).toBe(1);
    watch.setVisible(true); time.run();
    resolve(result(snapshot({ revision: "revision-3", status: "completed", terminal: true }))); await flush();
    expect(calls).toBe(2); expect(time.pending.size).toBe(0);
    watch.setVisible(false); watch.setVisible(true); expect(time.pending.size).toBe(0);
  });

  it("ignores a stale in-flight result after the host supplies a newer snapshot", async () => {
    const time = timers(); let resolve!: (value: unknown) => void;
    const watch = new SnapshotWatch({ callTool: () => new Promise(done => { resolve = done; }) }, () => {}, time.clock);
    watch.accept(result(snapshot())); time.run();
    watch.accept(result(snapshot({ status: "completed", terminal: true, revision: "revision-3" })));
    resolve(result(snapshot({ revision: "revision-2" }))); await flush();
    expect(watch.current?.status).toBe("completed"); expect(time.pending.size).toBe(0);
  });

  it("backs off only inside the card after transport errors", async () => {
    const time = timers();
    const watch = new SnapshotWatch({ callTool: async () => { throw new Error("transport"); } }, () => {}, time.clock);
    watch.accept(result(snapshot())); time.run(); await flush();
    expect([...time.pending.values()][0].milliseconds).toBe(2_000);
    time.run(); await flush(); expect([...time.pending.values()][0].milliseconds).toBe(4_000);
    expect(watch.error).toContain("not been restarted"); watch.dispose();
  });

  it("refreshes connection readiness without inventing a task or model loop", async () => {
    const time = timers(), calls: Record<string, unknown>[] = [];
    const value = snapshot({ delegation_id: undefined, status: "connection", connection: { state: "setup_required" } });
    const watch = new SnapshotWatch({ callTool: async (_name, args) => { calls.push(args); return result(value); } }, () => {}, time.clock);
    watch.accept(result(value)); expect([...time.pending.values()][0].milliseconds).toBe(10_000);
    time.run(); await flush(); expect(calls[0]).toEqual({ after_revision: "revision-1", wait_ms: 0 }); watch.dispose();
  });

  it("shows cancellation and recovery truthfully and offers Opus only when available", () => {
    expect(cardView(snapshot({ status: "cancelling" })).title).toBe("Cancellation requested");
    expect(cardView(snapshot({ status: "cancelling" })).detail).toContain("Waiting for the worker to confirm");
    expect(cardView(snapshot({ status: "cancelling" })).actions.map(value => value.id)).not.toContain("cancel");
    expect(cardView(snapshot({ connection: { state: "expired" } })).title).toBe("Computer access expired");
    expect(cardView(snapshot({ connection: { state: "revoked" } })).title).toBe("Computer access was revoked");
    expect(cardView(snapshot({ delegation_id: undefined, connection: { state: "offline" } })).title).toBe("Computer is offline");
    expect(cardView(snapshot({ delegation_id: undefined, connection: { state: "expired" } })).title).toBe("Computer access expired");
    expect(cardView(snapshot({ delegation_id: undefined, connection: { state: "revoked" } })).title).toBe("Computer access was revoked");
    for (const status of ["unknown", "recovery_required"]) {
      const recovering = cardView(snapshot({ status }));
      expect(recovering.title).toBe("Task status unavailable");
      expect(recovering.detail).toContain("It will not be started again automatically");
    }
    expect(cardView(snapshot({ terminal: true, status: "completed", connection: { state: "revoked" } })).actions.map(value => value.id)).not.toContain("review");
    expect(cardView(snapshot({ delegation_id: undefined, connection: { state: "setup_required" } })).detail).not.toContain("saved task");
    const unavailable = snapshot({ decision: { summary: "Gemini 4 Argon is unavailable." } });
    expect(cardView(unavailable).actions.map(value => value.id)).toEqual(["respond"]);
    expect(cardView(snapshot({ decision: { ...unavailable.decision!, opus_available: true } })).actions.map(value => value.id)).toEqual(["respond", "opus"]);
    expect(cardView(snapshot({ ...unavailable, status: "completed", terminal: true })).title).toBe("Work finished");
    for (const state of [snapshot(), unavailable, snapshot({ status: "completed", terminal: true }), snapshot({ status: "awaiting_setup" })]) expect(cardView(state).actions.length).toBeLessThanOrEqual(2);
  });

  it("shows view-only access and offers write actions only with an explicit current grant", () => {
    for (const permissions of [undefined, { can_write_tasks: false }]) {
      const state = snapshot({ permissions });
      expect(canWriteTasks(state)).toBe(false);
      expect(cardView(state).actions.map(action => action.id)).not.toContain("cancel");
      const connected = cardView({ ...state, delegation_id: undefined });
      expect(connected.title).toContain("view only");
      expect(connected.detail).toContain("reconnect the plugin with task access");
      expect(connected.actions.map(action => action.id)).toEqual(["connect"]);
      const decision = cardView({ ...state, decision: { summary: "Argon is unavailable.", opus_available: true } });
      expect(decision.actions).toEqual([{ id: "respond", label: "Explain decision", primary: true }]);
    }
    expect(cardView(snapshot()).actions.map(action => action.id)).toContain("cancel");
    const connected = cardView(snapshot({ delegation_id: undefined }));
    expect(connected.detail).toBe("Describe a task in chat to delegate work to your connected computer.");
    expect(connected.actions.map(action => action.id)).toEqual(["connect"]);
  });

  it("formats timestamps in the host locale and calendar day without false epoch freshness", () => {
    const now = new Date("2026-10-16T00:30:00Z");
    const at = "2026-10-15T23:45:00Z";
    const local = formatUpdatedAt(at, { locale: "en-US", timeZone: "America/New_York" }, now)!;
    expect(local.text).toBe(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }).format(new Date(at)));
    const old = formatUpdatedAt(at, { locale: "en-US", timeZone: "UTC" }, now)!;
    expect(old.text).toContain("Oct 15");
    expect(old.iso).toBe("2026-10-15T23:45:00.000Z");
    expect(old.full).toContain("2026");
    const finnish = formatUpdatedAt(at, { locale: "fi-FI", timeZone: "Europe/Helsinki" }, now)!;
    expect(finnish.text).toBe(new Intl.DateTimeFormat("fi-FI", { timeZone: "Europe/Helsinki", hour: "numeric", minute: "2-digit" }).format(new Date(at)));
    expect(formatUpdatedAt("2025-10-15T23:45:00Z", { locale: "en-US", timeZone: "UTC" }, now)?.text).toContain("2025");
    expect(formatUpdatedAt("1970-01-01T00:00:00Z", {}, now)).toBeUndefined();
    expect(formatUpdatedAt("invalid", {}, now)).toBeUndefined();
    expect(formatUpdatedAt(at, { locale: "not_a_locale", timeZone: "invalid" }, now)?.text).toBeTruthy();
  });

  it("allows host-mediated file transfer only, regardless of an available browser URL", () => {
    const artifact = { artifact_id: "file-1", name: "answer.txt", export_available: true, download_available: true, download_url: "https://relay.example.test/downloads/task/file" };
    expect(downloadRoute(artifact, true)).toBe("native");
    expect(downloadRoute(artifact, false)).toBe("unavailable");
    expect(downloadRoute({ ...artifact, export_available: false, bytes: 20 * 1024 * 1024 }, true)).toBe("native");
    expect(downloadRoute({ ...artifact, export_available: false, bytes: 20 * 1024 * 1024 + 1 }, true)).toBe("unavailable");
    expect(downloadRoute({ ...artifact, download_url: "javascript:alert(1)" }, false)).toBe("unavailable");
    expect(downloadRoute({ ...artifact, download_available: false, export_available: false }, true)).toBe("unavailable");
  });

  it("keeps the complete authoritative outcome alongside a readable Unicode-safe preview", () => {
    const full = "Updated the approved files. " + "Evidence and limitations remain available. ".repeat(30);
    const presentation = outcomePresentation(full);
    expect(presentation.full).toBe(full);
    expect(Array.from(presentation.preview).length).toBeLessThanOrEqual(241);
    expect(presentation.preview).toMatch(/\S…$/);
    expect(full.startsWith(presentation.preview.slice(0, -1))).toBe(true);
    expect(outcomePresentation("A short complete result.")).toEqual({ preview: "A short complete result." });
    const unicode = outcomePresentation("🧪".repeat(300));
    expect(unicode.preview).toBe("🧪".repeat(240) + "…");
    expect(unicode.full).toBe("🧪".repeat(300));
  });

  it("allows only credential-free HTTPS links, keeping custom protocols outside ChatGPT", () => {
    expect(httpsUrl("https://relay.example.test/connect?request_id=task")).toBe("https://relay.example.test/connect?request_id=task");
    for (const url of ["javascript:alert(1)", "http://127.0.0.1/setup", "astra-antigravity://connect", "https://secret:token@relay.example.test/connect"]) expect(httpsUrl(url)).toBeUndefined();
  });

  it("verifies native download bytes at the export limit and rejects mismatches", async () => {
    const content = "x".repeat(12_000), bytes = new TextEncoder().encode(content);
    const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), value => value.toString(16).padStart(2, "0")).join("");
    const artifact = { artifact_id: "file-1", name: "answer.txt", bytes: bytes.length, sha256 };
    const meta = { _meta: { astra_artifact: { id: "file-1", bytes: bytes.length, sha256, encoding: "base64", content: btoa(content) } } };
    expect((await verifiedArtifact(meta, artifact))?.bytes).toBe(12_000);
    expect(await verifiedArtifact(meta, { ...artifact, artifact_id: "another-file" })).toBeUndefined();
    expect(await verifiedArtifact(meta, { ...artifact, sha256: "0".repeat(64) })).toBeUndefined();
    expect(await verifiedArtifact({ _meta: { astra_artifact: { ...meta._meta.astra_artifact, bytes: 12_001 } } }, artifact)).toBeUndefined();
    expect(await verifiedArtifact({ _meta: { astra_artifact: { ...meta._meta.astra_artifact, content: "invalid!" } } }, artifact)).toBeUndefined();
    expect(await verifiedArtifact({ _meta: { astra_artifact: null, status: "transfer_requested" } }, artifact)).toBeUndefined();
  });
});
