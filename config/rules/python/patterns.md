---
id: python/patterns
title: Python patterns
applies_to: [Python]
priority: 34
summary: Never block the event loop; catch specific exceptions; compose over inheritance.
---

- Never call a blocking I/O function inside `async def` without awaiting it
  through something that yields control (`asyncio.to_thread` for a
  necessarily-blocking call) — it stalls the whole event loop.
- Catch specific exception types; never a bare `except:`.
- Prefer composition (pass collaborators into the constructor) over a deep
  inheritance/mixin chain for sharing behaviour.
- Use `Protocol` for structural typing when you need duck-typing with a
  checkable shape, rather than a formal ABC nobody implements directly.
- Prefer a generator/iterator over building a full list in memory for
  anything that can be large or unbounded.
- Close what you open via a context manager, not a manual call you might
  forget on an error path.
