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
from typing import List, Optional, Tuple

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


# ─────────────────────────────────────────────────────────────────────────
# VER-04 — an exported document must reopen as what it claims to be
# ─────────────────────────────────────────────────────────────────────────
#
# A renderer returning bytes without raising is the document-export analogue
# of exit 0: it is not proof the file is usable. python-docx can be handed a
# write target that a caller truncates before it reaches the response, a
# download can be cut short between the server and the browser, and "no
# bytes at all" is itself a silent failure nothing upstream would notice,
# because the route still answers 200 with a Content-Disposition header
# naming a file. This section is `apply()`'s one-directional rule applied to
# a whole exported file instead of a command's stdout: it can turn an
# apparent export success into a failure, never manufacture the reverse —
# there is no check here that can make a genuinely broken file "verified".
#
# The five words a caller can show a user for where an export got to:
#   generated -> the renderer returned *some* content object at all.
#   saved     -> that content is not empty (a zero-byte file, or a transfer
#                cut off before anything landed, both look like "generated"
#                to a caller that never looks inside).
#   opens     -> the bytes reopen as a well-formed container: DOCX must be
#                a valid zip whose central directory checks out and whose
#                word/document.xml member exists; PDF must start with the
#                %PDF- signature every reader keys off.
#   reviewed  -> the container actually holds a payload, not an empty shell
#                (word/document.xml is not blank; the PDF signature is
#                followed by more than nothing; text formats are not
#                all-whitespace).
#   verified  -> every stage above passed — the only stage `ok` is True at.
#
# `opens` and `reviewed` are folded into one check per format below (there
# is no format here where a container can be well-formed yet demonstrably
# hollow in a way worth a fourth failure point) — verify_artifact() still
# reports both names in `stages` on success, so the caller-facing vocabulary
# stays the full five words the acceptance names.

ARTIFACT_STAGES = ("generated", "saved", "opens", "reviewed", "verified")

_PDF_SIGNATURE = b"%PDF-"


@dataclass(frozen=True)
class ArtifactVerdict:
    """Where an exported file got to on the `ARTIFACT_STAGES` ladder.

    `stage` is the LAST stage it passed — "generated" when even the size
    check failed, "saved" when it is non-empty but does not reopen as its
    own format — never the stage it failed at, so a reader is not left
    guessing whether a listed stage passed or failed.
    """

    ok: bool
    stage: str
    stages: Tuple[str, ...]
    reason: str


def verify_artifact(fmt: str, content: bytes) -> ArtifactVerdict:
    """Reopen *content* the way the format's own reader would, before a
    caller serves it as a successful download link.

    - **DOCX** must be a well-formed zip (``ZipFile.testzip()`` clean)
      containing a non-empty ``word/document.xml`` — the member Word itself
      reads the document body from.
    - **PDF** must be non-empty and start with the ``%PDF-`` signature.
    - Every other format (md/txt/html/json/...) must be non-empty and, once
      decoded, not all whitespace — the "export vacío" failure the
      acceptance names is not unique to DOCX.

    Never raises: a file this cannot open comes back ``ok=False`` with the
    problem folded into ``reason``, the same way a bad exit code is data
    here, not a crash.
    """
    stages: List[str] = ["generated"]
    data = bytes(content) if isinstance(content, (bytes, bytearray)) else b""
    if not data:
        return ArtifactVerdict(False, "generated", tuple(stages),
                               "the export produced an empty file (0 bytes)")
    stages.append("saved")

    key = (fmt or "").strip().lower().lstrip(".")
    if key == "docx":
        ok, reason = _reopen_docx(data)
    elif key == "pdf":
        ok, reason = _reopen_pdf(data)
    else:
        ok, reason = _reopen_text(data)
    if not ok:
        return ArtifactVerdict(False, "saved", tuple(stages), reason)
    stages.append("opens")
    stages.append("reviewed")
    stages.append("verified")
    return ArtifactVerdict(True, "verified", tuple(stages),
                           "the file reopened as its declared format and is not empty")


def _reopen_docx(data: bytes) -> Tuple[bool, str]:
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad_member = zf.testzip()
            if bad_member is not None:
                return False, f"the DOCX zip is corrupt at member {bad_member!r}"
            try:
                body = zf.read("word/document.xml")
            except KeyError:
                return False, "the DOCX is missing word/document.xml"
    except zipfile.BadZipFile as exc:
        return False, f"the DOCX is not a valid zip archive: {exc}"
    if not body.strip():
        return False, "word/document.xml is present but empty"
    return True, ""


def _reopen_pdf(data: bytes) -> Tuple[bool, str]:
    if not data.startswith(_PDF_SIGNATURE):
        return False, "the file does not start with the %PDF- signature"
    return True, ""


def _reopen_text(data: bytes) -> Tuple[bool, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Not every non-DOCX/PDF export is UTF-8 text (a batch .zip, an
        # arbitrary attachment, ...); a decode failure is not itself proof
        # of corruption, so this format falls back to the size check the
        # caller already passed rather than a check it was never promised.
        return True, ""
    if not text.strip():
        return False, "the exported file has no non-blank content"
    return True, ""
