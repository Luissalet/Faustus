---
id: php/testing
title: PHP testing
applies_to: [PHP]
priority: 36
summary: PHPUnit/Pest per behaviour, mock at the boundary, use data providers over duplicated cases.
---

- One behaviour per test method, named for the expectation.
- Use a data provider for multiple input/output cases instead of copy-
  pasted test methods that differ by one literal.
- Mock only the actual external boundary (an HTTP client, a mailer); let
  real internal logic run in a unit test.
- Use a database transaction rollback (or an in-memory test database)
  between tests rather than manually cleaning up rows.
