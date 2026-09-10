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
`GET /openapi.json`.
