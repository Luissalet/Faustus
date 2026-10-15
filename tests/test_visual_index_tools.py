"""tests/test_visual_index_tools.py -- the agent tools of the visual index, registration included."""
from __future__ import annotations

import asyncio
import json

import pytest

from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import visual_index_tool as vit
from src.visual_index import service as svc

from tests.vi_helpers import BLUE, RED, fake_web, make_service, page, png_file


@pytest.fixture(autouse=True)
def _fresh_vision_cache():
    from src.visual_index.vision import clear_vision_cache
    clear_vision_cache()
    yield
    clear_vision_cache()


NAMES = ("visual_index_add", "visual_search", "view_tile", "visual_index_list", "visual_index_remove")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def service(tmp_path):
    shot = page([(1024, RED), (1024, BLUE)], 875)
    s, _ = make_service(tmp_path, renderers=fake_web({"https://example.org/p": [(0, shot)]}))
    previous = svc.set_service_for_tests(s)
    svc.reset_views_for_tests()
    yield s
    svc.set_service_for_tests(previous)


def call(tool, args, ctx=None):
    return run(tool.execute(json.dumps(args), ctx or {"owner": "ada", "turn_id": "turn-1"}))


def test_the_five_tools_are_registered_everywhere():
    from src import tool_index, tool_schemas
    from src.tool_authority_catalog import unregistered_builtins
    from src.tool_index_examples import EXAMPLES
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, PLAN_MODE_READONLY_TOOLS

    for name in NAMES:
        assert name in TOOL_HANDLERS
        assert name in tool_index.BUILTIN_TOOL_DESCRIPTIONS
        assert name in EXAMPLES
        first_line = tool_index.BUILTIN_TOOL_DESCRIPTIONS[name].split(". ")[0]
        assert len(first_line) <= 110, name
    assert unregistered_builtins() == []
    assert "visual_index_add" in NON_ADMIN_BLOCKED_TOOLS
    assert {"visual_search", "view_tile", "visual_index_list"} <= set(PLAN_MODE_READONLY_TOOLS)
    desc = " ".join(tool_index.BUILTIN_TOOL_DESCRIPTIONS[n] for n in NAMES).lower()
    for word in ("screenshot", "captura", "pdf", "tabla"):
        assert word in desc, word


def test_add_search_list_remove(service):
    added = call(vit.VisualIndexAddTool(), {"urls": ["https://example.org/p"], "collection": "web"})
    assert added["exit_code"] == 0 and "2 strips" in added["output"]
    found = call(vit.VisualSearchTool(), {"query": "red", "k": 2, "collection": "web"})
    assert found["exit_code"] == 0 and found["results"][0]["y_offset"] == 0
    assert "id=" in found["output"] and "view_tile" in found["output"]
    listed = call(vit.VisualIndexListTool(), {})
    assert "web: 1 sources, 2 strips" in listed["output"]
    detail = call(vit.VisualIndexListTool(), {"collection": "web"})
    assert "https://example.org/p" in detail["output"]
    removed = call(vit.VisualIndexRemoveTool(), {"collection": "web"})
    assert removed["exit_code"] == 0 and removed["deleted_collection"] is True
    assert "no collections" in call(vit.VisualIndexListTool(), {})["output"]


def test_bad_arguments_are_explained(service):
    assert call(vit.VisualSearchTool(), {"query": ""})["error_code"] == "invalid_arguments"
    assert call(vit.VisualSearchTool(), {"query": "x", "bogus": 1})["error_code"] == "invalid_arguments"
    assert call(vit.VisualIndexAddTool(), {"nope": 1})["error_code"] == "invalid_arguments"
    assert call(vit.VisualIndexAddTool(), {"urls": []})["error_code"] == "invalid_arguments"
    assert call(vit.ViewTileTool(), {})["error_code"] == "invalid_arguments"
    assert call(vit.ViewTileTool(), {"id": "web~000000000000"})["error_code"] == "unknown_strip"
    assert call(vit.VisualSearchTool(), {"query": "x", "collection": "ghost"})["error_code"] == "unknown_collection"
    assert "empty" in call(vit.VisualSearchTool(), {"query": "x"}, {"owner": "nobody"})["output"]


def _first_id(service):
    call(vit.VisualIndexAddTool(), {"urls": ["https://example.org/p"]})
    return call(vit.VisualSearchTool(), {"query": "blue", "k": 1})["results"][0]["id"]


def test_view_tile_attaches_the_image_when_the_main_model_sees(service, monkeypatch):
    from src.agent_tools import image_inspect_tool

    monkeypatch.setattr(image_inspect_tool, "_main_model_can_see", lambda ctx: True)
    sid = _first_id(service)
    out = call(vit.ViewTileTool(), {"id": sid, "question": "what is the value?"})
    assert out["answered_by"] == "main_model" and out["images"][0]["mimeType"] == "image/png"
    assert out["images"][0]["data"].startswith("iVBOR")                  # a PNG, base64
    assert out["views_left"] == svc.view_limit() - 1


def test_view_tile_uses_the_resident_vision_model_otherwise(service, monkeypatch):
    from src.agent_tools import image_inspect_tool
    from src.visual_index import vision

    monkeypatch.setattr(image_inspect_tool, "_main_model_can_see", lambda ctx: False)
    monkeypatch.setattr(vision, "vision_ready", lambda owner: (True, "", "qwen-vl"))
    seen = {}

    def ask(image, prompt, owner=None):
        seen["prompt"], seen["size"] = prompt, image.size
        return {"text": "Revenue 2024: 1.2M", "model": "qwen-vl"}

    monkeypatch.setattr(vision, "ask", ask)
    sid = _first_id(service)
    out = call(vit.ViewTileTool(), {"id": sid, "question": "revenue in 2024?"})
    assert out["answered_by"] == "qwen-vl" and "Revenue 2024: 1.2M" in out["output"]
    assert seen["prompt"] == "revenue in 2024?" and seen["size"][1] == 1024
    assert "images" not in out


def test_view_tile_without_any_vision_model_says_so(service, monkeypatch):
    from src.agent_tools import image_inspect_tool
    from src.visual_index import vision

    monkeypatch.setattr(image_inspect_tool, "_main_model_can_see", lambda ctx: False)
    monkeypatch.setattr(vision, "vision_ready", lambda owner: (False, "no vision model is running", ""))
    sid = _first_id(service)
    out = call(vit.ViewTileTool(), {"id": sid})
    assert out["exit_code"] == 0 and out["answered_by"] == "none" and out["vision_available"] is False
    assert "cannot see images" in out["output"] and "do not describe" in out["output"]
    assert "images" not in out


def test_view_tile_is_capped_per_turn(service, monkeypatch):
    from src import settings
    from src.agent_tools import image_inspect_tool

    monkeypatch.setattr(image_inspect_tool, "_main_model_can_see", lambda ctx: True)
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: 2 if key == "visual_index_view_limit_per_turn" else default)
    sid = _first_id(service)
    ctx = {"owner": "ada", "turn_id": "T"}
    assert call(vit.ViewTileTool(), {"id": sid}, ctx)["exit_code"] == 0
    assert call(vit.ViewTileTool(), {"id": sid}, ctx)["exit_code"] == 0
    third = call(vit.ViewTileTool(), {"id": sid}, ctx)
    assert third["error_code"] == "view_limit" and "at most 2" in third["error"]
    assert call(vit.ViewTileTool(), {"id": sid}, {"owner": "ada", "turn_id": "T2"})["exit_code"] == 0


def test_tools_without_the_dependencies_return_the_install_hint(service, monkeypatch):
    from src.visual_index import embedder as emb_mod
    from src.visual_index.embedder import EmbedderPool

    monkeypatch.setattr(emb_mod, "dependencies_missing", lambda: ["transformers"])
    service._pool = EmbedderPool(lambda: emb_mod.Qwen3VLEmbedder(), idle_s=0)
    out = call(vit.VisualIndexAddTool(), {"urls": ["https://example.org/p"]})
    assert out["exit_code"] == 1 and out["error_code"] == "dependencies_missing" and "pip install" in out["error"]
    out = call(vit.VisualSearchTool(), {"query": "x"}, {"owner": "ada"})
    assert out["exit_code"] == 0 or out["error_code"] == "dependencies_missing"


def test_deferred_family_and_schema_shape():
    from src.tool_authority_catalog import DEFERRED_FAMILIES
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    rx = DEFERRED_FAMILIES["visual_index"]
    assert all(rx.match(n) for n in NAMES)
    names = {s["function"]["name"] if "function" in s else s.get("name") for s in FUNCTION_TOOL_SCHEMAS}
    assert set(NAMES) <= names
