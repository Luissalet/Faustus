"""Tool results that carry images reach the model as an image, not as JSON.

An MCP screenshot (Playwright) or a builtin `desktop_screenshot` returns
``{"images": [{"data": <b64>, "mimeType": "image/png"}]}``. Before this
plumbing existed, ``format_tool_result`` dumped that base64 into the
``**data:**`` JSON blob (~8 KB of noise per screenshot) and the model never
SAW the picture. Now:

* the text result stays text (no base64 in it);
* ``_append_tool_results`` appends a synthetic ``role: "user"`` message with an
  ``image_url`` block — only when the route's model is vision-capable;
* a text-only model gets a one-line note (or a VL-model description when a
  ``vision_model`` is configured and the loop produced one);
* the image is downscaled to ``agent_tool_image_max_px`` before it is sent.
"""
from __future__ import annotations

import base64
import io
import json

import pytest

import src.agent_tools  # noqa: F401  - resolves circular imports first
import src.agent_loop as al
from src.tool_execution import format_tool_result
from src import tool_images


def _png_b64(width: int = 4, height: int = 4, color=(255, 0, 0)) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _record(tool: str, result: dict) -> dict:
    return {"tool_name": tool, "content": "{}", "result": result, "text": "x"}


def _screenshot_result(b64: str | None = None) -> dict:
    return {
        "stdout": "[Screenshot captured (image/png)]",
        "stderr": "",
        "exit_code": 0,
        "images": [{"data": b64 or _png_b64(), "mimeType": "image/png"}],
    }


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """Default settings for the loop helpers: images on, no VL model."""
    values = {"agent_tool_images": True, "agent_tool_image_max_px": 1280, "vision_model": ""}

    def _get(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(al, "get_setting", _get, raising=False)
    monkeypatch.setattr(tool_images, "get_setting", _get, raising=False)
    return values


# ── format_tool_result ─────────────────────────────────────────────────────

def test_format_tool_result_does_not_dump_image_base64():
    b64 = _png_b64()
    text = format_tool_result("mcp: browser_take_screenshot", _screenshot_result(b64))
    assert b64 not in text
    assert "**data:**" not in text
    # The model is told an image is attached separately.
    assert "image" in text.lower()


def test_format_tool_result_does_not_dump_screenshot_data_url():
    result = {"output": "Screenshot 100x100", "exit_code": 0,
              "images": [{"data": _png_b64(), "mimeType": "image/png"}],
              "screenshot": "data:image/png;base64," + _png_b64()}
    text = format_tool_result("desktop_screenshot", result)
    assert "data:image/png;base64" not in text
    assert "Screenshot 100x100" in text


# ── _append_tool_results: native branch ────────────────────────────────────

def test_native_branch_appends_image_message_for_vision_model():
    messages = []
    b64 = _png_b64()
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "desktop_screenshot", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[_record("desktop_screenshot", _screenshot_result(b64))],
        model="qwen2.5-vl:7b", endpoint_url="",
    )
    assert messages[-2]["role"] == "tool"
    img = messages[-1]
    assert img["role"] == "user"
    assert isinstance(img["content"], list)
    text_block, image_block = img["content"]
    assert text_block == {"type": "text", "text": "[image from desktop_screenshot]"}
    assert image_block["type"] == "image_url"
    url = image_block["image_url"]["url"]
    assert url.startswith("data:image/")
    assert ";base64," in url
    assert img["metadata"] == {
        "trusted": False,
        "source": "tool result: desktop_screenshot",
        "tool_gate_untrusted": True,
    }


def test_native_branch_note_when_model_is_text_only():
    messages = []
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "desktop_screenshot", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[_record("desktop_screenshot", _screenshot_result())],
        model="qwen2.5:3b", endpoint_url="",
    )
    note = messages[-1]
    assert note["role"] == "user"
    assert isinstance(note["content"], str)
    assert "desktop_screenshot" in note["content"]
    assert "could not be viewed" in note["content"] or "cannot view" in note["content"]
    assert "image_url" not in json.dumps(note)


def test_record_vision_capable_flag_overrides_model_heuristic():
    """The loop probes the endpoint asynchronously and stamps the record;
    that answer wins over the name heuristic (qwen3.5:9b reports vision via
    Ollama /api/show but its name does not say so)."""
    messages = []
    record = _record("desktop_screenshot", _screenshot_result())
    record["vision_capable"] = True
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "desktop_screenshot", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[record], model="", endpoint_url="",
    )
    assert messages[-1]["content"][1]["type"] == "image_url"


def test_record_image_description_is_used_for_text_only_model():
    messages = []
    record = _record("desktop_screenshot", _screenshot_result())
    record["vision_capable"] = False
    record["image_description"] = "A terminal window with a green prompt."
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "desktop_screenshot", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[record], model="qwen2.5:3b", endpoint_url="",
    )
    note = messages[-1]
    assert note["role"] == "user"
    assert "A terminal window with a green prompt." in note["content"]
    assert note["metadata"]["trusted"] is False


def test_setting_off_appends_nothing():
    messages = []
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "desktop_screenshot", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[_record("desktop_screenshot", _screenshot_result())],
        model="qwen2.5-vl:7b", endpoint_url="", vision_capable=True,
        tool_images_enabled=False,
    )
    assert messages[-1]["role"] == "tool"


def test_no_images_no_extra_message():
    messages = []
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "bash", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[_record("bash", {"output": "hi", "exit_code": 0})],
        model="qwen2.5-vl:7b",
    )
    assert [m["role"] for m in messages] == ["assistant", "tool"]


def test_mcp_result_images_are_forwarded_too():
    messages = []
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "mcp__builtin_browser__browser_take_screenshot", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[_record("mcp__builtin_browser__browser_take_screenshot", _screenshot_result())],
        model="llava",
    )
    assert messages[-1]["content"][0]["text"] == "[image from mcp__builtin_browser__browser_take_screenshot]"


# ── _append_tool_results: fenced (non-native) branch ───────────────────────

def test_fenced_branch_appends_image_message_after_untrusted_wrapper():
    messages = []
    al._append_tool_results(
        messages, "```desktop_screenshot\n{}\n```", [], ["text"], ["text"], False, 1,
        tool_result_records=[_record("desktop_screenshot", _screenshot_result())],
        model="gemma3:4b",
    )
    roles = [m["role"] for m in messages]
    assert roles == ["assistant", "user", "user"]
    assert messages[-1]["content"][1]["type"] == "image_url"
    assert messages[-1]["metadata"]["source"] == "tool result: desktop_screenshot"


# ── downscale ──────────────────────────────────────────────────────────────

def test_downscale_shrinks_large_images_to_jpeg():
    from PIL import Image

    big = _png_b64(3000, 1500)
    b64, mime, info = tool_images.downscale_b64(big, "image/png", max_px=1280)
    assert mime == "image/jpeg"
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert max(img.size) == 1280
    assert info["scale"] == pytest.approx(1280 / 3000, rel=1e-3)
    assert info["width"] == 1280 and info["height"] == 640


def test_downscale_leaves_small_images_alone():
    small = _png_b64(10, 10)
    b64, mime, info = tool_images.downscale_b64(small, "image/png", max_px=1280)
    assert b64 == small and mime == "image/png"
    assert info["scale"] == 1.0


def test_downscale_tolerates_garbage():
    b64, mime, info = tool_images.downscale_b64("not-base64!!", "image/png", max_px=100)
    assert b64 == "not-base64!!" and mime == "image/png"
    assert info["scale"] == 1.0


def test_append_downscales_before_sending(monkeypatch, _settings):
    from PIL import Image

    _settings["agent_tool_image_max_px"] = 64
    messages = []
    al._append_tool_results(
        messages, "", [{"id": "c1", "name": "desktop_screenshot", "arguments": "{}"}],
        ["text"], ["text"], True, 1,
        tool_result_records=[_record("desktop_screenshot", _screenshot_result(_png_b64(400, 200)))],
        model="llava",
    )
    url = messages[-1]["content"][1]["image_url"]["url"]
    header, _, payload = url.partition(",")
    assert header == "data:image/jpeg;base64"
    img = Image.open(io.BytesIO(base64.b64decode(payload)))
    assert max(img.size) == 64


def test_normalize_images_accepts_data_urls_and_skips_junk():
    imgs = tool_images.normalize_result_images({
        "images": [
            {"data": "AAAA", "mimeType": "image/png"},
            {"data": "data:image/jpeg;base64,BBBB"},
            "not a dict",
            {"data": ""},
            {"mimeType": "image/png"},
        ]
    })
    assert imgs == [
        {"data": "AAAA", "mimeType": "image/png"},
        {"data": "BBBB", "mimeType": "image/jpeg"},
    ]
    assert tool_images.normalize_result_images({"output": "x"}) == []
    assert tool_images.normalize_result_images(None) == []


def test_screenshot_data_url_helper():
    assert tool_images.screenshot_data_url({"images": [{"data": "AAAA", "mimeType": "image/png"}]}) == "data:image/png;base64,AAAA"
    assert tool_images.screenshot_data_url({"screenshot": "data:image/jpeg;base64,BBBB"}) == "data:image/jpeg;base64,BBBB"
    assert tool_images.screenshot_data_url({}) == ""
