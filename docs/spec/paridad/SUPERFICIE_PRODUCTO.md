# Product surface map — session operations (A36)

Contract A36: "Edit, retry, branch/export or share a session through a
documented surface" → "Supported semantics mapped explicitly; unavailable
operations remain open gaps". Below, each operation a user expects from a
session maps to the exact real route that covers it in this tree, or is
named an open gap with what would need to be built.

`tests/acceptance/test_a36_product_surface.py` checks every cited path
below actually exists on the real `app` (`from app import app`, walking
`app.routes` including `fastapi.routing._IncludedRouter`-wrapped
sub-routers — the same "real app, not a rebuilt one" shape
`tests/test_l55_app_wiring.py` uses) — a citation here that stops matching
a real route fails that test instead of silently going stale.

| Operation | Faustus route | File | Semantics |
|---|---|---|---|
| **Edit** a past message | `POST /api/session/{session_id}/edit-message` | `routes/history/history_routes.py::edit_message` | Rewrites one message's content by its DB id in place (`msg_id`, `content`); marks it `metadata.edited = True`; updates both the DB row and the in-memory session history so the two never diverge. Does **not** itself re-run the assistant's reply — combine with Retry (below) to get "edit then redo" behaviour. |
| **Retry** the last answer | `POST /api/chat/regenerate/{sid}` | `routes/chat_routes.py::regenerate_chat_response` | Redoes the session's last assistant answer from the evidence its tool calls already gathered, reinjected as context. Every tool the install cannot prove read-only is refused for the retry run (application-wide, not just the tools the original turn happened to use) — a retry can produce a better-written summary of what happened, never repeat an effect (an email re-sent, a file rewritten). See the route's own docstring for the read-only-effect classification it uses. |
| **Branch** a session | `POST /api/session/{session_id}/fork` | `routes/history/history_routes.py::fork_session` | Creates a NEW session (`id`, name prefixed `⫝̸ <original name>`) that copies messages `[0:keep_count)` from the source, independent from that point on — a true copy-and-diverge branch, not a live-linked reference. A related but semantically DIFFERENT mechanism exists alongside it: `POST /api/session/{session_id}/side-threads` (`routes/side_thread_routes.py::create_side_thread_route`, "excursos") creates a new session anchored to one message of the parent and referencing it (`add_reference`/`src/side_threads.py`) rather than copying — useful for "explore a tangent and cite it back", not for "continue this exact conversation down a different path". Both are documented here so neither is mistaken for the other. |
| **Export** a session | `GET /api/session/{sid}/export?fmt=` | `routes/session_routes.py::export_session` | One conversation, `md`/`txt`/`json`/`html`/`pdf`/`docx` (`src/chat_export.py`'s shared block model — every format renders the same transcript, tool calls and attachments included). |
| **Export** many sessions | `GET /api/sessions/export?project=\|folder=\|ids=` | `routes/session_routes.py::export_sessions_batch` | A whole project/folder/id-list as one `.zip` (one file per conversation + `index.md`), same membership rule the project routes use. |
| **Share** a session (public/external link) | — | — | **Open gap.** No route in this tree issues a shareable link, a read-only public view, or any access grant to a session for someone without a Faustus account/session — confirmed by `app.routes` having no `share`/`public` path anywhere (checked by the same test). What is already there and could be a building block: `GET /api/session/{sid}/export` produces a downloadable file that can be shared manually (no live/updating link, no access control beyond "whoever has the file"); `routes/webhook_routes.py`/API tokens (`routes/api_token_routes.py`) give programmatic, authenticated access, not a public share link. Building a real "share" surface needs, at minimum: a signed/expiring share token, a public (unauthenticated) read route gated by that token, and a revoke path — none of which exist today. |

## Read this alongside

- `docs/spec/paridad/MATRIZ_PARIDAD.md` — the 22-row comparison; this file
  is the operation-level detail behind whichever row a reviewer maps A36
  onto (none of the existing 22 rows named A36 explicitly before this
  lot — session-level edit/retry/branch/export/share was not previously
  itemised anywhere in that matrix).
- `routes/history/history_routes.py` — edit-message, fork, and the
  chat-version history (`/versions`, `/versions/{version_id}/restore`,
  a related but distinct "restore a saved snapshot of the whole
  conversation" mechanism, not covered by this A36 case but adjacent to
  it).
