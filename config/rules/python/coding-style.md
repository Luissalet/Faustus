---
id: python/coding-style
title: Python coding style
applies_to: [Python]
priority: 32
summary: Type-hint public functions, prefer EAFP, avoid mutable default arguments.
---

- Type-hint public functions and dataclass fields; use built-in generics
  (`list[str]`, `X | None`) on 3.9+.
- Prefer EAFP (`try`/`except` around the operation) over pre-checking every
  condition (LBYL), especially for anything race-prone like "does this file
  exist".
- Never use a mutable default argument (`def f(items=[])`) — use `None` and
  create the default inside the function.
- Use `dataclasses` or a validation model instead of passing around a
  loosely-shaped dict for anything with more than one or two fields.
- Use a context manager for anything with a teardown (file, lock, session)
  rather than a manual try/finally.
- No wildcard imports (`from module import *`).
- Keep configuration explicit (constructor args, env read once at startup)
  rather than a global mutable singleton read mid-call.
