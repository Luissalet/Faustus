"""Embeddings with no model and no network (src/hash_embed.py).

The lane that has to work when everything optional is missing. These tests pin
the four properties the rest of the system leans on:

  * identical text scores exactly 1.0 and disjoint vocabulary scores ~0;
  * adding a word to a sentence keeps it close to the original;
  * the vector is the SAME IN ANOTHER PROCESS — the reason FNV-1a is computed
    here and Python's salted ``hash()`` is never used; and
  * an empty string is a zero vector, and nothing divides by zero.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import hash_embed

REPO = Path(__file__).resolve().parents[1]


# ── FNV-1a itself ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    # The published FNV-1a 32-bit test vectors. If one of these ever changes,
    # every vector ever cached with the old constants is wrong.
    ("", 0x811C9DC5),
    ("a", 0xE40C292C),
    ("foobar", 0xBF9CF968),
])
def test_fnv1a_matches_the_published_vectors(text, expected):
    assert hash_embed.fnv1a_32(text) == expected


def test_fnv1a_is_a_32_bit_value_for_anything_it_is_given():
    for value in ("", "x", "a much longer token", "ñ", "🙂", 12, None):
        digest = hash_embed.fnv1a_32(value)
        assert isinstance(digest, int) and 0 <= digest <= 0xFFFFFFFF


# ── the tokenizer choice ────────────────────────────────────────────────────


def test_tokens_keep_term_frequency_which_is_why_this_tokenizer_was_picked():
    """``personal_docs.tokenize`` returns a SET; ``1 + log(tf)`` needs counts."""
    from src import personal_docs

    assert isinstance(personal_docs.tokenize("run run run"), set)
    assert hash_embed.tokens("Run run RUN.") == ["run", "run", "run"]
    assert hash_embed.term_frequencies("Run run RUN.") == {"run": 3}


def test_tokens_are_the_same_normalisation_the_bm25_lane_uses():
    """Both tier-1 lanes must rank over ONE vocabulary."""
    from src import memory_engine

    text = "Deploy the Docker image; check the GPU."
    assert hash_embed.tokens(text) == memory_engine._tokens(text)


def test_tokens_survive_junk_input():
    for value in (None, "", "   ", 12, ["not", "a", "string"]):
        assert isinstance(hash_embed.tokens(value), list)


# ── the four properties ─────────────────────────────────────────────────────


def test_identical_text_scores_exactly_one():
    text = "the quick brown fox jumps over the lazy dog"
    assert hash_embed.similarity(hash_embed.embed(text), hash_embed.embed(text)) == 1.0


def test_disjoint_vocabulary_scores_near_zero():
    a = hash_embed.embed("quantum chromodynamics lattice gauge renormalisation")
    b = hash_embed.embed("sourdough proofing basket banneton hydration")
    score = hash_embed.similarity(a, b)
    assert 0.0 <= score < 0.15, score


def test_one_extra_word_stays_close():
    base = hash_embed.embed("run a shell command")
    more = hash_embed.embed("run a shell command now")
    assert hash_embed.similarity(base, more) > 0.85


def test_similarity_falls_as_the_texts_diverge():
    base = "run a shell command on the server"
    near = hash_embed.similarity(hash_embed.embed(base),
                                 hash_embed.embed("run a shell command on the host"))
    far = hash_embed.similarity(hash_embed.embed(base),
                                hash_embed.embed("bake a loaf of bread"))
    assert near > far


def test_an_empty_string_is_a_zero_vector_and_never_divides_by_zero():
    vector = hash_embed.embed("")
    assert len(vector) == hash_embed.DIMS
    assert not any(vector)
    # …and every comparison involving it answers 0.0 instead of raising.
    assert hash_embed.similarity(vector, vector) == 0.0
    assert hash_embed.similarity(vector, hash_embed.embed("something")) == 0.0
    # a string with no tokens at all is the same case
    assert not any(hash_embed.embed("   "))


def test_vectors_are_unit_length_and_the_right_width():
    for text, dims in (("hello world", 384), ("hello world", 64), ("a b c d e", 8)):
        vector = hash_embed.embed(text, dims)
        assert len(vector) == dims
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_sublinear_weighting_is_actually_sublinear():
    """Five occurrences of a word must not count five times as much as one."""
    once = hash_embed.embed("alpha beta")
    many = hash_embed.embed("alpha alpha alpha alpha alpha beta")
    weight_once = 1.0 + math.log(1)
    weight_many = 1.0 + math.log(5)
    assert weight_many < 5 * weight_once
    # the repeated word dominates, but does not annihilate the other one
    assert 0.5 < hash_embed.similarity(once, many) < 1.0


@pytest.mark.parametrize("dims", [0, -5, None, "nonsense"])
def test_a_nonsense_width_falls_back_to_the_default(dims):
    assert len(hash_embed.embed("hello", dims)) == hash_embed.DIMS


def test_similarity_is_defensive_about_everything_a_caller_can_pass():
    assert hash_embed.similarity(None, None) == 0.0
    assert hash_embed.similarity([], [1.0]) == 0.0
    assert hash_embed.similarity([1.0, 0.0], [1.0]) == 0.0        # widths differ
    assert hash_embed.similarity([0.0, 0.0], [1.0, 0.0]) == 0.0   # zero vector
    assert hash_embed.similarity(["x", "y"], [1.0, 0.0]) == 0.0   # not numbers
    # a raw (unnormalised) pair still answers a cosine
    assert hash_embed.similarity([3.0, 0.0], [7.0, 0.0]) == pytest.approx(1.0)
    assert hash_embed.similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_embed_many_and_rank():
    docs = [("a", "docker compose gpu runtime"), ("b", "sourdough starter"), ("c", "")]
    assert len(hash_embed.embed_many(["x", "y"])) == 2
    assert hash_embed.embed_many([]) == []
    ranked = hash_embed.rank("gpu runtime docker", docs)
    assert ranked and ranked[0][0] == "a"
    assert all(doc_id != "c" for doc_id, _ in ranked), "an empty doc never scores"
    assert hash_embed.rank("", docs) == []


def test_rank_ties_break_on_the_id_so_the_order_is_total():
    docs = [("zulu", "same text here"), ("alpha", "same text here")]
    assert [doc_id for doc_id, _ in hash_embed.rank("same text here", docs)] == ["alpha", "zulu"]


# ── the one that matters most: another process ──────────────────────────────


_SUBPROCESS = """
import json, sys
sys.path.insert(0, {repo!r})
from src import hash_embed
print(json.dumps({{
    "fnv": hash_embed.fnv1a_32("run a shell command"),
    "vector": hash_embed.embed("run a shell command"),
    "buckets": [hash_embed.fnv1a_32(t) % 384 for t in ("run", "shell", "command")],
}}))
"""


@pytest.mark.parametrize("seed", ["0", "1", "12345"])
def test_the_vector_is_identical_in_another_process(seed):
    """PYTHONHASHSEED changes ``hash()`` and must change nothing here.

    This is the whole reason FNV-1a is implemented in the module: a vector
    cached by one process and compared by the next has to be the same vector.
    """
    env = dict(os.environ, PYTHONHASHSEED=seed)
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS.format(repo=str(REPO))],
        capture_output=True, text=True, env=env, timeout=120, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["fnv"] == hash_embed.fnv1a_32("run a shell command")
    assert out["buckets"] == [hash_embed.fnv1a_32(t) % 384
                              for t in ("run", "shell", "command")]
    assert out["vector"] == hash_embed.embed("run a shell command")
    assert hash_embed.similarity(out["vector"],
                                 hash_embed.embed("run a shell command")) == 1.0


def test_the_module_calls_nothing_that_varies_between_runs():
    """A source-level guard over the AST, because the failure it prevents is
    invisible: a salted hash produces a plausible vector that simply does not
    match the one the previous process wrote."""
    import ast

    tree = ast.parse((REPO / "src" / "hash_embed.py").read_text(encoding="utf-8"))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "hash" not in called, "hash() is salted per process"
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not ({"random", "time", "uuid", "secrets"} & imported), sorted(imported)
    # …and stdlib only: nothing from outside the standard library or this app.
    assert imported <= {"logging", "math", "functools", "typing", "src", "__future__"}
