"""Saved workflows, and the tools they publish to outside callers.

Pinned here:

* a workflow may declare an `inputs` schema, and a definition without one keeps
  the fingerprint it always had;
* the library is per owner, refuses names and inputs a published tool cannot
  carry, and publishes nothing until somebody enables it;
* a tool is named and described from the workflow, takes its inputs as
  arguments (checked, defaults filled) and `overrides` — a whitelist, checked per
  field, that a loop's ceilings can only lower;
* a call starts a run and answers with its id and, within the wait, its result;
  a slow run goes on and is read back by `workflow_run_status`;
* the MCP server lists what the route lists and forwards calls to it.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from core.database import Base
from src.contracts import ContractError, WorkflowDefinition
from src.workflows import WorkflowEngine, WorkflowStore, default_handlers, published
from src.workflows.library import LibraryError, WorkflowLibrary, slug

START = {"id": "start", "type": "manual"}
INPUTS = {"type": "object", "required": ["topic"],
          "properties": {"topic": {"type": "string", "minLength": 1},
                         "depth": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2}}}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "wf_pub.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    yield WorkflowStore()
    engine.dispose()


def definition(wid="research.brief", inputs=INPUTS, extra_nodes=None, description="Write a short brief on a topic.", **top):
    nodes = [START,
             {"id": "think", "type": "agent", "needs": ["start"],
              "config": {"prompt": "Brief on {{ inputs.topic }}", "tools": [], "max_rounds": 3}},
             {"id": "send", "type": "skill", "needs": ["think"], "config": {"skill": "mail.send"}}]
    nodes += extra_nodes or []
    body = {"id": wid, "version": "1.0.0", "title": "Research brief", "description": description,
            "nodes": nodes, **top}
    if inputs is not None:
        body["inputs"] = inputs
    return WorkflowDefinition.parse(body)


# ═════════════════════════ the contract ═════════════════════════════════

def test_a_definition_without_inputs_keeps_its_fingerprint_and_shape():
    plain = definition(inputs=None)
    assert "inputs" not in plain.to_dict()
    with_inputs = definition()
    assert with_inputs.fingerprint() != plain.fingerprint()
    assert WorkflowDefinition.parse(with_inputs.to_dict()).fingerprint() == with_inputs.fingerprint()
    assert with_inputs.to_dict()["inputs"]["required"] == ["topic"]


@pytest.mark.parametrize("bad,needle", [
    ({"type": "array"}, "object"),
    ({"type": "object", "properties": {"a": "string"}}, "properties"),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]}, "required"),
    ({"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(65)}}, "at most 64"),
    ("a string", "JSON schema"),
])
def test_a_malformed_inputs_schema_is_refused(bad, needle):
    with pytest.raises(ContractError) as err:
        definition(inputs=bad)
    assert needle in str(err.value)


# ═════════════════════════ the library ══════════════════════════════════

def test_saving_keeps_a_named_definition_unpublished_until_enabled(store):
    lib = WorkflowLibrary()
    saved = lib.save("ana", definition())
    assert saved["name"] == "research_brief" and saved["tool"] == "wf_research_brief"
    assert saved["enabled"] is False and saved["publishable"] is True and saved["allow_overrides"] is True
    assert lib.get("ana", "research_brief")["definition"]["inputs"]["required"] == ["topic"]
    assert published.tool_specs("ana") == []
    lib.update("ana", "research_brief", enabled=True)
    assert [t["name"] for t in published.tool_specs("ana")] == ["wf_research_brief"]


def test_the_library_is_per_owner(store):
    lib = WorkflowLibrary()
    lib.save("ana", definition(), enabled=True)
    assert lib.get("bob", "research_brief") is None and lib.list("bob") == []
    assert published.tool_specs("bob") == []
    lib.save("bob", definition(description="Bob's own."), name="research_brief")
    assert lib.get("ana", "research_brief")["description"] == "Write a short brief on a topic."
    assert lib.delete("bob", "research_brief") is True and lib.get("ana", "research_brief") is not None
    with pytest.raises(LibraryError):
        lib.update("bob", "research_brief", enabled=True)


def test_saving_over_a_name_replaces_the_definition_and_keeps_the_switch(store):
    lib = WorkflowLibrary()
    lib.save("ana", definition(), enabled=True, allow_overrides=False)
    again = lib.save("ana", definition(description="Sharper brief."))
    assert again["description"] == "Sharper brief." and again["enabled"] is True and again["allow_overrides"] is False
    assert len(lib.list("ana")) == 1


def test_a_workflow_that_loses_its_inputs_is_unpublished(store):
    lib = WorkflowLibrary()
    lib.save("ana", definition(), enabled=True)
    lib.save("ana", definition(inputs=None), name="research_brief")
    assert lib.get("ana", "research_brief")["enabled"] is False
    assert published.tool_specs("ana") == []


@pytest.mark.parametrize("kwargs,needle", [
    ({"name": "Not Valid!"}, "not a usable name"),
    ({"name": "x" * 60}, "not a usable name"),
    ({"enabled": True, "definition": definition(inputs=None)}, "declares `inputs`"),
    ({"definition": definition(inputs={"type": "object", "properties": {"overrides": {"type": "string"}}})}, "keeps"),
    ({"definition": definition(inputs={"type": "object", "properties": {"wait_seconds": {"type": "number"}}})}, "keeps"),
    ({"definition": definition(inputs={"type": "object", "properties": {"x": {"$ref": "#/defs/x"}}})}, "cannot be checked"),
])
def test_the_library_refuses_what_a_tool_could_not_carry(store, kwargs, needle):
    kwargs = dict(kwargs)
    d = kwargs.pop("definition", definition())
    with pytest.raises(LibraryError) as err:
        WorkflowLibrary().save("ana", d, **kwargs)
    assert needle in str(err.value)


def test_enabling_needs_inputs(store):
    lib = WorkflowLibrary()
    lib.save("ana", definition(inputs=None))
    with pytest.raises(LibraryError):
        lib.update("ana", "research_brief", enabled=True)


def test_slug_is_a_safe_tool_name():
    assert slug("report.publish") == "report_publish" and slug("  A--B  ") == "a_b" and slug("x" * 80) == "x" * 48


# ═════════════════════════ the tool list ════════════════════════════════

def test_a_tool_is_named_and_described_from_the_workflow_and_takes_its_inputs(store):
    WorkflowLibrary().save("ana", definition(), enabled=True)
    [spec] = published.tool_specs("ana")
    assert spec["name"] == "wf_research_brief" and spec["workflow"] == "research.brief"
    assert "Write a short brief on a topic." in spec["description"]
    schema = spec["inputSchema"]
    assert schema["required"] == ["topic"]
    assert {"topic", "depth", "wait_seconds", "idempotency_key", "overrides"} <= set(schema["properties"])
    assert schema["properties"]["topic"] == {"type": "string", "minLength": 1}


def test_the_overrides_schema_lists_only_what_can_be_changed_on_this_workflow(store):
    WorkflowLibrary().save("ana", definition(), enabled=True)
    tw = published.tool_specs("ana")[0]["inputSchema"]["properties"]["overrides"]
    assert set(tw["properties"]) == {"think"}, "a skill node has nothing overridable"
    assert set(tw["properties"]["think"]["properties"]) == set(published.OVERRIDABLE["agent"])
    assert "tools" not in tw["properties"]["think"]["properties"]
    WorkflowLibrary().update("ana", "research_brief", allow_overrides=False)
    assert "overrides" not in published.tool_specs("ana")[0]["inputSchema"]["properties"]


def test_the_description_falls_back_to_the_title(store):
    WorkflowLibrary().save("ana", definition(description=""), enabled=True)
    assert published.tool_specs("ana")[0]["description"].startswith("Research brief")


# ═════════════════════════ checking a call ══════════════════════════════

def ready(store, **kw):
    WorkflowLibrary().save("ana", kw.pop("definition", None) or definition(), enabled=True, **kw)


def test_arguments_are_checked_against_the_declared_inputs_and_defaults_are_filled(store):
    ready(store)
    ok = published.prepare_call("ana", "wf_research_brief", {"topic": "tides"})
    assert ok["inputs"] == {"topic": "tides", "depth": 2} and ok["wait_seconds"] == published.DEFAULT_WAIT_S
    assert ok["dedupe_key"] == "" and ok["applied_overrides"] == []
    for bad, needle in [({}, "topic"), ({"topic": ""}, "topic"), ({"topic": 4}, "topic"),
                        ({"topic": "x", "depth": 9}, "depth"), ({"topic": "x", "depth": "deep"}, "depth")]:
        with pytest.raises(published.PublishError) as err:
            published.prepare_call("ana", "wf_research_brief", bad)
        assert err.value.code == "bad_arguments" and needle in str(err.value)


def test_unknown_disabled_and_other_owners_tools_are_unknown(store):
    ready(store)
    for owner, tool in [("ana", "wf_nothing"), ("bob", "wf_research_brief"), ("ana", "research_brief"), ("ana", "")]:
        with pytest.raises(published.PublishError) as err:
            published.prepare_call(owner, tool, {"topic": "x"})
        assert err.value.code == "unknown_tool"
    WorkflowLibrary().update("ana", "research_brief", enabled=False)
    with pytest.raises(published.PublishError):
        published.prepare_call("ana", "wf_research_brief", {"topic": "x"})


@pytest.mark.parametrize("extra,needle", [
    ({"wait_seconds": -1}, "wait_seconds"), ({"wait_seconds": 301}, "wait_seconds"),
    ({"wait_seconds": True}, "wait_seconds"), ({"idempotency_key": ""}, "idempotency_key"),
    ({"idempotency_key": 5}, "idempotency_key"),
])
def test_the_reserved_arguments_are_checked(store, extra, needle):
    ready(store)
    with pytest.raises(published.PublishError) as err:
        published.prepare_call("ana", "wf_research_brief", {"topic": "x", **extra})
    assert needle in str(err.value)


def test_reserved_arguments_are_not_inputs(store):
    ready(store)
    prepared = published.prepare_call("ana", "wf_research_brief",
                                      {"topic": "x", "wait_seconds": 5, "idempotency_key": " k1 "})
    assert prepared["inputs"] == {"topic": "x", "depth": 2} and prepared["wait_seconds"] == 5.0
    assert prepared["dedupe_key"] == "pub:ana:research_brief:k1"


def test_overrides_override_node_config_for_that_run_only(store):
    ready(store)
    prepared = published.prepare_call("ana", "wf_research_brief", {
        "topic": "x", "overrides": {"think": {"prompt": "Be brief about {{ inputs.topic }}", "max_rounds": 5}}})
    node = prepared["definition"].node("think")
    assert node.config["prompt"].startswith("Be brief") and node.config["max_rounds"] == 5
    assert node.config["tools"] == [], "what was not overridden is untouched"
    assert prepared["applied_overrides"] == [
        {"node": "think", "field": "prompt", "from": "Brief on {{ inputs.topic }}", "to": "Be brief about {{ inputs.topic }}"},
        {"node": "think", "field": "max_rounds", "from": 3, "to": 5}]
    saved = WorkflowLibrary().definition("ana", "research_brief")
    assert saved.node("think").config["max_rounds"] == 3, "the saved workflow is unchanged"


@pytest.mark.parametrize("overrides,needle", [
    ({"ghost": {"prompt": "x"}}, "no such node"),
    ({"send": {"skill": "evil.run"}}, "nothing that can be overridden"),
    ({"think": {"tools": ["bash"]}}, "not overridable"),
    ({"think": {"agent": "coder"}}, "not overridable"),
    ({"think": {"max_rounds": 500}}, "max_rounds"),
    ({"think": {"max_rounds": "many"}}, "max_rounds"),
    ({"think": {"prompt": ""}}, "prompt"),
    ({"think": {}}, "expected an object"),
    ({"think": "prompt"}, "expected an object"),
    ("think", "must be an object"),
    ({"think": {"prompt": "x" * 30000}}, "prompt"),
])
def test_overrides_are_whitelisted_and_checked_per_field(store, overrides, needle):
    ready(store)
    with pytest.raises(published.PublishError) as err:
        published.prepare_call("ana", "wf_research_brief", {"topic": "x", "overrides": overrides})
    assert err.value.code == "bad_overrides" and needle in str(err.value)


def test_every_problem_in_the_overrides_is_reported_at_once(store):
    ready(store)
    with pytest.raises(published.PublishError) as err:
        published.prepare_call("ana", "wf_research_brief", {"topic": "x", "overrides": {
            "think": {"tools": ["bash"], "max_rounds": 0}, "ghost": {"a": 1}}})
    assert len(err.value.problems) == 3


def test_an_owner_can_turn_overrides_off(store):
    ready(store, allow_overrides=False)
    with pytest.raises(published.PublishError) as err:
        published.prepare_call("ana", "wf_research_brief", {"topic": "x", "overrides": {"think": {"max_rounds": 2}}})
    assert err.value.code == "overrides_disabled"
    published.prepare_call("ana", "wf_research_brief", {"topic": "x", "overrides": {}})     # an empty object asks for nothing


def test_a_override_that_breaks_the_workflow_is_refused_by_the_contract(store):
    body = [{"id": "route", "type": "classify", "needs": ["start"],
             "config": {"text": "{{ inputs.topic }}", "labels": ["a", "b"]}},
            {"id": "go", "type": "skill", "needs": ["route"], "branch": {"route": "a"}, "config": {"skill": "x.y"}}]
    ready(store, definition=definition(extra_nodes=body))
    out = published.prepare_call("ana", "wf_research_brief",
                                 {"topic": "x", "overrides": {"route": {"threshold": 0.9, "fallback": "a"}}})
    assert out["definition"].node("route").config["fallback"] == "a"


def test_a_loops_ceilings_can_be_lowered_but_never_raised(store):
    body = [{"id": "work", "type": "skill", "needs": ["start"], "config": {"skill": "x.y"}},
            {"id": "lp", "type": "loop", "needs": ["start"],
             "config": {"body": ["work"], "budget": {"max_iterations": 5, "max_seconds": 60}}}]
    ready(store, definition=definition(extra_nodes=body))
    lowered = published.prepare_call("ana", "wf_research_brief", {
        "topic": "x", "overrides": {"lp": {"max_iterations": 2, "max_seconds": 30, "max_tool_calls": 4}}})
    budget = lowered["definition"].node("lp").config["budget"]
    assert budget["max_iterations"] == 2 and budget["max_seconds"] == 30 and budget["max_tool_calls"] == 4
    for bad in ({"max_iterations": 6}, {"max_seconds": 61}):
        with pytest.raises(published.PublishError) as err:
            published.prepare_call("ana", "wf_research_brief", {"topic": "x", "overrides": {"lp": bad}})
        assert "only lower" in str(err.value)


# ═════════════════════════ over HTTP ════════════════════════════════════

class Mail:
    """The skill runner: records what it was asked, optionally slow or failing."""

    def __init__(self, delay=0.0, fail=False):
        self.calls: List[Dict[str, Any]] = []
        self.delay = delay
        self.fail = fail
        self.gate = threading.Event()
        self.gate.set()
        self.pause = False

    def __call__(self, node, context):
        self.calls.append({"node": node.id, "inputs": dict(context["inputs"])})
        self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if self.pause:
            return {"status": "paused", "approval_id": "apr_7", "reason": "needs a person"}
        if self.fail:
            return {"status": "failed", "reason": "the mail server said no"}
        return {"sent": True, "brief": context["results"]["think"]["text"]}


class Brief:
    def __init__(self):
        self.prompts: List[str] = []

    def __call__(self, spec):
        self.prompts.append(spec["prompt"])
        return {"text": "BRIEF: " + spec["prompt"], "rounds": 1, "tool_calls": 0, "model": "m",
                "profile": "", "session_id": "s", "tool_events": []}


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core import middleware
    import routes.workflows_routes as wr
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    mail, brief = Mail(), Brief()
    monkeypatch.setattr(wr, "_engine", lambda s: WorkflowEngine(default_handlers(skill=mail, agent=brief), s))
    app = FastAPI()
    app.include_router(wr.setup_workflows_routes())
    c = TestClient(app)
    c.mail, c.brief = mail, brief
    return c


def save(client, d=None, **extra):
    return client.post("/api/workflows/library", json={"definition": (d or definition()).to_dict(), **extra})


def test_the_library_routes_save_list_update_and_delete(client):
    created = save(client).json()["workflow"]
    assert created["name"] == "research_brief" and created["enabled"] is False
    assert [w["name"] for w in client.get("/api/workflows/library").json()["workflows"]] == ["research_brief"]
    assert client.get("/api/workflows/library/research_brief").json()["workflow"]["definition"]["id"] == "research.brief"
    assert client.get("/api/workflows/library/ghost").status_code == 404
    assert client.get("/api/workflows/published").json()["tools"] == []
    on = client.patch("/api/workflows/library/research_brief", json={"enabled": True})
    assert on.status_code == 200 and on.json()["workflow"]["enabled"] is True
    assert [t["name"] for t in client.get("/api/workflows/published").json()["tools"]] == ["wf_research_brief"]
    assert client.patch("/api/workflows/library/research_brief", json={"enabled": "yes"}).status_code == 400
    assert client.patch("/api/workflows/library/research_brief", json={"oops": True}).status_code == 400
    assert client.patch("/api/workflows/library/ghost", json={"enabled": True}).status_code == 404
    assert client.delete("/api/workflows/library/research_brief").status_code == 200
    assert client.delete("/api/workflows/library/research_brief").status_code == 404


def test_the_library_route_refuses_what_the_contract_or_the_library_refuses(client):
    bad = definition().to_dict()
    bad["nodes"][1]["config"] = {}
    bad["nodes"][1]["needs"] = ["nobody"]
    assert save(client, WorkflowDefinition.parse({**definition(inputs=None).to_dict()}), enabled=True).status_code == 400
    assert client.post("/api/workflows/library", json={"definition": bad}).status_code == 400
    assert save(client, name="Bad Name").status_code == 400


def test_a_call_starts_a_run_and_answers_with_the_id_and_the_result(client):
    save(client, enabled=True)
    out = client.post("/api/workflows/published/wf_research_brief/call",
                      json={"arguments": {"topic": "tides", "wait_seconds": 20}})
    assert out.status_code == 200
    body = out.json()
    assert body["finished"] is True and body["status"] == "completed" and body["run_id"].startswith("wfr_")
    assert body["run"]["result"]["send"]["sent"] is True
    assert "BRIEF: Brief on tides" in body["run"]["result"]["send"]["brief"]
    assert client.mail.calls == [{"node": "send", "inputs": {"topic": "tides", "depth": 2}}]


def test_overrides_reach_the_run_and_its_snapshot(client):
    save(client, enabled=True)
    body = client.post("/api/workflows/published/wf_research_brief/call", json={"arguments": {
        "topic": "tides", "wait_seconds": 20, "overrides": {"think": {"prompt": "One line on {{ inputs.topic }}"}}}}).json()
    assert body["overrides_applied"][0]["field"] == "prompt"
    assert client.brief.prompts == ["One line on tides"]
    run = client.get(f"/api/workflows/runs/{body['run_id']}").json()
    assert run["definition"]["nodes"][1]["config"]["prompt"] == "One line on {{ inputs.topic }}"


def test_a_slow_run_answers_with_its_id_and_is_read_back_later(client):
    save(client, enabled=True)
    client.mail.gate.clear()
    first = client.post("/api/workflows/published/wf_research_brief/call",
                        json={"arguments": {"topic": "tides", "wait_seconds": 0}}).json()
    assert first["finished"] is False and first["status"] in ("running", "pending") and "result" not in first["run"]
    client.mail.gate.set()
    for _ in range(100):
        status = client.get(f"/api/workflows/published/runs/{first['run_id']}").json()
        if status["finished"]:
            break
        time.sleep(0.1)
    assert status["status"] == "completed" and status["result"]["send"]["sent"] is True
    assert status["nodes"]["think"]["status"] == "completed"


def test_the_same_idempotency_key_returns_the_same_run(client):
    save(client, enabled=True)
    args = {"topic": "tides", "wait_seconds": 20, "idempotency_key": "req-1"}
    one = client.post("/api/workflows/published/wf_research_brief/call", json={"arguments": args}).json()
    two = client.post("/api/workflows/published/wf_research_brief/call", json={"arguments": args}).json()
    assert one["run_id"] == two["run_id"] and one["duplicate"] is False and two["duplicate"] is True
    assert len(client.mail.calls) == 1


def test_a_failed_run_says_which_node_failed_and_why(client):
    save(client, enabled=True)
    client.mail.fail = True
    body = client.post("/api/workflows/published/wf_research_brief/call",
                       json={"arguments": {"topic": "x", "wait_seconds": 20}}).json()
    assert body["finished"] is True and body["status"] == "failed"
    assert body["run"]["failed"] == [{"node": "send", "reason": "the mail server said no"}]
    assert "result" not in body["run"]


def test_a_run_waiting_on_a_person_answers_with_what_it_waits_for(client):
    client.mail.pause = True
    save(client, enabled=True)
    body = client.post("/api/workflows/published/wf_research_brief/call",
                       json={"arguments": {"topic": "x", "wait_seconds": 20}}).json()
    assert body["finished"] is False and body["status"] == "paused"
    assert body["run"]["waiting_on"] == [{"node": "send", "approval_id": "apr_7"}]
    assert "result" not in body["run"]


def test_call_errors_are_refusals_with_a_code(client):
    save(client, enabled=True)
    missing = client.post("/api/workflows/published/wf_research_brief/call", json={"arguments": {}})
    assert missing.status_code == 400 and missing.json()["detail"]["code"] == "bad_arguments"
    unknown = client.post("/api/workflows/published/wf_ghost/call", json={"arguments": {}})
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "unknown_tool"
    bad = client.post("/api/workflows/published/wf_research_brief/call",
                      json={"arguments": {"topic": "x", "overrides": {"think": {"tools": []}}}})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "bad_overrides"
    client.patch("/api/workflows/library/research_brief", json={"allow_overrides": False})
    off = client.post("/api/workflows/published/wf_research_brief/call",
                      json={"arguments": {"topic": "x", "overrides": {"think": {"max_rounds": 2}}}})
    assert off.status_code == 403
    assert client.mail.calls == [], "nothing started for a refused call"


def test_a_run_is_only_visible_to_its_owner(client, store):
    other = definition()
    run_id = store.create_run(other, owner="somebody-else")["run_id"]
    assert client.get(f"/api/workflows/published/runs/{run_id}").status_code == 404
    assert published.run_status(store, "somebody-else", run_id)["run_id"] == run_id
    assert client.get("/api/workflows/published/runs/wfr_nothing").status_code == 404


def test_results_are_compacted_and_a_loops_body_is_not_listed_separately(store):
    d = WorkflowDefinition.parse({"id": "l", "version": "1.0.0", "title": "l", "nodes": [
        START,
        {"id": "lp", "type": "loop", "needs": ["start"], "config": {"body": ["w"], "budget": {"max_iterations": 1}}},
        {"id": "w", "type": "skill", "needs": ["start"], "config": {"skill": "x.y"}}]})
    run_id = store.create_run(d, owner="ana")["run_id"]
    big = "z" * 20000
    WorkflowEngine(default_handlers(skill=lambda n, c: {"text": big, "idempotency_key": "k", "tool_events": [1]}), store).advance(run_id)
    status = published.run_status(store, "ana", run_id)
    assert set(status["nodes"]) == {"start", "lp"} and list(status["result"]) == ["lp"]
    text = status["result"]["lp"]["results"]["w"]["text"]
    assert len(text) < 8100 and "truncated" in text
    assert "idempotency_key" not in json.dumps(status) and "tool_events" not in json.dumps(status)


# ═════════════════════════ the MCP server ═════════════════════════════

@pytest.fixture()
def mcp(monkeypatch):
    from mcp_servers import workflows_server as srv
    calls: List[Any] = []
    tools = [{"name": "wf_research_brief", "description": "Write a short brief.",
              "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}]

    def fake(method, path, body=None, timeout=30.0):
        calls.append((method, path, body, timeout))
        if path == "/api/workflows/published":
            return {"ok": True, "tools": tools}
        if path.startswith("/api/workflows/published/runs/"):
            return {"ok": True, "run_id": path.rsplit("/", 1)[1], "status": "completed"}
        return {"ok": True, "run_id": "wfr_1", "finished": True, "status": "completed"}

    monkeypatch.setattr(srv, "_request", fake)
    srv.calls = calls
    return srv


def test_the_server_lists_published_workflows_then_the_status_tool(mcp):
    names = [t.name for t in mcp.fetch_tools()]
    assert names == ["wf_research_brief", "workflow_run_status"]
    listed = asyncio.run(mcp.list_tools())
    assert listed[0].inputSchema["required"] == ["topic"]


def test_the_server_still_lists_the_status_tool_when_faustus_is_down(mcp, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("Faustus is not reachable")
    monkeypatch.setattr(mcp, "_request", down)
    assert [t.name for t in mcp.fetch_tools()] == ["workflow_run_status"]


def test_the_server_forwards_a_call_with_its_arguments_and_a_timeout_that_covers_the_wait(mcp):
    out = asyncio.run(mcp.call_tool("wf_research_brief", {"topic": "tides", "wait_seconds": 120}))
    assert json.loads(out[0].text)["run_id"] == "wfr_1"
    method, path, body, timeout = mcp.calls[-1]
    assert (method, path) == ("POST", "/api/workflows/published/wf_research_brief/call")
    assert body == {"arguments": {"topic": "tides", "wait_seconds": 120}} and timeout > 120


def test_the_status_tool_reads_a_run_and_errors_come_back_as_text(mcp, monkeypatch):
    out = asyncio.run(mcp.call_tool("workflow_run_status", {"run_id": "wfr_9"}))
    assert json.loads(out[0].text)["run_id"] == "wfr_9"
    assert mcp.calls[-1][:2] == ("GET", "/api/workflows/published/runs/wfr_9")
    assert asyncio.run(mcp.call_tool("workflow_run_status", {}))[0].text == "Error: run_id is required"

    def boom(*a, **k):
        raise RuntimeError("Faustus answered HTTP 404")
    monkeypatch.setattr(mcp, "_request", boom)
    assert "HTTP 404" in asyncio.run(mcp.call_tool("wf_research_brief", {"topic": "x"}))[0].text


def test_a_token_with_the_dispatch_scope_may_reach_published_workflows_and_nothing_else_here():
    from core import authz
    rule = authz.api_rule_for("POST", "/api/workflows/published/wf_x/call")
    assert rule is not None and "agents:dispatch" in rule.scopes
    assert authz.api_rule_for("GET", "/api/workflows/published/runs/wfr_1") is not None
    assert authz.api_rule_for("POST", "/api/workflows/library") is None
    assert authz.api_rule_for("POST", "/api/workflows/runs") is None


def test_tools_list_and_tools_call_over_a_real_mcp_session_against_the_routes(client, monkeypatch):
    """The whole path in one process: an MCP client session talks the protocol to
    the server module, which forwards to the real routes (through the test
    client), which start a real run."""
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp_servers import workflows_server as srv

    def forward(method, path, body=None, timeout=30.0):
        response = client.request(method, path, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"Faustus answered HTTP {response.status_code} for {method} {path}: {response.text}")
        return response.json()

    monkeypatch.setattr(srv, "_request", forward)
    save(client, enabled=True)

    async def session():
        async with create_connected_server_and_client_session(srv.server) as mcp_client:
            listed = await mcp_client.list_tools()
            by_name = {t.name: t for t in listed.tools}
            assert set(by_name) == {"wf_research_brief", "workflow_run_status"}
            tool = by_name["wf_research_brief"]
            assert tool.inputSchema["required"] == ["topic"]
            assert set(tool.inputSchema["properties"]) == {"topic", "depth", "wait_seconds", "idempotency_key", "overrides"}
            called = await mcp_client.call_tool(
                "wf_research_brief",
                {"topic": "tides", "overrides": {"think": {"prompt": "Write two lines on {{ inputs.topic }}"}},
                 "wait_seconds": 20, "idempotency_key": "k1"})
            first = json.loads(called.content[0].text)
            again = json.loads((await mcp_client.call_tool(
                "wf_research_brief", {"topic": "tides", "idempotency_key": "k1"})).content[0].text)
            status = json.loads((await mcp_client.call_tool(
                "workflow_run_status", {"run_id": first["run_id"]})).content[0].text)
            refused = await mcp_client.call_tool("wf_research_brief", {"topic": "x", "overrides": {"send": {"skill": "evil"}}})
            return first, again, status, refused

    first, again, status, refused = asyncio.run(session())
    assert first["finished"] is True and first["status"] == "completed"
    assert first["overrides_applied"] == [{"node": "think", "field": "prompt", "from": "Brief on {{ inputs.topic }}",
                                           "to": "Write two lines on {{ inputs.topic }}"}]
    assert "BRIEF: Write two lines on tides" in first["run"]["result"]["send"]["brief"]
    assert again["run_id"] == first["run_id"] and again["duplicate"] is True
    assert status["status"] == "completed" and status["run_id"] == first["run_id"]
    assert refused.isError and "'send' was unexpected" in refused.content[0].text, "the tool's own schema refuses it first"
    assert len(client.mail.calls) == 1, "the duplicate and the refused call started nothing"
