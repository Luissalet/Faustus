"""Typed 3D scenes (src/blender_scene): the validator, the path rules, engine
names per Blender version, report parsing, discovery order and the launcher.

Nothing here starts Blender; tests/test_blender_scene_integration.py does."""
import json
import ntpath
import os
import subprocess

import pytest

from src import blender_scene as bs
from src.blender_scene import discovery, scene_common as common
from src.blender_scene.spec import MAX_OPS, OPS, scene_schema

POSIX_ROOT = "/data/blender_scenes/s1"


def good_scene():
    return {
        "meta": {"name": "demo", "stop_on_error": False},
        "ops": [
            {"op": "new_scene"},
            {"op": "set_world", "color": "#102030", "strength": 0.8},
            {"op": "add_material", "name": "Red", "base_color": [1, 0, 0], "roughness": 0.4},
            {"op": "add_mesh", "primitive": "cube", "name": "Box", "location": [0, 0, 1], "rotation": [0, 0, 45],
             "scale": [1, 2, 1], "size": 2, "material": "Red", "smooth": True},
            {"op": "add_modifier", "target": "Box", "type": "bevel", "width": 0.05, "bevel_segments": 3},
            {"op": "add_light", "type": "spot", "location": [3, 3, 5], "energy": 900, "spot_angle": 40},
            {"op": "add_camera", "name": "Cam", "location": [6, -6, 4], "lens": 40, "f_stop": 2.8,
             "dof_focus_object": "Box"},
            {"op": "track_to", "object": "Cam", "target": "Box"},
            {"op": "add_text", "body": "Hi", "extrude": 0.1},
            {"op": "set_render", "engine": "EEVEE", "resolution": [320, 240], "samples": 8, "device": "cpu"},
            {"op": "render", "path": "${PROJECT_ROOT}/out/a.png"},
            {"op": "export_model", "path": "models/box.stl", "target": "Box"},
            {"op": "save", "path": "${PROJECT_ROOT}/a.blend"},
            {"op": "assert", "kind": "object_count", "object_type": "mesh", "count": 1, "cmp": "ge"},
            {"op": "assert", "kind": "bbox_within", "object": "Box", "min": [-3, -3, -1], "max": [3, 3, 3]},
            {"op": "assert", "kind": "file_exists", "path": "out/a.png"},
        ],
    }


def check(scene, root=POSIX_ROOT):
    return bs.validate_scene(scene, root)


def lines(result):
    return result.error_lines()


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------

def test_a_good_scene_passes_and_is_normalised():
    res = check(good_scene())
    assert res.ok, lines(res)
    assert res.op_count == 16
    ops = res.scene["ops"]
    assert ops[1]["color"] == pytest.approx([0.005182, 0.014444, 0.029557, 1.0], abs=1e-5)   # sRGB hex -> linear rgba
    assert ops[2]["base_color"] == [1.0, 0.0, 0.0, 1.0]
    assert ops[9]["engine"] == "eevee"                                                # case-insensitive enum
    assert ops[10]["path"] == POSIX_ROOT + "/out/a.png"                               # ${PROJECT_ROOT} substituted
    assert ops[11]["path"] == POSIX_ROOT + "/models/box.stl"                          # relative -> absolute
    assert res.scene["meta"] == {"name": "demo", "stop_on_error": False}


def test_a_bare_list_and_a_json_string_are_accepted():
    ops = good_scene()["ops"]
    assert check(ops).ok
    assert check(json.dumps({"ops": ops})).ok


def test_unknown_op_is_rejected_with_index_and_suggestion():
    res = check({"ops": [{"op": "new_scene"}, {"op": "add_meshh", "primitive": "cube"}]})
    assert not res.ok
    assert len(res.errors) == 1
    err = res.errors[0]
    assert (err.op_index, err.op) == (1, "add_meshh")
    assert "unknown op 'add_meshh'" in err.message and "'add_mesh'" in err.message
    assert lines(res)[0].startswith("ops[1] (add_meshh):")


def test_unknown_field_is_rejected_with_the_field_name():
    res = check({"ops": [{"op": "add_mesh", "primitive": "cube", "colour": "red"}]})
    assert [(e.op_index, e.field) for e in res.errors] == [(0, "colour")]
    assert "unknown field" in res.errors[0].message
    assert "ops[0] (add_mesh).colour" in lines(res)[0]


def test_there_is_no_code_op_and_unknown_top_level_keys_are_rejected():
    for name in ("python", "exec", "eval", "script", "run_python"):
        assert name not in OPS
        assert not check({"ops": [{"op": name, "code": "import os"}]}).ok
    res = check({"ops": [{"op": "new_scene"}], "script": "x"})
    assert "unknown top-level field 'script'" in lines(res)[0]
    # a code-looking field is just an unknown field
    assert not check({"ops": [{"op": "add_mesh", "primitive": "cube", "python": "x"}]}).ok


@pytest.mark.parametrize("op,field,needle", [
    ({"op": "add_mesh", "primitive": "uv_sphere", "radius": -1}, "radius", "out of range"),
    ({"op": "add_mesh", "primitive": "uv_sphere", "segments": 2}, "segments", "out of range"),
    ({"op": "add_mesh", "primitive": "uv_sphere", "segments": 8.5}, "segments", "whole number"),
    ({"op": "add_mesh", "primitive": "cube", "location": [0, 0]}, "location", "expected 3 numbers"),
    ({"op": "add_mesh", "primitive": "cube", "location": "up"}, "location", "expected 3 numbers"),
    ({"op": "add_mesh", "primitive": "cube", "location": [0, 0, True]}, "location", "must be a number"),
    ({"op": "add_mesh", "primitive": "cube", "location": [0, 0, float("nan")]}, "location", "must be a number"),
    ({"op": "add_mesh", "primitive": "cube", "location": [0, 0, 1e9]}, "location", "out of range"),
    ({"op": "add_mesh", "primitive": "teapot"}, "primitive", "is not allowed"),
    ({"op": "add_mesh", "primitive": "cube", "smooth": "yes"}, "smooth", "true or false"),
    ({"op": "add_mesh", "primitive": "cube", "name": ""}, "name", "must not be empty"),
    ({"op": "add_mesh", "primitive": "cube", "name": "x" * 64}, "name", "too long"),
    ({"op": "add_material", "name": "M", "metallic": 1.5}, "metallic", "out of range"),
    ({"op": "add_material", "name": "M", "base_color": [1, 0, 2]}, "base_color", "out of range"),
    ({"op": "add_material", "name": "M", "base_color": "red"}, "base_color", "#rrggbb"),
    ({"op": "add_light", "type": "lamp"}, "type", "is not allowed"),
    ({"op": "add_camera", "lens": 0}, "lens", "out of range"),
    ({"op": "set_render", "resolution": [0, 100]}, "resolution", "out of range"),
    ({"op": "set_render", "resolution": [10000, 10000]}, "resolution", "more than"),
    ({"op": "set_render", "samples": 0}, "samples", "out of range"),
    ({"op": "set_render", "engine": "unreal"}, "engine", "is not allowed"),
    ({"op": "add_modifier", "target": "A", "type": "array", "count": 5000}, "count", "out of range"),
    ({"op": "assert", "kind": "object_count", "count": -1}, "count", "out of range"),
])
def test_out_of_range_and_wrongly_typed_values_are_rejected_precisely(op, field, needle):
    res = check({"ops": [{"op": "new_scene"}, op]})
    assert not res.ok
    hits = [e for e in res.errors if e.field == field]
    assert hits, lines(res)
    assert hits[0].op_index == 1 and needle in hits[0].message, hits[0].message


def test_required_fields_and_combinations_are_checked():
    res = check({"ops": [{"op": "add_mesh"}, {"op": "set_transform", "target": "A"}, {"op": "render"},
                         {"op": "add_camera", "dof_focus_object": "A", "dof_focus_distance": 3}]})
    got = {(e.op_index, e.field): e.message for e in res.errors}
    assert got[(0, "primitive")] == "is required"
    assert "at least one of" in got[(1, None)]
    assert got[(2, "path")] == "is required"
    assert (3, "dof_focus_distance") in got


def test_parameters_that_do_not_fit_the_primitive_modifier_or_light_are_rejected():
    res = check({"ops": [
        {"op": "add_mesh", "primitive": "cube", "radius": 1},
        {"op": "add_mesh", "primitive": "torus", "depth": 1},
        {"op": "add_modifier", "target": "A", "type": "mirror", "width": 0.1},
        {"op": "add_light", "type": "point", "spot_angle": 30},
    ]})
    assert [(e.op_index, e.field) for e in res.errors] == [(0, "radius"), (1, "depth"), (2, "width"), (3, "spot_angle")]
    assert "does not apply" in res.errors[0].message


def test_assert_kinds_require_their_own_fields():
    res = check({"ops": [
        {"op": "assert", "kind": "object_exists"},
        {"op": "assert", "kind": "resolution", "width": 10},
        {"op": "assert", "kind": "bbox_within", "object": "A"},
        {"op": "assert", "kind": "material_assigned", "object": "A", "material": "M", "count": 1},
        {"op": "assert", "kind": "nope"},
    ]})
    got = {(e.op_index, e.field) for e in res.errors}
    assert {(0, "name"), (1, "height"), (2, "min"), (3, "count"), (4, "kind")} <= got


def test_model_extensions_and_declared_format_must_agree():
    res = check({"ops": [
        {"op": "import_model", "path": "a.xyz"},
        {"op": "import_model", "path": "a.stl", "format": "obj"},
        {"op": "export_model", "path": "a.ply"},
        {"op": "load_blend", "path": "a.stl"},
        {"op": "render", "path": "a.tiff"},
    ]})
    assert [e.op_index for e in res.errors] == [0, 1, 2, 3, 4]
    ok = check({"ops": [{"op": "import_model", "path": "a.glb"}, {"op": "export_model", "path": "b.gltf"},
                        {"op": "import_model", "path": "c.PLY"}]})
    assert ok.ok, lines(ok)


def test_all_problems_are_reported_not_only_the_first():
    res = check({"ops": [{"op": "nope"}, {"op": "add_mesh", "primitive": "cube", "radius": 1, "size": -3},
                         {"op": "render", "path": "../x.png"}]})
    assert len(res.errors) >= 4
    assert {e.op_index for e in res.errors} == {0, 1, 2}


def test_structure_errors():
    assert not check("not json").ok
    assert not check(42).ok
    assert "missing 'ops'" in lines(check({"meta": {}}))[0]
    assert "non-empty" in lines(check({"ops": []}))[0]
    assert not check({"ops": ["x"]}).ok
    assert not check({"ops": [{"nope": 1}]}).ok
    assert "too many ops" in lines(check({"ops": [{"op": "new_scene"}] * (MAX_OPS + 1)}))[0]
    assert "unknown field" in lines(check({"meta": {"colour": 1}, "ops": [{"op": "new_scene"}]}))[0]


def test_warnings_do_not_block():
    res = check({"ops": [{"op": "new_scene"}, {"op": "render", "path": "a.png"}]})
    assert res.ok and any("no add_camera" in w for w in res.warnings)


def test_json_schema_is_served_and_covers_every_op():
    schema = scene_schema()
    assert schema["$schema"].startswith("https://json-schema.org/")
    variants = schema["oneOf"][0]["properties"]["ops"]["items"]["oneOf"]
    assert {v["properties"]["op"]["const"] for v in variants} == set(OPS)
    for v in variants:
        assert v["additionalProperties"] is False
    mesh = next(v for v in variants if v["title"] == "add_mesh")
    assert mesh["required"] == ["op", "primitive"]
    assert mesh["properties"]["segments"]["maximum"] == 256
    assert "${PROJECT_ROOT}" in next(v for v in variants if v["title"] == "render")["properties"]["path"]["description"]
    json.dumps(schema)
    text = bs.scene_summary()
    assert all(f"- {name}:" in text for name in OPS) and len(text) < 9000
    assert bs.scene_summary("render").startswith("- render:")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "../x.png", "a/../../x.png", "${PROJECT_ROOT}/../x.png", "${PROJECT_ROOT}/a/../../b.png",
    "..\\x.png", "a/./../../b.png",
])
def test_dot_dot_is_always_rejected(raw):
    res = check({"ops": [{"op": "render", "path": raw}]})
    assert not res.ok and "'..'" in res.errors[0].message


@pytest.mark.parametrize("raw", ["/etc/passwd.png", "/data/blender_scenes/s2/x.png", "/data/blender_scenes/s1evil/x.png",
                                 "/tmp/x.png", "C:\\Windows\\x.png", "\\\\server\\share\\x.png"])
def test_absolute_paths_outside_the_root_are_rejected(raw):
    res = check({"ops": [{"op": "render", "path": raw}]})
    assert not res.ok and "outside the project folder" in res.errors[0].message


def test_absolute_paths_inside_the_root_are_accepted():
    res = check({"ops": [{"op": "render", "path": POSIX_ROOT + "/deep/x.png"}]})
    assert res.ok and res.scene["ops"][0]["path"] == POSIX_ROOT + "/deep/x.png"


def test_the_root_token_is_only_allowed_at_the_start_and_only_that_variable():
    mid = check({"ops": [{"op": "render", "path": "a/${PROJECT_ROOT}/x.png"}]})
    assert "only allowed at the very start" in mid.errors[0].message
    unknown = check({"ops": [{"op": "render", "path": "${HOME}/x.png"}]})
    assert "unknown variable" in unknown.errors[0].message


def test_the_root_is_substituted_in_every_string_not_only_paths():
    res = check({"ops": [{"op": "add_text", "body": "root=${PROJECT_ROOT}"}]})
    assert res.ok and res.scene["ops"][0]["body"] == "root=" + POSIX_ROOT


def test_nul_and_empty_paths_are_rejected():
    assert not check({"ops": [{"op": "render", "path": "a\x00.png"}]}).ok
    assert not check({"ops": [{"op": "render", "path": "  "}]}).ok
    assert "not the folder itself" in check({"ops": [{"op": "save", "path": POSIX_ROOT}]}).errors[0].message


def test_a_symlink_out_of_the_root_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(str(outside), str(root / "link"))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    res = check({"ops": [{"op": "render", "path": "link/x.png"}, {"op": "render", "path": "ok/x.png"}]}, str(root))
    assert [e.op_index for e in res.errors] == [0]
    assert "symbolic link" in res.errors[0].message


def test_windows_paths_follow_the_windows_rules_on_any_platform():
    root = "C:\\Faustus\\data\\blender_scenes\\s1"
    ok, err = common.confine_path("${PROJECT_ROOT}\\out\\a.png", root)
    assert err is None and ok == ntpath.normpath(root + "\\out\\a.png")
    assert common.confine_path("out/a.png", root)[0] == ntpath.normpath(root + "\\out\\a.png")
    assert common.confine_path("c:\\FAUSTUS\\Data\\blender_scenes\\S1\\a.png", root)[1] is None   # case-insensitive
    for bad in ("C:\\Users\\me\\a.png", "D:\\a.png", "\\\\srv\\share\\a.png", "\\a.png", "/a.png",
                root + "evil\\a.png", "out\\..\\..\\a.png", "out\\a.png:stream", "C:a.png"):
        assert common.confine_path(bad, root)[1], bad
    assert "not the folder itself" in common.confine_path(root, root)[1]


# --------------------------------------------------------------------------
# Engine names, versions, report parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("version,expected", [
    ("3.6.5", "BLENDER_EEVEE"), ((4, 0, 2), "BLENDER_EEVEE"), ("4.1.1", "BLENDER_EEVEE"),
    ((4, 2, 0), "BLENDER_EEVEE_NEXT"), ("4.3.2", "BLENDER_EEVEE_NEXT"), ("4.5.1", "BLENDER_EEVEE_NEXT"),
    ((5, 0, 0), "BLENDER_EEVEE"), ("Blender 5.1.0", "BLENDER_EEVEE"),
])
def test_eevee_maps_to_the_internal_name_of_the_version(version, expected):
    assert common.engine_name("eevee", version) == expected
    assert common.engine_name("EEVEE", version) == expected
    assert common.engine_name("cycles", version) == "CYCLES"
    assert common.engine_name("workbench", version) == "BLENDER_WORKBENCH"


def test_engine_name_rejects_unknown_engines_and_round_trips():
    with pytest.raises(ValueError):
        common.engine_name("unreal", "4.3")
    for logical in ("eevee", "cycles", "workbench"):
        for ver in ("3.6", "4.3", "5.0"):
            assert common.logical_engine(common.engine_name(logical, ver)) == logical


def test_parse_version():
    assert common.parse_version("Blender 4.3.2\n build date") == (4, 3, 2)
    assert common.parse_version("4.0") == (4, 0, 0)
    assert common.parse_version((3, 6)) == (3, 6, 0)
    assert common.parse_version("Blender 5.0 LTS") == (5, 0, 0)
    assert common.parse_version("no digits") is None


def test_parse_report_takes_the_last_valid_report_line():
    report = {"ok": True, "errors": 0, "ops": []}
    out = "\n".join([
        "Blender 4.3.2", "Read prefs", "BLENDER_SCENE_REPORT {not json}",
        "BLENDER_SCENE_REPORT " + json.dumps({"ok": False}),
        "BLENDER_SCENE_REPORT " + json.dumps(report),
        "Blender quit",
    ])
    assert bs.parse_report(out) == report
    assert bs.parse_report("nothing here") is None
    assert bs.parse_report("") is None
    assert bs.parse_report("BLENDER_SCENE_REPORT [1, 2]") is None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class FakeFS:
    def __init__(self, files=(), dirs=None):
        self.files = {f for f in files}
        self.dirs = dirs or {}

    def isfile(self, p): return p in self.files
    def isdir(self, p): return p in self.dirs
    def listdir(self, p):
        if p not in self.dirs:
            raise FileNotFoundError(p)
        return self.dirs[p]


def find(fs, **kw):
    kw.setdefault("env", {})
    kw.setdefault("which", lambda name: None)
    kw.setdefault("home", "/home/u")
    return discovery.find_blender(isfile=fs.isfile, isdir=fs.isdir, listdir=fs.listdir, **kw)


def test_discovery_order_setting_then_env_then_path_then_common():
    files = ["/s/blender", "/e/blender", "/p/blender", "/usr/bin/blender"]
    fs = FakeFS(files)
    which = lambda name: "/p/blender"
    cand, _ = find(fs, setting="/s/blender", env={"BLENDER_PATH": "/e/blender"}, which=which, platform="linux")
    assert (cand.source, cand.path) == ("setting", "/s/blender")
    cand, _ = find(fs, setting="", env={"BLENDER_PATH": "/e/blender"}, which=which, platform="linux")
    assert (cand.source, cand.path) == ("env", "/e/blender")
    cand, _ = find(fs, setting="", env={}, which=which, platform="linux")
    assert (cand.source, cand.path) == ("path", "/p/blender")
    cand, _ = find(fs, setting="", env={}, platform="linux")
    assert (cand.source, cand.path) == ("common", "/usr/bin/blender")


def test_a_configured_path_that_does_not_exist_falls_through_to_the_next_source():
    fs = FakeFS(["/usr/bin/blender"])
    cand, tried = find(fs, setting="/missing/blender", platform="linux")
    assert cand.path == "/usr/bin/blender"
    assert tried[0].source == "setting"


def test_a_configured_folder_is_resolved_to_the_executable():
    fs = FakeFS(["/opt/mine/blender"], dirs={"/opt/mine": ["blender"]})
    cand, _ = find(fs, setting="/opt/mine", platform="linux")
    assert cand.path == "/opt/mine/blender"
    wfs = FakeFS(["D:\\Tools\\Blender\\blender.exe"], dirs={"D:\\Tools\\Blender": ["blender.exe"]})
    cand, _ = find(wfs, setting='"D:\\Tools\\Blender"', platform="win32")
    assert cand.path == "D:\\Tools\\Blender\\blender.exe"


def test_windows_install_folders_are_searched_newest_version_first():
    base = "C:\\Program Files\\Blender Foundation"
    names = ["Blender 3.6", "Blender 4.0", "Blender 4.3", "Blender 4.5", "Blender 5.0", "Blender Launcher"]
    fs = FakeFS([f"{base}\\{n}\\blender.exe" for n in names[:5]], dirs={base: names})
    cand, tried = find(fs, platform="win32", env={"ProgramFiles": "C:\\Program Files"})
    assert (cand.source, cand.path) == ("windows", base + "\\Blender 5.0\\blender.exe")
    order = [c.path for c in tried if c.source == "windows"]
    assert order[:5] == [f"{base}\\Blender {v}\\blender.exe" for v in ("5.0", "4.5", "4.3", "4.0", "3.6")]
    # 5.0 uninstalled: the next newest wins
    fs2 = FakeFS([f"{base}\\{n}\\blender.exe" for n in names[:4]], dirs={base: names})
    assert find(fs2, platform="win32")[0].path.endswith("Blender 4.5\\blender.exe")


def test_windows_falls_back_to_the_known_versions_when_the_folder_cannot_be_listed():
    base = "C:\\Program Files\\Blender Foundation"
    fs = FakeFS([base + "\\Blender 4.0\\blender.exe"])
    cand, tried = find(fs, platform="win32")
    assert cand.path == base + "\\Blender 4.0\\blender.exe"
    assert [c.path for c in tried if c.source == "windows"][0].endswith("Blender 5.0\\blender.exe")


def test_windows_also_looks_in_the_local_programs_folder_and_uses_where_blender_is_on_path():
    local = "C:\\Users\\me\\AppData\\Local\\Programs\\Blender Foundation"
    fs = FakeFS([local + "\\Blender 4.3\\blender.exe"], dirs={local: ["Blender 4.3"]})
    cand, _ = find(fs, platform="win32", env={"LOCALAPPDATA": "C:\\Users\\me\\AppData\\Local"})
    assert cand.path == local + "\\Blender 4.3\\blender.exe"
    fs2 = FakeFS(["C:\\Tools\\blender.exe"])
    cand, _ = find(fs2, platform="win32", which=lambda n: "C:\\Tools\\blender.exe")
    assert cand.source == "path"


def test_linux_and_mac_locations():
    fs = FakeFS(["/opt/blender-4.3.2-linux-x64/blender", "/opt/blender-3.6.0-linux-x64/blender"],
                dirs={"/opt": ["blender-3.6.0-linux-x64", "blender-4.3.2-linux-x64", "other"]})
    cand, _ = find(fs, platform="linux")
    assert cand.path == "/opt/blender-4.3.2-linux-x64/blender"
    mac = FakeFS(["/Applications/Blender.app/Contents/MacOS/Blender"])
    cand, _ = find(mac, platform="darwin")
    assert cand.source == "common" and cand.path.endswith("Blender.app/Contents/MacOS/Blender")


def test_probe_reports_path_and_version_or_a_clear_message():
    fs = FakeFS(["/usr/bin/blender"])
    fake = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="Blender 4.3.2\n\tbuild date: x\n", stderr="")
    info = discovery.probe(setting="", env={}, which=lambda n: None, platform="linux", home="/h",
                           isfile=fs.isfile, isdir=fs.isdir, listdir=fs.listdir, runner=fake)
    assert info["found"] and info["version"] == "4.3.2" and info["version_tuple"] == [4, 3, 2]
    assert info["engines"] == {"eevee": "BLENDER_EEVEE_NEXT", "cycles": "CYCLES", "workbench": "BLENDER_WORKBENCH"}
    assert "/usr/bin/blender" in info["message"]

    none = discovery.probe(setting="", env={}, which=lambda n: None, platform="linux", home="/h",
                           isfile=lambda p: False, isdir=lambda p: False, listdir=lambda p: [], runner=fake)
    assert none["found"] is False and "not found" in none["message"] and "blender_path" in none["message"]

    broken = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="segfault")
    bad = discovery.probe(setting="", env={}, which=lambda n: None, platform="linux", home="/h",
                          isfile=fs.isfile, isdir=fs.isdir, listdir=fs.listdir, runner=broken)
    assert bad["found"] is False and "did not answer" in bad["message"]


def test_probe_notes_a_setting_or_env_that_points_nowhere():
    fs = FakeFS(["/usr/bin/blender"])
    fake = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="Blender 4.0.1", stderr="")
    info = discovery.probe(setting="/nope", env={"BLENDER_PATH": "/nada"}, which=lambda n: None, platform="linux",
                           home="/h", isfile=fs.isfile, isdir=fs.isdir, listdir=fs.listdir, runner=fake)
    assert info["found"] and len(info["notes"]) == 2


def test_the_blender_path_setting_is_read_from_settings(monkeypatch):
    from src import settings

    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: "/from/settings" if key == "blender_path" else default)
    assert discovery.configured_path() == "/from/settings"
    assert "blender_path" in settings.DEFAULT_SETTINGS and "blender_timeout_seconds" in settings.DEFAULT_SETTINGS


# --------------------------------------------------------------------------
# Launcher (Blender replaced by a fake)
# --------------------------------------------------------------------------

FOUND = {"found": True, "path": "/fake/blender", "version": "4.3.2", "source": "path"}


def _completed(stdout="", stderr="", code=0):
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)


def test_run_scene_stops_at_validation_without_starting_blender(tmp_path):
    called = []
    res = bs.run_scene({"ops": [{"op": "bogus"}]}, str(tmp_path), runner=lambda *a, **k: called.append(1),
                       probe_result=FOUND)
    assert res["ok"] is False and res["stage"] == "validate" and not called
    assert "unknown op 'bogus'" in res["errors"][0]


def test_run_scene_reports_a_missing_blender(tmp_path):
    res = bs.run_scene({"ops": [{"op": "new_scene"}]}, str(tmp_path),
                       probe_result={"found": False, "message": "Blender was not found. Install it"})
    assert res["stage"] == "discovery" and "not found" in res["errors"][0]


def test_run_scene_launches_the_runner_with_the_expected_command_and_parses_the_report(tmp_path):
    seen = {}
    report = {"ok": True, "errors": 0, "blender": {"version": "4.3.2"},
              "ops": [{"index": 0, "op": "new_scene", "status": "ok", "duration_s": 0.1}],
              "assertions": [{"kind": "object_count", "passed": True}],
              "outputs": [{"path": str(tmp_path / "a.png"), "bytes": 10, "kind": "render"},
                          {"path": str(tmp_path / "a.blend"), "bytes": 99, "kind": "blend"}],
              "warnings": ["w"]}

    def fake(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="noise\nBLENDER_SCENE_REPORT " + json.dumps(report) + "\n", stderr="")

    res = bs.run_scene({"ops": [{"op": "new_scene"}]}, str(tmp_path), runner=fake, probe_result=FOUND, timeout=42)
    cmd = seen["cmd"]
    assert cmd[0] == "/fake/blender"
    assert cmd[1:4] == ["--background", "--factory-startup", "--disable-autoexec"]
    assert cmd[cmd.index("--python") + 1].endswith("blender_runner.py")
    assert cmd[-3] == "--" and cmd[-2].endswith("scene.json") and cmd[-1] == str(tmp_path)
    assert seen["kw"]["cwd"] == str(tmp_path) and seen["kw"]["timeout"] == 42
    assert "PYTHONHOME" not in seen["kw"]["env"]
    scene_file = json.loads(open(cmd[-2], encoding="utf-8").read())
    assert scene_file["ops"] == [{"op": "new_scene"}]
    assert res["ok"] and res["stage"] == "done" and res["errors"] == []
    assert res["images"] == [str(tmp_path / "a.png")]
    assert res["warnings"] == ["w"] and res["assertions"] == {"total": 1, "failed": 0}


def test_failed_ops_make_the_run_fail_with_one_line_each(tmp_path):
    report = {"ok": False, "errors": 1, "ops": [
        {"index": 0, "op": "new_scene", "status": "ok"},
        {"index": 1, "op": "add_mesh", "status": "error", "message": "object 'A' already exists"}]}
    res = bs.run_scene({"ops": [{"op": "new_scene"}]}, str(tmp_path), probe_result=FOUND,
                       runner=_completed("BLENDER_SCENE_REPORT " + json.dumps(report), code=1))
    assert res["ok"] is False and res["stage"] == "done"
    assert res["errors"] == ["ops[1] add_mesh: object 'A' already exists"]


def test_a_missing_report_line_is_a_clear_failure_with_the_stderr_tail(tmp_path):
    res = bs.run_scene({"ops": [{"op": "new_scene"}]}, str(tmp_path), probe_result=FOUND,
                       runner=_completed("Blender 4.3.2\n", "x" * 3000 + "\nTraceback: boom", code=3))
    assert res["ok"] is False and res["stage"] == "no_report"
    assert "without a BLENDER_SCENE_REPORT line" in res["errors"][0] and "code 3" in res["errors"][0]
    assert res["stderr_tail"].endswith("Traceback: boom") and len(res["stderr_tail"]) < 1700


def test_a_clean_exit_without_a_report_is_still_a_failure(tmp_path):
    res = bs.run_scene({"ops": [{"op": "new_scene"}]}, str(tmp_path), probe_result=FOUND, runner=_completed("hi", code=0))
    assert res["ok"] is False and res["stage"] == "no_report"


def test_a_timeout_stops_blender_and_says_so(tmp_path):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"partial out", stderr=b"partial err")

    res = bs.run_scene({"ops": [{"op": "new_scene"}]}, str(tmp_path), probe_result=FOUND, runner=slow, timeout=7)
    assert res["stage"] == "timeout" and "within 7 s" in res["errors"][0]
    assert res["stdout_tail"] == "partial out" and res["stderr_tail"] == "partial err"


def test_the_timeout_comes_from_the_setting_and_is_clamped(tmp_path, monkeypatch):
    from src import settings

    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: 99999 if key == "blender_timeout_seconds" else default)
    assert bs.default_timeout() == 3600
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: 1 if key == "blender_timeout_seconds" else default)
    assert bs.default_timeout() == 5
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: "abc" if key == "blender_timeout_seconds" else default)
    assert bs.default_timeout() == 300


def test_an_executable_that_cannot_start_is_a_launch_failure(tmp_path):
    def boom(cmd, **kw):
        raise PermissionError("denied")

    res = bs.run_scene({"ops": [{"op": "new_scene"}]}, str(tmp_path), probe_result=FOUND, runner=boom)
    assert res["stage"] == "launch" and "denied" in res["errors"][0]


def test_the_runner_script_and_shared_module_ship_next_to_each_other():
    from src.blender_scene.run import RUNNER_PATH

    folder = os.path.dirname(RUNNER_PATH)
    assert os.path.isfile(os.path.join(folder, "blender_runner.py"))
    assert os.path.isfile(os.path.join(folder, "scene_common.py"))
    src = open(os.path.join(folder, "blender_runner.py"), encoding="utf-8").read()
    for forbidden in ("exec(", "eval(", "subprocess", "os.system", "compile(", "__import__"):
        assert forbidden not in src, forbidden
    compile(src, "blender_runner.py", "exec")


def test_the_smoke_scene_is_valid_small_and_confined():
    scene = bs.smoke_scene()
    res = check(scene)
    assert res.ok, lines(res)
    assert len(scene["ops"]) <= 24
    assert {"new_scene", "set_world", "add_mesh", "add_modifier", "add_material", "add_light", "add_camera",
            "track_to", "set_render", "render", "save", "assert"} <= {o["op"] for o in scene["ops"]}
    render = next(o for o in scene["ops"] if o["op"] == "set_render")
    assert render["resolution"] == [320, 240] and render["samples"] <= 16
    assert check(bs.reload_scene()).ok
    assert check(bs.smoke_scene("eevee")).ok and check(bs.smoke_scene("cycles")).ok
