---
id: python/testing
title: Python testing
applies_to: [Python]
priority: 36
summary: pytest fixtures over setup/teardown boilerplate; isolate state per test.
---

- Use `pytest` fixtures for setup/teardown rather than manual `setUp`-style
  boilerplate, unless the project already uses `unittest`-style classes.
- Isolate filesystem/DB state per test with `tmp_path`/a scoped fixture —
  never write to a shared path two tests could race on.
- Parametrize a test over several inputs (`@pytest.mark.parametrize`)
  instead of copy-pasting the same test body with one literal changed.
- Mock only the actual I/O boundary (network, filesystem, external
  service); test real internal logic directly rather than mocking it away.
- Assert on behaviour and specific values, not just "no exception was
  raised".
- Run `pytest -q <path>` for the narrow file during the loop; run the full
  suite once before reporting done.
