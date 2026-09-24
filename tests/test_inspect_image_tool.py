"""tests/test_inspect_image_tool.py — the `inspect_image` agent tool wrapper
(src/agent_tools/image_inspect_tool.py): action dispatch, model choice
(main-model-can-see vs. the configured Vision model), and the local
measurements every result carries.
"""
from __future__ import annotations

import base64
import json

import pytest
from PIL import Image, ImageDraw

from src import document_processor as dp
from src.agent_tools.image_inspect_tool import InspectImageTool


def _save_png(path, w=400, h=300, color="white"):
    Image.new("RGB", (w, h), color).save(path)
    return str(path)


def _frame_and_circle_png(path):
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([50, 50, 350, 250], outline="black", width=4)
    d.ellipse([100, 130, 140, 170], fill="black")
    img.save(path)
    return str(path)


async def _run(args: dict, ctx: dict | None = None):
    tool = InspectImageTool()
    return await tool.execute(json.dumps(args), ctx or {})


# ---------------------------------------------------------------------------
# argument handling / errors
# ---------------------------------------------------------------------------

async def test_unknown_action_errors():
    result = await _run({"action": "frobnicate", "path": "x.png"})
    assert result["exit_code"] == 1
    assert "unknown action" in result["error"]


async def test_missing_path_and_url_errors():
    result = await _run({"action": "view"})
    assert result["exit_code"] == 1
    assert "path" in result["error"] or "url" in result["error"]


async def test_both_path_and_url_errors(tmp_path):
    p = _save_png(tmp_path / "a.png")
    result = await _run({"action": "view", "path": p, "url": "http://example.com/a.png"})
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

async def test_view_returns_the_crop_and_measurements(tmp_path):
    p = _save_png(tmp_path / "a.png", 400, 300)
    result = await _run({"action": "view", "path": p, "region": [0.0, 0.0, 0.5, 0.5]})
    assert result["exit_code"] == 0
    assert result["images"]
    m = result["measurements"]
    assert m["region_used"] == pytest.approx((0.0, 0.0, 0.5, 0.5))
    assert m["output_size"] == [200, 150]
    assert m["original_size"] == [400, 300]


# ---------------------------------------------------------------------------
# shapes (local, model-free)
# ---------------------------------------------------------------------------

async def test_shapes_finds_circle_and_reports_frame_relative_position(tmp_path):
    p = _frame_and_circle_png(tmp_path / "shapes.png")
    frame = [50 / 400, 50 / 300, 350 / 400, 250 / 300]
    result = await _run({"action": "shapes", "path": p, "frame": frame, "annotate": False})
    assert result["exit_code"] == 0
    assert result["answered_by"] == "local"
    assert result["circles"], "expected a detected circle"
    circle = result["circles"][0]
    assert circle["box_in_frame"] is not None


async def test_shapes_annotate_attaches_a_preview_image(tmp_path):
    p = _frame_and_circle_png(tmp_path / "shapes2.png")
    result = await _run({"action": "shapes", "path": p, "annotate": True})
    assert result.get("images"), "expected an annotated preview image"


# ---------------------------------------------------------------------------
# ask: custom question reaches the vision model, region applied, model named
# ---------------------------------------------------------------------------

async def test_ask_sends_custom_question_and_region_to_the_configured_model(tmp_path, monkeypatch):
    p = _save_png(tmp_path / "a.png", 400, 300)
    captured = {}

    def fake_analyze(images, prompt, owner=None, model_override=None):
        captured["images"] = images
        captured["prompt"] = prompt
        captured["owner"] = owner
        captured["model_override"] = model_override
        return {"text": "there is a red circle in the top-left", "model": "local-vl-model"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)

    result = await _run(
        {"action": "ask", "path": p, "question": "what does the circle point at?",
         "region": [0.0, 0.0, 0.5, 0.5]},
        ctx={"owner": "alice", "turn_model": "qwen2.5:7b-instruct"},  # text-only main model
    )

    assert result["exit_code"] == 0
    assert captured["owner"] == "alice"
    assert "what does the circle point at?" in captured["prompt"]
    # Literal / "unclear" / position-as-fraction instructions are present.
    assert "unclear" in captured["prompt"]
    assert captured["images"] and len(captured["images"][0]) == 2
    raw, mime = captured["images"][0]
    assert isinstance(raw, (bytes, bytearray)) and len(raw) > 0
    assert mime.startswith("image/")
    assert result["answered_by"] == "local-vl-model"
    assert "local-vl-model" in result["output"]
    assert result["measurements"]["region_used"] == pytest.approx((0.0, 0.0, 0.5, 0.5))


async def test_ask_default_question_when_none_given(tmp_path, monkeypatch):
    p = _save_png(tmp_path / "a.png")
    captured = {}

    def fake_analyze(images, prompt, owner=None, model_override=None):
        captured["prompt"] = prompt
        return {"text": "a plain white image", "model": "local-vl-model"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)
    result = await _run({"action": "ask", "path": p}, ctx={"turn_model": "qwen2.5:7b"})
    assert result["exit_code"] == 0
    assert "Question:" in captured["prompt"]


async def test_ask_reports_no_vision_model_configured(tmp_path, monkeypatch):
    p = _save_png(tmp_path / "a.png")

    def fake_analyze(images, prompt, owner=None, model_override=None):
        return {"text": "[No vision model configured — set one in Settings → Vision]", "model": ""}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)
    result = await _run({"action": "ask", "path": p, "question": "what is this?"},
                         ctx={"turn_model": "qwen2.5:7b"})
    assert result["exit_code"] == 0
    assert result["answered_by"] == "none"
    assert "Settings" in result["output"]
    assert "Vision" in result["output"]
    # Local measurements are still returned even with no vision model.
    assert "measurements" in result


async def test_ask_with_a_real_vision_disabled_end_to_end(tmp_path, monkeypatch):
    """No mock of analyze_image_with_vl_prompt itself -- exercises the real
    document_processor path down to `_load_vl_settings`, matching the
    existing vision-owner-scope tests' style."""
    p = _save_png(tmp_path / "a.png")
    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_enabled": False})
    result = await _run({"action": "ask", "path": p, "question": "what is this?"},
                         ctx={"turn_model": "qwen2.5:7b"})
    assert result["exit_code"] == 0
    assert "Vision is disabled" in result["output"]


async def test_ask_main_model_can_see_attaches_image_without_calling_vision_model(tmp_path, monkeypatch):
    p = _save_png(tmp_path / "a.png")

    def boom(*a, **kw):
        raise AssertionError("the configured Vision model must not be called when the main model can see")

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", boom)

    result = await _run(
        {"action": "ask", "path": p, "question": "what color is this?"},
        ctx={"turn_model": "gpt-4o"},  # vision-capable main model
    )
    assert result["exit_code"] == 0
    assert result["answered_by"] == "main_model"
    assert result["images"]
    assert "what color is this?" in result["output"]


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------

async def test_compare_sends_both_images_to_the_vision_model(tmp_path, monkeypatch):
    a = _save_png(tmp_path / "a.png", color="red")
    b = _save_png(tmp_path / "b.png", color="blue")
    captured = {}

    def fake_analyze(images, prompt, owner=None, model_override=None):
        captured["n_images"] = len(images)
        captured["prompt"] = prompt
        return {"text": "different colors", "model": "local-vl-model"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)
    result = await _run(
        {"action": "compare", "path": a, "path_b": b, "question": "same color?"},
        ctx={"turn_model": "qwen2.5:7b"},
    )
    assert result["exit_code"] == 0
    assert captured["n_images"] == 2
    assert "same color?" in captured["prompt"]
    assert result["measurements"]["a"]["source"] == a
    assert result["measurements"]["b"]["source"] == b


async def test_compare_crosshair_marks_both_images(tmp_path, monkeypatch):
    a = _save_png(tmp_path / "a.png")
    b = _save_png(tmp_path / "b.png")

    def fake_analyze(images, prompt, owner=None, model_override=None):
        return {"text": "ok", "model": "local-vl-model"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)
    result = await _run(
        {"action": "compare", "path": a, "path_b": b, "point": [0.5, 0.5], "crosshair": True},
        ctx={"turn_model": "qwen2.5:7b"},
    )
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# grid_locate
# ---------------------------------------------------------------------------

async def test_grid_locate_requires_a_question(tmp_path):
    p = _save_png(tmp_path / "a.png")
    result = await _run({"action": "grid_locate", "path": p})
    assert result["exit_code"] == 1
    assert "question" in result["error"]


async def test_grid_locate_parses_cell_ids_from_the_answer(tmp_path, monkeypatch):
    p = _save_png(tmp_path / "a.png", 400, 300)

    def fake_analyze(images, prompt, owner=None, model_override=None):
        assert "grid" in prompt.lower()
        return {"text": "The mark is in cell C4.", "model": "local-vl-model"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)
    result = await _run(
        {"action": "grid_locate", "path": p, "question": "where is the mark?", "grid": 10},
        ctx={"turn_model": "qwen2.5:7b"},
    )
    assert result["exit_code"] == 0
    cells = result["cells"]
    assert any(c["cell"] == "C4" for c in cells)


async def test_grid_locate_main_model_can_see_attaches_grid_image(tmp_path, monkeypatch):
    p = _save_png(tmp_path / "a.png")

    def boom(*a, **kw):
        raise AssertionError("must not call the configured Vision model")

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", boom)
    result = await _run(
        {"action": "grid_locate", "path": p, "question": "where is the logo?"},
        ctx={"turn_model": "gpt-4o"},
    )
    assert result["exit_code"] == 0
    assert result["images"]
    assert result["grid_cells"]


# ---------------------------------------------------------------------------
# URL path (no real network)
# ---------------------------------------------------------------------------

async def test_ask_with_url_uses_the_outbound_broker(monkeypatch):
    import io as _io
    from src import outbound_fetch as of

    png_bytes = _io.BytesIO()
    Image.new("RGB", (30, 20), "green").save(png_bytes, format="PNG")
    png_bytes = png_bytes.getvalue()

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = png_bytes
        encoding = None

    def fake_fetch(url, *, profile, timeout=None, headers=None, allowed_mime=None, **kw):
        return _FakeResponse()

    monkeypatch.setattr(of, "fetch", fake_fetch)

    def fake_analyze(images, prompt, owner=None, model_override=None):
        return {"text": "a green rectangle", "model": "local-vl-model"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)

    result = await _run(
        {"action": "ask", "url": "https://example.com/pic.png", "question": "what color?"},
        ctx={"turn_model": "qwen2.5:7b"},
    )
    assert result["exit_code"] == 0
    assert result["measurements"]["source"] == "https://example.com/pic.png"


async def test_the_endpoint_probe_beats_a_multimodal_sounding_name(tmp_path, monkeypatch):
    """Live: a text-only 27B on llama.cpp, whose family name the heuristic
    counts as multimodal, got the raw image attached (which it cannot see)
    instead of the Vision model's answer to the question."""
    import src.chat_helpers as ch

    p = _save_png(tmp_path / "a.png", 400, 300)
    seen = {}

    def fake_probe(model, endpoint):
        seen["probe"] = (model, endpoint)
        return False  # the server says: no vision

    def fake_analyze(images, prompt, owner=None, model_override=None):
        return {"text": "a circle", "model": "local-vl-model"}

    monkeypatch.setattr(ch, "model_supports_vision", fake_probe)
    monkeypatch.setattr(ch, "is_vision_model", lambda m: True)  # the name lies
    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake_analyze)
    result = await _run(
        {"action": "ask", "path": p, "question": "what is circled?"},
        ctx={"turn_model": "family-27b-llamacpp", "turn_endpoint_url": "http://127.0.0.1:8081/v1"},
    )
    assert seen["probe"] == ("family-27b-llamacpp", "http://127.0.0.1:8081/v1")
    assert result["answered_by"] == "local-vl-model"


async def test_the_turn_endpoint_reaches_the_tool_ctx(monkeypatch):
    """The loop's turn options carry the endpoint; the tool context built for
    a registry tool must pass it on (it used to stop at the model name)."""
    import src.tool_execution as te
    import src.agent_tools as at

    seen = {}

    async def probe_tool(content, ctx):
        seen["ctx"] = ctx
        return {"output": "ok", "exit_code": 0}

    monkeypatch.setitem(at.TOOL_HANDLERS, "probe_ctx_tool", probe_tool)
    token = te._active_turn_options.set({"turn_model": "m", "turn_endpoint_url": "http://127.0.0.1:8081/v1"})
    try:
        await te._direct_fallback("probe_ctx_tool", "{}")
    finally:
        te._active_turn_options.reset(token)
    assert seen["ctx"]["turn_model"] == "m"
    assert seen["ctx"]["turn_endpoint_url"] == "http://127.0.0.1:8081/v1"


def test_a_slow_vision_model_gets_the_configured_timeout_and_the_reason(monkeypatch, tmp_path):
    """Live: a CPU-only vision model on a full page needed more than the old
    fixed 120 s, and the answer only said "VL model unavailable"."""
    import src.document_processor as dpm

    calls = {}

    def fake_llm_call(url, model, messages, headers=None, timeout=None):
        calls["timeout"] = timeout
        raise TimeoutError("read timed out")

    monkeypatch.setattr(dpm, "_resolve_vl_model", lambda m, owner=None: ("http://x/v1", "vl", {}))
    monkeypatch.setattr(dpm, "llm_call", fake_llm_call)
    monkeypatch.setattr(dpm, "_load_vl_settings", lambda: {"vision_enabled": True, "vision_model": "vl"})
    monkeypatch.setattr("src.settings.get_setting",
                        lambda k, d=None: 900 if k == "vision_timeout_seconds" else d)
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_vision_fallback_candidates", lambda owner=None: [])
    import src.privacy_policy as pp
    monkeypatch.setattr(pp, "assert_outbound", lambda *a, **k: None)
    out = dpm.analyze_image_with_vl_prompt([(b"\x89PNG....", "image/png")], "what?", None, None)
    assert calls["timeout"] == 900
    assert "TimeoutError" in out["text"] and "vision_timeout_seconds=900" in out["text"]
