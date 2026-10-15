import { defineConfig } from "@playwright/test";
import path from "node:path";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : 3,
  timeout: 30_000,
  expect: { timeout: 7_000 },
  reporter: [["line"], ["html", { open: "never" }]],
  outputDir: "test-results",
  use: {
    baseURL: "http://127.0.0.1:8790", browserName: "chromium",
    viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1,
    locale: "en-US", timezoneId: "UTC", reducedMotion: "reduce",
    serviceWorkers: "block", trace: "retain-on-failure", screenshot: "only-on-failure",
  },
  webServer: {
    command: "node dev/gallery-server.mjs",
    url: "http://127.0.0.1:8790/__gallery/health",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { PATH: path.dirname(process.execPath) + path.delimiter + (process.env.PATH || ""), WRANGLER_SEND_METRICS: "false" },
  },
});
