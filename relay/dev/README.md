# Local UI gallery

Run from `relay/` with Node 24:

```sh
npm ci
npm run dev:gallery
```

Open `http://127.0.0.1:8790/__gallery`. The gallery renders the production hosted pages, companion assets, and embedded task card using synthetic local state. It includes first use, approval, connected, recovery, task, decision, and result views.

The entry point is separate from production Wrangler configuration. It binds to loopback, uses local SQLite/KV fixtures, and does not expose provider commands, OAuth callbacks, MCP task endpoints, or production API access. Companion actions update canned state; the card's mock host records actions without sending them anywhere.

Run browser checks with:

```sh
npx playwright install chromium
npm run test:browser
```

Playwright starts the gallery if it is not already running. Checks cover light/dark modes, mobile and desktop widths, WCAG AA checks, enlarged text, keyboard focus, status updates, download feedback, and companion recovery. Screenshots, traces, and reports are written to ignored `test-results/` and `playwright-report/` directories.
