"""/api/council/* — the transport (routes/council_routes.py).

One test per hard rule of the API, and the rules are all about the edge, because
that is where a council can leak or duplicate:

1. the owner comes from the authenticated session; an `owner` in the body is
   dropped and named back in `ignored_fields` (plan 16, 20);
2. another owner's room is 404 and never 403 — a 403 confirms it exists — and
   the refusal carries no title;
3. `POST /messages` answers with a `turn_id` while the turn is still running
   (plan 13), so a client that reconnects can find the turn it started;
4. the same `idempotency_key` twice opens ONE turn (plan 15.3, 20);
5. `DELETE` archives: the transcript, the ledger and the decisions stay
   readable afterwards (plan 13 — archive, never drop the evidence);
6. `/events` (SSE) and `/wait` (long poll) both resume from `seq`, and a
   reconnect neither repeats an event nor skips one (plan 20);
7. a command is refused with a STABLE token from `service.ERRORS`, and an
   unknown one is refused by name with the valid ones listed (plan 13);
8. `agent_council` off refuses EXECUTION and keeps every read answering;
9. `GET /config` hands the form what it needs.

Rules 3 and 4 run against the REAL `CouncilOrchestrator` with a doubled model
invoker: they are the two rules a fake coordinator could pass while the shipped
one failed, and the point of testing them here is that the whole path — route,
service seam, orchestrator, store — is exercised by one HTTP call.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.council_routes as cr
from src.council import events as council_events
from src.council import orchestrator as council_orchestrator
from src.council import persistence as P
from src.council import scheduler as council_scheduler
from src.council import service as service_mod
from src.council.contracts import TERMINAL_TURN_STATES


# ── doubles ────────────────────────────────────────────────────────────────

class _Invoker:
    """A model that answers instantly. No network, no GPU, no tokenizer."""

    def __init__(self) -> None:
        self.calls: list = []

    async def invoke(self, *, participant, packet, turn, timeout_s):
        self.calls.append(getattr(participant, "id", ""))
        return {"content": f"an answer from {getattr(participant, 'id', '')}",
                "usage": {"total_tokens": 5}}


class _GatedInvoker:
    """A model that does not answer until the test lets it.

    The gate is a `threading.Event` rather than an `asyncio.Event` on purpose:
    the turn runs on the TestClient's portal loop in another thread, and
    `asyncio.Event.set()` from the test's thread is not safe. Polling a
    threading primitive from the coroutine side is, and it costs one 10 ms tick.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.released = threading.Event()
        self.calls: list = []

    async def invoke(self, *, participant, packet, turn, timeout_s):
        import asyncio

        self.calls.append(getattr(participant, "id", ""))
        self.entered.set()
        while not self.released.is_set():
            await asyncio.sleep(0.01)
        return {"content": "finally", "usage": {"total_tokens": 1}}


class _Caller:
    """Who the middleware would have resolved. Mutable so one client can be
    two different people without rebuilding the app."""

    def __init__(self, who: str = "alice") -> None:
        self.who = who


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def caller() -> _Caller:
    return _Caller("alice")


@pytest.fixture
def invoker() -> _Invoker:
    return _Invoker()


@pytest.fixture
def council(tmp_path, monkeypatch, caller, invoker):
    """An app with only this router, its own database, and nothing shared.

    `require_admin` is stubbed here so the tests below are about the council's
    own rules; that the gate is really wired is its own test at the bottom,
    which runs against the unpatched middleware.
    """
    P.use_path(str(tmp_path / "council.db"))
    service_mod.reset_service()
    service_mod.reset_orchestrator()
    council_scheduler.reset_schedulers()
    council_events.reset_streams()
    # No Context Engine on the turn path: these tests are about the transport,
    # and the orchestrator degrades to a stated stand-in packet by design.
    council_orchestrator.use_packet_builder(None)

    svc = service_mod.CouncilService(invoker=invoker)
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(cr, "require_admin", lambda request: None)
    monkeypatch.setattr(cr, "effective_user", lambda request: caller.who)
    monkeypatch.setattr(cr, "get_current_user", lambda request: caller.who)
    monkeypatch.setattr(cr, "enabled", lambda: True)

    app = FastAPI()
    app.include_router(cr.setup_council_routes())
    # A context manager, not a bare TestClient: the portal loop has to outlive
    # the request, or the background turn `POST /messages` starts is collected
    # the moment the response is written and rule 3 becomes untestable.
    with TestClient(app) as client:
        client.svc = svc
        yield client

    council_orchestrator.reset_packet_builder()
    service_mod.reset_service()
    service_mod.reset_orchestrator()
    council_scheduler.reset_schedulers()
    council_events.reset_streams()
    P.use_path(None)


TWO_SEATS = [{"display_name": "Claude", "model": "model-a", "roles": ["architect"]},
             {"display_name": "Codex", "model": "model-b", "roles": ["reviewer"]}]


def _open(client, **over):
    payload = {"title": "Secret refactor", "policy": "chat",
               "participants": TWO_SEATS}
    payload.update(over)
    response = client.post("/api/council", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _settle(client, session_id, *, tries=400):
    """Wait for every turn in the room to reach a terminal state."""
    for _ in range(tries):
        rows = P.store().list_turns(session_id)
        if rows and all(t.state in TERMINAL_TURN_STATES for t in rows):
            return rows
        time.sleep(0.02)
    return P.store().list_turns(session_id)


# ── rule 1: the owner is the caller's, and a body owner is ignored ────────

def test_the_owner_comes_from_the_session_and_a_body_owner_is_ignored(client_pair):
    """A room anybody could create as anybody is a multi-tenant hole with the
    shape of a convenience (plan 16)."""
    client, caller = client_pair
    body = _open(client, owner="mallory", id="council_chosen_by_the_caller",
                 revision=99, status="active")

    assert body["session"]["owner"] == "alice"
    assert body["session"]["id"] != "council_chosen_by_the_caller"
    assert body["session"]["revision"] == 1
    # Not an error and not obeyed: named back so a client learns it was not
    # choosing, rather than being silently overruled.
    assert body["ignored_fields"] == ["owner", "id", "revision", "status"]

    caller.who = "mallory"
    assert client.get(f"/api/council/{body['session']['id']}").status_code == 404


def test_a_patch_cannot_hand_the_room_to_somebody_else(client_pair):
    client, _ = client_pair
    room = _open(client)["session"]

    response = client.patch(f"/api/council/{room['id']}",
                            json={"owner": "mallory", "title": "mine now"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["ignored_fields"] == ["owner"]
    assert body["session"]["owner"] == "alice"
    assert body["session"]["title"] == "mine now"


def test_the_author_of_a_message_is_the_caller_not_the_body(client_pair):
    client, _ = client_pair
    room = _open(client)["session"]

    response = client.post(f"/api/council/{room['id']}/messages",
                           json={"content": "hello", "author_id": "somebody_else"})

    assert response.status_code == 200
    assert response.json()["ignored_fields"] == ["author_id"]
    _settle(client, room["id"])
    rows = client.get(f"/api/council/{room['id']}/messages").json()["messages"]
    user_rows = [m for m in rows if m["author_kind"] == "user"]
    assert user_rows and all(m["author_id"] == "alice" for m in user_rows)


# ── rule 2: somebody else's room is 404, and leaks no title ───────────────

READS = ("", "/messages", "/ledger", "/tasks", "/decisions", "/usage",
         "/events", "/wait")


def test_another_owners_room_is_404_never_403_and_leaks_no_title(client_pair):
    """A 403 says "this exists and is not yours", which is half the secret; the
    title in the message would be the other half (plan 20)."""
    client, caller = client_pair
    room = _open(client, title="Secret refactor of the payment gateway")["session"]

    caller.who = "bob"
    for suffix in READS:
        response = client.get(f"/api/council/{room['id']}{suffix}",
                              params={"timeout": 0})
        assert response.status_code == 404, f"GET {suffix} answered {response.status_code}"
        assert "Secret refactor" not in response.text
        assert "payment gateway" not in response.text

    for method, suffix, payload in (
            ("patch", "", {"title": "x"}),
            ("delete", "", None),
            ("post", "/messages", {"content": "hi"}),
            ("post", "/commands", {"command": "pause"})):
        call = getattr(client, method)
        response = (call(f"/api/council/{room['id']}{suffix}", json=payload)
                    if payload is not None else call(f"/api/council/{room['id']}{suffix}"))
        assert response.status_code == 404, f"{method} {suffix} answered {response.status_code}"
        assert "Secret refactor" not in response.text

    assert client.get("/api/council").json()["sessions"] == []
    caller.who = "alice"
    assert client.get(f"/api/council/{room['id']}").status_code == 200


def test_a_room_that_never_existed_answers_exactly_like_a_stranger_s(client_pair):
    client, caller = client_pair
    room = _open(client)["session"]
    caller.who = "bob"

    mine = client.get(f"/api/council/{room['id']}")
    absent = client.get("/api/council/council_0000000000000000")

    assert mine.status_code == absent.status_code == 404
    assert mine.json() == absent.json()


# ── rule 3: the response beats the turn (plan 13) ─────────────────────────

def test_posting_a_message_answers_with_a_turn_id_while_the_turn_still_runs(
        tmp_path, monkeypatch, caller):
    """A request that waited for four models would hold a connection for
    minutes and lose the whole turn when it dropped. The whole design of §13 is
    that the POST returns a handle and the room is watched through events.

    This runs against the REAL orchestrator: a doubled coordinator answering
    fast would prove nothing about the one that ships.
    """
    gated = _GatedInvoker()
    with _app(tmp_path, monkeypatch, caller, gated) as client:
        room = _open(client)["session"]

        started = time.monotonic()
        response = client.post(f"/api/council/{room['id']}/messages",
                               json={"content": "@Claude take a look"})
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["turn_id"], "the POST must answer with the turn it opened"
        assert body["stream"] == "events"

        # The proof that the answer came first: the turn it names is still not
        # finished, because the model behind it has not been let go yet.
        turns = P.store().list_turns(room["id"])
        assert [t.id for t in turns] == [body["turn_id"]]
        assert turns[0].state not in TERMINAL_TURN_STATES
        assert elapsed < 10.0

        gated.released.set()
        settled = _settle(client, room["id"])
        assert settled[0].state == "completed"
        rows = client.get(f"/api/council/{room['id']}/messages").json()["messages"]
        assert any(m["author_kind"] == "model" and m["content"] == "finally" for m in rows)


# ── rule 4: one key, one turn (plan 15.3, 20) ─────────────────────────────

def test_the_same_idempotency_key_does_not_open_a_second_turn(
        tmp_path, monkeypatch, caller):
    """A client that lost its response retries. It must land on the turn the
    first request opened, not start a second round of paid work."""
    with _app(tmp_path, monkeypatch, caller, _Invoker()) as client:
        room = _open(client)["session"]
        payload = {"content": "decide this", "idempotency_key": "key-abc"}

        first = client.post(f"/api/council/{room['id']}/messages", json=payload).json()
        _settle(client, room["id"])
        second = client.post(f"/api/council/{room['id']}/messages", json=payload).json()
        _settle(client, room["id"])

        assert first["turn_id"] == second["turn_id"]
        assert len(P.store().list_turns(room["id"])) == 1
        user_rows = [m for m in client.get(f"/api/council/{room['id']}/messages").json()
                     ["messages"] if m["author_kind"] == "user"]
        assert len(user_rows) == 1, "the retry must not commit a second user message"


def test_the_idempotency_key_may_arrive_in_the_header(tmp_path, monkeypatch, caller):
    """`Idempotency-Key` is the spelling `src/dispatch.py` already answers to;
    a client should not have to know which of the two this endpoint wanted."""
    with _app(tmp_path, monkeypatch, caller, _Invoker()) as client:
        room = _open(client)["session"]
        headers = {"Idempotency-Key": "from-the-header"}

        first = client.post(f"/api/council/{room['id']}/messages",
                            json={"content": "go"}, headers=headers).json()
        _settle(client, room["id"])
        second = client.post(f"/api/council/{room['id']}/messages",
                             json={"content": "go"}, headers=headers).json()

        assert first["idempotency_key"] == "from-the-header"
        assert first["turn_id"] == second["turn_id"]
        assert len(P.store().list_turns(room["id"])) == 1


# ── rule 5: DELETE archives, it does not delete ───────────────────────────

def test_delete_archives_and_the_evidence_stays_readable(client_pair):
    """Evidence of what several models decided and did is the one thing a
    council must not lose to a click (plan 13)."""
    client, _ = client_pair
    room = _open(client)["session"]
    client.post(f"/api/council/{room['id']}/messages", json={"content": "on the record"})
    _settle(client, room["id"])
    before = client.get(f"/api/council/{room['id']}/messages").json()["messages"]
    assert before

    response = client.delete(f"/api/council/{room['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["archived"] is True and body["deleted"] is False

    # Gone from the listing — which is the exact, always-correct signal that a
    # room is archived, and the reason the GET carries no `archived` field.
    assert [s["id"] for s in client.get("/api/council").json()["sessions"]] == []
    # ...and every read still answers, with the same evidence.
    shown = client.get(f"/api/council/{room['id']}")
    assert shown.status_code == 200
    assert shown.json()["session"]["id"] == room["id"]
    assert shown.json()["session"]["title"] == room["title"]
    assert client.get(f"/api/council/{room['id']}/messages").json()["messages"] == before
    for suffix in ("/ledger", "/tasks", "/decisions", "/usage"):
        assert client.get(f"/api/council/{room['id']}{suffix}").status_code == 200
    # and the room is still exactly one owner's: archiving is not a hand-over
    assert client.get(f"/api/council/{room['id']}").json()["session"]["owner"] == "alice"


# ── rule 6: resuming duplicates nothing and skips nothing (plan 20) ───────

def _publish(client, session_id, count, first=1):
    stream = client.svc.events(session_id, owner="alice")
    for n in range(first, first + count):
        stream.publish("council_message", message_id=f"m{n}", content=f"line {n}")
    return stream


def test_reconnecting_to_events_neither_repeats_nor_skips(client_pair):
    """The cursor means STRICTLY AFTER. A consumer that stores the last seq it
    saw and comes back with it gets the rest of the room exactly once."""
    client, _ = client_pair
    room = _open(client)["session"]
    _publish(client, room["id"], 6)

    first = client.get(f"/api/council/{room['id']}/events",
                       params={"since": 0, "limit": 3}).json()
    cursor = first["events"][-1]["seq"]
    second = client.get(f"/api/council/{room['id']}/events",
                        params={"since": cursor}).json()

    seen = [e["seq"] for e in first["events"]] + [e["seq"] for e in second["events"]]
    assert len(seen) == len(set(seen)), f"an event was delivered twice: {seen}"
    assert seen == sorted(seen)
    assert seen[0] == 1 and seen[-1] == first["last_seq"]
    assert seen == list(range(1, len(seen) + 1)), f"a gap opened in {seen}"
    assert all(e["seq"] > cursor for e in second["events"])


def test_the_cursor_may_be_since_seq_or_last_event_id(client_pair):
    """Three spellings, one meaning: a page sends `since`, a script keeps
    `seq`, and a browser `EventSource` sends `Last-Event-ID` unprompted."""
    client, _ = client_pair
    room = _open(client)["session"]
    _publish(client, room["id"], 4)
    url = f"/api/council/{room['id']}/events"

    by_since = client.get(url, params={"since": 2}).json()["events"]
    by_seq = client.get(url, params={"seq": 2}).json()["events"]
    by_header = client.get(url, headers={"Last-Event-ID": "2"}).json()["events"]

    assert [e["seq"] for e in by_since] == [3, 4]
    assert [e["seq"] for e in by_seq] == [3, 4]
    assert [e["seq"] for e in by_header] == [3, 4]


def test_a_dropped_event_is_announced_as_a_gap_and_not_hidden(client_pair):
    """A silent hole costs a decision nobody knows was taken; the marker costs
    a refresh. The stream builds it — this only proves the route passes it on."""
    client, _ = client_pair
    room = _open(client)["session"]
    stream = client.svc.events(room["id"], owner="alice")
    stream.capacity = 3
    stream._events = type(stream._events)(stream._events, maxlen=3)
    _publish(client, room["id"], 8)

    body = client.get(f"/api/council/{room['id']}/events", params={"since": 1}).json()

    markers = [e for e in body["events"] if e["payload"].get("gap")]
    assert markers, "a buffer that dropped events must say so"
    assert markers[0]["payload"]["reason"] == "buffer_overflow"
    assert markers[0]["payload"]["missed"] > 0


def test_wait_is_a_long_poll_that_returns_the_moment_something_happens(client_pair):
    client, _ = client_pair
    room = _open(client)["session"]
    stream = client.svc.events(room["id"], owner="alice")

    def _later():
        time.sleep(0.2)
        stream.publish("council_turn_state", state="running")

    threading.Thread(target=_later, daemon=True).start()
    started = time.monotonic()
    body = client.get(f"/api/council/{room['id']}/wait",
                      params={"since": 0, "timeout": 10}).json()
    elapsed = time.monotonic() - started

    assert body["timed_out"] is False
    assert [e["seq"] for e in body["events"]] == [1]
    assert elapsed < 9.0, "the poll slept on the event, it did not wait out the timeout"


def test_wait_times_out_with_an_empty_list_and_the_cursor_it_was_given(client_pair):
    """A timeout is not an error: it answers with what to send back."""
    client, _ = client_pair
    room = _open(client)["session"]

    body = client.get(f"/api/council/{room['id']}/wait",
                      params={"since": 5, "timeout": 0.2}).json()

    assert body["ok"] is True
    assert body["events"] == []
    assert body["timed_out"] is True
    assert body["since"] == 5


def test_events_speaks_sse_with_unnamed_frames_and_one_named_end(client_pair):
    """An SSE frame carrying `event: <name>` never reaches `onmessage`. Every
    council event therefore goes out unnamed with its name inside the JSON, and
    only the terminal frame is named — the dialect Tournament and Dispatch use."""
    client, _ = client_pair
    room = _open(client)["session"]
    stream = _publish(client, room["id"], 3)
    stream.close()  # so the stream ends at once instead of holding the request

    body = client.get(f"/api/council/{room['id']}/events",
                      params={"stream": 1, "timeout": 5}).text

    frames = [f for f in body.split("\n\n") if f.strip()]
    payloads = [json.loads(f.split("data: ", 1)[1])
                for f in frames if not f.startswith("event:") and "data: " in f]
    assert [p["seq"] for p in payloads] == [1, 2, 3]
    assert all(p["name"] == "council_message" for p in payloads)
    assert body.count("event: end") == 1
    assert body.index("event: end") > body.rindex('"seq": 3')


def test_an_event_source_gets_sse_from_its_accept_header_alone(client_pair):
    """`new EventSource(url)` sends no query parameter and cannot add one."""
    client, _ = client_pair
    room = _open(client)["session"]
    _publish(client, room["id"], 1).close()

    response = client.get(f"/api/council/{room['id']}/events",
                          headers={"Accept": "text/event-stream"},
                          params={"timeout": 5})

    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: " in response.text


# ── rule 7: commands answer with tokens, not with sentences ───────────────

def test_an_unknown_command_is_refused_by_name_and_lists_the_valid_ones(client_pair):
    client, _ = client_pair
    room = _open(client)["session"]

    response = client.post(f"/api/council/{room['id']}/commands",
                           json={"command": "self_destruct"})

    assert response.status_code == 200, "a rejection is an answer, not a 4xx"
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "unknown_command"
    assert body["error"]["code"] in service_mod.ERRORS
    assert "self_destruct" in body["error"]["message"]
    assert body["valid_commands"] == list(service_mod.COMMANDS)


def test_every_command_of_the_plan_is_routable(client_pair):
    """§13's list, closed. A command nothing routes on is a string in a log."""
    client, _ = client_pair
    room = _open(client)["session"]

    for command in service_mod.COMMANDS:
        body = client.post(f"/api/council/{room['id']}/commands",
                           json={"command": command}).json()
        code = body.get("error", {}).get("code", "")
        assert code != "unknown_command", f"{command} is in COMMANDS and does not route"
        if not body["ok"]:
            assert code in service_mod.ERRORS, f"{command} answered with {code!r}"


def test_a_command_missing_an_argument_says_which_one(client_pair):
    client, _ = client_pair
    room = _open(client)["session"]

    body = client.post(f"/api/council/{room['id']}/commands",
                       json={"command": "steer", "participant_id": "p_claude"}).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "missing_argument"
    assert "message" in body["error"]["message"]


def test_a_stale_revision_loses_and_is_told_which_one_won(client_pair):
    client, _ = client_pair
    room = _open(client)["session"]

    body = client.post(f"/api/council/{room['id']}/commands",
                       json={"command": "pause", "expected_revision": 99}).json()

    assert body["ok"] is False
    assert body["error"]["code"] == "revision_conflict"
    assert body["revision"] == room["revision"]


def test_a_refusal_never_carries_the_text_of_an_exception_as_its_code(client_pair):
    """§13: stable domain errors, never the text of an exception. Every code
    this surface can answer with is in `service.ERRORS` or in `ROUTE_ERRORS`."""
    client, _ = client_pair
    room = _open(client)["session"]
    known = set(service_mod.ERRORS) | set(cr.ROUTE_ERRORS)

    for payload in ({"command": "nope"},
                    {"command": "cancel_turn"},
                    {"command": "assign_role", "participant_id": "p_claude",
                     "role": "wizard"},
                    {"command": "handoff_task", "task_id": "t1"}):
        body = client.post(f"/api/council/{room['id']}/commands", json=payload).json()
        assert body["ok"] is False, payload
        assert body["error"]["code"] in known, (payload, body["error"])


def test_a_malformed_body_is_the_one_case_that_is_a_4xx(client_pair):
    client, _ = client_pair
    room = _open(client)["session"]

    assert client.post(f"/api/council/{room['id']}/commands",
                       content="not json").status_code == 400
    assert client.post(f"/api/council/{room['id']}/commands",
                       json=["a", "list"]).status_code == 400
    assert client.post("/api/council", content="{").status_code == 400


# ── rule 8: the flag stops execution and stops nothing else ───────────────

def test_with_the_flag_off_reads_answer_and_execution_refuses(client_pair, monkeypatch):
    """Turning a subsystem off is a decision about what may RUN, never an
    instruction to hide what already happened."""
    client, _ = client_pair
    room = _open(client)["session"]
    client.post(f"/api/council/{room['id']}/messages", json={"content": "before"})
    _settle(client, room["id"])
    monkeypatch.setattr(cr, "enabled", lambda: False)

    # the reading half: everything still answers
    for suffix in ("", "/messages", "/ledger", "/tasks", "/decisions", "/usage"):
        response = client.get(f"/api/council/{room['id']}{suffix}")
        assert response.status_code == 200, f"{suffix} stopped answering"
    assert client.get("/api/council").json()["count"] == 1
    assert client.get(f"/api/council/{room['id']}/messages").json()["count"] >= 2
    assert client.get("/api/council/config").json()["config"]["enabled"] is False

    # the executing half: refused, and it says why
    for suffix, payload in (("/messages", {"content": "after"}),
                            ("/commands", {"command": "pause"})):
        body = client.post(f"/api/council/{room['id']}{suffix}", json=payload).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "council_disabled"
        assert "switched off" in body["error"]["message"]
        assert body["enabled"] is False

    # and nothing ran
    assert len(P.store().list_turns(room["id"])) == 1


def test_the_flag_defaults_to_off():
    """A council multiplies one request by several models and, under
    collaborate, writes to the workspace. That is opt-in."""
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["agent_council"] is False


def test_the_flag_has_a_settings_schema_entry():
    """Parity is enforced by tests/test_agent_settings_schema.py; this states
    the requirement where somebody reading the council will see it."""
    from src.agent_settings_schema import schema_fields, schema_problems

    entry = next(f for f in schema_fields() if f["key"] == "agent_council")
    assert entry["type"] == "bool"
    assert entry["help"].strip()
    assert schema_problems() == []


# ── rule 9: the form gets what it needs ───────────────────────────────────

def test_config_gives_the_form_everything_it_needs_to_open_a_room(client_pair):
    client, _ = client_pair

    config = client.get("/api/council/config").json()["config"]

    for key in ("policies", "roles", "tool_profiles", "policy_tool_ceilings",
                "session_statuses", "default_budgets", "commands", "errors",
                "verdicts", "orchestrator_available", "enabled"):
        assert key in config, f"the form cannot be drawn without {key}"
    assert config["commands"] == list(service_mod.COMMANDS)
    assert config["errors"] == list(service_mod.ERRORS)
    assert "chat" in config["policies"] and "collaborate" in config["policies"]
    assert config["orchestrator_available"] is True, (
        "the shipped build has an orchestrator; a False here means the service "
        "seam cannot reach src/council/orchestrator.py")


def test_config_is_reachable_and_is_not_swallowed_by_the_id_route(client_pair):
    """`/config` is declared before `/{session_id}`; if that order is ever lost
    this endpoint starts answering 404 for a room called `config`."""
    client, _ = client_pair

    assert client.get("/api/council/config").status_code == 200


# ── the gate, and the owner that must never be empty ──────────────────────

def test_the_surface_is_admin_only(tmp_path):
    """Unpatched middleware: a council spends every GPU on the box, so the
    whole surface is admin, exactly like Tournament."""
    P.use_path(str(tmp_path / "council.db"))
    service_mod.reset_service()
    app = FastAPI()
    app.include_router(cr.setup_council_routes())
    try:
        with TestClient(app) as client:
            assert client.get("/api/council").status_code == 403
            assert client.get("/api/council/config").status_code == 403
            assert client.post("/api/council", json={"title": "x"}).status_code == 403
    finally:
        service_mod.reset_service()
        P.use_path(None)


def test_a_caller_with_no_owner_is_refused_rather_than_reading_every_room(
        tmp_path, monkeypatch):
    """`persistence._owner_clause` reads an empty owner as the UNSCOPED query —
    right for recovery, a disclosure of every room on the machine if a route
    ever passed one. So the route stops there instead."""
    P.use_path(str(tmp_path / "council.db"))
    service_mod.reset_service()
    svc = service_mod.CouncilService()
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(cr, "require_admin", lambda request: None)
    monkeypatch.setattr(cr, "effective_user", lambda request: None)
    monkeypatch.setattr(cr, "get_current_user", lambda request: None)
    monkeypatch.setattr(cr, "effective_storage_owner", lambda who: None)
    svc.create(owner="alice", title="Somebody's room", policy="chat",
               participants=TWO_SEATS)
    app = FastAPI()
    app.include_router(cr.setup_council_routes())
    try:
        with TestClient(app) as client:
            response = client.get("/api/council")
            assert response.status_code == 403
            assert "Somebody's room" not in response.text
    finally:
        service_mod.reset_service()
        P.use_path(None)


# ── plumbing shared by the tests that need their own app ──────────────────

class _App:
    """A council app with a chosen invoker, torn down cleanly."""

    def __init__(self, tmp_path, monkeypatch, caller, invoker) -> None:
        self._tmp, self._mp, self._caller, self._invoker = (
            tmp_path, monkeypatch, caller, invoker)
        self._client = None

    def __enter__(self):
        P.use_path(str(self._tmp / "council.db"))
        service_mod.reset_service()
        service_mod.reset_orchestrator()
        council_scheduler.reset_schedulers()
        council_events.reset_streams()
        council_orchestrator.use_packet_builder(None)
        svc = service_mod.CouncilService(invoker=self._invoker)
        self._mp.setattr(service_mod, "service", lambda: svc)
        self._mp.setattr(cr, "require_admin", lambda request: None)
        self._mp.setattr(cr, "effective_user", lambda request: self._caller.who)
        self._mp.setattr(cr, "get_current_user", lambda request: self._caller.who)
        self._mp.setattr(cr, "enabled", lambda: True)
        app = FastAPI()
        app.include_router(cr.setup_council_routes())
        self._client = TestClient(app).__enter__()
        self._client.svc = svc
        return self._client

    def __exit__(self, *exc):
        try:
            self._client.__exit__(*exc)
        finally:
            council_orchestrator.reset_packet_builder()
            service_mod.reset_service()
            service_mod.reset_orchestrator()
            council_scheduler.reset_schedulers()
            council_events.reset_streams()
            P.use_path(None)
        return False


def _app(tmp_path, monkeypatch, caller, invoker) -> _App:
    return _App(tmp_path, monkeypatch, caller, invoker)


@pytest.fixture
def client_pair(council, caller):
    """The client and the mutable identity behind it, together — most tests
    need both and passing them as one tuple keeps the signatures short."""
    return council, caller
