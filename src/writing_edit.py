"""
writing_edit.py — WRITE-01: localized edits with a declared intention,
never a silent full-document rewrite dressed up as a "correction".

Five distinct operations (`OPERATIONS`), each a different promise about how
much of the text an edit is allowed to touch:

  * ``correct``   — spelling/grammar only. Bounded by `_word_change_ratio`:
    an edit that changes more than `MAX_CORRECTION_CHANGE_RATIO` of the
    span's words is refused outright (`CorrectionTooWide`) — that is what
    keeps a spellcheck from quietly renarrating a paragraph, which is the
    acceptance line this module exists for ("una correccion ortografica no
    cambia la voz del narrador").
  * ``rewrite``, ``summarize``, ``develop``, ``critique`` — no such
    ceiling; the caller is asking for a different text on purpose, and
    WRITE-01's job there is only the span/protection discipline below.

Every operation, regardless of which one:

  * touches exactly ONE span of the document (`start`, `end` character
    offsets) — everything outside it is asserted byte-identical between
    the original and the result, checked here rather than left as a
    promise the caller has to keep on its own;
  * refuses to land if it drops a ``protected`` fragment the caller pinned
    (`ProtectedFragmentLost`) — a phrase to keep verbatim whatever the
    operation, the "irony kept" half of the acceptance line;
  * is recorded as one `EditRecord` (operation, span, rationale, a diff),
    so a caller building an "accept/undo per change" UI has one place to
    read from, and `undo_edit` can restore the original text of any
    recorded edit without the caller keeping its own copy.

Nothing here calls a model. This is the enforcement layer a caller (the
actual "propose a correction" prompt, wherever it lives) passes its
proposed replacement through before the edit is allowed to land — the same
relationship `src/workflows/store.py` has to the node handlers it does not
itself implement.
"""
from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

OPERATIONS = ("correct", "rewrite", "summarize", "develop", "critique")

#: How much of a `correct` span's words may change before it stops being a
#: correction and starts being a rewrite wearing a correction's name. About
#: one word in three — loose enough that real spellcheck output (which can
#: touch several short words in one sentence) passes, tight enough that a
#: paraphrase does not. See the tests for the boundary this was calibrated
#: against.
MAX_CORRECTION_CHANGE_RATIO = 0.34


class EditRefused(ValueError):
    """A proposed edit violated WRITE-01's own contract, not a caller bug."""


class ProtectedFragmentLost(EditRefused):
    """A protected fragment did not survive into the edited span."""


class CorrectionTooWide(EditRefused):
    """A `correct` edit changed more of the span than a correction should."""


def _words(text: str) -> List[str]:
    return re.findall(r"\w+", text, flags=re.UNICODE)


def _word_change_ratio(before: str, after: str) -> float:
    """0.0 = identical words, 1.0 = nothing in common. Word-level, not
    character-level: a correction that fixes three separate typos should
    not be judged by how many characters that touched, only how many
    words changed meaning."""
    before_words, after_words = _words(before), _words(after)
    if not before_words and not after_words:
        return 0.0
    matcher = difflib.SequenceMatcher(a=before_words, b=after_words, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    denom = max(len(before_words), len(after_words), 1)
    return 1.0 - (matched / denom)


@dataclass
class EditRecord:
    id: str
    operation: str
    start: int
    end: int
    original_span: str
    result_span: str
    rationale: str
    diff: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "operation": self.operation, "start": self.start,
                "end": self.end, "original_span": self.original_span,
                "result_span": self.result_span, "rationale": self.rationale,
                "diff": self.diff}


def apply_edit(document: str, *, start: int, end: int, replacement: str,
               operation: str, rationale: str = "",
               protected: Optional[List[str]] = None) -> Tuple[str, EditRecord]:
    """Apply one localized edit. Returns `(new_document, EditRecord)` or
    raises `EditRefused` (never a partial/best-effort result)."""
    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {OPERATIONS}, got {operation!r}")
    if not (0 <= start <= end <= len(document)):
        raise ValueError(f"start/end ({start}, {end}) out of range for a "
                         f"{len(document)}-character document")

    original_span = document[start:end]
    for fragment in (protected or []):
        if fragment and fragment in original_span and fragment not in replacement:
            raise ProtectedFragmentLost(
                f"the edit drops a protected fragment: {fragment!r}")

    if operation == "correct":
        ratio = _word_change_ratio(original_span, replacement)
        if ratio > MAX_CORRECTION_CHANGE_RATIO:
            raise CorrectionTooWide(
                f"a 'correct' edit changed {ratio:.0%} of the span's words "
                f"(limit {MAX_CORRECTION_CHANGE_RATIO:.0%}); use 'rewrite' "
                "for a change this size")

    result = document[:start] + replacement + document[end:]
    # The whole point of "localized": everything outside the span is
    # untouched — checked here, not merely promised by the slicing above.
    assert result[:start] == document[:start]
    assert result[start + len(replacement):] == document[end:]

    diff = "\n".join(difflib.unified_diff(
        original_span.splitlines(), replacement.splitlines(),
        lineterm="", fromfile="before", tofile="after"))
    record = EditRecord(id=f"edit_{uuid.uuid4().hex[:12]}", operation=operation,
                        start=start, end=end, original_span=original_span,
                        result_span=replacement, rationale=rationale, diff=diff)
    return result, record


def undo_edit(document: str, record: EditRecord) -> str:
    """Revert exactly the span `record` touched, using ITS OWN recorded
    original text — not a caller-supplied guess of what used to be there.
    Refuses (rather than corrupting the document) if the span has since
    been changed by something else."""
    start = record.start
    end = start + len(record.result_span)
    if document[start:end] != record.result_span:
        raise EditRefused(
            "the document changed since this edit; cannot undo blindly — "
            "re-locate the span before retrying")
    return document[:start] + record.original_span + document[end:]
