---
id: java/testing
title: Java testing
applies_to: [Java]
priority: 36
summary: JUnit + a real assertion library, mock at the boundary, one behaviour per test.
---

- One behaviour asserted per test method; name the method for the
  behaviour, not the method under test alone.
- Mock only the actual external boundary (a repository, an HTTP client);
  let internal collaborators run for real in a unit test.
- Use a dedicated assertion library (whatever the project already
  includes) over chained plain `assertTrue` calls, for readable failure
  messages.
- Use a test slice/context appropriate to the layer under test (a plain
  unit test for a service, a web-layer test for a controller) rather than
  spinning up the whole application context for everything.
