# @faustus/embed

An embeddable `<faustus-chat>` Web Component: one isolated chat session per
element, built on `faustus-sdk` (`sdk/ts`). Meant for a host application
that wants to drop a Faustus chat panel into its own page — possibly more
than once, each with its own session and its own user's token — without
pulling in the full Studio (`studio/`) app, its router, or its global CSS.

## Usage

```html
<script type="module">
  import '@faustus/embed'; // registers <faustus-chat>
</script>

<faustus-chat id="chat" server="https://faustus.example" theme="auto"></faustus-chat>
<script type="module">
  // Prefer setting the token as a PROPERTY, never as markup — see "Auth"
  // below for why.
  document.getElementById('chat').token = myUsersBearerToken;
</script>
```

Attributes / properties:

| Name      | Type                        | Notes                                                                 |
|-----------|-----------------------------|------------------------------------------------------------------------|
| `server`  | `string` (required)         | Base URL the SDK talks to.                                            |
| `token`   | `string \| null`            | Bearer token. Stripped from the DOM the instant it is read — see Auth. |
| `session` | `string \| null`            | An existing session id to attach to. Omit to create a new one lazily.  |
| `theme`   | `'light' \| 'dark' \| 'auto'` | Default `'auto'` (`prefers-color-scheme`).                            |
| `mode`    | `'chat' \| 'agent'`          | Default `'agent'`.                                                    |

Slots: `header`, `footer`, `empty` (shown before the first message is sent).

Events (bubble, `composed: true` — they cross the shadow boundary so the
host page can listen on the element itself):

- `faustus:turn-start` — `detail: {sessionId, runId}`
- `faustus:turn-end` — `detail: {sessionId, reason}`
- `faustus:error` — `detail: {sessionId, message, cause}`
- `faustus:event` — `detail`: every raw decoded SSE event from `faustus-sdk`, for a host that wants more than the three above.

Methods: `.send(message)`, `.cancel()`. Read-only: `.sessionId`, `.busy`.

## Isolation

Each `<faustus-chat>` instance:

- owns its own `FaustusClient` (own base URL, own token) — nothing is
  module-level state shared between instances;
- renders into its own Shadow DOM (`mode: 'open'`, but still a real shadow
  boundary) — the host page's stylesheets never leak in, and this
  component's `<style>` never leaks out;
- registers no router, no global event bus, no singleton of any kind.

Two instances on the same page with two different `server`/`token`/`session`
values are two fully independent chat sessions. See
`studio/checks/embed.check.mjs` for the check that proves this (mounts two
instances against a fake server and asserts neither's events, transcript
text, or token ever reach the other), and
`tests/acceptance/test_a21_embed_two_sessions.py` for the same property
proven against a real Faustus server (real session ownership, real 403/404
on a cross-token read) and, where Chromium is available, a real browser
driving two `<faustus-chat>` panels via Playwright.

## Auth

The bearer token is **never** written to `localStorage`, `sessionStorage`,
or any URL, and it does not linger in the light DOM:

- Setting it as a property (`el.token = '…'`) never touches the DOM at all.
- Setting it as an HTML attribute (`<faustus-chat token="…">`) is supported
  for convenience, but the element reads the attribute exactly once
  (`connectedCallback`/`attributeChangedCallback`) and immediately
  `removeAttribute('token')`s it — so `outerHTML`/`innerHTML` never show it
  after the element has mounted. Prefer the property form in a host page
  that can set it in script, since the attribute form is briefly visible to
  any code that inspects the DOM in the same tick it is set.
- The token only ever leaves this component as an `Authorization: Bearer …`
  request header (via `faustus-sdk`'s `HttpContext`) — never as a query
  parameter, never logged.

## CORS

A host page embedding `<faustus-chat>` is, from the Faustus server's point
of view, a **cross-origin** request (a different scheme/host/port than the
server itself almost always) carrying a custom `Authorization` header —
that triggers a real CORS preflight. The server does **not** and must not
respond with a permissive `allow_origins: ["*"]` for a credentialed/bearer
API; it needs the host page's exact origin listed explicitly.

`app.py` already supports this — no server code changes were needed for
A21 (checked before writing anything: `app.py`'s `CORSMiddleware` reads
`allow_origins` from the `ALLOWED_ORIGINS` environment variable at process
start, defaulting to `http://localhost,http://127.0.0.1`). To embed
`<faustus-chat>` from `https://intranet.example`, start (or configure) the
Faustus server with:

```
ALLOWED_ORIGINS=https://intranet.example,http://localhost,http://127.0.0.1
```

(comma-separated, exact origins — scheme + host + port, no path, no
wildcard). `tests/acceptance/test_a21_embed_two_sessions.py`'s Playwright
test exercises exactly this: it serves `example.html` on its own origin,
sets that origin in the real server's `ALLOWED_ORIGINS`, and the turns only
complete because the browser's preflight actually succeeds — a CORS
misconfiguration there would show up as the transcript assertions failing,
not as a separately-mocked check.

If a deployment wants a friendlier, per-request-configurable version of
this (e.g. an admin UI setting instead of an environment variable, or an
allowlist keyed by API token rather than one process-wide list), that is a
real product change to `app.py`/`core/` this lot did not make — see "Scope"
below.

## Build

```
cd studio/embed
node build.mjs          # or: npm run build
node build.mjs --watch
```

Produces `dist/faustus-embed.js` (a single dependency-free ESM bundle —
esbuild inlines `sdk/ts/src/*.ts` directly, so this never needs `sdk/ts`
pre-built) and ships a hand-written `dist/faustus-embed.d.ts` alongside it
(not `tsc`-generated — this package's public surface is small enough that a
generated `.d.ts` would not add anything a short hand-written one doesn't
already say more plainly).

## Scope

This lot (A21) shipped the Web Component, its build, its checks, and the
documentation above. It deliberately did **not**:

- add a new server-side setting (`embed_allowed_origins` or similar) —
  `ALLOWED_ORIGINS` already does the job the design doc asked for;
- touch `app.py` or `core/` at all (no wiring gap: nothing here needed a
  change to a file this lot doesn't own — see the contract's file-ownership
  table, `studio/embed/` + `sdk/ts/` additive is U7's whole scope);
- build a richer approval UI (attachments, `revision` conflict retry beyond
  what `faustus-sdk` itself already handles, voice, etc.) — only the
  baseline `ask_user` (question + `tool_approval`) response the blueprint
  asked for.
