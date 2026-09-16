# ui_smoke — harness-driven UI smoke test (H1)

`src/ui_smoke.py`. Forensic motivation: `silhouettes_analysis.md`, chat
`782b7d89` (16-09-2026) — a turn rewrote `templates/editor.html` +
`static/editor/*`, 170 pytest + 20 node tests passed, the turn closed
`verified`. Nothing worked: Flask served the new `.mjs` modules with
`Content-Type: text/plain`, so the browser refused to run them as ES
modules and every button in the app was dead. Invisible to unit tests;
found only when Luis reported it by hand.

## What it does

Given a workspace and the list of files a turn mutated:

1. **Trigger.** Skip entirely (`ran=False`) unless a mutated path looks
   UI-facing (`static/`, `templates/`, `public/`, `dist/`, `*.html`,
   `*.css`, `*.js`/`*.mjs`/`*.jsx`/`*.tsx`, `app.py`/`main.py`/`server.py`/
   `wsgi.py`/`asgi.py`, `package.json`).
2. **Detect a server** (`detect_server`, first match wins): Flask
   (`Flask(` in `app.py`/`wsgi.py`/…), FastAPI (`FastAPI(`, run via
   `uvicorn`), an npm `start`/`dev` script in `package.json`, or a bare
   `index.html` (served with `python -m http.server`).
3. **Launch** on a free port (`socket.bind(("127.0.0.1", 0))`), via
   `subprocess.Popen` with no shell, `cwd=workspace` (or the static
   directory), `PORT`/`FLASK_RUN_PORT` in the environment, own process
   group/group on POSIX (`start_new_session=True`) or a new process group
   on Windows — the server never runs in the turn's own foreground.
4. **Wait for readiness**: poll `GET /` until any HTTP response or the
   process exits, bounded to `min(20s, 0.4 * total timeout)`.
5. **Crawl**: fetch `/` plus routes found via `@app.route(...)` in the
   entry file's source (up to 6 pages); for every HTML page, extract
   `<script src>`, `<link href="*.css">` and ES `import`/dynamic `import()`
   targets, resolve them against the page URL, and fetch each once
   (deduped, capped at 25). **The actual check**: a `.js`/`.mjs` asset must
   come back with a JS `Content-Type` (`text/javascript`,
   `application/javascript`, …) — not just a 2xx status. A `.css` asset
   must come back `text/css`. Mismatch → `ok: false, problem:
   "content_type"`; non-2xx → `problem: "status"`.
6. **Optional Playwright pass** (Python `playwright` + a Chromium build,
   e.g. `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`): load `/` headless,
   collect `console` `error` messages, uncaught `pageerror` exceptions, and
   4xx/5xx network responses. Skipped silently (not a failure) when
   Playwright or a browser isn't available — `playwright_used: false`.
7. **Kill**, always, in `finally`: `src/process_ownership.py::terminate_tree`
   (the same tree-kill the agent's own background jobs use —
   `src/agent_tools/subprocess_tools.py::_kill_tree`/`_kill_tree_async`),
   `unverified_tree_ok=True` because the just-spawned `Popen` object is
   proof enough of ownership.

## API

```python
from src import ui_smoke

report = ui_smoke.run_for_turn(workspace, mutated_paths)
# -> None                      if agent_ui_smoke is off
# -> {"ran": False, ...}       nothing UI-shaped changed, or no server found
# -> {"ran": True, "ok": bool, "summary": str,
#     "server_cmd": str, "url": str,
#     "pages":  [{"url": str, "status": int|None, "ok": bool}, ...],
#     "assets": [{"url": str, "status": int|None, "content_type": str|None,
#                  "ok": bool, "problem": "status"|"content_type"|None}, ...],
#     "console_errors": [str, ...],
#     "playwright_used": bool}
```

`ui_smoke.failure_message(report)` — the bounded fix-round instruction
(mirrors `project_tests.failure_message`'s shape).
`ui_smoke.compact(report)` — the small shape for persistence/the turn's
`verified` payload (counts, not full bodies).

## Settings (`src/settings.py` / `src/agent_settings_schema.py`, group
`verification`)

- `agent_ui_smoke` (bool, default `True`)
- `agent_ui_smoke_timeout_seconds` (int, default `60`, 10–600)
- `agent_ui_smoke_playwright` (bool, default `True`) — use the optional
  Playwright pass when available.

## Guarantees

- Never raises: every failure (bad launch command, server crash, timeout,
  Playwright unavailable) is folded into the returned report.
- Never blocks the turn in the foreground the way a model's own
  `python app.py` would (H1's whole reason to exist) — it is always run
  off the event loop (`asyncio.to_thread`, see `H1_wiring.md`) and the
  server is always killed before `run_smoke` returns.
- No orphan process: `tests/test_h1.py` asserts (via `psutil`) that no
  process with `cwd == workspace` survives a run, including the "server
  crashes on startup" case.
