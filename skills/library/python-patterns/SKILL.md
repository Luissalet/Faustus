---
name: python-patterns
description: Idiomatic Python defaults — type hints, EAFP error handling, context managers, dataclasses over ad-hoc dicts — for readable, maintainable Python code. Use when writing or reviewing Python, especially in a codebase that doesn't already dictate its own conventions.
version: 1.0.0
category: engineering
tags: [python, patterns, style]
status: published
source: imported
---

## When to Use

Writing or reviewing Python code as the language-specific default layered
on top of `coding-standards`. Defer to the project's own linter config and
existing style where they differ from any of this.

## Procedure

1. **Type-hint public functions and dataclass fields.** Modern built-in
   generics (`list[str]`, `dict[str, int]`, `X | None`) on 3.9+; `Protocol`
   for structural typing when you need duck-typing with a checkable shape
   instead of a formal ABC.
2. **Prefer EAFP over LBYL where it reads cleanly**: `try`/`except` around
   the operation that might fail beats pre-checking every condition that
   could make it fail, especially for race-prone checks like "does this
   file exist" (it can vanish between the check and the use).
3. **Catch specific exceptions**, never a bare `except:` — see
   `error-handling` for the full treatment.
4. **Use `dataclasses` (or `pydantic` models where validation matters)**
   instead of passing around loosely-shaped dicts — a typed field catches a
   typo'd key at development time instead of a `KeyError` at 2am.
5. **Use context managers for anything with a teardown**: files, locks,
   database sessions, temporary state. Write a custom one (`@contextmanager`
   or a class with `__enter__`/`__exit__`) rather than a manual
   try/finally scattered across call sites.
6. **Prefer composition and explicit dependencies over inheritance** for
   sharing behaviour — a class that takes its collaborators as constructor
   arguments is easier to test and reason about than one that inherits a
   deep mixin chain.
7. **Use `asyncio` consistently once you're in it** — a sync call that
   blocks inside an async function stalls the whole event loop; wrap a
   necessarily-blocking call (`requests`, a CPU-bound loop) in
   `asyncio.to_thread` rather than calling it directly.
8. **Keep configuration explicit** (constructor args, environment read once
   at startup) rather than reaching for a global mutable singleton mid-call
   — hidden global state is the single hardest thing to test around.

## Pitfalls

- Mutable default arguments (`def f(items=[])`) — the same list object is
  reused across every call that didn't pass one explicitly, and it
  accumulates state nobody expected.
- Catching `Exception` broadly "to keep the request alive" and silently
  swallowing programming errors that should have crashed loudly in
  development.
- Blocking I/O inside an `async def` without `await`ing it through
  something that yields control — the symptom is a service that goes
  unresponsive under load with no obvious single culprit.
- A dict with string keys standing in for what should be a small dataclass
  — the moment a second caller needs the same shape, the missing type
  becomes a bug source.
- Wildcard imports (`from module import *`) that make it impossible to tell
  where a name came from by reading the file alone.

## Verification

- `mypy`/`pyright` (if configured) passes on the changed files with no new
  suppressions added to make it pass.
- No bare `except:` and no mutable default argument exist in the diff.
- Every blocking call inside async code is either awaited through a
  non-blocking wrapper or deliberately justified in a comment.
