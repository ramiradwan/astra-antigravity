import type { UiArtifact, UiArtifactContent, UiSnapshot } from "../src/ui-contracts";

export interface UiBridge {
  callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
}
export interface WatchClock {
  schedule(callback: () => void, milliseconds: number): unknown;
  cancel(handle: unknown): void;
}
const clock: WatchClock = {
  schedule: (callback, milliseconds) => setTimeout(callback, milliseconds),
  cancel: handle => clearTimeout(handle as ReturnType<typeof setTimeout>),
};
const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));

/** Metadata is component-only; never copy the result into model context. */
export function snapshotFrom(result: unknown): UiSnapshot | undefined {
  if (!record(result) || !record(result._meta)) return;
  const value = result._meta.astra_ui;
  if (!record(value) || value.version !== 1 || typeof value.revision !== "string" ||
      typeof value.status !== "string" || typeof value.summary !== "string" ||
      typeof value.terminal !== "boolean" || typeof value.updated_at !== "string" ||
      !record(value.connection) || !["online", "offline", "expired", "revoked", "unknown", "setup_required"].includes(String(value.connection.state)) ||
      !record(value.notifications) || typeof value.notifications.active !== "boolean" ||
      (value.delegation_id !== undefined && (typeof value.delegation_id !== "string" || !/^[a-zA-Z0-9._-]{1,160}$/.test(value.delegation_id)))) return;
  if (value.permissions !== undefined && (!record(value.permissions) || typeof value.permissions.can_write_tasks !== "boolean")) return;
  if (value.request_id !== undefined && (typeof value.request_id !== "string" || !/^[a-zA-Z0-9_.-]{1,128}$/.test(value.request_id))) return;
  for (const key of ["task_summary", "workspace_label", "selected_model", "setup_url", "outcome"]) {
    if (value[key] !== undefined && typeof value[key] !== "string") return;
  }
  if (value.decision !== undefined && (!record(value.decision) || typeof value.decision.summary !== "string" ||
      (value.decision.opus_available !== undefined && typeof value.decision.opus_available !== "boolean"))) return;
  if (value.worker_checks !== undefined && (!Array.isArray(value.worker_checks) || !value.worker_checks.every(item => typeof item === "string"))) return;
  if (value.artifacts !== undefined && (!Array.isArray(value.artifacts) || !value.artifacts.every(item => record(item) && typeof item.name === "string" &&
      (item.artifact_id === undefined || typeof item.artifact_id === "string") && (item.bytes === undefined || (typeof item.bytes === "number" && Number.isInteger(item.bytes) && item.bytes >= 0))))) return;
  return value as unknown as UiSnapshot;
}

export function httpsUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return;
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && !url.username && !url.password) return url.href;
  } catch { /* An absent action is safer than an invalid external destination. */ }
}

/** Presentation only: locale/time-zone changes never trigger another server read. */
export function formatUpdatedAt(value: string, context: { locale?: string; timeZone?: string } = {}, now = new Date()): { text: string; full: string; iso: string } | undefined {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime()) || date.getTime() <= 0) return;
  const format = (options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat => {
    try { return new Intl.DateTimeFormat(context.locale || undefined, { ...options, ...(context.timeZone ? { timeZone: context.timeZone } : {}) }); }
    catch { return new Intl.DateTimeFormat(undefined, options); }
  };
  const calendarDay = format({ year: "numeric", month: "2-digit", day: "2-digit" });
  const older = calendarDay.format(date) !== calendarDay.format(now);
  const calendarYear = format({ year: "numeric" });
  const differentYear = calendarYear.format(date) !== calendarYear.format(now);
  const text = format({ hour: "numeric", minute: "2-digit", ...(older ? { month: "short", day: "numeric", ...(differentYear ? { year: "numeric" } : {}) } : {}) }).format(date);
  return { text, full: format({ year: "numeric", month: "long", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }).format(date), iso: date.toISOString() };
}

export const canWriteTasks = (snapshot: UiSnapshot): boolean => snapshot.permissions?.can_write_tasks === true;

/** A readable preview accompanies, rather than replaces, the full result. */
export function outcomePresentation(text: string, limit = 240): { preview: string; full?: string } {
  const characters = Array.from(text);
  if (characters.length <= limit) return { preview: text };
  let boundary = limit;
  while (boundary > limit * 0.6 && !/\s/.test(characters[boundary])) boundary--;
  if (boundary <= limit * 0.6) boundary = limit;
  return { preview: characters.slice(0, boundary).join("").trimEnd() + "…", full: text };
}

export function downloadRoute(artifact: UiArtifact, nativeSupported: boolean): "native" | "unavailable" {
  return (artifact.download_available || artifact.export_available) && (artifact.bytes === undefined || artifact.bytes <= 20 * 1024 * 1024) && nativeSupported ? "native" : "unavailable";
}

export async function verifiedArtifact(result: unknown, artifact: UiArtifact): Promise<UiArtifactContent | undefined> {
  if (!record(result) || !record(result._meta)) return;
  const value = result._meta.astra_artifact;
  if (!record(value) || value.encoding !== "base64" || typeof value.bytes !== "number" || !Number.isInteger(value.bytes) ||
      value.bytes < 0 || value.bytes > 12_000 || value.id !== artifact.artifact_id ||
      typeof value.content !== "string" || value.content.length > 16_000 || typeof value.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(value.sha256)) return;
  try {
    const bytes = Uint8Array.from(atob(value.content), character => character.charCodeAt(0));
    const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
    if (bytes.length !== value.bytes || (artifact.bytes !== undefined && artifact.bytes !== bytes.length) || digest !== value.sha256 ||
        (artifact.sha256 && digest !== artifact.sha256)) return;
    return value as unknown as UiArtifactContent;
  } catch { return; }
}

/** A single foreground read loop. It cannot delegate, acknowledge or message chat. */
export class SnapshotWatch {
  current?: UiSnapshot;
  error = "";
  private visible = true;
  private disposed = false;
  private inFlight = false;
  private retry = 0;
  private timer?: unknown;
  private refreshRequested = false;
  private generation = 0;

  constructor(private bridge: UiBridge, private changed: () => void, private timers: WatchClock = clock) {}

  accept(result: unknown): boolean {
    const snapshot = snapshotFrom(result);
    if (!snapshot) return false;
    this.generation++;
    this.current = snapshot;
    this.error = "";
    this.retry = 0;
    this.changed();
    this.plan(0);
    return true;
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    this.clearTimer();
    if (visible) this.plan(0);
  }

  refresh(): void {
    if (this.disposed || !this.visible) return;
    if (this.inFlight) { this.refreshRequested = true; return; }
    this.clearTimer();
    void this.read(0);
  }

  dispose(): void { this.disposed = true; this.generation++; this.clearTimer(); }

  private clearTimer(): void {
    if (this.timer !== undefined) this.timers.cancel(this.timer);
    this.timer = undefined;
  }
  private plan(delay: number): void {
    this.clearTimer();
    if (this.disposed || !this.visible || this.inFlight || !this.current || this.current.terminal) return;
    // Connection-only cards have no task transition to wait on.
    this.timer = this.timers.schedule(() => { this.timer = undefined; void this.read(this.current?.delegation_id ? 20_000 : 0); },
      this.current.delegation_id ? delay : Math.max(delay, 10_000));
  }
  private async read(wait: 0 | 20_000): Promise<void> {
    if (this.disposed || !this.visible || this.inFlight || !this.current) return;
    this.inFlight = true;
    this.refreshRequested = false;
    const generation = this.generation;
    const previous = this.current;
    let delay = 1_000;
    try {
      const result = await this.bridge.callTool("watch_delegation_ui", {
        ...(previous.delegation_id ? { delegation_id: previous.delegation_id } : {}),
        after_revision: previous.revision, wait_ms: wait,
      });
      if (this.disposed || generation !== this.generation) return;
      const next = snapshotFrom(result);
      if (!next || next.delegation_id !== previous.delegation_id) throw new Error("Unexpected task update");
      this.current = next;
      this.error = "";
      this.retry = 0;
      this.changed();
    } catch {
      if (!this.disposed && generation === this.generation) {
        this.error = "Updates are temporarily unavailable. Your task has not been restarted.";
        delay = Math.min(30_000, 2_000 * 2 ** Math.min(this.retry++, 4));
        this.changed();
      }
    } finally {
      this.inFlight = false;
      if (!this.disposed && this.refreshRequested && this.visible) this.refresh();
      else this.plan(delay);
    }
  }
}

export interface CardAction { id: "connect" | "refresh" | "cancel" | "respond" | "opus" | "review"; label: string; primary?: boolean }
export type StatusIcon = "check" | "clock" | "activity" | "alert" | "close" | "computer";
export interface CardStatus { icon: StatusIcon; label: string; note?: string }

/** Task identity stays stable while its status and latest update change. */
export function taskHeading(snapshot: UiSnapshot): string {
  return snapshot.delegation_id ? snapshot.task_summary?.trim() || "Antigravity task" : "Your computer";
}

export function cardStatus(snapshot: UiSnapshot, title: string): CardStatus {
  if (["expired", "revoked"].includes(snapshot.connection.state)) return { icon: "alert", label: title };
  if (snapshot.decision && (!snapshot.terminal || typeof snapshot.decision.opus_available === "boolean")) return { icon: "alert", label: title };
  if (snapshot.terminal) return snapshot.status === "completed"
    ? { icon: "check", label: title, note: "Review pending" }
    : { icon: snapshot.status === "cancelled" ? "close" : "alert", label: title };
  if (!snapshot.delegation_id && snapshot.connection.state === "online") return { icon: "check", label: "Computer connected" };
  if (["offline", "unknown"].includes(snapshot.connection.state) ||
      ["unknown", "recovery_required"].includes(snapshot.status)) return { icon: "alert", label: title };
  if (snapshot.status === "queued") return { icon: "clock", label: title };
  if (snapshot.status === "awaiting_setup" || snapshot.connection.state === "setup_required") return { icon: "computer", label: "Connection needed" };
  return { icon: "activity", label: title };
}

export function cardView(snapshot: UiSnapshot): { title: string; detail: string; actions: CardAction[]; tone: string } {
  const { status, connection, terminal } = snapshot;
  const recovery = () => {
    const title = connection.state === "expired" ? "Computer access expired" : connection.state === "revoked" ? "Computer access was revoked" : status === "unknown" || status === "recovery_required" ? "Task status unavailable" : connection.state === "unknown" ? "Connection needs attention" : "Computer is offline";
    return { title, detail: connection.message || (snapshot.delegation_id
      ? "Use Computer controls to restore the connection and check this task. It will not be started again automatically."
      : "Use Computer controls to check setup and restore your connection."), tone: "attention",
      actions: [{ id: "connect" as const, label: connection.state === "revoked" ? "Manage connection" : "Reconnect computer", primary: true }, { id: "refresh" as const, label: snapshot.delegation_id ? "Check status" : "Check connection" }] };
  };
  // Expired/revoked access must not invite review or display a stale result.
  if (["expired", "revoked"].includes(connection.state)) return recovery();
  if (snapshot.decision && (!terminal || typeof snapshot.decision.opus_available === "boolean")) return {
    title: "Your decision is needed", detail: snapshot.decision.summary, tone: "attention",
    actions: [{ id: "respond", label: canWriteTasks(snapshot) ? "Respond in chat" : "Explain decision", primary: true },
      ...(snapshot.decision.opus_available && canWriteTasks(snapshot) ? [{ id: "opus" as const, label: "Approve Opus 5.5" }] : [])],
  };
  if (!snapshot.delegation_id && connection.state === "online") return {
    title: canWriteTasks(snapshot) ? "Computer connected" : "Computer connected · view only",
    detail: canWriteTasks(snapshot) ? "Describe a task in chat to delegate work to your connected computer." : "This connection can view tasks. To delegate work, reconnect the plugin with task access.", tone: "success",
    actions: [{ id: "connect", label: "Manage connection" }],
  };
  if (terminal) return {
    title: status === "completed" ? "Work finished" : status === "cancelled" ? "Task cancelled" : "Task needs attention",
    detail: snapshot.outcome || snapshot.summary, tone: status === "completed" ? "success" : status === "cancelled" ? "neutral" : "danger",
    actions: [{ id: "review", label: status === "completed" ? "Review with ChatGPT" : "Review saved work", primary: true }],
  };
  if (["offline", "unknown"].includes(connection.state) || ["unknown", "recovery_required"].includes(status)) return recovery();
  if (status === "awaiting_setup" || connection.state === "setup_required" || !snapshot.delegation_id) return {
    title: "Connect your computer", detail: "Connect your accounts and approve a workspace in Computer controls. Provider sign-in opens on your computer." + (snapshot.delegation_id ? " Your saved task will continue here." : canWriteTasks(snapshot) ? " Then describe a task in chat." : " This connection can view status. Reconnect the plugin with task access before delegating."), tone: "neutral",
    actions: [{ id: "connect", label: "Connect your computer", primary: true }, { id: "refresh", label: "Check connection" }],
  };
  return {
    title: status === "cancelling" ? "Cancellation requested" : status === "queued" ? "Task queued" : "Task in progress",
    detail: status === "cancelling" ? "Cancellation was requested. Waiting for the worker to confirm." : snapshot.summary,
    tone: "neutral", actions: [
      ...(status === "cancelling" || !canWriteTasks(snapshot) ? [] : [{ id: "cancel" as const, label: "Cancel task" }]),
      { id: "refresh", label: "Refresh status" }],
  };
}
