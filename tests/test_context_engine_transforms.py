"""What `context_engine/transforms.py` must never get wrong.

Shortening is where a context engine is most tempted to lie: a summary always
fits, and a summary of a file always sounds like the file.  These tests pin the
four promises that make the shortening trustworthy:

* the budget is never exceeded — a packet that overflows is a turn that dies
  after the tools have already run;
* a protected source type never comes back as a generated summary, even when a
  summary was available and would have fitted;
* an excerpt is a literal slice cut on boundaries, and one that would split an
  identifier is not produced at all;
* `fit()` is deterministic down to the `item_id`, because Branching Futures
  proves two branches started from one context by comparing exactly that.

Fixtures are local, the estimator is the conservative default, and nothing here
calls a model — which is also the property under test.
"""

import pytest

from src.context_engine.budgets import estimator_for
from src.context_engine.contracts import TRANSFORMATIONS, ContextCandidate
from src.context_engine.transforms import (
    PROTECTED_SOURCE_TYPES,
    excerpt,
    fit,
    fit_all,
    reference_only,
    to_item,
)

#: Twelve lines of prose with a distinctive term on line 7.
LINES = "\n".join(
    f"line {n}: the router registers handler number {n}" for n in range(12)
) + "\n"

#: A payload with no whitespace anywhere: there is no boundary to cut on, so
#: no honest excerpt of it exists at any budget.  This is not a contrived
#: shape — it is what a base64 artifact or a minified bundle looks like.
BLOB = "e30" + "Zm9vYmFyYmF6" * 40
SUMMARY = "A base64 payload of about 480 characters."


def _est():
    return estimator_for("")


def _candidate(ref, body="", **overrides):
    payload = {
        "candidate_id": f"cand::{ref}",
        "source_type": "document",
        "source_ref": ref,
        "title": ref,
        "body": body,
        "observed_at": "2026-09-01T12:00:00+00:00",
        "trust_class": "observed",
        "authority": "agent_claim",
        "source_revision": "rev-a",
    }
    payload.update(overrides)
    return ContextCandidate.parse(payload)


# ── the budget is a ceiling ────────────────────────────────────────────────

@pytest.mark.parametrize("budget", [0, 4, 8, 12, 25, 60, 400, 5000])
def test_fit_never_exceeds_the_budget(budget):
    candidate = _candidate("doc:long", LINES)
    item, omission = fit(candidate, budget_tokens=budget, estimator=_est(),
                         query="handler number 7")
    assert item is not None or omission is not None
    if item is not None:
        assert item.tokens <= budget
        assert item.chars == len(item.body)


def test_fit_all_prices_each_candidate_against_its_own_budget():
    pairs = [
        (_candidate("doc:a", LINES), 5000),
        (_candidate("doc:b", LINES), 40),
        (_candidate("doc:c", LINES), 8),
    ]
    items, omissions = fit_all(pairs, estimator=_est(), query="handler number 7")

    assert [i.transformation for i in items] == ["verbatim", "excerpt", "reference"]
    assert [i.tokens <= budget for i, (_c, budget) in zip(items, pairs)] == [True] * 3
    # Only the reference lost content, so only the reference is an omission.
    assert [o.source_ref for o in omissions] == ["doc:c"]


# ── protected sources are never summarised ─────────────────────────────────

@pytest.mark.parametrize("source_type", PROTECTED_SOURCE_TYPES)
def test_a_protected_source_never_ends_up_generated(source_type):
    """The summary fits.  The protection, not the budget, is what stops it."""
    candidate = _candidate("src/blob.py", BLOB, source_type=source_type,
                           meta={"summary": SUMMARY})
    item, omission = fit(candidate, budget_tokens=30, estimator=_est(),
                         allow_generative=True)

    assert item is not None
    assert item.transformation == "reference"
    assert item.is_generated() is False
    assert item.body == ""
    assert omission is not None
    assert omission.reason == "budget"
    assert omission.recoverable is True
    assert omission.source_ref == "src/blob.py"


def test_the_same_payload_is_summarised_when_it_is_not_protected():
    """The control for the test above: without the protection, it fits."""
    candidate = _candidate("artifact:blob-1", BLOB, source_type="artifact",
                           meta={"summary": SUMMARY})
    item, omission = fit(candidate, budget_tokens=30, estimator=_est(),
                         allow_generative=True)

    assert item is not None
    assert item.transformation == "generated"
    assert item.is_generated() is True
    assert item.body == SUMMARY
    assert item.tokens <= 30
    assert omission is None


def test_a_summary_is_never_invented_when_the_source_did_not_supply_one():
    """`generated` is a label for somebody else's summary, never a model call."""
    candidate = _candidate("artifact:blob-2", BLOB, source_type="artifact")
    item, omission = fit(candidate, budget_tokens=30, estimator=_est(),
                         allow_generative=True)

    assert item is not None
    assert item.transformation == "reference"
    assert omission is not None


def test_generative_is_off_by_default():
    candidate = _candidate("artifact:blob-3", BLOB, source_type="artifact",
                           meta={"summary": SUMMARY})
    item, _omission = fit(candidate, budget_tokens=30, estimator=_est())
    assert item.transformation == "reference"


# ── excerpts cut on boundaries ─────────────────────────────────────────────

def test_excerpt_cuts_on_line_boundaries():
    out = excerpt(LINES, max_chars=120, query="handler number 7")

    assert out
    assert len(out) <= 120
    assert out in LINES                      # a literal slice, not a rebuild
    original = LINES.splitlines()
    assert all(line in original for line in out.splitlines())
    assert "handler number 7" in out         # the region that matches the query


def test_excerpt_prefers_the_region_that_matches_the_query():
    early = excerpt(LINES, max_chars=100, query="handler number 1")
    late = excerpt(LINES, max_chars=100, query="handler number 10")
    assert "handler number 1\n" in early
    assert "handler number 10" in late
    assert early != late


def test_excerpt_refuses_to_split_an_identifier():
    assert excerpt("a" * 200, max_chars=50) == ""

    text = "short_name a_very_long_identifier_that_will_not_fit_here"
    out = excerpt(text, max_chars=20)
    assert out == "short_name"
    assert "a_very_long_ident" not in out


def test_excerpt_returns_the_whole_text_when_it_already_fits():
    assert excerpt(LINES, max_chars=len(LINES)) == LINES
    assert excerpt("", max_chars=100) == ""
    assert excerpt(LINES, max_chars=0) == ""


def test_a_protected_source_is_only_cut_on_whole_lines():
    """A word-boundary cut is honest prose and a dishonest source file."""
    protected = _candidate("src/router.py", LINES, source_type="file")
    loose = _candidate("doc:router", LINES, source_type="document")

    strict_item, _ = fit(protected, budget_tokens=20, estimator=_est(),
                         query="handler number 7")
    loose_item, _ = fit(loose, budget_tokens=20, estimator=_est(),
                        query="handler number 7")

    if strict_item.transformation == "excerpt":
        assert all(line in LINES.splitlines()
                   for line in strict_item.body.splitlines())
    assert loose_item.transformation in ("excerpt", "reference")


# ── determinism ────────────────────────────────────────────────────────────

def test_fit_is_deterministic_byte_for_byte():
    candidate = _candidate("doc:long", LINES)
    first, first_omission = fit(candidate, budget_tokens=40, estimator=_est(),
                                query="handler number 7")
    second, second_omission = fit(candidate, budget_tokens=40, estimator=_est(),
                                  query="handler number 7")

    assert first is not None
    assert first.to_dict() == second.to_dict()
    # Including the id: it is a fingerprint of what the item says, not a uuid.
    assert first.item_id == second.item_id
    assert first_omission == second_omission


def test_a_different_budget_produces_a_different_item_id():
    candidate = _candidate("doc:long", LINES)
    whole, _ = fit(candidate, budget_tokens=5000, estimator=_est())
    cut, _ = fit(candidate, budget_tokens=40, estimator=_est(),
                 query="handler number 7")
    assert whole.item_id != cut.item_id


# ── the ladder only ever descends ──────────────────────────────────────────

def test_fit_walks_down_the_ladder_and_never_up():
    candidate = _candidate("doc:long", LINES)
    estimator = _est()

    whole, no_omission = fit(candidate, budget_tokens=5000, estimator=estimator)
    assert whole.transformation == "verbatim"
    assert whole.body == candidate.body
    assert no_omission is None

    cut, still_none = fit(candidate, budget_tokens=40, estimator=estimator,
                          query="handler number 7")
    assert cut.transformation == "excerpt"
    assert cut.body in candidate.body
    assert len(cut.body) < len(candidate.body)
    # An excerpt is present in the packet, so it is not also an omission.
    assert still_none is None
    assert (TRANSFORMATIONS.index(cut.transformation)
            > TRANSFORMATIONS.index(whole.transformation))

    pointer, omission = fit(candidate, budget_tokens=8, estimator=estimator)
    assert pointer.transformation == "reference"
    assert pointer.body == ""
    assert omission is not None
    assert omission.recoverable is True

    # `structured` and `projected` need to know the shape of a source, which
    # this layer does not; they are never reached by accident.
    for item in (whole, cut, pointer):
        assert item.transformation not in ("structured", "projected")


def test_not_even_a_reference_fits_is_reported_rather_than_guessed():
    candidate = _candidate("doc:long", LINES)
    item, omission = fit(candidate, budget_tokens=1, estimator=_est())
    assert item is None
    assert omission is not None
    assert omission.reason == "budget"
    assert "reference" in omission.detail


# ── the plain builders ─────────────────────────────────────────────────────

def test_reference_only_keeps_provenance_and_drops_content():
    candidate = _candidate("src/router.py", LINES, source_type="file")
    item = reference_only(candidate, estimator=_est())

    assert item.transformation == "reference"
    assert item.body == ""
    assert item.chars == 0
    assert item.source_ref == "src/router.py"
    assert item.source_revision == "rev-a"
    # A pointer is printed, so it is not free.
    assert item.tokens > 0


def test_to_item_labels_and_does_not_transform():
    candidate = _candidate("doc:long", LINES)
    item = to_item(candidate, estimator=_est(), transformation="verbatim",
                   reason="mandatory section")
    assert item.body == candidate.body
    assert item.reason == "mandatory section"
    assert item.trust_class == candidate.trust_class
    assert item.authority == candidate.authority
    assert item.lanes == candidate.lanes
