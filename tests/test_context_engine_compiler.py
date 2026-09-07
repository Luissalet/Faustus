"""The retrieval planner, the compiler and the background pass
(src/context_engine/planner.py, compiler.py, maintenance.py).

These are the tests for the five promises the compiler is built on, each of
which is a specific, expensive failure:

* a casual "good morning" must not wake the document store or the code index —
  the failure the plan opens with, and the one that costs four hundred
  milliseconds of a turn that used none of it;
* mandatory context does not compete: a safety instruction is never trimmed to
  make room for a semantically similar memory, and when it truly does not fit
  the packet says so by name instead of shipping quietly smaller;
* the packet never exceeds `window.input_budget`, even at an absurd budget with
  twenty fat candidates, because a turn that overflows dies at the provider
  *after* the tools have run;
* `compile()` never raises — a source that throws, an estimator that explodes
  and a broken cache all degrade into a valid packet with a warning;
* the same candidates, budget and clock produce the same packet, because
  Branching Futures proves two branches started from one context by comparing
  exactly that.

Everything is injected: the sources, the estimator, the cache and the clock.
Nothing here reaches the network, a model or the real settings file.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.context_engine import cache, compiler, maintenance, planner, store
from src.context_engine.cache import WorkingSet
from src.context_engine.compiler import ContextCompiler
from src.context_engine.contracts import (
    MANDATORY_SECTIONS,
    ContextActor,
    ContextCandidate,
    ContextExecution,
    ContextPacket,
    ContextPolicy,
    ContextReceipt,
    ContextRequest,
    ContextTask,
)

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
OBSERVED = "2026-08-31T12:00:00Z"

#: A body with no whitespace at all: there is no boundary to cut on, so no
#: honest excerpt of it exists at any budget and `fit()` must fall back to a
#: reference.  This is what a base64 artifact or a minified bundle looks like.
BLOB = "e30" + "Zm9vYmFyYmF6" * 120


@pytest.fixture()
def ce_db(tmp_path):
    """The context engine's own SQLite file, per test."""
    store.use_path(str(tmp_path / "ce.db"))
    cache.reset_working_set()
    compiler.reset_compiler()
    try:
        yield tmp_path
    finally:
        store.use_path(None)
        cache.reset_working_set()
        compiler.reset_compiler()


# ── builders ───────────────────────────────────────────────────────────────

def _request(**over):
    fields = dict(
        request_id="ctxreq_test",
        actor=ContextActor(agent_id="builder", role="engineer", model="test-model"),
        execution=ContextExecution(owner="luis", session_id="s1", project_id="p1",
                                   workspace="/repo", turn_id="t1"),
        task=ContextTask(intent="", phase="act",
                         query="fix the failing test in src/app.py"),
        policy=ContextPolicy(),
        consumer="agent",
        created_at=NOW.isoformat().replace("+00:00", "Z"),
    )
    fields.update(over)
    return ContextRequest(**fields)


def _chat_request(**over):
    fields = dict(
        execution=ContextExecution(owner="luis", session_id="s1"),
        task=ContextTask(query="buenos dias, que tal todo?"),
    )
    fields.update(over)
    return _request(**fields)


def _candidate(ref, body, *, section="retrieved_documents",
               source_type="document", **over):
    fields = dict(
        candidate_id=f"cand::{ref}", source_type=source_type, source_ref=ref,
        title=ref, body=body, section=section, lanes=("lexical",),
        trust_class="observed", authority="observed_state", observed_at=OBSERVED,
        source_revision="rev-a",
    )
    fields.update(over)
    return ContextCandidate(**fields)


def _prose(seed, count):
    """Boundary-rich text whose vocabulary is unique to `seed`.

    Every token carries the seed, so two bodies built from different seeds
    share no words at all.  That matters: `ranking.dedupe` drops candidates
    with near-identical wording, and a budget test whose candidates were
    removed as duplicates would pass while proving nothing."""
    return "\n".join(
        f"{seed}line{n} {seed}alpha{n} {seed}beta{n} {seed}gamma{n} {seed}delta{n}"
        for n in range(count))


class FakeSource:
    """A source with a name, a section list and a scripted answer."""

    def __init__(self, source_id, sections, candidates=(), *, raises=False,
                 unavailable=False):
        self.source_id = source_id
        self.sections = tuple(sections)
        self.handles = ("doc:", "file:", "mem:")
        self._candidates = list(candidates)
        self._raises = raises
        self._unavailable = unavailable
        self.searches = 0
        self.fetches = 0

    def available(self):
        return not self._unavailable

    async def search(self, req):
        self.searches += 1
        if self._raises:
            raise RuntimeError("the index.json was half written")
        return list(self._candidates)

    async def fetch(self, source_ref, req):
        self.fetches += 1
        for candidate in self._candidates:
            if candidate.source_ref == source_ref:
                return candidate
        return _candidate(source_ref, "fetched on demand")


def _compiler(sources, **kw):
    kw.setdefault("cache", WorkingSet())
    kw.setdefault("clock", lambda: NOW)
    return ContextCompiler(sources=list(sources), **kw)


def _tight(**over):
    """A 188-token input budget: 700 of window, 512 reserved for the answer."""
    kw = dict(context_length=700, window_known=True, max_output_tokens=512)
    kw.update(over)
    return kw


def _roomy(**over):
    kw = dict(context_length=8192, window_known=True, max_output_tokens=1024)
    kw.update(over)
    return kw


# ══ planner ════════════════════════════════════════════════════════════════

def test_a_casual_chat_wakes_neither_rag_nor_the_code_index():
    """The failure the plan opens with, and the reason this module exists."""
    plan = planner.plan(_chat_request(), available=(
        "documents", "experts", "files", "code_index", "memory_engine",
        "sessions", "project_memory"))

    assert plan.intent == "chat"
    for expensive in ("documents", "experts", "files", "code_index"):
        assert expensive not in plan.source_ids
        assert expensive in plan.skipped and plan.skipped[expensive]
    assert "retrieved_documents" not in plan.sections
    assert "code_map" not in plan.sections
    # It is still a conversation: history and memory are exactly what it needs.
    assert set(plan.source_ids) == {"memory_engine", "sessions"}


def test_a_coding_turn_in_a_workspace_asks_for_the_code_lanes():
    plan = planner.plan(_request(), available=(
        "documents", "files", "code_index", "memory_engine", "sessions"))
    assert plan.intent == "code_change"
    assert {"files", "code_index", "documents"} <= set(plan.source_ids)
    assert "code_map" in plan.sections and "retrieved_documents" in plan.sections
    assert "graph" in plan.lanes


@pytest.mark.parametrize("query,workspace,expected", [
    ("buenos dias", "", "chat"),
    ("what is the capital of France", "/repo", "chat"),
    ("the latest contest results", "/repo", "chat"),      # not "test"
    ("fix the traceback in src/app.py", "/repo", "code_change"),
    ("arregla el error de src/app.py", "/repo", "code_change"),
    ("please review this pull request", "/repo", "review"),
    ("genera una imagen de un castillo", "", "media"),
    ("research the state of the art on RAG", "", "research"),
    ("fix the bug", "", "chat"),                          # no workspace, no code
])
def test_the_signal_table_classifies_without_a_model(query, workspace, expected):
    request = _request(task=ContextTask(query=query),
                       execution=ContextExecution(owner="luis", session_id="s1",
                                                  workspace=workspace))
    assert planner.classify_intent(request) == expected


def test_a_declared_intent_beats_every_signal():
    request = _request(task=ContextTask(intent="media",
                                        query="fix the traceback in src/app.py"))
    assert planner.classify_intent(request) == "media"


def test_the_voice_consumer_is_its_own_intent_and_half_the_deadline():
    voice = _request(consumer="voice", task=ContextTask(query="que hora es"))
    assert planner.classify_intent(voice) == "voice"
    plan = planner.plan(voice, available=("memory_engine", "sessions"))
    text = planner.plan(_chat_request(), available=("memory_engine", "sessions"))
    assert plan.timeout_s == pytest.approx(text.timeout_s / 2)
    assert plan.per_source_limit <= text.per_source_limit


def test_the_phase_changes_what_is_asked_for_not_what_is_scored():
    def sections(phase):
        return set(planner.plan(
            _request(task=ContextTask(intent="code_change", phase=phase,
                                      query="the OAuth callback")),
            available=("documents",)).sections)

    planning, verifying = sections("plan"), sections("verify")
    assert "project_rules" in planning and "tool_guidance" in planning
    assert "past_experiences" in verifying and "retrieved_documents" in verifying
    assert "retrieved_memory" not in verifying
    # A phase may never drop a mandatory section.
    for phase in ("plan", "act", "verify", "summarize"):
        assert set(MANDATORY_SECTIONS) <= sections(phase)


def test_policy_removes_a_source_from_the_plan_and_says_why():
    incognito = _request(policy=ContextPolicy(allow_personal_memory=False,
                                              allow_project_sources=False))
    plan = planner.plan(incognito, available=(
        "memory_engine", "personal_memory", "project_memory", "files",
        "code_index", "documents"))

    for gated in ("memory_engine", "personal_memory"):
        assert gated not in plan.source_ids
        assert "personal memory is off" in plan.skipped[gated]
    for gated in ("project_memory", "files", "code_index"):
        assert gated not in plan.source_ids
        assert "project sources are off" in plan.skipped[gated]
    assert "documents" in plan.source_ids


def test_a_source_with_no_workspace_or_session_is_not_woken():
    bare = _request(execution=ContextExecution(owner="luis"))
    plan = planner.plan(bare, available=("files", "code_index", "sessions"))
    assert plan.source_ids == ()
    assert "no workspace" in plan.skipped["files"]
    assert "no session_id" in plan.skipped["sessions"]


def test_an_unknown_source_is_consulted_the_way_gather_would():
    plan = planner.plan(_request(), available=("brand_new_adapter",))
    assert plan.source_ids == ("brand_new_adapter",)
    assert "declares no sections" in plan.reasons["brand_new_adapter"]


def test_the_plan_is_reproducible_and_explains_itself():
    first = planner.plan(_request(), available=("documents", "files"))
    second = planner.plan(_request(), available=("documents", "files"))
    assert first == second
    assert first.to_dict() == second.to_dict()
    text = planner.explain(first)
    assert "intent=code_change" in text and "consult   documents" in text


def test_the_semantic_lane_is_off_when_the_policy_says_so():
    plan = planner.plan(_request(policy=ContextPolicy(allow_semantic_lane=False)),
                        available=("documents",))
    assert "semantic" not in plan.lanes
    assert "lexical" in plan.lanes


def test_the_retrieval_deadline_comes_from_the_setting(monkeypatch):
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda: {"agent_context_timeout_ms": 5000})
    assert planner.timeout_for(_request()) == pytest.approx(5.0)

    def boom():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(settings_mod, "load_settings", boom)
    assert planner.timeout_for(_request()) == pytest.approx(
        planner.DEFAULT_TIMEOUT_MS / 1000.0)


# ══ the compiler: mandatory context does not compete ═══════════════════════

def _safety(body="Never delete a file the user did not name.", **over):
    return _candidate("instruction:safety", body, section="system_constraints",
                      source_type="instruction", authority="system_policy",
                      trust_class="human_explicit", **over)


async def test_mandatory_context_is_placed_whole_while_memories_are_cut(ce_db):
    """Rule 1.  A tight budget takes it out of the ranked candidates, never out
    of the safety instruction."""
    fat = [_candidate(f"doc:{n}", _prose(f"alpha{n}", 40)) for n in range(6)]
    engine = _compiler([FakeSource("documents", ("retrieved_documents",), fat)])

    packet = await engine.compile(_request(), mandatory=[_safety()], **_tight())

    section = packet.section("system_constraints")
    assert section is not None and len(section.items) == 1
    assert section.items[0].transformation == "verbatim"
    assert section.items[0].body.startswith("Never delete")
    assert section.trim_policy == "never"
    assert packet.tokens() <= packet.window.input_budget
    # And what did not fit is on the record rather than merely absent.
    assert any(o.reason == "budget" for o in packet.omissions)


async def test_mandatory_context_that_cannot_fit_degrades_the_packet_by_name(ce_db):
    """Rule 1's other half: shipping quietly smaller is the failure."""
    engine = _compiler([])
    packet = await engine.compile(_request(), mandatory=[_safety(BLOB)],
                                  **_tight())

    assert packet.degraded is True
    named = [w for w in packet.warnings if w.startswith(compiler.MANDATORY_LOST)]
    assert named and "instruction:safety" in named[0]
    # It was not silently paraphrased into something that fits: an instruction
    # is a protected source type, so the packet holds a pointer to it.
    item = packet.section("system_constraints").items[0]
    assert item.transformation == "reference" and item.body == ""
    assert any(o.source_ref == "instruction:safety" and o.reason == "budget"
               for o in packet.omissions)


async def test_a_retrieved_candidate_in_a_mandatory_section_stops_competing(ce_db):
    goal = _candidate("objective:OBJ-3", "Ship the OAuth callback.",
                      section="active_goal", source_type="objective",
                      authority="binding_decision")
    noise = [_candidate(f"doc:{n}", _prose("beta", 40)) for n in range(6)]
    engine = _compiler([
        FakeSource("objectives", ("active_goal",), [goal]),
        FakeSource("documents", ("retrieved_documents",), noise),
    ])

    packet = await engine.compile(_request(), **_tight())
    section = packet.section("active_goal")
    assert section is not None and section.items[0].source_ref == "objective:OBJ-3"
    assert section.trim_policy == "never"


async def test_a_mandatory_candidate_still_answers_to_ownership(ce_db):
    stolen = _safety(owner="someone_else")
    engine = _compiler([])
    packet = await engine.compile(_request(), mandatory=[stolen], **_roomy())

    assert packet.section("system_constraints") is None
    assert [(o.reason, o.recoverable) for o in packet.omissions
            if o.source_ref == "instruction:safety"] == [("unauthorised", False)]


async def test_incognito_gates_retrieval_and_not_the_system_prompt(ce_db):
    """`allow_project_sources=False` stops a store from being *consulted*; it
    must not delete the instruction the runtime handed in itself."""
    project = _candidate("project:MEMORY.md", "Use tabs.", section="project_rules",
                         source_type="project_memory")
    engine = _compiler([FakeSource("blocks", ("project_rules",), [project])])
    request = _request(policy=ContextPolicy(allow_project_sources=False))

    packet = await engine.compile(request, mandatory=[_safety()], **_roomy())

    assert packet.section("system_constraints") is not None
    assert packet.section("project_rules") is None
    assert any(o.reason == "policy" and o.source_ref == "project:MEMORY.md"
               for o in packet.omissions)


async def test_a_mandatory_item_may_arrive_as_a_plain_mapping(ce_db):
    engine = _compiler([])
    packet = await engine.compile(_request(), mandatory=[{
        "source_type": "instruction", "source_ref": "instruction:role",
        "section": "role_and_permissions", "title": "role",
        "body": "You are a build engineer.",
    }], **_roomy())
    assert packet.section("role_and_permissions").items[0].body.startswith("You are")


async def test_the_same_mandatory_ref_twice_is_one_item_and_one_omission(ce_db):
    engine = _compiler([])
    packet = await engine.compile(_request(), mandatory=[_safety(), _safety()],
                                  **_roomy())
    assert len(packet.section("system_constraints").items) == 1
    assert [o.reason for o in packet.omissions] == ["duplicate"]


async def test_an_optional_copy_of_a_mandatory_ref_is_dropped_once(ce_db):
    engine = _compiler([FakeSource(
        "documents", ("retrieved_documents",),
        [_candidate("instruction:safety", "a retrieved copy")])])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())

    refs = [i.source_ref for i in packet.items()]
    assert refs.count("instruction:safety") == 1
    dupes = [o for o in packet.omissions if o.source_ref == "instruction:safety"]
    assert len(dupes) == 1 and "mandatory" in dupes[0].detail


# ══ the compiler: the budget is a ceiling ══════════════════════════════════

@pytest.mark.parametrize("window,output", [(700, 512), (1024, 512), (2048, 512),
                                           (8192, 1024), (32768, 4096)])
async def test_the_packet_never_exceeds_the_input_budget(ce_db, window, output):
    """Rule 2, against twenty fat candidates and an absurd budget."""
    fat = []
    for kind, section, source_type in (
            ("doc", "retrieved_documents", "document"),
            ("file", "code_map", "file"),
            ("mem", "retrieved_memory", "memory"),
            ("exp", "past_experiences", "experience")):
        for n in range(5):
            fat.append(_candidate(f"{kind}:{n}", _prose(f"{kind}{n}", 220),
                                  section=section, source_type=source_type))
    engine = _compiler([FakeSource("fake", (), fat)])

    packet = await engine.compile(
        _request(), mandatory=[_safety()], context_length=window,
        window_known=True, max_output_tokens=output)

    assert packet.window.input_budget > 0
    assert packet.tokens() <= packet.window.input_budget
    assert packet.fits()
    assert packet.omissions, "twenty fat candidates and nothing was left out?"


async def test_every_discarded_candidate_produces_exactly_one_omission(ce_db):
    """Rule 8.  `packet.omissions` is output, not logging: an absence that is
    explained twice is as unreadable as one that is not explained at all."""
    rows = [_candidate(f"doc:{n}", _prose(f"gamma{n}", 60)) for n in range(5)]
    engine = _compiler([FakeSource("documents", ("retrieved_documents",), rows)])

    packet = await engine.compile(_request(), **_tight())

    placed = {item.source_ref for item in packet.items()}
    counts = {}
    for omission in packet.omissions:
        counts[omission.source_ref] = counts.get(omission.source_ref, 0) + 1
    assert all(count == 1 for count in counts.values()), counts
    for candidate in rows:
        assert candidate.source_ref in placed or candidate.source_ref in counts


async def test_an_unspent_section_share_is_handed_to_the_next_one(ce_db):
    """Rule 7.  4000 tokens must not sit unused in `multimodal_recipes` while
    `retrieved_documents` truncates."""
    tiny = _candidate("file:map.py", "def main(): pass\n", section="code_map",
                      source_type="file")
    big = _candidate("doc:manual", _prose("delta", 300))
    engine = _compiler([FakeSource("fake", (), [tiny, big])])

    packet = await engine.compile(_request(), **_roomy())
    documents = packet.section("retrieved_documents")
    budget = packet.window.input_budget

    # `retrieved_documents` is worth 15% of a balanced profile and `code_map`
    # 10%; with only those two present and code_map spending almost nothing,
    # the document gets far more than its nominal share.
    assert documents is not None
    assert documents.tokens() > budget * 0.5
    assert packet.tokens() <= budget


async def test_an_empty_section_is_allocated_nothing_and_not_rendered(ce_db):
    engine = _compiler([FakeSource("documents", ("retrieved_documents",),
                                   [_candidate("doc:one", "a short note")])])
    packet = await engine.compile(_request(), **_roomy())
    kinds = [s.kind for s in packet.sections]
    assert "multimodal_recipes" not in kinds and "peer_findings" not in kinds


# ══ the compiler: it never raises ══════════════════════════════════════════

async def test_a_source_that_throws_costs_its_section_and_nothing_else(ce_db):
    """Rule 3.  One expert's half-written index.json is not a dead chat."""
    good = FakeSource("documents", ("retrieved_documents",),
                      [_candidate("doc:ok", "a usable note")])
    bad = FakeSource("experts", ("retrieved_documents",), raises=True)
    engine = _compiler([bad, good])

    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())

    assert isinstance(packet, ContextPacket)
    assert packet.section("system_constraints") is not None
    assert "doc:ok" in [i.source_ref for i in packet.items()]
    assert any("experts" in w and "degraded" in w for w in packet.warnings)


async def test_a_source_whose_available_probe_throws_is_survivable(ce_db):
    class Hostile(FakeSource):
        def available(self):
            raise RuntimeError("the store is on a disconnected network drive")

    engine = _compiler([Hostile("documents", ("retrieved_documents",))])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())
    assert packet.section("system_constraints") is not None


async def test_an_estimator_that_explodes_still_produces_a_packet(ce_db):
    class Boom:
        name = "boom"

        def count(self, text):
            raise RuntimeError("surrogate pair")

        def count_messages(self, messages):
            raise RuntimeError("surrogate pair")

    engine = _compiler([FakeSource("documents", ("retrieved_documents",),
                                   [_candidate("doc:one", _prose("eps", 20))])],
                       estimator=Boom())
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())

    assert isinstance(packet, ContextPacket)
    assert packet.tokens() <= packet.window.input_budget


async def test_a_broken_cache_is_not_a_broken_turn(ce_db):
    class BrokenCache:
        def get(self, scope, key):
            raise RuntimeError("cache is gone")

        def put(self, scope, key, value, **kw):
            raise RuntimeError("cache is gone")

        def scopes(self):
            raise RuntimeError("cache is gone")

    engine = ContextCompiler(sources=[], cache=BrokenCache(), clock=lambda: NOW)
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())
    assert packet.section("system_constraints") is not None


async def test_a_pipeline_failure_still_returns_the_mandatory_context(ce_db,
                                                                      monkeypatch):
    def boom(*args, **kw):
        raise RuntimeError("ranking blew up in a way nobody predicted")

    engine = _compiler([FakeSource("documents", ("retrieved_documents",),
                                   [_candidate("doc:one", "a note")])])
    monkeypatch.setattr(ContextCompiler, "_allocate_budget", boom)

    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())

    assert packet.degraded is True
    assert any("only the mandatory context" in w for w in packet.warnings)
    assert packet.section("system_constraints").items[0].body.startswith("Never")


async def test_an_unusable_store_does_not_stop_a_packet(ce_db, monkeypatch):
    def boom(*args, **kw):
        raise store.ContextStoreError("the database is a directory")

    monkeypatch.setattr(store, "db", boom)
    engine = _compiler([])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())
    assert packet.section("system_constraints") is not None


# ══ the compiler: determinism ══════════════════════════════════════════════

def _comparable(packet):
    data = packet.to_dict()
    data.pop("packet_id", None)
    data.pop("created_at", None)
    return data


async def test_the_same_input_compiles_to_the_same_packet(ce_db):
    """Rule 6.  Branching Futures compares exactly this."""
    rows = [_candidate(f"doc:{n}", _prose(f"zeta{n}", 30)) for n in range(4)]
    engine = _compiler([FakeSource("documents", ("retrieved_documents",), rows)])

    first = await engine.compile(_request(), mandatory=[_safety()], **_tight())
    second = await engine.compile(_request(), mandatory=[_safety()], **_tight())

    assert first.packet_id != second.packet_id
    assert _comparable(first) == _comparable(second)
    assert first.identity() == second.identity()


async def test_two_compilers_with_the_same_world_agree(ce_db):
    rows = [_candidate(f"doc:{n}", _prose(f"eta{n}", 30)) for n in range(4)]
    left = _compiler([FakeSource("documents", ("retrieved_documents",), rows)])
    right = _compiler([FakeSource("documents", ("retrieved_documents",), rows)])

    assert _comparable(await left.compile(_request(), **_tight())) == \
        _comparable(await right.compile(_request(), **_tight()))


async def test_a_packet_round_trips_through_its_own_contract(ce_db):
    # No leading or trailing whitespace in the bodies: `contracts.base.text`
    # strips, so a trailing newline does not survive a parse - a property of
    # the contract, not of the compiler, and not what this test is about.
    engine = _compiler([FakeSource("documents", ("retrieved_documents",),
                                   [_candidate("doc:one",
                                               _prose("theta", 10).strip())])])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())
    assert ContextPacket.parse(packet.to_dict()) == packet


# ══ the compiler: rule 3 for the conversation path ═════════════════════════

async def test_a_chat_turn_never_calls_the_document_source(ce_db):
    documents = FakeSource("documents", ("retrieved_documents",),
                           [_candidate("doc:one", "an unread manual")])
    memory = FakeSource("memory_engine", ("retrieved_memory",),
                        [_candidate("mem:1", "the user prefers metric units",
                                    section="retrieved_memory",
                                    source_type="memory")])
    engine = _compiler([documents, memory])

    packet = await engine.compile(_chat_request(), **_roomy())

    assert documents.searches == 0, "a casual chat woke the document store"
    assert memory.searches == 1
    assert packet.intent == "chat"
    assert packet.section("retrieved_documents") is None


# ══ expand: incremental, never a recompilation ═════════════════════════════

async def test_expand_adds_a_ref_without_recompiling_what_was_already_seen(ce_db):
    source = FakeSource("documents", ("retrieved_documents",),
                        [_candidate("doc:seen", _prose("iota", 8))])
    engine = _compiler([source])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())
    before = source.searches

    grown = await engine.expand(packet, refs=("doc:extra",), extra_tokens=400)

    assert grown.packet_id != packet.packet_id
    assert grown.tokens() > packet.tokens()
    assert "doc:extra" in [i.source_ref for i in grown.items()]
    assert "doc:seen" in [i.source_ref for i in grown.items()]
    # Nothing was searched again: expansion fetches by reference.
    assert source.searches == before and source.fetches == 1
    assert any(w.startswith(compiler.EXPANDED_FROM) and packet.packet_id in w
               for w in grown.warnings)


async def test_expand_does_not_fetch_a_ref_the_packet_already_carries(ce_db):
    source = FakeSource("documents", ("retrieved_documents",),
                        [_candidate("doc:seen", "a short note")])
    engine = _compiler([source])
    packet = await engine.compile(_request(), **_roomy())

    grown = await engine.expand(packet, refs=("doc:seen",))
    assert source.fetches == 0
    assert [i.source_ref for i in grown.items()] == [i.source_ref
                                                     for i in packet.items()]


async def test_expand_records_a_reference_nothing_can_resolve(ce_db):
    engine = _compiler([FakeSource("documents", ("retrieved_documents",))])
    packet = await engine.compile(_request(), **_roomy())

    grown = await engine.expand(packet, refs=("nowhere:42",))
    missing = [o for o in grown.omissions if o.source_ref == "nowhere:42"]
    assert len(missing) == 1 and missing[0].reason == "unavailable"


async def test_expand_still_respects_the_budget_it_was_granted(ce_db):
    source = FakeSource("documents", ("retrieved_documents",),
                        [_candidate("doc:huge", _prose("kappa", 400))])
    engine = _compiler([source])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_tight())

    grown = await engine.expand(packet, refs=("doc:huge",), extra_tokens=50)

    assert grown.window.input_budget <= (packet.window.max_tokens
                                         - packet.window.reserved_output
                                         - packet.window.reserved_tools)
    assert grown.tokens() <= grown.window.input_budget


async def test_expand_never_raises_and_returns_the_packet_it_was_given(ce_db,
                                                                       monkeypatch):
    engine = _compiler([FakeSource("documents", ("retrieved_documents",))])
    packet = await engine.compile(_request(), **_roomy())

    def boom(*args, **kw):
        raise RuntimeError("the source pool is on fire")

    monkeypatch.setattr(ContextCompiler, "sources", boom)
    grown = await engine.expand(packet, refs=("doc:x",))
    assert grown.packet_id == packet.packet_id
    assert any("expansion failed" in w for w in grown.warnings)


# ══ shadow: measured, never delivered ══════════════════════════════════════

MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "here is doc:one, please summarise"},
    {"role": "assistant", "content": "sure"},
    {"role": "user", "content": "fix the failing test in src/app.py"},
]


async def test_shadow_reports_the_difference_and_writes_no_ledger_row(ce_db):
    engine = _compiler([FakeSource("documents", ("retrieved_documents",),
                                   [_candidate("doc:one", _prose("lambda", 6))])])

    report = await engine.shadow(_request(), messages=MESSAGES,
                                 mandatory=[_safety()], **_roomy())

    assert report["delivered"] is False
    assert report["packet"] is not None
    assert report["summary"]["items"] >= 1
    assert "would_add" in report and "would_drop" in report
    assert report["packet_tokens"] >= 0 and report["sent_tokens"] > 0
    # Phase 1 measures; it does not record.  Nothing reached the ledger.
    assert compiler.get_packet_row(report["packet"]["packet_id"]) is None
    assert compiler.recent_packets(owner="luis") == []


async def test_shadow_never_raises(ce_db, monkeypatch):
    engine = _compiler([])

    def boom(*args, **kw):
        raise RuntimeError("compare exploded")

    monkeypatch.setattr(compiler.manifest, "compare", boom)
    report = await engine.shadow(_request(), messages=MESSAGES)
    assert report["delivered"] is False and report["packet"] is None


# ══ the ledger (§24) ═══════════════════════════════════════════════════════

async def test_a_packet_leaves_one_ledger_row_with_no_content(ce_db):
    engine = _compiler([FakeSource("documents", ("retrieved_documents",),
                                   [_candidate("doc:one", _prose("mu", 40))])])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_tight())

    row = compiler.get_packet_row(packet.packet_id)
    assert row is not None
    assert row["owner"] == "luis" and row["project_id"] == "p1"
    assert row["model"] == "test-model" and row["intent"] == "code_change"
    assert row["tokens"] == packet.tokens()
    assert row["input_budget"] == packet.window.input_budget
    assert row["section_tokens"]["system_constraints"] > 0
    assert sum(row["omission_counts"].values()) == len(packet.omissions)
    # The bodies are not in it, and neither is anything shaped like one.
    blob = str(row)
    assert "muline0" not in blob and "Never delete" not in blob


async def test_recent_packets_and_diagnostics_read_the_scope(ce_db):
    engine = _compiler([])
    await engine.compile(_request(), mandatory=[_safety()], **_roomy())
    other = _request(execution=ContextExecution(owner="ada", project_id="p9",
                                                session_id="s2"))
    await engine.compile(other, mandatory=[_safety()], **_roomy())

    mine = compiler.recent_packets(owner="luis")
    assert len(mine) == 1 and mine[0]["owner"] == "luis"
    assert len(compiler.recent_packets()) == 2

    report = compiler.diagnostics(owner="luis")
    assert report["packets"] == 1 and report["degraded"] == 0
    assert report["by_consumer"] == {"agent": 1}
    assert report["avg_tokens"] > 0 and report["last_packet_at"]


async def test_a_receipt_is_stored_beside_its_packet(ce_db):
    engine = _compiler([])
    packet = await engine.compile(_request(), mandatory=[_safety()], **_roomy())
    item = packet.items()[0]

    compiler.record_receipt(ContextReceipt(
        packet_id=packet.packet_id, request_id=packet.request_id,
        consumer="agent", used_item_ids=(item.item_id,),
        declared_item_ids=("ctxitem_invented",), feedback="helpful"))

    assert compiler.diagnostics(owner="luis")["receipts"] == 1
    # A receipt with no packet id is a receipt for nothing.
    compiler.record_receipt(ContextReceipt(packet_id=""))
    assert compiler.diagnostics(owner="luis")["receipts"] == 1


def test_the_ledger_never_raises_when_the_store_is_gone(ce_db, monkeypatch):
    def boom(*args, **kw):
        raise store.ContextStoreError("the database is a directory")

    monkeypatch.setattr(store, "db", boom)
    compiler.record_packet(ContextPacket(packet_id="ctxpkt_x"))
    compiler.record_receipt(ContextReceipt(packet_id="ctxpkt_x"))
    assert compiler.get_packet_row("ctxpkt_x") is None
    assert compiler.recent_packets() == []
    assert compiler.diagnostics()["packets"] == 0
    assert compiler.prune_packets(days=1) == 0


async def test_prune_packets_drops_only_what_is_old(ce_db):
    engine = _compiler([])
    fresh = await engine.compile(_request(), **_roomy())
    stale = ContextCompiler(sources=[], cache=WorkingSet(),
                            clock=lambda: NOW - timedelta(days=90))
    old = await stale.compile(_request(), **_roomy())

    assert compiler.prune_packets(days=30, now=NOW) == 1
    assert compiler.get_packet_row(old.packet_id) is None
    assert compiler.get_packet_row(fresh.packet_id) is not None


def test_the_singleton_compiler_is_one_per_process(ce_db):
    assert compiler.compiler() is compiler.compiler()
    first = compiler.compiler()
    compiler.reset_compiler()
    assert compiler.compiler() is not first


# ══ maintenance (§13) ══════════════════════════════════════════════════════

def test_every_named_task_exists_and_none_of_them_is_generative():
    assert set(maintenance.TASK_NAMES) == set(maintenance.TASKS)
    assert set(maintenance.TASK_NAMES) <= set(maintenance.TASK_INTERVALS_S)


def test_a_pass_returns_one_row_per_requested_task(ce_db):
    results = maintenance.run(owner="luis", project_id="p1", budget_s=30.0)
    assert [r.name for r in results] == list(maintenance.TASK_NAMES)
    assert all(r.ok for r in results)
    assert all(isinstance(r.to_dict(), dict) for r in results)

    subset = maintenance.run(("vacuum", "prune_packets", "not_a_task"), budget_s=30.0)
    assert [r.name for r in subset] == ["prune_packets", "vacuum"]


def test_prune_packets_uses_the_ledger_setting(ce_db, monkeypatch):
    import src.settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_settings",
                        lambda: {"agent_context_ledger_days": 7})
    result = maintenance.run(("prune_packets",))[0]
    assert result.ok and "7 day(s)" in result.detail


def test_the_time_budget_stops_the_pass_without_failing_it(ce_db, monkeypatch):
    import time as time_mod

    def slow(**scope):
        time_mod.sleep(0.05)
        return 1, "did some work"

    monkeypatch.setattr(maintenance, "TASKS",
                        {name: slow for name in maintenance.TASK_NAMES})
    results = maintenance.run(budget_s=0.01)

    assert results[0].changed == 1
    skipped = results[1:]
    assert skipped and all(r.ok and "time budget" in r.detail for r in skipped)


def test_maintenance_yields_to_a_turn_in_flight(ce_db, monkeypatch):
    monkeypatch.setattr(maintenance, "should_yield", lambda: True)
    results = maintenance.run(budget_s=30.0)
    assert all(r.ok and "yields" in r.detail for r in results)
    assert all(r.changed == 0 for r in results)


def test_should_yield_reads_the_run_registry(monkeypatch):
    from src import agent_runs

    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: [])
    assert maintenance.should_yield() is False
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: ["s1"])
    assert maintenance.should_yield() is True

    def boom():
        raise RuntimeError("agent_runs moved")

    monkeypatch.setattr(agent_runs, "active_session_ids", boom)
    assert maintenance.should_yield() is False


def test_a_failing_task_costs_itself_and_nothing_else(ce_db, monkeypatch):
    def boom(**scope):
        raise RuntimeError("the index is on a disconnected drive")

    tasks = dict(maintenance.TASKS)
    tasks["refresh_code_index"] = boom
    monkeypatch.setattr(maintenance, "TASKS", tasks)

    results = {r.name: r for r in maintenance.run(budget_s=30.0)}
    assert results["refresh_code_index"].ok is False
    assert "RuntimeError" in results["refresh_code_index"].detail
    assert results["vacuum"].ok is True


def test_due_and_last_run_remember_what_already_ran(ce_db):
    assert maintenance.due(now=NOW) == list(maintenance.TASK_NAMES)

    maintenance.run(("vacuum",), budget_s=30.0)
    history = maintenance.last_run()
    assert history["vacuum"]["ok"] is True and history["vacuum"]["ran_at"]
    assert "vacuum" not in maintenance.due(now=NOW + timedelta(minutes=1))
    # And it comes back once its own interval has gone by.
    assert "vacuum" in maintenance.due(now=NOW + timedelta(days=30))


def test_tasks_without_a_workspace_say_so_instead_of_failing(ce_db):
    results = {r.name: r for r in maintenance.run(
        ("refresh_code_index", "degrade_experiences"), budget_s=30.0)}
    assert all(r.ok for r in results.values())
    assert "no workspace" in results["refresh_code_index"].detail
    assert "no workspace" in results["degrade_experiences"].detail


def test_scheduled_pass_fans_scoped_tasks_over_every_project(ce_db, monkeypatch):
    calls = []

    def task(**scope):
        calls.append(dict(scope))
        return 1, "updated"

    tasks = dict(maintenance.TASKS)
    tasks["refresh_code_index"] = task
    monkeypatch.setattr(maintenance, "TASKS", tasks)
    monkeypatch.setattr(maintenance, "due", lambda: ["refresh_code_index"])

    results = maintenance.run_scheduled(projects=[
        {"id": "p1", "owner": "alice", "workspace": "A:/one"},
        {"id": "p2", "owner": "bob", "workspace": "B:/two"},
        {"id": "empty", "owner": "bob", "workspace": ""},
    ])

    assert [(c["owner"], c["project_id"], c["workspace"]) for c in calls] == [
        ("alice", "p1", "A:/one"),
        ("bob", "p2", "B:/two"),
    ]
    assert results[0].changed == 2
    assert "2/2 project workspace(s)" in results[0].detail


def test_audit_blocks_reports_and_changes_nothing(ce_db):
    result = maintenance.run(("audit_blocks",), owner="luis", project_id="p1")[0]
    assert result.ok and result.changed == 0
    assert "nothing changed" in result.detail
