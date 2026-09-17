# The Faustus mobile app's server surface (lot M-A)

`routes/mobile_routes.py` is everything under `/api/mobile/*` plus its
WebSocket push. It is additive: every read reuses the same DB tables and
`session_manager` the desktop Studio already reads, in a compact,
phone-shaped shape. The Android app (lot M-B, `mobile/android/`) codes
against this page.

## Pairing and auth

Pairing is unchanged and already existed (`companion/routes.py`):

```
POST /api/companion/pair?format=json      (admin cookie session)
→ {"host", "port", "token", "token_id", "hosts", "payload", "qr"}
```

The phone stores `token` and sends it as `Authorization: Bearer <token>` on
every HTTP call and `?token=<token>` (or the same header) on the WebSocket.

### What the contract assumed, and what is actually true

The lot M-A contract said every route here is `require_admin`, "which the
bearer token from pairing already satisfies". **That is false as written.**
Two separate gates had to be worked around, both discovered and fixed while
building this lot — not left as follow-ups:

**1. `require_admin` does not accept a bearer token for a real user.**
`core/middleware.py::require_admin` checks `auth_mgr.is_admin(user)` where
`user = request.state.current_user`. `app.py`'s `AuthMiddleware` always
stamps a bearer `ody_` caller's `current_user` as the sandboxed pseudo-user
`"api"` — never a real username (`src/auth_helpers.effective_user`'s own
docstring explains why: bearer callers must not wander into cookie/user
routes by accident). `auth_mgr.is_admin("api")` is therefore always `False`,
so a plain `require_admin(request)` call 403s *every* companion-paired phone
request. `routes/approvals_routes.py::_read_owner` already worked around
exactly this for its own bearer-token reads; `routes/mobile_routes.py::
_mobile_owner` is the same pattern generalized to this whole router — a
bearer token is trusted directly (its scope was already checked by
`AuthMiddleware`/`core/authz.py` before the request reached this module),
and only a cookie caller still goes through the real `require_admin`.

**2. The companion pairing token does not, by itself, reach
`POST /api/chat_stream`.** Not a cookie/CSRF issue (the contract's own
guess) — a *scope* one. `core/authz.py` is a deny-by-default matrix: a
bearer token reaches a route only if a rule names one of its scopes.
`POST /api/chat_stream` requires the `sessions` scope
(`docs/api/sdk_surface.md`); the companion pairing token is minted with
`scope="chat"` only (`companion/pairing.py:COMPANION_SCOPE`). A direct
`POST /api/chat_stream` bearer call from a paired phone gets a 403 naming
the missing scope, before the route body ever runs. This is why
`POST /api/mobile/session/{sid}/send` exists (below) — the contract's own
"add a shim if needed" escape hatch, triggered by a different cause than it
guessed.

Two fixes ship with this lot:

- `core/authz.py` gained one new rule opening the whole `/api/mobile/*`
  prefix to the `chat` scope — the scope the pairing token already carries.
  Deliberately **not** widened to `sessions`: the mobile app's surface is
  this router's own compact endpoints, not the raw SDK surface, so it never
  needed the broader scope.
- `routes/mobile_routes.py::_mobile_owner` replaces `require_admin(request)`
  everywhere in this router with a helper that trusts an already-scope-
  checked bearer token directly, and only defers to the real
  `require_admin` for a cookie caller.

### The WebSocket has no middleware at all

`AuthMiddleware` is a Starlette `BaseHTTPMiddleware`, and Starlette never
runs HTTP middleware against a `websocket` ASGI scope — verified directly
against a live `TestClient` WS connection while building this lot: a socket
with no credentials at all reaches the handler with `request.state` never
touched. So `GET /api/mobile/ws` authenticates itself, by hand, before
`accept()`:

- reads `?token=` from the query string, or an `Authorization: Bearer`
  header if the query param is empty (some Android WS client stacks make
  setting a handshake header awkward — both are honored so the app can use
  whichever its library supports),
- looks the token up directly against the `api_tokens` table (prefix match,
  then `bcrypt.checkpw`) — the same check `AuthMiddleware`'s bearer path
  runs, reimplemented here because that middleware's token cache is a
  private closure inside `app.py` with nothing exported to reuse,
- on failure, closes with WebSocket code **4401** before `accept()` — not a
  silently-dropped connection, so a client can show "pairing expired"
  instead of retrying forever.

## Endpoints

All `require_admin`-equivalent (`_mobile_owner`, see above) unless noted.

### `GET /api/mobile/bootstrap`

```json
{
  "version": "…",
  "server_name": "Faustus",
  "me": "luis",
  "capabilities": {"chat": true, "calendar": true, "notes": true,
                    "approvals": true, "whatsapp": true, "cards": true},
  "counts": {"sessions": 12, "pending_approvals": 1, "active_turns": 0}
}
```

`capabilities` is static today (every listed surface already exists); it is
a flat object so a future server can turn one off without the app needing a
new field to check for.

### `GET /api/mobile/notifications?since_id=&limit=50`

```json
{"items": [{"id": 42, "kind": "turn_finished", "owner": "luis",
            "title": "Weather chat", "body": "18°C, light rain in…",
            "data": {}, "session_id": "s1", "approval_id": null,
            "ts": 1758000000.0}],
 "last_id": 42}
```

Backed by `src/notifications.py` — see **The notification bus** below.
`since_id=0` returns up to `limit` of the most recent events; a caught-up
client (nothing new since its last `since_id`) gets `items: []` and
`last_id` unchanged, so it never needs to special-case "nothing happened".

### `GET /api/mobile/sessions?limit=30`

```json
{"sessions": [{"id": "s1", "name": "Weather chat", "updated_at": "…",
               "model": "qwen3.8:27b", "mode": "agent", "running": false,
               "last_preview": "the forecast is sunny with a high of…"}]}
```

Ordered by `updated_at` desc, capped to `limit` (max 100). `running` reads
the same live-turn registry `GET /api/chat/activity` uses
(`src/agent_runs.active_session_ids()`). `last_preview` is the session's
most recent message content, trimmed to 200 characters.

### `GET /api/mobile/session/{sid}/messages?limit=60&before=`

```json
{"messages": [{"id": "m1", "role": "user", "content": "…",
               "created_at": "2026-09-17T18:00:00", "tool_calls_summary": null}]}
```

Plain text only — no image/blob payloads, matching the contract. Oldest
first, newest last. `before` is an ISO-8601 timestamp (from an earlier
page's oldest message's `created_at`) for paging further back; omitted, the
most recent `limit` messages are returned. `tool_calls_summary` is present
only when the message's metadata carries `tool_events` (e.g.
`"used: web_search, read_file"`) — a one-line hint for a phone bubble, not
the full tool payload.

### `POST /api/mobile/session/{sid}/send` — the `chat_stream` shim

Body: the same JSON `POST /api/chat_stream` accepts (`message`, optional
`attachments`, `use_web`, etc. — `session` is set from the path and
overrides anything in the body). Response: the identical `text/event-stream`
SSE wire `POST /api/chat_stream` emits (`docs/api/sse_events.json`) — this
route does not re-implement any of that logic, it loopbacks to the real
endpoint over HTTP using the internal-tool token with owner impersonation
(`X-Odysseus-Internal-Token` + `X-Odysseus-Owner`, the exact mechanism
`src/builtin_actions.py`'s Cookbook actions already use for admin-gated
loopback calls) and relays the upstream bytes back to the phone unchanged.

Use this instead of a direct bearer call to `/api/chat_stream` — see
**What the contract assumed** above for why the direct call 403s on scope.

### `GET /api/mobile/ws?token=<bearer>`

On connect (after the token check above):

```json
{"type": "hello", "last_id": 42}
```

Then, forever, one of:

```json
{"type": "event", "event": {"id": 43, "kind": "approval_pending", …}}
{"type": "ping"}
```

`ping` is sent every 25 seconds when nothing else has been sent, as a
keepalive (mobile network idle-socket timeouts are commonly well under a
minute). The socket accepts `{"type": "ack", "id": N}` and
`{"type": "pong"}` from the client — both are no-ops server-side; the
notification ring is the source of truth, so there is nothing to "advance"
on an ack, and unrecognized message shapes are ignored rather than rejected
(keeps the wire forward-compatible with a client field this server doesn't
know about yet).

`hello.last_id` is **not** a backlog — the socket never resends events that
predate the connection. A reconnecting client that wants to catch up on
what it missed calls `GET /api/mobile/notifications?since_id=<its last known
id>` once, then relies on the socket for everything after. This keeps the
WS handler simple (three independent loops — forward the queue, send pings,
read acks — with no client-supplied cursor to validate) and matches what
the ring already exists for.

## The notification bus (`src/notifications.py`)

A separate, deliberately smaller system from the existing client-driven
store (`routes/notifications_routes.py`, `/api/notifications/*`, ACT-02).
That one is observed and reported by the *browser* (dedupe by
`dedupe_key`, per-type channel prefs, quiet hours) and was explicitly scoped
to never touch `chat_routes`/`agent_loop`/`task_scheduler`. This module is
the other half: the actual places a turn/approval/task/reminder *finishes*
push into it directly, so a phone that isn't polling anything still gets a
push. Different file (`notifications.jsonl` vs `notifications.json`),
different shape (a flat bounded ring vs per-owner unread state), on purpose
— they do not share dedupe or read state.

- `emit(kind, *, owner, title="", body="", data=None, session_id=None,
  approval_id=None)` — appends to a 200-event ring (oldest dropped first),
  mirrors the ring to `data/notifications.jsonl` (so a restart doesn't lose
  the last few events), and wakes any live WS subscriber for that owner.
  **Never raises** — every call site wraps it in `try/except` anyway, but
  the function itself already swallows and logs internally. `title`/`body`
  are truncated to 200 characters.
- `list_events(owner, since_id, limit)` → `(rows, last_id)`.
- `subscribe(owner)` / `unsubscribe(owner, queue)` — an `asyncio.Queue` per
  live WS connection.
- `latest_id()` — the ring's current tail id, what a fresh WS connection's
  `hello` reports.

### Event kinds and where each one fires

| kind | fires from | title | body |
|---|---|---|---|
| `turn_finished` | `routes/chat_helpers.py::save_assistant_response` — the single function every `/api/chat_stream` finalization branch (normal completion, image generation, the error-recovery paths) calls to persist the final assistant message, so this fires exactly once per turn regardless of which branch produced the reply. Skipped for an incognito turn. | the session's name | the final assistant text |
| `approval_pending` | `POST /api/approvals/request` | `plan.action` | `plan.detail` (falls back to recipients, then `skill_id`) |
| `approval_resolved` | `POST /api/approvals/{id}/grant` and `/deny` — only on an actual decision; a lost race (`already_granted`, `expired`, …) does not fire a second event | `plan.action` | `"granted: …"` / `"denied: …"` |
| `task_finished` | `src/task_scheduler.py::notify_task_finished`, called where a `TaskRun` finalises with `status` in `{success, error}` (fenced/aborted/skipped/deferred runs are not covered, matching what the scheduler's own browser notification already treats as worth telling the user about) | the task's name | the result's first line, falling back to the error, falling back to the status word |
| `reminder` | `routes/note/note_routes.py::dispatch_reminder`, fired once regardless of which channel (email/ntfy/webhook/browser) actually carried it | the reminder's title | the synthesized sentence, or the note body |
| `turn_error` | reserved — not wired to a hook in this lot. `KINDS` declares it so a future caller has a name to emit under instead of inventing a fourth string for "a turn failed"; no chat_routes error-recovery branch was hooked because none maps as cleanly to "one call site, once per failure" as `save_assistant_response` does for success. |

## Verification

- `python3 -m pytest tests/test_notifications.py tests/test_mobile_routes.py
  tests/test_approvals_routes.py tests/test_l68_sec01_approvals_active_revoke.py
  tests/test_auth1_token_matrix.py tests/test_sdk_surface_authz.py
  tests/test_chat_helpers.py tests/test_task_scheduler_cache.py
  tests/test_task_scheduler_cancel.py tests/test_scheduler_restart_doublefire.py
  tests/test_note_reminder_fire_scope.py tests/test_note_reminder_email_oauth.py
  tests/test_note_routes_shim.py tests/test_reminder_ntfy_ssrf.py -q` — 255
  passed.
- Full `app.py` import boots with `/api/mobile/*` and `/api/chat_stream` both
  present in the route table (FastAPI 0.141's `_IncludedRouter` wraps
  sub-routers lazily — `app.routes` needs walking one level down to see
  individual paths, which is how this was actually confirmed rather than
  taken on faith).
- `core.authz.api_token_allowed("GET", "/api/mobile/bootstrap", ["chat"])` →
  `(True, "")`; the same call with `[]` → `(False, "API token missing
  required scope: chat")`.

## Web Push (`src/push.py`, `routes/push_routes.py`, lot P-A)

Real browser/OS push notifications for the same events `src/notifications.py`
already emits — the ones above already reach a connected WebSocket; this is
what reaches a phone/desktop when Studio's tab (or the installed PWA) isn't
even open. Built without `pywebpush`/`http-ece` (their wheels don't build in
this environment): message encryption is RFC 8291 (`aes128gcm`, ECDH P-256 +
HKDF-SHA256) and the sender identity is RFC 8292 VAPID (an ES256 JWT), both
implemented directly on `cryptography` (already a hard dependency). See
`src/push.py`'s module docstring for the derivation steps; the encryption is
validated against RFC 8291's own Appendix A worked example byte-for-byte in
`tests/test_push.py`.

### Auth

Same pattern as the rest of this file: `_push_owner()` in
`routes/push_routes.py` trusts a companion-pairing bearer token directly
(already scope-checked by `AuthMiddleware`/`core/authz.py`) and only defers
to the real `require_admin` for a cookie caller.

### Endpoints

**`GET /api/push/vapid-key`** → `{"key": "<base64url>"}` — the VAPID public
key as an uncompressed P-256 point, exactly what
`PushManager.subscribe({applicationServerKey})` expects. Generated once into
`data/push/vapid.json` (0600) and stable for the life of the install — a new
keypair on every restart would invalidate every subscription a browser
already pinned `applicationServerKey` against.

**`POST /api/push/subscribe`**
```json
{"subscription": {"endpoint": "https://…", "keys": {"p256dh": "…", "auth": "…"}},
 "device_name": "Pixel 8"}
```
→ `{"ok": true, "subscription": {"id": "…", "owner": "…", "endpoint": "…",
"keys": {...}, "device_name": "…", "created_at": …, "last_ok": null,
"failures": 0}}`. Deduped by `endpoint` — resubscribing (a rotated key, a
re-registered service worker) updates the existing row instead of piling up
dead duplicates. Persisted to `data/push/subscriptions.json` (0600).

**`POST /api/push/unsubscribe`** — body `{"endpoint": "https://…"}` →
`{"ok": true, "removed": true|false}`.

**`GET /api/push/subscriptions`** → `{"subscriptions": [...]}` — every
subscription the caller owns, in the same shape `subscribe` returns.

**`POST /api/push/test`** — sends "Faustus está conectado" to every
subscription the caller owns and reports per-subscription delivery:
`{"ok": true, "results": [{"id": "…", "device_name": "…", "ok": true,
"status": 201}]}`.

### The payload a service worker's `push` handler receives

Every push body (after the browser's own decryption) is JSON shaped as:

```json
{"title": "Weather chat", "body": "the forecast is sunny…",
 "url": "/studio?s=s1", "kind": "turn_finished", "id": 42}
```

`url` is where the service worker should navigate/focus a tab on click,
derived from the bus event's `kind`:

| kind | url |
|---|---|
| `turn_finished` / `turn_error` | `/studio?s=<session_id>` (or `/studio` if unknown) |
| `approval_pending` / `approval_resolved` | `/studio?s=<session_id>` (or `/studio`) |
| `task_finished` | `/` |
| `reminder` | `/notes` |

`approval_pending` sends with `Urgency: high` (wakes a dozing device sooner
on networks that throttle low-urgency push); every other kind sends
`normal`.

### Bus wiring

`src/notifications.py` gained a small sink registry
(`register_sink`/`unregister_sink`) so a module `app.py` doesn't import
directly — this one — can still react to every `emit()`. Importing
`routes/push_routes.py` (which `app.py` already does, to mount the router)
registers `src/push.py`'s sink exactly once (`push.start()` is idempotent);
the sink resolves the event's `owner`, skips silently if that owner has no
stored subscriptions or `push_enabled` is off, and otherwise
`broadcast()`s the mapped payload above.

Settings (`src/settings.py`): `push_enabled` (default `True` — a kill
switch; a subscription only exists if the browser was granted permission
and the PWA registered one) and `push_contact` (the RFC 8292 VAPID `sub`
claim; empty → `mailto:faustus@localhost`).

A send is best-effort per subscription: **404/410** (the push service has
permanently given up on that endpoint) drops it immediately; **429/5xx**
or a network error counts a failure and drops the subscription after 10
consecutive failures.

### Verification

- `python3 -m pytest tests/test_push.py tests/test_notifications.py -q` — 56
  passed, including the RFC 8291 Appendix A vector matched byte-for-byte
  (salt and ephemeral key injected) and an ES256 VAPID JWT verified against
  its own published public key.
- Full `app.py` import boots with `/api/push/vapid-key`, `/api/push/subscribe`,
  `/api/push/unsubscribe`, `/api/push/subscriptions` and `/api/push/test`
  all present in the route table.

## Installing on the phone (lot P-B)

The Android app (lot M-B) is one way onto a phone; Studio itself installing
as a PWA is the other, and needs no app-store build at all — a browser tab
becomes a home-screen icon that opens full-screen, keeps working offline
for the shell and anything already cached, and can wake with real OS
notifications while closed.

### What ships

- **`static/manifest.json`**, served at **`GET /manifest.webmanifest`**
  (`routes/pwa_routes.py`) — name, icons (`static/pwa/icon-*.png`,
  `maskable-*.png`), `display: "standalone"`, `start_url: "/?source=pwa"`.
- **`static/sw.js`**, served at **`GET /sw.js`** with
  `Service-Worker-Allowed: /` so its scope covers every Studio route, not
  just `/static/` — registered from `static/index.html` as
  `navigator.serviceWorker.register('/sw.js', {scope: '/'})`. Both routes
  are unauthenticated (`app.py`'s `AUTH_EXEMPT_EXACT`): the very first
  visit to the public shell needs them before any session cookie exists.
- The service worker's `push` handler turns the payload shape documented
  above into `self.registration.showNotification(...)`; `notificationclick`
  focuses an existing Faustus tab (navigating it to the notification's
  `url`) or opens a new one; `pushsubscriptionchange` re-subscribes and
  re-registers with `/api/push/*` on its own if the push service ever
  rotates a subscription's keys out from under the browser.

### Installing

1. Open Faustus in a Chromium browser (Chrome, Edge, Android's WebView)
   and either use the browser's own "Install app" menu entry, or open
   **Settings → This device** and press "Install Faustus" — that button
   only appears once the browser has actually offered
   (`beforeinstallprompt`, captured on load by
   `studio/src/lib/installPrompt.ts`).
2. **iOS/iPadOS Safari never fires that event** — there is no native
   install button at all. "This device" instead shows the manual step:
   Share → "Add to Home Screen".
3. Once installed, the app opens standalone (no browser chrome) at
   `/?source=pwa`, themed with the same dark palette as
   `<meta name="theme-color">`.

### Enabling notifications

Also from **Settings → This device**:

- "Enable notifications on this device" asks for `Notification` permission,
  subscribes the browser's `PushManager` with the server's VAPID key
  (`GET /api/push/vapid-key`), and registers the subscription
  (`POST /api/push/subscribe`) under an editable device label.
- "Send a test notification" round-trips through the same bus payload
  shape every real event uses (`POST /api/push/test`).
- The subscribed-devices list (`GET /api/push/subscriptions`) shows every
  device that has ever enabled push for this account, with a remove
  button per row (`POST /api/push/unsubscribe`) — useful for dropping a
  phone that was reset or sold without ever reopening Faustus on it.

### Reaching Faustus away from the local network

The manifest and service worker only make the *app* installable — they do
not, by themselves, make the server reachable from outside the network it
runs on. A push notification still has to be delivered by the browser
vendor's own push service (which needs outbound internet from the
server), and *opening* Faustus from elsewhere needs the server exposed
over HTTPS: a VPN into the home network, or a tunnel to it, set up once by
whoever runs the server. Neither this lot nor "This device" configures
that — it is a prerequisite the settings screen only mentions in passing.

### Verification

- `node studio/checks/pwa.check.mjs` — the manifest link, service-worker
  registration/scope, the service worker's push/notificationclick/
  pushsubscriptionchange handlers, the manifest icons on disk, the adapter's
  exports and the Settings wiring, all checked against the shipped source.
- `python3 -m pytest tests/test_pwa_routes.py -q` — `/sw.js` and
  `/manifest.webmanifest` both come back 200, correctly typed, with
  `Service-Worker-Allowed: /`, against the real `app` and **no** auth
  headers at all.
