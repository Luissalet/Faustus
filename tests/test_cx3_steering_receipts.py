"""H19: a steering message is never reported as delivered without a receipt,
and nothing a restart or a finished run swallows is lost silently."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import agent_runs, exec_ledger as xl, steering_journal as sj


@pytest.fixture(autouse=True)
def isolated(tmp_path):
    xl.set_db_path(str(tmp_path / "ledger.sqlite3"))
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    xl.set_db_path(None)


async def _slow(started, release):
    started.set()
    await release.wait()
    yield 'data: {"type": "token", "text": "x"}\n\n'


async def _live(sid):
    started, release = asyncio.Event(), asyncio.Event()
    run = agent_runs.start(sid, _slow(started, release), label=sid)
    await asyncio.wait_for(started.wait(), 2)
    return run, release


@pytest.mark.asyncio
async def test_a_steer_is_queued_then_drained_then_applied_only_when_the_loop_says_so():
    run, release = await _live("s1")
    receipt = agent_runs.queue_steer_receipt("s1", "  use pytest  ", owner="alice")
    rid = receipt["receipt_id"]
    assert receipt["state"] == "queued" and receipt["durable"] is True
    assert sj.get(rid)["state"] == "queued"
    assert agent_runs.take_steers("s1") == [{"text": "use pytest", "source": "user"}]
    assert sj.get(rid)["state"] == "drained"      # the run took it: not "delivered"
    agent_runs._publish(run, 'data: {"type": "steer", "round": 2, "text": "use pytest", "source": "user"}\n\n')
    rec = sj.get(rid)
    assert rec["state"] == "applied" and [h["state"] for h in rec["history"]] == ["queued", "drained", "applied"]
    release.set()
    await asyncio.wait_for(run.task, 5)


@pytest.mark.asyncio
async def test_a_steer_the_run_never_read_is_dropped_with_a_reason_and_its_text_kept():
    run, release = await _live("s2")
    rid = agent_runs.queue_steer_receipt("s2", "also check the docs", owner="alice")["receipt_id"]
    release.set()
    await asyncio.wait_for(run.task, 5)
    rec = sj.get(rid)
    assert rec["state"] == "dropped" and rec["reason"] == "run_ended_before_it_was_read"
    assert rec["text"] == "also check the docs"
    assert [r["receipt_id"] for r in sj.undelivered("s2")] == [rid]


@pytest.mark.asyncio
async def test_a_dropped_message_reaches_the_next_turn_once():
    run, release = await _live("s3")
    rid = agent_runs.queue_steer_receipt("s3", "remember the deadline", owner="alice")["receipt_id"]
    release.set()
    await asyncio.wait_for(run.task, 5)
    block = sj.carry_block("s3", owner="alice")
    assert "remember the deadline" in block and "run_ended_before_it_was_read" in block
    assert sj.get(rid)["state"] == "redelivered"
    assert sj.carry_block("s3", owner="alice") is None  # once, not every turn


def test_restart_drops_what_the_dead_process_left_unresolved(monkeypatch):
    queued = sj.queued("s4", "run-old", owner="alice", text="first", source="user", mode="steer")["receipt_id"]
    drained = sj.queued("s4", "run-old", owner="alice", text="second", source="user", mode="steer")["receipt_id"]
    sj.transition(drained, "drained", session_id="s4", run_id="run-old", owner="alice")
    applied = sj.queued("s4", "run-old", owner="alice", text="third", source="user", mode="steer")["receipt_id"]
    sj.transition(applied, "applied", session_id="s4", run_id="run-old", owner="alice")
    after = sj.queued("s4", "run-old", owner="alice", text="fourth", source="user", mode="send_after")["receipt_id"]

    monkeypatch.setattr(xl, "BOOT_ID", "boot-2")
    out = sj.recover_after_restart()
    by = {d["receipt_id"]: d for d in out["dropped"]}
    assert by[queued]["reason"] == "process_restart_before_delivery"
    assert by[drained]["reason"] == "process_restart_after_handoff_unconfirmed"
    assert by[after]["reason"] == "process_restart_before_delivery"
    assert applied not in by and sj.get(applied)["state"] == "applied"  # delivered ones stay delivered
    assert sj.recover_after_restart() == {"dropped": []}  # idempotent
    block = sj.carry_block("s4", owner="alice")
    assert "first" in block and "second" in block and "fourth" in block and "third" not in block


def test_owner_scoping_of_receipts_and_carry():
    rid = sj.queued("s5", "r", owner="alice", text="private", source="user", mode="steer")["receipt_id"]
    sj.transition(rid, "dropped", session_id="s5", run_id="r", owner="alice", reason="x")
    assert sj.get(rid, owner="bob") is None
    assert sj.carry_block("s5", owner="bob") is None
    assert "private" in sj.carry_block("s5", owner="alice")


@pytest.mark.asyncio
async def test_send_after_is_claimed_then_acknowledged_and_never_claimed_twice():
    run, release = await _live("s6")
    receipt = agent_runs.queue_send_after_receipt("s6", "and then summarize", owner="alice")
    rid = receipt["receipt_id"]
    assert receipt["mode"] == "send_after" and sj.get(rid)["state"] == "queued"
    release.set()
    await asyncio.wait_for(run.task, 5)
    assert sj.get(rid)["state"] == "queued"        # a run ending does not drop a send-after
    claimed = sj.claim_send_after("s6", owner="alice")
    assert [c["text"] for c in claimed] == ["and then summarize"]
    assert sj.get(rid)["state"] == "handed_off"    # given to the client, not yet sent
    assert sj.claim_send_after("s6", owner="alice") == []
    assert sj.acknowledge(rid, owner="bob") is None
    assert sj.acknowledge(rid, owner="alice")["state"] == "applied"
    assert sj.acknowledge(rid, owner="alice") is None  # one acknowledgement


def test_unacknowledged_handoff_is_dropped_after_a_restart(monkeypatch):
    rid = sj.queued("s7", "r", owner="alice", text="later", source="user", mode="send_after")["receipt_id"]
    sj.transition(rid, "handed_off", session_id="s7", run_id="r", owner="alice", mode="send_after")
    monkeypatch.setattr(xl, "BOOT_ID", "boot-3")
    sj.recover_after_restart()
    rec = sj.get(rid)
    assert rec["state"] == "dropped" and rec["reason"] == "process_restart_client_never_confirmed"


@pytest.mark.asyncio
async def test_steering_survives_a_disabled_ledger_honestly(monkeypatch):
    monkeypatch.setattr(xl, "enabled", lambda: False)
    run, release = await _live("s8")
    receipt = agent_runs.queue_steer_receipt("s8", "hello", owner="alice")
    assert receipt["durable"] is False            # the queue works, the receipt says it is not durable
    assert agent_runs.take_steers("s8") == [{"text": "hello", "source": "user"}]
    release.set()
    await asyncio.wait_for(run.task, 5)


# --- HTTP surface -----------------------------------------------------------

class _Req:
    def __init__(self, body=None, run_id=None, user="alice"):
        self.headers = {"X-Odysseus-Run-Id": run_id} if run_id else {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user=user)
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def routes(monkeypatch):
    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **kw: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace())

    def _route(path, method):
        for r in reversed(router.routes):
            if r.path == path and method in getattr(r, "methods", set()):
                return r.endpoint
        raise AssertionError(f"route not found: {method} {path}")
    return _route


@pytest.mark.asyncio
async def test_routes_return_receipts_and_hand_off_send_after(routes):
    steer = routes("/api/chat/steer/{session_id}", "POST")
    claim = routes("/api/chat/steer/{session_id}/claim", "POST")
    ack = routes("/api/chat/steer/receipt/{receipt_id}/ack", "POST")
    listing = routes("/api/chat/steer/{session_id}/receipts", "GET")
    run, release = await _live("h1")
    out = await steer(_Req(body={"text": "later please", "mode": "queue"}, run_id=run.run_id), "h1")
    rid = out["receipt"]["receipt_id"]
    assert (await claim(_Req(), "h1")) == {"messages": [], "blocked": "run_active"}  # the turn is still going
    release.set()
    await asyncio.wait_for(run.task, 5)
    got = await claim(_Req(), "h1")
    assert [m["text"] for m in got["messages"]] == ["later please"]
    assert (await ack(_Req(), rid))["receipt"]["state"] == "applied"
    states = {r["receipt_id"]: r["state"] for r in (await listing(_Req(), "h1"))["receipts"]}
    assert states[rid] == "applied"
