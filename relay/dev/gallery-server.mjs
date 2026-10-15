// Local design/testing only. This command cannot deploy or run provider tools.
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const relay = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
await import("../scripts/build-ui.mjs");
const child = spawn(process.execPath, [path.join(relay, "node_modules/wrangler/bin/wrangler.js"), "dev", "--config", "dev/wrangler.gallery.jsonc", "--ip", "127.0.0.1", "--port", "8790", "--local", "--persist-to", ".wrangler/ui-gallery"], {
  cwd: relay, stdio: "inherit", env: { ...process.env, WRANGLER_SEND_METRICS: "false" },
});
child.once("exit", code => { process.exitCode = code ?? 1; });
child.once("error", error => { process.stderr.write(error.message + "\n"); process.exitCode = 1; });
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => { child.kill(signal); });
