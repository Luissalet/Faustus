---
id: typescript/coding-style
title: TypeScript coding style
applies_to: [TypeScript]
priority: 32
summary: No `any`, prefer `unknown` and narrow it, strict null checks on.
---

- Avoid `any`; use `unknown` and narrow it with a type guard when the shape
  isn't known up front.
- Keep `strict`/`strictNullChecks` on in `tsconfig.json`; don't silence a
  type error with a non-null assertion (`!`) without checking it's actually
  safe.
- Prefer `interface` for object shapes that might be extended, `type` for
  unions/aliases/utility types.
- Model a value that can be one of several distinct shapes as a
  discriminated union, not an object with several optional fields.
- Avoid `as` type casts except at a genuine boundary (parsing external
  JSON) where the runtime shape has just been validated.
- Name a function for its return behaviour when it's async
  (`fetchUser`, not `getUser`, if it performs I/O).
