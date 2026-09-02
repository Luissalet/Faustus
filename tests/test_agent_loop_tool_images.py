"""End-to-end through stream_agent_loop: a screenshot tool result is (1)
persisted with the tool event so the preview survives a reload, and (2) fed
to the next model round as an image block when the model can see.

Same mocking pattern as tests/test_agent_harness_loop.py: real loop body,
fake LLM stream, fake tool execution.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest

import src.agent_loop as al


def _png_b64(w=8, h=8):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


@pytest.fixture
def loop(monkeypatch):
    settings = {"agent_tool_images": True, "agent_tool_image_max_px": 1280, "vision_model": ""}

    def _get(key, default=None):
        return settings.get(key, default)

    monkeypatch.setattr(al, "get_setting", _get, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    from src import tool_images
    monkeypatch.setattr(tool_images, "get_setting", _get, raising=False)
    # The tool preflight prunes desktop tools on a headless box (no DISPLAY);
    # pretend a desktop exists so the tool is offered and the fake runs.
    from src.agent_tools import desktop_tools as dt

    class _Desk(dt.DesktopBackend):
        def available(self):
            return True, ""
    monkeypatch.setattr(dt, "get_backend", lambda: _Desk())

    b64 = _png_b64()
    result = {
        "output": "Screenshot of monitor 0: 8x8 px",
        "exit_code": 0,
        "images": [{"data": b64, "mimeType": "image/png"}],
        "screenshot": f"data:image/png;base64,{b64}",
    }

    async def _fake_exec(block, *a, **k):
        return (block.tool_type, dict(result))
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    seen = {"messages": [], "n": 0}
    rounds = [('```desktop_screenshot\n{}\n```', "tool_calls"), ("I can see the desktop.", "stop")]

    async def _fake_stream(_candidates, messages, **kwargs):
        seen["messages"].append([dict(m) for m in messages])
        i = min(seen["n"], len(rounds) - 1)
        seen["n"] += 1
        text, finish = rounds[i]
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": finish})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    def _run(model, vision):
        monkeypatch.setattr(al, "model_supports_vision", lambda m, u="": vision, raising=False)
        gen = al.stream_agent_loop(
            "http://127.0.0.1:11434/v1", model,
            [{"role": "user", "content": "haz una captura de pantalla"}],
            max_rounds=4, relevant_tools={"desktop_screenshot"},
        )
        return _events(_collect(gen)), seen, b64
    return _run


def test_screenshot_persists_in_tool_event_and_reaches_vision_model(loop):
    events, seen, b64 = loop("qwen3.5:9b", vision=True)
    metrics = next(e for e in events if e.get("type") == "metrics")["data"]
    tool_events = metrics["tool_events"]
    assert tool_events and tool_events[0]["tool"] == "desktop_screenshot"
    assert tool_events[0]["screenshot"] == f"data:image/png;base64,{b64}"
    # The live stream event carried it too (already the case for MCP).
    live = next(e for e in events if e.get("type") == "tool_output")
    assert live["screenshot"].startswith("data:image/png;base64,")

    # Second model round saw an image block after the tool result.
    assert seen["n"] == 2
    second = seen["messages"][1]
    image_msgs = [m for m in second if isinstance(m.get("content"), list)
                  and any(b.get("type") == "image_url" for b in m["content"])]
    assert len(image_msgs) == 1
    assert image_msgs[0]["role"] == "user"
    assert image_msgs[0]["content"][0]["text"] == "[image from desktop_screenshot]"
    assert image_msgs[0]["metadata"]["source"] == "tool result: desktop_screenshot"


def test_text_only_model_gets_a_note_not_an_image(loop):
    events, seen, b64 = loop("qwen2.5:3b", vision=False)
    second = seen["messages"][1]
    assert not any(isinstance(m.get("content"), list) and any(b.get("type") == "image_url" for b in m["content"])
                   for m in second)
    notes = [m for m in second if isinstance(m.get("content"), str) and "desktop_screenshot" in m["content"]
             and "could not be viewed" in m["content"]]
    assert notes, [m.get("content") for m in second]
    # No base64 leaked into the text the model reads.
    assert not any(b64 in (m.get("content") or "") for m in second if isinstance(m.get("content"), str))
