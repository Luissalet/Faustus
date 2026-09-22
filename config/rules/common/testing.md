---
id: common/testing
title: Testing defaults
applies_to: []
priority: 16
summary: Watch the test fail first, run the narrow command, quote the real output.
---

- Detect the project's own test runner before running anything; run the
  untouched suite once to confirm the baseline is green.
- Write the test before the implementation for new behaviour when
  practical, and actually watch it fail for the right reason first.
- Run the narrowest command that exercises the change (the specific test
  file) during the loop; run the wider suite once before reporting done.
- Report the command and its real output/exit code — never a paraphrase
  like "tests pass" with nothing to back it.
- Don't chase coverage percentage over behaviour: a suite that hits every
  line but asserts nothing about an edge case is decoration.
- Treat a flaky test as a bug to diagnose, not noise to rerun until green.
- For a bug fix, prove the test would have caught the original bug (revert
  the fix locally, watch it fail, then restore it).
