"""Lote 91 (OBJ-7) -- src.tool_index_examples and its wiring into tool_index.

Luis: "un usuario no debería saberse de memoria todas las tools; 'dime
nosequé para el proyecto X' tiene que bastar." This covers three things:

1. `EXAMPLES` (src/tool_index_examples.py) has a natural-language entry for
   every tool in `src.agent_tools.TOOL_HANDLERS` (>= 2 phrases; most tools
   get 4-8), plus the board_* tools from lote 92 even though they are not
   registered yet.
2. `src.tool_index.index_builtin_tools` folds those examples into the text
   it indexes/searches (the one-line change plus its accent-folding
   helper, `_examples_block`), so a colloquial request that never names the
   tool still surfaces it — in BOTH the embedding lane's indexed documents
   and the lexical/keyword fallback used when no vector lane can answer.
3. The fallback matches an unaccented Spanish query against an accented
   example (and vice versa): `hash_embed.tokens` (its tokenizer) does not
   fold Unicode on its own, so `_examples_block` folds a duplicate of the
   examples text without accents into the same indexed document.

The failing-first check for (1)/(2): before this lot, `tool_index.py` had no
`src.tool_index_examples` import at all and `BUILTIN_TOOL_DESCRIPTIONS`
carried no example phrases, so a plain "mándale un correo a Marta" was not
findable by literal/lexical text match against `send_email`'s description
(which never says "mándale" or "Marta"). That gap is asserted directly below
via `_examples_block`/`corpus_rows` rather than re-derived by reverting code.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.agent_tools import TOOL_HANDLERS
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex, _examples_block
from src.tool_index_examples import EXAMPLES, BOARD_TOOL_NAMES


# ---------------------------------------------------------------------------
# 1. Coverage
# ---------------------------------------------------------------------------

def test_every_registered_tool_has_examples():
    missing = sorted(n for n in TOOL_HANDLERS if n not in EXAMPLES)
    assert not missing, f"tools with no EXAMPLES entry: {missing}"


def test_every_indexed_builtin_tool_has_examples():
    # The corpus tool_index actually embeds/searches is BUILTIN_TOOL_DESCRIPTIONS,
    # a superset of TOOL_HANDLERS (some entries are optional/plugin tools).
    # Covering the superset means every phrase the benchmark can throw at
    # retrieve() has a fighting chance of ranking the tool it means.
    missing = sorted(n for n in BUILTIN_TOOL_DESCRIPTIONS if n not in EXAMPLES)
    assert not missing, f"indexed tools with no EXAMPLES entry: {missing}"


def test_board_tools_have_examples_regardless_of_registration():
    # Lote 92 (board backend) runs in parallel and may land board_* into
    # TOOL_HANDLERS before or after this lot finishes — EXAMPLES must cover
    # them either way, since tool_index only *consumes* the dict (it never
    # requires the tool to already exist; an unregistered name here is just
    # inert data, per BRIEF_CIERRE/CONTRATO_BOARD's "pending" framing).
    for name in sorted(BOARD_TOOL_NAMES):
        assert name in EXAMPLES, name
        assert len(EXAMPLES[name]) >= 2, name


def test_minimum_phrase_count_per_tool():
    # >= 2 for every tool (the brief's floor for internal/rare tools).
    short = sorted(name for name, phrases in EXAMPLES.items() if len(phrases) < 2)
    assert not short, f"tools with fewer than 2 examples: {short}"


def test_most_tools_have_richer_coverage():
    # Not every tool needs to be rare/internal — most should carry more than
    # the bare minimum of 2.
    average = sum(len(v) for v in EXAMPLES.values()) / len(EXAMPLES)
    assert average >= 3.5, average


def test_examples_never_name_the_tool_by_its_snake_case_identifier():
    # A "natural" example should read like something a person typed, not an
    # API call — catches an accidental "use send_email to ..." phrase. Only
    # checked for multi-word (snake_case) tool names: a single-word name
    # like "pipeline" or "grep" is also an ordinary English word and is
    # expected to show up in natural phrasing.
    offenders = []
    for name, phrases in EXAMPLES.items():
        if "_" not in name:
            continue
        for phrase in phrases:
            if name in phrase:
                offenders.append((name, phrase))
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# 2. tool_index consumes EXAMPLES
# ---------------------------------------------------------------------------

def test_examples_block_folds_phrases_and_an_unaccented_copy():
    block = _examples_block("send_email")
    for phrase in EXAMPLES["send_email"]:
        assert phrase in block
    # The literal accented example is present...
    assert "mándale un correo a Marta con el resumen" in block
    # ...and so is an accent-folded copy of the whole block, so a query
    # typed without accents still finds it on token identity.
    assert "mandale un correo a Marta con el resumen" in block


def test_examples_block_empty_for_unknown_tool():
    assert _examples_block("this_tool_does_not_exist") == ""


def test_index_builtin_tools_embeds_examples_in_indexed_text():
    idx = ToolIndex(force_memory=True)
    idx.index_builtin_tools()
    rows = {row["id"]: row["text"] for row in idx.corpus_rows()}
    assert "send_email" in rows
    assert "mándale un correo a Marta con el resumen" in rows["send_email"]
    # If board_* has landed (BUILTIN_TOOL_DESCRIPTIONS grows once lote 92
    # registers it), its own examples must be indexed the same way; if not,
    # there is simply nothing to index yet — either is correct.
    if "board_create" in BUILTIN_TOOL_DESCRIPTIONS:
        assert EXAMPLES["board_create"][0] in rows["board_create"]
    else:
        assert "board_create" not in rows


@pytest.mark.parametrize(
    "accented,plain",
    [
        ("mándale un correo a Marta con el resumen", "mandale un correo a Marta con el resumen"),
        ("qué hay pendiente en este proyecto", "que hay pendiente en este proyecto"),
    ],
)
def test_lexical_fallback_matches_with_and_without_accents(accented, plain):
    idx = ToolIndex(force_memory=True)
    idx.index_builtin_tools()
    with_accents = idx.lexical_retrieve(accented, k=8)
    without_accents = idx.lexical_retrieve(plain, k=8)
    assert with_accents, "accented query returned nothing"
    assert without_accents, "unaccented query returned nothing"
    # Both spellings should reach essentially the same ranking — the
    # tokenizer itself does not fold accents, so this only holds because the
    # indexed document carries the folded duplicate too.
    assert with_accents[0] == without_accents[0]
