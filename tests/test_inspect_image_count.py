"""inspect_image `count`: tiles whose cores partition the region, the red box
on each tile, the count parser, and the action's two numbers (tile total and
one whole look) with the model the turn can see."""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from src import document_processor as dp
from src import image_inspection as ii
from src.agent_tools import image_inspect_tool as tool_mod
from src.agent_tools.image_inspect_tool import InspectImageTool


def test_tile_cores_partition_the_picture_and_tiles_overlap():
    tiles = ii.count_tiles(3, overlap=0.2)
    assert len(tiles) == 9
    area = sum((t["core"][2] - t["core"][0]) * (t["core"][3] - t["core"][1]) for t in tiles)
    assert area == pytest.approx(1.0)
    middle = tiles[4]
    assert middle["cell"] == "B2"
    assert middle["tile"][0] < middle["core"][0] and middle["tile"][2] > middle["core"][2]
    corner = tiles[0]
    assert corner["tile"][0] == 0.0 and corner["tile"][1] == 0.0
    assert [t["cell"] for t in tiles[:3]] == ["A1", "B1", "C1"]


def test_tile_count_is_clamped():
    assert len(ii.count_tiles(0)) == 1
    assert len(ii.count_tiles(9)) == 16


def test_tile_crop_draws_the_core_box_in_red():
    img = Image.new("RGB", (400, 400), "white")
    spec = ii.count_tiles(2, overlap=0.25)[0]
    crop = ii.tile_with_core_box(img, spec["tile"], spec["core"])
    # tile = [0, 0, 0.625, 0.625] of 400 px -> 250 px; the core ends at 200 px
    assert crop.size == (250, 250)
    assert crop.getpixel((199, 100))[0] == 255 and crop.getpixel((199, 100))[1] == 0
    assert crop.getpixel((100, 100)) == (255, 255, 255)
    assert crop.getpixel((230, 100)) == (255, 255, 255)


@pytest.mark.parametrize("text,expected", [
    ("7\npeople at the table", 7),
    ("There are 13 figures.", 13),
    ("None\nno people inside the box", 0),
    ("tres\npersonas", 3),
    ("unclear", None),
    ("I think\nabout 4", 4),
    ("", None),
])
def test_parse_count(text, expected):
    assert ii.parse_count(text) == expected


def _png(path, w=600, h=400):
    Image.new("RGB", (w, h), "white").save(path)
    return str(path)


async def test_count_asks_each_tile_and_the_whole_and_sums(tmp_path, monkeypatch):
    path = _png(tmp_path / "p.png")
    calls = []

    def fake(images, prompt, owner=None, model_override=None):
        calls.append({"prompt": prompt, "model": model_override,
                      "size": Image.open(io.BytesIO(images[0][0])).size})
        if "red box" in prompt:
            return {"text": f"{len(calls) % 3}\npeople", "model": "seeing-27b"}
        return {"text": "5\npeople", "model": "seeing-27b"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake)
    monkeypatch.setattr(tool_mod, "_main_model_can_see", lambda ctx: True)
    monkeypatch.setattr(tool_mod, "_vision_runs_locally", lambda m: True)
    out = await InspectImageTool().execute(json.dumps({"action": "count", "path": path, "what": "people"}),
                                           {"turn_model": "seeing-27b", "owner": "u"})
    assert out["exit_code"] == 0
    assert len(calls) == 5
    assert all(c["model"] == "seeing-27b" for c in calls), "the turn's own model counts when it can see"
    assert sum(1 for c in calls if "red box" in c["prompt"]) == 4
    assert all("people" in c["prompt"] for c in calls)
    assert out["whole_count"] == 5
    assert out["tile_total"] == sum(t["count"] for t in out["tiles"])
    assert [t["cell"] for t in out["tiles"]] == ["A1", "B1", "A2", "B2"]
    assert "total" in out["output"] and "whole region: 5" in out["output"]
    if out["tile_total"] != 5:
        assert "differ" in out["output"]


async def test_count_uses_the_vision_model_for_a_blind_turn_and_reports_unclear_tiles(tmp_path, monkeypatch):
    path = _png(tmp_path / "p.png")
    seen_models = []

    def fake(images, prompt, owner=None, model_override=None):
        seen_models.append(model_override)
        return {"text": "unclear" if "red box" in prompt else "2", "model": "cpu-vl"}

    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt", fake)
    monkeypatch.setattr(tool_mod, "_main_model_can_see", lambda ctx: False)
    monkeypatch.setattr(tool_mod, "_vision_runs_locally", lambda m: True)
    out = await InspectImageTool().execute(json.dumps({"action": "count", "path": path, "what": "circles",
                                                       "tiles": 3}), {"turn_model": "blind-27b"})
    assert set(seen_models) == {None}
    assert len(seen_models) == 10
    assert out["tile_total"] is None
    assert out["count"] == 2
    assert "gave no number" in out["output"]


async def test_count_needs_what(tmp_path):
    path = _png(tmp_path / "p.png")
    out = await InspectImageTool().execute(json.dumps({"action": "count", "path": path}), {})
    assert out["exit_code"] == 1 and "what" in out["error"]


async def test_count_with_one_tile_is_only_the_whole_look(tmp_path, monkeypatch):
    path = _png(tmp_path / "p.png")
    monkeypatch.setattr(dp, "analyze_image_with_vl_prompt",
                        lambda images, prompt, owner=None, model_override=None: {"text": "4", "model": "m"})
    monkeypatch.setattr(tool_mod, "_main_model_can_see", lambda ctx: False)
    out = await InspectImageTool().execute(json.dumps({"action": "count", "path": path, "what": "x",
                                                       "tiles": 1}), {})
    assert out["count"] == 4 and out["tiles"] == []
