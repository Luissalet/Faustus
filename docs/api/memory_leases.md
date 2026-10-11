# Memory leases (facts with an expiry)

Facts in `memory.json` (the flat list of things the user told Faustus) can carry a lease: a kind and an expiry date. The logic lives in `src/memory_leases.py` and uses only local rules, never a model call. Facts without a `kind` (anything saved before leases existed) have no lease and behave exactly as before.

## Lease fields on an entry

| Field | Meaning |
| --- | --- |
| `kind` | `location`, `status`, `job`, `project` or `preference` (empty on old entries) |
| `lease_days` | length of the lease in days, 1 to 3650 (absent when the kind never expires) |
| `confirmed_at` | epoch of the last time the fact was said or renewed |
| `lease_until` | epoch when the lease ends |
| `closed_at`, `superseded_by` | set when newer evidence of the same kind replaced the fact |

Default leases: `location` and `status` 30 days, `project` 90 days, `job` 365 days, `preference` none. Override with the setting `memory_lease_days` (for example `{"job": 180, "preference": 0}`; values above 3650 are clamped); `memory_leases_enabled` turns the whole feature off and `memory_lease_review_window_days` (default 7) is how far ahead a fact counts as "expiring".

## What the model sees

An expired fact is not dropped and not presented as current: it is sent marked `User lives in Madrid (as of 2026-08-31, may be outdated)`, with the day in the user's time zone. A fact closed by newer evidence is left out. Everything else is injected unchanged. The filter is applied in the chat memory block, the context engine memory source, the memory provider recall and the agent `manage_memory` list/search.

Switching `memory_leases_enabled` off removes the filter altogether: closed facts are injected again and nothing is marked.

## What can close what

A wrong closure silently removes a true fact from the prompt, so the rules are narrow.

- Only `location` and `job` are single-valued: a newer fact in the same slot closes the older one (per owner). Forgetting the newer fact reopens the older one.
- `status` is **not** single-valued. "On holiday", "currently learning Rust" and "currently drinks tea" are independent phases; a status only expires, it never closes another status. `project` and `preference` can also have several open facts.
- The subject has to be the user (`User ...`, `The user ...`, `I ...`, `Yo ...`, `El usuario ...`, or no subject at all). A possessive ("User's sister lives in Berlin", "The user's brother ...", "Luis's wife works at ...", "El hermano del usuario ...", "Su hermana ..."), a name or any other subject is someone else's fact and gets no kind.
- A place needs a proper noun after *lives in / vive en / based in / moved to* ("lives in the moment" is not a place). Only a residence closes a residence: "currently in Berlin" or "staying in Lisbon" get a lease of their own but never replace where the user lives.
- A job is an employer (a proper noun after *works at/for*, *trabaja en/para*) or a role (*works as a ...*, *trabaja como ...*, *job title is ...*); employer and role are two slots. A side activity ("works at night", "works at home on Fridays", "trabaja en Faustus por las noches") is not the job.
- Only what is true NOW closes anything. A wish, a plan, the future, the past, a hypothesis, an application, a negation or a search gets no single-valued kind and closes nothing: "wants to / would like to / hopes to / plans to / is going to live in Tokyo", "will live in Berlin next year", "is thinking of / dreams of living in ...", "applied to work at Google", "is looking for a job working at Google", "used to live in Paris", "no longer works at Acme", "doesn't live in Madrid anymore", and in Spanish "quiere / le gustaría / piensa / va a / vivirá / solía / antes vivía / ya no / busca trabajo / ha solicitado". The marker has to be in the same clause before the place or employer, so "used to live in Paris and now lives in Berlin" still replaces the old place. A relative clause belongs to somebody else ("User met Ana, who lives in Berlin", "una amiga que trabaja en Google"), and two candidates ("lives in Madrid or Berlin") are not a statement.
- Vague sentences ("User is sick of Python", "User stays at hotels when travelling", "User dreams of moving to Berlin") get no kind.

## Older facts: opt-in backfill

Nothing is ever rewritten when the file is loaded. `backfill` gives the untyped facts a kind and a lease **on request**: a dry run by default that lists what would change (kind, lease days, the state it would land in and which facts it would close), and writes only with `apply`. `basis=recorded` (default) counts the lease from when the fact was saved, so an old "User lives in Madrid" lands expired and waits in the review list; `basis=now` starts every lease today. The review also reports `untyped: {total, typable}`.

## REST

- `POST /api/memory/add` accepts the optional `kind` and `lease_days` (JSON body or form fields; 400 for an invalid kind, a non-number, a negative value or more than 3650) and returns `id` and a `lease` block. Adding the same text again renews it.
- `GET /api/memory` annotates every entry that has a kind or is closed with a `lease` block (`state` is `none`, `active`, `expiring`, `expired` or `closed`, plus `days_left`, `lease_until`, `confirmed_at`).
- `GET /api/memory/leases?window_days=7` (400 for a negative value, like the MCP tool) returns `{expired, expiring, closed, counts, untyped, settings}`.
- `POST /api/memory/leases/{memory_id}/renew` (form field `days`, optional, at most 3650) restarts the lease and reopens a closed fact; 400 if the fact has no lease and no `days` is given, 404 if unknown.
- `POST /api/memory/leases/{memory_id}/forget` deletes the fact.
- `POST /api/memory/leases/backfill` (form fields `apply`, default false, and `basis`) as described above; only the caller's own facts.

## Automatic extraction

When the chat extractor finds a fact the user already has and its lease expired (or is about to), the lease is renewed instead of the repeat being dropped. A fact that merely looks alike ("lives in Paris" next to "lives in Madrid", or "User lives in Berlin" next to "User's sister lives in Berlin.") is a different claim and is stored. A fact that a newer one replaced is reopened only by the very same text.

## MCP (`mcp_servers/memory_server.py`)

- `manage_memory` `add` takes `kind` and `lease_days`; `list` and `search` mark expired facts and hide closed ones.
- `memory_leases` with `action` = `review` (optional `window_days`, a whole number of 0 or more), `renew` (`memory_id`, optional `days`), `forget` (`memory_id`) or `backfill` (`apply`, `basis`; dry run unless `apply` is true).

## Studio

The Memory screen has a "Caducados / por revisar" section listing expired and expiring facts with Renew and Forget buttons, a collapsible list of facts replaced by newer ones (with Renew, which brings one back, and Forget), and, when older facts could get a lease, a note with a preview of what would change and an Apply button. Each fact row shows a lease badge.

## Not covered

The learned-rules store (`src/memory_engine.py`, with its own `valid_until` and conflict handling) is separate and unchanged.
