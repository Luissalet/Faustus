"""src/launch_receipts.py — INF-02 A4.

No real network: every probe goes through `httpx.MockTransport` (no respx
installed in this repo), reached by monkeypatching `httpx.AsyncClient` to
always use it. `RECEIPTS_DIR` is monkeypatched to a tmp directory so nothing
here touches the real `data/` tree.
"""
from __future__ import annotations

import functools

import httpx
import pytest

from src import launch_receipts as lr
from src.contracts.inference import CapabilityAssessment, EngineIdentity


@pytest.fixture(autouse=True)
def _receipts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, "RECEIPTS_DIR", str(tmp_path / "launch_receipts"))


def _mock_httpx(monkeypatch, handler):
    """Route every `httpx.AsyncClient()` call (anywhere `launch_receipts`
    constructs one) through a `MockTransport`, so no real socket is ever
    opened. `handler` may be sync or async, per httpx.MockTransport's own
    contract."""
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    factory = functools.partial(real_async_client, transport=transport)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _assessment(option, requested, requirements=()):
    return CapabilityAssessment(
        option=option, requested=requested, support="supported",
        scope="server_start", requirements=tuple(requirements),
    )


# ── record(): generation bump + history ─────────────────────────────────────

def test_record_first_launch_is_generation_one_with_no_history():
    engine = EngineIdentity(implementation="llama-server")
    receipt = lr.record(
        "serve-aaaaaaaa", engine=engine, model=None,
        requested_cmd="llama-server --ctx-size 8192",
        final_cmd="llama-server --ctx-size 8192",
        rewrites=[], plan={"manual": True}, assessments=[],
    )
    assert receipt.engine.generation == 1
    assert receipt.verify_state == "pending"
    assert receipt.engine.session_id == "serve-aaaaaaaa"

    raw = lr._read_raw("serve-aaaaaaaa")
    assert raw["history"] == []


def test_second_record_of_same_session_bumps_generation_and_files_history():
    engine = EngineIdentity(implementation="llama-server")
    first = lr.record(
        "serve-bbbbbbbb", engine=engine, model=None,
        requested_cmd="cmd1", final_cmd="cmd1",
        rewrites=[], plan={"manual": True}, assessments=[],
    )
    assert first.engine.generation == 1

    second = lr.record(
        "serve-bbbbbbbb", engine=engine, model=None,
        requested_cmd="cmd2", final_cmd="cmd2",
        rewrites=[], plan={"manual": True}, assessments=[],
    )
    assert second.engine.generation == 2
    assert second.final_cmd == "cmd2"

    raw = lr._read_raw("serve-bbbbbbbb")
    assert len(raw["history"]) == 1
    assert raw["history"][0]["final_cmd"] == "cmd1"
    assert raw["history"][0]["engine"]["generation"] == 1


def test_get_returns_none_for_unknown_session():
    assert lr.get("serve-doesnotexist") is None


def test_mark_stale_sets_verify_state_and_reason():
    engine = EngineIdentity(implementation="llama-server")
    lr.record(
        "serve-cccccccc", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={"manual": True}, assessments=[],
    )
    updated = lr.mark_stale("serve-cccccccc", "final_cmd changed between generations")
    assert updated.verify_state == "stale"
    freshness = [c for c in updated.checks if c.name == "freshness"][0]
    assert freshness.state == "failed"
    assert "changed" in freshness.detail

    assert lr.mark_stale("serve-does-not-exist", "x") is None


# ── verify(): llama-server /props mismatch ──────────────────────────────────

def _llama_props_handler(n_ctx: int, flash_attn: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={
                "default_generation_settings": {
                    "n_ctx": n_ctx,
                    "params": {"flash_attn": flash_attn, "cache_type_k": "f16", "cache_type_v": "f16"},
                },
                "total_slots": 1,
                "model_path": "/models/foo.gguf",
                "build_info": "b4600",
            })
        if request.url.path == "/slots":
            return httpx.Response(200, json=[{"n_ctx": n_ctx}])
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "foo"}]})
        return httpx.Response(404)
    return handler


@pytest.mark.asyncio
async def test_verify_llama_server_reports_mismatch_and_difference(monkeypatch):
    _mock_httpx(monkeypatch, _llama_props_handler(n_ctx=8192))
    engine = EngineIdentity(implementation="llama-server")
    lr.record(
        "serve-dddddddd", engine=engine, model=None,
        requested_cmd="llama-server --ctx-size 16384",
        final_cmd="llama-server --ctx-size 16384",
        rewrites=[], plan={"implementation": "llama-server", "options": {"ctx": 16384}},
        assessments=[_assessment("ctx", 16384)],
    )

    updated = await lr.verify("serve-dddddddd", base_url="http://127.0.0.1:8080")

    assert updated.verify_state == "verified"
    reachable = [c for c in updated.checks if c.name == "http_reachable"][0]
    assert reachable.state == "passed"
    ctx_assessment = [a for a in updated.assessments if a.option == "ctx"][0]
    assert ctx_assessment.effective.state == "mismatch"
    assert ctx_assessment.effective.value == 8192
    # A probe that actually answered this option supersedes the pre-launch
    # manifest evidence with what was actually observed.
    assert ctx_assessment.evidence.kind == "engine_probe"
    assert ctx_assessment.evidence.observed_at == updated.verified_at
    assert len(updated.differences) == 1
    assert updated.differences[0].requested == 16384
    assert updated.differences[0].observed == 8192


@pytest.mark.asyncio
async def test_verify_llama_server_confirms_matching_ctx(monkeypatch):
    _mock_httpx(monkeypatch, _llama_props_handler(n_ctx=16384))
    engine = EngineIdentity(implementation="llama-server")
    lr.record(
        "serve-eeeeeeee", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={"implementation": "llama-server", "options": {"ctx": 16384}},
        assessments=[_assessment("ctx", 16384)],
    )
    updated = await lr.verify("serve-eeeeeeee", base_url="http://127.0.0.1:8080")
    ctx_assessment = [a for a in updated.assessments if a.option == "ctx"][0]
    assert ctx_assessment.effective.state == "confirmed"
    assert ctx_assessment.evidence.kind == "engine_probe"
    assert updated.differences == ()


# ── verify(): Ollama /api/ps ─────────────────────────────────────────────────

def _ollama_handler(context_length):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.5.0"})
        if request.url.path == "/api/ps":
            model = {"name": "llama3:8b"}
            if context_length is not None:
                model["context_length"] = context_length
            return httpx.Response(200, json={"models": [model]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {}})
        return httpx.Response(404)
    return handler


@pytest.mark.asyncio
async def test_verify_ollama_confirms_num_ctx_when_context_length_present(monkeypatch):
    _mock_httpx(monkeypatch, _ollama_handler(context_length=4096))
    engine = EngineIdentity(implementation="ollama")
    lr.record(
        "serve-ffffffff", engine=engine, model=None,
        requested_cmd="ollama serve", final_cmd="ollama serve",
        rewrites=[],
        plan={"implementation": "ollama", "model": "llama3:8b", "options": {"num_ctx": 4096}},
        assessments=[_assessment("num_ctx", 4096)],
    )
    updated = await lr.verify("serve-ffffffff", base_url="http://127.0.0.1:11434")
    a = [x for x in updated.assessments if x.option == "num_ctx"][0]
    assert a.effective.state == "confirmed"
    assert a.effective.value == 4096


@pytest.mark.asyncio
async def test_verify_ollama_unconfirmed_when_context_length_absent(monkeypatch):
    _mock_httpx(monkeypatch, _ollama_handler(context_length=None))
    engine = EngineIdentity(implementation="ollama")
    lr.record(
        "serve-gggggggg", engine=engine, model=None,
        requested_cmd="ollama serve", final_cmd="ollama serve",
        rewrites=[],
        plan={"implementation": "ollama", "model": "llama3:8b", "options": {"num_ctx": 4096}},
        assessments=[_assessment("num_ctx", 4096)],
    )
    updated = await lr.verify("serve-gggggggg", base_url="http://127.0.0.1:11434")
    a = [x for x in updated.assessments if x.option == "num_ctx"][0]
    assert a.effective.state == "unconfirmed"
    assert a.effective.value is None


# ── verify(): unreachable probe never invents values ────────────────────────

@pytest.mark.asyncio
async def test_verify_unreachable_engine_fails_without_inventing_values(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)
    _mock_httpx(monkeypatch, handler)

    engine = EngineIdentity(implementation="llama-server")
    lr.record(
        "serve-hhhhhhhh", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={"implementation": "llama-server", "options": {"ctx": 8192}},
        assessments=[_assessment("ctx", 8192)],
    )
    updated = await lr.verify("serve-hhhhhhhh", base_url="http://127.0.0.1:8080")

    assert updated.verify_state == "failed"
    reachable = [c for c in updated.checks if c.name == "http_reachable"][0]
    assert reachable.state == "failed"
    assert updated.observed == {}
    # The pre-launch assessment is untouched -- still unconfirmed, not
    # flipped to confirmed/mismatch by a probe that never answered.
    ctx_assessment = [a for a in updated.assessments if a.option == "ctx"][0]
    assert ctx_assessment.effective.state == "unconfirmed"


# ── verify(): authorized_probe gating ───────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_probe_is_skipped_without_authorization(monkeypatch):
    _mock_httpx(monkeypatch, _llama_props_handler(n_ctx=8192))
    engine = EngineIdentity(implementation="llama-server")
    lr.record(
        "serve-iiiiiiii", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={"implementation": "llama-server", "options": {}},
        assessments=[],
    )
    updated = await lr.verify("serve-iiiiiiii", base_url="http://127.0.0.1:8080", authorized_probe=False)
    chat_probe = [c for c in updated.checks if c.name == "chat_probe"][0]
    assert chat_probe.state == "skipped"
    assert chat_probe.detail == "not authorized"


@pytest.mark.asyncio
async def test_chat_probe_runs_when_authorized(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 8192, "params": {}},
                "model_path": "/models/foo.gguf",
            })
        if request.url.path == "/slots":
            return httpx.Response(404)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "foo"}]})
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        return httpx.Response(404)

    _mock_httpx(monkeypatch, handler)
    engine = EngineIdentity(implementation="llama-server")
    lr.record(
        "serve-jjjjjjjj", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={"implementation": "llama-server", "model": "foo", "options": {}},
        assessments=[],
    )
    updated = await lr.verify("serve-jjjjjjjj", base_url="http://127.0.0.1:8080", authorized_probe=True)
    chat_probe = [c for c in updated.checks if c.name == "chat_probe"][0]
    assert chat_probe.state == "passed"


# ── verify() raises for a session with no receipt ───────────────────────────

@pytest.mark.asyncio
async def test_verify_raises_for_missing_session(monkeypatch):
    with pytest.raises(lr.LaunchReceiptError):
        await lr.verify("serve-missing", base_url="http://127.0.0.1:8080")


# ── find_by_endpoint() / identity_for_endpoint() — INF-03 ───────────────────

def test_find_by_endpoint_matches_host_and_port():
    engine = EngineIdentity(implementation="llama-server", host="127.0.0.1", port=8090)
    lr.record(
        "serve-kkkkkkkk", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={}, assessments=[],
    )
    found = lr.find_by_endpoint("127.0.0.1", 8090)
    assert found is not None
    assert found.session_id == "serve-kkkkkkkk"
    assert found.engine.implementation == "llama-server"


def test_find_by_endpoint_none_for_unmatched_host_or_port():
    engine = EngineIdentity(implementation="llama-server", host="127.0.0.1", port=8090)
    lr.record(
        "serve-llllllll", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={}, assessments=[],
    )
    assert lr.find_by_endpoint("127.0.0.1", 9999) is None
    assert lr.find_by_endpoint("10.0.0.5", 8090) is None
    assert lr.find_by_endpoint(None, 8090) is None
    assert lr.find_by_endpoint("127.0.0.1", None) is None


def test_find_by_endpoint_picks_newest_generation_across_sessions(monkeypatch):
    # `created_at` has only second precision (contracts/base.now_iso) — pin
    # it explicitly instead of racing the wall clock within one test.
    monkeypatch.setattr(lr, "now_iso", lambda: "2026-01-01T00:00:00Z")
    older = EngineIdentity(implementation="llama-server", host="127.0.0.1", port=8091)
    lr.record(
        "serve-mmmmmmmm", engine=older, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={}, assessments=[],
    )
    monkeypatch.setattr(lr, "now_iso", lambda: "2026-01-01T00:00:05Z")
    newer = EngineIdentity(implementation="vllm", host="127.0.0.1", port=8091)
    lr.record(
        "serve-nnnnnnnn", engine=newer, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={}, assessments=[],
    )
    found = lr.find_by_endpoint("127.0.0.1", 8091)
    assert found is not None
    assert found.session_id == "serve-nnnnnnnn"
    assert found.engine.implementation == "vllm"


def test_identity_for_endpoint_prefers_a_matching_receipt():
    engine = EngineIdentity(implementation="llama-server", host="127.0.0.1", port=8092, version="b1234")
    lr.record(
        "serve-oooooooo", engine=engine, model=None,
        requested_cmd="cmd", final_cmd="cmd",
        rewrites=[], plan={}, assessments=[],
    )
    identity = lr.identity_for_endpoint("http://127.0.0.1:8092/v1/chat/completions")
    assert identity is not None
    assert identity.implementation == "llama-server"
    assert identity.managed == "faustus"
    assert identity.version == "b1234"
    assert identity.session_id == "serve-oooooooo"


def test_identity_for_endpoint_falls_back_to_ollama_hint_as_external():
    identity = lr.identity_for_endpoint(
        "http://localhost:11434/api/chat", implementation_hint="ollama",
    )
    assert identity is not None
    assert identity.implementation == "ollama"
    assert identity.managed == "external"
    assert identity.host == "localhost"
    assert identity.port == 11434


def test_identity_for_endpoint_none_for_unverifiable_llamacpp_hint():
    # No receipt, and "llamacpp" cannot tell native binary from Python
    # wrapper apart (§06 H04) — never guess one of IMPLEMENTATIONS's two
    # llama.cpp entries.
    assert lr.identity_for_endpoint(
        "http://127.0.0.1:8099/v1", implementation_hint="llamacpp",
    ) is None


def test_identity_for_endpoint_none_for_cloud_provider_with_no_hint():
    assert lr.identity_for_endpoint("https://api.openai.com/v1") is None
