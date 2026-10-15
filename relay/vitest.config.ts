import { defineConfig } from "vitest/config";
import { cloudflareTest } from "@cloudflare/vitest-plugin";

export default defineConfig({
  plugins: [cloudflareTest({ wrangler: { configPath: "./wrangler.jsonc" }, remoteBindings: false,
    miniflare: { bindings: { PUBLIC_ORIGIN: "https://relay.example.test", DEPLOYMENT_MODE: "local",
      SESSION_SECRET: "synthetic-local-test-key-with-at-least-32-bytes", GITHUB_CLIENT_ID: "synthetic-id", GITHUB_CLIENT_SECRET: "synthetic-secret" } } })],
  test: { include: ["test/**/*.test.ts"], testTimeout: 15_000 }
});
