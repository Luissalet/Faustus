"""Turn-reference resolution: what "this document" means.

The test this file exists for is
``test_two_documents_created_in_one_operation_ask_instead_of_guessing``. Every
other rule here has a defensible fallback; that one does not. Picking the more
recent of two documents created by the same operation attaches the wrong half
of a pair and gives the user nothing to notice it by.
"""

import pytest

from src.project_context.references import (
    TurnReference, TurnReferenceRegistry, note_created, registry,
)

OWNER = "luis"
OTHER = "mallory"


class Clock:
    """A hand-cranked clock, so ordering and TTL are decided by the test."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def tick(self, seconds=1.0):
        self.now += float(seconds)
        return self.now


def make_registry(**kwargs):
    """A registry with no ambient active pointer unless a test asks for one."""
    kwargs.setdefault("active_provider", lambda session_id, owner: ("", ""))
    return TurnReferenceRegistry(**kwargs)


def ref(ref_id, *, kind="document", label="", turn="t1", session="s1", owner=OWNER,
        relation="created", at=0.0, tool="create_document"):
    return TurnReference(kind=kind, ref_id=ref_id, label=label, source_tool=tool,
                         turn_id=turn, session_id=session, owner=owner,
                         relation=relation, created_at=at)


# ── recording and isolation ────────────────────────────────────────────────

def test_notes_are_scoped_to_owner_and_session():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d1", session="s1", owner=OWNER, at=clock.tick()))
    reg.note(ref("d2", session="s2", owner=OWNER, at=clock.tick()))
    reg.note(ref("d3", session="s1", owner=OTHER, at=clock.tick()))

    assert [r.ref_id for r in reg.for_turn("s1", owner=OWNER)] == ["d1"]
    assert [r.ref_id for r in reg.for_turn("s2", owner=OWNER)] == ["d2"]
    assert [r.ref_id for r in reg.for_turn("s1", owner=OTHER)] == ["d3"]


def test_a_blank_owner_is_its_own_scope_not_a_wildcard():
    """The bug this subsystem exists to avoid, asserted directly."""
    reg = make_registry()
    reg.note(ref("secret", session="s1", owner=OWNER, at=1.0))
    assert reg.for_turn("s1", owner="") == []


def test_note_stamps_a_missing_timestamp_and_never_raises():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d1", at=0.0))
    assert reg.for_turn("s1", owner=OWNER)[0].created_at == clock.now
    reg.note(ref(""))            # no id
    reg.note("not a reference")  # not even the right type
    assert len(reg.for_turn("s1", owner=OWNER)) == 1


def test_entries_expire_and_are_capped():
    clock = Clock()
    reg = make_registry(clock=clock, ttl_seconds=10.0, max_entries=3)
    reg.note(ref("old", at=clock.tick()))
    clock.tick(60)
    reg.note(ref("fresh", at=clock.now))
    assert [r.ref_id for r in reg.for_turn("s1", owner=OWNER)] == ["fresh"]

    for i in range(5):
        reg.note(ref(f"n{i}", at=clock.tick()))
    kept = [r.ref_id for r in reg.for_turn("s1", owner=OWNER)]
    assert len(kept) == 3
    assert kept == ["n2", "n3", "n4"]


def test_clear_turn_and_clear_session():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("a", turn="t1", at=clock.tick()))
    reg.note(ref("b", turn="t2", at=clock.tick()))
    assert reg.clear_turn("s1", "t1") == 1
    assert [r.ref_id for r in reg.for_turn("s1", owner=OWNER)] == ["b"]
    assert reg.clear_session("s1") == 1
    assert reg.for_turn("s1", owner=OWNER) == []


def test_for_turn_filters_by_turn_id():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("a", turn="t1", at=clock.tick()))
    reg.note(ref("b", turn="t2", at=clock.tick()))
    assert [r.ref_id for r in reg.for_turn("s1", turn_id="t2", owner=OWNER)] == ["b"]


# ── the priority order (plan §10) ──────────────────────────────────────────

def test_1_an_explicit_id_wins_even_when_unrecorded():
    clock = Clock()
    reg = make_registry(clock=clock, active_provider=lambda s, o: ("active", "document"))
    reg.note(ref("recorded", at=clock.tick()))
    chosen, candidates = reg.resolve({"id": "typed-by-the-user", "kind": "document"},
                                     session_id="s1", owner=OWNER)
    assert chosen is not None
    assert chosen.ref_id == "typed-by-the-user"
    assert chosen.relation == "mentioned"
    assert candidates == []


def test_2_the_active_entity_comes_next():
    clock = Clock()
    reg = make_registry(clock=clock, active_provider=lambda s, o: ("d_active", "document"))
    reg.note(ref("d_active", label="Active doc", at=clock.tick()))
    reg.note(ref("d_other", label="Other doc", at=clock.tick()))
    chosen, candidates = reg.resolve({"kind": "document"}, session_id="s1", owner=OWNER)
    assert chosen.ref_id == "d_active"
    assert candidates == []


def test_an_explicit_empty_active_id_disables_the_ambient_pointer():
    clock = Clock()
    reg = make_registry(clock=clock, active_provider=lambda s, o: ("d_active", "document"))
    reg.note(ref("d_turn", turn="t1", at=clock.tick()))
    chosen, _ = reg.resolve({"kind": "document", "active_id": "", "turn_id": "t1"},
                            session_id="s1", owner=OWNER)
    assert chosen.ref_id == "d_turn"


def test_3_a_single_result_created_this_turn_resolves():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("older", turn="t0", at=clock.tick()))
    reg.note(ref("this_turn", turn="t1", at=clock.tick()))
    chosen, candidates = reg.resolve({"kind": "document", "turn_id": "t1"},
                                     session_id="s1", owner=OWNER)
    assert chosen.ref_id == "this_turn"
    assert candidates == []


def test_two_documents_created_in_one_operation_ask_instead_of_guessing():
    """MANDATORY. Two equally plausible candidates is a question, not a race
    won by whichever was written last."""
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d_first", label="Chapter one", turn="t1", at=clock.tick()))
    reg.note(ref("d_second", label="Chapter two", turn="t1", at=clock.tick()))

    chosen, candidates = reg.resolve({"kind": "document", "turn_id": "t1"},
                                     session_id="s1", owner=OWNER)
    assert chosen is None
    assert [c.ref_id for c in candidates] == ["d_first", "d_second"]

    # Without a turn_id the same pair must still be a question, not a guess.
    chosen, candidates = reg.resolve({"kind": "document"}, session_id="s1", owner=OWNER)
    assert chosen is None
    assert len(candidates) == 2


def test_a_title_breaks_the_tie_that_recency_must_not():
    """An ambiguous step falls through to a *more discriminating* signal. What
    is forbidden is choosing by recency, not answering a question the user
    actually made answerable by naming the document."""
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d_first", label="Chapter one", turn="t1", at=clock.tick()))
    reg.note(ref("d_second", label="Chapter two", turn="t1", at=clock.tick()))
    chosen, candidates = reg.resolve(
        {"kind": "document", "turn_id": "t1", "label": "Chapter two"},
        session_id="s1", owner=OWNER)
    assert chosen.ref_id == "d_second"
    assert candidates == []


def test_the_question_is_asked_about_the_narrowest_ambiguous_set():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("old_a", turn="t0", at=clock.tick()))
    reg.note(ref("old_b", turn="t0", at=clock.tick()))
    reg.note(ref("new_a", turn="t1", at=clock.tick()))
    reg.note(ref("new_b", turn="t1", at=clock.tick()))
    chosen, candidates = reg.resolve({"kind": "document", "turn_id": "t1"},
                                     session_id="s1", owner=OWNER)
    assert chosen is None
    assert [c.ref_id for c in candidates] == ["new_a", "new_b"]


def test_4_the_previous_turn_answers_when_this_one_produced_nothing():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("older", turn="t0", at=clock.tick()))
    reg.note(ref("previous", turn="t1", at=clock.tick()))
    chosen, candidates = reg.resolve({"kind": "document", "turn_id": "t2"},
                                     session_id="s1", owner=OWNER)
    assert chosen.ref_id == "previous"
    assert candidates == []


def test_5_a_unique_title_match_resolves_and_a_shared_one_does_not():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d1", label="Voice architecture", turn="t1", at=clock.tick()))
    reg.note(ref("d2", label="Voice roadmap", turn="t1", at=clock.tick()))

    chosen, _ = reg.resolve({"kind": "document", "turn_id": "t9", "label": "architecture"},
                            session_id="s1", owner=OWNER)
    assert chosen.ref_id == "d1"

    chosen, candidates = reg.resolve({"kind": "document", "turn_id": "t9", "label": "Voice"},
                                     session_id="s1", owner=OWNER)
    assert chosen is None
    assert len(candidates) == 2


def test_an_exact_title_beats_a_substring():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d1", label="Voice", turn="t1", at=clock.tick()))
    reg.note(ref("d2", label="Voice architecture", turn="t1", at=clock.tick()))
    chosen, _ = reg.resolve({"kind": "document", "turn_id": "t9", "label": "Voice"},
                            session_id="s1", owner=OWNER)
    assert chosen.ref_id == "d1"


def test_6_nothing_plausible_returns_the_candidates_to_ask_about():
    clock = Clock()
    reg = make_registry(clock=clock)
    chosen, candidates = reg.resolve({"kind": "document"}, session_id="s1", owner=OWNER)
    assert chosen is None
    assert candidates == []


def test_the_kind_filter_keeps_an_image_out_of_a_document_question():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("img", kind="gallery_image", turn="t1", at=clock.tick()))
    reg.note(ref("doc", kind="document", turn="t1", at=clock.tick()))
    chosen, _ = reg.resolve({"kind": "document", "turn_id": "t1"},
                            session_id="s1", owner=OWNER)
    assert chosen.ref_id == "doc"


def test_resolve_ignores_unknown_hint_keys_and_never_raises():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d1", turn="t1", at=clock.tick()))
    chosen, _ = reg.resolve({"kind": "document", "turn_id": "t1", "nonsense": object()},
                            session_id="s1", owner=OWNER)
    assert chosen.ref_id == "d1"
    # No hint at all is not an error: one candidate in the session resolves it
    # (plan §22, "si hay un único documento activo, 'este documento' lo resuelve").
    assert reg.resolve(None, session_id="s1", owner=OWNER)[0].ref_id == "d1"


def test_another_owner_cannot_resolve_this_owners_reference():
    clock = Clock()
    reg = make_registry(clock=clock)
    reg.note(ref("d1", label="Private", turn="t1", at=clock.tick()))
    chosen, candidates = reg.resolve({"kind": "document", "turn_id": "t1"},
                                     session_id="s1", owner=OTHER)
    assert chosen is None
    assert candidates == []


# ── the process singleton ──────────────────────────────────────────────────

def test_note_created_records_through_the_singleton():
    reg = registry()
    assert reg is registry()
    try:
        note_created("document", "d_singleton", label="Made here", tool="create_document",
                     session_id="s_singleton", turn_id="t1", owner=OWNER)
        entries = reg.for_turn("s_singleton", owner=OWNER)
        assert [e.ref_id for e in entries] == ["d_singleton"]
        assert entries[0].relation == "created"
        assert entries[0].created_at > 0
    finally:
        reg.clear_session("s_singleton")


def test_the_default_active_provider_never_raises(monkeypatch):
    """The one coupling to the legacy process-global must degrade, not throw.

    The stub is installed in ``sys.modules`` under every name the late import
    inside ``_default_active`` could resolve to, instead of patching the
    attribute on one module object. That is not pedantry: this test passed on
    its own and failed inside the full suite, because a neighbour running in
    the same xdist worker had already imported the tool runtime under a second
    module path, and ``references`` then bound the copy nobody had patched.
    A test whose verdict depends on which file ran before it is not a test.
    """
    import sys
    import types

    import src.project_context.references as refs

    def explode():
        raise RuntimeError("no document tools here")

    stub = types.ModuleType("document_tools_stub")
    stub.get_active_document = explode
    for name in ("src.agent_tools.document_tools", "agent_tools.document_tools"):
        monkeypatch.setitem(sys.modules, name, stub)
    assert refs._default_active("s1", OWNER) == ("", "")


def test_a_raising_active_provider_does_not_break_resolution():
    clock = Clock()

    def explode(session_id, owner):
        raise RuntimeError("boom")

    reg = TurnReferenceRegistry(clock=clock, active_provider=explode)
    reg.note(ref("d1", turn="t1", at=clock.tick()))
    chosen, _ = reg.resolve({"kind": "document", "turn_id": "t1"},
                            session_id="s1", owner=OWNER)
    assert chosen.ref_id == "d1"
