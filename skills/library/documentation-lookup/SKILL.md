---
name: documentation-lookup
description: Fetch a library's current, version-matched documentation before writing code against it, instead of relying on training-data memory that can be stale or wrong for the installed version. Use when writing code against a library or framework whose API you're not certain of, especially one that changes quickly across versions.
version: 1.0.0
category: research
tags: [documentation, research, libraries]
status: published
source: imported
---

## When to Use

Writing code against an external library or framework API you're not
completely certain of — especially one known to change quickly across
versions (a fast-moving frontend framework, a cloud SDK, a library that
recently had a major version bump). Memory of an API's shape can be stale,
version-mismatched, or simply wrong; checking costs one search and saves a
debugging session later.

## Procedure

1. **Identify the exact library and version in use** — check the project's
   own manifest (`package.json`, `requirements.txt`, `go.mod`) rather than
   assuming the latest version, since the API you need might be from a
   pinned older release with a meaningfully different surface.
2. **Search for the library's own documentation**, using `web_search` with
   the library name, version, and the specific feature/method you need
   ("prisma 5 nested writes", not just "prisma writes").
3. **Select the match that's actually for the right version** — a result
   for the current major version when the project pins an older one will
   describe methods that don't exist in the installed release, or that
   changed signature.
4. **Fetch the actual page** with `web_fetch` rather than trusting a search
   snippet — a snippet is often just enough to be plausible and wrong about
   parameter order or a default value.
5. **Use what you fetched directly in the code**, citing the specific
   behaviour you relied on if it's non-obvious (a default that changed
   between versions, a required option that isn't obvious from the method
   name).

## Pitfalls

- Writing code from memory for a library whose API surface changed across
  major versions, without checking which version this specific project has
  pinned.
- Trusting a search result snippet instead of fetching the actual page —
  snippets truncate exactly the parameter details that matter.
- Citing documentation for the latest version when the installed version is
  several majors behind and has a different signature for the same method.
- Skipping the check entirely for a library used constantly in general, but
  for a specific corner of its API you haven't personally used before.

## Verification

- The documentation consulted matches the exact version pinned in this
  project's manifest, not just "the library in general".
- The code compiles/runs against the actual installed version, not just
  against what the documentation implied — a quick local check catches a
  version mismatch documentation search alone might miss.
- Any non-obvious behaviour relied on (a default, a required option) is
  attributable to something actually fetched, not remembered.
