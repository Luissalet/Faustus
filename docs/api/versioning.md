# API versioning (OPS-06)

This document is the human-readable half of `src/api_version.py`, which is
the single authority for everything below — read that module's docstrings
for the "why", this file for "what a client integrator does with it".

## Headers

| Header | Direction | Meaning |
| --- | --- | --- |
| `X-Faustus-Api-Version` | server → client | The server's current `API_VERSION` (e.g. `2.0`). Sent on `/api/version` and on chat responses. |
| `X-Faustus-Client-Version` | client → server | Optional. A client that sends this identifies its own wire-protocol understanding. Omitting it (every client shipped before this scheme existed) is always treated as compatible. |

## Compatibility rule

- **No header sent** → treated as compatible. This is not a legacy
  allowance that will be tightened later without notice — see
  `deprecations.md` for how a real tightening would be announced.
- **Header sent, version >= `MIN_CLIENT_VERSION`** → request proceeds
  normally. If the version is below the server's current `API_VERSION`, a
  soft adaptation notice is available via
  `api_version.client_adaptation_notice()` (surfaced by whichever route
  wires it in) — the request still succeeds.
- **Header sent, version < `MIN_CLIENT_VERSION`** → `426 Upgrade Required`
  with a message naming both the client's version and the server's floor
  (`api_version.upgrade_required_detail()`). This is the one case this
  scheme exists to give a legible error for, instead of a client parsing a
  stream shape it does not understand and failing ambiguously mid-response.

## Additive vs. breaking

A field is **additive** (no version bump needed) when an old client that
does not know the field can safely ignore it — most chat-event fields added
by the observability lots fall here. A change is **breaking** (requires
raising `MIN_CLIENT_VERSION` in the same change that ships it) when an old
client cannot safely ignore it: a renamed or repurposed existing field, or a
new event type a client must not silently skip.

## OpenAPI

`api_version.openapi_version_extension()` returns the `x-api-version`,
`x-min-client-version` and `x-deprecations` fields meant to be merged into
the generated OpenAPI document's `info` object. As of this lot the function
exists and is tested; wiring it into `app.py`'s actual `app.openapi()`
override is a one-line addition to a file this lot does not own — see the
lot's final report for the exact change.
