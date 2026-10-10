# Memory leases (facts with an expiry)

Facts in `memory.json` (the flat list of things the user told Faustus) can carry a lease: a kind and an expiry date. The logic lives in `src/memory_leases.py` and uses only local rules, never a model call. Facts without a `kind` (anything saved before leases existed) have no lease and behave exactly as before.

## Lease fields on an entry

| Field | Meaning |
| --- | --- |
| `kind` | `location`, `status`, `job`, `project` or `preference` (empty on old entries) |
| `lease_days` | length of the lease in days (absent or `null` when the kind never expires) |
| `confirmed_at` | epoch of the last time the fact was said or renewed |
| `lease_until` | epoch when the lease ends |
| `closed_at`, `superseded_by` | set when newer evidence of the same kind replaced the fact |

Default leases: `location` and `status` 30 days, `project` 90 days, `job` 365 days, `preference` none. Override with the setting `memory_lease_days` (for example `{"job": 180, "preference": 0}`); `memory_leases_enabled` turns the whole feature off and `memory_lease_review_window_days` (default 7) is how far ahead a fact counts as "expiring".

## What the model sees

An expired fact is not dropped and not presented as current: it is sent marked `User lives in Madrid (as of 2026-08-31, may be outdated)`. A fact closed by newer evidence is left out. Everything else is injected unchanged. The filter is applied in the chat memory block, the context engine memory source, the memory provider recall and the agent `manage_memory` list/search.

## Single-valued kinds

`location`, `status` and `job` hold one current value per owner. A newer fact of the same kind closes the older one (`closed_at`, `superseded_by`); forgetting the newer one reopens the older. `project` and `preference` can have several open facts at once.

## REST

- `POST /api/memory/add` accepts the optional `kind` and `lease_days` (400 for an invalid kind or negative days) and returns `id` and a `lease` block. Adding the same text again renews it.
- `GET /api/memory` annotates every entry that has a kind or is closed with a `lease` block (`state` is `none`, `active`, `expiring`, `expired` or `closed`, plus `days_left`, `lease_until`, `confirmed_at`).
- `GET /api/memory/leases?window_days=7` returns `{expired, expiring, closed, counts, settings}`.
- `POST /api/memory/leases/{memory_id}/renew` (form field `days`, optional) restarts the lease; 400 if the fact has no lease and no `days` is given, 404 if unknown.
- `POST /api/memory/leases/{memory_id}/forget` deletes the fact.

## MCP (`mcp_servers/memory_server.py`)

- `manage_memory` `add` takes `kind` and `lease_days`; `list` and `search` mark expired facts and hide closed ones.
- `memory_leases` with `action` = `review` (optional `window_days`), `renew` (`memory_id`, optional `days`) or `forget` (`memory_id`).

## Studio

The Memory screen has a "Caducados / por revisar" section listing expired and expiring facts with Renew and Forget buttons, and a collapsible list of facts replaced by newer ones. Each fact row shows a lease badge.

## Not covered

The learned-rules store (`src/memory_engine.py`, with its own `valid_until` and conflict handling) is separate and unchanged.