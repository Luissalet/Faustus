---
name: security-review
description: A concrete checklist for secrets, input validation, injection, auth, and data exposure — run before shipping code that touches user input, credentials, or a database. Use when the change adds an endpoint, a form, a query, a file upload, an auth check, or anything that stores or renders untrusted data.
version: 1.0.0
category: security
tags: [security, review, checklist]
status: published
source: imported
---

## When to Use

Any change that accepts input from outside the process (an HTTP request, an
uploaded file, a webhook payload, a CLI argument fed by another program) or
that touches credentials, a database query, or data that gets rendered back
to a user. Skip it for pure internal refactors that don't move the trust
boundary.

## Procedure

1. **Secrets.** Grep the diff for anything that looks like a key, token, or
   password literal. Nothing goes in source, a commit message, or a log
   line — it goes through the app's own settings/env mechanism. If a secret
   was already committed, rotating it matters more than removing the line;
   git history keeps the old value.
2. **Input validation.** Every value from outside the trust boundary gets a
   type and a shape check before use — length limits, an allow-list of
   values where the set is small, and rejection (not silent truncation) of
   anything unexpected. Validate on the server; a client-side check is UX,
   not security.
3. **Injection.** Never build a SQL string, a shell command, or an HTML
   fragment by concatenating untrusted input. Parameterised queries for SQL,
   `shlex`/argument lists (never `shell=True`) for subprocesses, and
   templating with autoescape (never manual string concatenation) for HTML.
4. **Auth and authorization.** Confirm both: is the caller who they claim
   (authentication), and are they allowed to do *this specific thing to this
   specific record* (authorization) — checking the first is not a substitute
   for the second. An object lookup by ID must also check that ID belongs to
   the caller, not just that the caller is logged in.
5. **Output encoding.** Anything derived from user input that gets rendered
   as HTML, used in a URL, or written to a file path must be escaped or
   validated for that context — an HTML-safe string is not automatically
   URL-safe or filesystem-safe.
6. **File paths.** Resolve to an absolute real path and check it stays
   inside an allowed root before any read/write driven by user-supplied
   input — the classic bug is `..` walking out of an intended directory.
7. **Dependencies.** A new package pulled in for one small feature is a new
   supply-chain surface; check it's maintained and reasonably scoped before
   adding it, especially for something a stdlib function could do.
8. **Errors.** An error message returned to the caller should say enough to
   fix their request and nothing about internal structure (stack traces,
   file paths, query text) that helps an attacker map the system.

## Pitfalls

- Validating on the client and trusting that alone — anyone can call the
  API directly and skip the client entirely.
- Confusing "the user is logged in" with "the user may do this" — the most
  common real-world break is an authenticated user reaching another user's
  record by changing an ID in the request.
- String-formatting a query with an f-string "just this once" because the
  input looked safe — the input that looked safe today is attacker-
  controlled tomorrow if the endpoint is ever exposed more widely.
- Logging the full request body on error, which quietly logs passwords and
  tokens along with everything else.
- Treating a security pass as done because nothing obviously bad was found,
  rather than checking each item on this list explicitly.

## Verification

- Every value from outside the process has an explicit validation step
  before it's used, not just before it's stored.
- No string-built query, shell command, or HTML fragment exists in the diff;
  grep confirms it.
- An authorization check exists on every code path that reads or writes a
  specific record by ID, not only on the ones you happened to test by hand.
- No secret-shaped literal appears in the diff, commit message, or log
  output.
