"""src/council — the twelve modules WIRED TOGETHER, not each against its doubles.

Every module in this package has its own suite and every one of them is green.
That is exactly the state in which a subsystem gets written, tested and shipped
without ever having run: each file passes against the stand-ins its author
imagined, and nobody asks whether the shapes those stand-ins imitate are the
shapes the neighbours actually have. This file asks.

So nothing here doubles a council module. The store is a real SQLite file, the
ledger is `CouncilLedger`, the policies are `policies.policy_for`, the scheduler
is `CouncilScheduler`, the events are `CouncilEventStream`, the summary is
`synthesis.build`, and — the point of the whole file — the coordinator is the
real `orchestrator.CouncilOrchestrator`, reached through `service.py`'s seam
rather than constructed here.

Only the two things that leave the process are doubled, because they are the
boundary the package declares (§25): a `ModelInvoker` and a `TaskExecutor`. No
network, no GPU, no model.

The eight questions, one test each:

1. **Does the seam connect?** `service.py` was written before `orchestrator.py`
   existed and reaches it through `use_orchestrator` with a lazy import and an
   `engine_unavailable` fallback. A fallback that quietly wins is a council that
   never runs a turn while every unit test stays green, so the first test here
   asserts the real class is what the seam resolves AND that a turn posted
   through the service produces a participant message in the store.
2. **chat**, end to end, with a mention: only the mentioned model answers.
3. **consult**, end to end: the first round is blind — A's context packet does
   not contain B's answer.
4. **collaborate**: two tasks, two claims on different files, and a third
   participant that cannot claim a file somebody already holds.
5. **An assertion is not evidence**: an executor that says `done` with no
   ChangeSet leaves the task unverified, and the summary says so.
6. **A restart repeats nothing**: `recover()` interrupts the turn in flight,
   releases the claims, and the same idempotency key does not open a second turn.
7. **The room reconstructs from its persistence alone** (§20's last row).
8. **The package imports whole**, all twelve modules, no cycle.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys

import pytest

from src.council import contracts as C
from src.council import events as council_events
from src.council import orchestrator as council_orchestrator
from src.council import persistence as P
from src.council import scheduler as council_scheduler
from src.council import service as service_mod
from src.council import synthesis as council_synthesis
from src.council.ledger import CouncilLedger

SUBMODULES = ("contracts", "persistence", "ledger", "synthesis", "participants",
              "context", "scheduler", "events", "policies", "orchestrator",
              "adapters", "service")


# ── the only doubles in this file: the two boundaries §25 declares ────────

class Invoker:
    """A `ModelInvoker`. Answers from a script, records the packet it was given.

    The packet is kept because test 3 has to look inside it: blindness is not
    something a model can be trusted to respect, it is something the context
    the model receives either contains or does not.
    """

    def __init__(self, script=None) -> None:
        self.calls: list = []
        self.packets: dict = {}
        self._script = script or (lambda pid: f"answer from {pid}")

    async def invoke(self, *, participant, packet, turn, timeout_s):
        pid = str(getattr(participant, "id", ""))
        self.calls.append(pid)
        self.packets[pid] = packet
        return {"content": self._script(pid), "usage": {"total_tokens": 4}}


class Executor:
    """A `TaskExecutor`. Reports a status and nothing else — deliberately.

    Test 5 rests on this: it returns `done` with no evidence, no ChangeSet and
    no proof packet, which is precisely a model asserting an outcome. The room
    must record that the work finished and must NOT record that it was verified.
    """

    def __init__(self, result=None) -> None:
        self.ran: list = []
        self._result = result or {"status": "done", "run_id": "run_double"}

    async def run(self, *, task, participant, session):
        self.ran.append((str(getattr(task, "id", "")),
                         str(getattr(participant, "id", ""))))
        return dict(self._result)


@pytest.fixture
def room(tmp_path):
    """A real store, real streams, real schedulers, and the REAL coordinator.

    `reset_orchestrator()` is the important line: it undoes any
    `use_orchestrator` a neighbouring test installed, so this file cannot
    accidentally pass against somebody else's double.
    """
    P.use_path(str(tmp_path / "council.db"))
    service_mod.reset_service()
    service_mod.reset_orchestrator()
    council_scheduler.reset_schedulers()
    council_events.reset_streams()
    council_orchestrator.reset_packet_builder()
    try:
        yield tmp_path
    finally:
        council_orchestrator.reset_packet_builder()
        service_mod.reset_service()
        service_mod.reset_orchestrator()
        council_scheduler.reset_schedulers()
        council_events.reset_streams()
        P.use_path(None)


def _open(svc, *, policy="chat", seats, owner="alice", workspace="", title="A room"):
    return svc.create(owner=owner, title=title, policy=policy,
                      workspace=str(workspace), participants=seats)


async def _settle(session_id, *, tries=600):
    """Wait for every turn in the room to reach a terminal state."""
    for _ in range(tries):
        rows = P.store().list_turns(session_id)
        if rows and all(t.state in C.TERMINAL_TURN_STATES for t in rows):
            return rows
        await asyncio.sleep(0.01)
    return P.store().list_turns(session_id)


def _committed(session_id):
    """Every message in the room, as the owner's audit read sees it."""
    return list(P.store().list_messages(session_id, audit=True, limit=500))


# ── 1. the seam ───────────────────────────────────────────────────────────

async def test_the_service_seam_reaches_the_real_orchestrator(room):
    """The one thing no module test can see.

    `service._orchestrator_class()` imports `orchestrator.CouncilOrchestrator`
    lazily and answers `None` — leading to `engine_unavailable` — on any
    exception. That is the right behaviour for a build without the module and a
    silent catastrophe for a build with it: a room that reads perfectly and
    never runs a turn, with every unit test green.

    So this asserts three separate things, because each fails differently:
    the class resolves, the orchestrator is BUILT for a real session (the
    constructor call in `_orchestrator_for` has six keyword arguments and any
    one of them could disagree with the signature), and a posted message
    actually produces a participant message in the store.
    """
    assert service_mod._ORCHESTRATOR_FACTORY is None, "a double is still installed"
    resolved = service_mod._orchestrator_class()
    assert resolved is council_orchestrator.CouncilOrchestrator, (
        "service.py's seam does not resolve to the real coordinator; the council "
        f"would answer engine_unavailable for every turn (got {resolved!r})")

    invoker = Invoker()
    svc = service_mod.CouncilService(invoker=invoker)
    session = _open(svc, seats=[{"display_name": "Claude", "model": "model-a",
                                 "roles": ["architect"]}], workspace=room)

    built = svc._orchestrator_for(session)
    assert isinstance(built, council_orchestrator.CouncilOrchestrator), (
        "the seam resolved the class but could not construct it for a real "
        "session; `_orchestrator_for` swallows that and degrades to no engine")

    answer = await svc.post_message(session.id, author_id="alice",
                                    content="hello", owner="alice")
    assert answer["ok"] is True, answer
    assert answer["turn_id"]
    assert answer.get("error") != "engine_unavailable"

    turns = await _settle(session.id)
    assert [t.id for t in turns] == [answer["turn_id"]]
    assert turns[0].state == "completed", turns[0].to_dict()
    assert turns[0].selected_participants == ("p_claude",)

    models = [m for m in _committed(session.id) if m.author_kind == "model"]
    assert models, "the turn advanced and no participant ever spoke"
    assert models[0].author_id == "p_claude"
    assert models[0].content == "answer from p_claude"
    assert invoker.calls == ["p_claude"]

    # and the room said so on its own stream, in the order §8 demands
    names = [e.name for e in council_events.stream_for(session.id).since(0)]
    assert "council_turn_state" in names and "council_message" in names
    assert "council_activity_completed" in names


async def test_the_config_the_ui_reads_agrees_that_the_engine_is_there(room):
    """`/api/council/config` reports `orchestrator_available`, and a UI that
    hides the composer on `False` would hide it for the wrong reason."""
    assert service_mod.CouncilService().config()["orchestrator_available"] is True


# ── 2. chat, end to end, with a mention ───────────────────────────────────

async def test_a_mention_makes_exactly_the_mentioned_participant_answer(room):
    """§20: "`@Claude` solo ejecuta Claude". Routing lives in `policies.py`,
    seat identity in `participants.py`, the round in `orchestrator.py` and the
    entry point in `service.py`; this is the only place all four are asked at
    once."""
    invoker = Invoker()
    svc = service_mod.CouncilService(invoker=invoker)
    session = _open(svc, policy="chat", workspace=room, seats=[
        {"display_name": "Claude", "model": "model-a", "roles": ["architect"]},
        {"display_name": "Codex", "model": "model-b", "roles": ["driver"]}])
    assert session.participants == ("p_claude", "p_codex")

    answer = await svc.post_message(session.id, author_id="alice",
                                    content="@Claude what do you make of this?",
                                    mentions=["p_claude"], owner="alice")
    turns = await _settle(session.id)

    assert answer["ok"] is True
    assert turns[0].state == "completed"
    assert turns[0].selected_participants == ("p_claude",)
    assert invoker.calls == ["p_claude"], "a model nobody asked for was invoked"

    spoke = {m.author_id for m in _committed(session.id) if m.author_kind == "model"}
    assert spoke == {"p_claude"}


async def test_the_room_addressed_as_a_whole_reaches_everybody(room):
    """The other half of the same rule: without a mention, chat still routes."""
    invoker = Invoker()
    svc = service_mod.CouncilService(invoker=invoker)
    session = _open(svc, policy="chat", workspace=room, seats=[
        {"display_name": "Claude", "model": "model-a", "roles": ["architect"]},
        {"display_name": "Codex", "model": "model-b", "roles": ["driver"]}])

    await svc.post_message(session.id, author_id="alice",
                           content="Everyone: what do you think?", owner="alice")
    await _settle(session.id)

    assert set(invoker.calls) == {"p_claude", "p_codex"}


# ── 3. consult: the first round is blind ──────────────────────────────────

async def test_the_first_consult_round_is_blind_in_the_context_packet(room):
    """§4.2 and §20: "responded todos de forma independiente" must not share the
    first round's answers.

    The assertion is deliberately made against the PACKET each participant was
    handed and not against the transcript. A transcript that contains both
    answers is correct — they were both given. What must be true is that
    neither model could see the other's while writing its own, and the only
    place that is decidable is the context each one actually received.

    This runs the REAL `context.build_packet`, because blindness is a property
    of the compiled packet and a stubbed builder would assert nothing.
    """
    secrets = {"p_aye": "AYE_SAYS_MIGRATE_TO_POSTGRES",
               "p_bee": "BEE_SAYS_STAY_ON_SQLITE"}
    invoker = Invoker(script=lambda pid: secrets.get(pid, "?"))
    svc = service_mod.CouncilService(invoker=invoker)
    session = _open(svc, policy="consult", workspace=room, seats=[
        {"display_name": "Aye", "model": "model-a", "roles": ["architect"]},
        {"display_name": "Bee", "model": "model-b", "roles": ["reviewer"]}])

    question = "Answer independently: which database, POSTGRES_OR_SQLITE?"
    await svc.post_message(session.id, author_id="alice", content=question,
                           owner="alice")
    turns = await _settle(session.id)

    assert turns[0].state == "completed"
    assert set(invoker.calls) == {"p_aye", "p_bee"}
    assert turns[0].selected_participants == ("p_aye", "p_bee")

    for pid, other in (("p_aye", "p_bee"), ("p_bee", "p_aye")):
        packet = invoker.packets[pid]
        # Not a degraded stand-in: `build_packet` never raises and falls back to
        # a rules-only packet, which would contain no peer answer for the
        # trivial reason that it contains almost nothing. A test that accepted
        # that would pass on the day the Context Engine broke.
        assert getattr(packet, "degraded", None) is False, (
            f"{pid} got a degraded packet, so this proves nothing about blindness: "
            f"{getattr(packet, 'warnings', ())}")
        rendered = json.dumps(packet.to_dict(), default=str)
        assert "POSTGRES_OR_SQLITE" in rendered, (
            "the packet does not even carry the user's question; it is not the "
            "compiled context this test means to inspect")
        assert secrets[other] not in rendered, (
            f"{pid}'s context packet contained {other}'s first-round answer; "
            "the blind round is not blind")

    # Both answers ARE on the record — blindness is about the packet, not the log.
    said = {m.content for m in _committed(session.id) if m.author_kind == "model"}
    assert said == set(secrets.values())


async def test_the_blind_round_shares_one_snapshot_so_nobody_can_race_ahead(room):
    """A `blind_parallel` round takes ONE transcript snapshot before it starts.
    If it re-read per participant, the second seat could see the first's message
    whenever the first happened to commit sooner — a leak that only appears
    under timing, which is the kind that survives a unit test."""
    invoker = Invoker(script=lambda pid: f"UNIQUE_{pid.upper()}")
    svc = service_mod.CouncilService(invoker=invoker)
    session = _open(svc, policy="consult", workspace=room, seats=[
        {"display_name": "Aye", "model": "model-a", "roles": ["architect"]},
        {"display_name": "Bee", "model": "model-b", "roles": ["reviewer"]}])

    await svc.post_message(session.id, author_id="alice",
                           content="Independently please", owner="alice")
    await _settle(session.id)

    for pid in ("p_aye", "p_bee"):
        packet = invoker.packets[pid]
        assert getattr(packet, "degraded", None) is False
        rendered = json.dumps(packet.to_dict(), default=str)
        assert "UNIQUE_P_AYE" not in rendered and "UNIQUE_P_BEE" not in rendered


# ── 4. collaborate: one resource, one owner ───────────────────────────────

async def test_two_drivers_hold_two_files_and_a_third_cannot_take_either(room):
    """§3.3 and §20: "dos drivers no adquieren el mismo archivo". The claim goes
    through `ledger.request_claim`, which normalises the path and routes it to
    the real `FileLockRegistry` backend — two spellings of the same file are one
    claim, and the loser is told WHO holds it rather than being sent into a
    retry loop against a resource that will never free itself by waiting."""
    invoker, executor = Invoker(), Executor()
    svc = service_mod.CouncilService(invoker=invoker, executor=executor)
    session = _open(svc, policy="collaborate", workspace=room, seats=[
        {"display_name": "Aye", "model": "model-a", "roles": ["driver"]},
        {"display_name": "Bee", "model": "model-b", "roles": ["driver"]},
        {"display_name": "Cee", "model": "model-c", "roles": ["driver"]}])
    ledger = CouncilLedger(session.id, store=P.store(), workspace=str(room))

    first = ledger.add_task(title="the parser", owner_participant_id="p_aye")
    second = ledger.add_task(title="the writer", owner_participant_id="p_bee")
    got_a, claim_a = ledger.request_claim(kind="file", resource="parser.py",
                                          holder_id="p_aye", task_id=first.id)
    got_b, claim_b = ledger.request_claim(kind="file", resource="writer.py",
                                          holder_id="p_bee", task_id=second.id)

    assert got_a and got_b
    assert claim_a.state == "held" and claim_b.state == "held"
    assert claim_a.resource != claim_b.resource

    # the third driver asks for a file that is already held
    got_c, blocker = ledger.request_claim(kind="file", resource="parser.py",
                                          holder_id="p_cee", task_id=second.id)
    assert got_c is False, "two drivers acquired the same file"
    assert blocker.holder_id == "p_aye", "the loser was not told who holds it"
    assert blocker.id == claim_a.id, "a second claim row was created for one resource"
    assert ledger.holder_of("file", "parser.py") == "p_aye"

    # the same file spelled differently is the same claim, not a second one
    again, same = ledger.request_claim(kind="file", resource="./parser.py",
                                       holder_id="p_cee", task_id=second.id)
    assert again is False and same.id == claim_a.id

    # and the turn runs both owners' tasks, each against its own file
    await svc.post_message(session.id, author_id="alice",
                           content="Split the work and get on with it", owner="alice")
    turns = await _settle(session.id)
    assert turns[0].state == "completed", turns[0].to_dict()
    assert sorted(executor.ran) == sorted([(first.id, "p_aye"), (second.id, "p_bee")])

    fresh = CouncilLedger(session.id, store=P.store(), workspace=str(room))
    resources = {t.id: list(t.claimed_resources) for t in fresh.tasks()}
    assert len(resources[first.id]) == 1 and len(resources[second.id]) == 1
    assert resources[first.id] != resources[second.id]


async def test_a_reviewer_in_a_collaborate_room_is_not_given_a_writing_profile(room):
    """§5: revisores read-only por defecto. The profile is computed on the
    server by `participants.effective_profile` and the request cannot widen it."""
    svc = service_mod.CouncilService(invoker=Invoker())
    session = _open(svc, policy="collaborate", workspace=room, seats=[
        {"display_name": "Aye", "model": "model-a", "roles": ["driver"]},
        {"display_name": "Bee", "model": "model-b", "roles": ["critic"],
         "tool_profile": "full_with_gates"}])

    seats = {p.id: p for p in P.store().list_participants(session.id)}
    assert C.can_write(seats["p_aye"].tool_profile)
    assert not C.can_write(seats["p_bee"].tool_profile), (
        "a critic asked for full_with_gates and was given it")


# ── 5. an assertion is not evidence ───────────────────────────────────────

async def test_an_executor_that_says_done_without_a_changeset_does_not_verify(room):
    """§1.7 and §12.2: an outcome is asserted by evidence, never by a returned
    field.

    The executor here reports `done` and nothing else — no ChangeSet, no proof
    packet, no observation. The room must record that the work FINISHED (that is
    a measured fact about the run) and must not record that it was VERIFIED, and
    the close must say which of the two it is holding, in the field a reader
    looks at. A summary that reported `verified` here would be presenting a
    double's word as proof.
    """
    executor = Executor({"status": "done", "run_id": "run_no_evidence"})
    svc = service_mod.CouncilService(invoker=Invoker(), executor=executor)
    session = _open(svc, policy="collaborate", workspace=room, seats=[
        {"display_name": "Aye", "model": "model-a", "roles": ["driver"]},
        {"display_name": "Bee", "model": "model-b", "roles": ["reviewer"]}])
    ledger = CouncilLedger(session.id, store=P.store(), workspace=str(room))
    task = ledger.add_task(title="rewrite the parser", owner_participant_id="p_aye")
    ledger.request_claim(kind="file", resource="parser.py", holder_id="p_aye",
                         task_id=task.id)

    await svc.post_message(session.id, author_id="alice", content="do it", owner="alice")
    await _settle(session.id)

    assert executor.ran == [(task.id, "p_aye")]
    stored = P.store().get_task(task.id)
    assert stored.status == "done", "a measured run outcome was not recorded"
    # `verified` IS a status a task may reach — that is what makes this test
    # worth running. It is reachable only through `adapters.verify_task`, and
    # only when `prove` returns `proved` about a ChangeSet built from observed
    # changes. This run produced no changes, so the status must stay `done`.
    assert "verified" in C.TASK_STATUSES
    assert stored.status != "verified", (
        "the room called a bare `done` verified; an assertion became evidence")
    assert stored.proof_id == "", "a proof id appeared from nowhere"

    fresh = CouncilLedger(session.id, store=P.store(), workspace=str(room))
    summary = council_synthesis.build(fresh, messages=_committed(session.id),
                                      usage={}, stop_reason="completed")

    assert summary.status == "unverified", (
        f"the close claimed {summary.status!r} on an executor's bare word")
    assert summary.verification["sufficient"] is False
    assert summary.verification["verdict"] == "none"
    assert "no proof packet" in summary.verification["note"]
    # and it still reports honestly that the work itself finished
    assert "1 of 1 task(s) finished" in summary.result
    assert summary.result.startswith("unverified:")
    assert [c["task_id"] for c in summary.changes] == [task.id]


async def test_a_blocking_objection_stops_done_from_being_recorded_at_all(room):
    """The stricter half of §12.1: over an open blocking objection the ledger
    refuses `done` outright, so an executor cannot even record the finish."""
    svc = service_mod.CouncilService(invoker=Invoker(), executor=Executor())
    session = _open(svc, policy="collaborate", workspace=room, seats=[
        {"display_name": "Aye", "model": "model-a", "roles": ["driver"]},
        {"display_name": "Bee", "model": "model-b", "roles": ["critic"]}])
    ledger = CouncilLedger(session.id, store=P.store(), workspace=str(room))
    task = ledger.add_task(title="ship it", owner_participant_id="p_aye")
    objection = ledger.object_to(target_kind="task", target_id=task.id,
                                 author_id="p_bee", severity="blocking",
                                 claim="this drops the retry path")

    ok, why = ledger.can_verify(task.id)
    assert ok is False and objection.id in why
    with pytest.raises(C.CouncilError):
        ledger.set_task_status(task.id, "done", actor="p_aye")

    summary = council_synthesis.build(ledger, stop_reason="blocked")
    assert summary.status == "disputed"
    assert [o["id"] for o in summary.open_objections] == [objection.id]


# ── 6. a restart repeats nothing (§15.2, §15.3, §20) ──────────────────────

async def test_a_restart_interrupts_the_turn_frees_the_claims_and_duplicates_nothing(room):
    """Three failures in one, because they only happen together.

    A turn in flight when the process dies must come back `interrupted` (not
    `completed`, and not `failed` — nobody reached a conclusion); the claims it
    held must be released, since recovery is the one moment when "no live
    execution holds this" is knowable for free; and the client that retries with
    the idempotency key it already used must land on the SAME turn rather than
    start a second round of paid work.
    """
    released = asyncio.Event()

    class Slow(Invoker):
        async def invoke(self, *, participant, packet, turn, timeout_s):
            self.calls.append(str(getattr(participant, "id", "")))
            await released.wait()
            return {"content": "too late"}

    invoker = Slow()
    svc = service_mod.CouncilService(invoker=invoker)
    session = _open(svc, policy="chat", workspace=room, seats=[
        {"display_name": "Aye", "model": "model-a", "roles": ["architect"]}])
    ledger = CouncilLedger(session.id, store=P.store(), workspace=str(room))
    task = ledger.add_task(title="in progress", owner_participant_id="p_aye")
    held, claim = ledger.request_claim(kind="file", resource="half_written.py",
                                       holder_id="p_aye", task_id=task.id)
    assert held and claim.state == "held"

    first = await svc.post_message(session.id, author_id="alice", content="go",
                                   idempotency_key="the-same-key", owner="alice")
    assert first["ok"] is True
    for _ in range(400):                       # let the turn actually start
        if invoker.calls:
            break
        await asyncio.sleep(0.01)
    in_flight = P.store().get_turn(first["turn_id"])
    assert in_flight.state not in C.TERMINAL_TURN_STATES

    report = svc.recover()

    assert report["turns_interrupted"] == [first["turn_id"]]
    assert report["claims_released"] == [claim.id]
    assert report["effects_repeated"] == [], "recovery re-ran an effect"
    assert P.store().get_turn(first["turn_id"]).state == "interrupted"
    assert P.store().get_claim(claim.id).state == "released"
    assert "recovery" in P.store().get_claim(claim.id).note
    # The task is REPORTED for reconciliation, never guessed at: its outcome
    # belongs to whoever owns its run_id, and a restart inventing one is §25's
    # named failure.
    assert P.store().get_task(task.id).status == task.status

    second = await svc.post_message(session.id, author_id="alice", content="go",
                                    idempotency_key="the-same-key", owner="alice")

    assert second["turn_id"] == first["turn_id"], "the retry opened a second turn"
    assert len(P.store().list_turns(session.id)) == 1
    users = [m for m in _committed(session.id) if m.author_kind == "user"]
    assert len(users) == 1, "the retry committed a second user message"

    released.set()
    await asyncio.sleep(0.05)
    # The late answer changes nothing: the turn stays interrupted.
    assert P.store().get_turn(first["turn_id"]).state == "interrupted"


async def test_recovery_is_safe_to_run_twice(room):
    """A second pass finds nothing and says so — a start-up path that only
    worked once would fail exactly when a restart loops."""
    svc = service_mod.CouncilService(invoker=Invoker())
    _open(svc, workspace=room, seats=[{"display_name": "Aye", "model": "model-a"}])

    first = svc.recover()
    second = svc.recover()

    assert second["counts"]["turns_interrupted"] == 0
    assert second["counts"]["claims_released"] == 0
    assert second["effects_repeated"] == [] == first["effects_repeated"]


# ── 7. the room reconstructs from its persistence alone ───────────────────

async def test_a_whole_room_is_rebuilt_from_the_database_and_nothing_else(room):
    """§20's last row, and §23's last bullet: "cualquier consumidor pueda
    reconstruir el estado enteramente desde API + eventos".

    The second `CouncilStore` is a genuinely separate object opened on the same
    file — no shared cache, no shared ledger, no shared scheduler. What it can
    read is what actually survived a restart.
    """
    invoker = Invoker()
    svc = service_mod.CouncilService(invoker=invoker)
    session = _open(svc, policy="collaborate", workspace=room, title="The OAuth room",
                    seats=[{"display_name": "Claude", "model": "model-a",
                            "roles": ["architect"]},
                           {"display_name": "Codex", "model": "model-b",
                            "roles": ["driver"]}])
    await svc.post_message(session.id, author_id="alice",
                           content="Decide how we do OAuth", owner="alice")
    await _settle(session.id)

    ledger = CouncilLedger(session.id, store=P.store(), workspace=str(room))
    task = ledger.add_task(title="write the callback", owner_participant_id="p_codex")
    ledger.request_claim(kind="file", resource="callback.py", holder_id="p_codex",
                         task_id=task.id)
    objection = ledger.object_to(target_kind="task", target_id=task.id,
                                 author_id="p_claude", severity="concern",
                                 claim="the state parameter is unchecked")
    decision = ledger.decide(question="which OAuth flow?", chosen="authorization code + PKCE",
                             supporters=["p_claude", "p_codex"],
                             rationale=["no client secret on the device"])

    expected_messages = [(m.author_id, m.content) for m in _committed(session.id)]
    expected_turns = [(t.id, t.state) for t in P.store().list_turns(session.id)]
    assert expected_messages and expected_turns

    # A brand-new store on the same file. Nothing in memory is reused.
    revived = P.CouncilStore(path=P.db_path())

    reborn = revived.get_session(session.id, owner="alice")
    assert reborn is not None
    assert (reborn.id, reborn.title, reborn.policy) == (session.id, "The OAuth room",
                                                        "collaborate")
    assert reborn.participants == session.participants
    assert [p.id for p in revived.list_participants(session.id)] == list(session.participants)
    assert [(m.author_id, m.content)
            for m in revived.list_messages(session.id, audit=True, limit=500)] == expected_messages
    assert [(t.id, t.state) for t in revived.list_turns(session.id)] == expected_turns
    assert [(t.id, t.title) for t in revived.list_tasks(session.id)] == [(task.id,
                                                                         "write the callback")]
    assert [o.id for o in revived.list_objections(session.id)] == [objection.id]
    assert [(d.id, d.chosen) for d in revived.list_decisions(session.id)] == [
        (decision.id, "authorization code + PKCE")]
    assert [c.resource for c in revived.list_claims(session.id)]

    # and a ledger built on the revived store gives the same close
    rebuilt = CouncilLedger(session.id, store=revived, workspace=str(room))
    assert [t.id for t in rebuilt.tasks()] == [task.id]
    assert [d.id for d in rebuilt.decisions()] == [decision.id]
    assert [o.id for o in rebuilt.open_objections()] == [objection.id]
    summary = council_synthesis.build(rebuilt, stop_reason="completed")
    assert summary.status == "disputed"          # the concern is still open
    assert [d["chosen"] for d in summary.decisions] == ["authorization code + PKCE"]

    # a stranger reading the same file still sees nothing
    assert revived.get_session(session.id, owner="bob") is None
    assert revived.list_sessions(owner="bob") == []


# ── 8. the package imports whole ──────────────────────────────────────────

def test_the_package_and_all_twelve_modules_import():
    """Not ceremony. Every module here imports several of the others, three of
    them import lazily INSIDE a function to avoid a cycle, and a package that
    only imports in the order one test happened to use is a package that fails
    on the first fresh process."""
    package = importlib.import_module("src.council")
    assert package.__name__ == "src.council"

    for name in SUBMODULES:
        module = importlib.import_module(f"src.council.{name}")
        assert module.__name__ == f"src.council.{name}"
        assert sys.modules[f"src.council.{name}"] is module


def test_every_module_imports_alone_in_a_fresh_interpreter_state():
    """Import each module FIRST, with the package's namespace dropped in
    between. A cycle that only resolves because a neighbour was imported
    earlier is exactly the bug that survives a suite run in one order and
    breaks the process that imports the service first."""
    import subprocess

    for name in SUBMODULES:
        finished = subprocess.run(
            [sys.executable, "-c", f"import src.council.{name} as m; print(m.__name__)"],
            capture_output=True, text=True, timeout=180)
        assert finished.returncode == 0, (
            f"src.council.{name} cannot be imported first:\n{finished.stderr[-2000:]}")
        assert name in finished.stdout


def test_the_facade_exports_what_the_routes_are_written_against():
    """`routes/council_routes.py` is written against exactly these names. A
    rename here is a broken transport, and it should fail in this file rather
    than in a 500 at runtime."""
    for name in ("COMMANDS", "ERRORS", "CouncilService", "service",
                 "reset_service", "use_orchestrator", "reset_orchestrator"):
        assert hasattr(service_mod, name), f"service.py no longer exports {name}"
    assert service_mod.COMMANDS == (
        "pause", "resume", "cancel_turn", "stop_participant", "steer",
        "assign_role", "handoff_task", "request_synthesis")
    assert "not_found" in service_mod.ERRORS
    assert "engine_unavailable" in service_mod.ERRORS

    stream = council_events.stream_for("council_facade_probe")
    for name in ("publish", "since", "wait", "last_seq", "closed"):
        assert hasattr(stream, name), f"CouncilEventStream no longer has {name}"
    council_events.reset_streams()
