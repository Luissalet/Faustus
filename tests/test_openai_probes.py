"""Capability probes for OpenAI-compatible endpoints: declared, probed, unsupported.

Offline: every server is an ``httpx.MockTransport``; the endpoint store is a real
SQLite file with the revision triggers installed.
"""
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as d
from src import endpoint_resolver as er
from src import model_calibration as mcal
from src import openai_probes as op

REAL_CLIENT = httpx.Client
URL = "http://127.0.0.1:8081/v1/chat/completions"
SECRET = "sk-synthetic-secret-value"


# ------------------------------------------------------------------ helpers --

def client_for(handler):
    return lambda timeout: REAL_CLIENT(transport=httpx.MockTransport(handler), timeout=timeout)


def serve(monkeypatch, handler):
    """Every ``httpx.Client`` the code under test creates talks to ``handler``."""
    monkeypatch.setattr(httpx, "Client", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler)))


def chat(message, status=200):
    return httpx.Response(status, json={"choices": [{"message": message}]})


def error(status, message):
    return httpx.Response(status, json={"error": {"message": message}})


def sse(*events):
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def delta(**fields):
    return {"choices": [{"index": 0, "delta": fields}]}


TOOL_CALL = {"role": "assistant", "content": None, "tool_calls": [
    {"id": "1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}}]}


def run(handler, name, **kw):
    with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
        return op._RUNNERS[name](client, URL, {"Authorization": f"Bearer {SECRET}"}, "m", **kw)


def state(result):
    return mcal.probe_state(result)


# ------------------------------------------------------------- tool calling --

def test_a_structured_tool_call_is_supported():
    result = run(lambda r: chat(TOOL_CALL), mcal.TEST_TOOL_CALLING)
    assert result["ok"] is True and result["evidence"]["state"] == "supported"
    assert result["evidence"]["protocol"] == mcal.OPENAI_CHAT_PROTOCOL


def test_text_instead_of_a_tool_call_is_unsupported_with_the_reason():
    result = run(lambda r: chat({"role": "assistant", "content": "It is sunny in Paris."}), mcal.TEST_TOOL_CALLING)
    assert result["ok"] is False and "text instead" in result["evidence"]["reason"]


def test_a_tool_call_without_the_required_argument_is_not_supported():
    bad = {"role": "assistant", "tool_calls": [{"function": {"name": "get_weather", "arguments": "{}"}}]}
    assert run(lambda r: chat(bad), mcal.TEST_TOOL_CALLING)["ok"] is False


def test_a_rejection_that_names_tools_is_unsupported():
    result = run(lambda r: error(400, "This model does not support tools"), mcal.TEST_TOOL_CALLING)
    assert result["ok"] is False and result["evidence"]["status"] == 400
    assert "named the feature" in result["evidence"]["reason"]


@pytest.mark.parametrize("status,message", [
    (400, "context length exceeded"), (401, "bad key"), (403, "forbidden"), (404, "model not found"),
    (429, "slow down"), (500, "boom"), (503, "loading model"),
])
def test_anything_that_does_not_name_the_feature_is_unknown_never_a_failure(status, message):
    result = run(lambda r: error(status, message), mcal.TEST_TOOL_CALLING)
    assert result["ok"] is None and result["evidence"]["state"] == "unknown"


def test_a_dead_server_is_unknown():
    def handler(request):
        raise httpx.ConnectError("refused")
    result = run(handler, mcal.TEST_TOOL_CALLING)
    assert result["ok"] is None and "no response" in result["evidence"]["reason"]


def test_a_timeout_is_unknown():
    def handler(request):
        raise httpx.ReadTimeout("slow")
    assert run(handler, mcal.TEST_TOOL_CALLING)["ok"] is None


# ---------------------------------------------------- streaming tool calls --

def test_a_tool_call_assembled_from_stream_deltas_is_supported():
    events = [
        delta(role="assistant"),
        delta(tool_calls=[{"index": 0, "id": "1", "function": {"name": "get_weather", "arguments": ""}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"ci'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'ty": "Paris"}'}}]),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    result = run(lambda r: sse(*events), mcal.TEST_STREAMING_TOOL_CALLS)
    assert result["ok"] is True and result["evidence"]["chunks_with_tool_calls"] == 3


def test_streamed_text_instead_of_a_tool_call_is_unsupported():
    result = run(lambda r: sse(delta(content="Sunny"), delta(content=" in Paris")), mcal.TEST_STREAMING_TOOL_CALLS)
    assert result["ok"] is False and "streamed text" in result["evidence"]["reason"]


def test_a_server_that_refuses_tools_with_stream_is_unsupported():
    result = run(lambda r: error(400, "Cannot use tools with stream=true"), mcal.TEST_STREAMING_TOOL_CALLS)
    assert result["ok"] is False


def test_a_stream_that_breaks_before_a_call_is_unknown():
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
            raise httpx.ReadError("cut")

    def handler(request):
        return httpx.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})
    result = run(handler, mcal.TEST_STREAMING_TOOL_CALLS)
    assert result["ok"] is None


def test_a_stream_route_that_does_not_exist_is_unknown():
    assert run(lambda r: error(404, "no such route"), mcal.TEST_STREAMING_TOOL_CALLS)["ok"] is None


# ---------------------------------------------------------------- json mode --

def test_json_mode_valid_object_is_supported_and_asks_for_json_object():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return chat({"role": "assistant", "content": '{"ok": true, "n": 2}'})
    assert run(handler, mcal.TEST_JSON_MODE)["ok"] is True
    assert seen["response_format"] == {"type": "json_object"}


def test_json_mode_prose_is_unsupported():
    result = run(lambda r: chat({"content": "Sure! Here you go: {}"}), mcal.TEST_JSON_MODE)
    assert result["ok"] is False and "not valid JSON" in result["evidence"]["reason"]


def test_json_mode_rejected_by_name_is_unsupported_and_other_errors_are_unknown():
    assert run(lambda r: error(400, "response_format is not supported"), mcal.TEST_JSON_MODE)["ok"] is False
    assert run(lambda r: error(500, "oops"), mcal.TEST_JSON_MODE)["ok"] is None


def test_json_that_is_not_an_object_is_unsupported():
    assert run(lambda r: chat({"content": "[1, 2]"}), mcal.TEST_JSON_MODE)["ok"] is False


# ------------------------------------------------------------------- vision --

def test_vision_accepted_image_is_supported_and_sent_as_an_image_url_part():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return chat({"content": "A tiny image."})
    assert run(handler, mcal.TEST_VISION)["ok"] is True
    part = seen["messages"][0]["content"][1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_rejection_naming_the_image_is_unsupported_and_empty_answer_is_unknown():
    assert run(lambda r: error(400, "image input is not supported by this model"), mcal.TEST_VISION)["ok"] is False
    assert run(lambda r: error(500, "x"), mcal.TEST_VISION)["ok"] is None
    assert run(lambda r: chat({"content": ""}), mcal.TEST_VISION)["ok"] is None


# ------------------------------------------------------------------ context --

def test_context_needs_a_declared_length_and_is_skipped_otherwise():
    with REAL_CLIENT(transport=httpx.MockTransport(lambda r: pytest.fail("no request expected"))) as client:
        result = op.probe_context(client, URL, {}, "m", announced_limit=0)
    assert result["ok"] is None and result["evidence"]["skipped"]


def test_context_finds_the_needle_placed_at_the_end():
    def handler(request):
        text = json.loads(request.content)["messages"][0]["content"]
        code = text.split("The secret code is ")[1].split(".")[0]
        return chat({"content": code})
    with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
        assert op.probe_context(client, URL, {}, "m", announced_limit=16000)["ok"] is True
    with REAL_CLIENT(transport=httpx.MockTransport(lambda r: chat({"content": "I don't know"}))) as client:
        assert op.probe_context(client, URL, {}, "m", announced_limit=16000)["ok"] is False


# --------------------------------------------------------- run and evidence --

def test_the_key_and_headers_never_reach_the_stored_evidence():
    handler = lambda r: chat(TOOL_CALL)  # noqa: E731
    with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
        tested = op.run_probes(client, URL, {"Authorization": f"Bearer {SECRET}"}, "m",
                               include=[mcal.TEST_TOOL_CALLING, mcal.TEST_JSON_MODE])
    blob = json.dumps(tested)
    assert SECRET not in blob and "Authorization" not in blob and "Bearer" not in blob


def test_default_probes_and_unknown_names():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return chat(TOOL_CALL)
    with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
        tested = op.run_probes(client, URL, {}, "m")
        assert set(tested) == set(op.DEFAULT_PROBES)
        assert op.run_probes(client, URL, {}, "m", include=["refusal_format", "nope"]) == {}


def test_an_exhausted_budget_records_unknown_not_missing():
    with REAL_CLIENT(transport=httpx.MockTransport(lambda r: pytest.fail("must not call"))) as client:
        tested = op.run_probes(client, URL, {}, "m", deadline_s=0)
    assert set(tested) == set(op.DEFAULT_PROBES)
    assert all(v["ok"] is None and v["evidence"]["skipped"] == "time budget exceeded" for v in tested.values())


def test_the_protocol_is_read_from_the_resolved_chat_url_only():
    assert mcal.explicit_openai_protocol("http://h:8081/v1/chat/completions") == mcal.OPENAI_CHAT_PROTOCOL
    assert mcal.explicit_openai_protocol("http://h/api/chat") == ""
    assert mcal.explicit_openai_protocol("https://host.example/v1/messages") == ""
    assert mcal.explicit_openai_protocol("http://h/v1") == ""
    assert mcal.explicit_openai_protocol("") == ""


# ------------------------------------------------------ declared vs probed --

def rows(announced, tested):
    return {r["capability"]: r for r in mcal.capability_states({"announced": announced, "tested": tested})}


def ok(value, **evidence):
    return {"ok": value, "tested_at": "2026-09-30T10:00:00+00:00", "evidence": evidence}


def test_the_verdicts_keep_declared_probed_and_unsupported_apart():
    r = rows({"capabilities": {"tools": True, "vision": False}},
             {mcal.TEST_TOOL_CALLING: ok(True), mcal.TEST_JSON_MODE: ok(False, status=400),
              mcal.TEST_STREAMING_TOOL_CALLS: ok(None, reason="timed out"),
              mcal.TEST_VISION: ok(None, skipped="vision not announced")})
    assert (r["tool_calling"]["declared"], r["tool_calling"]["probed"], r["tool_calling"]["verdict"]) == (True, True, "verified")
    assert (r["json_mode"]["declared"], r["json_mode"]["probed"], r["json_mode"]["verdict"]) == (None, False, "unsupported")
    assert r["json_mode"]["status"] == 400
    assert r["streaming_tool_calls"]["verdict"] == "undetermined" and r["streaming_tool_calls"]["probed"] is None
    assert r["vision"]["verdict"] == "declared_unsupported", "a skipped probe is not an inconclusive attempt"
    assert r["context_length_effective"]["verdict"] == "not_declared"
    assert not any(row["conflict"] for row in r.values())


def test_a_declaration_the_observation_contradicts_is_flagged():
    r = rows({"capabilities": {"tools": True}}, {mcal.TEST_TOOL_CALLING: ok(False)})
    assert r["tool_calling"]["verdict"] == "unsupported" and r["tool_calling"]["conflict"] is True
    r = rows({"capabilities": {"vision": False}}, {mcal.TEST_VISION: ok(True)})
    assert r["vision"]["verdict"] == "verified" and r["vision"]["conflict"] is True


def test_declared_but_never_probed_is_not_verified():
    r = rows({"capabilities": {"tools": True}}, {})
    assert r["tool_calling"]["verdict"] == "declared_unverified" and r["tool_calling"]["probed"] is None


def test_a_later_inconclusive_attempt_does_not_erase_an_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(mcal, "_record_deployment_evidence", lambda *a, **k: None)
    ident = dict(vendor="openai_compatible", model_id="m", endpoint_id="e",
                 protocol=mcal.OPENAI_CHAT_PROTOCOL, endpoint_revision="r1", data_dir=str(tmp_path))
    mcal.save_scoped_tested(**ident, announced={}, tested={mcal.TEST_TOOL_CALLING: ok(True)})
    saved = mcal.save_scoped_tested(**ident, announced={}, tested={mcal.TEST_TOOL_CALLING: ok(None, reason="timed out")})
    row = {r["capability"]: r for r in mcal.capability_states(saved)}["tool_calling"]
    assert row["verdict"] == "verified"
    assert saved["tested"][mcal.TEST_TOOL_CALLING]["last_attempt"]["ok"] is None


# ---------------------------------------------------- a real endpoint store --

@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "app.db"
    engine = create_engine("sqlite:///" + str(path), connect_args={"check_same_thread": False})
    d.ModelEndpoint.__table__.create(engine)
    d.ProviderAuthSession.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as s:
        s.add(d.ModelEndpoint(id="ep1", name="local", base_url="http://127.0.0.1:8081/v1",
                              api_key=SECRET, supports_tools=True))
        s.add(d.ModelEndpoint(id="native", name="other", base_url="http://127.0.0.1:11434/api", api_key="k"))
        s.add(d.ModelEndpoint(id="off", name="off", base_url="http://127.0.0.1:9/v1", is_enabled=False))
        s.commit()
    raw = sqlite3.connect(path)
    d.install_revision_triggers(raw)
    raw.commit()
    monkeypatch.setattr(er, "SessionLocal", sessions)
    monkeypatch.setattr(d, "SessionLocal", sessions)
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path / "probe-store"))
    monkeypatch.setattr(mcal, "_record_deployment_evidence", lambda *a, **k: None)
    yield raw, sessions, tmp_path
    raw.close()
    engine.dispose()


def revision(raw, endpoint="ep1"):
    return raw.execute("SELECT connection_revision FROM model_endpoints WHERE id=?", (endpoint,)).fetchone()[0]


def working_server(request):
    body = json.loads(request.content)
    if body.get("stream"):
        return sse(delta(tool_calls=[{"index": 0, "function": {"name": "get_weather",
                                                                "arguments": '{"city": "Paris"}'}}]))
    if body.get("response_format"):
        return chat({"content": '{"ok": true}'})
    return chat(TOOL_CALL)


def test_probing_an_endpoint_files_results_under_its_revision_and_reads_them_back(store):
    raw, _, _ = store
    seen_auth = []

    def handler(request):
        seen_auth.append(request.headers.get("authorization"))
        return working_server(request)
    result = op.probe_endpoint("ep1", "qwen", client_factory=client_for(handler))
    assert result["saved"] and result["endpoint_revision"] == revision(raw)
    assert seen_auth and set(seen_auth) == {f"Bearer {SECRET}"}, "the endpoint's own key goes to the endpoint"
    caps = {c["capability"]: c for c in result["capabilities"]}
    assert caps["tool_calling"]["verdict"] == "verified" and caps["tool_calling"]["declared"] is True
    assert caps["json_mode"]["verdict"] == "verified" and caps["json_mode"]["declared"] is None
    scope = result["manifest"]["calibration_scope"]
    assert scope["endpoint_revision"] == revision(raw) and scope["protocol"] == mcal.OPENAI_CHAT_PROTOCOL
    assert scope["vendor"] == "openai_compatible"
    again = op.read_endpoint("ep1", "qwen")
    assert again["saved"] and {c["capability"]: c["verdict"] for c in again["capabilities"]}["json_mode"] == "verified"
    assert SECRET not in json.dumps(result) and SECRET not in json.dumps(again)


def test_a_plain_sql_change_to_the_endpoint_retires_what_was_probed(store):
    raw, _, _ = store
    op.probe_endpoint("ep1", "qwen", client_factory=client_for(working_server))
    raw.execute("UPDATE model_endpoints SET base_url='http://127.0.0.1:8090/v1' WHERE id='ep1'")
    raw.commit()
    after = op.read_endpoint("ep1", "qwen")
    assert not after["saved"] and after["manifest"]["tested"] == {}
    verdicts = {c["capability"]: c["verdict"] for c in after["capabilities"]}
    assert verdicts["tool_calling"] == "declared_unverified" and verdicts["json_mode"] == "not_declared"


def test_an_orm_change_retires_it_too(store):
    raw, sessions, _ = store
    op.probe_endpoint("ep1", "qwen", client_factory=client_for(working_server))
    with sessions() as s:
        s.get(d.ModelEndpoint, "ep1").api_key = "rotated"
        s.commit()
    assert op.read_endpoint("ep1", "qwen")["manifest"]["tested"] == {}


def test_a_change_during_the_probe_files_results_under_the_revision_it_started_with(store):
    raw, _, _ = store
    started_with = revision(raw)
    changed = []

    def handler(request):
        if not changed:
            other = sqlite3.connect(raw.execute("PRAGMA database_list").fetchone()[2])
            other.execute("UPDATE model_endpoints SET base_url='http://127.0.0.1:8090/v1' WHERE id='ep1'")
            other.commit()
            other.close()
            changed.append(True)
        return working_server(request)
    result = op.probe_endpoint("ep1", "qwen", client_factory=client_for(handler))
    assert result["endpoint_revision"] == started_with and revision(raw) != started_with
    assert result["manifest"]["calibration_scope"]["endpoint_revision"] == started_with
    assert op.read_endpoint("ep1", "qwen")["manifest"]["tested"] == {}, "nothing current reads the old observation"


def test_probes_of_one_model_do_not_answer_for_another_or_for_another_endpoint(store):
    op.probe_endpoint("ep1", "qwen", client_factory=client_for(working_server))
    assert op.read_endpoint("ep1", "other-model")["manifest"]["tested"] == {}


def test_what_cannot_be_probed_is_refused_with_a_reason(store):
    with pytest.raises(op.ProbeRefused) as exc:
        op.probe_endpoint("missing", "qwen", client_factory=client_for(working_server))
    assert exc.value.status == 404
    with pytest.raises(op.ProbeRefused) as exc:
        op.probe_endpoint("off", "qwen", client_factory=client_for(working_server))
    assert exc.value.status == 404
    with pytest.raises(op.ProbeRefused) as exc:
        op.probe_endpoint("native", "m", client_factory=client_for(working_server))
    assert exc.value.status == 422 and "chat-completions" in str(exc.value)
    with pytest.raises(op.ProbeRefused) as exc:
        op.probe_endpoint("ep1", "qwen", include=["telepathy"], client_factory=client_for(working_server))
    assert exc.value.status == 400
    with pytest.raises(op.ProbeRefused):
        op.probe_endpoint("", "qwen")


def test_an_unreachable_server_files_unknowns_and_keeps_earlier_observations(store):
    op.probe_endpoint("ep1", "qwen", client_factory=client_for(working_server))

    def down(request):
        raise httpx.ConnectError("refused")
    second = op.probe_endpoint("ep1", "qwen", client_factory=client_for(down))
    caps = {c["capability"]: c for c in second["capabilities"]}
    assert caps["tool_calling"]["verdict"] == "verified", "an outage does not erase what was observed"
    assert second["manifest"]["tested"]["tool_calling"]["last_attempt"]["status"] == "unknown"


def test_probing_by_address_files_nothing(store, monkeypatch):
    serve(monkeypatch, working_server)
    result = op.probe_url("http://127.0.0.1:8081/v1", "qwen", api_key=SECRET)
    assert result["saved"] is False and result["manifest"]["evidence_scope"] == "unsaved"
    assert {c["capability"]: c["verdict"] for c in result["capabilities"]}["tool_calling"] == "verified"
    assert SECRET not in json.dumps(result)
    assert op.read_endpoint("ep1", "qwen")["manifest"]["tested"] == {}
    with pytest.raises(op.ProbeRefused):
        op.probe_url("http://127.0.0.1:11434/api", "m")


# ------------------------------------------------------------------- routes --

@pytest.fixture
def api(store, monkeypatch):
    import routes.model_probe_routes as routes
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    monkeypatch.setattr(routes, "require_user", lambda request: "admin")
    monkeypatch.setattr(routes, "effective_user", lambda request: None)
    serve(monkeypatch, working_server)
    app = FastAPI()
    app.include_router(routes.setup_model_probe_routes())
    return TestClient(app), routes


def test_route_round_trip_probe_then_read(api):
    client, _ = api
    posted = client.post("/api/model-probes/openai", json={"endpoint_id": "ep1", "model": "qwen"})
    assert posted.status_code == 200, posted.text
    assert posted.json()["saved"] is True
    got = client.get("/api/model-probes/openai", params={"endpoint_id": "ep1", "model": "qwen"}).json()
    verdicts = {c["capability"]: c["verdict"] for c in got["capabilities"]}
    assert verdicts["tool_calling"] == "verified" and got["protocol"] == mcal.OPENAI_CHAT_PROTOCOL


def test_route_statuses(api):
    client, _ = api
    assert client.post("/api/model-probes/openai", json={"endpoint_id": "nope", "model": "m"}).status_code == 404
    assert client.post("/api/model-probes/openai", json={"endpoint_id": "native", "model": "m"}).status_code == 422
    assert client.post("/api/model-probes/openai", json={"endpoint_id": "ep1", "model": "m", "probes": "x"}).status_code == 400
    assert client.post("/api/model-probes/openai", json={"endpoint_id": "ep1", "model": "m", "probes": ["nope"]}).status_code == 400
    assert client.get("/api/model-probes/openai", params={"endpoint_id": "ep1"}).status_code == 400


def test_probing_needs_a_same_origin_admin_and_reading_does_not_send_anything(api, monkeypatch):
    client, routes = api
    cross = client.post("/api/model-probes/openai", json={"endpoint_id": "ep1", "model": "qwen"},
                        headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    from fastapi import HTTPException

    def deny(request):
        raise HTTPException(status_code=403, detail="Admin-only")
    monkeypatch.setattr(routes, "require_admin", deny)
    assert client.post("/api/model-probes/openai", json={"endpoint_id": "ep1", "model": "qwen"}).status_code == 403
    assert client.post("/api/model-probes/openai/url", json={"base_url": "http://x/v1", "model": "m"}).status_code == 403
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("a read must not touch the network"))
    assert client.get("/api/model-probes/openai", params={"endpoint_id": "ep1", "model": "qwen"}).status_code == 200


def test_url_route_returns_unsaved_results_without_echoing_the_key(api):
    client, _ = api
    body = client.post("/api/model-probes/openai/url",
                       json={"base_url": "http://127.0.0.1:8081/v1", "model": "qwen", "api_key": SECRET,
                             "probes": ["tool_calling"]}).json()
    assert body["saved"] is False and set(body["manifest"]["tested"]) == {"tool_calling"}
    assert SECRET not in json.dumps(body)


def test_the_model_cannot_run_a_probe_through_the_generic_bridge():
    from src.tools.system import _APP_API_BLOCKLIST_METHOD_PATH
    assert ("POST", "/api/model-probes") in _APP_API_BLOCKLIST_METHOD_PATH


@pytest.mark.asyncio
async def test_app_api_refuses_the_probe_before_any_loopback(monkeypatch):
    from src.tool_implementations import do_app_api

    class NoLoopback:
        def __init__(self, *a, **k):
            raise AssertionError("must be refused before the loopback")
    monkeypatch.setattr(httpx, "AsyncClient", NoLoopback)
    out = await do_app_api(json.dumps({"action": "call", "method": "POST", "path": "/api/model-probes/openai/url",
                                       "body": {"base_url": "http://10.0.0.1/v1", "model": "m"}}))
    assert out["exit_code"] == 1 and "blocked" in out["error"]


# ---------------------------------------------------- MCP, builtin, script --

def test_mcp_tool_reads_probes_and_refuses_writes_without_an_owner(store, monkeypatch):
    import asyncio
    from mcp_servers import context_engine_server as ces
    serve(monkeypatch, working_server)
    probed = json.loads(asyncio.run(ces._tool_model_capabilities(
        "admin", {"action": "probe", "endpoint_id": "ep1", "model": "qwen"}))[0].text)
    assert probed["ok"] and probed["saved"]
    read = json.loads(asyncio.run(ces._tool_model_capabilities(
        "admin", {"endpoint_id": "ep1", "model": "qwen"}))[0].text)
    assert {c["capability"]: c["verdict"] for c in read["capabilities"]}["json_mode"] == "verified"
    refused = json.loads(asyncio.run(ces._tool_model_capabilities(
        "admin", {"endpoint_id": "native", "model": "m"}))[0].text)
    assert refused["ok"] is False and refused["status"] == 422
    monkeypatch.delenv("ODYSSEUS_MCP_CONTEXT_OWNER", raising=False)
    monkeypatch.delenv("ODYSSEUS_CONTEXT_OWNER", raising=False)
    out = asyncio.run(ces.call_tool("context_model_capabilities",
                                    {"action": "probe", "endpoint_id": "ep1", "model": "qwen"}))
    assert "refused" in out[0].text


def test_builtin_action_reports_the_verdicts(store, monkeypatch):
    import asyncio
    from src.builtin_actions import BUILTIN_ACTIONS, BUILTIN_ACTION_INFO, action_probe_model_capabilities
    assert "probe_model_capabilities" in BUILTIN_ACTIONS and "probe_model_capabilities" in BUILTIN_ACTION_INFO
    serve(monkeypatch, working_server)
    text, success = asyncio.run(action_probe_model_capabilities(
        "admin", command=json.dumps({"endpoint_id": "ep1", "model": "qwen", "probes": ["tool_calling"]})))
    assert success and "tool_calling: verified" in text and "json_mode" not in text
    text, success = asyncio.run(action_probe_model_capabilities("admin", command="{"))
    assert not success and text.startswith("Invalid JSON")
    text, success = asyncio.run(action_probe_model_capabilities(
        "admin", command=json.dumps({"endpoint_id": "native", "model": "m"})))
    assert not success and "refused" in text


def test_the_script_prints_each_state_and_exits_with_what_it_learned(monkeypatch, capsys):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "probe_script", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "probe_openai_endpoint.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    serve(monkeypatch, working_server)
    assert script.main(["--base-url", "http://127.0.0.1:8081/v1", "--model", "qwen"]) == 0
    out = capsys.readouterr().out
    assert "tool_calling" in out and "supported" in out and "openai_chat_completions" in out

    def mixed(request):
        if json.loads(request.content).get("response_format"):
            return error(500, "boom")
        return working_server(request)
    serve(monkeypatch, mixed)
    assert script.main(["--base-url", "http://127.0.0.1:8081/v1", "--model", "qwen", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["manifest"]["tested"]["json_mode"]["ok"] is None
    assert script.main(["--base-url", "http://127.0.0.1:11434/api", "--model", "m"]) == 2
