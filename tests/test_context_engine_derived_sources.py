"""tests/test_context_engine_derived_sources.py — the six derived stores, connected.

Six stores were built under `src/context_engine/` with an `as_candidates()`
each and registered as a `ContextSource` nowhere, so `default_sources()` never
returned them, the compiler could never consult them, and `/context` reported
six declared sources as unavailable.  Everything here pins the wiring that
fixes that, and the properties that make the wiring worth having:

* the six are registered, and the `source_id`, `sections` and `handles` each
  declares are the ones `planner.SOURCE_SECTIONS` plans for — a planner that
  disagreed with `gather` would be a second policy nobody could find;
* a casual chat still wakes neither the code index nor the recipe book, which
  is the failure the planner exists to prevent and the one adding six sources
  could most easily have reintroduced;
* a block written now is retrievable now, in the section its type maps to;
* a `contradicted` experience arrives as the anti-pattern it is, never as
  advice;
* an EMPTY store is available and answers with zero candidates and
  `degraded=False` — empty and broken are different facts and the diagnostics
  screen shows them differently;
* every one of the six `source_ref` prefixes resolves through its own source,
  and an id nobody minted answers None instead of raising;
* and two owners never see each other's rows through any of them.

The stores are real here, in a `tmp_path` database: these adapters exist to
call them, and doubling them out would leave the calls untested.
"""

from __future__ import annotations

import asyncio

import pytest

from src.context_engine import blocks, candidates as C, capsules, code_index
from src.context_engine import experiences, multimodal_memory, planner, ranking
from src.context_engine import shared_memory, store
from src.context_engine.adapters import derived
from src.context_engine.contracts import (
    ContextActor,
    ContextExecution,
    ContextPolicy,
    ContextRequest,
    ContextTask,
)

#: The table this whole module is about: id -> (sections, handles).
DECLARED = {
    "blocks": (("project_rules", "active_goal", "current_state", "decisions",
                "tool_guidance"), ("block:",)),
    "capsules": (("current_state",), ("capsule:",)),
    "experiences": (("past_experiences",), ("exp:", "experience:")),
    "code_index": (("code_map",), ("symbol:",)),
    "shared_memory": (("peer_findings",), ("finding:",)),
    "multimodal": (("multimodal_recipes",), ("recipe:",)),
}


@pytest.fixture(autouse=True)
def ce_store(tmp_path):
    """One database per test, and a registry that inherits nothing.

    `reset_sources` matters as much as `use_path`: the registry is process-wide
    on purpose, so a test that left the six built against another test's
    database would be asserting against a file that no longer exists.
    """
    store.use_path(str(tmp_path / "ce.db"))
    derived.forget_store_probe()
    C.reset_sources()
    try:
        yield
    finally:
        C.reset_sources()
        derived.forget_store_probe()
        store.use_path(None)


# ── helpers ────────────────────────────────────────────────────────────────

def _request(*, owner="alice", project_id="p1", workspace="", session_id="s1",
             run_id="", council_id="", branch_id="", intent="code_change",
             phase="act", query="the oauth state check", refs=(), **policy):
    return ContextRequest(
        actor=ContextActor(agent_id="worker-1", model="test-model"),
        execution=ContextExecution(owner=owner, project_id=project_id,
                                   workspace=workspace, session_id=session_id,
                                   run_id=run_id, council_id=council_id,
                                   branch_id=branch_id),
        task=ContextTask(intent=intent, phase=phase, query=query),
        policy=ContextPolicy(**policy),
        explicit_refs=tuple(refs),
    )


def _round(request, *, sections=(), limit=8, lanes=(), refs=()):
    return C.RetrievalRequest(request=request, query=request.task.query,
                              sections=tuple(sections), limit=limit,
                              lanes=tuple(lanes), explicit_refs=tuple(refs))


def _planned(request):
    """The round the compiler would build for this request."""
    plan = planner.plan(request, available=tuple(planner.known_sources()))
    return _round(request, sections=plan.sections, limit=plan.per_source_limit,
                  lanes=plan.lanes)


def _one(source, round_):
    """`gather` one source and hand back its single `SourceResult`."""
    results = asyncio.run(C.gather([source], round_))
    assert len(results) == 1
    return results[0]


def _experience(**over):
    fields = {
        "owner": "alice",
        "project_id": "p1",
        "intent": "code_change",
        "technologies": ["python"],
        "concepts": ["oauth", "state"],
        "problem": "the oauth state was consumed before the token exchange",
        "strategy": ["move the state check after the exchange"],
        "touched_symbols": ["src/auth/github.py"],
        "result": "success",
        "verdict": "proved",
        "verification_refs": ["changeset:chg_1"],
        "lesson": "consume the state after the exchange",
        "source_run": "run_1",
    }
    fields.update(over)
    return experiences.admit(fields)


def _recipe(**over):
    fields = {
        "owner": "alice",
        "project_id": "p1",
        "media_type": "image",
        "request": "a product shot of the blue mug",
        "model": "flux.1-dev",
        "model_version": "2024-08",
        "prompt": "blue ceramic mug, linen backdrop",
        "seed": "7",
    }
    fields.update(over)
    return multimodal_memory.record(**fields)


def _finding(**over):
    fields = {
        "scope": "council-7",
        "owner": "alice",
        "project_id": "p1",
        "author": "worker-security",
        "topic": "oauth-state",
        "kind": "fact",
        "claim": "the state is consumed after the exchange",
        "evidence_refs": ["file:src/auth/oauth.py#L120"],
    }
    fields.update(over)
    return shared_memory.post(**fields)


def _workspace(tmp_path, name="repo", body="def greet(who):\n    return who\n"):
    root = tmp_path / name
    root.mkdir()
    (root / "app.py").write_text(body, encoding="utf-8")
    return str(root)


# ── they exist, and they are the ones the planner plans for ────────────────

def test_the_six_derived_stores_are_registered_sources():
    built = {source.source_id: source for source in C.default_sources()}

    for source_id, (sections, handles) in DECLARED.items():
        assert source_id in built, f"{source_id} is declared and not built"
        assert built[source_id].sections == sections
        assert built[source_id].handles == handles

    # The sentence on the Overview tab: "N declared sources are not available".
    # `declared` is `planner.known_sources()` and `built` is the registry, so
    # this assertion is the diagnostic, not a paraphrase of it.
    assert not set(planner.known_sources()) - set(built)


def test_the_planner_and_the_adapters_agree_on_what_each_source_fills():
    """`SOURCE_SECTIONS` is what decides whether a source is woken; a section
    listed there and not on the adapter (or the other way round) is a policy
    that only one of the two halves applies."""
    for source in C.default_sources():
        declared = planner.SOURCE_SECTIONS.get(source.source_id)
        if declared is None:
            continue
        assert tuple(source.sections) == declared, source.source_id


# ── the failure the planner exists to prevent, still prevented ─────────────

def test_a_casual_chat_wakes_neither_the_code_index_nor_the_recipe_book():
    request = _request(intent="chat", query="buenos dias",
                       workspace="/repo", session_id="s1")
    plan = planner.plan(request, available=tuple(planner.known_sources()))

    assert plan.intent == "chat"
    for expensive in ("code_index", "multimodal"):
        assert expensive not in plan.source_ids
        assert plan.skipped[expensive], f"{expensive} was skipped without a reason"
    assert "code_map" not in plan.sections
    assert "multimodal_recipes" not in plan.sections


def test_the_recipe_book_is_consulted_for_a_media_turn_and_not_otherwise():
    _recipe()
    media = _request(intent="image", query="a product shot of the blue mug")
    chat = _request(intent="chat", query="a product shot of the blue mug")

    assert _one(derived.RecipeSource(), _planned(media)).candidates
    assert _one(derived.RecipeSource(), _planned(chat)).candidates == ()


# ── a block written now is retrievable now ─────────────────────────────────

def test_a_block_arrives_as_a_candidate_in_the_section_its_type_maps_to():
    block = blocks.create_block(
        type="project_rules", scope="project", owner="alice", project_id="p1",
        title="House rules", content="never touch the migrations folder",
        priority=90, always_loaded=True)

    result = _one(derived.BlockSource(), _planned(_request()))

    assert result.error == "" and result.degraded is False
    assert [c.source_ref for c in result.candidates] == [f"block:{block.id}"]
    only = result.candidates[0]
    assert only.section == "project_rules" == blocks.SECTION_BY_TYPE["project_rules"]
    assert only.lanes == ("mandatory",), "an always-loaded block is placed, not ranked"
    assert only.body == "never touch the migrations folder"


def test_a_block_whose_section_this_round_did_not_ask_for_is_not_offered():
    """`known_failures` maps to `past_experiences`, which a chat never wants."""
    blocks.create_block(type="known_failures", scope="project", owner="alice",
                        project_id="p1", title="Retry storms",
                        content="do not retry a 429 without a backoff",
                        always_loaded=True)

    chat = _one(derived.BlockSource(),
                _planned(_request(intent="chat", query="buenos dias")))
    verifying = _one(derived.BlockSource(),
                     _planned(_request(intent="review", query="review the retry code",
                                       workspace="/repo")))

    assert chat.candidates == ()
    assert [c.section for c in verifying.candidates] == ["past_experiences"]


# ── a contradicted experience is a warning, never a recipe ─────────────────

def test_a_contradicted_experience_arrives_as_an_antipattern_not_a_pattern():
    good = _experience(problem="the oauth state was consumed too early")
    bad = _experience(problem="retrying the oauth exchange on a 429",
                      result="failure", verdict="contradicted",
                      verification_refs=["changeset:chg_2"],
                      failure_modes=["it made the rate limit worse"],
                      lesson="do not retry the exchange")

    result = _one(derived.ExperienceSource(),
                  _planned(_request(query="oauth exchange retry state")))
    by_ref = {c.source_ref: c for c in result.candidates}

    assert f"experience:{bad.id}" in by_ref
    warning = by_ref[f"experience:{bad.id}"]
    assert warning.meta["role"] == "anti_pattern"
    assert warning.body.startswith("ANTI-PATTERN")
    # §14.2: a warning drawn from a failed attempt may never outrank an
    # approach the evidence supports.
    assert warning.authority == "agent_claim"
    if f"experience:{good.id}" in by_ref:
        pattern = by_ref[f"experience:{good.id}"]
        assert pattern.meta["role"] == "pattern"
        assert pattern.authority == "validated_experience"
        assert pattern.authority_rank() > warning.authority_rank()


def test_an_unproved_experience_is_history_and_is_never_recommended():
    kept = _experience(problem="a hunch about the token cache",
                       result="abandoned", verdict="unproved",
                       verification_refs=[])

    assert experiences.get(kept.id) is not None
    result = _one(derived.ExperienceSource(),
                  _planned(_request(query="token cache hunch")))
    assert [c.source_ref for c in result.candidates] == []


# ── empty is not degraded, and it is not unavailable either ────────────────

def test_an_empty_store_is_available_and_answers_with_no_candidates(tmp_path):
    """The distinction the Overview tab is built on.  A store with nothing in
    it is reachable and answers "nothing"; a store that is not there at all is
    what "N declared sources are not available" is supposed to mean."""
    request = _request(intent="image", workspace=_workspace(tmp_path),
                       run_id="run-1", council_id="council-7",
                       query="a product shot of the blue mug")
    # No section list: every source runs its own gate rather than being
    # filtered out by the round, so all six really do reach their store.
    round_ = _round(request)

    for factory in derived.DERIVED_SOURCES:
        source = factory()
        assert source.available() is True, source.source_id
        result = _one(source, round_)
        assert result.candidates == (), source.source_id
        assert result.degraded is False, source.source_id
        assert result.error == "", source.source_id
        assert result.ok() is True, source.source_id


# ── a source_ref is only provenance if something can reopen it ─────────────

def test_every_prefix_resolves_through_the_source_that_minted_it(tmp_path):
    workspace = _workspace(tmp_path)
    code_index.refresh(workspace, project_id="p1")

    block = blocks.create_block(type="project_rules", scope="project",
                                owner="alice", project_id="p1", title="Rules",
                                content="never touch the migrations folder")
    capsule = capsules.ensure("run-1", owner="alice", project_id="p1",
                              objective="finish the oauth fix")
    experience = _experience()
    finding = _finding()
    recipe = _recipe()
    symbol_ref = code_index.as_candidates(
        code_index.search("greet", workspace=workspace, project_id="p1"),
        workspace=workspace)[0].source_ref

    request = _request(workspace=workspace, run_id="run-1", council_id="council-7")
    round_ = _round(request)

    for ref in (f"block:{block.id}", f"capsule:{capsule.scope_id}",
                f"experience:{experience.id}", f"exp:{experience.id}",
                symbol_ref, f"finding:{finding.id}", f"recipe:{recipe.id}"):
        found = asyncio.run(C.fetch_ref(ref, round_))
        assert found is not None, ref
        assert found.source_ref == ref or ref.endswith(found.source_ref.split(":", 1)[1])


def test_a_reference_nobody_minted_answers_none_instead_of_raising(tmp_path):
    request = _request(workspace=_workspace(tmp_path), run_id="run-1",
                       council_id="council-7")
    round_ = _round(request)

    for ref in ("block:nope", "capsule:nope", "exp:nope", "experience:nope",
                "symbol:src/missing.py#L1-L2", "finding:nope", "recipe:nope",
                "block:", "symbol:"):
        assert asyncio.run(C.fetch_ref(ref, round_)) is None, ref


# ── isolation, applied before the query ────────────────────────────────────

def test_two_owners_never_see_each_others_rows_through_any_of_the_six(tmp_path):
    """Six stores, six ways to leak.  The code index is the one isolated by
    workspace rather than by owner — it holds no owner column, because a
    checkout is the thing two people would share — so it is given two
    workspaces here, which is the isolation it actually offers."""
    alice_ws = _workspace(tmp_path, "alice-repo")
    bob_ws = _workspace(tmp_path, "bob-repo", "def farewell(who):\n    return who\n")
    code_index.refresh(alice_ws, project_id="p1")
    code_index.refresh(bob_ws, project_id="p1")

    blocks.create_block(type="project_rules", scope="project", owner="alice",
                        project_id="p1", title="Alice rules",
                        content="never touch the migrations folder",
                        always_loaded=True)
    capsules.ensure("run-1", owner="alice", project_id="p1", objective="alice work")
    _experience()
    _finding()
    _recipe()

    bob = _request(owner="bob", intent="image", workspace=bob_ws, run_id="run-1",
                   council_id="council-7",
                   query="greet farewell mug oauth state migrations")
    round_ = _round(bob)

    for factory in derived.DERIVED_SOURCES:
        source = factory()
        result = _one(source, round_)
        for candidate in result.candidates:
            assert candidate.owner in ("", "bob"), (source.source_id, candidate.source_ref)
            assert "alice" not in candidate.body, source.source_id
        if source.source_id == "code_index":
            # Bob's workspace has `farewell`, Alice's has `greet`.
            assert all("greet" not in c.title for c in result.candidates)
        else:
            assert result.candidates == (), source.source_id


def test_a_fetch_by_id_is_refused_for_another_owners_row(tmp_path):
    """`blocks.get_block`, `experiences.get`, `shared_memory.get` and
    `multimodal_memory.get` all read by primary key and take no owner: the
    isolation a search gets from `scope_clause` has to be applied by the
    adapter, and this is the test that says so."""
    block = blocks.create_block(type="project_rules", scope="project",
                                owner="alice", project_id="p1", title="Rules",
                                content="never touch the migrations folder")
    capsules.ensure("run-1", owner="alice", project_id="p1", objective="alice work")
    experience = _experience()
    finding = _finding()
    recipe = _recipe()

    bob = _round(_request(owner="bob", run_id="run-1", council_id="council-7"))
    for ref in (f"block:{block.id}", "capsule:run-1", f"experience:{experience.id}",
                f"finding:{finding.id}", f"recipe:{recipe.id}"):
        assert asyncio.run(C.fetch_ref(ref, bob)) is None, ref


# ── policy is applied before the store is consulted ────────────────────────

def test_the_project_flag_gates_exactly_what_the_ranker_would_refuse():
    """A flag that blocks more than it names turns incognito into "no context
    at all", and `ranking.PROJECT_SOURCE_TYPES` is where this repository
    already said what it names: `project_memory`, `file`, `symbol`,
    `instruction`, `artifact`.  Every `code_index` row is a `symbol`, so
    consulting it under the flag buys a thread and a guaranteed omission; a
    block, an experience and a recipe are not on that list, and an incognito
    turn keeps the standing rule that says which folder it may not touch."""
    assert planner.PROJECT_SOURCE_IDS == ("project_memory", "files", "code_index")
    assert "symbol" in ranking.PROJECT_SOURCE_TYPES
    for kind in ("block", "experience", "recipe", "capsule", "finding"):
        assert kind not in ranking.PROJECT_SOURCE_TYPES

    incognito = _round(_request(intent="image", workspace="/repo",
                                allow_project_sources=False))
    assert derived.CodeIndexSource()._gate(incognito) \
        == "policy.allow_project_sources is false"
    for factory in (derived.BlockSource, derived.ExperienceSource,
                    derived.RecipeSource):
        assert factory()._gate(incognito) == "", factory.source_id


def test_a_scoped_store_is_not_consulted_without_a_scope_to_look_under():
    """A capsule and a blackboard are keyed by session, run, council or branch.
    `SCOPED_SOURCE_IDS` is a separate list from `SESSION_SOURCE_IDS` because a
    delegated worker has a `run_id` and no session, and skipping its capsule
    would cost it the one thing it can resume from."""
    scopeless = _round(_request(session_id="", run_id="", council_id=""))
    for factory in (derived.CapsuleSource, derived.FindingSource):
        assert factory()._gate(scopeless), factory.source_id

    with_run = _round(_request(session_id="", run_id="run-1"))
    assert derived.CapsuleSource()._gate(with_run) == ""

    plan = planner.plan(_request(session_id="", run_id="run-1"),
                        available=tuple(planner.known_sources()))
    assert "capsules" in plan.source_ids
    assert "sessions" not in plan.source_ids


def test_the_code_index_is_not_consulted_without_a_workspace_or_a_query():
    assert derived.CodeIndexSource()._gate(_round(_request(workspace=""))) \
        == "no workspace on the request"
    assert derived.CodeIndexSource()._gate(
        _round(_request(workspace="/repo", query="  "))) \
        == "no query to look a symbol up by"
    assert derived.CodeIndexSource()._gate(
        _round(_request(workspace="/repo", query="serve_index"))) == ""
