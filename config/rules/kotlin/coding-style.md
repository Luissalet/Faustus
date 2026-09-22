---
id: kotlin/coding-style
title: Kotlin coding style
applies_to: [Kotlin]
priority: 32
summary: val over var, data classes for plain carriers, avoid !! outside proven-safe cases.
---

- Prefer `val` over `var`; reach for mutability only when the value
  genuinely needs to change.
- Use a `data class` for a plain data carrier instead of a manual class
  with hand-written `equals`/`hashCode`/`toString`.
- Avoid the not-null assertion (`!!`) except where nullness has just been
  proven impossible and a comment says why.
- Use named arguments for a call with several parameters of the same type,
  to avoid an easy-to-make ordering mistake.
- Prefer an extension function over a static utility method when it reads
  more naturally at the call site.
