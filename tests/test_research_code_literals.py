"""Flags and code in a report sentence are checked against the cited source
like figures are (found live: `--rope-scaling ntk` with no source for it)."""
from src.research_citations import _verdict_of, build_legend, code_literals, VERDICT_REFUTED, VERDICT_SUPPORTED, VERDICT_UNCHECKED

SOURCE = "Use --rope-scaling yarn with --yarn-orig-ctx 32768 and --rope-scale 4 to extend the context."


def test_literals_are_backticks_and_bare_flags():
    s = "Para NTK usa `--rope-scaling ntk` y también --rope-scale [3]."
    assert code_literals(s) == ["--rope-scaling ntk", "--rope-scale"]
    assert code_literals("Sin código aquí [2].") == []
    assert code_literals("Ver `[3]` y `7`.") == []


def test_a_flag_the_source_lacks_is_refuted_even_if_the_ladder_agreed():
    sentence = "En llama.cpp el escalado NTK se activa con `--rope-scaling ntk`."
    assert _verdict_of({"supported": True, "layer": 1}, sentence, SOURCE) == VERDICT_REFUTED


def test_flags_in_the_source_support_a_sentence_without_figures():
    sentence = "YaRN se activa con `--rope-scaling yarn` y `--yarn-orig-ctx`."
    assert _verdict_of({"supported": False, "layer": 5}, sentence, SOURCE) == VERDICT_SUPPORTED
    assert _verdict_of({"supported": False}, "Se usa `--rope-scale=4`.", SOURCE) == VERDICT_SUPPORTED


def test_without_code_or_figures_nothing_changes():
    assert _verdict_of({"supported": False, "layer": 5}, "YaRN extiende el contexto.", SOURCE) == VERDICT_UNCHECKED


def test_the_legend_says_code_is_checked_too():
    text = build_legend({"cited_sentences": 2, "total_sentences": 4, "verdicts": {}, "citations": 2}, "es")
    assert "comillas invertidas" in text
    assert text.count("\n\n") == 3, "heading plus the same three paragraphs"
