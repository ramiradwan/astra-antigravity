import { build } from "esbuild";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const result = await build({
  entryPoints: [path.join(root, "ui/main.ts")], bundle: true, write: false,
  platform: "browser", format: "iife", target: "es2022", minify: true,
  legalComments: "inline", charset: "utf8", logLevel: "warning",
});
const javascript = result.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
const shared = await readFile(path.join(root, "../ui/astra.css"), "utf8");
const css = (shared + "\n" + await readFile(path.join(root, "ui/styles.css"), "utf8")).replace(/<\/style/gi, "<\\/style");
const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Delegate to Antigravity</title><style>${css}</style></head><body data-surface="chatgpt"><main id="root" aria-label="Delegate to Antigravity"><article class="card"><header class="card-header"><h1>Delegate to Antigravity</h1><div class="status-line"><span class="task-status">Waiting for details</span></div></header><p class="latest-update">The card will update after the tool is approved.</p></article></main><p id="announcement" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></p><script>${javascript}</script></body></html>`;
await mkdir(path.join(root, "ui/dist"), { recursive: true });
await writeFile(path.join(root, "ui/dist/delegation.html"), html, "utf8");
console.log(`Built self-contained task card (${Buffer.byteLength(html)} bytes).`);
