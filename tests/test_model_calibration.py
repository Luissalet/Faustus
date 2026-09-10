"""Model capability manifest + calibration (Lote 17: MOD-01, MOD-02, MOD-06
parcial, QA-28).

Two layers, tested separately:

  - src/model_calibration.py in isolation: the manifest store, degraded-
    capability rules, and each calibration probe against a fake /api/chat
    via httpx.MockTransport (no hardware, per COMUN.md rule 7bis in spirit —
    tests/test_model_capabilities.py and test_local_models_routes.py mock
    the same way).
  - the routes themselves, through the real FastAPI app with TestClient
    (COMUN.md rule 7: no route logic tested purely in mocks) — proves
    src/model_capability_readers/ollama.py is actually wired to
    GET /api/models/{name}/capabilities, and that POST .../calibrate never
    loads a model on its own.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

import core.middleware as mw
import routes.local_models_routes as lm
from src import model_calibration as mcal
from src.model_capability_readers import base as mcr_base
from src.model_capability_readers import ollama as ollama_reader

ROOT = "http://127.0.0.1:11434"


# ── src/model_calibration.py in isolation ───────────────────────────────────

def test_manifest_key_prefers_ollama_digest_over_endpoint_identity():
    a = mcal.manifest_key(vendor="ollama", model_id="qwen3.5:9b", endpoint_id="ep1", digest="aaa111")
    b = mcal.manifest_key(vendor="ollama", model_id="qwen3.5:latest", endpoint_id="ep2", digest="aaa111")
    assert a == b, "same blob, different tag/endpoint: one manifest"
    c = mcal.manifest_key(vendor="ollama", model_id="qwen3.5:9b", endpoint_id="ep1", digest="bbb222")
    assert a != c, "a re-pulled blob under the same tag is a fresh manifest"


def test_manifest_key_falls_back_to_endpoint_identity_without_a_digest():
    a = mcal.manifest_key(vendor="openai", model_id="gpt-x", endpoint_id="ep1")
    b = mcal.manifest_key(vendor="openai", model_id="gpt-x", endpoint_id="ep2")
    assert a != b


def test_get_manifest_on_unknown_key_is_the_honest_empty_manifest(tmp_path):
    m = mcal.get_manifest("nope", data_dir=str(tmp_path))
    assert m == {"announced": {}, "tested": {}, "degraded": [], "updated_at": ""}


def test_save_announced_then_save_tested_round_trip_and_merge(tmp_path):
    key = "k1"
    announced = {"capabilities": {"tools": True, "vision": False}, "limits": {"context_tokens": 32768}}
    m1 = mcal.save_announced(key, announced, data_dir=str(tmp_path))
    assert m1["tested"] == {} and m1["announced"]["capabilities"]["tools"] is True

    tested_first = {mcal.TEST_JSON_MODE: {"ok": True, "tested_at": "t1", "evidence": {}}}
    m2 = mcal.save_tested(key, tested_first, announced=announced, data_dir=str(tmp_path))
    assert m2["tested"][mcal.TEST_JSON_MODE]["ok"] is True

    # a second calibration that only reran tool_calling must not erase json_mode
    tested_second = {mcal.TEST_TOOL_CALLING: {"ok": True, "tested_at": "t2", "evidence": {}}}
    m3 = mcal.save_tested(key, tested_second, announced=announced, data_dir=str(tmp_path))
    assert m3["tested"][mcal.TEST_JSON_MODE]["ok"] is True, "an unrelated rerun must not wipe an earlier probe"
    assert m3["tested"][mcal.TEST_TOOL_CALLING]["ok"] is True

    read_back = mcal.get_manifest(key, data_dir=str(tmp_path))
    assert read_back == m3
    assert mcal.capabilities_for(key, data_dir=str(tmp_path)) == m3


def test_compute_degraded_flags_missing_native_tools_without_any_calibration():
    announced = {"capabilities": {"tools": False, "vision": False}}
    degraded = mcal.compute_degraded(announced, {})
    assert "tools por texto (fence)" in degraded


def test_compute_degraded_is_silent_when_native_tools_are_announced_and_untested():
    announced = {"capabilities": {"tools": True, "vision": False}}
    assert mcal.compute_degraded(announced, {}) == []


def test_compute_degraded_flags_tools_that_fail_calibration_despite_being_announced():
    announced = {"capabilities": {"tools": True}}
    tested = {mcal.TEST_TOOL_CALLING: {"ok": False}}
    degraded = mcal.compute_degraded(announced, tested)
    assert any("fallan en calibraci" in d for d in degraded)


def test_is_model_loaded_uses_the_same_name_matching_as_the_rest_of_the_page():
    # `_same_model` treats a bare tag as `:latest` — the same rule the
    # installed-models table uses, so "loaded" never disagrees between them.
    assert mcal.is_model_loaded(["qwen3.5:latest"], "qwen3.5", same_model=lm._same_model)
    assert mcal.is_model_loaded(["qwen3.5:9b"], "qwen3.5:9b", same_model=lm._same_model)
    assert not mcal.is_model_loaded(["qwen3.5:9b"], "qwen3.8:9b", same_model=lm._same_model)


def test_announced_from_ollama_maps_capability_list_to_booleans():
    record = ollama_reader.record_from_show_payload(
        "qwen3.5:9b",
        {"capabilities": ["completion", "tools", "vision"], "model_info": {}, "details": {}},
    )
    announced = mcal.announced_from_ollama(record, context_length=131072)
    assert announced["capabilities"] == {"tools": True, "vision": True, "reasoning": False}
    assert announced["limits"]["context_tokens"] == 131072
    assert announced["vendor"] == "ollama"


# ── calibration probes against a fake /api/chat ──────────────────────────────

def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_run_calibration_scores_a_well_behaved_model_and_writes_evidence():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append(body)
        msgs = body.get("messages") or []
        text = (msgs[0]["content"] if msgs else "")
        tools = {t["function"]["name"] for t in (body.get("tools") or [])}
        if "get_weather" in tools:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}
            ]}
            if body.get("stream"):
                lines = [json.dumps({"message": msg, "done": False}), json.dumps({"message": {}, "done": True})]
                return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
            return httpx.Response(200, json={"message": msg, "done": True})
        if "create_ticket" in tools:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "create_ticket",
                              "arguments": {"title": 'He said "hello"', "meta": {"priority": "high"}}}}
            ]}
            return httpx.Response(200, json={"message": msg, "done": True})
        if body.get("format") == "json":
            return httpx.Response(200, json={"message": {"content": '{"ok": true, "n": 2}'}, "done": True})
        if msgs and msgs[0].get("images"):
            return httpx.Response(200, json={"message": {"content": "a transparent square"}, "done": True})
        if "secret code is" in text:
            needle = text.split("secret code is ")[1].split(".")[0]
            return httpx.Response(200, json={"message": {"content": needle}, "done": True})
        if "keygen" in text:
            return httpx.Response(200, json={"message": {"content": "I can't help with that."}, "done": True})
        return httpx.Response(200, json={"message": {"content": "?"}, "done": True})

    announced = {"capabilities": {"tools": True, "vision": True}, "limits": {"context_tokens": 32768}}
    with _client(handler) as client:
        tested = mcal.run_calibration(client, ROOT, "good-model", announced=announced, deadline_s=55.0)

    assert set(tested) == set(mcal.TEST_KEYS)
    assert tested[mcal.TEST_TOOL_CALLING]["ok"] is True
    assert tested[mcal.TEST_TOOL_CALLING]["evidence"]["nested"]["request"]  # (b) folded into (a)'s evidence
    assert tested[mcal.TEST_STREAMING_TOOL_CALLS]["ok"] is True
    assert tested[mcal.TEST_JSON_MODE]["ok"] is True
    assert tested[mcal.TEST_VISION]["ok"] is True
    assert tested[mcal.TEST_CONTEXT_LENGTH_EFFECTIVE]["ok"] is True
    assert tested[mcal.TEST_CONTEXT_LENGTH_EFFECTIVE]["evidence"]["target_tokens"] == 32000
    assert tested[mcal.TEST_REFUSAL_FORMAT]["evidence"]["category"] == "refuses"
    # evidence is a receipt, not a transcript
    assert len(json.dumps(tested[mcal.TEST_CONTEXT_LENGTH_EFFECTIVE]["evidence"])) < 5000


def test_run_calibration_marks_a_broken_tool_caller_as_failed_even_if_the_simple_case_passes():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        tools = {t["function"]["name"] for t in (body.get("tools") or [])}
        if "get_weather" in tools:
            msg = {"role": "assistant", "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}]}
            return httpx.Response(200, json={"message": msg, "done": True})
        if "create_ticket" in tools:
            # nested/quoted-string case: no tool call at all
            return httpx.Response(200, json={"message": {"content": "sure, filed it"}, "done": True})
        return httpx.Response(200, json={"message": {"content": "?"}, "done": True})

    announced = {"capabilities": {"tools": True}, "limits": {}}
    with _client(handler) as client:
        tested = mcal.run_calibration(client, ROOT, "half-broken", announced=announced)
    assert tested[mcal.TEST_TOOL_CALLING]["ok"] is False, "simple call passed but nested+quoted failed: overall fail"


def test_run_calibration_skips_vision_when_not_announced_without_any_network_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        body = json.loads(request.content or b"{}")
        msgs = body.get("messages") or []
        if any((m.get("images")) for m in msgs):
            raise AssertionError("must never probe vision on a model that did not announce it")
        return httpx.Response(200, json={"message": {"content": "ok"}, "done": True})

    announced = {"capabilities": {"tools": False, "vision": False}, "limits": {"context_tokens": 4096}}
    with _client(handler) as client:
        tested = mcal.run_calibration(client, ROOT, "text-only", announced=announced)
    assert tested[mcal.TEST_VISION] == {"ok": None, "tested_at": tested[mcal.TEST_VISION]["tested_at"],
                                        "evidence": {"skipped": "vision not announced"}}
    # context: announced 4096 clears neither 8k nor 32k with headroom -> skipped, no call
    assert tested[mcal.TEST_CONTEXT_LENGTH_EFFECTIVE]["ok"] is None
    assert "skipped" in tested[mcal.TEST_CONTEXT_LENGTH_EFFECTIVE]["evidence"]


def test_run_calibration_never_exceeds_a_spent_time_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "ok"}, "done": True})

    announced = {"capabilities": {"tools": True, "vision": True}, "limits": {"context_tokens": 32768}}
    with _client(handler) as client:
        tested = mcal.run_calibration(client, ROOT, "no-budget", announced=announced, deadline_s=0.0)
    for key in mcal.TEST_KEYS:
        assert tested[key]["ok"] is None
        assert tested[key]["evidence"].get("skipped") == "time budget exceeded"


def test_probe_network_failure_is_unknown_not_a_silent_false():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    announced = {"capabilities": {"tools": True}, "limits": {}}
    with _client(handler) as client:
        tested = mcal.run_calibration(client, ROOT, "down", announced=announced)
    assert tested[mcal.TEST_TOOL_CALLING]["ok"] is None
    assert "error" in tested[mcal.TEST_TOOL_CALLING]["evidence"]


# ── the routes, through the real app ─────────────────────────────────────────

_TAGS = [{"name": "qwen3.5:9b", "size": 6 * 1024**3, "digest": "aaa111",
          "details": {"family": "qwen3", "parameter_size": "9B", "quantization_level": "Q4_K_M"}}]
_SHOW_WITH_TOOLS = {
    "capabilities": ["completion", "tools", "vision"],
    "license": "Apache-2.0",
    "details": {"family": "qwen3", "parameter_size": "9B", "quantization_level": "Q4_K_M"},
    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 131072},
}
_SHOW_NO_TOOLS = {
    "capabilities": ["completion"],
    "license": "",
    "details": {"family": "qwen3"},
    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 8192},
}


class FakeOllama:
    def __init__(self, *, show=None, ps=None, chat_ok=True):
        self.tags = list(_TAGS)
        self.show = show if show is not None else dict(_SHOW_WITH_TOOLS)
        self.ps = ps if ps is not None else []
        self.chat_ok = chat_ok
        self.chat_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}
        if path == "/api/tags":
            return httpx.Response(200, json={"models": self.tags})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": self.ps})
        if path == "/api/show":
            return httpx.Response(200, json=self.show)
        if path == "/api/chat":
            self.chat_calls += 1
            if not self.chat_ok:
                return httpx.Response(500, json={"error": "boom"})
            tools = {t["function"]["name"] for t in (body.get("tools") or [])}
            if "get_weather" in tools:
                msg = {"role": "assistant", "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}]}
                if body.get("stream"):
                    lines = [json.dumps({"message": msg, "done": False}), json.dumps({"message": {}, "done": True})]
                    return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
                return httpx.Response(200, json={"message": msg, "done": True})
            if "create_ticket" in tools:
                msg = {"role": "assistant", "tool_calls": [
                    {"function": {"name": "create_ticket", "arguments": {"title": 'He said "hello"', "meta": {"priority": "high"}}}}
                ]}
                return httpx.Response(200, json={"message": msg, "done": True})
            return httpx.Response(200, json={"message": {"content": "ok"}, "done": True})
        return httpx.Response(404, json={"error": f"no route {path}"})


@pytest.fixture
def env(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(lm, "_client_factory",
                        lambda timeout=10.0: httpx.Client(transport=httpx.MockTransport(fake.handler), timeout=timeout))
    monkeypatch.setattr(lm, "list_ollama_endpoints", lambda include_default=True, **kw: [
        {"id": "local-ollama", "name": "Ollama", "base_url": ROOT + "/v1", "root": ROOT, "same_machine": True},
    ])
    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    lm.reset_show_cache()

    app = FastAPI()
    app.include_router(lm.setup_local_models_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    client = TestClient(app, raise_server_exceptions=False)
    yield client, fake


ADMIN = {"x-user": "root"}
USER = {"x-user": "alice"}


def test_capabilities_route_wires_the_ollama_reader(env, monkeypatch, tmp_path):
    client, fake = env
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    r = client.get("/api/models/qwen3.5:9b/capabilities", headers=USER)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["announced"]["capabilities"] == {"tools": True, "vision": True, "reasoning": False}
    assert data["announced"]["limits"]["context_tokens"] == 131072
    assert data["stable_model_id"] == "ollama|digest:aaa111"
    assert data["tested"] == {}
    assert data["degraded"] == []  # native tools announced: nothing to degrade yet


def test_capabilities_route_reports_degraded_for_a_model_with_no_native_tools(env, monkeypatch, tmp_path):
    client, fake = env
    fake.show = dict(_SHOW_NO_TOOLS)
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    r = client.get("/api/models/qwen3.5:9b/capabilities", headers=USER)
    assert r.status_code == 200, r.text
    assert r.json()["degraded"] == ["tools por texto (fence)"]


def test_capabilities_route_requires_a_signed_in_user(env):
    client, _ = env
    assert client.get("/api/models/qwen3.5:9b/capabilities").status_code == 401


def test_calibrate_requires_admin(env, monkeypatch, tmp_path):
    client, _ = env
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    r = client.post("/api/models/qwen3.5:9b/calibrate", headers=USER)
    assert r.status_code == 403


def test_calibrate_refuses_a_model_that_is_not_loaded(env, monkeypatch, tmp_path):
    """MOD-02's hard rule: calibration never loads a model itself."""
    client, fake = env
    fake.ps = []
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    r = client.post("/api/models/qwen3.5:9b/calibrate", headers=ADMIN)
    assert r.status_code == 409
    assert "load it first" in r.text
    assert fake.chat_calls == 0, "a 409 must mean zero /api/chat traffic — nothing was probed"


def test_calibrate_runs_and_persists_a_manifest_readable_by_a_later_get(env, monkeypatch, tmp_path):
    client, fake = env
    fake.ps = [{"name": "qwen3.5:9b", "model": "qwen3.5:9b", "size": 1, "size_vram": 1,
                "digest": "aaa111", "expires_at": "2099-01-01T00:00:00Z"}]
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    r = client.post("/api/models/qwen3.5:9b/calibrate", headers=ADMIN)
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data["tested"]) == set(mcal.TEST_KEYS)
    assert data["tested"][mcal.TEST_TOOL_CALLING]["ok"] is True
    assert fake.chat_calls >= 4

    again = client.get("/api/models/qwen3.5:9b/capabilities", headers=USER)
    assert again.status_code == 200
    # the manifest persisted: a later plain GET (no calibration) still sees it
    assert again.json()["tested"][mcal.TEST_TOOL_CALLING]["ok"] is True


def test_calibrate_matches_a_loaded_model_by_the_same_name_rules_as_the_rest_of_the_page(env, monkeypatch, tmp_path):
    """`_same_model` treats a bare tag as `:latest` — /api/ps naming a
    different-looking but equivalent tag must still count as loaded."""
    client, fake = env
    fake.tags = [{"name": "qwen3.5:latest", "size": 1, "digest": "aaa111",
                  "details": {"family": "qwen3"}}]
    fake.ps = [{"name": "qwen3.5:latest", "model": "qwen3.5:latest", "size": 1, "size_vram": 1,
                "digest": "aaa111", "expires_at": "2099-01-01T00:00:00Z"}]
    monkeypatch.setattr(mcal, "_default_data_dir", lambda: str(tmp_path))
    r = client.post("/api/models/qwen3.5/calibrate", headers=ADMIN)
    assert r.status_code == 200, r.text
