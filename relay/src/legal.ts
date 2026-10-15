import privacy from "../../plugins/astra-antigravity-remote/docs/PRIVACY.md";
import terms from "../../plugins/astra-antigravity-remote/docs/TERMS.md";
import license from "../../LICENSE";
import { html, page } from "./security";

// Render the reviewed, version-controlled notices rather than maintaining a
// second copy of legal prose in a template. Raw HTML is always escaped.
function inline(text: string): string {
  const pattern = /`([^`]+)`|\[([^\]]+)\]\(([^\s)]+)\)/g;
  let result = "", offset = 0;
  for (const match of text.matchAll(pattern)) {
    result += html(text.slice(offset, match.index));
    if (match[1] !== undefined) result += `<code>${html(match[1])}</code>`;
    else {
      const relative: Record<string, string> = { "PRIVACY.md": "/privacy", "../LICENSE": "/license" };
      const target = relative[match[3]] ?? match[3];
      let safe = target.startsWith("/") && !target.startsWith("//");
      try { const url = new URL(target); safe = url.protocol === "https:" && !url.username && !url.password; } catch { /* Only mapped relative links are allowed. */ }
      result += safe ? `<a href="${html(target)}">${html(match[2])}</a>` : html(match[2]);
    }
    offset = match.index! + match[0].length;
  }
  return result + html(text.slice(offset));
}
export function legalPage(path: string): Response {
  const source = path === "/privacy" ? privacy : path === "/terms" ? terms : license;
  const blocks = source.trim().split(/\r?\n\s*\r?\n/);
  const heading = /^# (.+)$/.exec(blocks[0]);
  const title = heading ? heading[1] : "MIT license";
  const body = (heading ? blocks.slice(1) : blocks).map(block => {
    const section = /^## (.+)$/.exec(block);
    return section ? `<h2>${html(section[1])}</h2>` : `<p>${inline(block.replace(/\r?\n/g, " "))}</p>`;
  }).join("");
  return page(title, `<article class="legal-content">${body}</article>`, new Headers(), [],
    { eyebrow: "Delegate to Antigravity", wide: true });
}
