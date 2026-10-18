"""Real installed Blender regression, without a model or render/GPU workload."""
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_real_mesh_diff_and_cancelled_review_preserve_unedited_state(tmp_path):
    blender = Path(os.getenv("BLENDER_TEST_EXE", "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"))
    if not blender.is_file():
        pytest.skip("Set BLENDER_TEST_EXE to an installed Blender 5.1+")
    module = Path(__file__).resolve().parents[1] / "blender_addon/faustus_bridge.py"
    receipt = tmp_path / "proof.json"
    script = tmp_path / "regression.py"
    script.write_text(f'''
import bpy, importlib.util, json
from pathlib import Path
spec=importlib.util.spec_from_file_location('faustus_test', {str(module)!r})
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add();one=bpy.context.object;one.name='Edited'
bpy.ops.mesh.primitive_cube_add(location=(5,0,0));two=bpy.context.object;two.name='Preserved'
bpy.context.view_layer.update()
baseline=bridge.scene_snapshot()
one.data.vertices[0].co.x += 0.25
bpy.context.view_layer.update()
changed=bridge.compare_snapshot(baseline)
assert [row['name'] for row in changed['changed']] == ['Edited'], changed
assert 'mesh_sha256' in changed['changed'][0]['fields']
assert not changed['context_changed'] and not changed['added']
one.data.vertices[0].co.x -= 0.25
bpy.context.view_layer.update()
assert bridge.compare_snapshot(baseline)['unchanged']
scoped=bridge.scene_snapshot(['Edited'])
bpy.data.objects.remove(one,do_unlink=True)
assert bridge.compare_snapshot(scoped)['removed']==['Edited']
bpy.context.scene.display.shading.background_color=(.21,.34,.55)
bpy.context.scene.display.shading.background_type='VIEWPORT'
bpy.context.scene.frame_set(7)
before=bridge.scene_snapshot()
color=tuple(bpy.context.scene.display.shading.background_color)
class Service:
    directory=Path({str(tmp_path)!r})
    def check_cancel(self,*args): raise bridge.Cancelled('Original cancellation fixture')
    def progress(self,*args): pass
try:
    bridge.render_views({{'angles':[0],'frames':[12],'width':32,'height':32}},{{'id':'cancelled'}},Service())
except bridge.Cancelled: pass
else: raise AssertionError('Cancellation was ignored')
assert tuple(bpy.context.scene.display.shading.background_color)==color
assert bridge.compare_snapshot(before)['unchanged']
assert not any(obj.name.startswith('Faustus review') for obj in bpy.data.objects)
assert not any(camera.name.startswith('Faustus review') for camera in bpy.data.cameras)
Path({str(receipt)!r}).write_text(json.dumps({{'version':bpy.app.version_string,'changed':changed,'restored':True}}))
''', encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1")
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(key, None)
    for key in ("BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES", "BLENDER_USER_EXTENSIONS", "TEMP", "TMP"):
        directory = tmp_path / key
        directory.mkdir()
        env[key] = str(directory)
    result = subprocess.run([str(blender), "--background", "--factory-startup", "--offline-mode", "--python-exit-code", "1", "--python", str(script)],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(receipt.read_text())["restored"]
