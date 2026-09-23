"""Tests for `src/brain/render.py`'s entity note: fact citations that link to
the note actually holding them, a deduplicated "Mentioned in" of real
wikilinks, and a separate short-dated "Timeline" section. Pure functions —
no store, no filesystem (see the module docstring on why `render.py` stays
that way)."""

from __future__ import annotations

from src.brain import render

ENTITY = {
    "id": "e1", "name": "Ada Lovelace", "type": "person",
    "created_at": "2025-01-01T00:00:00Z", "updated_at": "2025-01-01T00:00:00Z",
}


def _empty_profile(**overrides):
    base = {"facts": [], "relations": [], "history": [], "timeline": [],
            "summary": "", "summary_sources": []}
    base.update(overrides)
    return base


def _section(generated: str, heading: str) -> str:
    marker = f"## {heading}"
    start = generated.index(marker) + len(marker)
    rest = generated[start:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


# ---------------------------------------------------------------------------
# Facts: real wikilinks when resolvable, plain citation otherwise
# ---------------------------------------------------------------------------


def test_fact_links_to_the_resolved_memory_note_title():
    profile = _empty_profile(facts=[
        {"source_ref": "mem:e8f6afac0000", "text": "Ada Lovelace trabaja en Bluehaven desde marzo de 2026"},
    ])
    titles = {"mem:e8f6afac0000": "Ada Lovelace trabaja en Bluehaven desde marzo de 2026 (e8f6afac)"}
    _, _, generated = render.entity_note_parts(ENTITY, profile, titles=titles)
    facts = _section(generated, "Facts")
    assert "[[Ada Lovelace trabaja en Bluehaven desde marzo de 2026 (e8f6afac)]]" in facts
    assert "`mem:" not in facts


def test_fact_falls_back_to_a_plain_citation_when_unresolvable():
    profile = _empty_profile(facts=[
        {"source_ref": "mem:deadbeef0000", "text": "Some fact"},
    ])
    _, _, generated = render.entity_note_parts(ENTITY, profile, titles={})
    facts = _section(generated, "Facts")
    assert "Some fact (`mem:deadbeef`)" in facts
    assert "[[" not in facts


def test_pmem_fact_is_cited_with_its_own_prefix_not_mem():
    profile = _empty_profile(facts=[
        {"source_ref": "pmem:abcdef123456", "text": "Personal note fact"},
    ])
    _, _, generated = render.entity_note_parts(ENTITY, profile, titles={})
    facts = _section(generated, "Facts")
    assert "`pmem:abcdef12`" in facts


def test_free_note_fact_links_by_its_own_path_without_a_titles_registry():
    profile = _empty_profile(facts=[
        {"source_ref": "note:Notes/About Ada.md", "text": "Ada likes tea."},
    ])
    _, _, generated = render.entity_note_parts(ENTITY, profile)  # titles=None
    facts = _section(generated, "Facts")
    assert "[[About Ada]]" in facts


# ---------------------------------------------------------------------------
# Mentioned in: deduplicated, sorted, real links only
# ---------------------------------------------------------------------------


def test_mentioned_in_deduplicates_and_sorts_and_includes_inbound_entity_links():
    profile = _empty_profile(
        facts=[
            {"source_ref": "mem:aaa1", "text": "fact one"},
            {"source_ref": "mem:aaa1", "text": "fact one, mentioned again"},
        ],
        relations=[
            {"src": "e3", "dst": "e1", "rel": "works_at",
             "dst_name": "Ada Lovelace", "src_name": "Zebra Corp", "valid_at": True},
        ],
    )
    titles = {"mem:aaa1": "Fact One Note (aaaa1111)", "ent:e3": "Zebra Corp"}
    _, _, generated = render.entity_note_parts(ENTITY, profile, titles=titles)
    mentioned = _section(generated, "Mentioned in")
    assert mentioned.count("[[Fact One Note (aaaa1111)]]") == 1
    assert "[[Zebra Corp]]" in mentioned
    # sorted alphabetically
    assert mentioned.index("[[Fact One Note (aaaa1111)]]") < mentioned.index("[[Zebra Corp]]")


def test_mentioned_in_never_includes_an_unresolvable_reference():
    profile = _empty_profile(facts=[{"source_ref": "mem:gone0000", "text": "orphaned"}])
    _, _, generated = render.entity_note_parts(ENTITY, profile, titles={})
    mentioned = _section(generated, "Mentioned in")
    assert "[[" not in mentioned


def test_outbound_relation_alone_is_not_a_mention():
    # this entity's own page linking OUT to another entity is not "mentioned
    # in" that other entity's sense — only the reverse (someone else's page
    # linking IN to this one) counts.
    profile = _empty_profile(relations=[
        {"src": "e1", "dst": "e9", "rel": "works_at",
         "dst_name": "Cordera Labs", "src_name": "Ada Lovelace", "valid_at": True},
    ])
    titles = {"ent:e9": "Cordera Labs"}
    _, _, generated = render.entity_note_parts(ENTITY, profile, titles=titles)
    mentioned = _section(generated, "Mentioned in")
    assert "Cordera Labs" not in mentioned


# ---------------------------------------------------------------------------
# Timeline: its own section, short dates
# ---------------------------------------------------------------------------


def test_timeline_is_its_own_section_with_short_dates():
    profile = _empty_profile(timeline=[
        {"at": "2026-03-15T00:00:00Z", "kind": "mention", "text": "Ada joined Bluehaven",
         "source_ref": "mem:x"},
        {"at": "2026-03-01T00:00:00Z", "kind": "valid_from", "text": "works_at Bluehaven",
         "source_ref": "r1"},
    ])
    _, _, generated = render.entity_note_parts(ENTITY, profile)
    assert "## Timeline" in generated
    timeline = _section(generated, "Timeline")
    assert "2026-03-15: Ada joined Bluehaven" in timeline
    assert "2026-03: works_at Bluehaven" in timeline  # day is 01: a bare month


def test_history_windows_use_short_dates():
    profile = _empty_profile(history=[
        {"rel": "works_at", "dst_name": "Cordera Labs",
         "valid_from": "2019-01-01T00:00:00Z", "valid_until": "2026-03-01T00:00:00Z"},
    ])
    _, _, generated = render.entity_note_parts(ENTITY, profile)
    history = _section(generated, "History")
    assert "2019-01 → 2026-03" in history
