import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

test.beforeEach(async ({ context }) => {
  await context.route("**/*", route => {
    const target = new URL(route.request().url());
    return ["127.0.0.1", "localhost"].includes(target.hostname) || target.protocol === "data:" ? route.continue() : route.abort("blockedbyclient");
  });
});
async function open(page: Page, scenario = "computers", theme = "light") {
  await page.goto(`/gallery/control/${scenario}?theme=${theme}`);
  await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  const panel = page.frameLocator("#card");
  await expect(panel.getByRole("heading", { name: "Delegate to Antigravity" })).toBeVisible();
  return panel;
}
async function calls(page: Page, name: string) {
  return page.evaluate(name => (window as unknown as { galleryCalls: { method: string; params?: { name?: string; arguments?: Record<string, unknown> } }[] }).galleryCalls.filter(call => call.params?.name === name), name);
}
async function enableChatGptUpload(page: Page) {
  await page.addInitScript(() => {
    type Upload = { name: string; bytes: number; mimeType: string; library: boolean };
    const top = window.parent as unknown as { galleryUploads: Upload[] };
    if (window === window.parent) { top.galleryUploads = []; return; }
    Object.defineProperty(window, "openai", { configurable: true, value: { uploadFile: async (file: File, options: { library: true }) => {
      top.galleryUploads.push({ name: file.name, bytes: file.size, mimeType: file.type, library: options.library });
      return { fileId: `synthetic-private-file-${top.galleryUploads.length}` };
    } } });
  });
  // The synthetic frame permits only this fake relay origin. Its one-use GET
  // is fulfilled locally below; all other external requests remain blocked.
  await page.route("**/gallery/card/frame", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(), "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src https://relay.example.test; frame-ancestors 'self'" } });
  });
  await page.route("https://relay.example.test/artifact-download/*", route => route.fulfill({ status: 200, headers: { "Access-Control-Allow-Origin": "http://127.0.0.1:8790", "Content-Type": "text/plain", "Content-Length": "2" }, body: "42" }));
}

for (const [width, theme] of [[320, "light"], [390, "dark"], [1280, "light"]] as const) {
  test(`control views remain accessible at ${width}px in ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 1000 }); await page.emulateMedia({ colorScheme: theme });
    const panel = await open(page, "computers", theme);
    for (const view of ["Tasks", "Computers", "Settings"]) {
      await panel.getByRole("button", { name: view, exact: true }).click();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect(await page.frames()[1].evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      expect((await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze()).violations).toEqual([]);
    }
    if (width === 390) await page.screenshot({ path: info.outputPath("control-settings-mobile-dark.png"), fullPage: true });
  });
}

test("machine changes require review and a separate confirmation, with truthful offline receipts", async ({ page }) => {
  const panel = await open(page); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  const laptop = panel.locator('[data-computer-id="laptop"]');
  await expect(laptop).toContainText("Offline"); await expect(laptop).toContainText("0 active · 1 pending");
  await laptop.getByRole("button", { name: "Pause new tasks", exact: true }).click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toBeVisible();
  expect(await calls(page, "prepare_operation")).toHaveLength(1);
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
  await panel.getByRole("button", { name: "Keep current settings" }).click();
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
  await laptop.getByRole("button", { name: "Pause new tasks", exact: true }).click();
  await panel.getByRole("button", { name: "Confirm change", exact: true }).click();
  await expect(panel.locator(".panel-receipts")).toContainText("Pending on computer");
  await expect(panel.locator(".panel-receipts")).toContainText("Waiting for Research laptop to reconnect and confirm");
  await expect.poll(async () => (await calls(page, "confirm_operation")).length).toBe(1);
});

test("a saved task cancelled before computer setup does not block the control panel", async ({ page }) => {
  const panel = await open(page, "cancelled-before-setup");
  await expect(panel.getByRole("button", { name: "Computers", exact: true })).toHaveAttribute("aria-current", "page");
  await panel.getByRole("button", { name: "Tasks", exact: true }).click();
  await expect(panel.getByRole("heading", { name: "Cancelled saved task", exact: true })).toBeVisible();
  await expect(panel.locator(".panel-item")).toContainText("cancelled");
  await expect(panel.getByRole("heading", { name: "Task status unavailable", exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "Computers", exact: true }).click();
  await expect(panel.getByText("No computers are connected yet.", { exact: true })).toBeVisible();
  await expect(panel.getByLabel("Pairing code", { exact: true })).toBeVisible();
  expect(await calls(page, "prepare_operation")).toHaveLength(0);
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
});

test("the computer entrypoint opens Computers and refresh preserves the selected view", async ({ page }) => {
  const panel = await open(page);
  await expect(panel.getByRole("button", { name: "Computers", exact: true })).toHaveAttribute("aria-current", "page");
  await panel.getByRole("button", { name: "Tasks", exact: true }).click();
  await panel.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(async () => (await calls(page, "watch_computers_ui")).length).toBeGreaterThan(0);
  await expect(panel.getByRole("button", { name: "Tasks", exact: true })).toHaveAttribute("aria-current", "page");
});

test("lost change responses offer status inspection without resending the change", async ({ page }) => {
  const panel = await open(page); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  await panel.locator('[data-computer-id="desktop"]').getByRole("button", { name: "Pause new tasks" }).click();
  await page.evaluate(() => { (window as unknown as { galleryConfirmFailure: boolean }).galleryConfirmFailure = true; });
  await panel.getByRole("button", { name: "Confirm change", exact: true }).click();
  await expect(panel.locator(".panel-receipts")).toContainText("Outcome not confirmed");
  await panel.locator(".panel-receipt").filter({ hasText: "Outcome not confirmed" }).getByRole("button", { name: "Check operation" }).click();
  expect(await calls(page, "confirm_operation")).toHaveLength(1);
  await expect.poll(async () => (await calls(page, "inspect_operation")).length).toBe(1);
});

test("view-only and legacy computers expose no unsupported machine controls", async ({ page }) => {
  const panel = await open(page, "view-only"); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  await expect(panel.locator("article")).toContainText("View only");
  await expect(panel.getByRole("button", { name: "Pause new tasks" })).toHaveCount(0);
  const legacy = panel.locator('[data-computer-id="previous"]');
  await expect(legacy).toContainText("Legacy approval");
  await expect(legacy.getByRole("button", { name: "Keep computer approved" })).toHaveCount(0);
  expect(await calls(page, "prepare_operation")).toHaveLength(0);
});

test("names survive refresh, preference changes are confirmed, and deletion has two deliberate steps", async ({ page }) => {
  const panel = await open(page); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  const computer = panel.locator('[data-computer-id="desktop"]');
  await computer.getByText("Computer settings", { exact: true }).click();
  await computer.getByLabel("Computer name").fill("My named computer");
  await panel.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(computer.getByLabel("Computer name")).toHaveValue("My named computer");
  await panel.getByRole("button", { name: "Settings", exact: true }).click();
  await panel.getByLabel("Preferred workspace", { exact: true }).selectOption("research");
  await panel.getByRole("button", { name: "Review preference" }).click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toBeVisible();
  expect((await calls(page, "prepare_operation"))[0].params?.arguments?.parameters).toEqual({ workspace_id: "research" });
  await panel.getByRole("button", { name: "Keep current settings" }).click();
  const deletion = panel.getByRole("button", { name: "Review account deletion" }); await expect(deletion).toBeDisabled();
  await panel.getByRole("checkbox", { name: /^I understand deletion/ }).check(); await deletion.click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toContainText("Local files and provider accounts remain");
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
  await expect(panel.locator(".panel-code")).toHaveCount(0);
});

test("account export uses a hidden native file payload without exposing it in chat", async ({ page }) => {
  const panel = await open(page); await panel.getByRole("button", { name: "Settings", exact: true }).click();
  await panel.getByRole("button", { name: "Export account records", exact: true }).click();
  await expect(panel.locator(".feedback")).toContainText("Download requested");
  expect(await calls(page, "export_account_ui")).toHaveLength(1);
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.filter(call => call.method === "ui/download-file").length)).toBe(1);
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => ["ui/message", "ui/update-model-context"].includes(call.method)))).toBe(false);
});

test("confirmed account deletion clears stale records and stops private reads after authorization revocation", async ({ page }) => {
  const panel = await open(page); await panel.getByRole("button", { name: "Settings", exact: true }).click();
  await panel.getByRole("checkbox", { name: /^I understand deletion/ }).check(); await panel.getByRole("button", { name: "Review account deletion" }).click();
  await panel.getByRole("button", { name: "Confirm change", exact: true }).click();
  await expect(panel.locator(".panel-receipts")).toContainText("Completed");
  const count = (await calls(page, "watch_computers_ui")).length;
  await panel.getByRole("button", { name: "Computers", exact: true }).click();
  await expect(panel.locator("[data-computer-id]")).toHaveCount(0);
  await panel.getByRole("button", { name: "Refresh", exact: true }).click();
  expect(await calls(page, "watch_computers_ui")).toHaveLength(count);
  expect(await calls(page, "confirm_operation")).toHaveLength(1);
});

test("pending files retry inside the card and larger files request a native resource link", async ({ page }) => {
  await page.goto("/gallery/card/download"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  const card = page.frameLocator("#card");
  await page.evaluate(() => { (window as unknown as { galleryDownloadPending: boolean }).galleryDownloadPending = true; });
  await card.getByRole("button", { name: "Download answer.txt", exact: true }).click();
  await expect(card.locator(".feedback")).toContainText("choose Retry download here");
  await expect(card.getByRole("button", { name: "Download answer.txt", exact: true })).toHaveText("Retry download");
  await card.getByRole("button", { name: "Download answer.txt", exact: true }).click();
  await expect(card.locator(".feedback")).toContainText("Download requested");
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => call.method === "ui/open-link"))).toBe(false);
  await page.goto("/gallery/card/completed"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  await card.getByRole("button", { name: "Download Quarterly report.pdf", exact: true }).click();
  const request = await page.evaluate(() => (window as unknown as { galleryCalls: { method: string; params: { contents?: { type: string; size?: number }[] } }[] }).galleryCalls.find(call => call.method === "ui/download-file"));
  expect(request?.params.contents?.[0]).toMatchObject({ type: "resource_link", size: 248_320 });
  await expect(card.locator("body")).not.toContainText("grant=synthetic");
});

test("unsupported hosts keep saved results inline and never open browser downloads", async ({ page }) => {
  await page.goto("/gallery/card/download?nativeDownloads=false");
  await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  const card = page.frameLocator("#card");
  await expect(card.getByRole("heading", { name: "Files", exact: true })).toBeVisible();
  await expect(card.locator(".files")).toContainText("answer.txt");
  await expect(card.locator(".files")).toContainText("File transfer is unavailable in this ChatGPT host");
  await expect(card.getByRole("button", { name: "Download answer.txt", exact: true })).toHaveCount(0);
  await expect(card.getByRole("button", { name: /Open download/ })).toHaveCount(0);
  await card.getByRole("button", { name: "Computer", exact: true }).click();
  await expect(card.getByRole("button", { name: "Download Windows companion", exact: true })).toHaveCount(0);
  await card.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(card.getByRole("button", { name: "Export account records", exact: true })).toBeDisabled();
  await expect(card.locator("body")).toContainText("Your account records remain saved");
  expect(await calls(page, "prepare_artifact_download_ui")).toHaveLength(0);
  expect(await calls(page, "export_account_ui")).toHaveLength(0);
  expect(await calls(page, "delegate_via_luna")).toHaveLength(0);
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => ["ui/open-link", "ui/download-file", "ui/message"].includes(call.method)))).toBe(false);
});

test("a refused native download stays inline and can retry without a browser escape", async ({ page }) => {
  await enableChatGptUpload(page);
  await page.goto("/gallery/card/download"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  const card = page.frameLocator("#card");
  await page.evaluate(() => { (window as unknown as { galleryDownloadDenied: boolean }).galleryDownloadDenied = true; });
  await card.getByRole("button", { name: "Download answer.txt", exact: true }).click();
  await expect(card.locator(".feedback")).toContainText("The download was not approved");
  expect(await page.evaluate(() => (window as unknown as { galleryUploads: unknown[] }).galleryUploads)).toEqual([]);
  await expect(card.getByRole("button", { name: /Open download/ })).toHaveCount(0);
  await page.evaluate(() => { (window as unknown as { galleryDownloadDenied: boolean }).galleryDownloadDenied = false; });
  await card.getByRole("button", { name: "Download answer.txt", exact: true }).click();
  await expect(card.locator(".feedback")).toContainText("Download requested");
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => call.method === "ui/open-link"))).toBe(false);
  expect(await calls(page, "prepare_artifact_download_ui")).toHaveLength(2);
});

test("the upload-capable host receives verified files and account exports without browser navigation", async ({ page }) => {
  await enableChatGptUpload(page);
  await page.goto("/gallery/card/download?nativeDownloads=false"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  const card = page.frameLocator("#card");
  await card.getByRole("button", { name: "Add to ChatGPT: answer.txt", exact: true }).click();
  await expect(card.locator(".feedback")).toContainText("File sent to ChatGPT");
  expect(await page.evaluate(() => (window as unknown as { galleryUploads: unknown[] }).galleryUploads)).toEqual([{ name: "answer.txt", bytes: 2, mimeType: "text/plain", library: true }]);
  await expect(card.locator("body")).not.toContainText("synthetic-private-file");
  await card.getByRole("button", { name: "Computer", exact: true }).click();
  await card.getByRole("button", { name: "Settings", exact: true }).click();
  await card.getByRole("button", { name: "Add account records to ChatGPT", exact: true }).click();
  await expect(card.locator(".feedback")).toContainText("File sent to ChatGPT");
  expect(await page.evaluate(() => (window as unknown as { galleryUploads: { name: string; mimeType: string; library: boolean }[] }).galleryUploads[1])).toMatchObject({ name: "account-records.json", mimeType: "application/json", library: true });
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => ["ui/open-link", "ui/download-file", "ui/message", "ui/update-model-context"].includes(call.method)))).toBe(false);
});

test("computer setup shows only a configured command and ignores installer download URLs", async ({ page }) => {
  const panel = await open(page);
  await expect(panel.getByRole("button", { name: "Download Windows companion", exact: true })).toHaveCount(0);
  await expect(panel.locator(".panel-code")).toHaveCount(0);
  await expect(panel.locator("body")).toContainText("A verified public setup release is not available");
  await open(page, "setup-command");
  await expect(panel.locator(".panel-code")).toHaveText("npx --yes https://github.com/ramiradwan/astra-antigravity/releases/download/companion-v0.4.1/delegate-to-antigravity-0.4.1.tgz setup");
  await expect(panel.getByRole("button", { name: "Download Windows companion", exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => call.method === "ui/open-link"))).toBe(false);
});

test("the task Computer button opens controls and keyboard focus stays usable", async ({ page }) => {
  await page.goto("/gallery/card/running"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  const card = page.frameLocator("#card"); await card.getByRole("button", { name: "Computer", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(card.getByRole("heading", { name: "Delegate to Antigravity" })).toBeVisible();
  await expect(card.getByRole("button", { name: "Computers", exact: true })).toHaveAttribute("aria-current", "page");
  await card.getByRole("button", { name: "Settings", exact: true }).focus(); await page.keyboard.press("Enter");
  await expect(card.getByRole("button", { name: "Settings", exact: true })).toBeFocused();
  await expect(card.getByRole("button", { name: "Settings", exact: true })).toHaveAttribute("aria-current", "page");
});

test("control views tolerate doubled text and forced colors", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.emulateMedia({ forcedColors: "active" });
  const panel = await open(page);
  await page.frames()[1].evaluate(() => {
    document.documentElement.style.fontSize = "200%";
    document.documentElement.style.setProperty("--font-text-md-size", "1rem");
    document.documentElement.style.setProperty("--font-text-sm-size", ".875rem");
  });
  for (const view of ["Tasks", "Computers", "Settings"]) {
    await panel.getByRole("button", { name: view, exact: true }).click();
    expect(await page.frames()[1].evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(panel.getByRole("button", { name: view, exact: true })).toHaveAttribute("aria-current", "page");
  }
});

test("headless pairing is explicit, preserves the saved request and opens no setup dashboard", async ({ page }) => {
  await page.goto("/gallery/card/setup"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  const panel = page.frameLocator("#card"); await panel.getByRole("button", { name: "Connect your computer", exact: true }).click();
  await panel.getByLabel("Pairing code", { exact: true }).fill("a".repeat(24));
  await expect(panel.getByRole("button", { name: "Approve computer", exact: true })).toBeDisabled();
  await panel.getByRole("checkbox", { name: /^Approve this computer for account setup/ }).check();
  await panel.getByRole("button", { name: "Approve computer", exact: true }).click();
  await expect(panel.locator(".panel-receipts")).toContainText("No workspace or task was approved");
  expect((await calls(page, "approve_pairing_ui"))[0].params?.arguments).toEqual({ code: "a".repeat(24), approval_mode: "until_revoked", request_id: "original-request" });
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => call.method === "ui/open-link"))).toBe(false);
  await expect(panel.getByRole("button", { name: "Open computer setup" })).toHaveCount(0);
});

test("provider setup and private workspace access require separate reviewed operations", async ({ page }) => {
  const panel = await open(page, "onboarding"); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  const computer = panel.locator('[data-computer-id="desktop"]');
  await expect(computer).toContainText("Delegated tasks cannot start until a workspace is approved");
  await expect(computer).toContainText("Codex · Not installed"); await expect(computer).toContainText("Antigravity · Sign-in needed");
  await computer.getByRole("button", { name: "Install Codex", exact: true }).click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toBeVisible();
  expect((await calls(page, "prepare_operation"))[0].params?.arguments).toMatchObject({ action: "install_provider", computer_id: "desktop", parameters: { provider: "codex" } });
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
  await panel.getByRole("button", { name: "Keep current settings" }).click();
  await computer.getByRole("button", { name: "Sign in to Antigravity", exact: true }).click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toBeVisible();
  expect((await calls(page, "prepare_operation"))[1].params?.arguments).toMatchObject({ action: "sign_in", parameters: { provider: "agy" } });
  await panel.getByRole("button", { name: "Keep current settings" }).click();
  await computer.getByLabel("Workspace folder on this computer", { exact: true }).fill("C:\\Projects\\private-report");
  await computer.getByLabel("Workspace name", { exact: true }).fill("private-report");
  await computer.getByLabel("Task isolation", { exact: true }).selectOption("shared");
  await expect(computer.getByRole("button", { name: "Review workspace access" })).toBeDisabled();
  await computer.getByRole("checkbox", { name: /^Allow authorized tasks in this workspace/ }).check();
  await computer.getByRole("button", { name: "Review workspace access" }).click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toBeVisible();
  expect((await calls(page, "prepare_operation"))[2].params?.arguments).toMatchObject({ action: "configure_workspace", parameters: { path: "C:\\Projects\\private-report", alias: "private-report", isolation: "shared" } });
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => ["ui/message", "ui/update-model-context"].includes(call.method)))).toBe(false);
});

test("a fresh host invocation clears old confirmations and private form drafts", async ({ page }) => {
  const panel = await open(page); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  await panel.getByLabel("Pairing code", { exact: true }).fill("b".repeat(24));
  await panel.getByRole("checkbox", { name: /^Approve this computer for account setup/ }).check();
  const computer = panel.locator('[data-computer-id="desktop"]');
  await computer.getByText("Computer settings", { exact: true }).click(); await computer.getByLabel("Computer name", { exact: true }).fill("Unsubmitted name");
  await computer.getByRole("button", { name: "Pause new tasks", exact: true }).click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toBeVisible();
  await page.evaluate(() => { (window as unknown as { galleryFreshControl(): void }).galleryFreshControl(); });
  await expect(panel.getByRole("region", { name: "Confirm change" })).toHaveCount(0);
  await expect(panel.getByLabel("Pairing code", { exact: true })).toHaveValue("");
  await expect(panel.getByRole("checkbox", { name: /^Approve this computer for account setup/ })).not.toBeChecked();
  await computer.getByText("Computer settings", { exact: true }).click();
  await expect(computer.getByLabel("Computer name", { exact: true })).toHaveValue("Office computer");
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
});

test("late operation replies from an earlier invocation do not restore its confirmation", async ({ page }) => {
  const panel = await open(page); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  await page.evaluate(() => { (window as unknown as { galleryDelayPrepare: boolean }).galleryDelayPrepare = true; });
  await panel.locator('[data-computer-id="desktop"]').getByRole("button", { name: "Pause new tasks", exact: true }).click();
  await expect.poll(async () => (await calls(page, "prepare_operation")).length).toBe(1);
  await page.evaluate(() => { (window as unknown as { galleryFreshControl(): void }).galleryFreshControl(); });
  await expect(panel.getByRole("button", { name: "Computers", exact: true })).not.toHaveAttribute("aria-disabled", "true");
  await page.evaluate(() => { (window as unknown as { galleryDeliverPrepare(): void }).galleryDeliverPrepare(); });
  await expect(panel.getByRole("region", { name: "Confirm change" })).toHaveCount(0);
  await expect(panel.locator(".feedback")).toHaveCount(0);
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
});

test("mobile visual evidence covers computers, setup permission and native downloads", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 1000 }); await page.emulateMedia({ colorScheme: "dark" });
  const panel = await open(page, "computers", "dark"); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  await panel.locator('[data-computer-id="laptop"]').screenshot({ path: info.outputPath("computer-offline-mobile-dark.png"), animations: "disabled" });
  await open(page, "onboarding", "dark"); await panel.getByRole("button", { name: "Computers", exact: true }).click();
  const computer = panel.locator('[data-computer-id="desktop"]');
  await computer.getByLabel("Workspace folder on this computer", { exact: true }).fill("C:\\Projects\\quarterly-report");
  await computer.getByLabel("Workspace name", { exact: true }).fill("quarterly-report");
  await expect(computer.getByRole("button", { name: "Review workspace access" })).toBeDisabled();
  await computer.screenshot({ path: info.outputPath("computer-setup-permission-mobile-dark.png"), animations: "disabled" });
  await computer.getByRole("checkbox", { name: /^Allow authorized tasks in this workspace/ }).check();
  await computer.getByRole("button", { name: "Review workspace access" }).click();
  await expect(panel.getByRole("region", { name: "Confirm change" })).toContainText("C:\\Projects\\quarterly-report");
  await panel.getByRole("region", { name: "Confirm change" }).screenshot({ path: info.outputPath("workspace-confirmation-mobile-dark.png"), animations: "disabled" });
  await page.goto("/gallery/card/completed?theme=dark"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  await panel.getByRole("button", { name: "Download Quarterly report.pdf", exact: true }).click();
  await expect(panel.locator(".feedback")).toContainText("Download requested. Check your downloads.");
  await panel.locator("article").screenshot({ path: info.outputPath("task-native-download-mobile-dark.png"), animations: "disabled" });
  await page.goto("/gallery/card/download?theme=dark"); await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
  await page.evaluate(() => { (window as unknown as { galleryDownloadPending: boolean }).galleryDownloadPending = true; });
  await panel.getByRole("button", { name: "Download answer.txt", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Download answer.txt", exact: true })).toHaveText("Retry download");
  await panel.locator("article").screenshot({ path: info.outputPath("task-pending-download-mobile-dark.png"), animations: "disabled" });
});

test("expired removal confirmation remains unconfirmed and points to separate computer controls", async ({ page }) => {
  const panel = await open(page, "removal-unconfirmed");
  const receipt = panel.locator(".panel-receipt");
  await expect(receipt).toContainText("Uninstall companion · Completion unconfirmed");
  await expect(receipt).toContainText("may have been removed locally; its completion was not confirmed");
  await expect(receipt).not.toContainText("Completed");
  await expect(receipt.getByRole("button", { name: "Check operation" })).toHaveCount(0);
  await receipt.getByRole("button", { name: "Computer controls", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Computers", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(panel.locator('[data-computer-id="laptop"]')).toBeFocused();
  expect(await calls(page, "confirm_operation")).toHaveLength(0);
});
