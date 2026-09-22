---
id: php/coding-style
title: PHP coding style
applies_to: [PHP]
priority: 32
summary: Typed properties and return types, PSR-12 formatting, avoid global mutable state.
---

- Declare types on properties, parameters, and return values; enable
  `declare(strict_types=1)` at the top of new files.
- Follow PSR-12 formatting (or the project's configured formatter) rather
  than hand-formatting.
- Avoid `global`/superglobal access inside a function; pass what's needed
  as a parameter or through a constructed dependency.
- Use a named constructor or a factory method instead of a constructor with
  many optional positional parameters.
- Prefer `readonly` properties for anything that shouldn't change after
  construction (PHP 8.1+).
