# API versioning (OPS-06)

This document is the human-readable half of `src/api_version.py`, which is
the single authority for everything below — read that module's docstrings
for the "why", this file for "what a client integrator does with it".

## Headers

| Header | Direction | Meaning |
| --- | --- | --- |
| `X-Faustus-Api-Version` | server → client | The server's current `API_VERSION` (e.g. `2.0`). Sent on every `/api/*` response — `core/middleware.py`'s `SecurityHeadersMiddleware` stamps it centrally (Lote 61), so no individual route has to remember to; the pre-existing per-route stamps on chat responses and `/api/version` are now redundant but harmless. |
| `X-Faustus-Client-Version` | client → server | Optional. A client that sends this identifies its own wire-protocol understanding. Omitting it (every client shipped before this scheme existed) is always treated as compatible. |

## Compatibility rule

- **No header sent** → treated as compatible. This is not a legacy
  allowance that will be tightened later without notice — see
  `deprecations.md` for how a real tightening would be announced.
- **Header sent, version >= `MIN_CLIENT_VERSION`** → request proceeds
  normally. If the version is below the server's current `API_VERSION`, a
  soft adaptation notice is available via
  `api_version.client_adaptation_notice()` — surfaced today on
  `GET /api/version`'s `client_adaptation_notice` field — the request still
  succeeds.
- **Header sent, version < `MIN_CLIENT_VERSION`** → `426 Upgrade Required`
  with a message naming both the client's version and the server's floor
  (`api_version.upgrade_required_detail()`). Enforced centrally in
  `core/middleware.py::SecurityHeadersMiddleware` for EVERY `/api/*`
  endpoint (Lote 61) — not just chat — except the two discovery endpoints a
  rejected client still has to be able to reach: `/api/version` and
  `/api/health`. This is the one case this scheme exists to give a legible
  error for, instead of a client parsing a stream shape it does not
  understand and failing ambiguously mid-response.

## Additive vs. breaking

A field is **additive** (no version bump needed) when an old client that
does not know the field can safely ignore it — most chat-event fields added
by the observability lots fall here. A change is **breaking** (requires
raising `MIN_CLIENT_VERSION` in the same change that ships it) when an old
client cannot safely ignore it: a renamed or repurposed existing field, or a
new event type a client must not silently skip.

## OpenAPI

`api_version.openapi_version_extension()` returns the `x-api-version`,
`x-min-client-version`, `x-api-version-header`, `x-client-version-header`
and `x-deprecations` fields, merged into the generated OpenAPI document's
`info` object by `app.py`'s `app.openapi` override (Lote 61) — visible at
`GET /openapi.json`. `scripts/export_openapi.py` writes that same document
to a file for a client generator to read offline (S1.3).

## SSE event catalogue

`docs/api/sse_events.json` is the versioned list of every `type` that can
arrive as a server-sent event on `POST /api/chat_stream` and
`GET /api/chat/resume/{session_id}` — the machine-readable half of the wire
contract this document describes in prose. `src/sse_catalog.py` reads it
back at runtime (`load_catalog()`, `event_types()`, `core_event_types()`);
`tests/test_sse_catalog.py` is the guard that fails the build the moment a
source file emits a `type` the JSON does not know about.

- **`schema_version`** is the same string every SSE envelope stamps as its
  own `schema_version` field (`src/agent_runs.py`'s `_observability_fields`)
  and always equals `API_VERSION` above — `src/sse_catalog.py` asserts this
  at import time, so the two cannot drift apart silently.
- **`stability: "core"`** events are the ones a generated client types
  explicitly. Stability rule: a core event's `type` and `fields` never
  change in a way an old client cannot safely ignore without a
  `MIN_CLIENT_VERSION` bump, same as any other breaking change above — a
  new field on a core event is additive and fine; renaming or repurposing
  one is not. A core event is never *removed* without first being announced
  as deprecated (`deprecations.md`).
- **`stability: "extended"`** events (research/document-streaming/harness
  internals, and the raw provider-stream passthrough frames a "chat"-mode
  turn forwards verbatim from `src/llm_core.py`) may change shape or
  disappear between minor versions. A generated client types these as one
  `UnknownEvent` catch-all it passes through rather than parses.
- **The envelope, `[DONE]`, and the framing block** (`sequence`, `trace_id`,
  `step_id`, `stream_id`, `schema_version`; the `data: <json>\n\n` line
  shape; the literal terminator frame `data: [DONE]`; the named SSE events;
  the heartbeat) are part of the contract exactly as `core` events are —
  the catalogue's own `envelope`/`framing` blocks are the source of truth
  for their shape, not this prose.
