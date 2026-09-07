import pytest

from src.research_citations import audit_citations


@pytest.mark.parametrize('text', [
    'The results are shown in Fig. 3 with a clear improvement [1].',
    'Los resultados aparecen en la fig. 3 con una mejora clara [1].',
    'This was measured by Dr. Smith in the same sample [1].',
    'La medición corresponde a la Dra. Pérez durante la visita [1].',
    'Se compararon ciudades, p. ej. Madrid y Barcelona, con resultados similares [1].',
    'We used examples, e.g. Madrid and Barcelona, in the comparison [1].',
])
def test_abbreviation_keeps_the_citation_attached_to_the_complete_claim(text):
    audit = audit_citations(text)
    assert len(audit.claims) == 1
    assert audit.claims[0].text == text
    assert audit.total_sentences == audit.cited_sentences == 1


def test_numeric_sentence_after_postposed_citation_stays_separate():
    text = 'Pain fell by 40%. [1] 30 participants completed the trial [2].'
    audit = audit_citations(text)
    assert [c.numbers for c in audit.claims] == [[1], [2]]
    assert audit.claims[0].text == 'Pain fell by 40%. [1]'
    assert audit.claims[1].text == '30 participants completed the trial [2].'


def test_closing_quote_is_part_of_the_original_claim():
    audit = audit_citations('They reported "the treatment helped [1]." Another trial found no difference [2].')
    assert audit.claims[0].text.endswith('[1]."')
    assert audit.claims[1].text == 'Another trial found no difference [2].'


def test_ordinary_periods_still_split_claims():
    audit = audit_citations('The participants improved after treatment [1]. The researchers confirmed this later [2].')
    assert len(audit.claims) == 2
