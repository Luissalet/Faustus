"""Vision routing: one resolver for the vision model that reads images for a
text-only chat model (src/vision_routing.py), and the places that use it —
chat attachments (off the event loop), tool-result images, the per-request
history filter, `/api/models`' `models_vision` and `/api/vision/status`."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from src import chat_helpers as ch
from src import vision_routing as vr


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    vr.clear_cache()
    # No per-user prefs and empty globals unless a test says otherwise.
    monkeypatch.setattr(vr, "_user_pref", lambda key, owner: None)
    monkeypatch.setattr(vr, "_global_setting", lambda key, default="": {
        "vision_history_filter": True, "vision_enabled": True}.get(key, default))
    yield
    vr.clear_cache()


def _ep(ep_id, url, models, local=True):
    return {"id": ep_id, "name": ep_id.upper(), "url": url, "headers": {"X": ep_id},
            "models": list(models), "local": local}


def _no_probes(monkeypatch, ollama=None, llama=None, lmstudio=None):
    monkeypatch.setattr(ch, "lmstudio_supports_vision", lambda url, m: (lmstudio or {}).get(m))
    monkeypatch.setattr(ch, "llamacpp_supports_vision", lambda url: llama)
    monkeypatch.setattr(ch, "ollama_supports_vision", lambda url, m: (ollama or {}).get(m))


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def test_auto_prefers_a_local_model_the_server_reports_as_vision(monkeypatch):
    endpoints = [
        _ep("cloud", "https://api.example.com/v1/chat/completions", ["gpt-4o"], local=False),
        _ep("ollama", "http://127.0.0.1:11434/v1/chat/completions", ["qwen3:8b", "qwen3.5:9b"]),
    ]
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: endpoints)
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch, ollama={"qwen3:8b": False, "qwen3.5:9b": True})

    route = vr.resolve_vision_route("alice-vr")
    assert route["source"] == "auto"
    assert (route["model"], route["endpoint_id"], route["local"]) == ("qwen3.5:9b", "ollama", True)


def test_auto_falls_back_to_known_names_and_skips_reported_text_only(monkeypatch):
    endpoints = [_ep("ollama", "http://127.0.0.1:11434/v1/chat/completions",
                     ["gemma3:1b", "llama3.2-vision:11b"])]
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: endpoints)
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    # gemma3:1b is text-only per the server; the other is unknown (no caps).
    _no_probes(monkeypatch, ollama={"gemma3:1b": False})

    route = vr.resolve_vision_route("bob-vr")
    assert route["model"] == "llama3.2-vision:11b"


def test_auto_never_picks_an_endpoint_the_privacy_gate_refuses(monkeypatch):
    endpoints = [_ep("cloud", "https://api.example.com/v1/chat/completions", ["gpt-4o"], local=False)]
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: endpoints)
    _no_probes(monkeypatch)
    from src import privacy_policy

    def gate(component, url, owner=None, **kw):
        assert component == "ocr_vision"
        raise privacy_policy.PrivacyPolicyError(component, url, "local_only", None)

    monkeypatch.setattr(privacy_policy, "assert_outbound", gate)
    route = vr.resolve_vision_route("carol-vr")
    assert route["source"] == "none" and route["model"] == ""


def test_auto_result_is_cached_briefly(monkeypatch):
    calls = []
    endpoints = [_ep("lm", "http://127.0.0.1:1234/v1/chat/completions", ["qwen2.5-vl-7b"])]

    def listing(owner):
        calls.append(owner)
        return endpoints

    monkeypatch.setattr(vr, "list_vision_endpoints", listing)
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch)
    first = vr.resolve_vision_route("dave-vr")
    second = vr.resolve_vision_route("dave-vr")
    assert first["model"] == second["model"] == "qwen2.5-vl-7b"
    assert calls == ["dave-vr"]


def test_configured_endpoint_resolves_the_model_there(monkeypatch):
    endpoints = [
        _ep("a", "http://127.0.0.1:11434/v1/chat/completions", ["llava:7b"]),
        _ep("b", "http://10.0.0.5:8080/v1/chat/completions", ["qwen3-vl:8b", "qwen3:8b"]),
    ]
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: endpoints)
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch)
    monkeypatch.setattr(vr, "_global_setting", lambda key, default="": {
        "vision_endpoint_id": "b"}.get(key, default))

    # No model: the endpoint's vision-capable one.
    route = vr.resolve_vision_route("erin-vr")
    assert (route["source"], route["endpoint_id"], route["model"]) == ("configured", "b", "qwen3-vl:8b")
    # A model: resolved on that endpoint, not searched elsewhere.
    route = vr.resolve_vision_route("erin-vr", configured="qwen3:8b")
    assert (route["endpoint_id"], route["model"]) == ("b", "qwen3:8b")


def test_per_user_endpoint_setting_wins(monkeypatch):
    endpoints = [_ep("mine", "http://127.0.0.1:1234/v1/chat/completions", ["moondream"])]
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: endpoints)
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch)
    monkeypatch.setattr(vr, "_user_pref", lambda key, owner: "mine" if key == "vision_endpoint_id" else None)
    assert vr.configured_vision_endpoint_id("frank") == "mine"
    assert vr.resolve_vision_route("frank")["endpoint_id"] == "mine"


def test_configured_model_that_is_missing_is_not_replaced_by_auto(monkeypatch):
    from src import ai_interaction

    def fake_resolve(spec, owner=None):
        raise ValueError(f"Model '{spec}' not found on any configured endpoint")

    monkeypatch.setattr(ai_interaction, "_resolve_model", fake_resolve)
    monkeypatch.setattr(vr, "list_vision_endpoints",
                        lambda owner: [_ep("x", "http://127.0.0.1:1/v1/chat/completions", ["llava"])])
    route = vr.resolve_vision_route("gina-vr", configured="missing-vl")
    assert route["source"] == "none" and "not found" in route["error"]


def test_document_processor_resolver_uses_the_shared_route(monkeypatch):
    from src import document_processor as dp

    monkeypatch.setattr(vr, "list_vision_endpoints",
                        lambda owner: [_ep("o", "http://127.0.0.1:11434/v1/chat/completions", ["minicpm-v:8b"])])
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch)
    assert dp._resolve_vl_model("", owner="hal-vr") == (
        "http://127.0.0.1:11434/v1/chat/completions", "minicpm-v:8b", {"X": "o"})
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: [])
    from src import ai_interaction
    monkeypatch.setattr(ai_interaction, "_resolve_model",
                        lambda spec, owner=None: (_ for _ in ()).throw(ValueError("none")))
    vr.clear_cache()
    with pytest.raises(ValueError):
        dp._resolve_vl_model("", owner="hal-vr")


def test_modern_local_vlm_names_are_candidates():
    for name in ("qwen3-vl", "qwen2.5vl", "gemma3", "llama3.2-vision", "minicpm-v", "moondream"):
        assert name in vr.AUTO_CANDIDATES


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_vision_status_reports_source_and_model(monkeypatch):
    monkeypatch.setattr(vr, "list_vision_endpoints",
                        lambda owner: [_ep("o", "http://127.0.0.1:11434/v1/chat/completions", ["llava:7b"])])
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch)
    status = vr.vision_status("ivy-vr")
    assert status["source"] == "auto" and status["model"] == "llava:7b"
    assert status["endpoint_name"] == "O" and "headers" not in status


def test_vision_status_route_is_owner_scoped(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.model_routes as mr

    seen = []
    monkeypatch.setattr(vr, "vision_status", lambda owner: seen.append(owner) or {"source": "none"})
    monkeypatch.setattr(mr, "require_user", lambda request: "jane")
    monkeypatch.setattr(mr, "effective_user", lambda request: "jane")
    app = FastAPI()
    app.include_router(mr.setup_model_routes(SimpleNamespace()))
    r = TestClient(app).get("/api/vision/status")
    assert r.status_code == 200 and r.json() == {"source": "none"}
    assert seen == ["jane"]


# ---------------------------------------------------------------------------
# /api/models: models_vision from caches only
# ---------------------------------------------------------------------------

def test_known_vision_models_uses_caches_and_never_the_network(monkeypatch):
    import httpx
    from src import llm_core

    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    url = "http://127.0.0.1:11434/v1/chat/completions"
    monkeypatch.setitem(llm_core._ollama_caps_cache, ("http://127.0.0.1:11434", "qwen3.5:9b"),
                        (0.0, frozenset({"completion", "vision"})))
    monkeypatch.setitem(llm_core._ollama_caps_cache, ("http://127.0.0.1:11434", "gemma3:1b"),
                        (0.0, frozenset({"completion"})))
    out = vr.known_vision_models(url, ["qwen3.5:9b", "gemma3:1b", "llava:7b", "qwen3:8b"])
    # server said yes / server said no / name heuristic / name heuristic
    assert out == ["qwen3.5:9b", "llava:7b"]


def test_known_vision_models_llamacpp_projector_decides(monkeypatch):
    url = "http://127.0.0.1:8081/v1/chat/completions"
    monkeypatch.setitem(ch._llamacpp_props_cache, ("127.0.0.1", 8081), (False, 1e18))
    assert vr.known_vision_models(url, ["qwen2.5-vl-7b"]) == []


def test_api_models_items_carry_models_vision():
    source = open("routes/model_routes.py", encoding="utf-8").read()
    assert '"models_vision": _known_vision_models(chat_url, [*curated, *extra])' in source
    assert '"models_vision": [],' in source


# ---------------------------------------------------------------------------
# history filter
# ---------------------------------------------------------------------------

def _img(url="data:image/png;base64,AAAA"):
    return {"type": "image_url", "image_url": {"url": url}}


def test_strip_images_uses_cached_description_and_placeholder(monkeypatch, tmp_path):
    import src.constants as constants

    monkeypatch.setattr(constants, "UPLOAD_DIR", str(tmp_path))
    (tmp_path / ".vision").mkdir()
    (tmp_path / ".vision" / "att2.txt").write_text("A red bicycle.", encoding="utf-8")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, _img(), _img(), _img()],
         "metadata": {"attachments": [
             {"id": "att1", "name": "one.png", "mime": "image/png", "vision": "A cat on a sofa."},
             {"id": "doc", "name": "a.pdf", "mime": "application/pdf"},
             {"id": "att2", "name": "two.png", "mime": "image/png"},
             {"id": "att3", "name": "three.png", "mime": "image/png"},
         ]}},
        {"role": "user", "content": [{"type": "text", "text": "[image from browser]"}, _img()],
         "metadata": {"source": "tool result: browser"}},
    ]
    original = [dict(m) for m in messages]
    out, n = vr.strip_images(messages)
    assert n == 4
    assert messages == original  # the caller's history is untouched
    texts = [b["text"] for b in out[1]["content"]]
    assert "A cat on a sofa." in texts[1] and "one.png" in texts[1]
    assert "A red bicycle." in texts[2]
    assert texts[3] == "[image: three.png — not shown, model has no vision]"
    assert out[2]["content"][1]["text"] == "[image: from browser — not shown, model has no vision]"
    assert not vr.messages_have_images(out)


async def test_filter_only_for_text_only_routes(monkeypatch):
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}, _img()]}]
    monkeypatch.setattr(vr, "route_sees_images", lambda url, model: model == "llava")
    assert await vr.strip_images_for_text_only_route("http://x", "llava", messages) is messages
    out = await vr.strip_images_for_text_only_route("http://x", "qwen3:8b", messages)
    assert out is not messages and not vr.messages_have_images(out)


async def test_filter_skips_the_probe_without_images(monkeypatch):
    monkeypatch.setattr(vr, "route_sees_images",
                        lambda url, model: (_ for _ in ()).throw(AssertionError("probed")))
    messages = [{"role": "user", "content": "hi"}]
    assert await vr.strip_images_for_text_only_route("http://x", "m", messages) is messages


async def test_filter_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(vr, "_global_setting", lambda key, default="": False if key == "vision_history_filter" else default)
    monkeypatch.setattr(vr, "route_sees_images", lambda url, model: False)
    messages = [{"role": "user", "content": [_img()]}]
    assert await vr.strip_images_for_text_only_route("http://x", "m", messages) is messages


async def test_every_stream_fallback_candidate_is_filtered(monkeypatch):
    """The fallback chain re-checks each route: a text-only fallback after a
    vision primary gets no image blocks."""
    from src import llm_core

    sent = []

    async def fake_stream(url, model, messages, **kw):
        sent.append((model, messages))
        if model == "vision-primary":
            yield 'event: error\ndata: {"error": "down", "status": 503}\n\n'
            return
        yield 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    monkeypatch.setattr(vr, "route_sees_images", lambda url, model: model == "vision-primary")
    messages = [{"role": "user", "content": [{"type": "text", "text": "what is this"}, _img()]}]
    chunks = [c async for c in llm_core.stream_llm_with_fallback(
        [("http://a/v1/chat/completions", "vision-primary", {}),
         ("http://b/v1/chat/completions", "text-fallback", {})],
        messages,
    )]
    assert chunks
    assert [m for m, _ in sent] == ["vision-primary", "text-fallback"]
    assert vr.messages_have_images(sent[0][1])
    assert not vr.messages_have_images(sent[1][1])
    assert vr.messages_have_images(messages)


async def test_nonstream_route_fallback_is_filtered(monkeypatch):
    from src import llm_core

    seen = {}

    async def fake_call(url, model, messages, **kw):
        seen["messages"] = messages
        return "fine"

    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)
    monkeypatch.setattr(vr, "route_sees_images", lambda url, model: False)
    messages = [{"role": "user", "content": [_img()]}]
    reply, _cand, _model = await llm_core.llm_call_async_with_route_fallback(
        [("http://a/v1/chat/completions", "text-only", {})], messages, fallback_statuses=())
    assert reply == "fine" and not vr.messages_have_images(seen["messages"])


# ---------------------------------------------------------------------------
# chat attachments: off the event loop
# ---------------------------------------------------------------------------

class _Uploads:
    def __init__(self, path):
        self.path = path

    def resolve_upload(self, att_id, owner=None):
        return {"id": att_id, "name": "pic.png", "mime": "image/png", "path": self.path}

    def is_image_file(self, name, mime=""):
        return True


async def test_attachment_description_runs_off_the_event_loop(monkeypatch, tmp_path):
    import src.chat_handler as chh
    from src.chat_handler import ChatHandler
    import src.settings as settings_mod

    image = tmp_path / "pic.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(chh, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(chh, "model_supports_vision", lambda *a, **k: False)
    monkeypatch.setattr(chh, "_sync_upload_vision_to_gallery", lambda *a, **k: None)
    monkeypatch.setattr(chh, "build_user_content", lambda msg, *a, **k: msg)
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: {"vision_enabled": True}.get(key, default))
    loop_thread = threading.current_thread()
    ran_in = []

    def fake_vl(path, owner=None):
        ran_in.append(threading.current_thread())
        return {"text": "A small drawing.", "model": "vl"}

    monkeypatch.setattr(chh, "analyze_image_with_vl_result", fake_vl)
    handler = ChatHandler(session_manager=None, memory_manager=None, chat_processor=None,
                          research_handler=None, preset_manager=None,
                          upload_handler=_Uploads(str(image)))
    sess = SimpleNamespace(model="text-only", endpoint_url="", owner="kim", id="s")
    enhanced, user_content, *_rest, meta = await handler.preprocess_message(
        "look", ["att-x"], sess, auto_opened_docs=[])
    assert ran_in and ran_in[0] is not loop_thread
    assert "A small drawing." in enhanced
    assert meta[0]["vision"] == "A small drawing."
    # Cached for the next turn (and for the history filter).
    assert (tmp_path / ".vision" / "att-x.txt").read_text(encoding="utf-8") == "A small drawing."


# ---------------------------------------------------------------------------
# tool images: auto-detected VLM describes them
# ---------------------------------------------------------------------------

async def test_tool_image_is_described_when_a_vlm_is_auto_detected(monkeypatch):
    from src import agent_loop as al

    monkeypatch.setattr(al, "model_supports_vision", lambda *a, **k: False)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: {"vision_model": ""}.get(key, default))
    monkeypatch.setattr(vr, "resolve_vision_route", lambda owner=None, configured=None, **k: {
        "model": "llava:7b", "url": "http://127.0.0.1:11434/v1/chat/completions", "source": "auto"})
    monkeypatch.setattr(al, "_describe_tool_image", lambda result, owner: "A login form.")
    extras = await al._image_record_extras({"images": []}, "qwen3:8b", "http://127.0.0.1:11434/v1", "lee")
    assert extras == {"vision_capable": False, "image_description": "A login form."}


async def test_tool_image_left_as_note_when_no_vlm(monkeypatch):
    from src import agent_loop as al

    monkeypatch.setattr(al, "model_supports_vision", lambda *a, **k: False)
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(vr, "resolve_vision_route", lambda owner=None, configured=None, **k: {"model": ""})
    monkeypatch.setattr(al, "_describe_tool_image",
                        lambda result, owner: (_ for _ in ()).throw(AssertionError("no VLM to call")))
    extras = await al._image_record_extras({"images": []}, "qwen3:8b", "", "lee")
    assert extras == {"vision_capable": False}


def test_settings_and_schema_declare_the_new_keys():
    from src.settings import DEFAULT_SETTINGS, _PER_USER_KEYS

    assert DEFAULT_SETTINGS["vision_endpoint_id"] == ""
    assert DEFAULT_SETTINGS["vision_history_filter"] is True
    assert "vision_endpoint_id" in _PER_USER_KEYS


# ---------------------------------------------------------------------------
# helper choice on a real local box (live inventory, ranking, aliases)
# ---------------------------------------------------------------------------

def _inv(monkeypatch, models, loaded=()):
    monkeypatch.setattr(vr, "live_local_inventory",
                        lambda url: {"models": dict(models), "loaded": set(loaded)})


def test_capable_models_rank_loaded_then_dedicated_then_smallest(monkeypatch):
    url = "http://127.0.0.1:11434/v1/chat/completions"
    _inv(monkeypatch, {"big-general:27b": 17_000_000_000, "qwen3-vl:8b-instruct": 6_100_000_000,
                       "tiny-vl:2b": 1_500_000_000})
    assert vr.rank_capable(url, ["big-general:27b", "qwen3-vl:8b-instruct", "tiny-vl:2b"])[0] == "tiny-vl:2b"
    _inv(monkeypatch, {"big-general:27b": 17_000_000_000, "qwen3-vl:8b-instruct": 6_100_000_000},
         loaded={"big-general:27b"})
    # Already in memory beats loading anything else.
    assert vr.rank_capable(url, ["qwen3-vl:8b-instruct", "big-general:27b"])[0] == "big-general:27b"


def test_auto_prefers_the_small_dedicated_vlm_over_a_big_general_alias(monkeypatch):
    # An Ollama tag named like a hosted model (an alias of a 27B general
    # model that also reports vision) must not win over the 8B VLM.
    endpoints = [_ep("ollama", "http://127.0.0.1:11434/v1/chat/completions",
                     ["claude-sonnet-4-5:latest", "qwen3-vl:8b-instruct"])]
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: endpoints)
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch, ollama={"claude-sonnet-4-5:latest": True, "qwen3-vl:8b-instruct": True})
    _inv(monkeypatch, {"claude-sonnet-4-5:latest": 17_000_000_000, "qwen3-vl:8b-instruct": 6_100_000_000})
    assert vr.resolve_vision_route("carol-vr")["model"] == "qwen3-vl:8b-instruct"


def test_hosted_names_never_match_local_aliases(monkeypatch):
    # No capability report at all: a local tag that merely looks like a
    # hosted model name is not a vision model by name.
    endpoints = [_ep("ollama", "http://127.0.0.1:11434/v1/chat/completions",
                     ["claude-sonnet-4-5-20250929"])]
    monkeypatch.setattr(vr, "list_vision_endpoints", lambda owner: endpoints)
    monkeypatch.setattr(vr, "_gate_allows", lambda url, owner: True)
    _no_probes(monkeypatch)
    route = vr.resolve_vision_route("dave-vr")
    assert route["model"] != "claude-sonnet-4-5-20250929"


def test_live_inventory_is_only_for_local_ollama(monkeypatch):
    monkeypatch.setattr(ch, "_is_local_ollama_url", lambda url: False)
    assert vr.live_local_inventory("https://api.example.com/v1/chat/completions") == {
        "models": {}, "loaded": set()}
