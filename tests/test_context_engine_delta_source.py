"""tests/test_context_engine_delta_source.py — a delta, retrievable as context.

`contracts.SOURCE_TYPES` reserved `delta` — "a Universal Delta Engine result" —
and reserved is not wired.  The last time this repository shipped a store with
an `as_candidates()` and no registration, `default_sources()` did not return
it, the compiler could not consult it and `/context` reported six declared
sources as unavailable.  Everything here pins the wiring that stops that
happening again, and the rules that make the wiring worth having:

* the source is in `candidates.registered_sources()` AND in
  `planner.SOURCE_SECTIONS` under the same key, and the two agree about which
  sections it fills — that disagreement is the whole of the old bug, so it is
  checked from both ends rather than from one;
* every section it declares is in `SECTION_KINDS`;
* a `delta:<id>` belonging to somebody else answers nothing, by search and by
  ref, because the owner comes off the request and never out of the reference;
* with `agent_delta_engine` OFF the source is still available and still
  answers with what was already stored: the switch is a budget for work, not
  an instruction to forget a conclusion recorded honestly;
* a `matched` delta with 20% semantic coverage is recovered saying BOTH (§1.4);
* a superseded delta is out of the search and reachable by ref, labelled as
  the history it is (§1.9.6);
* and a search candidate does not grow with the number of assertions — one
  delta with three hundred of them may not eat the budget of the section
  (§1.9.7, §20).

The store is real here, in a `tmp_path` database: this adapter exists to call
`delta_engine/persistence.py`, and doubling it out would leave the call
untested.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import pytest

from src.context_engine import candidates as C, planner, ranking
from src.context_engine.adapters import deltas as D
from src.context_engine.contracts import (
    SECTION_KINDS,
    ContextActor,
    ContextCandidate,
    ContextExecution,
    ContextPolicy,
    ContextRequest,
    ContextTask,
)
from src.delta_engine import persistence as P
from src.delta_engine.contracts import UniversalDelta

OWNER = "alice"
OTHER = "bob"
CREATED = "2026-09-06T11:00:00Z"


@pytest.fixture(autouse=True)
def delta_db(tmp_path):
    """One delta database per test, and a registry that inherits nothing.

    `forget_store_probe` matters as much as `use_path`: `available()` caches
    its answer per path, so a test that kept another test's probe would be
    asserting against a file that no longer exists.
    """
    P.use_path(str(tmp_path / "delta_engine.db"))
    D.forget_store_probe()
    C.reset_sources()
    try:
        yield
    finally:
        C.reset_sources()
        D.forget_store_probe()
        P.use_path(None)


# ── helpers ────────────────────────────────────────────────────────────────

def digest(seed: str) -> str:
    """A real sha256, because `RevisionRef` refuses anything else."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def revision(name: str) -> dict:
    return {"kind": "checkpoint", "ref": name, "hash": digest(name), "label": name}


def assertion(index: int = 1, **over) -> dict:
    payload = {"id": f"assertion_{index}",
               "path": f"src/auth_{index}.py#consume_state",
               "operation": "modified", "classification": "regression",
               "severity": "material", "confidence": "exact", "tier": "parser",
               "before": "one_time", "after": "reusable", "method": "python_ast",
               "detail": "the state token is no longer consumed"}
    payload.update(over)
    return payload


def delta(**over) -> UniversalDelta:
    payload = {"id": "delta_1", "owner": OWNER, "project_id": "p1",
               "domain": "code",
               "source": revision("before"), "target": revision("after"),
               "assessment": "partial",
               "intent_contract_id": "intent_1", "intent_fingerprint": "fp-1",
               "assertions": [assertion()],
               "coverage": {"source_readable": True, "target_readable": True,
                            "dimensions": {"structural": 1.0, "semantic": 0.2}},
               "extractor_versions": {"python_ast": "1.0"},
               "created_at": CREATED}
    payload.update(over)
    return UniversalDelta.parse(payload)


def stored(**over) -> UniversalDelta:
    """One delta, in the store, answered back."""
    built = delta(**over)
    P.store().save_delta(built)
    return built


def _request(*, owner=OWNER, project_id="p1", intent="code_change", phase="act",
             query="the oauth state check", refs=(), **policy) -> ContextRequest:
    return ContextRequest(
        actor=ContextActor(agent_id="worker-1", model="test-model"),
        execution=ContextExecution(owner=owner, project_id=project_id,
                                   workspace="/repo", session_id="s1"),
        task=ContextTask(intent=intent, phase=phase, query=query),
        policy=ContextPolicy(**policy),
        explicit_refs=tuple(refs),
    )


def _round(request, *, sections=("past_experiences",), limit=8, lanes=(), refs=()):
    return C.RetrievalRequest(request=request, query=request.task.query,
                              sections=tuple(sections), limit=limit,
                              lanes=tuple(lanes), explicit_refs=tuple(refs))


def _planned(request):
    """The round the compiler would build for this request."""
    plan = planner.plan(request, available=tuple(planner.known_sources()))
    return _round(request, sections=plan.sections, limit=plan.per_source_limit,
                  lanes=plan.lanes)


def _search(round_, source=None):
    return list(asyncio.run((source or D.DeltaSource()).search(round_)))


def _fetch(ref, round_, source=None):
    return asyncio.run((source or D.DeltaSource()).fetch(ref, round_))


def _gathered(round_, source=None):
    results = asyncio.run(C.gather([source or D.DeltaSource()], round_))
    assert len(results) == 1
    return results[0]


# ── it exists, and the planner plans for the thing that exists ─────────────

def test_the_delta_source_is_registered_and_the_planner_agrees_with_it():
    """The old bug in one assertion, checked from both ends.

    Six stores were built and registered nowhere, so `registered_sources()` and
    `planner.SOURCE_SECTIONS` disagreed and the disagreement was invisible: the
    planner planned for a source the compiler could not build.  Checking only
    one of the two would leave exactly that gap open again.
    """
    built = {source.source_id: source for source in C.default_sources()}

    assert "deltas" in built, "the delta source is declared and not built"
    assert "deltas" in C.registered_sources()
    assert "deltas" in planner.SOURCE_SECTIONS

    source = built["deltas"]
    assert source.sections == planner.SOURCE_SECTIONS["deltas"]
    assert source.sections == ("past_experiences",)
    assert source.handles == ("delta:",)

    # The sentence on the Overview tab: "N declared sources are not available".
    assert not set(planner.known_sources()) - set(built)


def test_every_section_the_delta_source_declares_is_a_real_section():
    """A section nobody priced is a section no budget can place: `budgets.
    section_order` and every profile are keyed on `SECTION_KINDS`."""
    for kind in D.DeltaSource().sections:
        assert kind in SECTION_KINDS, kind


def test_a_casual_chat_does_not_wake_the_delta_store():
    """The failure the planner exists to prevent.  A delta is worth reading on
    a coding or a verification turn; on "good morning" it is a thread and a
    database open nobody asked for."""
    plan = planner.plan(_request(intent="chat", query="buenos dias"),
                        available=tuple(planner.known_sources()))
    assert plan.intent == "chat"
    assert "deltas" not in plan.source_ids
    assert plan.skipped["deltas"], "deltas was skipped without a reason"

    coding = planner.plan(_request(), available=tuple(planner.known_sources()))
    assert "deltas" in coding.source_ids
    assert "past_experiences" in coding.sections


# ── isolation: the owner is the request's, never the reference's ───────────

def test_another_owners_delta_is_reachable_neither_by_search_nor_by_ref():
    """`DeltaStore` takes `owner` as a required keyword on every read, and this
    adapter passes the request's.  A `delta:<id>` that exists for somebody else
    answers the same None a `delta:<id>` that never existed answers — telling
    the two apart would confirm the id."""
    mine = stored()
    source = D.DeltaSource()

    theirs = _round(_request(owner=OTHER))
    assert _search(theirs, source) == []
    assert _fetch(f"delta:{mine.id}", theirs, source) is None
    assert asyncio.run(C.fetch_ref(f"delta:{mine.id}", theirs,
                                   sources=[source])) is None

    ours = _round(_request())
    assert [c.source_ref for c in _search(ours, source)] == [f"delta:{mine.id}"]
    assert _fetch(f"delta:{mine.id}", ours, source) is not None


# ── the switch is a budget for work, not an instruction to forget ──────────

def test_the_engine_being_switched_off_does_not_hide_a_stored_delta(monkeypatch):
    """`agent_delta_engine` is off by default and decides whether a comparison
    may RUN.  A delta already stored was concluded honestly before the switch
    moved; hiding it would make the switch mean "forget", which is not what
    `src/settings.py` says it is for.  The setting is forced off here — every
    setting is — so that a future edit that started reading one would fail."""
    import src.settings as settings

    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: False, raising=True)

    kept = stored()
    source = D.DeltaSource()

    assert source.available() is True
    found = _search(_round(_request()), source)
    assert [c.source_ref for c in found] == [f"delta:{kept.id}"]


def test_an_empty_store_is_available_and_answers_with_nothing():
    """Empty and broken are different facts, and the diagnostics line shows
    them differently: "one declared source is not available" has to mean
    something other than "you have not compared anything yet"."""
    result = _gathered(_round(_request()))

    assert result.candidates == ()
    assert result.degraded is False
    assert result.error == ""
    assert D.DeltaSource().available() is True


# ── §1.4: the verdict never travels without its reservations ──────────────

def test_a_matched_delta_with_low_coverage_is_recovered_saying_both():
    """"No usa deltas como memoria infalible."

    A `matched` whose semantic coverage was 0.2 is a conclusion with a
    reservation, and recovering it as "matched" alone is how a reservation
    becomes a fact three turns later.  The sentence comes from
    `verdict.explain`, whose last clause is `coverage.gaps()`; this asserts the
    adapter does not trim it off."""
    stored(assessment="matched",
           assertions=[assertion(1, classification="requested", severity="info")],
           coverage={"source_readable": True, "target_readable": True,
                     "dimensions": {"structural": 1.0, "semantic": 0.2}})

    body = _search(_round(_request()))[0].body

    assert "Assessment: matched" in body
    assert "Not checked" in body
    assert "semantic coverage was 0.2" in body


def test_the_summary_says_what_was_asked_what_was_collateral_and_what_regressed():
    """§20: "guarda resúmenes de delta y refs, no outputs completos" — and a
    summary that dropped any of the four clauses would be a different, shorter
    claim than the delta makes."""
    kept = stored(assertions=[
        assertion(1, classification="requested", severity="info"),
        assertion(2, classification="incidental", severity="minor"),
        assertion(3, classification="regression", severity="material"),
        assertion(4, classification="unknown", severity="info"),
    ], coverage={"source_readable": True, "target_readable": True,
                 "dimensions": {"structural": 1.0, "semantic": 0.4}})

    candidate = _search(_round(_request()))[0]
    body = candidate.body

    assert "1 requested change observed" in body        # what was asked for
    assert "1 change not requested" in body             # what was collateral
    assert "1 regression" in body                       # what regressed
    assert "1 observation could not be classified" in body   # what is unknown
    assert "semantic coverage was 0.4" in body          # what was not covered
    assert "Compared code:" in body                     # the domain
    assert "before (checkpoint:" in body and "after (checkpoint:" in body
    assert f"Open delta:{kept.id}" in body              # where the detail is
    assert candidate.source_ref == f"delta:{kept.id}"


def test_a_delta_candidate_is_one_the_contract_accepts():
    """`make_candidate` builds the dataclass directly, so nothing has run
    `parse` on the result: a 600-character title or a lane nobody declared
    would surface three layers later, where the error no longer names the
    source that produced it."""
    kept = stored()
    round_ = _round(_request())

    for candidate in (_search(round_)[0], _fetch(f"delta:{kept.id}", round_)):
        parsed = ContextCandidate.parse(candidate.to_dict())
        assert parsed.source_type == "delta"
        assert parsed.section == "past_experiences"
        assert parsed.source_ref == f"delta:{kept.id}"
        assert parsed.owner == OWNER


# ── §1.9.6: a quarantine does not rewrite history ─────────────────────────

def test_a_superseded_delta_is_out_of_the_search_and_still_open_by_ref():
    """Recomputing REPLACES, and the replaced row stays readable: a council
    decision or a proof may point at the delta that was live when it was
    taken, and answering None because a newer interpretation exists would
    rewrite what that decision was based on.  What it may NOT do is come back
    looking current."""
    old = stored()
    # Same source, target, intent and extractor versions: the same question,
    # answered again.  `save_delta` supersedes the incumbent.
    new = stored(id="delta_2", assessment="matched",
                 assertions=[assertion(1, classification="requested",
                                       severity="info")],
                 created_at="2026-09-06T12:00:00Z")

    round_ = _round(_request())
    assert [c.source_ref for c in _search(round_)] == [f"delta:{new.id}"]

    history = _fetch(f"delta:{old.id}", round_)
    assert history is not None, "a decision that points at it must still open it"
    assert history.title.startswith("SUPERSEDED ")
    assert "SUPERSEDED" in history.body
    assert f"delta:{new.id}" in history.body
    assert history.meta["live"] is False
    assert history.meta["superseded_by"] == new.id
    assert history.degraded is True
    assert history.authority == "agent_claim", (
        "history may not outrank the interpretation that replaced it")

    current = _fetch(f"delta:{new.id}", round_)
    assert current.meta["live"] is True
    assert current.degraded is False
    assert not current.title.startswith("SUPERSEDED")


# ── §1.9.7: summaries in the search, detail on demand ─────────────────────

def test_a_search_candidate_does_not_grow_with_the_number_of_assertions():
    """One candidate per delta, and a summary that is bounded.

    The rule is a ceiling (`SUMMARY_MAX_CHARS`) and not a lucky number: a
    hundredfold more assertions may move the counts inside the sentence, never
    the size of it, because `verdict.explain` names three paths per clause and
    then counts the rest.  A delta with three hundred assertions that grew its
    body with them would spend the whole `past_experiences` budget on itself.
    """
    small = stored(id="delta_small", source=revision("s-small"),
                   target=revision("t-small"), intent_fingerprint="fp-small",
                   assertions=[assertion(i) for i in range(1, 4)])
    big = stored(id="delta_big", source=revision("s-big"),
                 target=revision("t-big"), intent_fingerprint="fp-big",
                 assertions=[assertion(i) for i in range(1, 301)])

    round_ = _round(_request())
    by_ref = {c.source_ref: c for c in _search(round_)}
    assert set(by_ref) == {f"delta:{small.id}", f"delta:{big.id}"}

    small_body = by_ref[f"delta:{small.id}"].body
    big_body = by_ref[f"delta:{big.id}"].body

    assert "300 assertions" in big_body, "the count is stated, not the rows"
    assert len(big_body) <= D.SUMMARY_MAX_CHARS
    assert len(big_body) <= len(small_body) + 200, (
        f"a hundredfold more assertions grew the summary by "
        f"{len(big_body) - len(small_body)} characters")


def test_the_detail_opens_the_material_rows_and_says_what_it_left_out():
    """§12: a material assertion is never summarised away — and a list nobody
    can read has been summarised away just as effectively.  So `_fetch` opens
    `material_assertions()`, which the contract sorts strongest-first, keeps
    `DETAIL_ASSERTIONS` of them, and NAMES the count it dropped beside the ref
    that still holds all of them."""
    big = stored(assertions=[assertion(i) for i in range(1, 301)])
    round_ = _round(_request())

    summary = _search(round_)[0]
    detail = _fetch(f"delta:{big.id}", round_)

    rows = [line for line in detail.body.splitlines() if line.startswith("- ")]
    assert len(rows) == D.DETAIL_ASSERTIONS
    assert f"and {300 - D.DETAIL_ASSERTIONS} more material assertions" in detail.body
    assert len(detail.body) > len(summary.body), (
        "the detail is what the search deliberately did not carry")
    # Strongest first, and the values quoted rather than the file pasted.
    assert "one_time -> reusable" in detail.body
    assert detail.lanes == ("exact",)
    assert summary.lanes == ("temporal",)


# ── policy, and the clock ─────────────────────────────────────────────────

def test_an_incognito_turn_never_reads_a_delta():
    """`allow_project_sources` is enforced on the source, on the event loop,
    before the store is consulted — `ProjectLinksSource`' shape.  It is NOT in
    `ranking.PROJECT_SOURCE_TYPES`, because that list names ROW kinds and a
    `state` or an `image` delta carries no project content for the ranker to
    refuse; naming `delta` there would make it a second, wider policy."""
    kept = stored()
    source = D.DeltaSource()
    incognito = _round(_request(allow_project_sources=False))

    assert source._gate(incognito) == "policy.allow_project_sources is false"
    assert source._ref_gate(incognito) == "policy.allow_project_sources is false"
    assert _search(incognito, source) == []
    assert _fetch(f"delta:{kept.id}", incognito, source) is None

    assert "delta" not in ranking.PROJECT_SOURCE_TYPES
    assert "deltas" not in planner.PROJECT_SOURCE_IDS


def test_a_named_delta_is_reopened_on_a_round_that_was_not_looking_for_one():
    """`_ref_gate` is the narrower half of the gate.  A `delta:<id>` in
    `explicit_refs` comes from the runtime, never from a model, and refusing to
    reopen it because this round wanted `retrieved_memory` would break the one
    channel by which a caller names a specific thing."""
    kept = stored()
    chat = _round(_request(intent="chat", query="buenos dias"),
                  sections=("retrieved_memory", "recent_messages"))

    assert D.DeltaSource()._gate(chat) == "past_experiences was not requested"
    assert _search(chat) == []
    assert _fetch(f"delta:{kept.id}", chat) is not None


def test_a_delta_does_not_go_stale_like_a_state_projection():
    """A State Mirror projection is worthless by tomorrow.  A delta is the
    conclusion about two IMMUTABLE revisions and stays true while they exist;
    what decays is its bearing on today's work.  At the projection's half-life
    every comparison older than about two days would sit on the freshness
    floor, which does not read as "old" but as "never happened"."""
    assert (ranking.FRESHNESS_HALF_LIFE_DAYS["delta"]
            > ranking.FRESHNESS_HALF_LIFE_DAYS["state"])

    stored()
    request = _request()
    candidate = _search(_round(request))[0]
    a_week_later = datetime(2026, 9, 13, 11, 0, tzinfo=timezone.utc)

    assert ranking.validate(candidate, request, now=a_week_later).ok
    scored = ranking.score(candidate, request, now=a_week_later)
    assert scored.parts["freshness"] > 0.5, scored.parts
