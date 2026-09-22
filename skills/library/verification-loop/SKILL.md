---
name: verification-loop
description: Run build, type-check, lint, tests and a diff review in that order before calling any change finished. Use when you are about to say a task is done, especially one that touched more than a single obvious file.
version: 1.0.0
category: testing
tags: [verification, testing, quality]
status: published
source: imported
---

## When to Use

Before reporting any non-trivial change as complete — a feature, a bug fix,
a refactor across more than one file. Not every one-line typo fix needs the
full loop, but "I changed the code and it looks right" is never enough on
its own for anything a user will rely on.

## Procedure

1. **Build.** Run whatever makes the project runnable (`python -c "import
   module"` for a Python package, `npm run build` / `tsc --noEmit` for a
   frontend, `go build ./...`, `cargo check`). A build failure invalidates
   every later step, so fix it before doing anything else.
2. **Type-check**, if the project has types: `mypy`/`pyright` for Python,
   `tsc --noEmit` for TypeScript. A change that satisfies the runtime but
   not the type checker is a bug waiting for the next caller.
3. **Lint.** Run the project's own linter with its own config rather than a
   generic one — a rule the project deliberately disabled should stay
   disabled. Fix what the change introduced; don't fix unrelated pre-existing
   warnings in the same pass unless asked.
4. **Tests.** Run the narrow test file for the change first, then the
   related suite. Read the actual failure output rather than assuming a
   green exit code means the right things were asserted.
5. **Security pass on the diff.** Scan your own diff for a hardcoded secret,
   string-built SQL, an unescaped value reaching HTML/shell/a subprocess
   argument list, or a new dependency pulled in for one call site.
6. **Read the diff end to end**, not just the parts you meant to change.
   Confirm every line is either something you intended, or an unavoidable
   side effect you can explain — not "I don't remember writing that".
7. **Report each step's actual result** (command + outcome), not a single
   "all checks passed" summary. A reader should be able to tell which step,
   if any, you skipped and why.

## Pitfalls

- Running the loop once at the very end of a long session instead of after
  each meaningful change — by then ten things can be broken and none of the
  errors point at the one that caused it.
- Treating "no output" as "no problem": a linter or type checker that was
  never actually invoked (wrong working directory, wrong config picked up)
  prints nothing and looks identical to a clean pass.
- Skipping the diff read because "I know what I changed" — the tool that
  applied the edit is not infallible, and a stray duplicated block or an
  accidentally reverted line is exactly the kind of thing only a full read
  catches.
- Declaring success from the model's own narrative ("this should now work")
  instead of a command's actual exit code and output.

## Verification

- Every step that applies to this project ran, and its real output (not a
  summary) is quoted in the final report.
- The diff was read in full and every line is accounted for.
- Any step that was skipped is named explicitly, with the reason (e.g. "no
  linter configured for this language").
