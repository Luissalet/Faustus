# Connectors (F1 — catalogue, presets, real status, launch profiles)

Backend for the Connectors screen (Faustus connector plan, Phases C+D).
Builds entirely on the existing MCP stack — `src/mcp_manager.py::McpManager`
and the `McpServer` table (`core/database.py`) — and adds nothing that
duplicates them. `routes/mcp/mcp_routes.py`'s own `/api/mcp/*` contract is
unchanged; this is a new, additive router.

## Files

| File | Purpose |
|---|---|
| `src/connectors.py` | The two built-in presets (`jobhunter`, `writer`) and `resolve_preset_values(preset, values)` — pure placeholder substitution + `os.path.isdir`/`isfile` checks. No network, no secrets. |
| `src/connector_sidecar.py` | `data/connectors.json` — the extra fields `McpServer` has no column for (`preset_id`, `app_url`, `ui_url`, `launch_profile_id`, `owner`). Atomic writes, redaction on read. |
| `src/connector_status.py` | The real `ConnectorStatus` computation: a live health GET against the domain app + the MCP adapter's own status, combined per the priority order below. 15s debounce cache per connector. |
| `src/process_launch.py` | Generic detached-process primitive (`subprocess.Popen(..., shell=False)` + `core.platform_compat.detached_popen_kwargs()` + a 256KB-capped log) used by launch profiles. Not shared with `routes/cookbook_routes.py::_launch_local_detached` — see "Deviations" below. |
| `src/launch_profiles.py` | `data/launch_profiles.json` — user-defined "start this local app" profiles: validated argv/executable/cwd/env, idempotent `launch()`, loopback detection. |
| `routes/connector_routes.py` | `setup_connector_routes(mcp_manager)` — every HTTP route below. |

## Routes

All under `require_admin` (same gate as `/api/mcp/*`) **except** the ones
that can start a local process/executable directly, which are
`require_human` (refuses Faustus's own internal agent-tool token —
principle 4: "the model gets no arbitrary execution").

| Method & path | Auth | Notes |
|---|---|---|
| `GET /api/app-connectors/presets` | admin | The two built-in presets, with placeholders and launch-profile hints. |
| `GET /api/app-connectors?check=1` | admin | Every sidecar-backed connector (values redacted) **plus** every `McpServer` with no sidecar entry (`preset_id: null`), so the screen shows one unified list. Without `check=1`, returns the last cached health (or `state: "unknown"` if never checked); `check=1` forces a fresh probe (debounced to once per 15s). |
| `POST /api/app-connectors` | admin | `{preset_id, values, name?, launch_profile_id?}`. Resolves the preset, creates an `McpServer` through the **same internal handler** `POST /api/mcp/servers` uses (no duplicated validation), then the sidecar entry. Does not connect. 409 on a duplicate (same preset + owner + `APP_URL`). |
| `PATCH /api/app-connectors/{id}` | admin | `{values?, name?, launch_profile_id?, is_enabled?}`. Changing `values` regenerates the `McpServer`'s `command`/`args`/`env` and disconnects it (reconnect is separate). |
| `DELETE /api/app-connectors/{id}` | admin | Deletes the sidecar entry and the underlying `McpServer`. |
| `POST /api/app-connectors/{id}/check` | admin | Forces a fresh health check + returns the full status. |
| `POST /api/app-connectors/{id}/connect` / `/disconnect` | admin | Delegates to `McpManager`. Idempotent: connecting an already-connected server returns its current status without a second session. |
| `GET /api/app-connectors/{id}/tools` | admin | Delegates to `GET /api/mcp/servers/{id}/tools`. |
| `POST /api/app-connectors/{id}/launch` | **human** | Runs the connector's `launch_profile_id`. 403 if none is configured, or if the profile belongs to a different user. |
| `POST /api/app-connectors/{id}/open` | **human** | Returns `{"kind":"url","url":...}` when the connector has a `ui_url` (client opens it — no process spawned), otherwise runs an `open_exe` launch profile if one is set. |
| `GET/POST/PATCH/DELETE /api/launch-profiles[/{id}]` | **human** (all verbs, including reads) | CRUD for launch profiles. Never reachable from an agent tool — verified in `tests/test_connectors.py::test_launch_profiles_tool_is_never_exposed_to_the_agent`. |

## `ConnectorStatus` — the seven states

`unconfigured | app_off | connecting | available | error | disabled | unknown`,
decided in this order (first match wins):

1. **`disabled`** — the `McpServer.is_enabled` is false.
2. **`unconfigured`** — a declared placeholder has no value and no default,
   or `{X_DIR}`/the bridge script fails `os.path.isdir`/`isfile`.
3. **`unknown`** — health has never been checked for this connector and the
   caller didn't ask for a fresh one (`app.reachable: null`, never `false`).
4. **`app_off`** — the health GET was refused or timed out (2s, no
   redirects, no token sent).
5. **error (Writer only)** — the app answered, but its `service` field in
   the health body doesn't match `writers-hoard-ai-bridge` — a different
   process is listening on that port. Message: `Port {port} answers but it
   is not Writer's Hoard (service={x})`.
6. **`connecting`** — the MCP adapter itself is mid-connect.
7. **`available`** — health OK (Jobhunter: *any* HTTP response counts,
   including a 404 from an old build without `/api/health` — old-build
   compatibility; Writer: the service marker must match) **and** the
   adapter reports `connected` with `tool_count > 0`.
8. **`error`** — anything else (adapter not connected, connected but no
   tools, etc.), with an actionable `reasons` list. Never a secret.

`app` and `adapter` are always reported separately — a `tools/list` catalogue
proves the bridge started, never that the domain app itself is alive
(principle 3).

## Launch profiles

- CRUD and the two launching routes are `require_human` only — an agent
  tool can never create/edit a profile or reach `/api/launch-profiles`.
- Validated identically at save time and at launch time: `executable`
  absolute + `isfile` (for `process`/`open_exe`), `cwd` absolute + `isdir`
  (for `process`), `argv` a plain list of strings with no newlines, `env`
  keys matching `[A-Z_][A-Z0-9_]*`. Always `shell=False`, always a
  structured argv — nothing is ever built as a shell string.
- **Idempotent**: a launch that finds the readiness URL already answering,
  or its own previously-spawned pid still alive, returns
  `{"launched": false, "already_running": true}}` instead of spawning again.
  Concurrent double-clicks are serialised by a per-profile lock.
- `kind: "process"` polls its `readiness.url` every 0.5s up to
  `readiness.timeout_s`; a timeout returns `{"launched": true, "ready":
  false, ...}` with the pid — **the process is never killed**.
- `kind: "open_url"` launches nothing; the caller opens the URL itself.
- `kind: "open_exe"` launches the executable with no readiness polling
  (a desktop GUI app, e.g. Writer's Hoard, has nothing to poll).
- **Loopback**: when a readiness/open URL is `127.0.0.1`/`localhost` and the
  request came from a non-loopback client, the result carries a `reasons`
  entry ("This URL is loopback on the Faustus host; configure the real
  address") instead of silently handing back a URL that will never work for
  that client.
- **Stopping Faustus never terminates a launched process.** These are the
  user's own local apps (Jobhunter, Writer's Hoard); this module has no
  teardown path for them at all, by design.
- **Windows**: no console window (`detached_popen_kwargs()` uses
  `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`), same as every other
  detached spawn in this codebase.
- Logs are capped to 256KB per profile, trimmed from the front (the crash is
  always the last line).

## Secrets

`connector_sidecar` redacts any `values` key whose name contains `TOKEN`,
`KEY`, `SECRET`, or `PASSWORD` on every read (`GET /api/app-connectors`,
`get_connector(..., redact=True)`). Credentials themselves never live in the
sidecar or in a launch profile — only paths/URLs; the actual `McpServer.env`
still goes through the existing encrypted column.

## Deviations from the contract, and why

- **Owner filtering**: the contract asks for "the same convention as
  `src/connector_registry.py`", but the real `GET /api/mcp/servers`
  (`routes/mcp/mcp_routes.py::list_servers`) does not filter by owner at
  all — checked against the actual route. Per the contract's own escape
  hatch, `GET /api/app-connectors` does not filter by owner either. `owner` is
  still recorded on every sidecar entry and launch profile (informational,
  and used for the launch-time "belongs to a different user" check).
- **`_launch_local_detached` is not called directly.** It is a private
  closure inside `setup_cookbook_routes` built around cookbook's own tmux
  session convention (a bash wrapper script under `TMUX_LOG_DIR`,
  `<session>.pid`/`.log` for a poller) — not a general "start this
  executable" primitive, and a launch profile must run its executable
  directly with a structured argv, never through a bash wrapper (principle
  4: `shell=False`). `src/process_launch.py` factors out the actually
  reusable core (`detached_popen_kwargs()` + `Popen` + `process_ownership`
  bookkeeping + a bounded log) instead. `routes/cookbook_routes.py` itself
  is untouched; its own tests (`tests/test_cookbook_local_serve_pid_winpid.py`,
  `tests/test_inf02_serve_routes.py`) still pass unmodified.
- **Auth split within `routes/connector_routes.py`**: F1.5 says "same auth
  dependency as `mcp_routes.py`" for the whole router, but principle 4
  explicitly singles out launch profiles and "an agent tool must not create
  or edit a profile or pass argv" — those two statements conflict for the
  launch/open/profile routes specifically. Principle 4 (a numbered,
  non-negotiable rule) wins there: `require_human` on launch-profile CRUD
  and on `/connectors/{id}/launch` + `/connectors/{id}/open`; every other
  connector route stays `require_admin`, matching `/api/mcp/*` exactly (an
  MCP-backed connector's argv is the preset's own fixed script, only
  directories/URLs substituted — the same risk level `/api/mcp/servers`
  already accepts at `require_admin`).
- **`McpServer` has no `owner` column** (checked against `core/database.py`)
  — confirmed and used as the basis for the point above; not something this
  lote adds, per rule 10 (`/api/mcp/*`'s contract is additive-only).
- **Extra `ConnectorPreset` fields** (`defaults`, `optional_extra_env`) exist
  beyond F1.1's literal dataclass — needed to express Writer's
  platform-dependent `TOKEN_FILE` default and its optional
  `WH_BRIDGE_GROUPS` env var without inventing a second preset shape. All of
  F1.1's named fields are present and unchanged in meaning.

## Not yet implemented (F2/F3 territory)

- Per-task/session/project connector allow-lists and dispatcher enforcement
  (Lote F2, `src/connector_policy.py`).
- The Studio `/connectors` screen (Lote F3).
