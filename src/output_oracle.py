"""Exit 0 is not evidence that a command did what it was asked.

A test runner whose collection errored, a build whose target was already up to
date, a migration that found nothing to migrate, a curl against a server that
was never started — all exit 0. The classic silent failure is a step that
succeeds at doing nothing, and no exit code can tell it apart from a step that
worked.

What can tell them apart is a string the step's author named as proof *before
the step ran*: "pytest must print `47 passed`", "the build must print
`Compiled successfully`". Declared first, it is evidence. Invented afterwards
from whatever the output happens to contain, it is a rationalisation — which is
why nothing here derives an expectation from the output it is checking.

The oracle is one-directional on purpose: it can turn a success into a failure
(exit 0 with the promised string missing becomes EXIT_OUTPUT_MISMATCH) and it
can never do the reverse. A step that already failed keeps the exit code it
earned; no declaration can talk a failure into a pass.

`output_matched` is `None` when nothing was declared. That third value is the
point of the module: no expectation means the step was *unchecked*, and a
reader who collapses that to `True` has turned "we never looked" into "it
passed" — the very substitution the oracle exists to prevent.

Matching is a plain substring, never a pattern: `.*` as an expectation would
pass everything, and an oracle that can be satisfied by its own declaration is
not one. Whatever was declared is returned alongside the verdict so a reader
can judge how much the pass is worth — a step that promised only `e` proved
almost nothing, and the record says so out loud.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

#: Exit code forced onto a step that exited 0 without its declared output.
#: 65 is BSD sysexits' EX_DATAERR — "the input data was incorrect somehow" —
#: which is as close as a standard code gets to "it ran, the result is wrong".
EXIT_OUTPUT_MISMATCH = 65

#: An expectation is a short landmark from the output, not a copy of it.
MAX_EXPECTED_CHARS = 512

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class OracleResult:
    """`matched` is None when nothing was declared: unchecked, not passed."""

    matched: Optional[bool]
    expected: Optional[str]
    why: str


def validate_expectation(expected: object) -> Optional[str]:
    """Why `expected` cannot serve as a declaration, or None when it can.

    Belongs at the moment the expectation is *declared* — plan creation, the
    request that carries a verify command — so a step never reaches the runner
    carrying something that cannot be checked.
    """
    if expected is None:
        return None
    if not isinstance(expected, str):
        return "an expected output must be a string"
    if not expected.strip():
        return "an expected output must not be blank"
    if len(expected) > MAX_EXPECTED_CHARS:
        return f"an expected output must be at most {MAX_EXPECTED_CHARS} characters"
    return None


def check(output: str, expected: Optional[str]) -> OracleResult:
    """Did `output` contain the declared `expected` string?"""
    if expected is None or not str(expected).strip():
        return OracleResult(None, None, "nothing was declared, so nothing was checked")

    expected = str(expected)
    text = output if isinstance(output, str) else str(output or "")
    if expected in text:
        return OracleResult(True, expected, f"the output contains {expected!r}")

    # A near miss is still a miss — but saying which kind saves the reader from
    # re-running a command that worked to fix a declaration that did not.
    if expected.lower() in text.lower():
        why = f"the output contains {expected!r} only in a different case"
    elif _WHITESPACE.sub(" ", expected).strip() in _WHITESPACE.sub(" ", text):
        why = f"the output contains {expected!r} only with different whitespace"
    else:
        why = f"the output does not contain {expected!r}"
    return OracleResult(False, expected, why)


def apply(
    exit_code: int, output: str, expected: Optional[str]
) -> Tuple[int, Optional[bool]]:
    """Return the exit code to record and `output_matched` beside it.

    Only a zero exit code can be overturned. A step that already failed keeps
    its own code, because the oracle's business is unmasking false successes,
    not relabelling honest failures.
    """
    result = check(output, expected)
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 1
    if result.matched is None:
        return code, None
    if result.matched:
        return code, True
    return (EXIT_OUTPUT_MISMATCH if code == 0 else code), False


def describe(result: OracleResult) -> str:
    """One line for a step log / a tool result, in the model's own reading."""
    if result.matched is None:
        return "No expected output was declared for this step; it was not checked."
    if result.matched:
        return f"Expected output observed: {result.expected!r}."
    return (
        f"Expected output was NOT observed: {result.expected!r} — {result.why}. "
        f"The step is recorded as exit {EXIT_OUTPUT_MISMATCH} even though the "
        "command itself reported success."
    )
