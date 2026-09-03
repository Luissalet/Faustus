"""Positional near-duplicate detection (src/text_overlap.py).

The point being tested throughout: a span is reported ONLY after an exact
substring comparison confirms it. Hashes and the diagonal vote find candidates;
they never decide. So the suite asserts the strong property directly —
``span_text(a, a0, a1) == span_text(b, b0, b1)`` for every pair the module
returns — plus the two failure modes a fingerprint scheme has: a phrase
repeated in one document must not be merged into one long false span, and
unrelated text must come back empty rather than "a little bit similar".
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import text_overlap  # noqa: E402


SHARED = ("the deployment script must never be run against production "
          "without taking a checkpoint of the workspace first")


def _verified(a, b, result):
    """Every reported span really is literally equal on both sides."""
    return all(text_overlap.span_text(a, a0, a1) == text_overlap.span_text(b, b0, b1)
               for (a0, a1), (b0, b1) in result["spans"])


# ── normalization and q-grams ───────────────────────────────────────────


def test_normalize_lowercases_and_collapses_whitespace():
    assert text_overlap.normalize("  Hello\t\tWORLD\n again ") == "hello world again"
    assert text_overlap.normalize(None) == ""
    assert text_overlap.normalize(12) == "12"


def test_qgrams_are_every_window_with_its_offset():
    grams = text_overlap.qgrams("Hello  World", 5)
    assert [g for _, g in grams][:3] == ["hello", "ello ", "llo w"]
    assert [offset for offset, _ in grams] == list(range(len("hello world") - 4))
    # The offset is where the gram starts in the NORMALIZED string.
    norm = text_overlap.normalize("Hello  World")
    assert all(norm[offset:offset + 5] == gram for offset, gram in grams)


def test_text_shorter_than_k_has_no_qgrams_and_no_fingerprints():
    assert text_overlap.qgrams("ab", 5) == []
    assert text_overlap.fingerprints("ab", 5, 4) == []
    assert text_overlap.qgrams("", 5) == []


def test_identical_text_always_selects_identical_fingerprints():
    """The property winnowing is chosen for: an exact duplicate can never be
    missed, whatever the window happens to land on."""
    text = "the curator inverts a rule that proved harmful into an anti-pattern"
    assert text_overlap.fingerprints(text) == text_overlap.fingerprints(text.upper())


def test_winnowing_keeps_the_fingerprint_set_sparse():
    """The other half of the bargain: the set stays small, so comparing a few
    hundred memories pairwise is cheap. Winnowing's expected density is
    2/(w+1); on prose with w=4 that is around 0.4 of the q-grams."""
    text = ("The curator is deterministic and llm free: everything here is arithmetic over "
            "the records the store already keeps, so the same store and the same clock "
            "always produce the same report. ") * 3
    grams = text_overlap.qgrams(text)
    fps = text_overlap.fingerprints(text)
    assert fps                                    # never empty
    assert len(fps) < len(grams) / 2              # far fewer fingerprints than q-grams


# ── overlap ─────────────────────────────────────────────────────────────


def test_identical_text_is_ratio_one_and_one_verified_span():
    text = "always run the project tests before claiming the work is done"
    result = text_overlap.overlap(text, text)
    assert result["ratio"] == 1.0
    assert len(result["spans"]) == 1
    (a0, a1), (b0, b1) = result["spans"][0]
    assert (a0, a1) == (b0, b1) == (0, len(text))
    assert _verified(text, text, result)


def test_case_and_whitespace_differences_are_still_identical():
    a = "Always run   the project TESTS before claiming done"
    b = "always run the project tests   before claiming done"
    assert text_overlap.overlap(a, b)["ratio"] == 1.0


def test_a_shared_paragraph_inside_two_documents_is_reported_and_verified():
    a = "Introductory notes that belong to document A only. " + SHARED + " Tail of A."
    b = "A completely different opening for document B. " + SHARED + " Ending of B."
    result = text_overlap.overlap(a, b)

    assert result["spans"], "the shared paragraph must be found"
    assert _verified(a, b, result), "every reported span is literally equal"
    # The verified span really does contain the shared paragraph.
    joined = " ".join(text_overlap.span_text(a, a0, a1) for (a0, a1), _ in result["spans"])
    assert SHARED in joined
    # ...and it is a genuine overlap, not a claim that the documents are one.
    assert 0.0 < result["ratio"] < 1.0


def test_unrelated_text_is_zero_with_no_spans():
    a = "always run the project tests before claiming the work is finished"
    b = "Los gorriones anidaron bajo el alero durante todo el mes de junio"
    result = text_overlap.overlap(a, b)
    assert result == {"ratio": 0.0, "spans": []}


def test_a_repeated_phrase_is_not_merged_across_diagonals():
    """Two copies of a phrase in A both align to the one copy in B. Each is its
    own span on its own diagonal; merging them would invent a shared region
    twice as long as anything that exists."""
    phrase = "alpha beta gamma delta epsilon zeta "
    a, b = phrase * 2, phrase
    result = text_overlap.overlap(a, b)

    assert len(result["spans"]) == 2
    assert _verified(a, b, result)
    norm_b = text_overlap.normalize(b)
    for (a0, a1), (b0, b1) in result["spans"]:
        assert a1 - a0 == b1 - b0                       # equal-length alignment
        assert b1 - b0 <= len(norm_b)                   # never longer than B itself
    # The two spans are different places in A aligned to the same place in B.
    assert result["spans"][0][0] != result["spans"][1][0]
    assert result["spans"][0][1] == result["spans"][1][1]


def test_a_hash_match_that_is_not_a_text_match_reports_nothing(monkeypatch):
    """Force every q-gram to the same hash: the diagonal vote then proposes
    candidates everywhere, and the literal verification must throw them all
    away. This is the discipline the whole feature rests on."""
    monkeypatch.setattr(text_overlap, "_hash", lambda gram: 1)
    a = "aaaaaaaaaaaaaaaaaaaaaaaa"
    b = "bbbbbbbbbbbbbbbbbbbbbbbb"
    result = text_overlap.overlap(a, b)
    assert result == {"ratio": 0.0, "spans": []}


def test_overlap_is_deterministic_across_calls():
    a = "Introductory notes for A. " + SHARED + " Tail."
    b = "Another opening for B. " + SHARED + " End."
    assert text_overlap.overlap(a, b) == text_overlap.overlap(a, b)


def test_overlap_is_symmetric_in_its_ratio():
    a = "Introductory notes for A. " + SHARED + " Tail."
    b = "Another opening for B. " + SHARED + " End."
    assert text_overlap.overlap(a, b)["ratio"] == text_overlap.overlap(b, a)["ratio"]


@pytest.mark.parametrize("a,b", [
    ("", ""), ("", "something"), ("a", "a"), ("ab", "ab"), (None, None),
    (None, "text"), (12345, 12345), ([1, 2], {"x": 1}),
])
def test_empty_short_and_unusable_input_never_raises(a, b):
    result = text_overlap.overlap(a, b)
    assert set(result) == {"ratio", "spans"}
    assert isinstance(result["ratio"], float)
    assert isinstance(result["spans"], list)


def test_an_exploding_object_is_empty_text_not_an_exception():
    class Boom:
        def __str__(self):
            raise RuntimeError("no")

    assert text_overlap.normalize(Boom()) == ""
    assert text_overlap.overlap(Boom(), "anything at all") == {"ratio": 0.0, "spans": []}
    assert text_overlap.span_text(Boom(), 0, 5) == ""


def test_span_text_clamps_out_of_range_offsets():
    assert text_overlap.span_text("hello world", 0, 5) == "hello"
    assert text_overlap.span_text("hello world", -10, 500) == "hello world"
    assert text_overlap.span_text("hello world", 8, 3) == ""


# ── find_duplicates ─────────────────────────────────────────────────────


def _items():
    return [
        {"id": "m3", "text": "Always run the project tests before claiming the work is done"},
        {"id": "m1", "text": "Always run the project tests before claiming the work is done."},
        {"id": "m2", "text": "Los gorriones anidaron bajo el alero durante el mes de junio"},
    ]


def test_find_duplicates_reports_the_pair_above_the_threshold():
    pairs = text_overlap.find_duplicates(_items(), 0.6)
    assert [(p["a"], p["b"]) for p in pairs] == [("m1", "m3")]
    assert pairs[0]["ratio"] > 0.9
    texts = {i["id"]: i["text"] for i in _items()}
    assert _verified(texts["m1"], texts["m3"], pairs[0])


def test_find_duplicates_orders_pairs_deterministically():
    items = _items() + [
        {"id": "m4", "text": "Always run the project tests before claiming the work is done!!"},
    ]
    first = text_overlap.find_duplicates(items, 0.6)
    second = text_overlap.find_duplicates(list(reversed(items)), 0.6)
    assert first == second
    # Sorted by (-ratio, a, b), and `a` always sorts before `b` inside a pair.
    assert first == sorted(first, key=lambda p: (-p["ratio"], p["a"], p["b"]))
    assert all(p["a"] < p["b"] for p in first)


def test_find_duplicates_threshold_is_respected():
    a = "Introductory notes for document A. " + SHARED + " Tail of A."
    b = "A different opening for document B. " + SHARED + " End of B."
    items = [{"id": "a", "text": a}, {"id": "b", "text": b}]
    ratio = text_overlap.overlap(a, b)["ratio"]
    assert text_overlap.find_duplicates(items, ratio + 0.01) == []
    assert len(text_overlap.find_duplicates(items, ratio)) == 1


def test_find_duplicates_ignores_malformed_items_and_never_raises():
    items = [
        {"id": "ok", "text": "always run the project tests before claiming done"},
        {"id": "ok2", "text": "always run the project tests before claiming done"},
        {"no_id": True}, "not a dict", None, {"id": "", "text": "x"},
        {"id": "short", "text": "ab"},
        {"id": "ok", "text": "a duplicate id is taken once"},
    ]
    pairs = text_overlap.find_duplicates(items, 0.6)
    assert [(p["a"], p["b"]) for p in pairs] == [("ok", "ok2")]
    assert text_overlap.find_duplicates(None) == []
    assert text_overlap.find_duplicates("nonsense") == []
    assert text_overlap.find_duplicates([], threshold="not a number") == []


def test_find_duplicates_caps_the_number_of_items_compared():
    items = [{"id": f"m{i:03d}", "text": f"shared preamble for every item, number {i}"}
             for i in range(20)]
    assert text_overlap.find_duplicates(items, 0.6, max_items=0) == []
    capped = text_overlap.find_duplicates(items, 0.6, max_items=3)
    assert {p["a"] for p in capped} | {p["b"] for p in capped} <= {"m000", "m001", "m002"}
