---
name: api-design
description: Resource-oriented URLs, correct HTTP status codes, and a consistent request/response shape, so a new endpoint reads like the ones next to it. Use when designing or reviewing an HTTP API endpoint, especially one other services or a frontend will depend on.
version: 1.0.0
category: engineering
tags: [api, rest, http, design]
status: published
source: imported
---

## When to Use

Adding, changing, or reviewing an HTTP endpoint that other code (a frontend,
another service, a public integration) will call. Less critical for a
purely internal, single-caller RPC, but still worth the status-code
discipline.

## Procedure

1. **Model resources as nouns, plural, kebab/lowercase**: `/orders`,
   `/orders/{id}`, `/orders/{id}/line-items`. Actions that don't map to
   CRUD on a resource are the one place a verb is fine: `/orders/{id}/
   cancel`, as a `POST`.
2. **Use HTTP methods for what they mean.** `GET` never mutates state and is
   safe to retry; `POST` creates or performs a non-idempotent action;
   `PUT`/`PATCH` update (whole vs. partial); `DELETE` removes. A `GET` that
   secretly writes to the database will break the first time something
   caches or prefetches it.
3. **Return the status code that actually describes what happened**: `200`
   for a successful read/update, `201` (with a `Location` header) for a
   created resource, `204` for a successful action with no body, `400`/`422`
   for a validation failure — never `200` with an error described only in
   the body, and never a `500` for something the client did wrong.
4. **Keep the response shape consistent across the whole API**: the same
   envelope for a list vs. a single item, the same error shape everywhere
   (a `code`, a human `message`, and field-level detail for validation
   errors), and the same pagination convention on every list endpoint.
5. **Version deliberately, not accidentally.** Decide up front whether a
   breaking change gets a new version prefix or a new field with the old
   one kept — and stick to whichever the project already does elsewhere.
6. **Validate on the server and describe exactly what's wrong** per field
   for a `422`, not just "invalid request" — the caller (often
   also code, not a person reading the message) needs to know which field
   and why.
7. **Design pagination and filtering as first-class**, not bolted on later:
   a list endpoint that will ever return more than a page's worth of rows
   needs a cursor or offset/limit from day one, because adding it after
   clients depend on "returns everything" is a breaking change.

## Pitfalls

- Using `200` for every outcome and letting the client parse the body to
  find out if it actually failed — this defeats every HTTP-aware client,
  proxy, and monitoring tool that reads the status code.
- A `GET` endpoint with side effects (incrementing a counter, marking
  something read) that breaks the moment a browser prefetches the link or a
  CDN caches the response.
- Inconsistent pluralisation or casing across endpoints (`/getUser` next to
  `/orders`) that makes the API feel like it was written by different
  people at different times, because it usually was.
- Returning different error shapes from different endpoints, forcing every
  client to special-case each one instead of writing a single error
  handler.
- Breaking an existing client silently by changing a field's type or
  removing it, instead of adding a new field and deprecating the old one on
  a schedule.

## Verification

- Every endpoint's status codes match what actually happened, checked by
  triggering both the success and at least one failure path.
- The response shape (success and error) matches the convention already
  used elsewhere in this API — checked against a real sibling endpoint, not
  from memory.
- A validation failure names the specific field and reason, and a client
  could act on that message without guessing.
- Pagination behaves correctly at the boundary (empty result, exactly one
  page, more than one page).
