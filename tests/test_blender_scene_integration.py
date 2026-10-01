"""Runs the real Blender when one is installed (skipped otherwise): the smoke
scene renders a PNG and saves a .blend with every assertion passing, a second
run re-loads the .blend and asserts again, and the runner enforces the path
rules itself."""
import json
import os
import subprocess

import pytest

from src import blender_scene as bs
from src.blender_scene.run import RUNNER_PATH, build_command, parse_report

INFO = bs.probe()
pytestmark = [
    pytest.mark.skipif(not INFO.get("found"), reason="Blender is not installed here"),
    pytest.mark.timeout(240),
]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def smoke_root(tmp_path_factory):
    return str(tmp_path_factory.mktemp("smoke"))


@pytest.fixture(scope="module")
def smoke_result(smoke_root):
    return bs.run_scene(bs.smoke_scene(), smoke_root, timeout=200, probe_result=INFO)


def test_smoke_scene_renders_saves_and_every_assertion_passes(smoke_result, smoke_root):
    res = smoke_result
    assert res["ok"], res["errors"] or res.get("stderr_tail")
    assert res["stage"] == "done" and res["errors"] == []
    report = res["report"]
    assert report["blender"]["version"].startswith(INFO["version"].rsplit(".", 1)[0])
    assert report["errors"] == 0 and res["returncode"] == 0
    assert [o["status"] for o in report["ops"]] == ["ok"] * len(report["ops"])
    assert all(isinstance(o["duration_s"], float) for o in report["ops"])
    assert len(report["assertions"]) == 9 and all(a["passed"] for a in report["assertions"])
    for a in report["assertions"]:
        assert "observed" in a and "expected" in a

    png = os.path.join(smoke_root, "smoke.png")
    blend = os.path.join(smoke_root, "smoke.blend")
    with open(png, "rb") as fh:
        head = fh.read(8)
    assert head == PNG_MAGIC and os.path.getsize(png) > 2000
    assert os.path.getsize(blend) > 10_000
    assert {o["path"] for o in report["outputs"]} == {png, blend}
    assert all(o["bytes"] > 0 for o in report["outputs"])
    assert res["images"] == [png]


def test_the_png_has_the_requested_size(smoke_result, smoke_root):
    from PIL import Image

    with Image.open(os.path.join(smoke_root, "smoke.png")) as img:
        assert img.size == (320, 240)
        extrema = img.convert("L").getextrema()
    assert extrema[1] - extrema[0] > 20    # not a blank frame


def test_a_second_run_reloads_the_saved_blend_and_asserts_again(smoke_result, smoke_root):
    assert smoke_result["ok"]
    res = bs.run_scene(bs.reload_scene(), smoke_root, timeout=200, probe_result=INFO)
    assert res["ok"], res["errors"]
    assert res["assertions"] == {"total": 10, "failed": 0}
    assert res["report"]["ops"][0]["op"] == "load_blend" and res["report"]["ops"][0]["info"]["objects"] == 5


def test_a_failing_assertion_fails_the_run_with_observed_and_expected(tmp_path):
    scene = {"ops": [{"op": "add_mesh", "primitive": "cube", "name": "A"},
                     {"op": "assert", "kind": "object_count", "count": 2},
                     {"op": "assert", "kind": "object_exists", "name": "A"}]}
    res = bs.run_scene(scene, str(tmp_path), timeout=120, probe_result=INFO)
    assert res["ok"] is False and res["returncode"] == 1
    bad = [a for a in res["report"]["assertions"] if not a["passed"]]
    assert len(bad) == 1 and bad[0]["observed"] == 1 and bad[0]["expected"] == "eq 2"
    assert res["errors"] == ["ops[1] assert: assertion failed (object_count): 1 object(s), expected eq 2"]
    assert res["report"]["ops"][2]["status"] == "ok"      # the run continued


def test_a_failing_op_is_recorded_and_the_run_continues_unless_stop_on_error(tmp_path):
    ops = [{"op": "add_mesh", "primitive": "cube", "name": "A", "material": "Missing"},
           {"op": "add_mesh", "primitive": "plane", "name": "B"},
           {"op": "assert", "kind": "object_exists", "name": "B"}]
    cont = bs.run_scene({"ops": ops}, str(tmp_path / "a"), timeout=120, probe_result=INFO)
    assert [o["status"] for o in cont["report"]["ops"]] == ["error", "ok", "ok"]
    assert "material 'Missing' not found" in cont["report"]["ops"][0]["message"]
    assert cont["ok"] is False and cont["report"]["errors"] == 1

    stop = bs.run_scene({"meta": {"stop_on_error": True}, "ops": ops}, str(tmp_path / "b"), timeout=120, probe_result=INFO)
    assert [o["status"] for o in stop["report"]["ops"]] == ["error", "skipped", "skipped"]
    assert stop["report"]["errors"] == 1


def test_modifiers_text_materials_exports_and_imports_round_trip(tmp_path):
    R = "${PROJECT_ROOT}/"
    scene = {"meta": {"name": "round trip"}, "ops": [
        {"op": "add_material", "name": "Gold", "base_color": [1, 0.7, 0.2], "metallic": 1, "emission_color": [1, 0.5, 0],
         "emission_strength": 2},
        {"op": "add_mesh", "primitive": "torus", "name": "Ring", "location": [0, 0, 1], "material": "Gold", "smooth": True},
        {"op": "add_modifier", "target": "Ring", "type": "subdivision", "levels": 1},
        {"op": "add_mesh", "primitive": "cylinder", "name": "Post", "location": [3, 0, 0]},
        {"op": "add_modifier", "target": "Post", "type": "array", "count": 3, "relative_offset": [0, 2, 0]},
        {"op": "add_modifier", "target": "Post", "type": "mirror", "axes": ["x", "y"]},
        {"op": "add_modifier", "target": "Post", "type": "solidify", "thickness": 0.05},
        {"op": "add_text", "body": "Hi", "name": "Label", "extrude": 0.1, "material": "Gold"},
        {"op": "add_empty", "name": "Pivot", "display": "sphere"},
        {"op": "set_transform", "target": "Pivot", "location": [1, 2, 3], "rotation": [0, 0, 90], "scale": [2, 2, 2]},
        {"op": "export_model", "path": R + "m/ring.stl", "target": "Ring"},
        {"op": "export_model", "path": R + "m/post.obj", "target": "Post"},
        {"op": "export_model", "path": R + "m/all.glb"},
        {"op": "export_model", "path": R + "m/ring.fbx", "target": "Ring"},
        {"op": "new_scene"},
        {"op": "import_model", "path": R + "m/ring.stl", "location": [0, 0, 2], "scale": [0.5, 0.5, 0.5]},
        {"op": "import_model", "path": R + "m/post.obj"},
        {"op": "import_model", "path": R + "m/all.glb"},
        {"op": "import_model", "path": R + "m/ring.fbx"},
        {"op": "assert", "kind": "object_count", "object_type": "mesh", "count": 4, "cmp": "ge"},
        {"op": "assert", "kind": "bbox_within", "object": "ring", "min": [-0.7, -0.7, 2.3], "max": [0.7, 0.7, 2.7]},
        {"op": "delete", "pattern": "*", "object_type": "mesh", "must_match": True},
        {"op": "assert", "kind": "object_count", "object_type": "mesh", "count": 0},
    ]}
    res = bs.run_scene(scene, str(tmp_path), timeout=200, probe_result=INFO)
    assert res["ok"], res["errors"]
    sizes = {os.path.basename(o["path"]): o["bytes"] for o in res["outputs"]}
    assert set(sizes) == {"ring.stl", "post.obj", "all.glb", "ring.fbx"} and all(v > 100 for v in sizes.values())


def test_cycles_render_with_glare_and_jpeg_output(tmp_path):
    scene = {"ops": [
        {"op": "add_mesh", "primitive": "monkey", "location": [0, 0, 0]},
        {"op": "add_light", "type": "point", "location": [2, -2, 3], "energy": 500},
        {"op": "add_camera", "location": [4, -4, 3], "rotation": [65, 0, 45]},
        {"op": "set_world", "color": [0.2, 0.2, 0.3], "strength": 1},
        {"op": "set_render", "engine": "cycles", "resolution": [96, 72], "samples": 2, "denoise": False,
         "device": "cpu", "view_transform": "standard"},
        {"op": "compositor_glare", "glare_type": "fog_glow"},
        {"op": "render", "path": "out/c.jpg", "quality": 60},
        {"op": "assert", "kind": "engine", "engine": "cycles"},
        {"op": "assert", "kind": "resolution", "width": 96, "height": 72},
    ]}
    res = bs.run_scene(scene, str(tmp_path), timeout=200, probe_result=INFO)
    assert res["ok"], res["errors"]
    out = tmp_path / "out" / "c.jpg"
    assert out.read_bytes()[:3] == b"\xff\xd8\xff" and out.stat().st_size > 500
    assert res["images"] == [str(out)]


def test_the_runner_confines_paths_even_when_the_validator_is_bypassed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.png"
    scene = {"meta": {}, "ops": [
        {"op": "add_mesh", "primitive": "cube"},
        {"op": "add_camera", "location": [4, -4, 3], "rotation": [65, 0, 45]},
        {"op": "set_render", "engine": "workbench", "resolution": [64, 48]},
        {"op": "render", "path": str(outside)},
        {"op": "save", "path": "../escaped.blend"},
        {"op": "import_model", "path": "/etc/passwd.stl"},
        {"op": "render", "path": str(root / "inside.png")},
    ]}
    scene_file = tmp_path / "scene.json"
    scene_file.write_text(json.dumps(scene), encoding="utf-8")
    proc = subprocess.run(build_command(INFO["path"], str(scene_file), str(root)), cwd=str(root), capture_output=True,
                          text=True, timeout=200)
    report = parse_report(proc.stdout)
    assert report is not None and proc.returncode == 1
    statuses = [o["status"] for o in report["ops"]]
    assert statuses == ["ok", "ok", "ok", "error", "error", "error", "ok"]
    assert "outside the project folder" in report["ops"][3]["message"]
    assert "'..'" in report["ops"][4]["message"]
    assert not outside.exists() and not (tmp_path / "escaped.blend").exists()
    assert (root / "inside.png").exists()


def test_a_runner_run_prints_exactly_one_report_line(smoke_result):
    # the report line is the contract with the Python side
    assert smoke_result["report"]["schema"] == 1
    assert os.path.isfile(RUNNER_PATH)


def test_the_tool_smoke_action_end_to_end(tmp_path, monkeypatch):
    import asyncio

    from src import constants
    from src.agent_tools.blender_scene_tool import BlenderSceneTool

    monkeypatch.setattr(constants, "BLENDER_SCENES_DIR", str(tmp_path / "scenes"))
    monkeypatch.setattr(constants, "GENERATED_IMAGES_DIR", str(tmp_path / "gen"))
    out = asyncio.run(BlenderSceneTool().execute(json.dumps({"action": "smoke"}), {"session_id": "it", "no_gallery": True}))
    assert out["exit_code"] == 0, out["output"]
    assert "Reload check: 10/10 assertions passed" in out["output"]
    assert out["images"][0]["mimeType"] == "image/png"
    assert os.path.isfile(tmp_path / "scenes" / "it" / "smoke" / "smoke.blend")
