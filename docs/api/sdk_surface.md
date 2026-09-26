# The `sessions` token scope — a paired SDK client's surface (S1.1)

Before this lot, a bearer `ody_` token reached only `POST /api/v1/chat`
(one prompt, one answer — no streaming, no tools), `GET /api/models`, and
the codex-skill and dispatch families. A client that wanted the full chat
cycle — create a session, stream a turn over SSE, answer an in-turn tool
approval, cancel, resume, read what a turn produced — had to authenticate
as a browser cookie session instead. This document is the table of what the
new `sessions` scope opens for that client, the ownership rule everything
on it follows, and what deliberately stays out.

`core/authz.py` is the source of truth this table transcribes — deny by
default, so a route not in the table is exactly as closed to a token as it
was before this lot, with a 403 that says so
(`api_token_allowed`'s "not part of the API-token surface" reason). Nothing
here changes an existing route's path or response body; this lot only opens
and verifies the surface an already-shipped route offers.

## Minting a token for this surface

```
POST /api/tokens
Content-Type: application/x-www-form-urlencoded (Form fields)

name=my-sdk-client
profile=sdk
```

`profile=sdk` expands to `["chat", "sessions"]` (`routes/api_token_routes.py`
`TOKEN_PROFILES["sdk"]`) — `chat` for the older non-streaming
`POST /api/v1/chat`, `sessions` for everything below. Scopes can also be
named explicitly (`scopes=chat,sessions`) instead of `profile=sdk`; the two
forms produce the same token. Minting requires an admin cookie session
(`POST /api/tokens` itself is `require_admin`, unchanged by this lot) — a
token cannot mint another token. The response's `token` field is the raw
secret, shown exactly once; only its hash is stored.

## The ownership rule

Every route below resolves its data through `src/auth_helpers.effective_user`,
never the raw authenticated principal. For a cookie session the two are the
same thing. For a bearer token, the raw principal is always the sandboxed
pseudo-user `"api"` (so a token can never wander into cookie-only routes by
accident) — `effective_user` looks past that to
`request.state.api_token_owner`, the real person who minted the token.

So a paired SDK client sees, creates, and modifies exactly what its owner's
own desktop session sees — the SAME sessions, history, and artifacts, not a
separate `"api"`-owned silo. A session created by token A is visible under
A's owner's cookie; a second token minted for a different owner gets a 404
on it (not a 403 — existence is not leaked across owners), the same answer
a foreign cookie session gets. A token missing the `sessions` scope never
reaches any route below at all — the 403 names the missing scope.
(`tests/test_sdk_surface_authz.py` pins all three.)

## The surface

| Method | Route | Scope | Effect | What it does |
| --- | --- | --- | --- | --- |
| POST | `/api/session` | `sessions` | reversible | Create a session owned by the token's owner. |
| GET | `/api/sessions` | `sessions` | read | List the token owner's own sessions. There is no `GET /api/session/{sid}` in this app at all (browser and SDK alike load one session's state from the routes below) — the SDK lists, then reads. |
| PATCH | `/api/session/{sid}` | `sessions` | reversible | Rename, re-home to a folder, or switch an owned session's model/endpoint. |
| DELETE | `/api/session/{sid}` | `sessions` | reversible | Delete an owned session (and its messages). |
| GET | `/api/session/{sid}/connectors` | `sessions` | read | An owned session's MCP connector allowlist, resolved. |
| PATCH | `/api/session/{sid}/connectors` | `sessions` | reversible | Change an owned session's connector allowlist. |
| GET | `/api/session/{sid}/tool-support` | `sessions` | read | Whether an owned session's current model/endpoint can use tools at all. |
| GET | `/api/session/{session_id}/context_info` | `sessions` | read | An owned session's real model context length. |
| GET | `/api/session/{session_id}/usage` | `sessions`, `agents:dispatch` | read | An owned session's usage per model: tokens, prompt cache, steps, tool calls, time, cost. |
| GET | `/api/session/{session_id}/turn_review?turns=N` | `sessions`, `agents:dispatch` | read | What an owned session's last N turns did (tools, failures, rounds, writes, cache), with findings. |
| GET | `/api/history/{session_id}` | `sessions` | read | An owned session's message history — the same route the desktop app itself loads a chat from. |
| GET | `/api/session/{sid}/export` | `sessions` | read | Download one owned session's rendered conversation (`?fmt=md\|txt\|json\|html\|pdf\|docx`). The bytes are also recorded as an artifact (`session_id` set), so `client.artifacts.list({sessionId})` finds it right after. |
| POST | `/api/chat_stream` | `sessions` | external (executes tools) | Run a turn: send a message, or answer an in-turn `ask_user`/`tool_approval`. Streams the events `docs/api/sse_events.json` catalogues. |
| GET | `/api/chat/resume/{session_id}` | `sessions` | read | Reattach to an owned session's in-flight turn (replays from `?cursor=`). |
| GET | `/api/chat/stream_status/{session_id}` | `sessions` | read | Whether an owned session has an active stream right now. |
| GET | `/api/chat/activity` | `sessions` | read | The token owner's own running/queued turns across all sessions. |
| POST | `/api/chat/stop/{session_id}` | `sessions` | reversible | Cancel an owned session's turn (generation/task/work scope, see the route's own docstring). |
| POST | `/api/chat/pause/{session_id}` | `sessions` | reversible | Pause an owned session's generation. |
| POST | `/api/chat/steer/{session_id}` | `sessions` | reversible | Send a steering instruction into an owned session's live turn. |
| GET | `/api/questions` | `sessions` | read | The token owner's own open `ask_user` questions across sessions. |
| GET | `/api/approvals/pending` | `sessions` | read | The token owner's own pending approval cards. Scoped to the token's own owner only — a token never sees another owner's cards, even though a cookie-session admin reviewing this same route can pass any `owner=`. |
| GET | `/api/approvals/active` | `sessions` | read | The token owner's own currently-granted approval cards, same owner scoping as above. |
| GET | `/api/artifacts` | `sessions` | read | The token owner's own artifacts. |
| GET | `/api/artifacts/{artifact_id}` | `sessions` | read | One owned artifact's metadata. |
| GET | `/api/artifacts/{artifact_id}/download` | `sessions` | read | Download one owned artifact's bytes. |
| GET | `/api/artifacts/{artifact_id}/manifest` | `sessions` | read | One owned artifact's version chain. |
| GET | `/api/artifacts/{artifact_id}/provenance` | `sessions` | read | One owned artifact's provenance (recipe, source artifacts). |
| GET | `/openapi.json` | `sessions` | read | The live OpenAPI document, for generating client types (`scripts/export_openapi.py` writes the same document offline). |

`GET /api/version` needs no scope at all — it is exempt from auth entirely,
the one endpoint a client has to be able to reach before it knows anything
about the server it is talking to.

## Answering an in-turn tool approval

`ask_user` events of `kind: "tool_approval"` (a plan step that needs a
person to approve before it runs) are answered the same way for a token as
for a cookie session: another `POST /api/chat_stream` call carrying
`tool_approval_id` + `tool_approval_decision` (`approve` / `approve_task` /
`deny`) in the form body. This is deliberately **not** gated by
`require_human` — that gate exists to keep Faustus's own in-process agent
tool loopback from granting its own approvals, and a bearer token minted by
a person is not that loopback. `tests/test_sdk_surface_authz.py` asserts
`routes/chat_routes.py` never imports `require_human` at all, and drives a
real turn's tool-approval answer through a token end to end.

What stays `require_human` — a card's **grant/deny/revoke**
(`POST /api/approvals/{id}/grant`, `.../deny`, `DELETE /api/approvals/{id}`)
— is the pending-card flow a human reviews out of band (approving a plan
before an agent-mode turn runs at all), a different mechanism from the
in-turn `tool_approval` above. It is not part of the token surface in this
lot: the whole point of that gate is that a person, not an automated
client, makes that call.

## What stays out, and why

- **Approval card decisions** — `POST /api/approvals/{id}/grant`,
  `.../deny`, `DELETE /api/approvals/{id}`, and opening a new card
  (`POST /api/approvals/request`) — `require_human`, unchanged (see above).
- **Settings, model/endpoint management, projects, connector installation,
  skills, backups, research/audit runs, token management itself** — never
  part of the API-token surface at all, before or after this lot; a bearer
  token is minted for driving chat sessions, not for administering the
  instance it runs on.
- **Mutating an artifact's review state or deleting it**
  (`POST /api/artifacts/{id}/review`, `DELETE /api/artifacts/{id}`) — only
  reading is opened in this lot; the SDK's own job is reading what a turn
  produced, not curating the library.

## Ejemplo verificado (A20)

`tests/acceptance/test_a20_external_sdk_consumer.py` es la evidencia de que
esta superficie funciona de punta a punta contra un servidor real: arranca
Faustus real (`uvicorn`, `AUTH_ENABLED=true`) en un subproceso, mintea un
token `sdk` real por `POST /api/tokens`, empaqueta `sdk/ts` con `npm pack` e
instala el `.tgz` resultante en dos proyectos Node limpios (uno ESM, uno
CommonJS, sin acceso a red — `sdk/ts/examples/esm` y `.../examples/cjs`), y
ejecuta ese consumidor instalado contra el servidor: crear sesión, mandar un
turno con `workspace`, aprobar un `tool_approval` en curso, seguir el turno
hasta `[DONE]`, exportar la sesión (`client.sessions.export`) y verificar
`artifacts.list/get/download` con el `sha256` real; más una cancelación
(`turn.cancel('task')` + `turns.resume()` lanzando `RunNotActiveError`) y un
token sin scope `sessions` recibiendo 403. Reproducir:

```
python3 -m pytest tests/acceptance/test_a20_external_sdk_consumer.py -q -p no:cacheprovider
```

## See also

- `docs/api/sse_events.json` / `docs/api/versioning.md`#SSE event catalogue
  — the versioned list of every event `type` `POST /api/chat_stream` and
  `GET /api/chat/resume/{session_id}` can send, and the stability rule for
  `core` vs. `extended` events.
- `docs/api/versioning.md` — the `X-Faustus-Api-Version` /
  `X-Faustus-Client-Version` negotiation every route above (like every
  other `/api/*` route) participates in, and the `x-api-version` OpenAPI
  extension `scripts/export_openapi.py` writes out.
- `core/authz.py` — the actual matrix (`API_TOKEN_RULES`) this table
  transcribes; it is the file to change if this table and reality ever
  disagree.
