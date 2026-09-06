"""Tests for src/council/ledger.py — the rules, not the plumbing.

Every test here is one of the failures the plan's test matrix (section 20)
names: two drivers on one file, a path spelled a second way, a handoff while a
tool is still writing, a cancel that frees a file somebody is in the middle of,
a blocking objection that gets rounded down to "verified", an objection
attached to nothing, and a decision edited after the fact.

The vocabulary is the contracts module's, and it is not the plan's sketch: a
task is `claimed` rather than `assigned`, there is no `verified` task status
(so the plan-12.1 veto attaches to `done`, the status that makes the same
assertion), an objection carries `target_kind`/`evidence_refs`, and every
timestamp is an ISO-8601 string. These tests are written against what
`src/council/contracts.py` actually declares.
"""
from __future__ import annotations

import dataclasses
import os

import pytest

from src.council import synthesis
from src.council.contracts import CouncilError
from src.council.ledger import (
    ACTIVE_CLAIM_STATES,
    CouncilLedger,
    FileLockRegistryBackend,
    MemoryClaims,
    RoutedClaims,
    normalize_resource,
)


class _NoStore:
    """A store that implements nothing.

    Passed explicitly so these tests write through to nowhere: the moment
    `src/council/persistence.py` lands, a default store would otherwise appear
    under them and the unit tests would start touching real storage.
    """


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("# a\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("# b\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ledger(workspace):
    return CouncilLedger("council_test", store=_NoStore(), workspace=str(workspace))


# --- claims: one resource, one owner ---------------------------------------

def test_two_drivers_do_not_acquire_the_same_file(ledger):
    ok, first = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")
    assert ok is True

    ok2, existing = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_codex")

    assert ok2 is False
    # The loser is handed the claim that already exists, not a bare False: a
    # model told only "no" retries against a resource waiting will not free.
    assert existing.id == first.id
    assert existing.holder_id == "p_claude"
    assert existing.state in ACTIVE_CLAIM_STATES
    assert ledger.holder_of("file", "src/a.py") == "p_claude"


def test_the_same_holder_asking_twice_gets_one_claim(ledger):
    ok, first = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")
    ok2, again = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")

    assert (ok, ok2) == (True, True)
    assert again.id == first.id
    assert len(ledger.claims_of("p_claude")) == 1


def test_equivalent_paths_resolve_to_the_same_claim(ledger, workspace):
    """Plan section 20: "rutas equivalentes/relativas resuelven al mismo claim".

    A lock a second spelling walks around is not a lock. The backslash form is
    only a separator where the OS says it is, so it is asserted there; the
    other spellings are equivalent everywhere.
    """
    spellings = ["./src/../src/a.py", os.path.join(str(workspace), "src", "a.py"),
                 "src//a.py"]
    if os.sep == "\\":
        spellings.append("src\\a.py")

    ok, first = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")
    assert ok is True

    for spelling in spellings:
        assert normalize_resource("file", spelling, workspace=str(workspace)) == \
            normalize_resource("file", "src/a.py", workspace=str(workspace)), spelling
        refused, existing = ledger.request_claim(kind="file", resource=spelling, holder_id="p_codex")
        assert refused is False, spelling
        assert existing.id == first.id, spelling
        assert ledger.holder_of("file", spelling) == "p_claude", spelling


def test_a_kind_is_part_of_the_resource_identity(ledger):
    ok, _ = ledger.request_claim(kind="artifact", resource="report", holder_id="p_claude")
    other, _ = ledger.request_claim(kind="document", resource="report", holder_id="p_codex")

    assert (ok, other) == (True, True)


# --- claims: one lock table, not two ---------------------------------------

def _registry_or_skip(ledger):
    registry = ledger.file_lock_registry
    if registry is None:
        pytest.skip("FileLockRegistry could not be imported in this environment")
    return registry


def test_file_claims_live_in_the_real_file_lock_registry(ledger):
    """The council must not own a second table of file owners.

    The write gate asks `FileLockRegistry` before every write; a claim the
    registry does not know about would not stop anything.
    """
    from src.agent_tools.subagent_tools import FileLockRegistry

    registry = _registry_or_skip(ledger)
    assert isinstance(registry, FileLockRegistry)

    ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")

    assert registry.blocked_by("some-other-worker", ["src/a.py"]) == "p_claude"


def test_a_delegated_worker_holding_the_file_blocks_a_council_claim(ledger):
    registry = _registry_or_skip(ledger)
    assert registry.claim("sa1-9f3c", ["src/a.py"]) == []      # a worker takes it first

    ok, existing = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_codex")

    assert ok is False
    assert existing.holder_id == "sa1-9f3c"


def test_release_gives_back_one_resource_and_keeps_the_rest(ledger):
    """`FileLockRegistry.release()` is worker-scoped; a handoff moves ONE
    resource. This is the behaviour the adapter exists for."""
    _, claim_a = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")
    ledger.request_claim(kind="file", resource="src/b.py", holder_id="p_claude")

    assert ledger.release_claim(claim_a.id, actor="p_claude") is True
    assert ledger.release_claim(claim_a.id, actor="p_claude") is False

    assert ledger.holder_of("file", "src/a.py") is None
    assert ledger.holder_of("file", "src/b.py") == "p_claude"
    registry = ledger.file_lock_registry
    if registry is not None:
        assert len(registry.owned_by("p_claude")) == 1


def test_a_lease_expires_and_frees_the_resource(ledger):
    import time

    _, claim = ledger.request_claim(kind="file", resource="src/a.py",
                                    holder_id="p_claude", ttl_seconds=60)

    assert ledger.expire_claims(now=time.time()) == 0
    assert ledger.expire_claims(now=time.time() + 61) == 1
    assert ledger.holder_of("file", "src/a.py") is None
    assert ledger.claims_of("p_claude") == []
    assert claim.state == "held"        # the record is replaced, never mutated


# --- claims: the handoff protocol (plan 11.3) ------------------------------

def test_handoff_is_refused_while_a_mutating_tool_is_active(ledger):
    _, claim = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")

    result = ledger.handoff(claim.id, to="p_codex", actor="coordinator", mutating_active=True)

    assert result["ok"] is False
    assert "mutating tool" in result["reason"]
    assert result["changed"] is False
    # The owner did not change, in the ledger or in the lock table.
    assert ledger.holder_of("file", "src/a.py") == "p_claude"
    assert [c.state for c in ledger.claims_of("p_claude")] == ["held"]
    assert ledger.claims_of("p_codex") == []


def test_handoff_moves_the_claim_and_emits_the_event(ledger):
    _, claim = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")

    result = ledger.handoff(claim.id, to="p_codex", actor="coordinator")

    assert result["ok"] is True and result["changed"] is True
    assert result["event"]["type"] == "council_claim_transferred"
    assert ledger.holder_of("file", "src/a.py") == "p_codex"
    assert ledger.claims_of("p_claude") == []
    registry = ledger.file_lock_registry
    if registry is not None:
        assert registry.blocked_by("someone", ["src/a.py"]) == "p_codex"


def test_handoff_of_an_unknown_claim_answers_instead_of_raising(ledger):
    result = ledger.handoff("claim_nope", to="p_codex", actor="coordinator")

    assert result["ok"] is False
    assert "claim_nope" in result["reason"]


def test_cancel_does_not_release_a_resource_in_use(ledger):
    _, busy = ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_claude")
    _, idle = ledger.request_claim(kind="file", resource="src/b.py", holder_id="p_claude")

    # Spelled differently on purpose: "in use" must survive normalisation too.
    out = ledger.release_on_cancel("p_claude", mutating_active_resources=["./src/../src/a.py"])

    assert [entry["claim_id"] for entry in out["kept"]] == [busy.id]
    assert [entry["claim_id"] for entry in out["released"]] == [idle.id]
    assert "still running" in out["kept"][0]["reason"]
    assert ledger.holder_of("file", "src/a.py") == "p_claude"
    assert ledger.holder_of("file", "src/b.py") is None


# --- agenda ---------------------------------------------------------------

def _ids(tasks):
    return {task.id for task in tasks}


def test_ready_tasks_respects_depends_on(ledger):
    """A dependency that has not finished WELL does not make its dependent
    ready -- and a dependency that failed never will."""
    parser = ledger.add_task(title="write the parser")
    tests = ledger.add_task(title="write the tests", depends_on=[parser.id])
    docs = ledger.add_task(title="document it", depends_on=[tests.id])
    ghost = ledger.add_task(title="depends on nothing that exists", depends_on=["task_missing"])

    assert _ids(ledger.ready_tasks()) == {parser.id}
    assert _ids(ledger.blocked_tasks()) == {tests.id, docs.id, ghost.id}

    ledger.set_task_status(parser.id, "done", actor="p_codex")
    assert _ids(ledger.ready_tasks()) == {tests.id}

    ledger.set_task_status(tests.id, "failed", actor="p_codex")
    assert _ids(ledger.ready_tasks()) == set()
    assert docs.id in _ids(ledger.blocked_tasks())


def test_a_task_marked_blocked_is_not_ready(ledger):
    task = ledger.add_task(title="waiting on the user")
    assert task.id in _ids(ledger.ready_tasks())

    ledger.set_task_status(task.id, "blocked", actor="coordinator")

    assert task.id in _ids(ledger.blocked_tasks())
    assert task.id not in _ids(ledger.ready_tasks())


def test_assign_gives_an_owner_without_mutating_the_old_record(ledger):
    task = ledger.add_task(title="implement the callback")

    updated = ledger.assign(task.id, "p_codex", actor="coordinator")

    assert updated.owner_participant_id == "p_codex"
    assert updated.status == "claimed"       # the contracts' name for "assigned"
    assert task.status == "pending"          # records are replaced, not edited


def test_an_unknown_task_status_is_refused(ledger):
    task = ledger.add_task(title="x")
    with pytest.raises(CouncilError):
        ledger.set_task_status(task.id, "almost-done", actor="p_codex")


# --- objections -----------------------------------------------------------

def test_an_objection_must_point_at_something_that_exists(ledger):
    with pytest.raises(CouncilError) as error:
        ledger.object_to(target="task", target_id="task_ghost", author_id="p_claude",
                         claim="this is wrong")

    assert "task_ghost" in str(error.value)


def test_a_registered_message_can_be_objected_to(ledger):
    ledger.track_target("msg_17")

    objection = ledger.object_to(target="message", target_id="msg_17", author_id="p_claude",
                                 claim="the benchmark quoted here does not exist")

    assert objection.status == "open"
    assert ledger.open_objections(target_id="msg_17")


def test_a_blocking_objection_prevents_verified_for_its_target_only(ledger):
    """Plan 12.1. `done` is the status that asserts the work finished well in
    the contracts' vocabulary, so that is where the veto lands."""
    blocked_task = ledger.add_task(title="OAuth callback")
    other_task = ledger.add_task(title="README")
    objection = ledger.object_to(target="task", target_id=blocked_task.id, severity="blocking",
                                 author_id="p_claude",
                                 claim="the callback never validates the state parameter")

    ok, why = ledger.can_verify(blocked_task.id)
    assert ok is False
    assert objection.id in why and "state parameter" in why
    assert ledger.blocking_open(target_id=blocked_task.id) is True

    with pytest.raises(CouncilError) as error:
        ledger.set_task_status(blocked_task.id, "done", actor="p_codex")
    assert objection.id in str(error.value)
    # Work can still move; only the claim that it finished well is refused.
    assert ledger.set_task_status(blocked_task.id, "review", actor="p_codex").status == "review"

    # The rest of the agenda keeps moving (plan 12.1).
    assert ledger.can_verify(other_task.id)[0] is True
    assert ledger.set_task_status(other_task.id, "done", actor="p_codex").status == "done"

    # Accepting an objection is agreeing with it, not doing the work.
    ledger.resolve_objection(objection.id, "accepted", actor="p_codex", note="fair point")
    assert ledger.can_verify(blocked_task.id)[0] is False

    ledger.resolve_objection(objection.id, "resolved", actor="p_claude", note="validated now")
    assert ledger.can_verify(blocked_task.id)[0] is True
    assert ledger.set_task_status(blocked_task.id, "done", actor="p_codex").status == "done"


def test_resolving_an_objection_needs_an_outcome(ledger):
    task = ledger.add_task(title="x")
    objection = ledger.object_to(target="task", target_id=task.id, author_id="p_claude",
                                 claim="unclear")
    with pytest.raises(CouncilError):
        ledger.resolve_objection(objection.id, "open", actor="p_claude")


# --- decisions ------------------------------------------------------------

def test_a_decision_is_superseded_never_edited(ledger):
    first = ledger.decide(question="where is the OAuth state validated?", chosen="in the client",
                          supporters=["p_codex"], dissenters=["p_claude"])

    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        first.chosen = "in the backend"
    assert [name for name in dir(ledger) if name.startswith(("update_decision", "edit_decision"))] == []

    second = ledger.supersede_decision(
        first.id, chosen="in the backend, before the token exchange",
        rationale=["a client-side check is advisory"], supporters=["p_codex", "p_claude"])

    assert second.supersedes == first.id
    assert second.question == first.question          # the question is carried over
    assert second.dissenters == ()                    # the dissent is NOT inherited
    assert [d.id for d in ledger.decisions()] == [second.id]
    stored = {d.id: d for d in ledger.decisions(include_superseded=True)}
    assert set(stored) == {first.id, second.id}
    assert stored[first.id].status == "superseded"
    assert stored[first.id].chosen == "in the client"  # the old text survives

    with pytest.raises(CouncilError) as error:
        ledger.supersede_decision(first.id, chosen="something else again")
    assert second.id in str(error.value)


def test_a_decision_records_the_question_it_answers(ledger):
    with pytest.raises(CouncilError):
        ledger.decide(chosen="do it this way")


# --- the seam with synthesis ----------------------------------------------

def test_a_session_with_dissent_says_so_in_its_summary(ledger, workspace):
    """Plan section 20: "el disenso aparece en la síntesis"."""
    task = ledger.add_task(title="OAuth callback", owner_participant_id="p_codex")
    ledger.request_claim(kind="file", resource="src/a.py", holder_id="p_codex", task_id=task.id)
    ledger.set_task_status(task.id, "done", actor="p_codex")
    ledger.decide(question="where is the OAuth state validated?", chosen="in the backend",
                  supporters=["p_codex"], dissenters=["p_claude"])
    ledger.object_to(target="task", target_id=task.id, severity="concern", author_id="p_claude",
                     claim="no test covers an invalid state")

    summary = synthesis.build(
        ledger,
        messages=[{"id": "m1", "author_id": "p_codex", "message_type": "proposal", "content": "done"},
                  {"id": "m2", "author_id": "p_claude", "message_type": "critique", "content": "not yet"}],
        usage={"total_tokens": 4211, "calls": 6},
        stop_reason="max_rounds",
    )

    assert summary.status == "disputed"
    assert summary.decisions[0]["dissenters"] == ["p_claude"]
    assert summary.changes[0]["resources"] == [
        normalize_resource("file", "src/a.py", workspace=str(workspace))]

    text = synthesis.render(summary)
    assert "no test covers an invalid state" in text
    assert "Dissent: p_claude" in text
    assert "max_rounds" in text


def test_a_snapshot_is_the_only_thing_synthesis_needs(ledger):
    ledger.add_task(title="x")
    snapshot = ledger.snapshot()

    assert snapshot["session_id"] == "council_test"
    assert set(snapshot) >= {"tasks", "claims", "objections", "decisions", "open_objections",
                             "ready_task_ids", "blocked_task_ids", "counts", "persistence_errors"}


def test_the_default_backend_routes_paths_to_the_registry_and_the_rest_apart():
    backend = RoutedClaims(MemoryClaims(), MemoryClaims())
    assert backend.acquire("file:/tmp/x", "p_a") is True
    assert backend.acquire("artifact:/tmp/x", "p_b") is True     # a different table
    assert backend.holder("file:/tmp/x") == "p_a"
    assert backend.release("file:/tmp/x", "p_b") is False
    assert backend.release("file:/tmp/x", "p_a") is True


class RecordingStore:
    """A store with `CouncilStore`'s real method signatures.

    `update_<kind>(id, patch)` -- not `update_<kind>(object)` -- and no
    `update_decision` at all. The ledger writing the wrong shape here is a
    silent failure in production (the write is refused, the session keeps its
    own truth and nothing is durable), which is exactly why it is asserted.
    """

    def __init__(self):
        self.calls = []

    def _record(self, *call):
        self.calls.append(call)

    def create_task(self, task): self._record("create_task", task); return task
    def update_task(self, task_id, patch): self._record("update_task", task_id, patch)
    def create_claim(self, claim): self._record("create_claim", claim); return claim
    def update_claim(self, claim_id, patch): self._record("update_claim", claim_id, patch)
    def create_objection(self, obj): self._record("create_objection", obj); return obj
    def update_objection(self, obj_id, patch): self._record("update_objection", obj_id, patch)
    def create_decision(self, decision): self._record("create_decision", decision); return decision
    def supersede_decision(self, previous_id, replacement):
        self._record("supersede_decision", previous_id, replacement)
        return previous_id, replacement

    def list_tasks(self, session_id, **kw): return []
    def list_claims(self, session_id, **kw): return []
    def list_objections(self, session_id, **kw): return []
    def list_decisions(self, session_id, **kw): return []


def test_every_change_is_written_through_in_the_shape_the_store_accepts(workspace):
    from src.council.persistence import _CLAIM_MUTABLE, _OBJECTION_MUTABLE, _TASK_MUTABLE

    store = RecordingStore()
    ledger = CouncilLedger("council_store", store=store, workspace=str(workspace))

    task = ledger.add_task(title="OAuth callback")
    ledger.assign(task.id, "p_codex", actor="coordinator")
    ledger.set_task_status(task.id, "running", actor="p_codex")
    _, claim = ledger.request_claim(kind="file", resource="src/a.py",
                                    holder_id="p_codex", task_id=task.id)
    ledger.handoff(claim.id, to="p_claude", actor="coordinator")
    ledger.release_claim(claim.id, actor="p_claude")
    objection = ledger.object_to(target="task", target_id=task.id, author_id="p_claude",
                                 claim="the callback is not covered by a test")
    ledger.resolve_objection(objection.id, "resolved", actor="p_claude", note="covered now")
    first = ledger.decide(question="where is the state validated?", chosen="in the client")
    ledger.supersede_decision(first.id, chosen="in the backend")

    made = {call[0] for call in store.calls}
    assert made == {"create_task", "update_task", "create_claim", "update_claim",
                    "create_objection", "update_objection", "create_decision",
                    "supersede_decision"}
    mutable = {"task": set(_TASK_MUTABLE), "claim": set(_CLAIM_MUTABLE),
               "objection": set(_OBJECTION_MUTABLE)}
    for name, ident, patch in [c for c in store.calls if c[0].startswith("update_")]:
        assert ident and isinstance(ident, str), name
        assert set(patch) <= mutable[name[len("update_"):]], (name, sorted(patch))
    assert ledger.snapshot()["persistence_errors"] == 0


def test_the_registry_adapter_answers_the_three_questions(workspace):
    from src.agent_tools.subagent_tools import FileLockRegistry

    backend = FileLockRegistryBackend(FileLockRegistry(str(workspace)))
    path = os.path.join(str(workspace), "src", "a.py")

    assert backend.acquire(path, "p_a") is True
    assert backend.acquire(path, "p_b") is False
    assert backend.holder(path) == "p_a"
    assert backend.release(path, "p_b") is False
    assert backend.release(path, "p_a") is True
    assert backend.holder(path) == ""
