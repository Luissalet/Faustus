---
id: python/security
title: Python security
applies_to: [Python]
priority: 30
summary: No shell=True with untrusted input, parameterised queries, no pickle on untrusted data.
---

- Never call `subprocess` with `shell=True` and untrusted input; pass an
  argument list (`shlex.split` if needed).
- Use parameterised queries (the driver's own placeholders) for SQL; never
  an f-string/`%`/`.format()` building a query from user input.
- Never `pickle.load`/`yaml.load` (without `SafeLoader`) untrusted or
  externally-sourced data — both can execute arbitrary code on load.
- Resolve a user-influenced path with `os.path.realpath` and check it's
  contained in the allowed root before opening it.
- Use `secrets`, not `random`, for anything security-sensitive (tokens,
  password reset codes).
- Never log a request body or exception `repr()` that might contain a
  password, token, or full PII record.
