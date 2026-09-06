"""tests/test_delta_engine_document.py -- what the `document` adapter must never
get wrong.

Every test fixes a RULE and not a snapshot. The wording of a note will change
and the extractors will learn more markup; none of that may turn a
repagination into data loss, a changed quantity into an edited sentence, or a
page nobody could read into a page that says the same thing.

The guarantees, one test each (§28's "Documentos" rows first):

* a block that moved without changing is `moved`, and there is no `missing`
  and no `added` anywhere in that comparison -- §10's trap;
* a changed figure is `material`, through the content invariant, and a figure
  that only changed FORMAT is not a change at all;
* a citation that disappears is material even when the paragraph around it is
  nearly the same sentence -- §10's own words, and the row prose similarity
  would swallow;
* a table total that stops equalling its column comes out as a `regression`
  with confidence `exact`: the column is re-added on both sides, so it is
  arithmetic and not an opinion;
* text nobody could extract produces `unknown` for every invariant and
  `preserved` for none -- rule 1 of `contracts.py`, where a document adapter is
  most tempted to break it;
* a document read only in part says so in its coverage, and its invariants stay
  `unknown`;
* a JSON document is addressed by path, and its figures are `exact` rather than
  lexical, because a parser read them;
* every invariant id this module points findings at is one `invariants.py`
  declares.
"""

from __future__ import annotations

import pytest

from src.delta_engine import classification
from src.delta_engine import invariants as invariants_mod
from src.delta_engine import registry, sources
from src.delta_engine.adapters import document as doc_mod
from src.delta_engine.adapters.base import Scope
from src.delta_engine.contracts import Budget, IntentContract, RevisionRef

OWNER = "alice"
FROZEN = "2026-09-06T12:00:00Z"

CITATIONS = {"id": doc_mod.CITATIONS_INVARIANT, "class": "provenance"}
FIGURES = {"id": doc_mod.FIGURES_INVARIANT, "class": "content"}


@pytest.fixture()
def adapter() -> doc_mod.DocumentAdapter:
    return doc_mod.DocumentAdapter()


@pytest.fixture()
def scope() -> Scope:
    return Scope(owner=OWNER, budget=Budget())


def document(text: str, *, name: str = "report.md") -> RevisionRef:
    """One document handed in by the caller, addressed by a stable name.

    The name only decides which reader runs -- prose or structured -- and never
    the element addresses: a document's elements are addressed inside it, so
    two ends with different filenames still align.
    """
    stashed = sources.stash(text, media_type="text/markdown")
    payload = stashed.to_dict()
    payload["label"] = name
    return RevisionRef.parse(payload)


def compare(adapter, scope, source_text: str, target_text: str, *,
            name: str = "report.md"):
    source = adapter.snapshot(document(source_text, name=name), scope=scope)
    target = adapter.snapshot(document(target_text, name=name), scope=scope)
    return adapter.compare(source, target, scope=scope), source, target


def operations(extraction) -> dict:
    return {finding.path: finding.operation for finding in extraction.findings}


def intent_with(*invariants) -> IntentContract:
    return IntentContract.parse({
        "id": "intent_document", "owner": OWNER, "domain": "document",
        "frozen_at": FROZEN, "invariants": list(invariants),
    })


def results_by_id(results) -> dict:
    return {result.invariant_id: result for result in results}


# -- §28: reflow without loss -----------------------------------------------


def test_a_block_that_only_moved_is_moved_and_nothing_was_lost(adapter, scope):
    """§10's trap: reordering by pagination is not deletion.

    The assertion that matters most is the negative one. A `moved` emitted
    ALONGSIDE a `missing` and an `added` would still leave a reader believing
    the document lost a paragraph and gained a different one, and a
    repagination of a long report would read as the whole document being
    rewritten.
    """
    first = "The first paragraph, entirely unchanged in wording."
    second = "The second paragraph, also unchanged, merely relocated."
    third = "The third paragraph stays where it was."
    extraction, _source, _target = compare(
        adapter, scope,
        f"{first}\n\n{second}\n\n{third}\n",
        f"{second}\n\n{first}\n\n{third}\n",
    )
    ops = operations(extraction)
    assert "missing" not in ops.values()
    assert "added" not in ops.values()
    assert sorted(ops.values()) == ["moved", "moved", "unchanged"]


def test_rewrapping_a_paragraph_is_not_a_change(adapter, scope):
    """A block is hashed on its normalised text, so a reflow at 40 columns is
    the same block.

    Hashing the raw text instead would report a document re-wrapped by an
    editor as rewritten end to end, and every real edit inside it would be
    invisible in the noise.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        "One sentence that will be rewrapped by an editor at a different width.\n",
        "One sentence that will be\nrewrapped by an editor\nat a different width.\n",
    )
    assert operations(extraction) == {"block:0": "unchanged"}


# -- §28: a changed figure --------------------------------------------------


def test_a_changed_figure_is_material_and_points_at_the_content_invariant(adapter, scope):
    """Rewriting a paragraph and rewriting a quantity are different events.

    The materiality does not come from the wording of the row: it comes from
    the reference to the content invariant, which `classification` turns into a
    `regression` once `check_invariants` reports that invariant violated. That
    is the only channel §16 accepts, and it is why the adapter names the
    invariant instead of naming a severity it is not allowed to decide.
    """
    extraction, source, target = compare(
        adapter, scope,
        "Revenue was 1,200 EUR last quarter.\n",
        "Revenue was 1,500 EUR last quarter.\n",
    )
    figure = next(f for f in extraction.findings if f.path.startswith("number:"))
    assert figure.operation == "modified"
    assert doc_mod.FIGURES_INVARIANT in figure.invariant_refs
    assert (figure.before, figure.after) == ("1,200", "1,500")

    intent = intent_with(FIGURES)
    results = adapter.check_invariants(intent, source, target, scope=scope)
    assert results_by_id(results)[doc_mod.FIGURES_INVARIANT].status == "violated"

    classified = classification.classify_all(extraction.findings, intent=intent,
                                             invariant_results=results)
    assertion = next(a for a in classified.assertions if a.path == figure.path)
    assert assertion.classification == "regression"
    assert assertion.severity == "material"


def test_a_figure_that_only_changed_format_is_not_a_changed_figure(adapter, scope):
    """`1000` and `1,000` are one quantity written twice.

    Compared as text they differ, and a delta that said so would report a
    formatting pass as a changed fact -- the same failure as the reflow, one
    layer down. The block around it still reads as `modified`, which is honest:
    its text did change.
    """
    extraction, source, target = compare(
        adapter, scope,
        "The budget is 1000 EUR.\n",
        "The budget is 1,000 EUR.\n",
    )
    figure = next(f for f in extraction.findings if f.path.startswith("number:"))
    assert figure.operation == "unchanged"
    results = results_by_id(adapter.check_invariants(
        intent_with(FIGURES), source, target, scope=scope))
    assert results[doc_mod.FIGURES_INVARIANT].status == "preserved"
    assert results[doc_mod.FIGURES_INVARIANT].observations


# -- §28: a citation that disappears ----------------------------------------


def test_a_lost_citation_is_material_even_when_the_prose_barely_moved(adapter, scope):
    """§10, literally: a disappeared citation is materially different even when
    the prose looks similar.

    The paragraph here changes by four words and comes out `modified`, which is
    the row a reader skims past. The reference layer is addressed separately
    precisely so that the source the document leaned on does not vanish inside
    that row -- prose similarity is not evidence about provenance.
    """
    extraction, source, target = compare(
        adapter, scope,
        "The method follows the approach of [@smith2020] and extends it.\n",
        "The method follows the approach of Smith and extends it.\n",
    )
    ops = operations(extraction)
    assert ops["block:0"] == "modified"

    lost = next(f for f in extraction.findings if f.path == "citation:smith2020")
    assert lost.operation == "missing"
    assert doc_mod.CITATIONS_INVARIANT in lost.invariant_refs

    intent = intent_with(CITATIONS)
    results = adapter.check_invariants(intent, source, target, scope=scope)
    provenance = results_by_id(results)[doc_mod.CITATIONS_INVARIANT]
    assert provenance.status == "violated"
    assert provenance.severity == "material"

    classified = classification.classify_all(extraction.findings, intent=intent,
                                             invariant_results=results)
    assertion = next(a for a in classified.assertions if a.path == "citation:smith2020")
    assert assertion.classification == "regression"
    assert assertion.severity == "material"


def test_a_lost_link_is_reported_even_when_the_document_still_mentions_it(adapter, scope):
    """A link is a reference, and a reference is provenance, not prose."""
    extraction, source, target = compare(
        adapter, scope,
        "See the dataset at https://example.com/data for the raw numbers.\n",
        "See the dataset published by the agency for the raw numbers.\n",
    )
    lost = next(f for f in extraction.findings if f.path.startswith("link:"))
    assert lost.operation == "missing"
    assert doc_mod.CITATIONS_INVARIANT in lost.invariant_refs
    results = results_by_id(adapter.check_invariants(
        intent_with(CITATIONS), source, target, scope=scope))
    assert results[doc_mod.CITATIONS_INVARIANT].status == "violated"


# -- §28: table and total ---------------------------------------------------


TABLE_SOURCE = """# Costs

| Item | Amount |
| --- | ---: |
| Hosting | 10 |
| Support | 32 |
| Total | 42 |
"""

TABLE_TARGET = """# Costs

| Item | Amount |
| --- | ---: |
| Hosting | 10 |
| Support | 35 |
| Total | 42 |
"""


def test_a_total_that_stops_adding_up_is_a_regression_with_exact_confidence(adapter, scope):
    """§10: totals are validated deterministically, on BOTH sides.

    The column is re-added here and there, and the declared total is compared
    against each sum. Nothing is estimated and no threshold is chosen, so two
    runs of this check cannot disagree -- which is what `exact` means in
    `CONFIDENCE` and why this is the one finding in the adapter that claims it.

    The `before`/`after` carry both numbers, declared and computed. "The total
    changed" would not distinguish a corrected figure from a broken sum, and
    only the second is a regression.
    """
    extraction, source, target = compare(adapter, scope, TABLE_SOURCE, TABLE_TARGET)
    total = next(f for f in extraction.findings if f.path == "table:0#total")
    assert total.operation == "modified"
    assert total.confidence == "exact"
    assert doc_mod.FIGURES_INVARIANT in total.invariant_refs
    assert "42" in total.before and "45" in total.after

    intent = intent_with(FIGURES)
    results = adapter.check_invariants(intent, source, target, scope=scope)
    content = results_by_id(results)[doc_mod.FIGURES_INVARIANT]
    assert content.status == "violated"
    assert content.confidence == "exact"

    classified = classification.classify_all(extraction.findings, intent=intent,
                                             invariant_results=results)
    assertion = next(a for a in classified.assertions if a.path == "table:0#total")
    assert assertion.classification == "regression"
    assert assertion.severity == "material"
    assert assertion.confidence == "exact"


def test_a_table_whose_total_still_adds_up_is_not_a_regression(adapter, scope):
    """The other half: an edited table that stays consistent is not an error.

    Both the cell and the declared total move together here. The cells are
    `modified` -- they did change -- and the total is not a violation, because
    the arithmetic still holds. A check that flagged every edited total would
    be a check nobody leaves switched on.
    """
    balanced = TABLE_TARGET.replace("| Total | 42 |", "| Total | 45 |")
    extraction, source, target = compare(adapter, scope, TABLE_SOURCE, balanced)
    total = next(f for f in extraction.findings if f.path == "table:0#total")
    assert total.operation == "modified"
    assert "45" in total.after
    results = results_by_id(adapter.check_invariants(
        intent_with(FIGURES), source, target, scope=scope))
    # The declared total changed, so the content invariant is violated -- but
    # the observation says a figure changed, not that the arithmetic broke.
    content = results[doc_mod.FIGURES_INVARIANT]
    assert content.status == "violated"
    assert not any("no longer equals" in observation
                   for observation in content.observations)


def test_a_total_nobody_could_sum_is_not_reported_as_a_broken_one(adapter, scope):
    """A column with a non-numeric cell is not summed, and says so.

    Skipping the cell would produce a total that is arithmetically wrong about
    a table that is fine -- a regression invented by the reader of the table
    rather than by its author.
    """
    text = TABLE_SOURCE.replace("| Support | 32 |", "| Support | about 32 |")
    extraction, source, target = compare(adapter, scope, text, text)
    assert not [f for f in extraction.findings if f.path == "table:0#total"]
    results = results_by_id(adapter.check_invariants(
        intent_with(FIGURES), source, target, scope=scope))
    assert results[doc_mod.FIGURES_INVARIANT].status == "preserved"


# -- §28: text that could not be extracted ----------------------------------


def test_a_document_that_is_not_text_is_unreadable_and_never_preserved(adapter, scope):
    """§10's "contenido no extraíble", and rule 1 of `contracts.py`.

    A PDF is refused by name rather than decoded with `errors="replace"`: two
    pages of mojibake compare cleanly and produce a delta about a document
    nobody read. Every invariant then answers `unknown` -- not `preserved`,
    which is the claim the whole subsystem exists to refuse, and not
    `violated`, which would be a statement about content nobody saw.
    """
    unreadable = sources.stash(b"%PDF-1.7\n\x00\x01binary", media_type="application/pdf")
    payload = unreadable.to_dict()
    payload["label"] = "report.pdf"
    source = adapter.snapshot(RevisionRef.parse(payload), scope=scope)
    target = adapter.snapshot(document("Some readable prose.\n"), scope=scope)

    assert source.readable is False
    assert source.elements == ()
    assert any("PDF" in note for note in source.notes)

    extraction = adapter.compare(source, target, scope=scope)
    assert extraction.findings == ()
    assert extraction.coverage.both_readable is False
    assert extraction.coverage.ratio("semantic") == 0.0

    results = adapter.check_invariants(intent_with(CITATIONS, FIGURES),
                                       source, target, scope=scope)
    assert {result.status for result in results} == {"unknown"}
    assert all(result.confidence == "unknown" for result in results)
    assert all(result.limitations for result in results)


def test_a_document_read_only_in_part_says_so_and_stays_unknown(adapter):
    """A budget that cut the read is a gap, and a gap is not an observation.

    The blocks past the cut were never extracted, so "no citation disappeared"
    is a statement about a set that is missing most of its members. The
    coverage carries the fraction that WAS read, and the invariants stay
    `unknown` with the cut named.
    """
    scope = Scope(owner=OWNER, budget=Budget(max_bytes=40))
    long_text = ("First paragraph with the citation [@keeper] in it.\n\n"
                 + "\n\n".join(f"Paragraph number {index} of the tail."
                               for index in range(10)) + "\n")
    source = adapter.snapshot(document(long_text), scope=scope)
    target = adapter.snapshot(document(long_text), scope=scope)

    assert source.truncated is True
    extraction = adapter.compare(source, target, scope=scope)
    assert extraction.coverage.ratio("semantic") < 1.0
    assert any("unknown, not preserved" in note for note in extraction.coverage.notes)

    results = results_by_id(adapter.check_invariants(
        intent_with(CITATIONS, FIGURES), source, target, scope=scope))
    assert results[doc_mod.CITATIONS_INVARIANT].status == "unknown"
    assert results[doc_mod.FIGURES_INVARIANT].status == "unknown"


# -- structured documents ---------------------------------------------------


def test_a_json_document_is_addressed_by_path_and_its_figures_are_exact(adapter, scope):
    """A structured document has an address, so its blocks use it.

    The n-th leaf of a mapping is not an address: adding one key renumbers
    every leaf after it and the whole document reads as rewritten. The path is
    the address JSON actually has, and a figure read out of it came from a
    parser rather than from a regex over prose -- which is why its findings are
    `parser` and not `algorithm`.
    """
    extraction, _source, _target = compare(
        adapter, scope,
        '{"title": "Report", "totals": {"revenue": 1200, "costs": 300}}',
        '{"title": "Report", "totals": {"revenue": 1500, "costs": 300}}',
        name="report.json",
    )
    findings = {f.path: f for f in extraction.findings}
    revenue = findings["number:totals.revenue#0"]
    assert revenue.operation == "modified"
    assert revenue.tier == "parser"
    assert revenue.confidence == "exact"
    assert doc_mod.FIGURES_INVARIANT in revenue.invariant_refs
    assert findings["number:totals.costs#0"].operation == "unchanged"
    assert findings["block:title"].operation == "unchanged"


def test_a_json_document_that_does_not_parse_is_read_as_prose_with_a_note(adapter, scope):
    """A trailing comma is not "the document could not be read".

    The text is still there and its blocks still compare. Refusing the whole
    revision over a syntax error would turn a readable document into an
    `inconclusive` delta, and the note is what keeps the weaker reading from
    passing as the intended one.
    """
    snapshot = adapter.snapshot(
        document('{"title": "Report",}', name="report.json"), scope=scope)
    assert snapshot.readable is True
    assert any("does not parse" in note for note in snapshot.notes)
    assert snapshot.elements


# -- registration and the invariant catalogue -------------------------------


def test_the_registry_finds_this_adapter_without_anybody_updating_a_list():
    """Declaring `ADAPTER_FACTORY` IS the registration; there is no tuple."""
    domains = {getattr(factory(), "domain", "") for factory in registry.discover()}
    assert "document" in domains
    assert registry.has_adapter("document")
    assert registry.adapter_for("document").domain == "document"


def test_every_invariant_id_this_adapter_names_is_one_the_catalogue_declares():
    """A finding must never point at an invariant nobody declared.

    `classification._violations_for` matches a finding's `invariant_refs`
    against the results of invariants the frozen contract carries. A ref that
    no catalogue entry can produce would never match anything, and the
    materiality this adapter is trying to carry would silently evaporate.
    """
    declared = {item["id"] for item in invariants_mod.DOMAIN_DEFAULTS["document"]}
    assert doc_mod.CITATIONS_INVARIANT in declared
    assert doc_mod.FIGURES_INVARIANT in declared
