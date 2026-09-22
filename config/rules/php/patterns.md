---
id: php/patterns
title: PHP patterns
applies_to: [PHP]
priority: 34
summary: Constructor-injected dependencies, typed exceptions, thin controllers.
---

- Inject dependencies through the constructor (or the framework's
  container), rather than instantiating a collaborator directly inside a
  method — it's what makes the class testable in isolation.
- Keep a controller/action thin: parse the request, call a service, shape
  the response — business logic lives in the service layer.
- Throw a specific exception type for an expected failure the caller
  should handle; let a genuine programming error propagate rather than
  catching it broadly and returning a generic error.
- Use an enum (PHP 8.1+) for a fixed set of named values instead of loose
  string/int constants.
