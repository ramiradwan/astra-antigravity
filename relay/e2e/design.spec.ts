import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// These tests use the production renderers through the dev-only fixture entry.
// Outbound browser requests are rejected even if a preview action is clicked.
test.beforeEach(async ({ context }) => {
  await context.route("**/*", route => {
    const target = new URL(route.request().url());
    return ["127.0.0.1", "localhost"].includes(target.hostname) || target.protocol === "data:" ? route.continue() : route.abort("blockedbyclient");
  });
});
async function ready(page: Page, surface: string) {
  if (surface === "companion") await expect(page.locator("#notice")).not.toHaveText("Checking this computer…");
  if (surface === "card") await expect(page.locator("#gallery-status")).toHaveText("Preview connected");
}
async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  for (const frame of page.frames().slice(1)) expect(await frame.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}
async function accessible(page: Page) {
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  expect(result.violations).toEqual([]);
}

for (const colorScheme of ["light", "dark"] as const) {
  for (const width of [320, 390, 768, 1280, 1440]) {
    for (const [surface, path] of [["hosted", "/gallery/hosted/account"], ["companion", "/gallery/companion/connected"], ["card", "/gallery/card/completed"]]) {
      test(`${surface} is readable and accessible at ${width}px in ${colorScheme}`, async ({ page }, info) => {
        await page.setViewportSize({ width, height: 1000 });
        await page.emulateMedia({ colorScheme });
        await page.goto(path + (surface === "card" ? "?theme=" + colorScheme : ""));
        await ready(page, surface);
        await noOverflow(page);
        await accessible(page);
        if ((width === 1440 && colorScheme === "light") || (width === 390 && colorScheme === "dark")) {
          await page.screenshot({ path: info.outputPath(`${surface}-${width}-${colorScheme}.png`), fullPage: true, animations: "disabled" });
        }
      });
    }
  }
}

test("all hosted routes render their actual production states", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const state of ["home", "login", "authorize", "connect", "choose-workspace", "pair", "empty-account", "renew", "download", "download-offline", "error", "privacy", "terms", "license"]) {
    const response = await page.goto("/gallery/hosted/" + state);
    expect(response?.status()).toBe(state === "error" ? 400 : 200);
    await expect(page.locator("h1")).toBeVisible();
    await noOverflow(page);
    await accessible(page);
  }
});

test("all companion states remain readable without provider commands", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const state of ["accounts", "workspace", "approval", "reconnecting", "draining", "expired", "renewal", "revoked", "error"]) {
    await page.goto("/gallery/companion/" + state);
    await ready(page, "companion");
    await expect(page.locator("h1")).toBeVisible();
    await noOverflow(page);
    await accessible(page);
  }
});

test("all task card states use the real MCP bridge and keep actions bounded", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const state of ["setup", "connected", "view-only", "queued", "running", "cancelling", "decision", "decision-no-fallback", "offline", "expired", "revoked", "recovery", "failed", "cancelled", "long", "download"]) {
    await page.goto("/gallery/card/" + state);
    await ready(page, "card");
    const card = page.frameLocator("#card");
    await expect(card.locator(".task-status")).toBeVisible();
    expect(await card.locator(".actions .primary").count()).toBeLessThanOrEqual(1);
    expect(await card.locator(".actions button").count()).toBeLessThanOrEqual(2);
    for (const control of await card.locator("button, summary").all()) expect((await control.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    await noOverflow(page);
    await accessible(page);
  }
});

test("companion field errors survive status refresh and dismiss explicitly", async ({ page }) => {
  await page.goto("/gallery/companion/action-error");
  await ready(page, "companion");
  await page.locator("#browse").click();
  await expect(page.locator("#action-error")).toBeVisible();
  await expect(page.locator("#folder")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#folder")).toBeFocused();
  const statuses: string[] = [];
  page.on("request", request => { if (request.url().endsWith("/api/status")) statuses.push(request.url()); });
  await expect.poll(() => statuses.length, { timeout: 6_000 }).toBeGreaterThan(0);
  await expect(page.locator("#action-error")).toContainText("selected folder is unavailable");
  await page.locator("#dismiss-error").click();
  await expect(page.locator("#action-error")).toBeHidden();
  await expect(page.locator("#folder")).not.toHaveAttribute("aria-invalid", "true");
});

test("companion quit stops polling and preserves the quit feedback", async ({ page }) => {
  await page.goto("/gallery/companion/connected");
  await ready(page, "companion");
  const statuses: string[] = [];
  page.on("request", request => { if (request.url().endsWith("/api/status")) statuses.push(request.url()); });
  await page.locator("#quit").click();
  await expect(page.locator("#quitting")).toBeVisible();
  await expect(page.locator("#quitting-title")).toBeFocused();
  const count = statuses.length;
  await page.waitForTimeout(3_300);
  expect(statuses.length).toBe(count);
  await expect(page.locator("#notice")).toContainText("Quit requested");
});

test("card live completion preserves focus and stops its foreground watch", async ({ page }) => {
  await page.goto("/gallery/card/running");
  await ready(page, "card");
  const card = page.frameLocator("#card");
  await card.locator('[data-action="cancel"]').focus();
  await page.locator("#advance").evaluate((button: HTMLButtonElement) => button.click());
  await expect(card.locator(".task-status")).toContainText("Work finished");
  await expect(card.locator("h1")).toBeFocused();
  await expect(card.locator(".status-note")).toHaveText("Review pending");
  await expect(card.locator('[data-action="cancel"]')).toHaveCount(0);
  const watchCount = await page.evaluate(() => (window as unknown as { galleryCalls: { method: string; params?: { name?: string } }[] }).galleryCalls.filter(call => call.params?.name === "watch_delegation_ui").length);
  await page.waitForTimeout(1_200);
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { params?: { name?: string } }[] }).galleryCalls.filter(call => call.params?.name === "watch_delegation_ui").length)).toBe(watchCount);
});

test("card disclosure and native-download feedback remain truthful", async ({ page }) => {
  await page.goto("/gallery/card/completed");
  await ready(page, "card");
  const card = page.frameLocator("#card");
  await card.locator(".checks summary").click();
  await page.locator("#advance").evaluate((button: HTMLButtonElement) => button.click());
  await expect(card.locator(".checks")).toHaveAttribute("open", "");
  await expect(card.locator(".checks summary")).toBeFocused();
  await page.goto("/gallery/card/download");
  await ready(page, "card");
  await card.getByRole("button", { name: "Download answer.txt", exact: true }).click();
  await expect(card.locator(".feedback")).toContainText("Download requested. Check your downloads.");
  expect(await page.evaluate(() => (window as unknown as { galleryCalls: { method: string }[] }).galleryCalls.some(call => call.method === "ui/download-file"))).toBe(true);
  await expect(card.locator("body")).not.toContainText("Your file has been saved");
});

test("200 percent text size does not clip hosted, companion or embedded card content", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  for (const path of ["/gallery/hosted/pair", "/gallery/companion/workspace", "/gallery/card/long"]) {
    await page.goto(path);
    await ready(page, path.includes("card") ? "card" : path.includes("companion") ? "companion" : "hosted");
    for (const frame of page.frames()) await frame.evaluate(() => {
      const root = document.documentElement;
      // CSSOM represents a user/host text-size preference without injecting an
      // inline stylesheet or weakening the production nonce-based CSP.
      root.style.setProperty("font-size", "200%", "important");
      if (document.body.dataset.surface === "chatgpt") {
        root.style.setProperty("--font-text-md-size", "1rem");
        root.style.setProperty("--font-text-sm-size", ".875rem");
      }
    });
    await noOverflow(page);
  }
});
