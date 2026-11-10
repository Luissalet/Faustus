---
name: panel-builder
description: Build a user panel - a small self-contained web app that Faustus mounts in the Studio sidebar - from a plain-language request, with its own database, scheduled server actions and ready-made templates; find the real data with panel_sources, test it headless and refine it in conversation. Use when the user asks for their own page, dashboard, viewer, tracker, board or tool inside Faustus ("hazme un panel que...", "quiero una pestaña con...", "un panel con las tareas del Ágora", "build me a panel for...").
version: 2.0.0
category: engineering
tags: [panels, ui, dashboard, sidebar, paneles, sqlite, plantillas, agora]
status: published
source: imported
---

## When to Use

The user wants a page of their own inside Faustus: a usage or GPU dashboard, the Ágora's tasks, a viewer for some data, a tracker, a kanban board, a theme switcher, a reader with notes. It stays in the sidebar and is used repeatedly; for a one-off chart, answer in the chat. To change an existing panel, start from `panel_list` and `panel_get`.

## Procedure

1. **Look for a template.** `panel_from_template` without arguments lists them: `gpu` (GPUs, loaded models, history every 5 minutes), `agora` (Ágora tasks per space with filters), `darwin` (Darwin proposals and captures), `notas` (notes and kanban board in its own database), `uso` (usage). If one fits, create it (`panel_from_template template=...`), then adjust it with `panel_update`; do not write it again from scratch.
2. **Find the real data with `panel_sources` - always, before writing any code.** It lists Faustus' routes with the shape of their answer, the Hoard Hub's routes and every connected Hoard with its tools. `panel_sources app=<id>` gives a Hoard's tool arguments; `panel_sources probe=/api/...` or `probe=hub:/api/...` fetches one route now and shows its live shape. Never guess a path and never build a probe panel to discover one.
   - **The Ágora (spaces, tasks, agents, locks) lives in the Hoard Hub, not in Faustus**: read it with `faustus.hub('/api/agora/spaces')` and `faustus.hub('/api/agora/tasks?space_id=<id>&status=active')`. Faustus' councils and groups are something else.
   - A Hoard's data comes from its tools: `faustus.tool('darwin_proposals', {state: 'pendiente'}, 'darwin')`; the answer's `result` field holds what the tool returned.
3. **Ask the minimum.** One short question only if the data source or the main action is genuinely unclear after step 2; otherwise decide and build. Say what you assumed afterwards.
4. **Write one self-contained HTML file.** Inline CSS and JS, no build step, `<script src="/api/panels/faustus-panel.js"></script>` in the head. The helper gives:
   - `faustus.api(path, opts)` Faustus' `/api/*`; `faustus.hub(path, opts)` the Hub (returns the parsed answer); `faustus.tool(name, args, app?)` an agent or Hoard tool; `faustus.fetch(url, opts)` any other http(s) URL fetched by the server (`{status, ok, body}`).
   - `faustus.db`: the panel's own SQLite. `query(sql, params, {limit, offset})` returns `{columns, rows: [{col: value}], more, next_offset}` (1000 rows per page by default, ask for more with `offset`); `exec(sql, params)`, `exec(sql, [[..], [..]], {many: true})`, `exec([{sql, params}, ...])` and scripts of several statements run in one transaction. Any SQL; only a time limit (10 s by default). Use it for anything the panel keeps: notes, cards, history.
   - `faustus.storage` small key-value settings (filters, last choice); `faustus.actions` `list()`, `run(name)`.
   - `faustus.theme` and the `--fs-*` CSS variables.
   Libraries only from cdn.jsdelivr.net, cdnjs.cloudflare.com or unpkg.com (pin a version); prefer plain DOM and inline SVG for charts.
5. **Create the tables first.** `panel_db_exec` with `CREATE TABLE IF NOT EXISTS ...` (and seed rows if the user asked for examples), so the page and the manager see them; the page may also create them on first load.
6. **History needs a server action.** If the panel should collect data while closed (a GPU sample every 5 minutes, Hoard counts every hour), add it with `panel_action_set`: `tool` (+ `app` for a Hoard) or `url` (`/api/...`, `hub:/api/...`, `https://...`), `every_minutes` or `cron`, and `save`: `{table, columns: {col: path}, rows?: path, keep?: n}`, `{sql, columns}` or `{storage, value?, append?}`. Paths are dotted (`gpu_pool.util`, `gpu.0.util`, `resident.models[].model`); `$now`, `$ts`, `$` and `$root.x` are special. Run it once with `panel_action_run` (or `run_now: true`) and read what it saved with `panel_db_query`. It runs through Faustus' scheduler, appears in Automations and does not notify on every run.
7. **Use the app's theme.** Style with `var(--fs-bg)`, `--fs-surface-1`, `--fs-surface-2`, `--fs-border`, `--fs-text-1/2/3`, `--fs-brand`; call `faustus.onTheme(fn)` if a canvas needs redrawing. Never hard-code a dark or light background.
8. **Design for the frame.** Fluid width, readable at 700 px, an empty state and an error state that shows the message, loading text while fetching, Spanish UI text when the user writes Spanish.
9. **Create it, then test it.** `panel_create` with a short name, a lucide icon in kebab-case (bar-chart-3, cpu, list-checks, box, book-open, table, gauge, wallet...) and a one-line description. Run `panel_check` and read every console error, page error and failed request; open the screenshot with `inspect_image`. Fix with `panel_update` (the whole page) and repeat until `ok` is true and the screenshot shows real data.
10. **Refine in conversation.** Each `panel_update` is a saved version (`panel_versions`, `panel_restore`). Tell the user where the panel is (sidebar, Panels), what it shows and where its data comes from; in Panels > Manage they can see its tables, rows and actions.

## Pitfalls

- Guessing a route: `/api/agora/...` on Faustus is a 404; the Ágora is `faustus.hub('/api/agora/...')`. Check with `panel_sources probe=...` instead.
- Building a throwaway probe panel to discover data: use `panel_sources`.
- Delivering without `panel_check`: a panel that throws on load is a blank frame.
- Guessing field names: a wrong one renders as `undefined` with no error; compare the screenshot with `panel_sources probe=...`.
- Keeping history in `faustus.storage` (one JSON blob): use `faustus.db` tables and a server action.
- Hard-coding colours, so the panel is unreadable in the other theme.
- Rewriting the whole page for a small change without reading it first: `panel_get`, change only what was asked.
- Secrets or tokens in the HTML: it is stored in plain text and in the version history.
- A second panel for what is a change to an existing one.

## Verification

- `panel_check` returns `ok: true` with no console errors, page errors or failed requests, and visible text.
- The screenshot shows the real data in the app's colours.
- `panel_db_query` (no sql) lists the tables the page uses; `panel_action_list` shows the action's last run as ok and its next run.
- The panel appears in the sidebar under Panels with its icon and name.
