"""
tests/test_context_engine_sources.py — the retrieval layer, with the stores faked.

Every test here installs its own doubles into ``sys.modules`` and tears them
down again.  Nothing touches ``memory_engine.db``, a Chroma directory or a
session database: those are other modules' tests, and a retrieval suite that
needed them would be measuring their availability rather than this layer's
behaviour.  The one real dependency kept on purpose is ``src.file_mentions``,
because the whole point of ``adapters/files.py`` is that it uses *those* guards
and not a copy of them — faking them would test the copy.

What is asserted, in the order it matters:

1. ``gather`` cannot fail.  A source that raises, hangs or returns rubbish costs
   itself and nothing else.
2. Policy gates are applied before the store is consulted, counted with a
   double that records calls rather than inspected after the fact.
3. Two owners never see each other's rows.
4. ``degraded`` survives the whole trip from a store's own flag to the
   candidate and to the ``SourceResult``.
5. Every candidate any adapter produces round-trips through
   ``ContextCandidate.parse(c.to_dict())``, and every ``source_ref`` carries
   the prefix its module documents.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types

import pytest

from src.context_engine import candidates as C
from src.context_engine.adapters import (
    documents as ad_documents,
    experts as ad_experts,
    files as ad_files,
    memory as ad_memory,
    objectives as ad_objectives,
    projects as ad_projects,
    provenance as ad_provenance,
    sessions as ad_sessions,
    state_mirror as ad_state_mirror,
)
from src.context_engine.contracts import (
    ContextCandidate,
    ContextExecution,
    ContextPolicy,
    ContextRequest,
    ContextTask,
)


# ── helpers ────────────────────────────────────────────────────────────────

def install_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    """Put a stand-in module at ``name`` for the duration of one test.

    Both halves are needed: ``import a.b as x`` falls back to
    ``sys.modules["a.b"]``, but ``from a.b import c`` also walks the parent
    package's attributes when the parent is already imported.
    """
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    parent_name, _, leaf = name.rpartition(".")
    if parent_name:
        try:
            parent = importlib.import_module(parent_name)
        except Exception:                                      # pragma: no cover
            parent = None
        if parent is not None:
            monkeypatch.setattr(parent, leaf, module, raising=False)
    return module


def make_request(*, owner="alice", workspace="", project_id="", session_id="",
                 query="", refs=(), evidence=(), **policy_kwargs) -> C.RetrievalRequest:
    return C.RetrievalRequest(
        request=ContextRequest(
            execution=ContextExecution(owner=owner, workspace=workspace,
                                       project_id=project_id, session_id=session_id),
            task=ContextTask(query=query, required_evidence=tuple(evidence)),
            policy=ContextPolicy(**policy_kwargs),
        ),
        query=query,
        explicit_refs=tuple(refs),
    )


def a_candidate(ref="mem:1", **kwargs) -> ContextCandidate:
    built = C.make_candidate(source_type="memory", source_ref=ref,
                             section="retrieved_memory", body="x", **kwargs)
    assert built is not None
    return built


class StubSource:
    """A source that does exactly what the test tells it to."""

    def __init__(self, source_id, *, sections=("retrieved_memory",), results=(),
                 raises=None, sleep=0.0, available=True, available_raises=False):
        self.source_id = source_id
        self.sections = sections
        self.handles = ()
        self._results = results
        self._raises = raises
        self._sleep = sleep
        self._available = available
        self._available_raises = available_raises
        self.searched = 0

    def available(self):
        if self._available_raises:
            raise RuntimeError("cannot reach the store")
        return self._available

    async def search(self, req):
        self.searched += 1
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raises is not None:
            raise self._raises
        return self._results

    async def fetch(self, source_ref, req):
        return None


def test_state_mirror_source_uses_an_owner_scoped_projection(monkeypatch):
    calls = []

    class _Mirror:
        def entities(self, **scope):
            calls.append(("entities", dict(scope)))
            return [{"id": "service://alice/real/comfyui"}]

        def project(self, **scope):
            calls.append(("project", dict(scope)))
            return {
                "projection_id": "stateproj_1",
                "revision": "state:7",
                "as_of": "2026-09-07T00:00:00Z",
                "freshness": "fresh",
                "sufficient": True,
                "entity_refs": ["service://alice/real/comfyui"],
                "fields": {
                    "service://alice/real/comfyui#health": {
                        "value": "available", "freshness": "fresh",
                        "epistemic": "observed", "source": "services",
                    }
                },
                "unknown_fields": [], "conflicts": [], "refresh_actions": [],
            }

    monkeypatch.setattr("src.state_mirror.service.service", lambda: _Mirror())
    req = make_request(owner="alice", project_id="project-1")
    rows = list(ad_state_mirror.StateMirrorSource()._search(req))
    assert len(rows) == 1
    row = rows[0]
    assert row.source_type == "state" and row.section == "current_state"
    assert row.source_ref == "state:project-1"
    assert row.authority == "observed_state" and "available" in row.body
    assert all(call[1]["owner"] == "alice" for call in calls)
    assert calls[1][1]["entity_refs"] == ["service://alice/real/comfyui"]


@pytest.fixture(autouse=True)
def clean_registry():
    """No test inherits another's sources, provider or cached graph."""
    C.reset_sources()
    ad_sessions.reset_history_provider()
    ad_provenance.clear_cache()
    yield
    C.reset_sources()
    ad_sessions.reset_history_provider()
    ad_provenance.clear_cache()


# ── gather: the guarantee the whole layer rests on ─────────────────────────

async def test_gather_returns_one_row_per_source_in_order():
    sources = [StubSource("a"), StubSource("b"), StubSource("c")]
    results = await C.gather(sources, make_request())
    assert [r.source_id for r in results] == ["a", "b", "c"]


async def test_gather_survives_a_source_that_raises():
    boom = StubSource("boom", raises=RuntimeError("index.json is half written"))
    fine = StubSource("fine", results=(a_candidate("mem:ok"),))
    results = await C.gather([boom, fine], make_request())

    failed, survived = results
    assert failed.source_id == "boom"
    assert failed.degraded is True
    assert "RuntimeError" in failed.error and "half written" in failed.error
    assert failed.candidates == ()

    assert survived.degraded is False
    assert [c.source_ref for c in survived.candidates] == ["mem:ok"]


async def test_gather_cuts_a_source_that_hangs_and_keeps_the_rest():
    slow = StubSource("slow", sleep=5.0)
    fast = StubSource("fast", results=(a_candidate("mem:fast"),))
    results = await C.gather([slow, fast], make_request(), timeout_s=0.05)

    hung, arrived = results
    assert hung.degraded is True
    assert "timeout" in hung.error
    assert arrived.candidates and arrived.error == ""


async def test_gather_marks_an_unavailable_source_without_degrading_it():
    off = StubSource("off", available=False)
    results = await C.gather([off], make_request())
    assert results[0].error == "unavailable"
    # A switched-off source did not damage the packet, and saying it did would
    # teach every reader to ignore the flag.
    assert results[0].degraded is False
    assert off.searched == 0


async def test_gather_degrades_when_available_itself_raises():
    broken = StubSource("broken", available_raises=True)
    results = await C.gather([broken], make_request())
    assert results[0].degraded is True
    assert "available()" in results[0].error
    assert broken.searched == 0


async def test_gather_skips_a_source_that_cannot_fill_the_requested_sections():
    memory = StubSource("memory", sections=("retrieved_memory",))
    req = C.RetrievalRequest(request=ContextRequest(), sections=("active_goal",))
    results = await C.gather([memory], req)
    assert results[0].error == "sections not requested"
    assert memory.searched == 0


async def test_gather_drops_malformed_candidates_and_reports_it():
    junk = StubSource("junk", results=({"source_ref": "mem:1"}, None,
                                       a_candidate("mem:real")))
    results = await C.gather([junk], make_request())
    assert [c.source_ref for c in results[0].candidates] == ["mem:real"]
    assert results[0].degraded is True
    assert "dropped 2" in results[0].error


async def test_gather_enforces_the_per_source_limit():
    many = StubSource("many", results=tuple(a_candidate(f"mem:{i}") for i in range(20)))
    req = C.RetrievalRequest(request=ContextRequest(), limit=3)
    results = await C.gather([many], req)
    assert len(results[0].candidates) == 3


async def test_gather_propagates_a_degraded_candidate_to_the_result():
    source = StubSource("degraded", results=(a_candidate("mem:1", degraded=True),))
    results = await C.gather([source], make_request())
    assert results[0].degraded is True


async def test_gather_with_no_sources_is_an_empty_list():
    assert await C.gather([], make_request()) == []


# ── the request object ─────────────────────────────────────────────────────

def test_scope_comes_from_execution_only():
    req = make_request(owner="alice", workspace="/w", project_id="p1", session_id="s1")
    assert (req.owner, req.workspace, req.project_id, req.session_id) == \
        ("alice", "/w", "p1", "s1")


def test_top_is_clamped_by_the_policy_ceiling():
    req = C.RetrievalRequest(request=ContextRequest(
        policy=ContextPolicy(max_items_per_source=4)), limit=50)
    assert req.top() == 4
    loose = C.RetrievalRequest(request=ContextRequest(
        policy=ContextPolicy(max_items_per_source=40)), limit=6)
    assert loose.top() == 6


def test_refs_for_merges_the_round_and_the_request():
    req = C.RetrievalRequest(
        request=ContextRequest(explicit_refs=("expert:tolkien", "file:a.py")),
        explicit_refs=("expert:borges", "expert:tolkien"))
    assert req.refs_for("expert:") == ("expert:borges", "expert:tolkien")
    assert req.refs_for("file:") == ("file:a.py",)


def test_the_semantic_lane_answers_to_the_policy():
    off = C.RetrievalRequest(request=ContextRequest(
        policy=ContextPolicy(allow_semantic_lane=False)))
    assert off.allows("semantic") is False
    assert off.allows("lexical") is True


def test_sections_empty_means_every_section():
    req = C.RetrievalRequest(request=ContextRequest())
    assert req.wants("active_goal") and req.wants("code_map")


# ── make_candidate: nothing invalid leaves this layer ──────────────────────

def test_a_candidate_without_provenance_is_not_a_candidate():
    assert C.make_candidate(source_type="memory", source_ref="  ",
                            section="retrieved_memory", body="x") is None


def test_make_candidate_clips_instead_of_raising():
    built = C.make_candidate(
        source_type="not_a_type", source_ref="mem:1" + "x" * 5000,
        section="not_a_section", title="t" * 4000, body="b",
        lanes=("lexical", "lexical", "nonsense"), scores={"a": 1, "bad": "x", "f": True},
        trust_class="invented", authority="invented", source_revision="r" * 500,
        observed_at="not a date", owner="o" * 900, project_id="p" * 900)
    assert built is not None
    assert len(built.source_ref) == C.MAX_REF_CHARS
    assert len(built.title) == C.MAX_TITLE_CHARS
    assert built.source_type == "memory" and built.section == "retrieved_memory"
    assert built.trust_class == "agent_assertion" and built.authority == "agent_claim"
    assert built.lanes == ("lexical",)
    assert built.scores == {"a": 1.0}
    assert built.observed_at == ""
    ContextCandidate.parse(built.to_dict())


@pytest.mark.parametrize("raw", [1699999999, 1699999999.5, "2024-01-02T03:04:05Z",
                                 "2024-01-02 03:04:05"])
def test_iso_or_blank_reads_every_shape_the_stores_write(raw):
    assert C.iso_or_blank(raw).endswith("Z")


@pytest.mark.parametrize("raw", [None, "", "yesterday", True, object()])
def test_iso_or_blank_refuses_to_guess(raw):
    assert C.iso_or_blank(raw) == ""


# ── the registry ───────────────────────────────────────────────────────────

def test_register_get_and_reset():
    stub = StubSource("stub")
    C.register_source(stub)
    assert C.get_source("stub") is stub
    assert "stub" in C.registered_sources()
    C.reset_sources()
    assert C.get_source("stub") is None


def test_a_registered_source_wins_over_the_default_of_the_same_id():
    stub = StubSource("memory_engine")
    C.register_source(stub)
    assert C.get_source("memory_engine") is stub
    assert C.get_source("memory_engine") in C.default_sources()


def test_default_sources_instantiates_every_adapter():
    ids = {s.source_id for s in C.default_sources()}
    assert {"memory_engine", "personal_memory", "objectives", "project_memory",
            "sessions", "files", "documents", "experts", "provenance"} <= ids


# ── memory_engine ──────────────────────────────────────────────────────────

class FakeMemoryEngine:
    """Enough of ``src/memory_engine.py`` to test the adapter, and no more."""

    def __init__(self, items, degraded=False):
        self.items = items
        self.degraded = degraded
        self.search_calls = []
        self.scoped_calls = []

    def search(self, query, owner=None, project=None, k=8, *, now=None,
               levels=None, statuses=("active", "anti_pattern"), touch_hits=True):
        self.search_calls.append({"query": query, "owner": owner, "project": project,
                                  "k": k, "touch_hits": touch_hits})
        rows = []
        for item in self._visible(owner, project):
            row = self.public_item(item)
            row.update({"relevance": 0.6, "lexical": 0.6,
                        "semantic": 0.0 if self.degraded else 0.4,
                        "graph": 0.0, "degraded": self.degraded, "score": 0.42})
            rows.append(row)
        return rows[:k]

    def scoped_items(self, owner=None, project=None, statuses=()):
        self.scoped_calls.append({"owner": owner, "project": project})
        return list(self._visible(owner, project))

    def _visible(self, owner, project):
        # Mirrors the real `scoped_items`: own rows plus unscoped ones.
        return [i for i in self.items
                if (not i.get("owner") or i.get("owner") == owner)
                and (not i.get("project") or i.get("project") == project)]

    def public_item(self, item, now=None):
        row = dict(item)
        row.update({"id8": str(item.get("id"))[:8], "effective_score": 0.8,
                    "harmful_ratio": 0.0, "helpful_count": 1, "harmful_count": 0})
        return row

    def get_item(self, item_id):
        return next((dict(i) for i in self.items if i.get("id") == item_id), None)

    @staticmethod
    def bm25_scores(query, docs):
        wanted = set(str(query).lower().split())
        return {doc_id: float(len(wanted & set(str(text).lower().split())))
                for doc_id, text in docs}


def learned(item_id, text, **kwargs):
    base = {"id": item_id, "text": text, "owner": "alice", "project": "/w",
            "level": "semantic", "status": "active", "maturity": "candidate",
            "trust_class": "agent_assertion", "updated_at": "2024-05-01T00:00:00Z",
            "created_at": "2024-04-01T00:00:00Z"}
    base.update(kwargs)
    return base


async def test_memory_engine_search_is_scoped_and_never_marks_rows_used(monkeypatch):
    fake = FakeMemoryEngine([learned("m1", "prefer pathlib")])
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake.search, "scoped_items": fake.scoped_items,
        "public_item": fake.public_item, "get_item": fake.get_item})

    source = ad_memory.MemoryEngineSource()
    req = make_request(owner="alice", workspace="/w", project_id="p1", query="pathlib")
    results = await C.gather([source], req)

    call = fake.search_calls[0]
    assert call["owner"] == "alice"
    # The memory engine's `project` column is a WORKSPACE PATH, not a project id.
    assert call["project"] == "/w"
    # Retrieval is not use: touching here would train the curator on candidates
    # the compiler may well drop for budget.
    assert call["touch_hits"] is False
    assert [c.source_ref for c in results[0].candidates] == ["mem:m1"]


async def test_memory_engine_degraded_reaches_the_candidate_and_the_result(monkeypatch):
    fake = FakeMemoryEngine([learned("m1", "prefer pathlib")], degraded=True)
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake.search, "scoped_items": fake.scoped_items,
        "public_item": fake.public_item, "get_item": fake.get_item})

    results = await C.gather([ad_memory.MemoryEngineSource()],
                             make_request(owner="alice", workspace="/w", query="pathlib"))
    candidate = results[0].candidates[0]
    assert candidate.degraded is True
    assert "semantic" not in candidate.lanes
    assert results[0].degraded is True


async def test_memory_engine_flags_anti_patterns(monkeypatch):
    fake = FakeMemoryEngine([
        learned("m1", "AVOID: rebasing shared branches", status="anti_pattern"),
        learned("m2", "the user asked for tabs", trust_class="human_explicit"),
    ])
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake.search, "scoped_items": fake.scoped_items,
        "public_item": fake.public_item, "get_item": fake.get_item})

    results = await C.gather([ad_memory.MemoryEngineSource()],
                             make_request(owner="alice", workspace="/w", query="branches"))
    by_ref = {c.source_ref: c for c in results[0].candidates}
    anti = by_ref["mem:m1"]
    assert anti.meta["anti_pattern"] is True
    assert anti.authority == "validated_experience"
    human = by_ref["mem:m2"]
    assert (human.trust_class, human.authority) == ("human_explicit", "human_memory")


async def test_memory_engine_returns_standing_rules_when_there_is_no_query(monkeypatch):
    fake = FakeMemoryEngine([
        learned("r1", "always run the linter", level="procedural"),
        learned("m1", "the deploy was on tuesday", level="episodic"),
        learned("a1", "AVOID: force pushing", status="anti_pattern"),
    ])
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake.search, "scoped_items": fake.scoped_items,
        "public_item": fake.public_item, "get_item": fake.get_item})

    results = await C.gather([ad_memory.MemoryEngineSource()],
                             make_request(owner="alice", workspace="/w", query=""))
    refs = [c.source_ref for c in results[0].candidates]
    assert refs == ["mem:r1", "mem:a1"]
    assert fake.search_calls == []          # nothing to match on, so nothing searched
    assert all(c.lanes == ("mandatory",) for c in results[0].candidates)


async def test_two_owners_never_see_each_others_memory(monkeypatch):
    fake = FakeMemoryEngine([
        learned("m_alice", "alice prefers pytest", owner="alice"),
        learned("m_bob", "bob prefers unittest", owner="bob"),
        learned("m_shared", "the repo prefers pytest", owner=""),
    ])
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake.search, "scoped_items": fake.scoped_items,
        "public_item": fake.public_item, "get_item": fake.get_item})
    source = ad_memory.MemoryEngineSource()

    alice = await C.gather([source], make_request(owner="alice", workspace="/w",
                                                  query="prefers"))
    bob = await C.gather([source], make_request(owner="bob", workspace="/w",
                                                query="prefers"))
    assert {c.source_ref for c in alice[0].candidates} == {"mem:m_alice", "mem:m_shared"}
    assert {c.source_ref for c in bob[0].candidates} == {"mem:m_bob", "mem:m_shared"}


async def test_a_memory_id_from_another_owner_cannot_be_fetched(monkeypatch):
    fake = FakeMemoryEngine([learned("m_bob", "bob's rule", owner="bob")])
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake.search, "scoped_items": fake.scoped_items,
        "public_item": fake.public_item, "get_item": fake.get_item})
    source = ad_memory.MemoryEngineSource()
    req = make_request(owner="alice", workspace="/w")
    assert await source.fetch("mem:m_bob", req) is None


# ── incognito: the gate that has to run before the store does ──────────────

class CountingMemoryManager:
    """Counts, and proves the gate ran before anything was read."""

    def __init__(self):
        self.load_calls = 0
        self.relevance_calls = 0
        self.entries = [
            {"id": "e1", "text": "the user lives in Madrid", "source": "user",
             "category": "fact", "owner": "alice", "timestamp": 1700000000, "uses": 2},
        ]

    def load(self, owner=None):
        self.load_calls += 1
        return [e for e in self.entries if owner is None or e.get("owner") == owner]

    def get_relevant_memories(self, query, memories, threshold=0.05, max_items=8):
        self.relevance_calls += 1
        return list(memories)[:max_items]


async def test_incognito_never_opens_the_personal_memory_store():
    store = CountingMemoryManager()
    source = ad_memory.PersonalMemorySource(manager=store)
    req = make_request(owner="alice", workspace="/w", query="where does the user live",
                       allow_personal_memory=False)

    results = await C.gather([source], req)

    assert store.load_calls == 0
    assert store.relevance_calls == 0
    assert results[0].candidates == ()
    # A gated source is not a broken one.
    assert results[0].degraded is False


async def test_incognito_also_gates_the_learned_store(monkeypatch):
    fake = FakeMemoryEngine([learned("m1", "prefer pathlib")])
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake.search, "scoped_items": fake.scoped_items,
        "public_item": fake.public_item, "get_item": fake.get_item})

    req = make_request(owner="alice", workspace="/w", query="pathlib",
                       allow_personal_memory=False)
    results = await C.gather([ad_memory.MemoryEngineSource()], req)

    # `search()` bumps last_used and `scoped_items` is still a read of the
    # owner's private rows; neither may happen for an incognito turn.
    assert fake.search_calls == [] and fake.scoped_calls == []
    assert results[0].candidates == ()


async def test_personal_memory_is_owner_scoped_and_human_trusted():
    store = CountingMemoryManager()
    store.entries.append({"id": "e2", "text": "bob likes tea", "source": "user",
                          "category": "fact", "owner": "bob", "timestamp": 1700000000})
    source = ad_memory.PersonalMemorySource(manager=store)
    results = await C.gather([source], make_request(owner="alice", query="live"))

    candidate = results[0].candidates[0]
    assert candidate.source_ref == "pmem:e1"
    assert (candidate.trust_class, candidate.authority) == ("human_explicit", "human_memory")
    assert candidate.observed_at.endswith("Z")
    assert all(c.source_ref != "pmem:e2" for c in results[0].candidates)


# ── objectives ─────────────────────────────────────────────────────────────

def objectives_module(records, monkeypatch):
    state = {"objectives": {r["id"]: r for r in records}, "edges": []}
    install_module(monkeypatch, "services.objectives", **{
        "load_state": lambda project: state,
        "serialize_state": lambda s: {
            "objectives": [dict(r, deps=[]) for r in s["objectives"].values()],
            "edges": []},
    })
    return state


async def test_objectives_split_the_work_from_the_decisions(monkeypatch):
    objectives_module([
        {"id": "OBJ-1", "title": "ship the engine", "status": "in_progress",
         "priority": 1, "notes": "", "updated_at": "2024-05-02T00:00:00Z"},
        {"id": "OBJ-2", "title": "pick a store", "status": "done", "priority": 2,
         "notes": "sqlite, separate file: a rebuild beats a migration",
         "updated_at": "2024-05-01T00:00:00Z"},
        {"id": "OBJ-3", "title": "finished, unexplained", "status": "done",
         "priority": 3, "notes": "", "updated_at": "2024-04-01T00:00:00Z"},
        {"id": "OBJ-4", "title": "abandoned", "status": "dropped", "priority": 4,
         "notes": "n/a", "updated_at": "2024-03-01T00:00:00Z"},
    ], monkeypatch)

    results = await C.gather([ad_objectives.ObjectivesSource()],
                             make_request(owner="alice", workspace="/w"))
    by_section = {c.section: c for c in results[0].candidates}
    assert by_section["active_goal"].source_ref == "objective:OBJ-1"
    assert by_section["active_goal"].lanes == ("mandatory",)
    assert by_section["decisions"].source_ref == "objective:OBJ-2"
    assert all(c.authority == "binding_decision" for c in results[0].candidates)
    refs = {c.source_ref for c in results[0].candidates}
    # A done objective with no note records that something happened, not what
    # was decided; a dropped one records nothing at all.
    assert "objective:OBJ-3" not in refs and "objective:OBJ-4" not in refs


async def test_objectives_stay_out_when_project_sources_are_forbidden(monkeypatch):
    calls = []

    def load_state(project):
        calls.append(project)
        return {"objectives": {}, "edges": []}

    install_module(monkeypatch, "services.objectives",
                   load_state=load_state, serialize_state=lambda s: s)
    req = make_request(owner="alice", workspace="/w", allow_project_sources=False)
    await C.gather([ad_objectives.ObjectivesSource()], req)
    assert calls == []


# ── project memory ─────────────────────────────────────────────────────────

class FakeProjectStore:
    def __init__(self, files):
        self.files = files

    def list_memory_files(self, project):
        return [{"name": n, "size": len(b), "modified": 1700000000}
                for n, b in self.files.items()]

    def read_index(self, project):
        return self.files.get("MEMORY.md", "")

    def read_memory_file(self, project, filename):
        if filename == "unreadable.md":
            raise RuntimeError("permission denied")
        return self.files.get(filename, "")

    def memory_dir(self, project):
        return os.path.join(project.get("workspace", ""), ".odysseus")


def projects_module(store, monkeypatch, *, instructions=""):
    install_module(monkeypatch, "services.projects", **{
        "MEMORY_INDEX": "MEMORY.md",
        "get_store": lambda: store,
        "project_for_session": lambda session_id, owner=None: None,
        "instructions_for_session": lambda session_id, owner=None: instructions,
    })


async def test_project_memory_is_one_candidate_per_file(monkeypatch):
    store = FakeProjectStore({
        "MEMORY.md": "# index\nread conventions.md first",
        "conventions.md": "tabs are forbidden; use four spaces",
        "unreadable.md": "never seen",
    })
    projects_module(store, monkeypatch, instructions="Answer in Spanish.")

    req = make_request(owner="alice", workspace="/w", session_id="s1")
    results = await C.gather([ad_projects.ProjectMemorySource()], req)
    refs = [c.source_ref for c in results[0].candidates]

    assert "project:instructions" in refs
    assert "project:MEMORY.md" in refs
    assert "project:conventions.md" in refs
    # An unreadable file costs itself and not the section.
    assert "project:unreadable.md" not in refs
    assert all(c.trust_class == "human_explicit" for c in results[0].candidates)
    assert all(c.section == "project_rules" for c in results[0].candidates)


async def test_project_memory_ranks_files_against_the_query(monkeypatch):
    fake_engine = FakeMemoryEngine([])
    install_module(monkeypatch, "src.memory_engine",
                   bm25_scores=fake_engine.bm25_scores)
    store = FakeProjectStore({"a.md": "nothing relevant here",
                              "b.md": "the deploy runbook lives here"})
    projects_module(store, monkeypatch)

    req = make_request(owner="alice", workspace="/w", query="deploy runbook")
    results = await C.gather([ad_projects.ProjectMemorySource()], req)
    assert [c.source_ref for c in results[0].candidates][0] == "project:b.md"
    assert results[0].candidates[0].lanes == ("lexical",)


# ── documents ──────────────────────────────────────────────────────────────

class FakeRag:
    def __init__(self, hits, healthy=True):
        self.hits = hits
        self.calls = []
        self.vector_rag = types.SimpleNamespace(healthy=healthy)

    def search(self, query, k=5, owner=None):
        self.calls.append({"query": query, "k": k, "owner": owner})
        return self.hits[:k]


def a_hit(similarity, source="/docs/manual.pdf", chunk_id=3):
    return {"id": f"{source}:{chunk_id}", "document": "the relevant paragraph",
            "similarity": similarity, "vector_similarity": similarity,
            "keyword_score": 0.1, "embedding_lane": "default",
            "metadata": {"source": source, "filename": "manual.pdf",
                         "chunk_id": chunk_id, "type": ".pdf", "owner": "alice"}}


async def test_documents_respect_the_similarity_floor():
    rag = FakeRag([a_hit(0.80, chunk_id=1), a_hit(0.20, chunk_id=2)])
    source = ad_documents.DocumentSource(manager=rag)
    results = await C.gather([source], make_request(owner="alice", query="how do I"))

    assert [c.source_ref for c in results[0].candidates] == \
        ["doc:/docs/manual.pdf#chunk1"]
    candidate = results[0].candidates[0]
    assert (candidate.trust_class, candidate.authority) == ("untrusted", "inference")
    assert rag.calls[0]["owner"] == "alice"


async def test_documents_refuse_an_unscoped_search():
    rag = FakeRag([a_hit(0.9)])
    source = ad_documents.DocumentSource(manager=rag)
    # `rag_vector` drops its `where` filter for a falsy owner, so an ownerless
    # request would search every tenant's chunks.
    await C.gather([source], make_request(owner="", query="how do I"))
    assert rag.calls == []


async def test_documents_report_an_unhealthy_collection_as_degraded():
    rag = FakeRag([a_hit(0.9)], healthy=False)
    source = ad_documents.DocumentSource(manager=rag)
    results = await C.gather([source], make_request(owner="alice", query="how do I"))
    assert results[0].degraded is True
    assert results[0].candidates[0].lanes == ("lexical",)


def test_the_similarity_floor_still_matches_chat_processor():
    # The constant is duplicated on purpose (importing chat_processor pulls the
    # web-search stack onto the turn path); this is what keeps the copy honest.
    chat_processor = pytest.importorskip("src.chat_processor")
    assert ad_documents.RAG_SIMILARITY_FLOOR == \
        chat_processor.ChatProcessor.RAG_SIMILARITY_THRESHOLD


# ── experts ────────────────────────────────────────────────────────────────

def experts_module(monkeypatch, result, *, calls=None, enabled=True):
    def search(slug, query, k=6, *, owner=None, reranker=True):
        if calls is not None:
            calls.append({"slug": slug, "query": query, "k": k, "owner": owner})
        return result

    install_module(monkeypatch, "services.experts", **{
        "search": search,
        "citation": lambda slug, chunk_id: {
            "chunk_id": chunk_id, "source": "book.pdf", "page": 12,
            "start_line": 1, "end_line": 9, "excerpt": "the cited passage",
            "file_path": "/e/book.pdf", "file_url": "/api/experts/x/corpus/book.pdf"},
        "experts_enabled": lambda: enabled,
    })


async def test_experts_do_not_run_unless_the_runtime_named_one(monkeypatch):
    calls = []
    experts_module(monkeypatch, {"hits": [], "tier": "hybrid", "degraded": False},
                   calls=calls)
    await C.gather([ad_experts.ExpertSource()],
                   make_request(owner="alice", query="ask the tolkien expert"))
    # The slug is in the prose, which a model can write; that is not a request.
    assert calls == []


async def test_experts_run_for_an_explicit_ref(monkeypatch):
    calls = []
    experts_module(monkeypatch, {
        "hits": [{"chunk_id": "c0123456789abcdef", "source": "book.pdf", "page": 12,
                  "start_line": 1, "end_line": 9, "text": "a passage",
                  "score": 0.7, "tier": "hybrid"}],
        "tier": "hybrid", "degraded": False, "rerank_reason": None}, calls=calls)

    results = await C.gather([ad_experts.ExpertSource()],
                             make_request(owner="alice", query="what about elves",
                                          refs=("expert:tolkien",)))
    assert calls[0]["slug"] == "tolkien"
    assert [c.source_ref for c in results[0].candidates] == \
        ["expert:tolkien#c0123456789abcdef"]
    assert results[0].degraded is False


async def test_a_lexical_only_expert_answer_is_degraded(monkeypatch):
    experts_module(monkeypatch, {
        "hits": [{"chunk_id": "c1", "source": "book.pdf", "page": None,
                  "start_line": 4, "end_line": 8, "text": "a passage", "score": 0.3}],
        "tier": "lexical", "degraded": True})

    results = await C.gather([ad_experts.ExpertSource()],
                             make_request(owner="alice", query="elves",
                                          evidence=("expert:tolkien",)))
    assert results[0].degraded is True
    assert results[0].candidates[0].lanes == ("lexical",)


async def test_one_broken_expert_does_not_cost_the_other(monkeypatch):
    def search(slug, query, k=6, *, owner=None, reranker=True):
        if slug == "broken":
            raise RuntimeError("index.json is corrupt")
        return {"hits": [{"chunk_id": "c9", "source": "b.pdf", "page": 1,
                          "start_line": 1, "end_line": 2, "text": "ok", "score": 0.5}],
                "tier": "hybrid", "degraded": False}

    install_module(monkeypatch, "services.experts", search=search,
                   citation=lambda *a: None, experts_enabled=lambda: True)
    results = await C.gather(
        [ad_experts.ExpertSource()],
        make_request(owner="alice", query="q", refs=("expert:broken", "expert:good")))
    assert [c.source_ref for c in results[0].candidates] == ["expert:good#c9"]


# ── sessions ───────────────────────────────────────────────────────────────

async def test_sessions_are_unavailable_without_a_provider():
    results = await C.gather([ad_sessions.SessionSource()],
                             make_request(owner="alice", session_id="s1"))
    assert results[0].error == "unavailable"


async def test_sessions_return_the_tail_with_trust_by_speaker():
    seen = []

    def provider(session_id, owner):
        seen.append((session_id, owner))
        return [{"role": "user", "content": "make it faster"},
                {"role": "assistant", "content": "I rewrote the loop"},
                {"role": "user", "content": "did the tests pass?"}]

    ad_sessions.set_history_provider(provider)
    req = C.RetrievalRequest(
        request=ContextRequest(execution=ContextExecution(owner="alice",
                                                          session_id="s1")),
        limit=2)
    results = await C.gather([ad_sessions.SessionSource()], req)

    assert seen == [("s1", "alice")]
    refs = [c.source_ref for c in results[0].candidates]
    assert refs == ["session:s1#1", "session:s1#2"]
    trust = {c.meta["role"]: (c.trust_class, c.authority) for c in results[0].candidates}
    assert trust["user"] == ("human_explicit", "user_instruction")
    assert trust["assistant"] == ("agent_assertion", "agent_claim")


async def test_sessions_skip_multimodal_blocks_rather_than_flattening_them():
    ad_sessions.set_history_provider(
        lambda session_id, owner: [{"role": "user", "content": [{"type": "image"}]},
                                   {"role": "user", "content": "and this one?"}])
    results = await C.gather([ad_sessions.SessionSource()],
                             make_request(owner="alice", session_id="s1"))
    assert [c.source_ref for c in results[0].candidates] == ["session:s1#1"]


# ── provenance ─────────────────────────────────────────────────────────────

def provenance_module(monkeypatch, builds):
    def build(owner=None, *, project=None, workspace=None, limit_nodes=2000, now=None):
        builds.append((owner, workspace))
        return {
            "nodes": [
                {"id": "checkpoint:job-17", "kind": "checkpoint",
                 "label": f"pathlib migration deployed for {owner}",
                 "detail": "3 files changed", "meta": {"job_id": "job-17"}},
                {"id": "objective:OBJ-1", "kind": "objective",
                 "label": "ship the pathlib migration", "detail": "done", "meta": {}},
                {"id": "file:src/app.py", "kind": "file", "label": "app.py",
                 "detail": "", "meta": {}},
            ],
            "edges": [], "sources": {}, "truncated": False,
        }

    install_module(monkeypatch, "src.provenance_graph", **{
        "build": build,
        "DEFAULT_LIMIT_NODES": 2000,
        "enabled": lambda: True,
        "ranking_signal": lambda graph, node_ids=None: {"checkpoint:job-17": 0.1},
        "explain": lambda graph, node_id, hops=3: {
            "node": {"id": node_id}, "missing": False,
            "summary": f"{node_id} rests on a diff observed on disk",
            "steps": [{"why": "the checkpoint diff of job-17 shows src/app.py changed"}]},
    })


async def test_provenance_sections_follow_the_node_kind(monkeypatch):
    provenance_module(monkeypatch, [])
    results = await C.gather([ad_provenance.ProvenanceSource()],
                             make_request(owner="alice", workspace="/w"))
    by_ref = {c.source_ref: c for c in results[0].candidates}

    assert by_ref["prov:checkpoint:job-17"].section == "past_experiences"
    assert by_ref["prov:checkpoint:job-17"].trust_class == "observed"
    assert by_ref["prov:objective:OBJ-1"].section == "decisions"
    # files.py owns file nodes, with the contents rather than a label.
    assert "prov:file:src/app.py" not in by_ref


async def test_provenance_caches_per_owner_and_workspace(monkeypatch):
    builds = []
    provenance_module(monkeypatch, builds)
    source = ad_provenance.ProvenanceSource()

    await C.gather([source], make_request(owner="alice", workspace="/w"))
    await C.gather([source], make_request(owner="alice", workspace="/w"))
    assert builds == [("alice", "/w")]          # the second turn reused the build

    await C.gather([source], make_request(owner="bob", workspace="/w"))
    # The owner is part of the cache KEY, not filtered out of the value: two
    # owners cannot share an entry even by accident.
    assert builds == [("alice", "/w"), ("bob", "/w")]


async def test_provenance_keeps_the_graph_signal_under_its_cap(monkeypatch):
    provenance_module(monkeypatch, [])
    results = await C.gather([ad_provenance.ProvenanceSource()],
                             make_request(owner="alice", workspace="/w"))
    assert results[0].candidates
    for candidate in results[0].candidates:
        assert candidate.scores.get("graph", 0.0) <= 0.1


# ── files ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "\n".join(f"line {n}" for n in range(1, 41)), encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=hunter2", encoding="utf-8")
    (tmp_path / "big.txt").write_text("x" * (ad_files.MAX_FILE_CHARS + 10),
                                      encoding="utf-8")
    return str(tmp_path)


async def test_a_named_file_is_read_now_and_marked_observed(workspace):
    results = await C.gather([ad_files.FileSource()],
                             make_request(owner="alice", workspace=workspace,
                                          refs=("file:src/app.py",)))
    candidate = results[0].candidates[0]
    assert candidate.source_ref == "file:src/app.py"
    assert (candidate.trust_class, candidate.authority) == ("observed", "observed_state")
    assert "line 1" in candidate.body
    assert candidate.section == "code_map"


async def test_a_secret_is_named_but_never_inlined(workspace):
    results = await C.gather([ad_files.FileSource()],
                             make_request(owner="alice", workspace=workspace,
                                          refs=("file:.env",)))
    candidate = results[0].candidates[0]
    assert candidate.source_ref == "file:.env"
    assert candidate.body == ""
    assert candidate.meta["withheld"] == "sensitive"
    assert "hunter2" not in candidate.to_dict()["body"]


async def test_a_path_that_escapes_the_workspace_is_withheld(workspace):
    results = await C.gather([ad_files.FileSource()],
                             make_request(owner="alice", workspace=workspace,
                                          refs=("file:../outside.txt",)))
    candidate = results[0].candidates[0]
    assert candidate.meta["withheld"] == "outside_workspace"
    assert candidate.body == ""


async def test_an_oversized_file_is_named_and_left_on_disk(workspace):
    results = await C.gather([ad_files.FileSource()],
                             make_request(owner="alice", workspace=workspace,
                                          refs=("file:big.txt",)))
    assert results[0].candidates[0].meta["withheld"] == "too_large"


async def test_a_named_range_narrows_the_reference_and_the_body(workspace):
    results = await C.gather([ad_files.FileSource()],
                             make_request(owner="alice", workspace=workspace,
                                          refs=("file:src/app.py#L10-L12",)))
    candidate = results[0].candidates[0]
    assert candidate.source_ref == "file:src/app.py#L10-L12"
    assert candidate.meta["lines"] == [10, 12]
    assert "line 40" not in candidate.body


# ── the two rules that hold for every adapter at once ──────────────────────

DOCUMENTED_PREFIXES = ("mem:", "pmem:", "objective:", "project:", "doc:",
                       "expert:", "session:", "prov:", "file:")


@pytest.fixture()
def every_source(monkeypatch, workspace):
    """One request that makes every adapter produce at least one candidate."""
    fake_engine = FakeMemoryEngine([
        learned("m1", "prefer pathlib over os.path", project="", owner="alice"),
        learned("a1", "AVOID: force pushing", status="anti_pattern", project=""),
    ])
    install_module(monkeypatch, "src.memory_engine", **{
        "search": fake_engine.search, "scoped_items": fake_engine.scoped_items,
        "public_item": fake_engine.public_item, "get_item": fake_engine.get_item,
        "bm25_scores": fake_engine.bm25_scores})
    objectives_module([
        {"id": "OBJ-1", "title": "ship the context engine", "status": "open",
         "priority": 1, "notes": "", "updated_at": "2024-05-02T00:00:00Z"},
        {"id": "OBJ-2", "title": "pick a store", "status": "done", "priority": 2,
         "notes": "sqlite in its own file", "updated_at": "2024-05-01T00:00:00Z"},
    ], monkeypatch)
    projects_module(FakeProjectStore({"MEMORY.md": "# index of pathlib conventions"}),
                    monkeypatch, instructions="Answer in Spanish.")
    experts_module(monkeypatch, {
        "hits": [{"chunk_id": "c0123456789abcdef", "source": "book.pdf", "page": 12,
                  "start_line": 1, "end_line": 9, "text": "a passage", "score": 0.7}],
        "tier": "reranked", "degraded": False, "rerank_reason": None})
    provenance_module(monkeypatch, [])
    ad_sessions.set_history_provider(
        lambda session_id, owner: [{"role": "user", "content": "use pathlib please"}])

    sources = [
        ad_memory.MemoryEngineSource(),
        ad_memory.PersonalMemorySource(manager=CountingMemoryManager()),
        ad_objectives.ObjectivesSource(),
        ad_projects.ProjectMemorySource(),
        ad_documents.DocumentSource(manager=FakeRag([a_hit(0.9)])),
        ad_experts.ExpertSource(),
        ad_sessions.SessionSource(),
        ad_provenance.ProvenanceSource(),
        ad_files.FileSource(),
    ]
    req = make_request(owner="alice", workspace=workspace, project_id="p1",
                       session_id="s1", query="pathlib",
                       refs=("expert:tolkien", "file:src/app.py"))
    return sources, req


async def test_every_source_ref_carries_a_documented_prefix(every_source):
    sources, req = every_source
    results = await C.gather(sources, req)
    produced = [c for r in results for c in r.candidates]

    assert produced, "the fixture must exercise every adapter"
    for candidate in produced:
        assert candidate.source_ref.startswith(DOCUMENTED_PREFIXES), candidate.source_ref

    # And every adapter really did contribute, so the assertion above is not
    # passing over an empty list for eight of the nine sources.
    prefixes = {c.source_ref.split(":", 1)[0] + ":" for c in produced}
    assert prefixes == set(DOCUMENTED_PREFIXES)


async def test_every_candidate_round_trips_through_the_contract(every_source):
    sources, req = every_source
    results = await C.gather(sources, req)
    produced = [c for r in results for c in r.candidates]
    assert produced

    for candidate in produced:
        restored = ContextCandidate.parse(candidate.to_dict())
        assert restored.source_ref == candidate.source_ref
        assert restored.section == candidate.section
        assert restored.trust_class == candidate.trust_class
        assert restored.authority == candidate.authority
        assert restored.lanes == candidate.lanes


async def test_no_source_ever_reports_another_owners_rows(every_source):
    sources, req = every_source
    results = await C.gather(sources, req)
    for result in results:
        for candidate in result.candidates:
            assert candidate.owner in ("", "alice"), candidate.source_ref
