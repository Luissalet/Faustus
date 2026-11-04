"""Back-off of capability probes against servers that refuse connections
(src/probe_backoff.py) and the auto-detection paths that honour it: the
vision helper, the llama.cpp / LM Studio / Ollama / OpenAI-compatible probes,
model_context's probes, hedge targets, and the REST routes behind Settings ›
Models › Paused model probes."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from src import probe_backoff as pb

DEAD = "http://127.0.0.1:8081/v1"
REFUSED = "[WinError 10061] No connection could be made because the target machine actively refused it"


@pytest.fixture(autouse=True)
def _clean():
    pb.clear()
    yield
    pb.clear()


def _refused():
    return httpx.ConnectError(REFUSED)


def test_two_refusals_pause_the_server_one_does_not():
    assert pb.note_failure(DEAD, _refused()) is False
    assert not pb.skipped(DEAD)
    assert pb.note_failure(DEAD + "/chat/completions", _refused()) is True
    # Any path on the same scheme://host:port shares the pause.
    assert pb.skipped("http://127.0.0.1:8081/props")
    assert not pb.skipped("http://127.0.0.1:8082/v1")


def test_only_refusals_count():
    remote = "http://192.168.0.185:8888/v1"
    for _ in range(3):
        pb.note_failure(DEAD, httpx.ReadTimeout("read timed out"))
        pb.note_failure(remote, httpx.ConnectTimeout("timed out"))
        pb.note_failure(DEAD, ValueError("bad json"))
    assert pb.status() == []
    # A connect timeout to loopback is a refusal reported late (1 s probe vs
    # ~2 s for Windows to report the refusal).
    pb.note_failure(DEAD, httpx.ConnectTimeout("timed out"))
    pb.note_failure(DEAD, httpx.ConnectTimeout("timed out"))
    assert pb.skipped(DEAD)
    # urllib wraps the refusal in URLError(reason=ConnectionRefusedError).
    import urllib.error
    url = "http://127.0.0.1:8082/v1/chat/completions"
    pb.note_failure(url, urllib.error.URLError(ConnectionRefusedError(10061, "refused")))
    pb.note_failure(url, urllib.error.URLError(ConnectionRefusedError(10061, "refused")))
    assert pb.skipped(url)


def test_window_doubles_and_caps(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pb.time, "time", lambda: now[0])
    pb.note_failure(DEAD, _refused())
    pb.note_failure(DEAD, _refused())
    assert pb.status()[0]["retry_in_s"] == pb.BASE_S
    now[0] += pb.BASE_S + 1  # window over: one probe goes out and is refused again
    assert not pb.skipped(DEAD)
    pb.note_failure(DEAD, _refused())
    assert pb.status()[0]["retry_in_s"] == pb.BASE_S * 2
    for _ in range(20):
        pb.note_failure(DEAD, _refused())
    assert pb.status()[0]["retry_in_s"] == pb.MAX_S


def test_an_answer_or_a_real_request_ends_the_pause():
    from src import llm_core
    for _ in range(2):
        pb.note_failure(DEAD, _refused())
    pb.note_success("http://127.0.0.1:8081/props")
    assert not pb.skipped(DEAD) and pb.status() == []
    for _ in range(2):
        pb.note_failure(DEAD, _refused())
    llm_core._clear_host_dead(DEAD + "/chat/completions")
    assert not pb.skipped(DEAD)


def test_clear_one_or_all():
    for url in (DEAD, "http://127.0.0.1:8082/v1"):
        pb.note_failure(url, _refused())
        pb.note_failure(url, _refused())
    assert pb.clear("http://127.0.0.1:8081/anything") == 1
    assert [r["server"] for r in pb.status()] == ["http://127.0.0.1:8082"]
    assert pb.clear() == 1 and pb.status() == []


def test_llamacpp_and_lmstudio_probes_stop_calling_a_refusing_server(monkeypatch):
    from src import chat_helpers as ch
    calls = []

    def fake_get(url, *a, **k):
        calls.append(url)
        raise _refused()

    monkeypatch.setattr(ch.httpx, "get", fake_get)
    ch._llamacpp_props_cache.clear()
    ch._lmstudio_models_cache.clear()
    for _ in range(5):
        assert ch.llamacpp_supports_vision(DEAD) is None
        assert ch.lmstudio_supports_vision(DEAD, "qwen") is None
        assert ch.llamacpp_reasoning_efforts(DEAD) is None
    assert len(calls) == pb.FAIL_THRESHOLD


def test_ollama_and_openai_compat_probes_skip_a_paused_server(monkeypatch):
    from src import chat_helpers as ch
    from src import llm_core
    for _ in range(2):
        pb.note_failure(DEAD, _refused())
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed a paused server"))  # noqa: E731
    monkeypatch.setattr(llm_core.httpx, "post", boom)
    monkeypatch.setattr(ch, "_tokenize_vision_answer", boom)
    llm_core._ollama_caps_cache.clear()
    assert llm_core._ollama_model_caps(DEAD, "qwen") is None
    assert ("http://127.0.0.1:8081", "qwen") not in llm_core._ollama_caps_cache
    assert ch.openai_compat_supports_vision(DEAD, "qwen") is None


def test_openai_compat_probe_feeds_the_backoff(monkeypatch):
    from src import chat_helpers as ch

    def unreachable(url, model):
        raise ch._ServerUnreachable() from ConnectionRefusedError(10061, "refused")

    monkeypatch.setattr(ch, "_tokenize_vision_answer", unreachable)
    ch._openai_vision_cache.clear()
    ch.openai_compat_supports_vision(DEAD, "m")
    ch.openai_compat_supports_vision(DEAD, "m")
    assert pb.skipped(DEAD)


def test_model_context_and_hedge_treat_a_paused_server_as_dead():
    from src import model_context, model_hedge
    assert not model_context._probe_host_dead(DEAD)
    model_context._probe_failed(DEAD, _refused())
    model_context._probe_failed(DEAD, _refused())
    assert model_context._probe_host_dead(DEAD)
    assert model_hedge._host_dead(DEAD + "/chat/completions")


def test_vision_auto_detection_skips_a_paused_endpoint(monkeypatch):
    from src import vision_routing as vr
    probed = []

    def probe(url, models, deadline):
        probed.append(url)
        return (["qwen3-vl:8b"] if "11434" in url else []), set()

    monkeypatch.setattr(vr, "_probe_endpoint", probe)
    monkeypatch.setattr(vr, "rank_capable", lambda url, capable: list(capable))
    monkeypatch.setattr(vr, "_user_pref", lambda key, owner: None)
    monkeypatch.setattr(vr, "_global_setting", lambda key, default="": default)
    dead = {"id": "dead", "name": "dead", "url": DEAD + "/chat/completions", "headers": {},
            "models": ["llava:7b"], "local": True}
    alive = {"id": "ok", "name": "ok", "url": "http://127.0.0.1:11434/v1/chat/completions",
             "headers": {}, "models": ["qwen3-vl:8b"], "local": True}
    for _ in range(2):
        pb.note_failure(DEAD, _refused())
    route = vr._pick_on_endpoints([dead, alive], None, "auto")
    assert probed == [alive["url"]]
    assert route["endpoint_id"] == "ok"
    # Its cached list does not win the name match either: nothing answers there.
    probed.clear()
    monkeypatch.setattr(vr, "_probe_endpoint", lambda url, models, deadline: ([], set()))
    assert vr._pick_on_endpoints([dead], None, "auto") is None


def test_live_inventory_skips_a_paused_ollama(monkeypatch):
    from src import vision_routing as vr
    vr.clear_cache()
    url = "http://127.0.0.1:11434/v1/chat/completions"
    for _ in range(2):
        pb.note_failure(url, _refused())
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert vr.live_local_inventory(url) == {"models": {}, "loaded": set()}


def test_rest_status_maps_endpoints_and_clear_resumes(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.model_routes as mr

    rows = [SimpleNamespace(id="3202765f", name="llama.cpp (local)", base_url="http://127.0.0.1:8081/v1"),
            SimpleNamespace(id="other", name="Other", base_url="http://127.0.0.1:9999/v1")]

    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return rows

        def first(self):
            return rows[0]

    class _Db:
        def query(self, *a, **k):
            return _Q()

        def close(self):
            pass

    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(mr, "require_user", lambda request: "jane")
    monkeypatch.setattr(mr, "effective_user", lambda request: None)
    app = FastAPI()
    app.include_router(mr.setup_model_routes(SimpleNamespace()))
    client = TestClient(app)
    assert client.get("/api/model-endpoints/probe-backoff").json()["servers"] == []
    for _ in range(2):
        pb.note_failure(DEAD, _refused())
    data = client.get("/api/model-endpoints/probe-backoff").json()
    assert [s["server"] for s in data["servers"]] == ["http://127.0.0.1:8081"]
    assert data["servers"][0]["skipped"] is True
    assert data["endpoints"] == {"3202765f": "http://127.0.0.1:8081"}
    assert data["endpoint_names"] == {"3202765f": "llama.cpp (local)"}
    r = client.post("/api/model-endpoints/probe-backoff/clear", json={"endpoint_id": "3202765f"})
    assert r.status_code == 200 and r.json()["cleared"] == 1
    assert not pb.skipped(DEAD)
