---
name: studio-vitruvius-design-critique
description: Get a cited design brief and rules from Vitruvius before coding a page, then render the built page at three widths, critique it, run its interaction assay and fix until the floor findings are gone. Use when a production order asks for a web page or interface and its design must be argued and checked rather than guessed.
version: 1.0.0
category: engineering
tags: [studio, vitruvius, design, critique, render, assay, web, landing, accessibility, diseno, critica, pagina, web, accesibilidad]
apps: [vitruvius]
accepts: [production-brief, web-page]
produces: [interface-brief, responsive-captures]
status: published
source: imported
---

## When to Use

The design station of a web order, used twice: before the code (brief and rules) and after it (render, critique, assay). For the general loop see `hoard-design-loop`; this is the exact route that worked and its traps.

- **Receives:** the brief with acceptance criteria (`studio-drucker-engagement-brief` or the order's own `expediente.md`); later, the built page.
- **Delivers:** before code, the cited brief and the rules applied (`direccion.md` saying what was followed and what was changed and why); after code, renders at 390, 1024 and 1440 px, the critique of each round, the assay result, and the fixed version.

## Procedure

1. **Reach the station.** `hub_app_status {app:"vitruvius"}`; if it is down, `hub_start_app {app:"vitruvius"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"vitruvius"}` gives the live catalogue; calls are `hub_call_app {app:"vitruvius", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/vitruvius/call` with `Authorization: Bearer <token>` (the Hub's data directory contains the `mcp-token` file) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/vitruvius` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/vitruvius/start` and `/stop` start and stop it and `GET /api/apps/vitruvius/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder.
2. **Module.** `software_modules`; if `design` is off, `software_module_set {module:"design", enabled:true, automatic:false}` and set it back at the end.
3. **Brief.** `design_brief {subject, vibe, product_type:"landing page", platform:"web", mode:"marketing", use_model:false}`. `mode` is `marketing` or `product`; `light` or `dark` answers 400 (those are colour schemes).
4. **Rules and guides.** `design_rules {area:"a11y", limit:12}` (the `interaction` area returned nothing in the probe); `web_guide_search {query, limit:4}` for specific questions such as form validation.
5. **Write `direccion.md`**: each decision with its citation, and each departure from the brief with its reason (offline fonts, a palette taken from the product's own images).
6. **After the code: render.** `render_preview {path:<self-contained HTML>, widths:[390,1024,1440], title, wait_ms:250}`. Inline CSS, scripts and images into one HTML file first: a loopback URL answered 400 and `path` does not resolve relative files (check the live catalogue: these may have been fixed). The compacted reply may list only one PNG; the other widths are in the same render folder.
7. **Critique.** `design_critique {render_id, focus, use_vision:false}`. With vision it ran out of time at 52 s; use it only with a long `timeout_s` and treat a timeout as no answer.
8. **Assay.** `page_assay {render_id, timeout_s:90, label, criteria:[{id, what, environment:{viewport:{width,height}, reduced_motion, color_scheme}, acts:[{click:"<selector>"}], expect:{want, no_overflow, no_motion}}]}`. With 30 s it timed out; 90 s completed.
9. **Fix and repeat** steps 6-8 until the floor findings are 0; record the score and findings per round.

## Pitfalls

- Known false positives: filter buttons counted as calls to action, an order form's email read as a newsletter signup, a form section judged sparse. Say which findings you leave and why.
- The assay runs on the portable HTML, not the served site: saving a form must be checked on the served site (`studio-headless-web-testing`).
- A module left on changes what other agents see in the catalogue; restore it.

## Verification

- Renders of every round opened at the three widths; the critique's floor findings at 0 on the final round.
- Assay result with every criterion passed, or the failing ones listed.
- `direccion.md` explains every decision a reviewer could question.
