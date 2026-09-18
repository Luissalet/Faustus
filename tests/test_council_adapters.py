"""Council adapters: the bridges call the engines, and invent nothing.

Every test here is one of the rules `src/council/adapters.py` exists to hold,
and the plan's own §20 row for integration is the shape of the file:

* the invoker reaches the streaming chat call Faustus already has, once, with
  no tools and no loop of its own;
* the task executor reaches `dispatch.start/wait/compact/cancel` and steers
  through the worker registry, instead of re-implementing a worker;
* `verify_task` delegates to `prove` and never manufactures `verified` — a
  task with no ChangeSet behind it stays `unproved` however confidently a
  worker announced it had finished;
* the claims that reach `prove` come from the LEDGER and the changes from what
  was OBSERVED (§20: "los archivos observados y reclamados llegan a `prove`");
* a failed judge produces no score at all (§3.4);
* every adapter degrades with a reason and never with a fake success.

Nothing here talks to a model, a worker or a GPU: `adapters.use_engines` is the
seam, and the autouse fixture puts the real modules back afterwards.  The two
exceptions are `prove` and `changesets`, which are pure and are exercised for
real — a verdict test against a double would only prove the double.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.council import adapters
from src.council.contracts import CouncilParticipant, CouncilSession, CouncilTask, CouncilTurn


@pytest.fixture(autouse=True)
def _real_engines_afterwards():
    yield
    adapters.reset_engines()


# --- doubles ---------------------------------------------------------------

def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _delta(text: str) -> str:
    return _sse({"delta": text})


def _usage(**data) -> str:
    return _sse({"type": "usage", "data": data})


def _error(message: str) -> str:
    return "event: error\n" + _sse({"status": 502, "text": message, "error": message})


_DONE = "data: [DONE]\n\n"


class _FakeLLM:
    """`llm_core` as far as this module is concerned: one `stream_llm`."""

    def __init__(self, chunks, *, delay: float = 0.0) -> None:
        self.chunks = list(chunks)
        self.delay = delay
        self.calls = []

    def stream_llm(self, url, model, messages, **kw):
        self.calls.append({"url": url, "model": model, "messages": messages, "kw": kw})
        chunks, delay = list(self.chunks), self.delay

        async def _gen():
            for chunk in chunks:
                if delay:
                    await asyncio.sleep(delay)
                yield chunk

        return _gen()


def _resolver(model, *, owner=""):
    return ("http://endpoint/v1", f"{model}::resolved", {"Authorization": "x"})


def _seat(**over) -> CouncilParticipant:
    payload = {"id": "p_claude", "display_name": "Claude", "model": "claude-x",
               "roles": ["critic"], "tool_profile": "read_only"}
    payload.update(over)
    return CouncilParticipant.parse(payload)


def _turn(content: str = "Rebut these two proposals") -> CouncilTurn:
    return CouncilTurn.parse({"id": "turn_1", "session_id": "council_1",
                              "content": content, "author_id": "alice"})


def _session(**over) -> CouncilSession:
    payload = {"id": "council_1", "owner": "alice", "title": "OAuth",
               "policy": "collaborate", "workspace": "D:/LocalAI/odysseus"}
    payload.update(over)
    return CouncilSession.parse(payload)


def _task(**over) -> CouncilTask:
    payload = {"id": "task_1", "session_id": "council_1", "title": "Implement the callback",
               "instruction": "Add the OAuth callback route", "run_id": "run_1",
               "owner_participant_id": "p_codex", "claimed_resources": ["src/a.py"]}
    payload.update(over)
    return CouncilTask.parse(payload)


class _Item:
    def __init__(self, title: str, body: str) -> None:
        self.title, self.body = title, body


class _Section:
    def __init__(self, kind: str, items) -> None:
        self.kind, self.items = kind, tuple(items)


class _Packet:
    """A `ContextPacket` as far as `_packet_text` needs to know."""

    def __init__(self, sections, owner: str = "alice") -> None:
        self.sections, self.owner = tuple(sections), owner


# --- the invoker uses the streaming chat call that already exists ----------

async def test_the_invoker_calls_the_existing_stream_once_and_asks_for_no_tools():
    """Rule 1: no third road to the model, and no loop of its own.

    The assertion that matters is the count: an invoker that looped would call
    `stream_llm` again after the first answer, and an invoker that had grown
    into an agent would have sent tools.
    """
    llm = _FakeLLM([_delta("The state parameter "), _delta("must be checked server-side."),
                    _usage(input_tokens=120, output_tokens=18), _DONE])
    adapters.use_engines(llm=llm)
    invoker = adapters.StreamingChatInvoker(endpoint_resolver=_resolver)

    result = await invoker.invoke(participant=_seat(), packet={"system": "room rules"},
                                  turn=_turn(), timeout_s=5)

    assert result["ok"] is True
    assert result["content"] == "The state parameter must be checked server-side."
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 18}
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["url"] == "http://endpoint/v1"
    assert call["model"] == "claude-x::resolved"
    assert "tools" not in call["kw"] and "tool_choice_none" not in call["kw"]
    assert [m["role"] for m in call["messages"]] == ["system", "user"]


async def test_a_context_packet_becomes_the_system_message_and_the_turn_the_question():
    packet = _Packet([_Section("system_constraints", [_Item("Council room rules", "one writer")]),
                      _Section("active_goal", [_Item("", "ship OAuth")])])
    llm = _FakeLLM([_delta("noted"), _DONE])
    adapters.use_engines(llm=llm)

    result = await adapters.StreamingChatInvoker(endpoint_resolver=_resolver).invoke(
        participant=_seat(), packet=packet, turn=_turn("what now?"))

    assert result["ok"] is True
    system, user = llm.calls[0]["messages"]
    assert system["role"] == "system"
    assert "Council room rules" in system["content"] and "one writer" in system["content"]
    assert "[active_goal]" in system["content"]
    assert user == {"role": "user", "content": "what now?"}


async def test_supplied_messages_keep_their_roles():
    """§3.2: a peer's words are never re-labelled on their way to a model."""
    llm = _FakeLLM([_delta("ok"), _DONE])
    adapters.use_engines(llm=llm)
    packet = {"messages": [{"role": "system", "content": "rules"},
                           {"role": "assistant", "content": "[Claude]: hi"},
                           {"role": "user", "content": "go"}]}

    await adapters.StreamingChatInvoker(endpoint_resolver=_resolver).invoke(
        participant=_seat(), packet=packet, turn=_turn())

    assert [m["role"] for m in llm.calls[0]["messages"]] == ["system", "assistant", "user"]


async def test_a_packet_with_no_question_degrades_instead_of_inventing_a_prompt():
    llm = _FakeLLM([_delta("should never run"), _DONE])
    adapters.use_engines(llm=llm)

    result = await adapters.StreamingChatInvoker(endpoint_resolver=_resolver).invoke(
        participant=_seat(), packet={"system": "rules"}, turn=_turn(""))

    assert result["ok"] is False
    assert result["reason"] == "empty_packet"
    assert result["degraded"] is True
    assert llm.calls == []


async def test_an_error_in_the_stream_is_not_an_answer():
    llm = _FakeLLM([_delta("partial"), _error("the endpoint is gone"), _DONE])
    adapters.use_engines(llm=llm)

    result = await adapters.StreamingChatInvoker(endpoint_resolver=_resolver).invoke(
        participant=_seat(), packet={"system": "rules"}, turn=_turn())

    assert result["ok"] is False
    assert result["reason"] == "model_error"
    assert "the endpoint is gone" in result["detail"]


async def test_an_empty_answer_is_not_read_as_an_abstention():
    llm = _FakeLLM([_usage(input_tokens=5, output_tokens=0), _DONE])
    adapters.use_engines(llm=llm)

    result = await adapters.StreamingChatInvoker(endpoint_resolver=_resolver).invoke(
        participant=_seat(), packet={"system": "rules"}, turn=_turn())

    assert result["ok"] is False
    assert result["reason"] == "empty_answer"


async def test_the_invoker_holds_a_deadline():
    llm = _FakeLLM([_delta("slow"), _DONE], delay=0.30)
    adapters.use_engines(llm=llm)

    result = await adapters.StreamingChatInvoker(endpoint_resolver=_resolver).invoke(
        participant=_seat(), packet={"system": "rules"}, turn=_turn(), timeout_s=0.05)

    assert result["ok"] is False
    assert result["reason"] == "timeout"


async def test_an_absent_chat_engine_degrades_with_a_reason():
    """Rule 6: never a fake success."""
    adapters.use_engines(llm=object())

    result = await adapters.StreamingChatInvoker(endpoint_resolver=_resolver).invoke(
        participant=_seat(), packet={"system": "rules"}, turn=_turn())

    assert result["ok"] is False
    assert result["reason"] == "engine_unavailable"
    assert "llm_core" in result["detail"]


# --- the task executor drives Dispatch, it does not re-implement it -------

class _FakeJob:
    def __init__(self, job_id="run_1", session_id="chat_workers", status="running"):
        self.id = job_id
        self.session_id = session_id
        self.status = status


class _FakeDispatch:
    """`src/dispatch.py` reduced to the five entry points the adapter uses."""

    def __init__(self, job=None, summary=None) -> None:
        self.job = job or _FakeJob()
        self.summary = summary or {}
        self.started = []
        self.waits = 0
        self.compacts = 0
        self.cancelled = []

    async def start(self, owner, body, *, idempotency_key=None, runner=None):
        self.started.append({"owner": owner, "body": body, "key": idempotency_key})
        return self.job

    async def wait(self, job, timeout):
        self.waits += 1
        job.status = "done"
        return True

    def get(self, job_id):
        return self.job if job_id == self.job.id else None

    def compact(self, job):
        self.compacts += 1
        return dict(self.summary)

    def cancel(self, job):
        self.cancelled.append(job.id)
        return True

    def worker_states(self, job):
        return {"Codex": {"state": "working", "why": "editing src/a.py"}}


class _FakeWorkers:
    """`agent_tools.subagent_tools` reduced to steering."""

    def __init__(self, board=None) -> None:
        self._board = board if board is not None else {"child_1": {"parent": "chat_workers"},
                                                       "child_2": {"parent": "someone_else"}}
        self.steered = []

    def worker_board(self):
        return dict(self._board)

    def steer_worker(self, child_session_id, text, source="user"):
        self.steered.append((child_session_id, text, source))
        return True


_DONE_SUMMARY = {
    "id": "run_1", "status": "done",
    "result": {
        "summary": "callback added",
        "files_changed": ["src/a.py"],
        "claimed_only": ["src/never_touched.py"],
        "changes": {"source": "checkpoint", "modified": ["src/a.py"], "checkpoint": "abc123"},
        "verification": {"mode": "tests", "ran": True, "ok": True, "summary": "12 passed"},
    },
}


async def test_the_executor_starts_a_dispatch_job_and_translates_its_summary():
    """§4.4: Council brings the room; Dispatch keeps doing the work."""
    engine = _FakeDispatch(summary=_DONE_SUMMARY)
    adapters.use_engines(dispatch=engine)

    result = await adapters.DispatchTaskExecutor().run(
        task=_task(), participant=_seat(id="p_codex", model="codex-x"), session=_session())

    assert engine.started, "the executor must call dispatch.start, not run a worker itself"
    assert engine.waits == 1 and engine.compacts == 1
    body = engine.started[0]["body"]
    assert body["workspace"] == "D:/LocalAI/odysseus"
    assert body["tasks"][0]["instruction"] == "Add the OAuth callback route"
    assert body["tasks"][0]["files"] == ["src/a.py"]
    assert result["ok"] is True
    assert result["run_id"] == "run_1"
    assert result["status"] == "done"
    assert result["files_changed"] == ["src/a.py"]
    assert result["claimed_only"] == ["src/never_touched.py"]
    assert result["changes"]["checkpoint"] == "abc123"


async def test_the_executor_declares_no_verdict_of_its_own():
    """A finished run is not a proved one; that word belongs to `prove`."""
    adapters.use_engines(dispatch=_FakeDispatch(summary=_DONE_SUMMARY))

    result = await adapters.DispatchTaskExecutor().run(
        task=_task(), participant=_seat(id="p_codex"), session=_session())

    assert "verdict" not in result
    assert adapters.VERIFIED not in json.dumps(result)


async def test_the_same_task_carries_one_idempotency_key():
    """§15.3: a retried delegation lands on the run the first one started."""
    engine = _FakeDispatch(summary=_DONE_SUMMARY)
    adapters.use_engines(dispatch=engine)
    executor = adapters.DispatchTaskExecutor()

    await executor.run(task=_task(), participant=_seat(id="p_codex"), session=_session())
    await executor.run(task=_task(), participant=_seat(id="p_codex"), session=_session())

    assert [row["key"] for row in engine.started] == ["council:council_1:task_1"] * 2


async def test_a_task_with_no_workspace_is_refused_rather_than_run_anywhere():
    engine = _FakeDispatch(summary=_DONE_SUMMARY)
    adapters.use_engines(dispatch=engine)

    result = await adapters.DispatchTaskExecutor().run(
        task=_task(), participant=_seat(id="p_codex"),
        session=_session(workspace=""))

    assert result["ok"] is False and result["reason"] == "no_workspace"
    assert engine.started == []


async def test_an_absent_dispatch_degrades_and_starts_nothing():
    adapters.use_engines(dispatch=object())

    result = await adapters.DispatchTaskExecutor().run(
        task=_task(), participant=_seat(id="p_codex"), session=_session())

    assert result["ok"] is False and result["reason"] == "engine_unavailable"
    assert result["run_id"] == ""


async def test_steer_goes_to_the_worker_registry_of_this_run_only():
    """§17.3: reuse steer/stop; do not open a second channel to a worker."""
    workers = _FakeWorkers()
    adapters.use_engines(dispatch=_FakeDispatch(), workers=workers)

    sent = await adapters.DispatchTaskExecutor().steer("run_1", "  security only  ")

    assert sent is True
    assert workers.steered == [("child_1", "security only", "user")]


async def test_steer_without_a_worker_registry_reports_failure():
    adapters.use_engines(dispatch=_FakeDispatch(), workers=object())

    assert await adapters.DispatchTaskExecutor().steer("run_1", "stop that") is False


async def test_stop_cancels_the_job_through_dispatch():
    engine = _FakeDispatch()
    adapters.use_engines(dispatch=engine)

    assert await adapters.DispatchTaskExecutor().stop("run_1") is True
    assert engine.cancelled == ["run_1"]
    assert await adapters.DispatchTaskExecutor().stop("run_missing") is False


def test_progress_reports_dispatch_s_own_board():
    engine = _FakeDispatch(summary={"status": "running", "phase": "round 2",
                                    "progress": {"Codex": {"last_event": "tool"}},
                                    "result": {}})
    adapters.use_engines(dispatch=engine)

    card = adapters.DispatchTaskExecutor().progress("run_1")

    assert card["ok"] is True
    assert card["status"] == "running"
    assert card["progress"] == {"Codex": {"last_event": "tool"}}
    assert card["workers"]["Codex"]["state"] == "working"


def test_progress_of_an_unknown_run_says_so():
    adapters.use_engines(dispatch=_FakeDispatch())

    card = adapters.DispatchTaskExecutor().progress("run_nope")

    assert card["ok"] is False and card["reason"] == "unknown_run"


# --- verify_task: the verdict is `prove`'s, and only `prove`'s ------------
#
# These run against the real `src/prove.py` and `src/changesets.py`.  Both are
# pure, and a verdict asserted against a double would only prove the double.

def test_a_task_with_no_changeset_is_never_verified_however_sure_the_worker_was():
    """The rule the plan repeats most often (§20, Fase 4).

    The worker's own report is not an input to this function at all: there is
    no argument through which "I finished" could arrive.
    """
    result = adapters.verify_task(task=_task(), session=_session())

    assert result["ok"] is True                 # a proof WAS produced
    assert result["verdict"] == adapters.UNPROVED
    assert result["verdict"] != adapters.VERIFIED
    assert result["proof"]["verdict"] == "unproved"
    assert any(u["kind"] == "no_verification_runner" for u in result["uncertainty"])


def test_verified_comes_out_only_when_the_proof_says_proved():
    result = adapters.verify_task(
        task=_task(),
        session=_session(),
        changes={"source": "checkpoint", "modified": ["src/a.py"], "checkpoint": "abc123"},
        verification={"mode": "tests", "ran": True, "ok": True, "command": "pytest",
                      "summary": "12 passed"})

    assert result["proof"]["verdict"] == "proved"
    assert result["verdict"] == adapters.VERIFIED
    assert result["changeset_id"].startswith("chg_")


def test_the_claims_come_from_the_ledger_and_the_changes_from_the_observation():
    """§20: "los archivos observados y reclamados llegan a `prove`".

    The ledger says this task reserved `src/b.py`; the disk says `src/a.py`
    changed.  A verifier that took its claims from the run's own report would
    call this proved.
    """
    result = adapters.verify_task(
        task=_task(claimed_resources=["src/b.py"]),
        session=_session(),
        changes={"source": "checkpoint", "modified": ["src/a.py"], "checkpoint": "abc123"},
        verification={"mode": "tests", "ran": True, "ok": True, "summary": "12 passed"})

    assert result["claims"] == [{"path": "src/b.py", "kind": "modified"}]
    assert result["verdict"] == "contradicted"
    assert result["verdict"] != adapters.VERIFIED


def test_without_the_changeset_builder_the_verdict_stays_unproved():
    adapters.use_engines(changesets=object())

    result = adapters.verify_task(task=_task(), session=_session())

    assert result["ok"] is False
    assert result["reason"] == "engine_unavailable"
    assert result["verdict"] == adapters.UNPROVED


# --- the blind round and the judge stay Tournament's ----------------------

class _FakeTournament:
    def __init__(self, *, answers=None, judge_block=None, run_error=None,
                 judge_error=None) -> None:
        self.answers = answers or []
        self.judge_block = judge_block
        self.run_error = run_error
        self.judge_error = judge_error
        self.runs = []
        self.judged = []
        self.owner = "unset"

    def default_llm_call(self, owner=None):
        self.owner = owner

        async def _call(messages, model):  # pragma: no cover - never reached
            return ""

        return _call

    def label_for(self, index):
        return chr(ord("A") + index)

    def strongest(self, models):
        return list(models)[0] if models else ""

    def gpu_slots(self, *a, **kw):
        return None

    async def run(self, prompt, models, *, rounds=1, llm_call=None, state=None,
                  cancel_event=None, **kw):
        self.runs.append({"prompt": prompt, "models": list(models), "rounds": rounds,
                          "cancel_event": cancel_event})
        if self.run_error:
            raise self.run_error
        state.update({"answers": list(self.answers), "convergence": None,
                      "ranking": "deterministic", "stopped_by": "rounds",
                      "cancelled": [], "errors": [], "degraded": False})
        return state

    async def _judge(self, prompt, solutions, judge, call, pool, on_event, events):
        self.judged.append({"prompt": prompt, "solutions": solutions, "judge": judge})
        if self.judge_error:
            raise self.judge_error
        return dict(self.judge_block or {})


async def test_the_blind_round_runs_on_tournament_and_maps_answers_back_to_seats():
    engine = _FakeTournament(answers=[
        {"entry": 0, "model": "claude-x", "round": 0, "text": "check it server-side",
         "tokens": 40, "tokens_source": "reported"},
        {"entry": 1, "model": "codex-x", "round": 0, "text": "check it client-side",
         "tokens": 33, "tokens_source": "estimated"},
    ])
    adapters.use_engines(tournament=engine)
    seats = [_seat(id="p_claude", model="claude-x"), _seat(id="p_codex", model="codex-x")]

    result = await adapters.TournamentAdapter(owner="alice").blind_round(
        question="Where is the OAuth state checked?", participants=seats)

    assert engine.runs and engine.runs[0]["rounds"] == 1, "round 0 is the blind one"
    assert engine.runs[0]["models"] == ["claude-x", "codex-x"]
    assert engine.runs[0]["cancel_event"] is not None
    assert engine.owner == "alice"
    assert result["ok"] is True
    assert [(a["participant_id"], a["content"]) for a in result["answers"]] == [
        ("p_claude", "check it server-side"), ("p_codex", "check it client-side")]


async def test_a_seat_that_did_not_answer_still_appears_in_the_blind_round():
    """§3.4: silence is a fact the coordinator needs, not agreement."""
    engine = _FakeTournament(answers=[{"entry": 0, "model": "claude-x", "round": 0,
                                       "text": "server-side"}])
    adapters.use_engines(tournament=engine)
    seats = [_seat(id="p_claude", model="claude-x"), _seat(id="p_codex", model="codex-x")]

    result = await adapters.TournamentAdapter().blind_round(question="q", participants=seats)

    assert [a["outcome"] for a in result["answers"]] == ["success", "no_answer"]
    assert result["ok"] is True and result["degraded"] is True
    assert result["reason"] == "partial_round"


async def test_a_blind_round_needs_something_to_contrast():
    adapters.use_engines(tournament=_FakeTournament())

    result = await adapters.TournamentAdapter().blind_round(
        question="q", participants=[_seat()])

    assert result["ok"] is False and result["reason"] == "too_few_participants"


async def test_an_absent_tournament_degrades_with_a_reason():
    adapters.use_engines(tournament=object())

    result = await adapters.TournamentAdapter().blind_round(
        question="q", participants=[_seat(id="a", model="m1"), _seat(id="b", model="m2")])

    assert result["ok"] is False and result["reason"] == "engine_unavailable"
    assert result["answers"] == []


async def test_a_failed_judge_produces_no_score_and_no_verdict():
    """§3.4 and §20: "un juez fallido no produce una puntuación inventada"."""
    engine = _FakeTournament(judge_block={
        "ok": False, "scores": None, "attempts": 2,
        "error": "the judge did not answer with the JSON the rubric asked for"})
    adapters.use_engines(tournament=engine)

    result = await adapters.TournamentAdapter().judge(
        question="Which proposal?", rubric="weight security first",
        answers=[{"participant_id": "p_claude", "model": "claude-x", "content": "one"},
                 {"participant_id": "p_codex", "model": "codex-x", "content": "two"}])

    assert result["ok"] is False
    assert result["scores"] is None
    assert result["verdict"] == ""
    assert result["ranking"] == []
    assert result["reason"] == "judge_unreadable"
    assert "weight security first" in engine.judged[0]["prompt"]


async def test_a_judge_that_raises_scores_nothing_either():
    adapters.use_engines(tournament=_FakeTournament(judge_error=RuntimeError("no endpoint")))

    result = await adapters.TournamentAdapter().judge(
        question="q", answers=[{"participant_id": "a", "model": "m1", "content": "one"},
                               {"participant_id": "b", "model": "m2", "content": "two"}])

    assert result["ok"] is False and result["scores"] is None
    assert result["verdict"] == "" and result["reason"] == "judge_failed"


async def test_a_judge_that_answers_ranks_the_participants():
    engine = _FakeTournament(judge_block={
        "ok": True, "attempts": 1, "error": None,
        "scores": {"A": {"total": 7}, "B": {"total": 9}}})
    adapters.use_engines(tournament=engine)

    result = await adapters.TournamentAdapter().judge(
        question="q", model="judge-x",
        answers=[{"participant_id": "p_claude", "model": "claude-x", "content": "one"},
                 {"participant_id": "p_codex", "model": "codex-x", "content": "two"}])

    assert result["ok"] is True
    assert result["judge_model"] == "judge-x"
    assert result["ranking"] == ["p_codex", "p_claude"]
    assert result["verdict"] == "p_codex"


async def test_an_absent_judge_leaves_a_synthesis_without_a_verdict():
    adapters.use_engines(tournament=object())

    result = await adapters.TournamentAdapter().judge(
        question="q", answers=[{"participant_id": "a", "content": "one"},
                               {"participant_id": "b", "content": "two"}])

    assert result["ok"] is False
    assert result["reason"] == "judge_unavailable"
    assert result["scores"] is None and result["verdict"] == ""


# --- the defaults the service builds -------------------------------------

def test_the_defaults_are_the_two_bridges_and_not_something_new():
    assert isinstance(adapters.default_invoker(), adapters.StreamingChatInvoker)
    assert isinstance(adapters.default_executor(owner="alice", workspace="D:/x"),
                      adapters.DispatchTaskExecutor)


def test_the_engine_seam_refuses_a_name_it_does_not_bridge_to():
    with pytest.raises(KeyError):
        adapters.use_engines(nonesuch=object())
