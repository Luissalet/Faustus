"""Behaviour that two near-identical-looking text helpers must each keep.

``src/story_bible.py`` and ``src/research_citations.py`` both split prose into
sentences; ``src/research_citations.py`` and ``src/visual_report.py`` both find
the stretches of markdown where text is code rather than prose. Each pair reads
like the same function written twice, and collapsing either one silently
changes a number a reader is shown. These tests pin the reasons, so the next
attempt fails loudly here instead of quietly in a report.

Nothing below asserts that the implementations differ — only what each one has
to do for its own caller.
"""
from __future__ import annotations

import pytest

from src import research_citations as rc
from src import story_bible as sb
from src import visual_report as vr


# ---------------------------------------------------------------------------
# Sentence segmentation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, abbreviation", [
    ("El efecto se mantuvo a los seis meses, p. ej. en la cohorte [2].", "p. ej."),
    ("The rest, e.g. atrophy, followed [3].", "e.g."),
    ("Reported by Smith et al. in the same cohort [4].", "et al."),
    # "Fig. 3" is deliberately absent: a digit counts as a sentence start, so
    # that one does split. Keeping "40%. [1] Recovery" whole is worth more.
])
def test_a_citation_unit_does_not_break_at_an_abbreviation(text, abbreviation):
    """A research report is counted, not just read.

    Every unit here is a row in "N of M sentences carry a citation", printed in
    the report's own legend. Splitting "p. ej." into a unit of its own adds a
    two-word fragment nobody could cite to M, so the percentage the report
    prints about itself drops for a reason that is not about the report.
    """
    units = [text[s:e] for s, e in rc._sentence_units(text, [])]
    assert any(abbreviation in unit for unit in units), units
    assert not any(unit.strip().rstrip(".") in {"p", "ej", "e.g", "et al", "Fig"}
                   for unit in units), units


def test_a_narrative_paragraph_break_ends_a_sentence():
    """Story prose is not punctuated like a report.

    ``story_bible`` reads chapters where a line ends on a blank line rather
    than a full stop; the blank line is the only sentence boundary there is.
    Its callers extract character and place candidates per sentence, so running
    two paragraphs together invents a name run that spans the break.
    """
    text = "Marta entró en la cocina\n\nEl reloj marcaba las tres"
    assert [piece for _s, _e, piece in sb.sentences(text)] == [
        "Marta entró en la cocina", "El reloj marcaba las tres",
    ]


def test_sentence_offsets_point_back_into_the_original_text():
    text = "Primera frase. Segunda frase."
    for start, end, piece in sb.sentences(text):
        assert text[start:end] == piece


# ---------------------------------------------------------------------------
# Protected regions in markdown
# ---------------------------------------------------------------------------

def test_an_unterminated_code_fence_hides_its_brackets_from_the_audit():
    """A model that ran out of tokens mid-snippet must not mint sources.

    ``rows[1]`` in a code block the model never closed is an index expression.
    Read as a citation it becomes a claim attributed to source 1, in a report
    whose whole promise is that a number points at a page we actually stored.
    """
    truncated = "## Método\n\n```python\ndata = rows[1]\ntotal = cols[2]\n"
    assert rc.find_markers(truncated) == []


def test_the_linkifier_leaves_a_url_alone_inside_markup_it_would_corrupt():
    """The linkifier rewrites the text it scans, so its protected set is wider.

    A URL inside an href, an existing markdown link or a code span is content.
    Wrapping it in ``[url](url)`` there produces broken markup, which is why
    this scanner covers HTML tags and links that the audit's does not.
    """
    for source in ('<a href="http://a.b/c">x</a>',
                   "[label](http://a.b/c)",
                   "`http://a.b/c`",
                   "[1]: http://a.b/c"):
        assert vr._autolink_urls(source) == source, source

    assert vr._autolink_urls("see http://a.b/c now") == (
        "see [http://a.b/c](http://a.b/c) now"
    )
