"""Abbreviations must not let unrelated-language memories win exact retrieval."""
import pytest

from src import memory_engine


@pytest.mark.parametrize("alias,language,other", [
    ("JS", "JavaScript", "Python"),
    ("TS", "TypeScript", "Python"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_programming_language_aliases_retrieve_both_directions(alias, language, other, reverse):
    query_name, document_name = (language, alias) if reverse else (alias, language)
    scores = memory_engine.bm25_scores(f"{query_name} indentation", [
        ("wanted", f"Use tabs for {document_name} indentation."),
        ("other", f"Use spaces for {other} indentation."),
    ])
    assert scores["wanted"] > scores["other"]


@pytest.mark.parametrize("query,document", [
    ("TS timestamp", "TypeScript timestamp"),
    ("JS initials", "JavaScript initials"),
    ("Go home", "Golang home"),
    ("R address", "Rlang address"),
])
def test_ambiguous_terms_do_not_become_programming_languages(query, document):
    scores = memory_engine.bm25_scores(query, [("literal", query), ("unrelated", document)])
    assert scores["literal"] > scores["unrelated"]
    assert memory_engine._tokens(query) == query.lower().split()


def test_filenames_and_identifiers_remain_distinct():
    tokens = memory_engine._tokens("code app.js app.ts js_config ts_config JS TS")
    assert tokens == ["code", "app.js", "app.ts", "js_config", "ts_config", "javascript", "typescript"]


def test_language_alias_is_not_a_substring_replacement():
    assert memory_engine._tokens("code JSON tests") == ["code", "json", "tests"]
