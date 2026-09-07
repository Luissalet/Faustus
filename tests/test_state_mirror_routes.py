"""/api/state/* -- the transport (routes/state_mirror_routes.py).

One test per hard rule of the API, and the rules are all about the edge,
because that is where a mirror leaks or lies:

1. every route answers for an admin, and the whole surface is admin-only
   (unpatched middleware at the bottom);
2. another owner's entity is 404 and never 403 -- a 403 confirms the row
   exists, and what it is about is then one error away -- and the refusal
   carries no id, no kind and no display name;
3. a malformed BODY is a 4xx and a bad VALUE is a 200 with `ok: false` and a
   stable token, the convention `routes/contracts_routes.py` set;
4. `agent_state_mirror` off refuses the two endpoints that probe the machine
   and keeps every read answering, because turning the mirror off is a
   decision about what may COST the box and never an instruction to hide what
   was already observed;
5. `/events` speaks SSE in unnamed frames and resumes from a cursor with no
   duplicate and no gap (plan 20);
6. an entity id carrying `://` and slashes survives the round trip through the
   URL, which is the only reason the path parameter is `{entity_id:path}`.

Rule 6 is the one a mocked client could pass while the shipped route failed,
so it is exercised end to end: a real id with a directory in its identifier is
percent-escaped, sent over HTTP, and compared with what came back.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.state_mirror_routes as sr
from src.state_mirror import events as state_events
from src.state_mirror import persistence as P
from src.state_mirror import service as service_mod
from src.state_mirror.contracts import (
    SCHEMA_FOR_KIND,
    MaterializedState,
    StateConflict,
    StateEntity,
    StateObservation,
    entity_id,
)


# -- doubles ----------------------------------------------------------------

class _Caller:
    """Who the middleware would have resolved. Mutable so one client can be two
    different people without rebuilding the app."""

    def __init__(self, who: str = "alice") -> None:
        self.who = who


class _Adapter:
    """A source that answers instantly. No subprocess, no registry, no GPU.

    Named `probe` and declaring `service_state.v1` so the seeded fields below
    resolve back to it: `service.refresh(entity_ids=...)` wakes the adapters
    the fields NAME, and an entity whose source no adapter answers to is the
    other half of that behaviour, tested separately.
    """

    name = "probe"
    schemas = ("service_state.v1",)

    def __init__(self) -> None:
        self.observed = 0
        self.discovered = 0

    def available(self) -> bool:
        return True

    def discover(self, scope):
        self.discovered += 1
        return []

    def observe(self, scope):
        self.observed += 1
        return []

    def relations(self, scope):
        return []


# -- seeding ----------------------------------------------------------------

def _stamp(seconds_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _seed(owner: str, kind: str, identifier: str, fields, *,
          namespace: str = "real", source: str = "probe",
          display_name: str = "") -> str:
    """One entity and the state currently believed about it.

    Written straight through the store rather than through an adapter: this
    file is about the transport, and a fixture that had to run the real eleven
    adapters would be testing the machine it is running on.

    `fields` maps a field name to `(value, seconds_ago, ttl_seconds)`, so a
    test can age one field past its guarantee without sleeping.
    """
    ident = entity_id(kind, owner, identifier, namespace=namespace)
    schema = SCHEMA_FOR_KIND[kind]
    store = P.store()
    store.upsert_entity(StateEntity.parse({
        "id": ident, "kind": kind, "owner": owner, "namespace": namespace,
        "display_name": display_name or identifier, "schema": schema,
        "created_at": _stamp(60), "updated_at": _stamp()}))
    store.put_state(MaterializedState.parse({
        "entity_id": ident, "owner": owner, "schema": schema, "revision": 1,
        "updated_at": _stamp(),
        "fields": {
            name: {"value": value, "epistemic": "observed", "freshness": "fresh",
                   "observed_at": _stamp(age), "source": source,
                   "ttl_seconds": ttl, "observation_id": f"obs_{name}"}
            for name, (value, age, ttl) in fields.items()},
    }))
    store.append_observation(StateObservation.parse({
        "entity_id": ident, "owner": owner, "schema": schema, "source": source,
        "observed_at": _stamp(1), "partial": True,
        "state": {name: value for name, (value, _age, _ttl) in fields.items()}}))
    return ident


def _conflict(owner: str, ident: str, field: str) -> str:
    row = StateConflict.parse({
        "entity_id": ident, "field": field, "owner": owner,
        "claims": [{"source": "probe", "value": "available"},
                   {"source": "other", "value": "unreachable"}],
        "next_check": "ask the endpoint directly"})
    P.store().open_conflict(row)
    return row.id


SERVICE_FIELDS = {"health": ("available", 5, 30), "latency_ms": (12, 5, 30)}


# -- fixtures ---------------------------------------------------------------

@pytest.fixture
def caller() -> _Caller:
    return _Caller("alice")


@pytest.fixture
def adapter() -> _Adapter:
    return _Adapter()


@pytest.fixture
def mirror(tmp_path, monkeypatch, caller, adapter):
    """An app with only this router, its own database, and nothing shared.

    `require_admin` and `require_human` are stubbed here so the tests below are
    about the mirror's own rules; that both gates are really wired is its own
    test at the bottom, which runs against the unpatched middleware.
    """
    P.use_path(str(tmp_path / "state.db"))
    service_mod.reset_service()
    state_events.reset_streams()

    svc = service_mod.StateMirrorService(adapters=[adapter])
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(sr, "require_admin", lambda request: None)
    monkeypatch.setattr(sr, "require_human", lambda request: None)
    monkeypatch.setattr(sr, "effective_user", lambda request: caller.who)
    monkeypatch.setattr(sr, "get_current_user", lambda request: caller.who)
    # Both halves of the flag, because there are two readers of it and they are
    # not the same guard: the route refuses before the service is called, and
    # the service refuses again for every caller that is not a route. A test
    # that patched only one would pass while the other still said no.
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(service_mod, "enabled", lambda: True)

    app = FastAPI()
    app.include_router(sr.setup_state_mirror_routes())
    with TestClient(app) as client:
        client.svc = svc
        yield client

    service_mod.reset_service()
    state_events.reset_streams()
    P.use_path(None)


@pytest.fixture
def client_pair(mirror, caller):
    """The client and the mutable identity behind it, together -- most tests
    need both and passing them as one tuple keeps the signatures short."""
    return mirror, caller


def _path(ident: str, tail: str = "") -> str:
    """The URL for an entity id: escaped whole, separators included."""
    return f"/api/state/entities/{quote(ident, safe='')}{tail}"


# -- rule 1: every route answers, and answers about this owner -------------

def test_every_route_of_the_plan_answers_for_an_admin(client_pair):
    """Section 17's list, end to end. A route that is written, registered and
    never exercised is the failure this file exists to catch."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)

    assert client.get("/api/state/entities").json()["count"] == 1
    assert client.get(_path(ident)).json()["entity"]["entity"]["id"] == ident
    assert client.get(_path(ident, "/history")).json()["count"] >= 1
    assert client.get("/api/state/changes").json()["ok"] is True
    assert client.get("/api/state/conflicts").json()["conflicts"] == []
    assert client.get("/api/state/diagnostics").json()["diagnostics"]["enabled"] is True
    projected = client.get("/api/state/project", params={"entity_ref": ident}).json()
    assert projected["ok"] is True
    assert projected["projection"]["entity_refs"] == [ident]
    assert client.get("/api/state/events").json()["events"] == []
    assert client.get("/api/state/situation/running_work").json()["ok"] is True
    assert client.post("/api/state/refresh",
                       json={"entity_id": ident}).status_code == 200
    assert client.post("/api/state/reconcile").json()["ok"] is True


def test_rebuild_previews_then_repairs_only_the_current_owners_entity(client_pair):
    client, caller = client_pair
    ident = _seed("alice", "service", "replay", SERVICE_FIELDS)
    preview = client.post("/api/state/rebuild", json={"entity_id": ident}).json()
    assert preview["ok"] and preview["matches"]
    with P.store()._db() as conn:
        conn.execute("DELETE FROM state_materialized WHERE entity_id=?", [ident])
    denied = client.post("/api/state/rebuild", json={"entity_id": ident, "apply": True}).json()
    assert not denied["ok"]
    repaired = client.post("/api/state/rebuild", json={"entity_id": ident, "apply": True,
        "expected_sha256": preview["receipt"]["sha256"]}).json()
    assert repaired["repaired"]
    caller.who = "bob"
    assert client.post("/api/state/rebuild", json={"entity_id": ident}).status_code == 404


def test_rebuild_refuses_malformed_requests_and_journal(client_pair):
    client, _ = client_pair
    ident = _seed("alice", "service", "replay", SERVICE_FIELDS)
    assert client.post("/api/state/rebuild", json={"entity_id": ident, "apply": "false"}).status_code == 400
    assert client.post("/api/state/rebuild", content='x' * 4097).status_code == 413
    with P.store()._db() as conn:
        conn.execute("UPDATE state_replay_steps SET patch='{}'")
    assert not client.post("/api/state/rebuild", json={"entity_id": ident}).json()["ok"]


def test_project_projection_validates_its_closed_vocabularies(client_pair):
    client, _ = client_pair
    invalid = client.get(
        "/api/state/project", params={"minimum_freshness": "probably"}
    ).json()
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "invalid_argument"
    assert invalid["error"]["path"] == "minimum_freshness"


def test_a_refresh_wakes_the_adapter_the_fields_name(client_pair, adapter):
    """The source is read off the FIELDS, because a field records who wrote it
    and that is the only thing that could refresh it."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)

    body = client.post("/api/state/refresh", json={"entity_id": ident}).json()

    assert body["ok"] is True
    assert body["refreshed"] == ["probe"]
    assert adapter.observed >= 1


def test_a_read_answers_the_state_with_its_age_and_its_source(client_pair):
    """A value with no `observed_at` and no `source` is a rumour with good
    posture: the whole point of the mirror is that it cannot store one."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)

    field = client.get(_path(ident)).json()["entity"]["state"]["fields"]["health"]

    assert field["value"] == "available"
    assert field["source"] == "probe"
    assert field["observed_at"]
    assert field["epistemic"] == "observed"
    assert field["freshness"] in ("fresh", "aging", "stale", "unknown")


def test_a_field_past_its_guarantee_is_answered_as_stale_not_as_current(client_pair):
    """The single most important thing this API does. A stale value is still
    RETURNED -- hiding it would be worse -- and it is returned saying so."""
    client, _ = client_pair
    ident = _seed("alice", "service", "old", {"health": ("available", 4000, 30)})

    field = client.get(_path(ident)).json()["entity"]["state"]["fields"]["health"]

    assert field["value"] == "available", "the last value is kept, not blanked"
    assert field["freshness"] == "stale", "and it is not presented as current"


# -- rule 2: somebody else's entity is 404, and leaks nothing --------------

READS = ("", "/history")


def test_another_owners_entity_is_404_never_403_and_leaks_no_name(client_pair):
    """A 403 says "this exists and is not yours", which is half the secret; the
    display name in the message would be the other half."""
    client, caller = client_pair
    ident = _seed("alice", "service", "secret-render-farm", SERVICE_FIELDS,
                  display_name="Secret render farm")

    caller.who = "bob"
    for suffix in READS:
        response = client.get(_path(ident, suffix))
        assert response.status_code == 404, f"GET {suffix} answered {response.status_code}"
        assert "Secret render farm" not in response.text
        assert "secret-render-farm" not in response.text

    refused = client.post("/api/state/refresh", json={"entity_id": ident})
    assert refused.status_code == 404
    assert "Secret render farm" not in refused.text

    assert client.get("/api/state/entities").json()["entities"] == []
    caller.who = "alice"
    assert client.get(_path(ident)).status_code == 200


def test_an_entity_that_never_existed_answers_exactly_like_a_strangers(client_pair):
    client, caller = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)
    caller.who = "bob"

    mine = client.get(_path(ident))
    absent = client.get(_path("service://bob/real/nothing-here"))

    assert mine.status_code == absent.status_code == 404
    assert mine.json() == absent.json()


def test_a_conflict_about_an_invisible_entity_is_not_answered(client_pair):
    """A conflict names two sources and two values; answering one for an entity
    the caller cannot see would leak both."""
    client, caller = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)
    _conflict("alice", ident, "health")

    assert len(client.get("/api/state/conflicts").json()["conflicts"]) == 1
    caller.who = "bob"
    body = client.get("/api/state/conflicts", params={"entity_id": ident}).json()
    assert body["conflicts"] == []
    assert client.get("/api/state/conflicts").json()["conflicts"] == []


# -- rule 3: a bad body is a 4xx, a bad value is a 200 with a token --------

def test_a_malformed_body_is_the_one_case_that_is_a_4xx(client_pair):
    client, _ = client_pair

    assert client.post("/api/state/refresh", content="not json").status_code == 400
    assert client.post("/api/state/refresh", json=["a", "list"]).status_code == 400


def test_a_bad_value_is_a_200_with_ok_false_and_a_stable_token(client_pair):
    """The caller asked a question and got an answer. `4xx` is for a request
    that could not be read at all."""
    client, _ = client_pair

    bad_id = client.post("/api/state/refresh", json={"entity_id": "not an id"})
    assert bad_id.status_code == 200
    assert bad_id.json()["ok"] is False
    assert bad_id.json()["error"]["path"] == "entity_id"
    assert bad_id.json()["error"]["code"] == "invalid_argument"

    bad_kind = client.get("/api/state/entities", params={"kind": "wizard"}).json()
    assert bad_kind["ok"] is False
    assert bad_kind["error"]["code"] == "invalid_argument"
    assert "wizard" not in bad_kind["error"]["message"], "the legal values, not the typo"

    bad_world = client.get("/api/state/entities", params={"namespace": "elsewhere"}).json()
    assert bad_world["ok"] is False and bad_world["error"]["path"] == "namespace"

    unknown = client.get("/api/state/situation/self_destruct").json()
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "unknown_situation"
    assert unknown["situations"], "a typo is answered with the ones that exist"


def test_every_code_this_surface_answers_with_is_in_a_closed_vocabulary(client_pair):
    """Stable domain errors, never the text of an exception. Every code comes
    from `service.ERRORS` or from `ROUTE_ERRORS`."""
    client, _ = client_pair
    known = set(service_mod.ERRORS) | set(sr.ROUTE_ERRORS)

    for call in (lambda: client.post("/api/state/refresh", json={"entity_id": "nope"}),
                 lambda: client.post("/api/state/refresh", json={}),
                 lambda: client.post("/api/state/refresh", json={"source": "nobody"}),
                 lambda: client.get("/api/state/entities", params={"kind": "wizard"}),
                 lambda: client.get("/api/state/situation/nope")):
        body = call().json()
        assert body["ok"] is False, body
        assert body["error"]["code"] in known, body["error"]


def test_a_server_decided_field_in_the_body_is_dropped_and_named_back(client_pair):
    """Not an error and not obeyed: a client that sent the whole object back is
    told it was not choosing, rather than being silently overruled."""
    client, _ = client_pair
    _seed("alice", "service", "comfyui", SERVICE_FIELDS)

    body = client.post("/api/state/refresh",
                       json={"source": "probe", "owner": "mallory",
                             "observed_at": "2020-01-01T00:00:00Z"}).json()

    assert body["ok"] is True
    assert body["ignored_fields"] == ["owner", "observed_at"]


# -- rule 4: the flag stops the probing and stops nothing else -------------

def test_with_the_flag_off_reads_answer_and_probing_refuses(client_pair, monkeypatch,
                                                            adapter):
    """Turning the mirror off is a decision about what may COST the machine,
    never an instruction to hide what was already observed."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)
    _conflict("alice", ident, "health")
    monkeypatch.setattr(sr, "enabled", lambda: False)

    for url in ("/api/state/entities", _path(ident), _path(ident, "/history"),
                "/api/state/changes", "/api/state/conflicts",
                "/api/state/diagnostics", "/api/state/events",
                "/api/state/situation/running_work"):
        response = client.get(url)
        assert response.status_code == 200, f"{url} stopped answering"
        assert response.json()["ok"] is True, url
    assert client.get("/api/state/entities").json()["count"] == 1
    assert len(client.get("/api/state/conflicts").json()["conflicts"]) == 1
    assert client.get("/api/state/diagnostics").json()["diagnostics"]["enabled"] is False

    for url, payload in (("/api/state/refresh", {"entity_id": ident}),
                         ("/api/state/reconcile", None)):
        body = (client.post(url, json=payload) if payload is not None
                else client.post(url)).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "state_mirror_disabled"
        assert "switched off" in body["error"]["message"]
        assert body["enabled"] is False

    assert adapter.observed == 0, "nothing probed the machine"


def test_the_flag_defaults_to_off():
    """The sweep forks probes and walks registries. That is opt-in."""
    from src.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["agent_state_mirror"] is False
    assert DEFAULT_SETTINGS["agent_state_mirror_sweep_seconds"] == 30


def test_the_flag_has_a_settings_schema_entry():
    """Parity is enforced by tests/test_agent_settings_schema.py; this states
    the requirement where somebody reading the mirror will see it."""
    from src.agent_settings_schema import schema_fields, schema_problems

    fields = {f["key"]: f for f in schema_fields()}
    assert fields["agent_state_mirror"]["type"] == "bool"
    assert fields["agent_state_mirror"]["help"].strip()
    sweep = fields["agent_state_mirror_sweep_seconds"]
    assert sweep["type"] == "int" and sweep["min"] == 5 and sweep["max"] == 3600
    assert schema_problems() == []


# -- rule 5: the stream resumes with no duplicate and no gap ---------------

def _publish(client, ident: str, count: int):
    stream = client.svc.events(owner="alice")
    for index in range(count):
        stream.publish("state_changed", entity_id=ident, field="health",
                       revision=index + 1)
    return stream


def test_events_answers_json_by_default_and_resumes_strictly_after_the_cursor(client_pair):
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)
    _publish(client, ident, 5)

    everything = client.get("/api/state/events").json()
    assert [e["seq"] for e in everything["events"]] == [1, 2, 3, 4, 5]
    assert everything["last_seq"] == 5

    rest = client.get("/api/state/events", params={"since": 3}).json()
    assert [e["seq"] for e in rest["events"]] == [4, 5], "strictly after, no repeat"
    assert rest["since"] == 3

    nothing = client.get("/api/state/events", params={"since": 5}).json()
    assert nothing["events"] == [], "and nothing at all once it has caught up"


def test_the_cursor_may_arrive_as_since_seq_or_a_last_event_id_header(client_pair):
    """Three spellings because three clients exist, and a browser reconnecting
    sends the header on its own without being asked."""
    client, _ = client_pair
    _publish(client, _seed("alice", "service", "comfyui", SERVICE_FIELDS), 4)

    by_since = client.get("/api/state/events", params={"since": 2}).json()
    by_seq = client.get("/api/state/events", params={"seq": 2}).json()
    by_header = client.get("/api/state/events",
                           headers={"Last-Event-ID": "2"}).json()

    assert [e["seq"] for e in by_since["events"]] == [3, 4]
    assert [e["seq"] for e in by_seq["events"]] == [3, 4]
    assert [e["seq"] for e in by_header["events"]] == [3, 4]


def test_events_speaks_sse_with_unnamed_frames_and_one_named_end(client_pair):
    """An SSE frame carrying `event: <name>` never reaches `onmessage`. Every
    state event therefore goes out unnamed with its name inside the JSON, and
    only the terminal frame is named -- the dialect Council and Dispatch use."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)
    _publish(client, ident, 3).close()

    body = client.get("/api/state/events",
                      params={"stream": 1, "timeout": 5}).text

    frames = [f for f in body.split("\n\n") if f.strip()]
    payloads = [json.loads(f.split("data: ", 1)[1])
                for f in frames if not f.startswith("event:") and "data: " in f]
    assert [p["seq"] for p in payloads] == [1, 2, 3]
    assert all(p["name"] == "state_changed" for p in payloads)
    assert body.count("event: end") == 1
    assert body.index("event: end") > body.rindex('"seq": 3')


def test_an_sse_reconnection_repeats_nothing_and_skips_nothing(client_pair):
    """The two failures a resume can have, and they are opposites: a repeat is
    a decision applied twice, a hole is one nobody knows was taken."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)
    stream = _publish(client, ident, 6)
    stream.close()

    first = client.get("/api/state/events",
                       params={"stream": 1, "timeout": 5, "since": 0}).text
    seen = [json.loads(f.split("data: ", 1)[1])["seq"]
            for f in first.split("\n\n")
            if f.strip() and not f.startswith("event:") and "data: " in f]
    second = client.get("/api/state/events",
                        params={"stream": 1, "timeout": 5, "since": seen[-1]}).text
    again = [json.loads(f.split("data: ", 1)[1])["seq"]
             for f in second.split("\n\n")
             if f.strip() and not f.startswith("event:") and "data: " in f]

    assert seen == [1, 2, 3, 4, 5, 6]
    assert again == [], "a reconnection from the last seq repeats nothing"
    assert sorted(set(seen)) == seen, "and nothing was delivered twice"


def test_an_event_source_gets_sse_from_its_accept_header_alone(client_pair):
    """`new EventSource(url)` sends no query parameter and cannot add one."""
    client, _ = client_pair
    _publish(client, _seed("alice", "service", "comfyui", SERVICE_FIELDS), 1).close()

    response = client.get("/api/state/events",
                          headers={"Accept": "text/event-stream"},
                          params={"timeout": 5})

    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "data: " in response.text


# -- rule 6: an id carries `://` and slashes, and survives a URL -----------

def test_an_entity_id_with_a_slash_in_its_identifier_survives_the_round_trip(client_pair):
    """`artifact://alice/real/exports/2026/report.pdf`. A path parameter that
    stopped at the first `/` would 404 every artifact under a directory, and a
    page that interpolated the id raw would address a row nobody named."""
    client, _ = client_pair
    ident = _seed("alice", "artifact", "exports/2026/report.pdf",
                  {"exists": (True, 5, 3600)}, display_name="report.pdf")
    assert "/" in ident.split("://", 1)[1].split("/", 2)[2], "the identifier has a slash"

    body = client.get(_path(ident)).json()

    assert body["ok"] is True
    assert body["entity"]["entity"]["id"] == ident, "byte for byte, out and back"
    assert client.get(_path(ident, "/history")).json()["entity_id"] == ident
    assert client.post("/api/state/refresh",
                       json={"entity_id": ident}).status_code == 200


def test_an_id_that_reached_the_route_still_escaped_is_read_anyway(client_pair):
    """A proxy that refuses to decode `%2F` in a path is a real deployment, and
    the alternative is a bare 404 that says nothing about why."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)

    twice = quote(quote(ident, safe=""), safe="")
    body = client.get(f"/api/state/entities/{twice}").json()

    assert body["ok"] is True
    assert body["entity"]["entity"]["id"] == ident


def test_the_history_suffix_is_not_swallowed_by_the_greedy_path_parameter(client_pair):
    """`{entity_id:path}` compiles to a greedy `.*`, so the order the two routes
    are declared in is load bearing: declared the other way round, `/history`
    would be read as part of the id and never reach its own handler."""
    client, _ = client_pair
    ident = _seed("alice", "service", "comfyui", SERVICE_FIELDS)

    body = client.get(_path(ident, "/history")).json()

    assert body["entity_id"] == ident
    assert "observations" in body


# -- the gates, and the owner that must never be empty ---------------------

def test_the_surface_is_admin_only(tmp_path):
    """Unpatched middleware: the mirror names every service, model, run and
    artifact on the machine, so the whole surface is admin."""
    P.use_path(str(tmp_path / "state.db"))
    service_mod.reset_service()
    app = FastAPI()
    app.include_router(sr.setup_state_mirror_routes())
    try:
        with TestClient(app) as client:
            assert client.get("/api/state/entities").status_code == 403
            assert client.get("/api/state/diagnostics").status_code == 403
            assert client.post("/api/state/reconcile").status_code == 403
    finally:
        service_mod.reset_service()
        P.use_path(None)


def test_reconcile_additionally_demands_a_person(tmp_path, monkeypatch, caller):
    """`require_admin` accepts the in-process internal token so the agent's own
    loopback calls reach admin routes. For the sweep that asks every source to
    look again, that would let a model spend the machine by calling the
    endpoint its own page calls."""
    # The token is minted once per process, and the gate compares against the
    # copy held by the module `sr.require_admin` actually came from. Importing
    # it by name here instead would be a SECOND copy, and any test anywhere in
    # the suite that reloads `core.middleware` -- which some do, to check module
    # identity -- leaves the two disagreeing. The symptom is this test failing
    # on its first assertion with 403 in a full run and passing on its own,
    # which is a story about `sys.modules` and not about the gate.
    import sys

    middleware = sys.modules[sr.require_admin.__module__]
    INTERNAL_TOOL_HEADER = middleware.INTERNAL_TOOL_HEADER
    INTERNAL_TOOL_TOKEN = middleware.INTERNAL_TOOL_TOKEN

    P.use_path(str(tmp_path / "state.db"))
    service_mod.reset_service()
    svc = service_mod.StateMirrorService(adapters=[])
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(sr, "effective_user", lambda request: "alice")
    monkeypatch.setattr(sr, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(sr, "enabled", lambda: True)
    monkeypatch.setattr(service_mod, "enabled", lambda: True)
    app = FastAPI()
    app.include_router(sr.setup_state_mirror_routes())
    headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
    try:
        with TestClient(app) as client:
            # the token reaches the reads
            assert client.get("/api/state/entities", headers=headers).status_code == 200
            assert client.post("/api/state/refresh", json={"source": "probe"},
                               headers=headers).status_code == 200
            # and is refused by the one that sweeps everything
            refused = client.post("/api/state/reconcile", headers=headers)
            assert refused.status_code == 403
            assert "person" in refused.text
    finally:
        service_mod.reset_service()
        P.use_path(None)


def test_a_caller_with_no_owner_is_refused_rather_than_reading_every_entity(
        tmp_path, monkeypatch):
    """`persistence._scope` reads an empty owner as the UNSCOPED query -- right
    for the sweep and the doctor, a disclosure of every entity on the machine
    if a route ever passed one. So the route stops there instead."""
    P.use_path(str(tmp_path / "state.db"))
    service_mod.reset_service()
    svc = service_mod.StateMirrorService(adapters=[])
    monkeypatch.setattr(service_mod, "service", lambda: svc)
    monkeypatch.setattr(sr, "require_admin", lambda request: None)
    monkeypatch.setattr(sr, "require_human", lambda request: None)
    monkeypatch.setattr(sr, "effective_user", lambda request: None)
    monkeypatch.setattr(sr, "get_current_user", lambda request: None)
    monkeypatch.setattr(sr, "effective_storage_owner", lambda who: None)
    _seed("alice", "service", "somebody-elses-box", SERVICE_FIELDS)
    app = FastAPI()
    app.include_router(sr.setup_state_mirror_routes())
    try:
        with TestClient(app) as client:
            response = client.get("/api/state/entities")
            assert response.status_code == 403
            assert "somebody-elses-box" not in response.text
    finally:
        service_mod.reset_service()
        P.use_path(None)
