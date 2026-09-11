# Showcase example (DEMO DATA — not a real project)

Everything under `sample-project/` is fabricated for `docs/showcase.md`'s
three walkthroughs — a document to review, a small requirement/test/issue
trail to follow, and a two-window desktop scenario. No real user data, no
real credentials, nothing that talks to a network by default.

## What's here

- `sample-project/README.md` — the document used by the "documento con
  revisión real" walkthrough: a short project note with a couple of
  sentences worth commenting on and a phrase repeated twice (the case
  `applySuggestion`'s multiple-occurrence handling is meant to catch).
- `sample-project/requirement.json` — one fabricated requirement
  (`REQ-DEMO-1`) with `implements`/`tests` pointers into this same folder,
  used to exercise the requirement→decision→symbol→test→run neighborhood
  walkthrough without touching a real project's board.
- `sample-project/desktop-scenario.json` — a tiny, static description of a
  two-window desktop scenario (a dialog with a duplicate-named button) fed
  to `src/desktop_semantics/fake_backend.py` in tests — never executed
  against a real OS.

## Running the walkthroughs

`docs/showcase.md` names, per walkthrough, which real test file exercises
it end to end and what settings it reads (all off by default; nothing here
turns anything on for a real account). This folder holds the INPUT data
those tests/manual walkthroughs use — it is not itself a script.

## Boundaries

- Nothing under `examples/showcase/` is executed automatically by the app
  at startup or by CI outside the tests that explicitly load it by path.
- No secrets, no real hostnames, no real usernames.
- If you copy this folder to try a walkthrough against your own Faustus
  instance, treat every value in it as a placeholder.
