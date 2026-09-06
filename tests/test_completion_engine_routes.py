"""`/api/completion/*` -- the transport (routes/completion_engine_routes.py).

One test per hard rule of this surface. The rules are all about the edge,
because the edge is where an account of a run turns into a leak or a lie:

1.  every one of the eight routes ANSWERS for an admin. A route that is
    written, registered and never exercised is the failure this file exists to
    catch, and this router is unusually easy to get wrong that way: nothing
    here runs the engine, so nothing here is exercised by the agent loop that
    does;
2.  the literal paths -- `/modes`, `/config`, `/settings`, `/diagnostics`,
    `/events` -- each answer their own thing and are NOT swallowed by
    `/{decision_id}`. FastAPI matches in DECLARATION ORDER, so a path parameter
    declared first would answer 404 for every route that exists;
3.  a rejection is a 200 with `{"ok": false, "error": {path, message, code}}`
    and the `code` is a stable token from `service.ERRORS` or `ROUTE_ERRORS`,
    never the text of an exception a client would have to regex. A body that is
    not a JSON object is the one 4xx: it never reached the question, so there is
    no question to answer `ok: false` about;
4.  another owner's decision is 404 and never 403 -- a 403 confirms that a
    guessed id is real, which is half of what was being guessed at. A decision
    quotes the goal somebody typed and names the paths their run touched;
5.  the owner comes from the SESSION. `persistence._owner_clause` reads
    `ANY_OWNER` (None) as the unscoped query, so a route that passed an
    unresolved caller through would hand back every account on the machine;
6.  `shadow` is a three-state string (`true|false|all`) and never a boolean,
    because the third state has to be reachable and a missing boolean would
    default to one of the other two silently. Mixing the two by default would
    ruin the measurement shadow mode exists to produce -- counting what the
    engine WOULD have done beside what it did, in one number, wrong in both
    directions at once and silently, because both halves are well formed;
7.  `/events` polls as JSON and resumes from a cursor without repeating, and
    switches to SSE only when asked. `since` means STRICTLY AFTER, so a page
    that reconnects with the last cursor it saw duplicates nothing;
8.  `reject-improvement` additionally demands a PERSON and a REASON. §12 is
    that a person can say no to an extra; a model able to reject its own
    improvements could quietly delete the record of having been told to do
    them, and §1.8's rule that a refusal must not reappear without new evidence
    is unenforceable if nobody wrote down why;
9.  every handler catches `ContractError` and not `CompletionError` alone.
    `CompletionError` is a SUBCLASS of `ContractError`, and the shared helpers
    in `src/contracts/base.py` -- which parse every field of every payload --
    raise the PARENT. Catching only the child answered 500 to the commonest
    caller mistake there is, in `routes/delta_engine_routes.py`, a few hours
    before this router was written;
10. and the two gates are really wired, which the last two tests prove against
    the UNPATCHED middleware -- the rest of this file stubs them so its tests
    are about this router's own rules.

Seeded through `service.decide_for_turn`, which is the only way a decision is
ever made: there is no `POST /run` on this surface on purpose, and a fixture
that invented one would be testing a door the router deliberately does not have.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.completion_engine_routes as cr
from src.completion_engine import events as completion_events
from src.completion_engine import persistence as P
from src.completion_engine import service as service_mod
from src.completion_engine.contracts import (
    COMPLETION_MODES,
    REJECTION_REASONS,
    STOP_REASONS,
    CompletionError,
    ScopeEnvelope,
)
from src.contracts import base as contracts_base
from src.contracts.base import ContractError
from src.owner_identity import effective_storage_owner

OWNER = "alice"
OTHER = "bob"
INSTRUCTION = "fix the login redirect in src/auth.py"

#: Every code this router may put in an `error`. From the service's own tuple
#: and the router's, never invented here: a test that spelled the token itself
#: would keep passing after the vocabulary moved.
KNOWN_CODES = set(service_mod.ERRORS) | set(cr.ROUTE_ERRORS)


# -- doubles ----------------------------------------------------------------


class _Caller:
    """Who the middleware would have resolved. Mutable so one client can be two
    different people without rebuilding the app."""

    def __init__(self, who: str = OWNER) -> None:
        self.who = who


class _Switches:
    """The two settings, live.

    Both halves of each are patched in the fixture, and they are not the same
    guard: the ROUTE reads `enabled()` to report what this machine is doing, and
    the SERVICE reads its own to decide whether the decision it just made is a
    real one or a measurement. A test that patched only one would flip the
    label on the answer without flipping the answer.
    """

    def __init__(self) -> None:
        self.live = True      # `agent_completion_engine`
        self.shadow = True    # `agent_completion_engine_shadow`


# -- seeding ----------------------------------------------------------------


def _decide(svc, *, owner: str, run_id: str = "run_1") -> dict:
    """One turn, decided. `decide_for_turn` never raises, so this asserts."""
    answer = svc.decide_for_turn(
        owner=owner, instruction=INSTRUCTION, project_id="faustus",
        run_id=run_id, session_id="session_1", rounds_used=2, rounds_budget=10)
    assert answer["ok"] is True, answer
    return answer


def _seed(client, *, shadow: bool = False, run_id: str = "run_1") -> dict:
    """A decision belonging to whoever the client currently is.

    `shadow` is decided by the ENGINE FLAG and never by an argument, because
    that is the only way a shadow decision is ever made: `_decide`'s answer
    carries `shadow: True` exactly when `enabled()` said no.
    """
    client.switches.live = not shadow
    try:
        answer = _decide(client.svc,
                         owner=effective_storage_owner(client.caller.who),
                         run_id=run_id)
    finally:
        client.switches.live = True
    assert answer["shadow"] is shadow, answer["shadow"]
    return answer


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def caller() -> _Caller:
    return _Caller()


@pytest.fixture
def switches() -> _Switches:
    return _Switches()


@pytest.fixture
def completion(tmp_path, monkeypatch, caller, switches):
    """An app with only this router, its own database, its own streams.

    Every global this package keeps is reset on the way in AND on the way out:
    the store memoises a connection, the service memoises itself, and
    `events._STREAMS` keeps a stream per owner FOREVER on purpose -- a closed
    stream that was forgotten would be recreated with its sequence back at 1,
    which inside a test suite reads as `/events` replaying history.

    `require_admin` and `require_human` are stubbed so the tests below are about
    this router's own rules; that both gates are really wired is its own test at
    the bottom, which runs against the unpatched middleware.
    """
    db = tmp_path / "completion_engine.db"
    P.use_path(str(db))
    P.reset_store()
    service_mod.reset_service()
    completion_events.reset_streams()

    svc = service_mod.CompletionEngineService()
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(cr, "require_admin", lambda request: None)
    monkeypatch.setattr(cr, "require_human", lambda request: None)
    monkeypatch.setattr(cr, "effective_user", lambda request: caller.who)
    monkeypatch.setattr(cr, "get_current_user", lambda request: caller.who)
    # Both halves of both switches. See `_Switches`.
    monkeypatch.setattr(cr, "enabled", lambda: switches.live)
    monkeypatch.setattr(service_mod, "enabled", lambda: switches.live)
    monkeypatch.setattr(cr, "shadow_enabled", lambda: switches.shadow)
    monkeypatch.setattr(service_mod, "shadow_enabled", lambda: switches.shadow)

    app = FastAPI()
    app.include_router(cr.setup_completion_engine_routes())
    with TestClient(app) as client:
        client.svc = svc
        client.db = db
        client.caller = caller
        client.switches = switches
        yield client

    service_mod.reset_service()
    completion_events.reset_streams()
    P.reset_store()
    P.use_path(None)


@pytest.fixture
def client_pair(completion, caller):
    """The client and the mutable identity behind it -- most tests need both,
    and one tuple keeps the signatures short."""
    return completion, caller


# -- rule 1: every route answers -------------------------------------------


def test_every_route_of_the_plan_answers_for_an_admin(client_pair):
    """All eight, end to end, against one real decision.

    A route that is written, registered and never exercised is the failure this
    file exists to catch -- and nothing on this surface RUNS the engine, so
    nothing here is exercised by the agent loop that does.
    """
    client, _ = client_pair
    seeded = _seed(client)
    ident = seeded["decision_id"]

    modes = client.get("/api/completion/modes").json()
    assert modes["ok"] is True and list(modes["modes"]) == list(COMPLETION_MODES)

    config = client.get("/api/completion/config").json()
    assert config["ok"] is True and list(config["stop_reasons"]) == list(STOP_REASONS)

    settings = client.get("/api/completion/settings").json()
    assert settings["ok"] is True and "verification_reserve" in settings

    diagnostics = client.get("/api/completion/diagnostics").json()
    assert diagnostics["ok"] is True and diagnostics["counts"]["decisions"] == 1

    events = client.get("/api/completion/events").json()
    assert events["ok"] is True and events["events"], (
        "a decision was recorded and the stream said nothing")

    listed = client.get("/api/completion").json()
    assert [row["id"] for row in listed["decisions"]] == [ident]

    one = client.get(f"/api/completion/{ident}").json()
    assert one["decision"]["id"] == ident
    assert one["closeout"] and one["summary"]

    candidate = one["decision"]["deferred"][0]["id"]
    refused = client.post(f"/api/completion/{ident}/reject-improvement",
                          json={"candidate_id": candidate,
                                "reason": "a person looked; not this quarter"})
    assert refused.status_code == 200, refused.text
    assert refused.json()["ok"] is True
    assert refused.json()["candidate_id"] == candidate


LITERAL_READS = (
    ("/api/completion/modes", "layers", "stop_reasons"),
    ("/api/completion/config", "stop_reasons", ""),
    ("/api/completion/settings", "verification_reserve", ""),
    ("/api/completion/diagnostics", "counts", ""),
    ("/api/completion/events", "events", ""),
)


def test_the_literal_paths_are_not_swallowed_by_the_id_parameter(client_pair):
    """FastAPI matches in DECLARATION ORDER, so `/{decision_id}` declared before
    these five would answer 404 for every one of them -- a route that exists,
    answering that it does not. Each is checked by a key only IT returns, since
    a 200 from the wrong handler is still a 200.

    The 404 for a word that really is not an id is the other half: the
    parameterised route still has to work after the literals are matched first.
    """
    client, _ = client_pair
    ident = _seed(client)["decision_id"]

    for path, key, absent in LITERAL_READS:
        response = client.get(path)
        assert response.status_code == 200, f"{path} answered {response.status_code}"
        body = response.json()
        assert body.get("ok") is True, body
        assert key in body, f"{path} was answered by another handler: {sorted(body)}"
        if absent:
            assert absent not in body, f"{path} was answered by /config"

    assert client.get(f"/api/completion/{ident}").status_code == 200
    assert client.get("/api/completion/not-an-id").status_code == 404


# -- rule 3: a bad value is a 200 with a stable token -----------------------


def test_a_bad_value_is_a_200_with_ok_false_and_a_code_from_the_vocabulary(
        client_pair):
    """The caller asked a question and got an answer.

    Every `code` is a stable token from `service.ERRORS` or `ROUTE_ERRORS`, and
    an empty page is never the answer to a typo: "you have none of those" and
    "that word does not exist" lead to opposite next actions, and only one of
    them is worth telling somebody.
    """
    client, _ = client_pair
    ident = _seed(client)["decision_id"]
    reject = f"/api/completion/{ident}/reject-improvement"
    candidate = client.get(f"/api/completion/{ident}").json()["decision"]["deferred"][0]["id"]

    calls = (
        ("shadow outside the three states", "shadow",
         lambda: client.get("/api/completion", params={"shadow": "maybe"})),
        ("a mode nobody declared", "mode",
         lambda: client.get("/api/completion", params={"mode": "wizard"})),
        ("a stop reason nobody declared", "stop_reason",
         lambda: client.get("/api/completion", params={"stop_reason": "tired"})),
        ("no candidate named", "candidate_id",
         lambda: client.post(reject, json={"reason": "no"})),
        ("a reason that is only spaces", "reason",
         lambda: client.post(reject, json={"candidate_id": candidate,
                                           "reason": "   "})),
        ("a candidate this decision never carried", "candidate_id",
         lambda: client.post(reject, json={"candidate_id": "improvement_nope",
                                           "reason": "no"})),
    )
    for label, path, call in calls:
        response = call()
        assert response.status_code == 200, f"{label} answered {response.status_code}"
        body = response.json()
        assert body["ok"] is False, f"{label}: {body}"
        assert set(body["error"]) >= {"path", "message", "code"}, f"{label}: {body}"
        assert body["error"]["path"] == path, f"{label}: {body['error']}"
        assert body["error"]["code"] in KNOWN_CODES, f"{label}: {body['error']}"
        assert body["error"]["message"], f"{label} refused without saying why"
        assert "decisions" not in body, f"{label} was answered with an empty page"

    # the closed vocabularies name their legal values, and not the typo
    refused = client.get("/api/completion", params={"mode": "wizard"}).json()
    assert refused["error"]["code"] == "unknown_mode"
    assert all(name in refused["error"]["message"] for name in COMPLETION_MODES)
    assert "wizard" not in refused["error"]["message"]
    stopped = client.get("/api/completion", params={"stop_reason": "tired"}).json()
    assert all(name in stopped["error"]["message"] for name in STOP_REASONS)

    # and the same words spelled correctly filter rather than refusing
    kept = client.get("/api/completion", params={"mode": "greedy"}).json()
    assert kept["ok"] is True and len(kept["decisions"]) == 1
    assert client.get("/api/completion", params={"stop_reason": "converged"}
                      ).json()["decisions"] == []


def test_a_body_that_is_not_a_json_object_is_the_one_4xx(client_pair):
    """A body that is not an object never reached the question, so there is no
    question to answer `ok: false` about. The one write on this surface is the
    only place a body is read at all."""
    client, _ = client_pair
    ident = _seed(client)["decision_id"]
    reject = f"/api/completion/{ident}/reject-improvement"

    assert client.post(reject, json=["a", "list"]).status_code == 400
    assert client.post(reject, json="a string").status_code == 400
    assert client.post(reject, json=7).status_code == 400
    # and an ABSENT body is not malformed: it is a request missing a field,
    # which is the 200-with-`ok: false` case and not the 4xx one.
    empty = client.post(reject)
    assert empty.status_code == 200 and empty.json()["ok"] is False


# -- rule 4: somebody else's id is 404, never 403 --------------------------


def test_another_owners_decision_is_404_and_never_403(client_pair):
    """A 403 confirms that an id somebody guessed is real, which is half of
    what they were guessing at. A decision quotes the goal its owner typed and
    names the files their run touched, so the absent id and the stranger's id
    have to answer identically -- byte for byte, not merely with the same
    status."""
    client, caller = client_pair
    seeded = _seed(client)
    ident = seeded["decision_id"]
    candidate = client.get(f"/api/completion/{ident}").json()["decision"]["deferred"][0]["id"]

    caller.who = OTHER
    reads = (client.get(f"/api/completion/{ident}"),
             client.post(f"/api/completion/{ident}/reject-improvement",
                         json={"candidate_id": candidate, "reason": "mine now"}))
    for response in reads:
        assert response.status_code == 404, (
            f"{response.request.method} {response.request.url.path} answered "
            f"{response.status_code}")

    absent = client.get("/api/completion/decision_nothing_here")
    assert absent.status_code == 404
    assert absent.json() == reads[0].json(), "the stranger's id is distinguishable"
    assert client.get("/api/completion", params={"shadow": "all"}).json()["decisions"] == []
    assert client.get("/api/completion/diagnostics").json()["counts"]["decisions"] == 0

    caller.who = OWNER
    assert client.get(f"/api/completion/{ident}").status_code == 200


# -- rule 5: the owner comes from the session ------------------------------


def test_a_caller_with_no_owner_is_refused_rather_than_reading_every_decision(
        client_pair, monkeypatch):
    """`persistence._owner_clause` reads `ANY_OWNER` (None) as the UNSCOPED
    query -- right for a sweep, a disclosure of every account on the machine if
    a route ever passed an unresolved caller through. So the route stops there,
    with a 403 that is about the CALLER and not about a row: nothing has been
    named yet, so there is no id to protect by answering 404.

    The three vocabulary endpoints keep answering, and that is not an oversight:
    `/modes`, `/config` and `/settings` describe what this SYSTEM does and name
    nobody's data.
    """
    client, _ = client_pair
    ident = _seed(client)["decision_id"]
    monkeypatch.setattr(cr, "effective_user", lambda request: None)
    monkeypatch.setattr(cr, "get_current_user", lambda request: None)
    monkeypatch.setattr(cr, "effective_storage_owner", lambda who: None)

    for path in ("/api/completion", "/api/completion/diagnostics",
                 "/api/completion/events", f"/api/completion/{ident}"):
        assert client.get(path).status_code == 403, path
    assert client.post(f"/api/completion/{ident}/reject-improvement",
                       json={"candidate_id": "improvement_1",
                             "reason": "anybody"}).status_code == 403

    for path in ("/api/completion/modes", "/api/completion/config",
                 "/api/completion/settings"):
        assert client.get(path).status_code == 200, path


# -- rule 6: shadow is three-state, and the default never mixes ------------


def test_shadow_is_three_state_and_the_default_never_mixes_the_two(client_pair):
    """`true`, `false`, `all` -- a string and not a boolean, because the third
    state has to be reachable and a missing boolean would default to one of the
    other two silently.

    What the default protects is the whole point of shadow mode: the engine
    records what it WOULD have done, and a page that listed those beside what it
    actually did would be wrong in both directions at once -- the measurement
    inflates the real numbers, and the real work makes the measurement look like
    it shipped. Both halves are well-formed decisions, so the ruin is silent.
    """
    client, _ = client_pair
    real = _seed(client, shadow=False, run_id="run_real")["decision_id"]
    shadow = _seed(client, shadow=True, run_id="run_shadow")["decision_id"]
    assert real != shadow

    def ids(**params):
        body = client.get("/api/completion", params=params).json()
        assert body["ok"] is True, body
        return {row["id"] for row in body["decisions"]}

    assert ids() == {real}, "the default listed a measurement as work that happened"
    assert ids(shadow="false") == {real}
    assert ids(shadow="true") == {shadow}
    assert ids(shadow="all") == {real, shadow}

    # `all` is the diagnostic answer, and diagnostics reports the split rather
    # than the sum for the same reason: one total counts runs that half happened.
    counts = client.get("/api/completion/diagnostics").json()["counts"]
    assert counts["real_decisions"] == 1 and counts["shadow_decisions"] == 1
    assert counts["decisions"] == 2

    # by ID there is nothing to mix -- the caller already named the row, and
    # `shadow` on the answer says which kind it is.
    assert client.get(f"/api/completion/{shadow}").json()["decision"]["shadow"] is True
    assert client.get(f"/api/completion/{real}").json()["decision"]["shadow"] is False


# -- rule 7: the stream polls, and resumes without repeating ---------------


def test_events_polls_as_json_and_resumes_from_a_cursor_without_repeating(
        client_pair):
    """`since` means STRICTLY AFTER, so a page that reconnects with the last
    cursor it saw duplicates nothing, and a gap is announced rather than hidden:
    a silent hole here is a rejected candidate nobody knows was refused, which
    is the same thing as a budget nobody knows to raise."""
    client, _ = client_pair
    _seed(client)

    first = client.get("/api/completion/events").json()
    assert first["ok"] is True and first["gap"] is False
    assert first["events"], "a decision was recorded and the stream said nothing"
    names = [row["name"] for row in first["events"]]
    assert "completion_contract_created" in names
    assert "completion_decision_recorded" in names
    assert first["cursor"] == first["events"][-1]["seq"]

    again = client.get("/api/completion/events",
                       params={"since": first["cursor"]}).json()
    assert again["events"] == [], "the cursor replayed what the caller had seen"
    assert again["cursor"] == first["cursor"], (
        "an empty page reset the cursor, which is how a poller starts replaying")

    # `Last-Event-ID` is the same cursor by another name -- it is what a
    # browser's `EventSource` sends on its own when it reconnects, and a stream
    # that ignored it would replay the whole buffer to every reconnecting page.
    resumed = client.get("/api/completion/events",
                         headers={"last-event-id": str(first["cursor"])}).json()
    assert resumed["events"] == []
    assert client.get("/api/completion/events", params={"limit": 1}
                      ).json()["events"][0]["seq"] == first["events"][0]["seq"]


def test_events_switches_to_sse_only_when_asked_and_the_frames_are_unnamed(
        client_pair, monkeypatch):
    """Polling is the default; `stream=1` or an `Accept` of `text/event-stream`
    switches. The frames are UNNAMED with the name inside the JSON, which is the
    dialect the rest of this repository's streams speak: a NAMED frame never
    reaches a page's `onmessage`, so a client written against the unnamed
    dispatch goes silently deaf on a named one -- no error, no log, no clue.

    The two constants are shortened rather than mocked so the real generator
    runs: the ceiling is what ends it, and a test that patched the generator
    instead would prove nothing about the loop that ships.
    """
    client, _ = client_pair
    _seed(client)
    monkeypatch.setattr(cr, "STREAM_MAX_S", 0.3)
    monkeypatch.setattr(cr, "STREAM_HEARTBEAT_S", 0.05)

    for label, call in (
        ("stream=1", lambda: client.get("/api/completion/events",
                                        params={"stream": 1})),
        ("Accept", lambda: client.get("/api/completion/events",
                                      headers={"accept": "text/event-stream"})),
    ):
        response = call()
        assert response.status_code == 200, label
        assert response.headers["content-type"].startswith("text/event-stream"), label
        assert response.headers["cache-control"] == "no-cache", label

        frames = [f for f in response.text.split("\n\n") if f.strip()]
        named = [f for f in frames if f.startswith("event:")]
        assert named == frames[-1:], (
            f"{label}: a named frame that is not the terminator; a page's "
            f"onmessage will never see it")
        assert named[0].startswith("event: end"), label
        payloads = [json.loads(f[len("data: "):]) for f in frames
                    if f.startswith("data: ")]
        assert any(row["name"] == "completion_decision_recorded" for row in payloads), (
            f"{label}: the decision never reached the stream")
        assert all(row["owner"] == effective_storage_owner(OWNER) for row in payloads)


# -- rule 8: a person, with a reason, and nothing is destroyed -------------


def test_reject_improvement_demands_a_reason_and_destroys_nothing(client_pair):
    """§1.8: a rejected opportunity must not reappear without new evidence, and
    a refusal nobody justified is indistinguishable next month from one nobody
    meant. So the reason is mandatory and a candidate this decision never
    carried is `not_found` -- named as such rather than accepted quietly, since
    a rejection filed against nothing is a rejection that never happened.

    And nothing is destroyed: the candidate keeps its evidence, its score and
    its place in the account. The interesting question later is not what ran --
    it is what was offered and turned down.
    """
    client, _ = client_pair
    ident = _seed(client)["decision_id"]
    before = client.get(f"/api/completion/{ident}").json()["decision"]
    candidate = before["deferred"][0]

    missing = client.post(f"/api/completion/{ident}/reject-improvement",
                          json={"candidate_id": "improvement_never_proposed",
                                "reason": "no thanks"}).json()
    assert missing["ok"] is False
    assert missing["error"]["code"] == "not_found"
    assert missing["error"]["path"] == "candidate_id"

    unjustified = client.post(f"/api/completion/{ident}/reject-improvement",
                              json={"candidate_id": candidate["id"],
                                    "reason": ""}).json()
    assert unjustified["ok"] is False and unjustified["error"]["path"] == "reason"

    accepted = client.post(f"/api/completion/{ident}/reject-improvement",
                           json={"candidate_id": candidate["id"],
                                 "reason": "the module is being replaced in June"})
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["ok"] is True
    assert body["decision_id"] == ident
    assert body["recorded"] == "the module is being replaced in June"
    assert body["actor"] == effective_storage_owner(OWNER), (
        "the actor came from somewhere other than the session")

    after = client.get(f"/api/completion/{ident}").json()["decision"]
    kept = [row for row in after["deferred"] if row["id"] == candidate["id"]][0]
    assert kept["evidence_refs"] == candidate["evidence_refs"]
    assert kept["expected_value"] == candidate["expected_value"]
    assert kept["title"] == candidate["title"]
    assert len(after["deferred"]) == len(before["deferred"]), (
        "a refusal deleted the record of the improvement having been offered")


def test_a_rejection_is_actually_recorded(client_pair):
    """The half of §12 that outlives the HTTP response. Regression guard.

    A person said no, once, with a reason. If that only ever existed in a
    response body then the same improvement comes back next round with the same
    evidence, the person says no again, and the engine has learned nothing --
    which is precisely the loop §1.8 was written to close. This shipped that
    way for a few hours: the endpoint answered `{"ok": true, "recorded": ...}`
    and stored nothing, because `persistence.py` had no writer for a human
    rejection at all.

    What it does now, and the shape is deliberate: the refusal is a row of its
    own in `completion_refusals`, keyed by `candidate.key`, and the DECISION is
    not touched. Rewriting the decision would have been the obvious fix and the
    wrong one -- the decision is the record of what one turn concluded, and the
    interesting question a month later is not what ran but what was offered and
    turned down. Both facts survive because they are stored separately.
    """
    client, _ = client_pair
    ident = _seed(client)["decision_id"]
    before = client.get(f"/api/completion/{ident}").json()["decision"]
    candidate = before["deferred"][0]

    answer = client.post(f"/api/completion/{ident}/reject-improvement",
                         json={"candidate_id": candidate["id"],
                               "reason": "the module is being replaced in June"})
    body = answer.json()
    assert body["ok"] is True and body["stored"] is True

    store = client.svc.store()
    owner = effective_storage_owner(OWNER)
    rows = store.refusals(owner=owner, decision_id=ident)
    assert len(rows) == 1, "the endpoint answered `recorded` and recorded nothing"
    assert rows[0]["reason"] == "the module is being replaced in June"
    assert rows[0]["candidate_id"] == candidate["id"]

    # Keyed by `key`, not by `id`. The id belongs to this decision; the same
    # improvement rediscovered tomorrow carries a new one, and an id key would
    # enforce the refusal exactly once -- against the row the person was looking
    # at, and never against the thing they were refusing.
    assert rows[0]["candidate_key"] != candidate["id"]
    assert rows[0]["candidate_key"] in store.declined_keys(owner=owner)

    # Non-destructive, and this is the assertion that would fail if someone
    # "simplified" this by flipping the candidate's status in place.
    after = client.get(f"/api/completion/{ident}").json()["decision"]
    kept = [row for row in after["deferred"] if row["id"] == candidate["id"]][0]
    assert kept["evidence_refs"] == candidate["evidence_refs"]
    assert len(after["deferred"]) == len(before["deferred"]), (
        "a refusal deleted the record of the improvement having been offered")


def test_every_stored_rejection_carries_its_reason(client_pair):
    """A refusal in the record always says why. Regression guard.

    Found in the browser and nowhere else, because for six seeded turns nothing
    was ever refused and the list was always empty. `_decide` built the
    decision's `rejected` list as a comprehension over the frontier's entries --
    `e.candidate.to_dict()` -- instead of calling `ranked.rejected()`, which is
    the method whose entire job is to move the reason and the status OFF the
    entry and ONTO the candidate, because the candidate is what outlives the
    frontier. So every rejection was stored as a bare `candidate` with no
    reason, and the screen drew it as "nobody recorded why": the one sentence a
    closed vocabulary exists to make impossible.
    """
    client, _ = client_pair
    ident = _seed(client)["decision_id"]
    detail = client.get(f"/api/completion/{ident}").json()["decision"]
    refused = detail["rejected"]
    if not refused:
        # The seed refuses nothing today. Prove the shape against the frontier
        # itself rather than skipping: a green test that asserted over an empty
        # list is how this went unseen for six turns.
        from src.completion_engine import frontier as frontier_mod
        from src.completion_engine.contracts import ImprovementCandidate
        entry = frontier_mod.FrontierEntry(
            candidate=ImprovementCandidate.parse(
                {"id": "improvement_x", "layer": "bonus", "title": "a thing",
                 "category": "coverage"}, "candidate"),
            utility=0.0, relation="adjacent", admitted=False, reason="declined",
            terms={})
        rebuilt = frontier_mod.Frontier(entries=(entry,)).rejected()
        refused = [c.to_dict() for c in rebuilt]

    assert refused, "nothing to check, and an empty list proves nothing"
    for row in refused:
        assert row.get("status") == "rejected", (
            f"stored as {row.get('status')!r}: the refusal lost its status on "
            f"the way into the decision")
        assert row.get("rejection_reason") in REJECTION_REASONS, (
            f"stored with reason {row.get('rejection_reason')!r}, which the "
            f"closed vocabulary does not contain -- the screen will draw this "
            f"as 'nobody recorded why'")


def test_a_refused_improvement_is_not_admitted_again(client_pair):
    """§1.8, the whole point of storing it: a person should not say no twice.

    The refusal is enforced in `frontier._admit`, which is where blocking
    decisions live -- above scoring, where nothing can outrank it. A filter
    applied after the frontier would have been a refusal a high enough value
    could argue with.
    """
    from src.completion_engine import frontier as frontier_mod

    client, _ = client_pair
    ident = _seed(client)["decision_id"]
    stored = client.svc.store().get_decision(ident,
                                             owner=effective_storage_owner(OWNER))
    candidate = stored.deferred[0]

    ok, reason = frontier_mod._admit(candidate, ScopeEnvelope(owner=OWNER),
                                     declined=(candidate.key,))
    assert ok is False and reason == "declined", (
        "a person's refusal did not survive as far as admission")
    assert "declined" in REJECTION_REASONS, (
        "`declined` must be a member of the closed vocabulary, or the closeout "
        "cannot name it and the projection cannot store it")


# -- rule 9: the PARENT is what the shared helpers raise -------------------


def _a_shared_helper_rejection() -> None:
    """Raise what `src/contracts/base.py` actually raises.

    Constructed by CALLING a shared helper rather than by building the
    exception, because the whole point of this test is which class those
    helpers use -- an assertion against a hand-made `ContractError` would keep
    passing on the day they started raising something else.
    """
    contracts_base.text({"cursor": 7}, "cursor", "decisions")


def test_a_rejection_from_a_shared_contract_helper_is_a_200_and_never_a_500(
        client_pair, monkeypatch):
    """Every handler catches `ContractError`, not `CompletionError` alone.

    `CompletionError` is a SUBCLASS of `ContractError`, and the shared helpers
    in `src/contracts/base.py` -- `as_mapping`, `text`, `one_of`,
    `reject_unknown`, `flag`, `whole`, `text_list`, which parse every field of
    every payload this package stores -- raise the PARENT. `except
    CompletionError` does not catch a `ContractError`; the inheritance runs the
    other way. That exact mistake shipped in `routes/delta_engine_routes.py` and
    answered 500 to the commonest caller mistake there is.

    The service is the seam because that is where a contract rejection reaches
    these three handlers: nothing on this surface parses a payload itself --
    every one of them is a read, and what a read parses is what is already
    stored.
    """
    client, _ = client_pair
    _seed(client)
    assert issubclass(CompletionError, ContractError)
    assert not issubclass(ContractError, CompletionError), (
        "the direction of this inheritance is the whole bug")

    ident = client.get("/api/completion").json()["decisions"][0]["id"]
    monkeypatch.setattr(client.svc, "list", lambda **kw: _a_shared_helper_rejection())
    monkeypatch.setattr(client.svc, "get", lambda *a, **kw: _a_shared_helper_rejection())
    monkeypatch.setattr(client.svc, "diagnostics",
                        lambda **kw: _a_shared_helper_rejection())

    for path in ("/api/completion", f"/api/completion/{ident}",
                 "/api/completion/diagnostics"):
        response = client.get(path)
        assert response.status_code == 200, (
            f"{path} answered {response.status_code}; a handler that caught only "
            f"CompletionError would let the parent escape as a 500")
        body = response.json()
        assert body["ok"] is False, f"{path}: {body}"
        assert body["error"]["path"] == "decisions.cursor", f"{path}: {body}"
        assert body["error"]["code"] in KNOWN_CODES, f"{path}: {body}"
        assert body["error"]["message"], f"{path} refused without saying why"


def test_one_damaged_row_does_not_cost_the_whole_page(client_pair):
    """A payload this build cannot parse is a fact about one row. Regression guard.

    Written as bytes in the database rather than a mocked exception, because
    that is how it happens: a row saved by a build whose vocabulary was wider,
    read back by one whose vocabulary is narrower.

    This failed when it was written. `_scope`, `_contract` and `_decision` in
    `src/completion_engine/persistence.py` caught `CompletionError` while the
    `parse()` they wrap raises the PARENT `ContractError` -- the shared helpers
    in `src/contracts/base.py` do -- which `except CompletionError` cannot
    catch. The same child-for-parent mistake as rule 9, one layer down, and it
    made two promises false at once: the docstring above those readers ("one
    damaged row must not make a whole page unreadable") and the file's rule
    that reads never raise. One row written by a newer build took
    `GET /api/completion` with it, and on the turn path it would have let the
    account of a run break the run it was accounting for.
    """
    client, _ = client_pair
    kept = _seed(client, run_id="run_kept")["decision_id"]
    damaged = _seed(client, run_id="run_damaged")["decision_id"]

    conn = sqlite3.connect(str(client.db))
    conn.execute("UPDATE completion_decisions SET payload = ? WHERE id = ?",
                 [json.dumps({"id": damaged, "owner": effective_storage_owner(OWNER),
                              "mode": "wizard"}), damaged])
    conn.commit()
    conn.close()
    P.reset_store()

    listed = client.get("/api/completion")
    assert listed.status_code == 200
    body = listed.json()
    assert body["ok"] is True, body
    assert [row["id"] for row in body["decisions"]] == [kept]


# -- the gates, against the unpatched middleware ---------------------------


def test_the_surface_is_admin_only(tmp_path):
    """A decision quotes the goal somebody typed, names the paths their run
    touched and lists what it refused to do about them, so the whole surface is
    admin -- the three vocabulary reads included, since they describe what this
    box would do with any of it."""
    P.use_path(str(tmp_path / "completion_engine.db"))
    service_mod.reset_service()
    app = FastAPI()
    app.include_router(cr.setup_completion_engine_routes())
    try:
        with TestClient(app) as client:
            for path in ("/api/completion", "/api/completion/modes",
                         "/api/completion/config", "/api/completion/settings",
                         "/api/completion/diagnostics", "/api/completion/events",
                         "/api/completion/decision_anything"):
                assert client.get(path).status_code == 403, path
            assert client.post("/api/completion/decision_anything/reject-improvement",
                               json={"candidate_id": "improvement_1",
                                     "reason": "no"}).status_code == 403
    finally:
        service_mod.reset_service()
        P.use_path(None)


def test_reject_improvement_additionally_demands_a_person(tmp_path, monkeypatch,
                                                          switches):
    """`require_admin` accepts the in-process internal token so the agent's own
    loopback calls reach admin routes. For this one write that would be a hole
    with the shape of a feature: a model able to call the endpoint its own page
    calls could reject every improvement it was told to make and answer that a
    person had said no.
    """
    # The token is minted once per process, and the gate compares against the
    # copy held by the module `cr.require_admin` actually came from. Importing
    # it by name here instead would be a SECOND copy, and any test anywhere in
    # the suite that reloads `core.middleware` -- which some do, to check module
    # identity -- leaves the two disagreeing. The symptom is this test failing
    # on its first assertion with 403 in a full run and passing on its own,
    # which is a story about `sys.modules` and not about the gate.
    import sys

    middleware = sys.modules[cr.require_admin.__module__]
    INTERNAL_TOOL_HEADER = middleware.INTERNAL_TOOL_HEADER
    INTERNAL_TOOL_TOKEN = middleware.INTERNAL_TOOL_TOKEN

    P.use_path(str(tmp_path / "completion_engine.db"))
    P.reset_store()
    service_mod.reset_service()
    completion_events.reset_streams()
    svc = service_mod.CompletionEngineService()
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(cr, "effective_user", lambda request: OWNER)
    monkeypatch.setattr(cr, "get_current_user", lambda request: OWNER)
    monkeypatch.setattr(cr, "enabled", lambda: switches.live)
    monkeypatch.setattr(service_mod, "enabled", lambda: switches.live)

    owner = effective_storage_owner(OWNER)
    ident = _decide(svc, owner=owner)["decision_id"]
    app = FastAPI()
    app.include_router(cr.setup_completion_engine_routes())
    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    try:
        with TestClient(app) as client:
            # the token reaches every read, which is what it is for
            assert client.get("/api/completion", headers=headers).status_code == 200
            one = client.get(f"/api/completion/{ident}", headers=headers)
            assert one.status_code == 200, one.text
            candidate = one.json()["decision"]["deferred"][0]

            refused = client.post(
                f"/api/completion/{ident}/reject-improvement",
                json={"candidate_id": candidate["id"],
                      "reason": "the model decided this was not worth doing"},
                headers=headers)
            assert refused.status_code == 403
            assert "person" in refused.text

            # and the account is exactly as the engine left it
            after = client.get(f"/api/completion/{ident}", headers=headers).json()
            assert after["decision"]["deferred"] == one.json()["decision"]["deferred"]
    finally:
        service_mod.reset_service()
        completion_events.reset_streams()
        P.reset_store()
        P.use_path(None)
