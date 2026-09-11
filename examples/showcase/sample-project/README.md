# Demo project note (fabricated — CMP-14 showcase data)

This is a short note used only to demonstrate the document-review
walkthrough in `docs/showcase.md`. It is not a real project.

## Summary

The onboarding flow needs a clearer error message when a connection
fails. The onboarding flow currently shows a generic "something went
wrong" toast, which does not tell the user whether the problem was the
network, the credentials, or the server being down.

## Open questions

- Should the retry button appear immediately, or only after the second
  failure?
- Does the error message need a link to the setup guide?

## Notes for reviewers

The phrase "onboarding flow" appears twice above on purpose — it is the
fixture for the "multiple occurrences must not silently apply the first
match" test case (`applySuggestion` in `studio/src/lib/docSession.ts`).
