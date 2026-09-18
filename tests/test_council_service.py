"""Council service: whose room it is, and who is allowed to say so.

The plan's §20 rows this file covers, one test each:

* another owner's room is not visible — not readable, not listable, not
  countable, and the refusal carries no title (§16, §20);
* the owner arrives from the authenticated caller and is not a patchable
  field;
* `post_message` answers with a `turn_id` and the turn runs in the background
  (§13), so a client that reconnects finds the turn it started;
* a command validates owner, revision and arguments and fails with a STABLE
  token rather than with the text of an exception (§13);
* `create` and `assign_role` compute the tool profile on the server, and
  neither can widen one (§11.1, §16, §25);
* `recover()` says what it reconciled and repeats no effect (§15.2);
* no read path raises, even with the database on fire.

`orchestrator.py` and `policies.py` are being written next to this file, so the
coordinator here is a double installed through `service.use_orchestrator` — the
seam that exists exactly so this service can be tested without one.
"""

from __future__ import annotations

import asyncio

import pytest

from src.council import contracts as C
from src.council import events as council_events
from src.council import persistence as P
from src.council import scheduler as council_scheduler
from src.council import service as service_mod
from src.council.contracts import CouncilError


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    """Own database, own streams, own schedulers, no real coordinator."""
    P.use_path(str(tmp_path / "council.db"))
    service_mod.reset_service()
    service_mod.reset_orchestrator()
    council_scheduler.reset_schedulers()
    council_events.reset_streams()
    try:
        yield
    finally:
        service_mod.reset_orchestrator()
        service_mod.reset_service()
        council_scheduler.reset_schedulers()
        council_events.reset_streams()
        P.use_path(None)


# --- doubles ---------------------------------------------------------------

class _Orchestrator:
    """The coordinator's command surface, recorded rather than executed."""

    def __init__(self, session, **kw) -> None:
        self.session = session
        self.kw = kw
        self.submitted = []
        self.turns = []
        self.commands = []
        self.gate = asyncio.Event()
        self.finished = asyncio.Event()

    async def submit(self, *, author_id, content, mentions=(), idempotency_key=""):
        self.submitted.append({"author_id": author_id, "content": content,
                               "mentions": tuple(mentions), "key": idempotency_key})
        return "turn_abc"

    async def run_turn(self, turn_id):
        self.turns.append(turn_id)
        await self.gate.wait()
        self.finished.set()
        return {"turn_id": turn_id, "state": "completed"}

    async def pause(self):
        self.commands.append(("pause", {}))
        return {"status": "paused"}

    async def resume(self):
        self.commands.append(("resume", {}))
        return {"status": "active"}

    async def request_synthesis(self):
        self.commands.append(("request_synthesis", {}))
        return {"queued": True}

    async def cancel_turn(self, turn_id, *, actor):
        self.commands.append(("cancel_turn", {"turn_id": turn_id, "actor": actor}))
        return {"cancelled": turn_id}

    async def stop_participant(self, *, participant_id, actor):
        self.commands.append(("stop_participant", {"participant_id": participant_id,
                                                   "actor": actor}))
        return {"stopped": participant_id}

    async def steer(self, *, participant_id, message, actor):
        self.commands.append(("steer", {"participant_id": participant_id,
                                        "message": message, "actor": actor}))
        return {"delivered": True}

    async def handoff_task(self, *, task_id, to, actor):
        self.commands.append(("handoff_task", {"task_id": task_id, "to": to, "actor": actor}))
        return {"task_id": task_id, "owner": to}

    def state(self):
        return {"session_id": self.session.id}


class _Coordinators:
    """Installs the double and keeps every instance it built."""

    def __init__(self) -> None:
        self.made = []

    def install(self) -> "_Coordinators":
        service_mod.use_orchestrator(self._factory)
        return self

    def _factory(self, session, **kw):
        made = _Orchestrator(session, **kw)
        self.made.append(made)
        return made

    @property
    def last(self) -> _Orchestrator:
        assert self.made, "no orchestrator was built"
        return self.made[-1]


class _BrokenStore:
    """Every call raises.  The read paths must survive it (rule 6)."""

    def __getattr__(self, name):
        def _boom(*a, **kw):
            raise RuntimeError("the database is gone")

        return _boom


def _svc(**kw) -> service_mod.CouncilService:
    return service_mod.CouncilService(**kw)


def _room(svc, *, owner="alice", policy="chat", participants=None, **over):
    payload = {"owner": owner, "title": "Secret refactor", "policy": policy,
               "workspace": "D:/LocalAI/odysseus",
               "participants": participants if participants is not None else [
                   {"display_name": "Claude", "model": "claude-x", "roles": ["critic"]}]}
    payload.update(over)
    return svc.create(**payload)


# --- §20: another owner's room is not visible, and leaks nothing ----------

def test_a_stranger_sees_nothing_and_is_told_nothing():
    svc = _svc()
    room = _room(svc)

    assert svc.get(room.id, owner="bob") is None
    assert svc.list(owner="bob") == []
    assert svc.messages(room.id, owner="bob") == []
    assert svc.tasks(room.id, owner="bob") == []
    assert svc.decisions(room.id, owner="bob") == []
    assert svc.ledger(room.id, owner="bob") == {}
    assert svc.usage(room.id, owner="bob") == {}
    assert svc.events(room.id, owner="bob") is None
    assert svc.archive(room.id, owner="bob") is False
    # and the owner still has everything
    assert svc.get(room.id, owner="alice").title == "Secret refactor"
    assert [s.id for s in svc.list(owner="alice")] == [room.id]


def test_a_refusal_never_carries_the_title():
    """`not found` and `not yours` are the same answer (§20)."""
    svc = _svc()
    room = _room(svc)

    with pytest.raises(P.NotFound) as failure:
        svc.update(room.id, {"title": "mine now"}, owner="bob")

    assert "Secret refactor" not in str(failure.value)
    assert svc.get(room.id, owner="alice").title == "Secret refactor"


async def test_a_command_in_a_stranger_s_room_is_not_found():
    svc = _svc()
    room = _room(svc)
    _Coordinators().install()

    answer = await svc.command(room.id, "pause", owner="bob")

    assert answer == {"ok": False, "error": "not_found",
                      "detail": "no such council room here", "command": "pause"}


async def test_posting_into_a_stranger_s_room_is_not_found():
    svc = _svc()
    room = _room(svc)
    _Coordinators().install()

    answer = await svc.post_message(room.id, author_id="bob", content="hi", owner="bob")

    assert answer["ok"] is False and answer["error"] == "not_found"


# --- the owner comes from the caller -------------------------------------

def test_a_room_has_no_anonymous_owner():
    with pytest.raises(CouncilError) as failure:
        _svc().create(owner="", title="x", policy="chat", participants=[])

    assert "authenticated" in str(failure.value)


def test_the_owner_is_not_a_patchable_field():
    svc = _svc()
    room = _room(svc)

    with pytest.raises(CouncilError) as failure:
        svc.update(room.id, {"owner": "bob"}, owner="alice")

    assert "owner" in str(failure.value)
    assert svc.get(room.id, owner="alice").owner == "alice"
    assert svc.get(room.id, owner="bob") is None


def test_a_stale_revision_loses_and_is_told_which_one_won():
    svc = _svc()
    room = _room(svc)
    svc.update(room.id, {"title": "first"}, owner="alice", expected_revision=room.revision)

    with pytest.raises(P.RevisionConflict) as failure:
        svc.update(room.id, {"title": "second"}, owner="alice",
                   expected_revision=room.revision)

    assert failure.value.revision == room.revision + 1


# --- §11.1 / §25: the profile is computed here, and only ever narrows -----

def test_a_seat_cannot_ask_its_way_into_more_tools_than_the_room_allows():
    svc = _svc()
    spec = {"display_name": "Codex", "model": "codex-x", "roles": ["driver"],
            "tool_profile": "full_with_gates"}

    talking = _room(svc, policy="chat", participants=[spec])
    working = _room(svc, policy="collaborate", participants=[spec])

    store = P.store()
    seated_in_chat = store.list_participants(talking.id)[0]
    seated_at_work = store.list_participants(working.id)[0]
    # chat grants no mutating tools (§4.1); collaborate stops at what the role
    # justifies (§5), never at what the request asked for.
    assert seated_in_chat.tool_profile == "read_only"
    assert seated_at_work.tool_profile == "scoped_write"


def test_a_new_policy_narrows_the_seats_that_are_already_in_the_room():
    svc = _svc()
    room = _room(svc, policy="collaborate",
                 participants=[{"display_name": "Codex", "model": "codex-x",
                                "roles": ["driver"], "tool_profile": "scoped_write"}])
    assert P.store().list_participants(room.id)[0].tool_profile == "scoped_write"

    svc.update(room.id, {"policy": "chat"}, owner="alice")

    assert P.store().list_participants(room.id)[0].tool_profile == "read_only"


def test_the_policy_cannot_change_under_a_running_turn():
    svc = _svc()
    room = _room(svc, policy="collaborate")
    P.store().create_turn(C.CouncilTurn.parse(
        {"id": "turn_live", "session_id": room.id, "state": "running",
         "author_id": "alice", "content": "go"}))

    with pytest.raises(CouncilError) as failure:
        svc.update(room.id, {"policy": "chat"}, owner="alice")

    assert "turn" in str(failure.value)
    assert svc.get(room.id, owner="alice").policy == "collaborate"


# --- §13: post fast, run in the background -------------------------------

async def test_post_message_answers_with_a_turn_id_before_the_turn_finishes():
    svc = _svc()
    room = _room(svc)
    coordinators = _Coordinators().install()

    answer = await svc.post_message(room.id, author_id="alice", content="decide this",
                                    mentions=["p_claude"], idempotency_key="req-42",
                                    owner="alice")

    orchestrator = coordinators.last
    assert answer == {"ok": True, "turn_id": "turn_abc", "session_id": room.id,
                      "status": "running", "stream": "events"}
    assert orchestrator.submitted == [{"author_id": "alice", "content": "decide this",
                                       "mentions": ("p_claude",), "key": "req-42"}]
    # The turn is running, and this call already returned.
    assert orchestrator.finished.is_set() is False
    await asyncio.sleep(0)
    assert orchestrator.turns == ["turn_abc"]
    assert orchestrator.finished.is_set() is False

    orchestrator.gate.set()
    await asyncio.wait_for(orchestrator.finished.wait(), 2)


async def test_a_turn_already_running_is_not_started_twice():
    """§15.3: a retried POST answers with the turn the first one opened."""
    svc = _svc()
    room = _room(svc)
    coordinators = _Coordinators().install()

    first = await svc.post_message(room.id, author_id="alice", content="go",
                                   idempotency_key="req-42", owner="alice")
    await asyncio.sleep(0)
    second = await svc.post_message(room.id, author_id="alice", content="go",
                                    idempotency_key="req-42", owner="alice")

    orchestrator = coordinators.last
    assert first["turn_id"] == second["turn_id"] == "turn_abc"
    assert second["status"] == "already_running"
    assert orchestrator.turns == ["turn_abc"]
    orchestrator.gate.set()
    await asyncio.wait_for(orchestrator.finished.wait(), 2)


async def test_a_build_with_no_coordinator_says_so_instead_of_pretending():
    def _explode(session, **kw):
        raise RuntimeError("no coordinator in this build")

    service_mod.use_orchestrator(_explode)
    svc = _svc()
    room = _room(svc)

    posted = await svc.post_message(room.id, author_id="alice", content="go", owner="alice")
    commanded = await svc.command(room.id, "pause", owner="alice")

    assert posted["error"] == "engine_unavailable"
    assert commanded["error"] == "engine_unavailable"


# --- §13: commands, with stable tokens -----------------------------------

async def test_an_unknown_command_is_refused_naming_the_valid_ones():
    svc = _svc()
    room = _room(svc)
    _Coordinators().install()

    answer = await svc.command(room.id, "self_destruct", owner="alice")

    assert answer["ok"] is False
    assert answer["error"] == "unknown_command"
    assert answer["valid_commands"] == list(service_mod.COMMANDS)


async def test_a_command_against_a_stale_revision_is_refused_with_the_revision():
    svc = _svc()
    room = _room(svc)
    _Coordinators().install()

    answer = await svc.command(room.id, "pause", owner="alice",
                               expected_revision=room.revision + 5)

    assert answer["error"] == "revision_conflict"
    assert answer["revision"] == room.revision


async def test_a_command_missing_its_argument_says_which_one():
    svc = _svc()
    room = _room(svc)
    _Coordinators().install()

    answer = await svc.command(room.id, "cancel_turn", owner="alice")

    assert answer["error"] == "missing_argument"
    assert "turn_id" in answer["detail"]


async def test_every_command_reaches_the_coordinator_with_its_actor():
    svc = _svc()
    room = _room(svc)
    coordinators = _Coordinators().install()

    await svc.command(room.id, "pause", owner="alice", actor="alice")
    await svc.command(room.id, "resume", owner="alice", actor="alice")
    await svc.command(room.id, "cancel_turn", owner="alice", actor="alice", turn_id="turn_1")
    await svc.command(room.id, "stop_participant", owner="alice", actor="alice",
                      participant_id="p_claude")
    await svc.command(room.id, "steer", owner="alice", actor="alice",
                      participant_id="p_claude", message="security only")
    await svc.command(room.id, "handoff_task", owner="alice", actor="alice",
                      task_id="task_1", to="p_codex")
    answer = await svc.command(room.id, "request_synthesis", owner="alice", actor="alice")

    assert [name for name, _ in coordinators.last.commands] == [
        "pause", "resume", "cancel_turn", "stop_participant", "steer",
        "handoff_task", "request_synthesis"]
    assert coordinators.last.commands[4][1] == {"participant_id": "p_claude",
                                                "message": "security only",
                                                "actor": "alice"}
    assert answer == {"ok": True, "command": "request_synthesis", "session_id": room.id,
                      "revision": room.revision, "result": {"queued": True}}


async def test_a_coordinator_that_refuses_answers_with_a_token_not_a_traceback():
    svc = _svc()
    room = _room(svc)
    coordinators = _Coordinators().install()

    async def _refuse():
        raise RuntimeError("the room is already paused")

    coordinators_installed = coordinators
    await svc.command(room.id, "pause", owner="alice")       # builds the coordinator
    coordinators_installed.last.pause = _refuse

    answer = await svc.command(room.id, "pause", owner="alice")

    assert answer["ok"] is False
    assert answer["error"] == "command_failed"
    assert answer["error"] in service_mod.ERRORS


async def test_assign_role_records_the_hat_and_grants_no_tools():
    """§5 and §11.1: a new hat is a record, never a permission."""
    svc = _svc()
    room = _room(svc, policy="chat")
    seat_id = room.participants[0]

    answer = await svc.command(room.id, "assign_role", owner="alice", actor="alice",
                               participant_id=seat_id, role="integrator")

    assert answer["ok"] is True
    seat = P.store().get_participant(room.id, seat_id)
    assert set(seat.roles) == {"critic", "integrator"}
    assert seat.tool_profile == "read_only"


async def test_assign_role_refuses_a_role_nobody_declared():
    svc = _svc()
    room = _room(svc)

    answer = await svc.command(room.id, "assign_role", owner="alice",
                               participant_id=room.participants[0], role="overlord")

    assert answer["error"] == "invalid_argument"
    assert "overlord" in answer["detail"]


# --- the transcript, as each reader may see it (§3.1, §4.2) --------------

def test_the_owner_reads_the_room_and_a_peer_reads_only_what_was_for_it():
    svc = _svc()
    room = _room(svc, participants=[
        {"display_name": "Claude", "model": "claude-x", "roles": ["critic"]},
        {"display_name": "Codex", "model": "codex-x", "roles": ["researcher"]}])
    claude, codex = room.participants
    store = P.store()
    store.append_message(C.CouncilMessage.parse(
        {"session_id": room.id, "author_id": claude, "content": "everyone hears this",
         "visibility": "room"}))
    store.append_message(C.CouncilMessage.parse(
        {"session_id": room.id, "author_id": claude, "content": "blind round answer",
         "visibility": "participant", "audience": [claude]}))

    everything = svc.messages(room.id, owner="alice")
    as_a_peer = svc.messages(room.id, viewer_id=codex, owner="alice")

    assert [m["content"] for m in everything] == ["everyone hears this", "blind round answer"]
    assert [m["content"] for m in as_a_peer] == ["everyone hears this"]


def test_the_event_stream_is_handed_out_only_for_a_visible_room():
    svc = _svc()
    room = _room(svc)

    stream = svc.events(room.id, owner="alice")

    assert stream is not None
    assert stream.session_id == room.id
    assert svc.events(room.id, owner="bob") is None
    assert svc.events("council_nope", owner="alice") is None


# --- rule 6: no read path raises, ever -----------------------------------

def test_every_read_survives_a_database_that_is_gone():
    svc = _svc(store=_BrokenStore())

    assert svc.get("council_1", owner="alice") is None
    assert svc.list(owner="alice") == []
    assert svc.messages("council_1", owner="alice") == []
    assert svc.tasks("council_1", owner="alice") == []
    assert svc.decisions("council_1", owner="alice") == []
    assert svc.ledger("council_1", owner="alice") == {}
    assert svc.usage("council_1", owner="alice") == {}
    assert svc.events("council_1", owner="alice") is None
    assert svc.archive("council_1", owner="alice") is False
    assert isinstance(svc.config(), dict)
    assert "failed" in svc.recover()


# --- §13: the form's own endpoint ----------------------------------------

def test_config_is_what_a_form_needs_to_open_a_room():
    from src.council import adapters

    config = _svc().config()

    assert config["policies"] == list(C.POLICIES)
    assert config["tool_profiles"] == list(C.TOOL_PROFILES)
    assert {row["id"] for row in config["roles"]} == set(C.ROLES)
    assert config["policy_tool_ceilings"]["chat"] == "read_only"
    assert config["default_budgets"] == dict(C.DEFAULT_BUDGETS)
    assert config["commands"] == list(service_mod.COMMANDS)
    assert config["errors"] == list(service_mod.ERRORS)
    assert config["verdicts"] == list(adapters.VERDICTS)
    assert "orchestrator_available" in config


def test_every_error_token_a_command_can_answer_with_is_declared():
    """A token nothing declares is a token no caller can branch on (§13)."""
    assert len(set(service_mod.ERRORS)) == len(service_mod.ERRORS)
    assert "not_found" in service_mod.ERRORS


# --- §15.2: recovery says what it reconciled and repeats nothing ---------

def test_recover_reports_what_it_reconciled_and_repeats_no_effect():
    svc = _svc()
    room = _room(svc)
    svc.update(room.id, {"status": "ready"}, owner="alice")
    svc.update(room.id, {"status": "active"}, owner="alice")
    P.store().create_turn(C.CouncilTurn.parse(
        {"id": "turn_live", "session_id": room.id, "state": "running",
         "author_id": "alice", "content": "go"}))

    report = svc.recover()

    assert report["sessions_interrupted"] == [room.id]
    assert report["turns_interrupted"] == ["turn_live"]
    assert report["effects_repeated"] == []
    assert svc.get(room.id, owner="alice").status == "interrupted"
    assert P.store().get_turn("turn_live").state == "interrupted"
    # A turn that died with the process keeps the reason it stopped empty:
    # "the process died" is not a conclusion the room reached.
    assert P.store().get_turn("turn_live").stop_reason == ""


def test_recovery_is_safe_to_run_twice():
    svc = _svc()
    room = _room(svc)
    svc.update(room.id, {"status": "ready"}, owner="alice")
    svc.update(room.id, {"status": "active"}, owner="alice")

    first = svc.recover()
    second = svc.recover()

    assert first["counts"]["sessions_interrupted"] == 1
    assert second["counts"]["sessions_interrupted"] == 0
    assert second["effects_repeated"] == []


def test_archiving_hides_the_room_without_deleting_its_evidence():
    svc = _svc()
    room = _room(svc)

    assert svc.archive(room.id, owner="alice") is True
    assert svc.list(owner="alice") == []
    assert svc.get(room.id, owner="alice") is not None


def test_the_shared_service_is_one_object_until_it_is_reset():
    first = service_mod.service()

    assert service_mod.service() is first
    service_mod.reset_service()
    assert service_mod.service() is not first
