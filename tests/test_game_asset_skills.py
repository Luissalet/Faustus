"""The game-asset skills ship scripts that really run: headless Blender builds, bakes, retargets and exports,
and the PBR packer rejects bad texture sets. Blender-dependent tests skip when it is not installed."""
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBRARY = os.path.join(ROOT, "skills", "library")
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "game_assets")
SKILLS = ("game-asset-rigging", "game-motion-from-video", "game-asset-retopology", "game-asset-uv-bake",
          "game-pbr-materials", "game-unity-export")


def _find_blender():
    found = shutil.which("blender")
    if found:
        return found
    base = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Blender Foundation")
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            exe = os.path.join(base, name, "blender.exe")
            if os.path.isfile(exe):
                return exe
    return None


BLENDER = _find_blender()
needs_blender = pytest.mark.skipif(BLENDER is None, reason="Blender is not installed")


def reference(skill, name):
    return os.path.join(LIBRARY, skill, "references", name)


def blender(blend, script, *args, timeout=600):
    command = [BLENDER, "-b", "--factory-startup"] + ([blend] if blend else []) + ["-P", script, "--"] + [str(a) for a in args]
    done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout + done.stderr


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    return tmp_path_factory.mktemp("game_assets")


@pytest.fixture(scope="module")
def blockman(work):
    out = str(work / "blockman.blend")
    code, log = blender(None, os.path.join(FIXTURES, "make_blockman.py"), out)
    assert code == 0 and os.path.isfile(out), log[-800:]
    return out


@pytest.fixture(scope="module")
def high(work):
    out = str(work / "high.blend")
    code, log = blender(None, os.path.join(FIXTURES, "make_highpoly.py"), out)
    assert code == 0 and os.path.isfile(out), log[-800:]
    return out


def test_every_game_asset_skill_ships_its_script():
    expected = {"game-asset-rigging": ["rig_humanoid.py"], "game-motion-from-video": ["landmarks_to_action.py", "extract_landmarks.py"],
                "game-asset-retopology": ["retopo.py"], "game-asset-uv-bake": ["bake_maps.py"],
                "game-pbr-materials": ["pbr_pack.py"], "game-unity-export": ["export_unity.py"]}
    assert set(expected) == set(SKILLS)
    for skill, files in expected.items():
        assert os.path.isfile(os.path.join(LIBRARY, skill, "SKILL.md"))
        for name in files:
            path = reference(skill, name)
            assert os.path.isfile(path), path
            compile(open(path, encoding="utf-8").read(), path, "exec")


@needs_blender
def test_rig_is_complete_weighted_and_deforms_the_right_parts(work, blockman):
    rigged, report = str(work / "rigged.blend"), str(work / "rig.json")
    code, log = blender(blockman, reference("game-asset-rigging", "rig_humanoid.py"), "build", "--out", rigged, "--report", report)
    assert code == 0, log[-1200:]
    data = json.load(open(report, encoding="utf-8"))
    assert data["ok"] and not data["missing_required"] and data["unweighted_vertices"] == 0
    # An independent reopen: a left-hand vertex must be driven by the left arm bones, not by a leg.
    probe = work / "probe.py"
    probe.write_text(textwrap.dedent("""
        import bpy, json, sys
        mesh = bpy.data.objects["Body"]
        names = {g.index: g.name for g in mesh.vertex_groups}
        far = max(mesh.data.vertices, key=lambda v: v.co.x)
        main = max(far.groups, key=lambda g: g.weight)
        print("PROBE", json.dumps({"x": far.co.x, "bone": names[main.group]}))
    """), encoding="utf-8")
    code, log = blender(rigged, str(probe))
    line = next(l for l in log.splitlines() if l.startswith("PROBE"))
    bone = json.loads(line[6:])["bone"]
    assert bone.startswith("Left") and ("Arm" in bone or "Hand" in bone), bone


@needs_blender
def test_retopo_bake_and_export_chain_on_a_dense_mesh(work, high):
    low, retopo = str(work / "low.blend"), str(work / "retopo.json")
    code, log = blender(high, reference("game-asset-retopology", "retopo.py"), "--source", "Sculpt", "--faces", 2000, "--out", low, "--report", retopo)
    assert code == 0, log[-1200:]
    r = json.load(open(retopo, encoding="utf-8"))
    assert r["source"]["faces"] > 50000 and 1000 < r["low"]["faces"] < 3500 and r["deviation"]["mean"] < 0.01
    maps, bake = str(work / "maps"), str(work / "bake.json")
    code, log = blender(low, reference("game-asset-uv-bake", "bake_maps.py"), "--high", "Sculpt", "--low", "Sculpt_low", "--size", 256, "--outdir", maps, "--report", bake)
    assert code == 0, log[-1500:]
    b = json.load(open(bake, encoding="utf-8"))
    assert b["ok"] and b["maps"]["ao"]["std"] > 0.1 and b["maps"]["normal"]["signal_fraction"] > 0.05
    assert os.path.getsize(os.path.join(maps, "Sculpt_low_normal.png")) > 1000
    fbx, export = str(work / "fbx" / "Sculpt.fbx"), str(work / "export.json")
    code, log = blender(low, reference("game-unity-export", "export_unity.py"), "--object", "Sculpt_low", "--out", fbx, "--report", export)
    assert code == 0, log[-1200:]
    e = json.load(open(export, encoding="utf-8"))
    tris = [l["triangles"] for l in e["lods"]]
    assert [l["name"] for l in e["lods"]] == ["Sculpt_LOD0", "Sculpt_LOD1", "Sculpt_LOD2"]
    assert tris == sorted(tris, reverse=True) and len(set(tris)) == 3
    assert os.path.getsize(fbx) > 10000


@needs_blender
def test_motion_retarget_matches_the_landmark_directions_after_reload(work, blockman):
    rigged = str(work / "rigged2.blend")
    assert blender(blockman, reference("game-asset-rigging", "rig_humanoid.py"), "build", "--out", rigged)[0] == 0
    frames = []
    for i in range(12):
        import math
        a = math.radians(i * 12)
        f = {"LEFT_HIP": [.1, 0, 0], "RIGHT_HIP": [-.1, 0, 0], "LEFT_SHOULDER": [.2, -.5, 0], "RIGHT_SHOULDER": [-.2, -.5, 0],
             "LEFT_ELBOW": [.2 + .3 * math.cos(a), -.5 + .3 * math.sin(a), 0], "LEFT_WRIST": [.2 + .6 * math.cos(a), -.5 + .6 * math.sin(a), .1],
             "RIGHT_ELBOW": [-.5, -.5, 0], "RIGHT_WRIST": [-.78, -.5, 0], "LEFT_KNEE": [.1, .45, 0], "LEFT_ANKLE": [.1, .9, 0],
             "RIGHT_KNEE": [-.1, .45, -.1], "RIGHT_ANKLE": [-.1, .9, -.15]}
        frames.append(f)
    pose = work / "pose.json"
    pose.write_text(json.dumps({"fps": 24, "frames": frames}), encoding="utf-8")
    animated, report = str(work / "animated.blend"), str(work / "motion.json")
    code, log = blender(rigged, reference("game-motion-from-video", "landmarks_to_action.py"), "--landmarks", pose, "--rig", "Rig", "--action", "Swing", "--out", animated, "--report", report)
    assert code == 0, log[-1500:]
    probe = work / "probe_motion.py"
    probe.write_text(textwrap.dedent("""
        import bpy, json, math
        from mathutils import Vector
        rig = bpy.data.objects["Rig"]
        pb = rig.pose.bones["LeftLowerArm"]
        out = []
        for frame in (1, 6, 12):
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            d = (rig.matrix_world @ pb.matrix).to_3x3() @ Vector((0, 1, 0))
            out.append([d.x, d.y, d.z])
        print("PROBE", json.dumps({"action": rig.animation_data.action.name, "dirs": out}))
    """), encoding="utf-8")
    code, log = blender(animated, str(probe))
    got = json.loads(next(l for l in log.splitlines() if l.startswith("PROBE"))[6:])
    assert got["action"] == "Swing"
    for frame, direction in zip((1, 6, 12), got["dirs"]):
        f = frames[frame - 1]
        want = [f["LEFT_WRIST"][0] - f["LEFT_ELBOW"][0], f["LEFT_WRIST"][2] , -(f["LEFT_WRIST"][1] - f["LEFT_ELBOW"][1])]
        norm = sum(c * c for c in want) ** .5
        dot = sum(a * b for a, b in zip(direction, [c / norm for c in want]))
        assert dot > 0.999, (frame, direction, want)


def _write_maps(folder, flipped=False, dark=False, size=64):
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(3)
    folder.mkdir(exist_ok=True)
    albedo = rng.integers(0 if dark else 80, 5 if dark else 190, (size, size, 3)).astype("uint8")
    n = np.zeros((size, size, 3), "float32")
    n[..., 0] = rng.normal(0, .05, (size, size))
    n[..., 1] = rng.normal(0, .05, (size, size))
    n[..., 2] = 1
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    if flipped:
        n[..., 2] *= -1
    metal = np.zeros((size, size), "uint8")
    metal[:, size // 2:] = 255
    paths = {}
    for key, array in (("albedo", albedo), ("normal", ((n + 1) / 2 * 255).astype("uint8")), ("roughness", np.full((size, size), 51, "uint8")),
                       ("metallic", metal), ("ao", np.full((size, size), 230, "uint8"))):
        paths[key] = str(folder / (key + ".png"))
        Image.fromarray(array).save(paths[key])
    return paths


def _pack(paths, out, *extra):
    cmd = [sys.executable, "-I", reference("game-pbr-materials", "pbr_pack.py"), "--outdir", str(out), "--name", "T", "--report", str(out) + ".json"]
    for key, value in paths.items():
        cmd += ["--" + key, value]
    return subprocess.run(cmd + list(extra), capture_output=True, text=True, timeout=120)


def test_pbr_pack_writes_hdrp_mask_with_inverted_roughness(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from PIL import Image
    import numpy as np
    done = _pack(_write_maps(tmp_path / "src"), tmp_path / "hdrp")
    assert done.returncode == 0, done.stdout + done.stderr
    mask = np.asarray(Image.open(tmp_path / "hdrp" / "T_Mask.png"))
    assert mask.shape[-1] == 4
    assert abs(int(mask[0, 0, 3]) - (255 - 51)) <= 1 and int(mask[0, 0, 0]) == 0 and int(mask[0, -1, 0]) == 255
    assert abs(int(mask[0, 0, 1]) - 230) <= 1
    gltf = _pack(_write_maps(tmp_path / "src"), tmp_path / "gltf", "--target", "gltf")
    assert gltf.returncode == 0, gltf.stdout
    orm = np.asarray(Image.open(tmp_path / "gltf" / "T_ORM.png"))
    assert orm.shape[-1] == 3 and abs(int(orm[0, 0, 1]) - 51) <= 1 and int(orm[0, -1, 2]) == 255


def test_pbr_pack_refuses_bad_sets_and_writes_nothing(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    inward = _pack(_write_maps(tmp_path / "a", flipped=True), tmp_path / "bad_normal")
    assert inward.returncode != 0 and "into the surface" in inward.stdout
    black = _pack(_write_maps(tmp_path / "b", dark=True), tmp_path / "bad_albedo")
    assert black.returncode != 0 and "albedo luminance" in black.stdout
    odd = _pack(_write_maps(tmp_path / "c", size=60), tmp_path / "bad_size")
    assert odd.returncode != 0 and "power of two" in odd.stdout
    for folder in ("bad_normal", "bad_albedo", "bad_size"):
        assert not (tmp_path / folder).exists()
