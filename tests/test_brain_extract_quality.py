"""Entity-extraction quality: function words are not names, and the model's
relations stay inside the closed vocabulary.

Observed on real mixed Spanish/English memories: a sentence such as
"En la carpeta de proyectos ..." produced an entity called "En", and the
background model produced relations like ``intocable``, ``create`` ->
"carpetas" or ``has_account_named``. These tests pin the filter that stops
both, in the rule pass and as a post-filter on model output.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.brain import db as brain_db  # noqa: E402
from src.brain import entities  # noqa: E402
from src.brain import extract  # noqa: E402
from src import memory_engine as engine  # noqa: E402

OWNER = "alice"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    brain_db.use_dir(str(tmp_path / "brain"))
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path / "engine"))
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "engine"))
    engine.set_vector_store(None)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint",
                        lambda *a, **k: ("http://local/v1", "local-test", {}))
    yield tmp_path
    brain_db.use_dir(None)
    engine.reset_vector_store()


def _names():
    return {e["name"] for e in entities.list_entities(OWNER, include_hidden=True)}


def _model_replies(monkeypatch, payload):
    async def _fake_call(*a, **k):
        return json.dumps(payload)

    monkeypatch.setattr("src.llm_core.llm_call_async", _fake_call)


def _report():
    return {"entities": 0, "relations": 0, "errors": 0}


# ---------------------------------------------------------------------------
# the context-free name filter
# ---------------------------------------------------------------------------

FUNCTION_WORDS_SAMPLE = [
    "En", "Para", "Todo", "La", "El", "Los", "Las", "Un", "Una", "Cada", "Si", "No",
    "Luego", "Cuando", "Donde", "Antes", "Después", "Hay", "Es", "Se", "Por", "Con",
    "Sin", "Sobre", "Tras", "Este", "Esta", "Estos", "Ese", "Esa", "Aquel", "Nunca",
    "Siempre", "También", "The", "A", "An", "In", "On", "For", "When", "If", "Always",
    "Never", "This", "That", "These", "Those", "It", "We", "I", "You",
]


@pytest.mark.parametrize("word", FUNCTION_WORDS_SAMPLE)
def test_function_words_are_never_valid_entity_names(word):
    assert entities.is_function_word(word)
    assert not entities.valid_entity_name(word)


@pytest.mark.parametrize("name", ["x", "7", "2024", "12 34", "En La", "Para el"])
def test_short_numeric_or_all_function_names_are_invalid(name):
    assert not entities.valid_entity_name(name)


@pytest.mark.parametrize("name", ["Ada", "Cordera Labs", "Banco de Villanueva", "Python", "R2",
                                  "Las Palmeras", "El Salvador"])
def test_real_names_are_valid(name):
    assert entities.valid_entity_name(name)


def test_clean_name_strips_leading_and_trailing_function_words():
    assert entities.clean_name("En Cordera Labs") == "Cordera Labs"
    assert entities.clean_name("Para Bluehaven Tras") == "Bluehaven"
    assert entities.clean_name("Banco de Villanueva") == "Banco de Villanueva"
    assert entities.clean_name("En La") == ""


def test_names_with_a_leading_function_word_are_invalid():
    # the cleaned form is what should exist, not this one
    assert not entities.valid_entity_name("En Cordera Labs")


@pytest.mark.parametrize("name", ["El modelo", "La carpeta", "The model"])
def test_article_plus_ordinary_lowercase_word_is_not_a_name(name):
    # a leading article followed only by a common noun (no capital of its
    # own) is a common noun phrase, not a name — "El modelo" and "La
    # carpeta" were showing up as entities before this filter.
    assert not entities.valid_entity_name(name)


@pytest.mark.parametrize("name", ["El Salvador", "La Rioja", "The Hague"])
def test_article_plus_capitalised_word_is_still_a_real_name(name):
    assert entities.valid_entity_name(name)


# ---------------------------------------------------------------------------
# proper-noun evidence in a source text
# ---------------------------------------------------------------------------


def test_sentence_initial_single_word_needs_more_evidence():
    assert not entities.proper_noun_in_source("Carpeta", "Carpeta nueva para los informes.")


def test_single_word_mid_sentence_capitalised_counts():
    assert entities.proper_noun_in_source("Python", "I use Python every day.")
    assert entities.proper_noun_in_source(
        "Bruno", "Bruno dice hola. Luego llamamos a Bruno.")


def test_single_word_with_a_second_capitalised_occurrence_counts():
    assert entities.proper_noun_in_source(
        "Ada", "Ada writes the tests. Ada reviews them too.")


def test_sentence_initial_single_word_counts_when_the_caller_allows_it():
    assert entities.proper_noun_in_source("Ada", "Ada works at Cordera Labs.",
                                           allow_sentence_initial=True)


def test_lowercase_common_nouns_are_not_proper_nouns():
    text = "Crear carpetas con nombres raros es intocable."
    assert not entities.proper_noun_in_source("carpetas", text)
    assert not entities.proper_noun_in_source("nombres raros", text)


def test_multiword_needs_every_content_word_capitalised():
    assert entities.proper_noun_in_source("Banco de Villanueva", "Abrí una cuenta en el Banco de Villanueva.")
    assert not entities.proper_noun_in_source("Nombres raros", "Nombres raros por todas partes.")


def test_function_word_is_never_a_proper_noun_even_mid_sentence():
    assert not entities.proper_noun_in_source("Todo", "Lo dejo así. Todo lo demás, Todo igual.")


# ---------------------------------------------------------------------------
# rule pass
# ---------------------------------------------------------------------------


def test_rule_pass_never_creates_an_entity_from_a_sentence_initial_function_word(store):
    extract.extract_source(OWNER, "mem:1", "En la carpeta de informes usa Python siempre.")
    assert "En" not in _names()
    extract.extract_source(OWNER, "mem:2", "Para todo lo demás prefiere tabs.")
    extract.extract_source(OWNER, "mem:3", "Todo el equipo usa Bluehaven.")
    extract.extract_source(OWNER, "mem:4", "Luego trabaja en Cordera Labs.")
    names = _names()
    for bad in ("En", "Para", "Todo", "Luego", "Para todo"):
        assert bad not in names


def test_rule_pass_strips_function_words_off_multiword_proper_nouns(store):
    extract.extract_source(OWNER, "mem:1", "En Cordera Labs revisamos los contratos cada mes.")
    names = _names()
    assert "En Cordera Labs" not in names
    assert "Cordera Labs" in names


def test_rule_pass_keeps_real_subjects_and_objects(store):
    result = extract.extract_source(OWNER, "mem:1", "Ada trabaja en Cordera Labs.")
    assert {e["name"] for e in result["entities"]} >= {"Ada", "Cordera Labs"}
    assert [r["rel"] for r in result["relations"]] == ["works_at"]


def test_rule_pass_subject_after_a_function_word_is_the_name(store):
    result = extract.extract_source(OWNER, "mem:1", "Luego Bruno usa Python.")
    names = {e["name"] for e in result["entities"]}
    assert "Bruno" in names
    assert "Luego Bruno" not in names and "Luego" not in names


def test_known_function_word_entity_never_matches_free_text(store):
    # a junk entity created before this filter existed must not be matched
    # (and re-mentioned) in every text containing that word
    junk = entities.upsert_entity(OWNER, "En")
    found = entities.entities_in_text(OWNER, "Vivo en Madrid y trabajo en casa")
    assert junk["id"] not in {e["id"] for e in found}


def test_self_aliases_still_match(store):
    me = entities.self_entity(OWNER)
    found = entities.entities_in_text(OWNER, "yo uso tabs")
    assert me["id"] in {e["id"] for e in found}


# ---------------------------------------------------------------------------
# relation vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("works_at", "works_at"), ("works at", "works_at"), ("trabaja en", "works_at"),
    ("works for", "works_at"), ("vive en", "lives_in"), ("usa", "uses"),
    ("utiliza", "uses"), ("prefiere", "prefers"), ("es parte de", "part_of"),
    ("creó", "created"), ("created", "created"), ("conoce", "knows"),
    ("pertenece a", "member_of"), ("member of", "member_of"), ("depende de", "depends_on"),
    ("estudió en", "studied_at"), ("es un", "is_a"), ("is a", "is_a"),
    ("relacionado con", "related_to"), ("Works-At", "works_at"),
])
def test_relation_synonyms_map_to_the_vocabulary(raw, expected):
    assert entities.canonical_relation(raw) == expected


@pytest.mark.parametrize("raw", ["intocable", "create", "has_account_named", "likes", "", "   "])
def test_relations_outside_the_vocabulary_do_not_map(raw):
    assert entities.canonical_relation(raw) is None


# ---------------------------------------------------------------------------
# model output post-filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_function_word_and_common_noun_entities_are_dropped(store, monkeypatch):
    batch = [{"source_ref": "mem:1",
              "text": "En la carpeta de Ada, crear carpetas con nombres raros es intocable. Todo bien."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "En", "type": "other"}, {"name": "Todo", "type": "concept"},
                     {"name": "carpetas", "type": "concept"},
                     {"name": "nombres raros", "type": "concept"},
                     {"name": "Ada", "type": "person"}],
        "relations": [],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    assert _names() == {"Ada"}


@pytest.mark.asyncio
async def test_model_relations_outside_the_vocabulary_are_dropped(store, monkeypatch):
    batch = [{"source_ref": "mem:1",
              "text": "Ada dice que crear carpetas con nombres raros es intocable; "
                      "Ada tiene una cuenta llamada adita."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}],
        "relations": [
            {"src": "Ada", "rel": "intocable", "dst": "carpetas"},
            {"src": "Ada", "rel": "create", "dst": "carpetas"},
            {"src": "Ada", "rel": "create", "dst": "nombres raros"},
            {"src": "Ada", "rel": "has_account_named", "dst": "adita"},
        ],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    ada = entities.upsert_entity(OWNER, "Ada")
    assert entities.list_relations(OWNER, entity_id=ada["id"]) == []


@pytest.mark.asyncio
async def test_model_relation_synonyms_are_stored_canonically(store, monkeypatch):
    batch = [{"source_ref": "mem:1", "text": "Ada trabaja en Cordera Labs y vive en Villanueva."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"},
                     {"name": "Cordera Labs", "type": "organization"},
                     {"name": "Villanueva", "type": "place"}],
        "relations": [{"src": "Ada", "rel": "trabaja en", "dst": "Cordera Labs"},
                      {"src": "Ada", "rel": "works for", "dst": "Cordera Labs"},
                      {"src": "Ada", "rel": "vive en", "dst": "Villanueva"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    ada = entities.upsert_entity(OWNER, "Ada")
    rels = {r["rel"] for r in entities.list_relations(OWNER, entity_id=ada["id"])}
    assert rels == {"works_at", "lives_in"}


@pytest.mark.asyncio
async def test_model_literal_destination_longer_than_40_chars_is_dropped(store, monkeypatch):
    tail = "a very long description of tooling preferences that goes on"
    batch = [{"source_ref": "mem:1", "text": f"Ada prefers {tail}."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"}],
        "relations": [{"src": "Ada", "rel": "prefers", "dst": tail},
                      {"src": "Ada", "rel": "prefers", "dst": "tooling preferences"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    ada = entities.upsert_entity(OWNER, "Ada")
    values = [r["dst_value"] for r in entities.list_relations(OWNER, entity_id=ada["id"])]
    assert values == ["tooling preferences"]


@pytest.mark.asyncio
async def test_model_common_noun_destination_is_a_literal_not_an_entity(store, monkeypatch):
    batch = [{"source_ref": "mem:1", "text": "Ada usa carpetas compartidas para todo."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Ada", "type": "person"},
                     {"name": "carpetas compartidas", "type": "concept"}],
        "relations": [{"src": "Ada", "rel": "usa", "dst": "carpetas compartidas"}],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    assert _names() == {"Ada"}
    ada = entities.upsert_entity(OWNER, "Ada")
    rels = entities.list_relations(OWNER, entity_id=ada["id"])
    assert [(r["rel"], r["dst"], r["dst_value"]) for r in rels] == \
        [("uses", "", "carpetas compartidas")]


@pytest.mark.asyncio
async def test_model_sentence_initial_concept_word_is_dropped(store, monkeypatch):
    batch = [{"source_ref": "mem:1", "text": "Carpeta compartida para informes."}]
    _model_replies(monkeypatch, {
        "entities": [{"name": "Carpeta", "type": "concept"}],
        "relations": [],
    })
    await extract._extract_llm_batch(OWNER, batch, _report())
    assert "Carpeta" not in _names()
