---
id: swift/coding-style
title: Swift coding style
applies_to: [Swift]
priority: 32
summary: Value types by default, guard for early exit, avoid force-unwrap outside tests.
---

- Prefer `struct` over `class` unless you specifically need reference
  semantics or identity.
- Use `guard let`/`guard ... else { return }` for early exit instead of
  nesting the rest of the function inside an `if let`.
- Avoid force-unwrap (`!`) and force-try (`try!`) outside test code and
  cases genuinely proven safe with a comment saying why.
- Name Boolean properties/methods so they read naturally at the call site
  (`isEmpty`, `canSubmit`), matching the standard library's convention.
- Keep access control explicit (`private`/`internal`/`public`) rather than
  leaving everything at the default.
