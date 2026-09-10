"""
tests/test_p1_write_01_localized_edit.py — WRITE-01, lote 53.

Acceptance line: "Una correccion ortografica no cambia la voz del narrador
ni elimina una ironia deliberada por normalizacion automatica." Pinned as
two separate, deterministic checks against `src/writing_edit.py`: a
`correct` edit that changes too much of the span is refused outright, and
an edit dropping a pinned (protected) phrase is refused regardless of
operation.
"""
from __future__ import annotations

import pytest

from src.writing_edit import (
    CorrectionTooWide,
    ProtectedFragmentLost,
    apply_edit,
    undo_edit,
)

DOC = "The cat sat on the mat. It was, of course, a perfectly ordinary Tuesday. The dog barked."


def test_a_narrow_spelling_fix_is_accepted_as_a_correction():
    start = DOC.index("mat.")
    end = start + len("mat.")
    result, record = apply_edit(DOC, start=start, end=end, replacement="mat,",
                                operation="correct", rationale="comma splice")
    assert result == DOC[:start] + "mat," + DOC[end:]
    assert record.operation == "correct"
    assert "mat." in record.original_span


def test_a_correction_that_actually_rewrites_the_sentence_is_refused():
    span = "It was, of course, a perfectly ordinary Tuesday."
    start = DOC.index(span)
    end = start + len(span)
    with pytest.raises(CorrectionTooWide):
        apply_edit(DOC, start=start, end=end,
                  replacement="Everything felt strange and unfamiliar that morning.",
                  operation="correct")


def test_the_same_wide_change_is_allowed_as_a_deliberate_rewrite():
    span = "It was, of course, a perfectly ordinary Tuesday."
    start = DOC.index(span)
    end = start + len(span)
    result, record = apply_edit(DOC, start=start, end=end,
                                replacement="Everything felt strange and unfamiliar that morning.",
                                operation="rewrite", rationale="tonal shift requested")
    assert "Everything felt strange" in result
    assert record.operation == "rewrite"


def test_an_edit_that_drops_a_deliberate_irony_is_refused_regardless_of_operation():
    span = "It was, of course, a perfectly ordinary Tuesday."
    start = DOC.index(span)
    end = start + len(span)
    with pytest.raises(ProtectedFragmentLost):
        apply_edit(DOC, start=start, end=end, replacement="It was a normal Tuesday.",
                  operation="rewrite", protected=["of course"])


def test_a_protected_fragment_kept_verbatim_still_lands():
    span = "It was, of course, a perfectly ordinary Tuesday."
    start = DOC.index(span)
    end = start + len(span)
    result, _ = apply_edit(DOC, start=start, end=end,
                           replacement="It was, of course, an unremarkable Tuesday.",
                           operation="rewrite", protected=["of course"])
    assert "of course" in result


def test_everything_outside_the_span_is_byte_identical():
    span = "The cat sat on the mat."
    start = DOC.index(span)
    end = start + len(span)
    result, _ = apply_edit(DOC, start=start, end=end, replacement="A cat sat on a mat.",
                           operation="rewrite")
    assert result[:start] == DOC[:start]
    assert result[start + len("A cat sat on a mat."):] == DOC[end:]
    assert "perfectly ordinary Tuesday" in result   # untouched tail survived


def test_undo_restores_exactly_the_original_span():
    span = "The dog barked."
    start = DOC.index(span)
    end = start + len(span)
    edited, record = apply_edit(DOC, start=start, end=end, replacement="The dog was silent.",
                                operation="rewrite")
    restored = undo_edit(edited, record)
    assert restored == DOC


def test_undo_refuses_if_the_span_moved_under_it():
    span = "The dog barked."
    start = DOC.index(span)
    end = start + len(span)
    edited, record = apply_edit(DOC, start=start, end=end, replacement="The dog was silent.",
                                operation="rewrite")
    tampered = edited.replace("The dog was silent.", "Something else entirely different.")
    with pytest.raises(Exception):
        undo_edit(tampered, record)


def test_unknown_operation_is_rejected():
    with pytest.raises(ValueError):
        apply_edit(DOC, start=0, end=3, replacement="Da", operation="paraphrase")
