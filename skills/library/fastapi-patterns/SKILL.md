---
name: fastapi-patterns
description: Layer routes thin, services holding logic, and dependency-injected sessions — plus never block the event loop with a sync call inside an async route. Use when writing or reviewing a FastAPI (or similarly async-Python) HTTP service.
version: 1.0.0
category: engineering
tags: [fastapi, python, backend, api]
status: published
source: imported
---

## When to Use

Writing or reviewing a FastAPI route, dependency, or the service layer
behind it. Layered on top of `python-patterns` and `api-design`.

## Procedure

1. **Keep route handlers thin.** A route parses the request (via a Pydantic
   model, not manual dict access), calls one service function, and shapes
   the response — it does not contain business logic, direct database
   queries, or a multi-step workflow inline. That logic belongs in a
   service function the route calls.
2. **Use Pydantic models (v2) for every request body and response shape.**
   Validation happens automatically at the boundary; a route that reads
   `request.json()` manually has opted out of that safety net for no
   benefit.
3. **Use dependency injection for anything request-scoped**: a database
   session, the current user, a per-request client. A `Depends(...)` that
   yields and cleans up (a session closed in a `finally`) is the standard
   shape — don't open a resource in the route body and forget to close it
   on the error path.
4. **Never run a blocking call inside an `async def` route.** A synchronous
   database driver call, `requests`, or a CPU-heavy loop blocks the entire
   event loop for every other concurrent request. Use an async driver, or
   push the blocking call through a thread pool explicitly.
5. **Put configuration in a `pydantic-settings` model read once at
   startup**, not scattered `os.environ.get()` calls through the codebase —
   one place to see every setting, with types and defaults.
6. **Structure errors as typed exceptions caught by an exception handler**
   that maps them to the right HTTP status and a consistent error shape
   (see `api-design`), rather than each route building its own
   `HTTPException` ad hoc.
7. **Test through the actual HTTP client** (an async test client hitting
   the real app, dependency-overridden for anything external) rather than
   calling service functions directly and skipping the routing/validation
   layer the real traffic goes through.

## Pitfalls

- A sync ORM call or `requests.get()` inside an `async def` route — it
  works in a quick manual test and falls over under real concurrent load,
  because every other request queues behind it.
- Business logic embedded directly in the route function, making it
  untestable without spinning up the whole HTTP layer and impossible to
  reuse from a background job or CLI.
- A dependency that opens a resource but only closes it on the success
  path — an exception raised inside the route leaks the session/connection.
- Manually building the response dict instead of returning a typed Pydantic
  model, silently drifting from the documented response schema over time.
- Reading configuration from `os.environ` in more than one place, so a
  renamed variable breaks in one spot and not another.

## Verification

- Every route's request/response uses a Pydantic model; no raw
  `request.json()` parsing and no untyped response dict for a documented
  endpoint.
- No synchronous blocking call exists inside an `async def` route or
  dependency — checked by reading each I/O call in the diff, not by
  assuming the framework will catch it.
- A dependency that yields a resource closes it in a `finally` (or via
  context manager) that also runs on the exception path.
- The service layer function has a test that doesn't go through HTTP, and
  the route has a test that does, so business logic and wiring are each
  verified independently.
