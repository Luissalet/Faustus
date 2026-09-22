"""Accents are spelling, not vocabulary (src/hash_embed.fold_accents).

Every lexical lane in this app reads `hash_embed.tokens`: BM25 inside
`two_tier_search`, the hash vectors beside it, memory search and expert
search. Before folding, `últimas` and `ultimas` were two different words to
all of them, so a user typing quickly — which means typing without accents —
scored zero against an index built from their own accented text.

It is not a hypothetical: the request that started this was typed "Que
aplicaciones mias puedes usar", and the examples in the catalogue are
written "qué aplicaciones mías".
"""
from __future__ import annotations

import pytest

from src import hash_embed
from src.two_tier_search import bm25_scores


PAIRS = [
    ("últimas noticias", "ultimas noticias"),
    ("qué aplicaciones mías", "que aplicaciones mias"),
    ("añade una función", "anade una funcion"),
    ("diseño rápido", "diseno rapido"),
    ("enséñamela", "ensenamela"),
    ("¿cuándo fue la última vez?", "cuando fue la ultima vez"),
]


@pytest.mark.parametrize("accented, plain", PAIRS)
def test_the_same_words_tokenise_the_same_with_or_without_accents(accented, plain):
    assert hash_embed.tokens(accented) == hash_embed.tokens(plain)


@pytest.mark.parametrize("accented, plain", PAIRS)
def test_and_therefore_embed_to_the_same_vector(accented, plain):
    assert hash_embed.embed(accented) == hash_embed.embed(plain)


def test_a_query_without_accents_still_scores_against_text_that_has_them():
    docs = [
        ("right", "Busca en la web las últimas noticias de la semana"),
        ("wrong", "Convierte un documento a PDF y lo guarda en disco"),
    ]
    scores = bm25_scores("ultimas noticias", docs)
    assert scores.get("right", 0) > 0, "the lexical lane went silent on a real match"
    assert scores.get("right", 0) > scores.get("wrong", 0)


def test_ascii_text_is_left_exactly_as_it_was():
    """Folding must cost nothing for the language most of the catalogue is
    written in — and `fold_accents` returns ASCII untouched, so every English
    token keeps the vocabulary entry it already had."""
    for token in ("bash", "read_file", "web_search", "commit", "server.py"):
        assert hash_embed.fold_accents(token) == token
    assert hash_embed.tokens("run a shell command") == ["run", "a", "shell", "command"]


def test_folding_is_idempotent():
    once = hash_embed.tokens("añádeme la función")
    assert hash_embed.tokens(" ".join(once)) == once


def test_a_token_that_is_only_marks_does_not_become_an_empty_one():
    """Stripping combining characters can empty a token. An empty token must
    never reach the vocabulary — it would collide with every other one."""
    assert "" not in hash_embed.tokens("́ ̈")
    assert all(tok for tok in hash_embed.tokens("café ́ té"))
