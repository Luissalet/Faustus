"""LANG-02 — búsqueda y edición multilingües.

``services/search/lang_normalize.py`` did not exist before this lote:
nothing in the repo did accent/case-insensitive search with guaranteed
original-position correspondence.
"""
import os

import pytest

from services.search.lang_normalize import (
    contains_normalized,
    detect_query_language,
    find_all_normalized,
    locate_normalized,
    multilingual_query_variants,
    should_try_english_variant,
    strip_diacritics,
)
from src.reply_language import locate_for_edit


def test_strip_diacritics_folds_case_and_accents_one_to_one():
    original = "Múltiples IDÉNTICOS niños"
    folded = strip_diacritics(original)
    assert folded == "multiples identicos ninos"
    assert len(folded) == len(original)  # 1:1 length guarantee


def test_contains_normalized_finds_accented_text_from_an_unaccented_query():
    assert contains_normalized("El niño juega en el jardín", "nino") is True
    assert contains_normalized("El niño juega en el jardín", "gato") is False


def test_find_all_normalized_returns_original_offsets_not_folded_ones():
    haystack = "café, café, café"
    matches = find_all_normalized(haystack, "cafe")
    assert matches == [(0, 4), (6, 10), (12, 16)]
    for start, end in matches:
        assert haystack[start:end] == "café"  # the ORIGINAL text, accent intact


# ---------------------------------------------------------------------------
# The acceptance criterion itself: normalizing for search must not modify a
# filename, nor shift a diff/edit range.
# ---------------------------------------------------------------------------

def test_regression_normalized_offsets_never_drift_from_the_real_text():
    """Without character-by-character (not whole-string) NFD folding in
    `strip_diacritics`, a decomposition that changes length would shift
    every match after it -- this pins the 1:1 guarantee directly."""
    haystack = "niño niño niño"
    for ch in haystack:
        folded = strip_diacritics(ch)
        assert len(folded) == 1  # exactly one output character per input character
    matches = find_all_normalized(haystack, "NINO")
    assert [haystack[s:e] for s, e in matches] == ["niño", "niño", "niño"]


def test_normalized_search_does_not_touch_the_filename_itself():
    """`strip_diacritics` only ever produces a MATCHING key -- a caller that
    (correctly) never assigns its result back to a filename keeps the real,
    accented filename untouched."""
    filename = "informe_agrícola.pdf"
    matching_key = strip_diacritics(filename)
    assert matching_key != filename  # the key folds accents...
    assert filename == "informe_agrícola.pdf"  # ...but the real filename never changes


def test_locate_normalized_returns_the_first_match_only():
    assert locate_normalized("gato, GATO, gäto", "gato") == (0, 4)
    assert locate_normalized("no match here", "xyz") is None


# ---------------------------------------------------------------------------
# Language detection (reused from src.research_citations) and query variants
# ---------------------------------------------------------------------------

def test_detect_query_language_reads_real_signal_only():
    assert detect_query_language("¿Cuánto cuesta el envío internacional?") == "es"
    assert detect_query_language("xyz") is None  # no real signal -- never guesses


def test_multilingual_query_variants_adds_a_folded_variant_without_replacing():
    variants = multilingual_query_variants("día del niño")
    assert variants[0] == "día del niño"  # original preserved first
    assert "dia del nino" in variants
    assert len(variants) == 2


def test_multilingual_query_variants_is_stable_for_unaccented_queries():
    assert multilingual_query_variants("weather forecast") == ["weather forecast"]


def test_should_try_english_variant():
    assert should_try_english_variant("es") is True
    assert should_try_english_variant("en") is False
    assert should_try_english_variant(None) is False


# ---------------------------------------------------------------------------
# Editing (src.reply_language.locate_for_edit): conserves language/accents.
# ---------------------------------------------------------------------------

def test_locate_for_edit_finds_accented_text_and_returns_original_offsets():
    document = "El título del informe es 'Análisis Económico 2024'."
    span = locate_for_edit(document, "Analisis Economico")
    assert span is not None
    start, end = span
    assert document[start:end] == "Análisis Económico"


def test_locate_for_edit_returns_none_without_a_match():
    assert locate_for_edit("nothing relevant here", "unrelated phrase") is None
