---
name: panel-builder
description: Build a user panel - a small self-contained web page that Faustus mounts in the Studio sidebar - from a plain-language request, test it headless and refine it in conversation. Use when the user asks for their own page, dashboard, viewer, tracker, board or tool inside Faustus ("hazme un panel que...", "quiero una pestaña con...", "build me a panel for...").
version: 1.0.0
category: engineering
tags: [panels, ui, dashboard, sidebar, paneles]
status: published
source: imported
---

## When to Use

The user wants a page of their own inside Faustus: a usage or cost dashboard, a viewer for some data, a tracker, a theme switcher, a design board, a reader with notes. It is a page that stays in the sidebar and is used repeatedly, not a one-off chart in a chat answer (for that, answer in the chat). To change an existing panel, start from `panel_list` and `panel_get`.

## Procedure

1. **Ask the minimum.** One short question only if the data source or the main action is genuinely unclear; otherwise decide and build. Say what you assumed afterwards, not before.
2. **Find the real data first.** Panels read Faustus through `faustus.api('/api/...')` and tools through `faustus.tool(name, args, app?)`. Call the route or tool yourself once (curl, or the tool directly) to see the exact shape of the answer; never invent field names. `faustus.tool` reaches every agent tool and every connected Hoard's tools, so a panel can also act (run, save, send), not only read.
3. **Write one self-contained HTML file.** Inline CSS and JS, no build step. Include `<script src="/api/panels/faustus-panel.js"></script>` in the head. Libraries only from cdn.jsdelivr.net, cdnjs.cloudflare.com or unpkg.com (pin a version); prefer plain DOM and inline SVG for charts. Besides Faustus itself, the page can fetch any HTTPS API (or open a wss: socket) directly.
4. **Use the app's theme.** The helper sets the `--fs-*` CSS variables on the page and `faustus.theme` (`vars`, `mode`); style with `var(--fs-bg)`, `--fs-surface-1`, `--fs-surface-2`, `--fs-border`, `--fs-text-1/2/3`, `--fs-brand`, and call `faustus.onTheme(fn)` if a canvas needs redrawing. Never hard-code a dark or light background.
5. **Keep state on the server.** Anything the user sets (filters, notes, last choice) goes in `faustus.storage.set(key, value)` / `get(key, default)`; it persists per panel. Do not rely on localStorage.
6. **Design for the frame.** Fluid width, readable at 700 px, an explicit empty state and error state when a request fails (show the message, not a blank page), loading text while fetching, Spanish UI text when the user writes Spanish.
7. **Create it, then test it.** `panel_create` with a short name, a lucide icon in kebab-case (bar-chart-3, box, book-open, palette, film, table, gauge, wallet...) and a one-line description. Then run `panel_check` and read every console error, page error and failed request; open the screenshot with `inspect_image` and look at it as the user will. Fix with `panel_update` (send the whole page) and repeat until `ok` is true and the screenshot shows real content, not an empty shell.
8. **Refine in conversation.** Each `panel_update` is a saved version; if a change makes things worse, `panel_versions` and `panel_restore`. Tell the user where the panel is (sidebar, Panels) and one sentence about what it shows and where its data comes from.

## Pitfalls

- Delivering without `panel_check`: a panel that throws on load looks like a blank frame to the user.
- Guessing an API shape. A wrong field name renders as `undefined` with no error; compare the screenshot with the real data.
- Hard-coding colours, so the panel is unreadable in the other theme.
- Fetching from external APIs or hosts: blocked by the page's policy and it would send the user's data out. Fetch through a Faustus route or tool.
- Rewriting the whole page for a small change without reading it first: use `panel_get`, change only what was asked, keep the rest identical.
- Putting secrets or tokens in the HTML; it is stored in plain text and shown in the version history.
- Creating a second panel for what is a change to an existing one.

## Verification

- `panel_check` returns `ok: true` with no console errors, page errors or failed requests, and `visible_text_chars` is greater than zero.
- The screenshot shows the real data in the panel, in the app's colours.
- The panel appears in the sidebar under Panels with the chosen icon and name.
- Data shown matches a direct call to the same route or tool.
