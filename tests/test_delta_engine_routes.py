"""`/api/deltas/*` -- the transport (routes/delta_engine_routes.py).

One test per hard rule of the surface, and the rules are all about the edge,
because the edge is where a delta leaks or lies:

1. the literal paths (`/config`, `/profiles`, `/extractors/status`,
   `/diagnostics`, `/intent/compile`, `/events`) all answer their own thing and
   are NOT swallowed by `/{delta_id}` -- FastAPI matches in declaration order
   and a path parameter declared first would answer 404 for every route that
   exists;
2. a rejection is a 200 with `{"ok": false, "error": {path, message, code}}`
   and the `code` comes from `service.ERRORS` or `ROUTE_ERRORS`; a body that is
   not a JSON object is the one 4xx;
3. another owner's id is 404 and never 403 -- a 403 confirms that a guessed id
   is real, which is half of what was being guessed at;
4. the owner comes from the session: an `owner` in the body is discarded and
   named back in `ignored_fields`, and a caller with no session is refused
   rather than handed the unscoped query;
5. a `domain` or an `assessment` outside the vocabulary is a 200 naming the
   legal values, never an empty list -- an empty list is indistinguishable
   from "you have none of those", and a typo that reads as good news is what a
   closed vocabulary exists to prevent;
6. the flag stops the comparison and never the read;
7. `/events` polls as JSON and resumes from a cursor without repeating;
8. `/{id}/reclassify` additionally demands a PERSON: reclassifying is how a
   `regression` becomes a `required` change, so a model able to call it could
   reinterpret its way out of every finding against it.

The adapter is a double registered through `registry.register()` and released
in the fixture, and its findings are addressed WITHOUT the `file:` prefix so
that `service._with_proof` stays a no-op -- a `file:`-addressed assertion would
send this file into `src/changesets.py` to prove something about a router.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.delta_engine_routes as dr
from src.delta_engine import coverage as coverage_mod
from src.delta_engine import events as delta_events
from src.delta_engine import persistence as P
from src.delta_engine import registry, sources
from src.delta_engine import service as service_mod
from src.delta_engine.adapters.base import Element, Extraction, Finding, Snapshot
from src.delta_engine.adapters.code import CODE_TREE_MEDIA_TYPE
from src.delta_engine.contracts import ASSESSMENTS, COVERAGE_DIMENSIONS, DOMAINS

DOMAIN = "code"
FULL_COVERAGE = {name: 1.0 for name in COVERAGE_DIMENSIONS}


# -- doubles ----------------------------------------------------------------


class _Caller:
    """Who the middleware would have resolved. Mutable so one client can be two
    different people without rebuilding the app."""

    def __init__(self, who: str = "alice") -> None:
        self.who = who


class _Adapter:
    """A `code` adapter that answers instantly. No parser, no disk, no budget."""

    domain = DOMAIN
    version = "1"

    def __init__(self) -> None:
        self.snapshots = 0
        self.comparisons = 0

    def available(self) -> bool:
        return True

    def snapshot(self, revision, *, scope):
        self.snapshots += 1
        return Snapshot(revision=revision, tier="parser",
                        elements=(Element(key="src/auth.py#consume_state",
                                          kind="symbol", hash=revision.hash),))

    def compare(self, source, target, *, scope):
        self.comparisons += 1
        return Extraction(
            findings=(Finding(path="src/auth.py#consume_state", operation="modified",
                              before="token", after="token * 2",
                              method="python_ast", tier="parser", confidence="high"),),
            coverage=coverage_mod.build(source_readable=True, target_readable=True,
                                        dimensions=FULL_COVERAGE),
            extractor_versions={"probe": self.version},
        )

    def check_invariants(self, intent, source, target, *, scope):
        return ()


# -- seeding ----------------------------------------------------------------


def _tree(files):
    """An immutable revision holding `{path: text}`. No disk, real sha256."""
    return sources.stash(dict(files), media_type=CODE_TREE_MEDIA_TYPE)


SOURCE = {"src/auth.py": "def consume_state(token):\n    return token\n"}
TARGET = {"src/auth.py": "def consume_state(token):\n    return token * 2\n"}


def _body(**over):
    body = {"domain": DOMAIN, "source": _tree(SOURCE).to_dict(),
            "target": _tree(TARGET).to_dict()}
    body.update(over)
    return body


def _create(client, **over):
    answer = client.post("/api/deltas", json=_body(**over))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["ok"] is True, body
    return body


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def caller() -> _Caller:
    return _Caller("alice")


@pytest.fixture
def adapter() -> _Adapter:
    return _Adapter()


@pytest.fixture
def deltas(tmp_path, monkeypatch, caller, adapter):
    """An app with only this router, its own database, and nothing shared.

    `require_admin` and `require_human` are stubbed so the tests below are
    about this router's own rules; that both gates are really wired is its own
    test at the bottom, which runs against the unpatched middleware.
    """
    P.use_path(str(tmp_path / "delta_engine.db"))
    P.reset_store()
    service_mod.reset_service()
    delta_events.reset_streams()
    registry.register(lambda: adapter)

    svc = service_mod.DeltaEngineService()
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(dr, "require_admin", lambda request: None)
    monkeypatch.setattr(dr, "require_human", lambda request: None)
    monkeypatch.setattr(dr, "effective_user", lambda request: caller.who)
    monkeypatch.setattr(dr, "get_current_user", lambda request: caller.who)
    # Both halves of the flag, because there are two readers of it and they are
    # not the same guard: the route refuses before the service is called, and
    # the service refuses again for every caller that is not a route. A test
    # that patched only one would pass while the other still said no.
    monkeypatch.setattr(dr, "enabled", lambda: True)
    monkeypatch.setattr(service_mod, "enabled", lambda: True)

    app = FastAPI()
    app.include_router(dr.setup_delta_engine_routes())
    with TestClient(app) as client:
        client.svc = svc
        yield client

    registry.reset()
    service_mod.reset_service()
    delta_events.reset_streams()
    P.reset_store()
    P.use_path(None)


@pytest.fixture
def client_pair(deltas, caller):
    """The client and the mutable identity behind it -- most tests need both,
    and one tuple keeps the signatures short."""
    return deltas, caller


# -- rule 1: every route answers, and the literal paths stay literal --------


def test_every_route_of_the_plan_answers_for_an_admin(client_pair):
    """The whole surface, end to end. A route that is written, registered and
    never exercised is the failure this file exists to catch."""
    client, _ = client_pair
    created = _create(client)
    ident = created["delta"]["id"]
    assertion = created["delta"]["assertions"][0]

    assert client.get("/api/deltas").json()["deltas"][0]["id"] == ident
    assert client.get(f"/api/deltas/{ident}").json()["delta"]["id"] == ident
    assert client.get(f"/api/deltas/{ident}/evidence").json()["ok"] is True
    rerun = client.post(f"/api/deltas/{created['request_id']}/run", json={})
    assert rerun.status_code == 200 and rerun.json()["cached"] is True

    revised = client.post(f"/api/deltas/{ident}/reclassify",
                          json={"assertion_id": assertion["id"],
                                "classification": "regression",
                                "reason": "a person looked and this is a bug"})
    assert revised.status_code == 200, revised.text
    successor = revised.json()["delta"]["id"]
    assert successor != ident, "a reinterpretation is a new revision, not an edit"

    retired = client.post(f"/api/deltas/{successor}/invalidate",
                          json={"reason": "the target revision moved"})
    assert retired.json() == {"ok": True, "superseded": True}
    assert client.get("/api/deltas").json()["deltas"] == []
    assert client.get(f"/api/deltas/{successor}").status_code == 200, (
        "a retired conclusion is still readable by id")


LITERAL_READS = (
    ("/api/deltas/config", "domains"),
    ("/api/deltas/profiles", "profiles"),
    ("/api/deltas/extractors/status", "extractors"),
    ("/api/deltas/diagnostics", "counts"),
    ("/api/deltas/events", "events"),
)


def test_the_literal_paths_are_not_swallowed_by_the_id_parameter(client_pair):
    """FastAPI matches in DECLARATION ORDER, so `/{delta_id}` declared before
    these would answer 404 for every one of them -- a route that exists,
    answering that it does not. The 404 for a word that really is not an id is
    the other half: the parameterised route still has to work."""
    client, _ = client_pair
    created = _create(client)

    for path, key in LITERAL_READS:
        response = client.get(path)
        assert response.status_code == 200, f"{path} answered {response.status_code}"
        body = response.json()
        assert body.get("ok") is True, body
        assert key in body, f"{path} was answered by another handler: {sorted(body)}"

    compiled = client.post("/api/deltas/intent/compile",
                           json={"domain": DOMAIN,
                                 "text": "src/auth.py#consume_state changes"})
    assert compiled.status_code == 200, compiled.text
    assert compiled.json()["intent"]["domain"] == DOMAIN

    assert client.get(f"/api/deltas/{created['delta']['id']}").status_code == 200
    assert client.get("/api/deltas/not-an-id").status_code == 404


# -- rule 2: a bad value is a 200 with a stable token, a bad body is a 4xx ---


def test_a_bad_value_is_a_200_with_ok_false_and_a_code_from_the_vocabulary(client_pair):
    """The caller asked a question and got an answer. Every `code` is a stable
    token from `service.ERRORS` or `ROUTE_ERRORS`, never the text of an
    exception a client would have to regex."""
    client, _ = client_pair
    created = _create(client)
    ident = created["delta"]["id"]
    known = set(service_mod.ERRORS) | set(dr.ROUTE_ERRORS)

    calls = (
        ("unknown domain, listing",
         lambda: client.get("/api/deltas", params={"domain": "wizard"})),
        ("unknown assessment",
         lambda: client.get("/api/deltas", params={"assessment": "great"})),
        ("no domain to compile",
         lambda: client.post("/api/deltas/intent/compile", json={"text": "hello"})),
        ("reclassify with no reason",
         lambda: client.post(f"/api/deltas/{ident}/reclassify",
                             json={"assertion_id": created["delta"]["assertions"][0]["id"],
                                   "classification": "regression", "reason": "  "})),
    )
    for label, call in calls:
        response = call()
        assert response.status_code == 200, f"{label} answered {response.status_code}"
        body = response.json()
        assert body["ok"] is False, f"{label}: {body}"
        assert set(body["error"]) >= {"path", "message", "code"}, f"{label}: {body}"
        assert body["error"]["code"] in known, f"{label}: {body['error']}"
        assert body["error"]["message"], f"{label} refused without saying why"


def test_a_body_that_is_not_a_json_object_is_the_one_4xx(client_pair):
    """A body that is not an object never reached the question, so there is no
    question to answer `ok: false` about."""
    client, _ = client_pair

    assert client.post("/api/deltas", json=["a", "list"]).status_code == 400
    assert client.post("/api/deltas", json="a string").status_code == 400
    assert client.post("/api/deltas", json=7).status_code == 400
    assert client.post("/api/deltas/intent/compile", json=["a", "list"]).status_code == 400
    assert client.post("/api/deltas/not-an-id/run", json=["a", "list"]).status_code == 400


# This began as an xfail against a real bug, and the diagnosis is kept because
# it is the kind that comes back: every handler in the router caught
# `DeltaError`, and the rejections raised by the SHARED contract helpers in
# `src/contracts/base.py` -- `as_mapping`, `text`, `one_of`, `reject_unknown`,
# `timestamp`, `flag`, `whole`, `text_list` -- are the base `ContractError`,
# which `except DeltaError` does not catch (`DeltaError` is its subclass, not
# the other way round). So the commonest caller mistake of all -- a missing or
# malformed `source` -- escaped as an unhandled exception and answered 500,
# instead of the 200 rejection this file's own docstring promises. FIXED: every
# handler now catches `ContractError`, and `test_delta_engine_wiring.py` asserts
# that no handler goes back to the subclass.
# Reproduced through the router because that is where the convention lives; the
# same body raises out of `service.create` for a non-HTTP caller too.
def test_a_body_the_contract_refuses_is_a_200_rejection_like_every_other(client_pair):
    """A missing `source` is a caller mistake about a field, which is the case
    the 200-with-`ok: false` convention exists for. `4xx` is reserved for a body
    that could not be read at all, and an unhandled exception is reserved for
    nothing."""
    client, _ = client_pair

    refused = client.post("/api/deltas", json={"domain": DOMAIN})

    assert refused.status_code == 200
    body = refused.json()
    assert body["ok"] is False
    assert body["error"]["path"].endswith("source")
    assert body["error"]["code"] in set(service_mod.ERRORS) | set(dr.ROUTE_ERRORS)


# -- rule 3: somebody else's id is 404, never 403 ---------------------------


def test_another_owners_delta_is_404_and_never_403(client_pair):
    """A 403 confirms that an id somebody guessed is real, which is half of
    what they were guessing at. The absent id and the stranger's id answer
    identically for the same reason."""
    client, caller = client_pair
    created = _create(client)
    ident, request_id = created["delta"]["id"], created["request_id"]
    assertion = created["delta"]["assertions"][0]

    caller.who = "bob"
    reads = (client.get(f"/api/deltas/{ident}"),
             client.get(f"/api/deltas/{ident}/evidence"),
             client.post(f"/api/deltas/{request_id}/run", json={}),
             client.post(f"/api/deltas/{ident}/reclassify",
                         json={"assertion_id": assertion["id"],
                               "classification": "incidental",
                               "reason": "mine now"}),
             client.post(f"/api/deltas/{ident}/invalidate", json={}))
    for response in reads:
        assert response.status_code == 404, (
            f"{response.request.method} {response.request.url.path} answered "
            f"{response.status_code}")

    absent = client.get("/api/deltas/delta_nothing_here")
    mine = client.get(f"/api/deltas/{ident}")
    assert absent.status_code == mine.status_code == 404
    assert absent.json() == mine.json(), "the stranger's id is distinguishable"
    assert client.get("/api/deltas").json()["deltas"] == []

    caller.who = "alice"
    assert client.get(f"/api/deltas/{ident}").status_code == 200


# -- rule 4: the owner comes from the session, never from the body ----------


def test_an_owner_in_the_body_is_ignored_and_named_back(client_pair):
    """Not an error and not obeyed: a client that copied a delta back as a
    request is told it was not choosing, rather than silently overruled."""
    client, caller = client_pair

    created = _create(client, owner="mallory")

    assert "owner" in created["ignored_fields"]
    assert created["delta"]["owner"] == "alice"
    caller.who = "mallory"
    assert client.get(f"/api/deltas/{created['delta']['id']}").status_code == 404


def test_a_caller_with_no_owner_is_refused_rather_than_reading_every_delta(
        client_pair, monkeypatch):
    """`persistence._owner_clause` reads an empty owner as the UNSCOPED query --
    right for the doctor, a disclosure of every delta on the machine if a route
    ever passed one. So the route stops there instead.

    The two vocabulary endpoints keep answering, and that is not an oversight:
    `/config` and `/profiles` are about what this SYSTEM can do and name
    nobody's data.
    """
    client, _ = client_pair
    _create(client)
    monkeypatch.setattr(dr, "effective_user", lambda request: None)
    monkeypatch.setattr(dr, "get_current_user", lambda request: None)
    monkeypatch.setattr(dr, "effective_storage_owner", lambda who: None)

    for path in ("/api/deltas", "/api/deltas/diagnostics", "/api/deltas/events"):
        assert client.get(path).status_code == 403, path
    assert client.post("/api/deltas", json=_body()).status_code == 403
    assert client.get("/api/deltas/config").status_code == 200
    assert client.get("/api/deltas/profiles").status_code == 200


# -- rule 5: a typo is answered with the vocabulary, never with an empty list -


def test_a_word_outside_the_vocabulary_is_a_200_naming_the_legal_values(client_pair):
    """An empty list is indistinguishable from "you have none of those", and a
    typo that reads as good news is what a closed vocabulary exists to
    prevent."""
    client, _ = client_pair
    _create(client)

    bad_domain = client.get("/api/deltas", params={"domain": "wizard"}).json()
    assert bad_domain["ok"] is False
    assert bad_domain["error"]["code"] == "unknown_domain"
    assert "deltas" not in bad_domain, "a typo was answered with an empty page"
    assert all(name in bad_domain["error"]["message"] for name in DOMAINS)
    assert "wizard" not in bad_domain["error"]["message"], "the legal values, not the typo"

    bad_assessment = client.get("/api/deltas", params={"assessment": "great"}).json()
    assert bad_assessment["ok"] is False
    assert bad_assessment["error"]["path"] == "assessment"
    assert "deltas" not in bad_assessment
    assert all(name in bad_assessment["error"]["message"] for name in ASSESSMENTS)

    # and the same word spelled correctly still filters rather than refusing
    listed = client.get("/api/deltas", params={"domain": DOMAIN}).json()
    assert listed["ok"] is True and len(listed["deltas"]) == 1


# -- rule 6: the flag stops the comparison and never the read ---------------


def test_the_flag_off_refuses_the_comparison_and_keeps_the_read(client_pair, monkeypatch):
    """A delta already stored was a conclusion recorded honestly, and switching
    the engine off is a decision about what the machine may SPEND."""
    client, _ = client_pair
    created = _create(client)
    monkeypatch.setattr(dr, "enabled", lambda: False)
    monkeypatch.setattr(service_mod, "enabled", lambda: False)

    refused = client.post("/api/deltas", json=_body())
    assert refused.status_code == 200
    assert refused.json()["ok"] is False
    assert refused.json()["error"]["code"] == "delta_engine_disabled"
    assert refused.json()["enabled"] is False
    for path in (f"/api/deltas/{created['request_id']}/run",
                 "/api/deltas/intent/compile"):
        body = client.post(path, json={"domain": DOMAIN}).json()
        assert body["error"]["code"] == "delta_engine_disabled", path

    kept = client.get(f"/api/deltas/{created['delta']['id']}")
    assert kept.status_code == 200
    assert kept.json()["delta"]["id"] == created["delta"]["id"]
    assert client.get("/api/deltas").json()["deltas"], "the record was hidden"
    assert client.get(f"/api/deltas/{created['delta']['id']}/evidence").json()["ok"] is True
    assert client.get("/api/deltas/diagnostics").json()["enabled"] is False


# -- rule 7: the stream polls, and resumes without repeating ----------------


def test_events_without_stream_is_json_and_a_cursor_that_repeats_nothing(client_pair):
    """`since` means STRICTLY AFTER, so a page that reconnects with the last
    cursor it saw duplicates nothing and a gap is announced rather than
    hidden."""
    client, _ = client_pair
    _create(client)

    first = client.get("/api/deltas/events").json()

    assert first["ok"] is True and first["gap"] is False
    assert first["events"], "a comparison ran and the stream said nothing"
    assert "delta_completed" in [row["name"] for row in first["events"]]
    assert first["cursor"] == first["events"][-1]["seq"]

    again = client.get("/api/deltas/events", params={"since": first["cursor"]}).json()
    assert again["events"] == [], "the cursor replayed what the caller had seen"
    assert again["cursor"] == first["cursor"]


# -- the gates, against the unpatched middleware ---------------------------


def test_the_surface_is_admin_only(tmp_path):
    """A delta names paths, symbols, configuration keys and state fields from
    this machine, so the whole surface is admin -- the vocabulary endpoints
    included, since they describe what this box can compare."""
    P.use_path(str(tmp_path / "delta_engine.db"))
    service_mod.reset_service()
    app = FastAPI()
    app.include_router(dr.setup_delta_engine_routes())
    try:
        with TestClient(app) as client:
            assert client.get("/api/deltas").status_code == 403
            assert client.get("/api/deltas/config").status_code == 403
            assert client.get("/api/deltas/diagnostics").status_code == 403
            assert client.post("/api/deltas", json={}).status_code == 403
    finally:
        service_mod.reset_service()
        P.use_path(None)


def test_reclassify_and_invalidate_additionally_demand_a_person(tmp_path, monkeypatch,
                                                                adapter):
    """`require_admin` accepts the in-process internal token so the agent's own
    loopback calls reach admin routes. For these two that would be a hole with
    the shape of a feature: reclassifying is how a `regression` becomes a
    `required` change, so a model able to call the endpoint its own page calls
    could reinterpret its way out of every finding against it.
    """
    # The token is minted once per process, and the gate compares against the
    # copy held by the module `dr.require_admin` actually came from. Importing
    # it by name here instead would be a SECOND copy, and any test anywhere in
    # the suite that reloads `core.middleware` -- which some do, to check module
    # identity -- leaves the two disagreeing. The symptom is this test failing
    # on its first assertion with 403 in a full run and passing on its own,
    # which is a story about `sys.modules` and not about the gate.
    import sys

    middleware = sys.modules[dr.require_admin.__module__]
    INTERNAL_TOOL_HEADER = middleware.INTERNAL_TOOL_HEADER
    INTERNAL_TOOL_TOKEN = middleware.INTERNAL_TOOL_TOKEN

    P.use_path(str(tmp_path / "delta_engine.db"))
    P.reset_store()
    service_mod.reset_service()
    delta_events.reset_streams()
    registry.register(lambda: adapter)
    svc = service_mod.DeltaEngineService()
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(dr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(dr, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(dr, "enabled", lambda: True)
    monkeypatch.setattr(service_mod, "enabled", lambda: True)
    app = FastAPI()
    app.include_router(dr.setup_delta_engine_routes())
    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    try:
        with TestClient(app) as client:
            # the token reaches the reads and the comparison itself
            assert client.get("/api/deltas", headers=headers).status_code == 200
            created = client.post("/api/deltas", json=_body(), headers=headers)
            assert created.status_code == 200, created.text
            delta = created.json()["delta"]

            refused = client.post(
                f"/api/deltas/{delta['id']}/reclassify",
                json={"assertion_id": delta["assertions"][0]["id"],
                      "classification": "requested",
                      "reason": "the model decided this was what was asked for"},
                headers=headers)
            assert refused.status_code == 403
            assert "person" in refused.text

            retire = client.post(f"/api/deltas/{delta['id']}/invalidate",
                                 json={"reason": "not by a tool call"},
                                 headers=headers)
            assert retire.status_code == 403
            assert "person" in retire.text

            # and the observation is exactly as the extractor left it
            after = client.get(f"/api/deltas/{delta['id']}", headers=headers).json()
            assert after["delta"]["assertions"] == delta["assertions"]
    finally:
        registry.reset()
        service_mod.reset_service()
        delta_events.reset_streams()
        P.reset_store()
        P.use_path(None)
