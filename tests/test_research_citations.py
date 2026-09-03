"""Tests for src/research_citations.py — the deterministic half of the deep
research quality work: stable source numbers, marker parsing, citation repair
and checking a cited sentence's figures against the source it cites.

Written before the module (house rule: parsing tests first). Everything here is
pure python — no network, no LLM, no DB — so these are the tests that must stay
green forever even when the model behind research changes.
"""
import pytest

from src.research_citations import (
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    VERDICT_UNCHECKED,
    VERDICT_WORDS,
    Claim,
    SourceRegistry,
    audit_citations,
    build_legend,
    canonical_url,
    check_claims,
    compute_coverage,
    detect_language,
    domain_of,
    finalize_report,
    find_markers,
    repair_citations,
    sources_heading,
)

# The real question Luis asked, in the shape he asked it: a numbered list of
# sub-questions in Spanish. Every language/structure test uses this one so a
# regression shows up against the case that actually motivated the work.
PHYSIO_QUESTION = """\
Necesito una revisión sobre fisioterapia para la tendinopatía rotuliana en \
deportistas amateur.
1. ¿Qué protocolos de carga excéntrica tienen mejor evidencia y con qué dosis?
2. ¿Cuánto tarda la recuperación y qué criterios marcan la vuelta al deporte?
3. ¿Las ondas de choque aportan algo frente al ejercicio isométrico?
4. ¿Qué dicen los ensayos clínicos recientes sobre la carga semanal óptima?
"""


def _registry(*urls):
    reg = SourceRegistry()
    for i, url in enumerate(urls, 1):
        reg.add({"url": url, "title": f"Source {i}", "summary": f"Summary {i}",
                 "evidence": f"Evidence {i}"})
    return reg


# ---------------------------------------------------------------------------
# URL normalisation and the registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b", [
    ("https://Example.COM/a", "https://example.com/a"),
    ("https://example.com/a/", "https://example.com/a"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com/a#section-2", "https://example.com/a"),
    ("https://example.com/a?utm_source=x&utm_medium=y", "https://example.com/a"),
    ("https://example.com/a?fbclid=123", "https://example.com/a"),
    ("https://example.com/a?gclid=123", "https://example.com/a"),
    ("https://example.com/a?id=7&utm_campaign=z", "https://example.com/a?id=7"),
])
def test_two_spellings_of_one_page_canonicalise_together(a, b):
    assert canonical_url(a) == canonical_url(b)


def test_different_pages_stay_different():
    assert canonical_url("https://example.com/a") != canonical_url("https://example.com/b")
    # www is NOT stripped: some hosts really do serve different content there,
    # and a false merge silently attributes a claim to the wrong page.
    assert canonical_url("https://www.example.com/a") != canonical_url("https://example.com/a")


def test_domain_of_drops_www_and_port():
    assert domain_of("https://www.Example.com:8443/a/b?c=1") == "example.com"
    assert domain_of("not a url") == ""


def test_numbers_are_assigned_on_first_sight_and_never_change():
    reg = SourceRegistry()
    assert reg.add({"url": "https://a.test/p", "title": "A"}) == 1
    assert reg.add({"url": "https://b.test/p", "title": "B"}) == 2
    # Round 2 re-fetches the first page under a dirtier URL — same number.
    assert reg.add({"url": "https://A.test/p/?utm_source=news", "title": "A again"}) == 1
    assert reg.add({"url": "https://c.test/p", "title": "C"}) == 3
    assert [s["n"] for s in reg.all()] == [1, 2, 3]
    assert reg.number_for("https://b.test/p#frag") == 2
    assert reg.number_for("https://never.test/") is None
    assert reg.source(2)["title"] == "B"
    assert reg.source(0) is None and reg.source(99) is None


def test_re_adding_a_url_fills_in_fields_that_were_missing():
    """A page first seen as a bare search hit (title only) and later extracted
    must end up carrying the extraction, without renumbering."""
    reg = SourceRegistry()
    n = reg.add({"url": "https://a.test/p", "title": "A"})
    assert reg.add({"url": "https://a.test/p", "summary": "the real summary",
                    "evidence": "the real evidence"}) == n
    entry = reg.source(n)
    assert entry["title"] == "A"
    assert entry["summary"] == "the real summary"
    assert entry["evidence"] == "the real evidence"


def test_a_finding_without_a_usable_url_gets_no_number():
    reg = SourceRegistry()
    assert reg.add({"url": "", "title": "nothing"}) == 0
    assert reg.add({"title": "nothing"}) == 0
    assert reg.all() == []


def test_registry_entries_carry_the_documented_fields():
    reg = SourceRegistry()
    reg.add({"url": "https://www.a.test/p", "title": "A", "summary": "s",
             "evidence": "e", "fetched_at": "2026-01-01T00:00:00+00:00"})
    entry = reg.source(1)
    assert set(entry) == {"n", "url", "title", "summary", "evidence", "domain", "fetched_at"}
    assert entry["domain"] == "a.test"
    assert entry["fetched_at"] == "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Marker parsing
# ---------------------------------------------------------------------------


def _nums(text):
    return [m.numbers for m in find_markers(text)]


def test_plain_marker():
    assert _nums("The rate fell [3].") == [[3]]


def test_grouped_and_adjacent_markers():
    assert _nums("Both agree [1, 2].") == [[1, 2]]
    assert _nums("Both agree [1,2].") == [[1, 2]]
    assert _nums("Both agree [1][2].") == [[1], [2]]
    assert _nums("Three of them [1, 2, 3].") == [[1, 2, 3]]


def test_markdown_link_is_not_a_citation():
    assert _nums("See [1](https://a.test) for details.") == []
    assert _nums("See [1] (spaced) is still a citation.") == [[1]]


def test_index_expressions_inside_inline_code_are_not_citations():
    assert _nums("Use `arr[3]` to read it.") == []
    assert _nums("Use ``an arr[3] with a ` tick`` here.") == []


def test_code_fences_are_skipped_entirely():
    md = "Before [1].\n\n```python\nx = arr[2]\ny = arr[3]\n```\n\nAfter [4].\n"
    assert _nums(md) == [[1], [4]]


def test_tilde_fences_are_skipped_too():
    md = "Before [1].\n\n~~~\narr[2]\n~~~\n\nAfter [4].\n"
    assert _nums(md) == [[1], [4]]


def test_table_cells_can_carry_markers():
    md = (
        "| Protocol | Dose | So what |\n"
        "|---|---|---|\n"
        "| Alfredson | 180/day [1] | Slow but proven [2] |\n"
    )
    assert _nums(md) == [[1], [2]]


def test_a_dangling_marker_still_parses_as_a_marker():
    # Parsing must not consult the registry — audit decides what dangles.
    assert _nums("Unsupported claim [99].") == [[99]]


def test_a_link_reference_definition_is_not_a_citation():
    """`[1]: https://…` at the start of a line defines a markdown link label,
    not a claim about source 1."""
    assert _nums("[1]: https://a.test/1\n\nA real claim here [2].\n") == [[2]]
    # Mid-sentence it still is one: "as shown [1]: the rate fell".
    assert _nums("As shown [1]: the rate fell.") == [[1]]


def test_zero_and_negative_and_huge_are_not_markers():
    assert _nums("[0] and [-1] and [1.5]") == []


# ---------------------------------------------------------------------------
# Claims: sentence segmentation around markers
# ---------------------------------------------------------------------------


def test_audit_pairs_each_cited_sentence_with_its_numbers():
    reg = _registry("https://a.test/1", "https://b.test/2")
    md = (
        "Eccentric loading reduces pain by 40% over 12 weeks [1]. "
        "Shockwave therapy shows no added benefit [2]. "
        "This sentence has no citation at all.\n"
    )
    audit = audit_citations(md, reg)
    assert [c.numbers for c in audit.claims] == [[1], [2]]
    assert "Eccentric loading" in audit.claims[0].text
    assert "40%" in audit.claims[0].text
    assert "Shockwave" in audit.claims[1].text
    assert audit.total_sentences == 3
    # The span must actually point at the sentence in the source text.
    c = audit.claims[0]
    assert md[c.start:c.end].strip().startswith("Eccentric loading")


def test_a_marker_after_the_full_stop_attaches_to_the_sentence_it_follows():
    """`...text. [1]` is as common as `...text [1].` — the marker must not be
    stolen by the next sentence, which would attribute the wrong claim."""
    reg = _registry("https://a.test/1")
    md = "Pain fell by 40% over twelve weeks. [1] Recovery times varied widely.\n"
    audit = audit_citations(md, reg)
    assert len(audit.claims) == 1
    assert "40%" in audit.claims[0].text
    assert "Recovery times" not in audit.claims[0].text


def test_list_items_are_separate_claims():
    reg = _registry("https://a.test/1", "https://b.test/2")
    md = "- Heavy slow resistance works [1]\n- Isometrics ease pain acutely [2]\n"
    audit = audit_citations(md, reg)
    assert [c.numbers for c in audit.claims] == [[1], [2]]
    assert "Isometrics" not in audit.claims[0].text


def test_soft_wrapped_paragraph_is_one_sentence():
    reg = _registry("https://a.test/1")
    md = ("Eccentric loading reduces pain measurably\nover a twelve week "
          "programme [1].\n")
    audit = audit_citations(md, reg)
    assert len(audit.claims) == 1
    assert "twelve week" in audit.claims[0].text


def test_a_marker_opening_a_table_cell_stays_in_that_cell():
    """Without a guard, the leading-marker fixup carries `[2]` back into the
    cell on its left and the whole row's citations pile up in one column."""
    reg = _registry("https://a.test/1", "https://b.test/2")
    md = ("| A | B |\n|---|---|\n"
          "| [1] loading works well | [2] shockwave adds nothing |\n")
    audit = audit_citations(md, reg)
    assert [c.numbers for c in audit.claims] == [[1], [2]]
    assert "shockwave" not in audit.claims[0].text


def test_a_marker_opening_a_list_item_stays_in_that_item():
    reg = _registry("https://a.test/1", "https://b.test/2")
    md = "- [1] Heavy slow resistance works\n- [2] Isometrics ease pain acutely\n"
    audit = audit_citations(md, reg)
    assert [c.numbers for c in audit.claims] == [[1], [2]]


def test_used_dangling_and_uncited():
    reg = _registry("https://a.test/1", "https://b.test/2", "https://c.test/3")
    md = "Claim A [2]. Claim B [1, 2]. Claim C [99].\n"
    audit = audit_citations(md, reg)
    assert audit.used == [2, 1, 99]          # order of first appearance
    assert audit.dangling == [99]
    assert audit.uncited == [3]


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def test_repair_removes_dangling_markers_and_records_them():
    reg = _registry("https://a.test/1")
    md = "Solid claim [1]. Invented claim [7].\n"
    out, audit = repair_citations(md, reg)
    assert "[7]" not in out
    assert "[1]" in out
    assert audit.removed == [7]
    assert audit.dangling == []
    # The sentence survives, only the lie about its source is gone.
    assert "Invented claim." in out


def test_repair_keeps_the_valid_half_of_a_mixed_group():
    reg = _registry("https://a.test/1")
    md = "Half true [1, 7].\n"
    out, _ = repair_citations(md, reg)
    assert "[1]" in out and "7" not in out.split(sources_heading("en"))[0]


def test_repair_converts_markdown_link_citations_into_numbers():
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/study", "title": "The study", "summary": "s"})
    md = "The trial ran for 12 weeks ([The study](https://a.test/study)).\n"
    out, audit = repair_citations(md, reg)
    body = out.split(sources_heading("en"))[0]
    assert "](https://a.test/study)" not in body
    assert "The study [1]" in body
    assert audit.used == [1]


def test_repair_leaves_links_to_unknown_urls_alone():
    reg = _registry("https://a.test/1")
    md = "Background reading: [somewhere](https://unknown.test/x) [1].\n"
    out, _ = repair_citations(md, reg)
    assert "[somewhere](https://unknown.test/x)" in out


def test_repair_does_not_touch_images():
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/pic.png", "title": "Pic"})
    md = "![a chart](https://a.test/pic.png)\n"
    out, _ = repair_citations(md, reg)
    assert "![a chart](https://a.test/pic.png)" in out


def test_sources_section_lists_exactly_the_cited_sources():
    reg = _registry("https://a.test/1", "https://b.test/2", "https://c.test/3")
    md = "Claim A [3]. Claim B [1].\n"
    out, audit = repair_citations(md, reg)
    tail = out.split(sources_heading("en"))[1]
    assert "1. [Source 1](https://a.test/1) — a.test" in tail
    assert "3. [Source 3](https://c.test/3) — c.test" in tail
    # Source 2 was never cited, so it is not listed.
    assert "b.test" not in tail
    assert audit.uncited == [2]


def test_sources_section_is_numbered_by_registry_not_by_order_of_use():
    reg = _registry("https://a.test/1", "https://b.test/2")
    md = "Claim B first [2]. Claim A second [1].\n"
    out, _ = repair_citations(md, reg)
    tail = out.split(sources_heading("en"))[1].strip().splitlines()
    numbered = [ln for ln in tail if ln.strip()]
    assert numbered[0].startswith("1.") and numbered[1].startswith("2.")


def test_repair_is_idempotent():
    reg = _registry("https://a.test/1", "https://b.test/2")
    md = "Claim A [1] and a bad one [42]. Claim B ([Source 2](https://b.test/2)).\n"
    once, _ = repair_citations(md, reg)
    twice, audit2 = repair_citations(once, reg)
    assert once == twice
    assert audit2.removed == []


def test_repair_never_invents_a_citation():
    reg = _registry("https://a.test/1")
    md = "A paragraph with no citation whatsoever.\n"
    out, audit = repair_citations(md, reg)
    assert find_markers(out.split(sources_heading("en"))[0]) == []
    assert audit.used == []
    # No sources cited, so no sources section is fabricated either.
    assert sources_heading("en") not in out


def test_repair_writes_the_spanish_heading_for_a_spanish_report():
    reg = _registry("https://a.test/1")
    md = "La carga excéntrica reduce el dolor [1].\n"
    out, _ = repair_citations(md, reg, language="es")
    assert "## Fuentes" in out
    assert "## Sources" not in out


def test_repair_ignores_markers_inside_code_fences():
    reg = _registry("https://a.test/1")
    md = "Real claim [1].\n\n```\nvalues = arr[99]\n```\n"
    out, audit = repair_citations(md, reg)
    assert "arr[99]" in out          # the snippet is untouched
    assert audit.removed == []
    assert audit.used == [1]


# ---------------------------------------------------------------------------
# Checking — uses the real src.claim_verify ladder, no stubs
# ---------------------------------------------------------------------------


SOURCE_TEXT = (
    "El protocolo de Alfredson consiste en 180 repeticiones diarias de trabajo "
    "excentrico durante 12 semanas y reduce el dolor de forma significativa."
)

# An English page of the kind a Spanish report is routinely written from. It
# shares its figures with the Spanish sentences below and almost nothing else.
ENGLISH_SOURCE = (
    "Resistance training increased lumbar spine bone mineral density by 2% "
    "over 12 months in postmenopausal women, while femoral neck density was "
    "unchanged. Adherence was 85%."
)


def _checked_registry():
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/alfredson", "title": "Alfredson",
             "summary": SOURCE_TEXT, "evidence": SOURCE_TEXT})
    return reg


def test_a_claim_the_source_states_verbatim_is_supported():
    reg = _checked_registry()
    md = ("El protocolo de Alfredson consiste en 180 repeticiones diarias de "
          "trabajo excentrico durante 12 semanas [1].\n")
    audit = audit_citations(md, reg)
    checked = check_claims(audit.claims, reg)
    assert [c.verdict for c in checked] == [VERDICT_SUPPORTED]
    assert checked[0].number == 1
    assert checked[0].layer in (1, 2)


def test_a_figure_the_cited_source_does_not_have_is_the_one_accusation_worth_printing():
    reg = _checked_registry()
    md = ("El protocolo de Alfredson consiste en 400 repeticiones diarias de "
          "trabajo excentrico durante 12 semanas [1].\n")
    audit = audit_citations(md, reg)
    checked = check_claims(audit.claims, reg)
    assert [c.verdict for c in checked] == [VERDICT_REFUTED]
    assert "400" in checked[0].why


def test_a_spanish_sentence_drawn_from_an_english_page_is_supported_by_its_figures():
    """The defect this whole file was rewritten for.

    The model writes Spanish; the page is English; no amount of token overlap
    will ever connect them. But 2% is 2% in both languages, and the sentence
    is only claiming the figure. Under the old three-point scale this was
    "débil" — a report of 57 such citations printed 51 of them as weak and
    read as untrustworthy when it was nothing of the kind.
    """
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/bmd", "title": "Resistance training and BMD",
             "summary": ENGLISH_SOURCE, "evidence": ENGLISH_SOURCE})
    md = ("El entrenamiento de fuerza incrementó la DMO de la columna lumbar "
          "en un 2% a los 12 meses [1].\n")
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert [c.verdict for c in checked] == [VERDICT_SUPPORTED]


def test_a_missing_name_alone_never_refutes_a_sentence():
    """A Spanish acronym is absent from an English page by construction.

    claim_verify's layer 4 refuses the sentence over "DMO"; treating that as
    "not in the source" would print an accusation about the translation.
    """
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/bmd", "title": "T",
             "summary": ENGLISH_SOURCE, "evidence": ENGLISH_SOURCE})
    md = "La DMO subió un 2% [1].\n"
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert checked[0].layer == 4          # the engine did refuse it
    assert checked[0].verdict == VERDICT_SUPPORTED


def test_a_sentence_with_no_figures_is_not_checkable_and_says_so():
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/bmd", "title": "T",
             "summary": ENGLISH_SOURCE, "evidence": ENGLISH_SOURCE})
    md = "El ejercicio mejoró la calidad de vida de las participantes [1].\n"
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert [c.verdict for c in checked] == [VERDICT_UNCHECKED]


def test_a_source_we_stored_no_text_for_is_unchecked_not_refuted():
    """We fetched nothing, so we found nothing — which is not a finding."""
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/empty"})
    md = "El dolor bajó un 40% en 12 semanas [1].\n"
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert [c.verdict for c in checked] == [VERDICT_UNCHECKED]


def test_the_citation_marker_is_not_mistaken_for_one_of_the_claim_s_figures():
    """`[12]` in the text is a source number, not a datum. If the marker is left
    in the claim, claim_verify's layer 4 hunts for "12" in the source and calls
    every sentence unsupported — the bug this test pins."""
    reg = SourceRegistry()
    for i in range(1, 13):
        reg.add({"url": f"https://a.test/{i}", "title": f"S{i}",
                 "summary": SOURCE_TEXT, "evidence": SOURCE_TEXT})
    md = ("El protocolo de Alfredson consiste en 180 repeticiones diarias de "
          "trabajo excentrico durante 12 semanas [12].\n")
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert checked[0].verdict == VERDICT_SUPPORTED


def test_a_reordered_paraphrase_of_the_source_is_supported():
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/1", "title": "T",
             "summary": "Loading protocols reduce patellar tendon pain in athletes.",
             "evidence": "Loading protocols reduce patellar tendon pain in athletes."})
    md = "Patellar tendon pain in athletes reduce loading protocols [1].\n"
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert checked[0].verdict == VERDICT_SUPPORTED


def test_a_figure_found_in_any_cited_source_settles_the_sentence():
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/1", "title": "Elsewhere",
             "summary": "unrelated text", "evidence": "unrelated text"})
    reg.add({"url": "https://a.test/2", "title": "Right one", "summary": SOURCE_TEXT,
             "evidence": SOURCE_TEXT})
    md = ("El protocolo de Alfredson consiste en 180 repeticiones diarias de "
          "trabajo excentrico durante 12 semanas [1, 2].\n")
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert checked[0].verdict == VERDICT_SUPPORTED
    assert checked[0].number == 2


def test_an_empty_co_citation_cannot_silence_a_figure_another_source_lacks():
    """Otherwise one page we stored nothing for buries the only count that is
    worth printing."""
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/1", "title": "Real page",
             "summary": SOURCE_TEXT, "evidence": SOURCE_TEXT})
    reg.add({"url": "https://a.test/2"})
    md = "El protocolo usa 400 repeticiones diarias durante 12 semanas [1, 2].\n"
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert checked[0].verdict == VERDICT_REFUTED


def test_checking_a_dangling_number_is_unchecked_not_a_crash():
    reg = _checked_registry()
    md = "Algo enteramente inventado, con un 99% de nada [9].\n"
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert [c.verdict for c in checked] == [VERDICT_UNCHECKED]


def test_a_page_whose_summary_is_empty_is_still_checked_against_its_evidence():
    """Half the stored text is not the widest text we hold, and a sentence
    called unchecked for that reason is our failure, not the report's."""
    reg = SourceRegistry()
    reg.add({"url": "https://a.test/1", "title": "T", "evidence": ENGLISH_SOURCE})
    md = "La adherencia fue del 85% [1].\n"
    checked = check_claims(audit_citations(md, reg).claims, reg)
    assert [c.verdict for c in checked] == [VERDICT_SUPPORTED]


def test_a_table_label_is_not_counted_as_a_sentence():
    """The coverage ratio must not be padded with column headers and one-word
    cells: nobody would cite "Dose", so counting it understates the report."""
    reg = _registry("https://a.test/1")
    md = (
        "| Protocol | Dose | So what |\n"
        "|---|---|---|\n"
        "| Alfredson | 180/day [1] | Slow but proven over many trials |\n"
    )
    audit = audit_citations(md, reg)
    # The marker in the "180/day" cell is still found and still repairable.
    assert [c.numbers for c in audit.claims] == [[1]]
    # But only the one full-sentence cell counts toward coverage.
    assert audit.total_sentences == 1
    assert audit.cited_sentences == 0


def test_a_full_sentence_in_a_table_cell_does_count():
    reg = _registry("https://a.test/1")
    md = (
        "| Protocol | Verdict |\n"
        "|---|---|\n"
        "| Alfredson | Pain fell measurably over twelve weeks [1] |\n"
    )
    audit = audit_citations(md, reg)
    assert audit.total_sentences == 1
    assert audit.cited_sentences == 1


def test_the_legend_counts_add_up_to_the_citations_it_names():
    reg = _registry("https://a.test/1")
    md = (
        "A properly written sentence that carries a citation [1].\n\n"
        "| Drug | Dose |\n"
        "|---|---|\n"
        "| A | 10mg [1] |\n"
    )
    audit = audit_citations(md, reg)
    checked = check_claims(audit.claims, reg)
    cov = compute_coverage(audit, checked)
    assert cov["cited_sentences"] == 1      # the cell is not a sentence
    assert cov["citations"] == 2            # but it is a citation, and checked
    assert sum(cov["verdicts"].values()) == cov["citations"]
    legend = build_legend(cov, "es")
    assert "2 citas" in legend


def test_coverage_counts_sentences_and_verdicts():
    reg = _checked_registry()
    md = ("El protocolo de Alfredson consiste en 180 repeticiones diarias de "
          "trabajo excentrico durante 12 semanas [1]. Esta frase no lleva cita.\n")
    audit = audit_citations(md, reg)
    cov = compute_coverage(audit, check_claims(audit.claims, reg))
    assert cov["cited_sentences"] == 1
    assert cov["total_sentences"] == 2
    assert cov["verdicts"] == {VERDICT_SUPPORTED: 1, VERDICT_REFUTED: 0,
                               VERDICT_UNCHECKED: 0}


# ---------------------------------------------------------------------------
# The legend — deterministic, and honest about what it is not
# ---------------------------------------------------------------------------


def test_the_legend_says_what_checking_meant_and_what_it_did_not():
    cov = {"cited_sentences": 3, "total_sentences": 4, "citations": 3,
           "verdicts": {VERDICT_SUPPORTED: 1, VERDICT_REFUTED: 1,
                        VERDICT_UNCHECKED: 1}}
    es = build_legend(cov, "es")
    assert es.startswith("## Cómo leer este informe")
    # What checking meant — figures, and only figures.
    assert "buscar las cifras de la frase" in es
    # "sin comprobar" is not a verdict about the claim, and the legend says so.
    assert "no es una objeción a la afirmación" in es
    # And no label may imply scientific strength.
    assert "dice si la afirmación es cierta en el mundo" in es
    assert "3" in es and "4" in es
    en = build_legend(cov, "en")
    assert en.startswith("## How to read this report")
    assert "looking for the sentence's figures" in en
    assert "not an objection to the claim" in en
    assert "whether the claim is true in the world" in en


def test_a_clean_report_does_not_carry_a_zero_accusation():
    """"not in the source: 0" is a sentence about a failure that did not
    happen; printing it invites the reader to look for one."""
    clean = {"cited_sentences": 57, "total_sentences": 80, "citations": 57,
             "verdicts": {VERDICT_SUPPORTED: 6, VERDICT_REFUTED: 0,
                          VERDICT_UNCHECKED: 51}}
    legend = build_legend(clean, "es")
    assert "respaldada: 6" in legend
    assert "sin comprobar: 51" in legend
    assert "no respaldada: 0" not in legend
    dirty = dict(clean, citations=58,
                 verdicts={VERDICT_SUPPORTED: 6, VERDICT_REFUTED: 1,
                           VERDICT_UNCHECKED: 51})
    assert "no respaldada: 1" in build_legend(dirty, "es")


@pytest.mark.parametrize("language", ["es", "en", "fr", "de", "pt", "it"])
def test_every_language_names_all_three_outcomes(language):
    cov = {"cited_sentences": 2, "total_sentences": 2, "citations": 2,
           "verdicts": {VERDICT_SUPPORTED: 1, VERDICT_REFUTED: 1,
                        VERDICT_UNCHECKED: 0}}
    legend = build_legend(cov, language)
    words = VERDICT_WORDS[language]
    for verdict in (VERDICT_SUPPORTED, VERDICT_REFUTED, VERDICT_UNCHECKED):
        assert words[verdict] in legend, verdict


def test_legend_is_empty_when_nothing_was_cited():
    cov = {"cited_sentences": 0, "total_sentences": 5,
           "verdicts": {VERDICT_SUPPORTED: 0, VERDICT_REFUTED: 0,
                        VERDICT_UNCHECKED: 0}}
    assert build_legend(cov, "es") == ""


# ---------------------------------------------------------------------------
# finalize_report — the seam deep_research calls
# ---------------------------------------------------------------------------


def test_finalize_adds_legend_repairs_and_appends_sources():
    reg = _checked_registry()
    md = ("# Tendinopatía rotuliana\n\n"
          "## Hallazgos\n\n"
          "El protocolo de Alfredson consiste en 180 repeticiones diarias de "
          "trabajo excentrico durante 12 semanas [1]. Una invención [8].\n")
    out, audit, checked = finalize_report(md, reg, "es")
    assert out.startswith("# Tendinopatía rotuliana")
    assert "## Cómo leer este informe" in out
    assert out.index("## Cómo leer este informe") < out.index("## Hallazgos")
    assert "[8]" not in out
    assert "## Fuentes" in out
    assert audit.removed == [8]
    assert [c.verdict for c in checked] == [VERDICT_SUPPORTED]
    assert audit.coverage["cited_sentences"] == 1


def test_finalize_is_idempotent():
    reg = _checked_registry()
    md = ("# T\n\nEl protocolo de Alfredson consiste en 180 repeticiones diarias "
          "de trabajo excentrico durante 12 semanas [1]. Otra [8].\n")
    once, _, _ = finalize_report(md, reg, "es")
    twice, audit, _ = finalize_report(once, reg, "es")
    assert once == twice
    assert audit.removed == []


def test_finalize_puts_the_legend_first_when_there_is_no_title():
    reg = _checked_registry()
    md = "El protocolo de Alfredson usa 180 repeticiones diarias [1].\n"
    out, _, _ = finalize_report(md, reg, "es")
    assert out.startswith("## Cómo leer este informe")


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def test_detects_spanish_on_the_real_physiotherapy_question():
    assert detect_language(PHYSIO_QUESTION) == "es"


@pytest.mark.parametrize("text,expected", [
    ("What are the best loading protocols for patellar tendinopathy in "
     "amateur athletes, and how long does recovery usually take?", "en"),
    ("Quels sont les meilleurs protocoles de charge pour la tendinopathie "
     "rotulienne chez les sportifs amateurs et combien de temps dure la "
     "récupération ?", "fr"),
    ("Welche Belastungsprotokolle sind bei der Patellasehnentendinopathie am "
     "besten belegt und wie lange dauert die Genesung bei Amateursportlern?", "de"),
    ("Quais são os melhores protocolos de carga para a tendinopatia patelar "
     "em atletas amadores e quanto tempo leva a recuperação?", "pt"),
    ("Quali sono i migliori protocolli di carico per la tendinopatia rotulea "
     "negli atleti amatoriali e quanto tempo richiede il recupero?", "it"),
])
def test_detects_the_other_supported_languages(text, expected):
    assert detect_language(text) == expected


def test_language_detection_falls_back_to_english_on_junk():
    assert detect_language("") == "en"
    assert detect_language("42 %%% ###") == "en"
    assert detect_language(None) == "en"


def test_sources_heading_follows_the_language():
    assert sources_heading("es") == "## Fuentes"
    assert sources_heading("en") == "## Sources"
    assert sources_heading("zz") == "## Sources"


# ---------------------------------------------------------------------------
# Robustness — this module runs inside a long research job and may never raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("junk", ["", None, 42, "x" * 50000])
def test_audit_survives_junk(junk):
    audit = audit_citations(junk, SourceRegistry())
    assert isinstance(audit.claims, list)
    out, _ = repair_citations(junk, SourceRegistry())
    assert isinstance(out, str)


def test_claim_dataclass_is_comparable():
    a = Claim(text="x [1]", numbers=[1], start=0, end=5)
    assert a.numbers == [1] and a.start == 0 and a.end == 5
