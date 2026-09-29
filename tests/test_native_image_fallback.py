"""Native image stripping preserves canonical block history per route."""
from copy import deepcopy

from fastapi import HTTPException
import pytest

from src import llm_core, vision_routing as routing


def message(block_image=False):
    content = [{"type": "text", "text": "Compare original and edited screenshot"},
               {"type": "custom", "payload": "opaque"},
               {"type": "text", "text": "Keep the labels unchanged"}]
    if block_image:
        content.insert(1, {"type": "image_url", "image_url": {"url": "data:image/png;base64,QkxPQ0s="}})
    return {"role": "user", "content": content, "images": ["TkFUSVZF", "U0VDT05E"],
            "metadata": {"source": "tool result: screenshot", "trusted": False}}


@pytest.mark.parametrize("block_image", [False, True])
def test_native_images_and_block_content_keep_order_and_all_notes(block_image):
    original = message(block_image)
    saved = deepcopy(original)
    projected, count = routing.strip_images([original])
    out = projected[0]
    assert original == saved
    assert out["metadata"] == original["metadata"]
    assert "images" not in out and not routing.messages_have_images(projected)
    assert count == 2 + int(block_image)
    blocks = out["content"]
    expected = [b for b in original["content"] if b.get("type") != "image_url"]
    retained = [b for b in blocks if b in expected]
    assert retained == expected
    notes = [b for b in blocks if b not in expected]
    assert len(notes) == count
    assert all(b["type"] == "text" and "not shown" in b["text"] for b in notes)
    if block_image:
        assert "not shown" in blocks[1]["text"]
    assert blocks[-2:] == notes[-2:]


def test_native_string_content_keeps_existing_shape():
    projected, count = routing.strip_images([{"role": "user", "content": "Instruction", "images": ["native"]}])
    assert count == 1
    assert isinstance(projected[0]["content"], str)
    assert projected[0]["content"].startswith("Instruction\n")


def test_empty_block_list_gets_native_notes_as_blocks():
    projected, count = routing.strip_images([{"role": "user", "content": [], "images": ["native"]}])
    assert count == 1 and projected[0]["content"][0]["type"] == "text"


@pytest.fixture
def route_fixture(monkeypatch):
    monkeypatch.setattr(routing, "history_filter_enabled", lambda: True)
    monkeypatch.setattr(routing, "route_sees_images", lambda url, model: model == "vision-primary")
    return [("http://synthetic-a/v1/chat/completions", "vision-primary", {}),
            ("http://synthetic-b/v1/chat/completions", "text-fallback", {})]


def assert_routed(seen, original, saved):
    assert [model for model, _ in seen] == ["vision-primary", "text-fallback"]
    assert routing.messages_have_images(seen[0][1])
    assert not routing.messages_have_images(seen[1][1])
    assert original == saved
    text = str(seen[1][1][0]["content"])
    assert "Compare original and edited screenshot" in text
    assert "Keep the labels unchanged" in text and "opaque" in text
    assert text.count("not shown") == 3


async def test_real_nonstream_fallback_projects_each_candidate(monkeypatch, route_fixture):
    original = [message(True)]
    saved = deepcopy(original)
    seen = []

    async def call(url, model, messages, **kwargs):
        seen.append((model, messages))
        if model == "vision-primary":
            raise HTTPException(503, "synthetic unavailable")
        return "ok"

    monkeypatch.setattr(llm_core, "llm_call_async", call)
    result, _, model = await llm_core.llm_call_async_with_route_fallback(
        route_fixture, original, fallback_statuses=(503,))
    assert result == "ok" and model == "text-fallback"
    assert_routed(seen, original, saved)


async def test_real_stream_fallback_projects_each_candidate(monkeypatch, route_fixture):
    original = [message(True)]
    saved = deepcopy(original)
    seen = []

    async def stream(url, model, messages, **kwargs):
        seen.append((model, messages))
        if model == "vision-primary":
            yield 'event: error\ndata: {"error": "synthetic unavailable", "status": 503}\n\n'
            return
        yield 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(llm_core, "stream_llm", stream)
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback(route_fixture, original)]
    assert chunks
    assert_routed(seen, original, saved)
