"""Edit-impact query on an invented scene in the installed Blender, read-only and without a model."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = r'''
import bpy, importlib.util, json, os
from pathlib import Path
spec = importlib.util.spec_from_file_location('faustus_impact_test', {module!r})
bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
copper = bpy.data.materials.new('Copper'); lookalike = bpy.data.materials.new('Lookalike')
lookalike.diffuse_color = copper.diffuse_color
def cube(name, material=None):
    bpy.ops.mesh.primitive_cube_add(); obj = bpy.context.object; obj.name = name
    if material: obj.data.materials.append(material)
    return obj
body = cube('Body', copper); lid = cube('Lid'); lid.parent = body
spare = bpy.data.objects.new('Spare', body.data); scene.collection.objects.link(spare)
badge = cube('Badge', copper); screw = cube('Screw', lookalike)
marker = bpy.data.objects.new('Marker', None); scene.collection.objects.link(marker)
follow = marker.constraints.new('COPY_LOCATION'); follow.name = 'FollowLid'; follow.target = lid
silent = screw.constraints.new('COPY_LOCATION'); silent.name = 'Silent'; silent.target = body; silent.mute = True
bpy.ops.object.select_all(action='DESELECT'); body.select_set(True); bpy.context.view_layer.objects.active = body
scene.frame_set(4)
before = bridge.scene_snapshot()
selection = [o.name for o in bpy.context.view_layer.objects if o.select_get()]
def names(result): return sorted(row['object'] for row in result['dependents'])

move = bridge.inspect_impact({{'object': 'Body', 'operation': 'transform'}})
assert names(move) == ['Lid', 'Marker'], move
marker_row = [r for r in move['dependents'] if r['object'] == 'Marker'][0]
assert [step['via'] for step in marker_row['path']] == ['parent', 'constraint:FollowLid'], marker_row
assert move['status'] == 'potential' and move['measured'] is None and move['frame'] == 4
assert [r['reason'] for r in move['inactive_relations']] == ['muted'] and 'Screw' not in names(move)

mesh = bridge.inspect_impact({{'object': 'Body', 'operation': 'mesh'}})
assert names(mesh) == ['Spare'] and mesh['shared_datablocks'][0]['users'] == ['Body', 'Spare']

material = bridge.inspect_impact({{'object': 'Body', 'operation': 'material'}})
assert names(material) == ['Badge', 'Spare'], material
assert material['shared_datablocks'][0]['name'] == 'Copper' and 'Screw' not in names(material)
assert material['coverage']['complete']

# A driver that reads the origin is a relation this reader does not follow: partial coverage.
drv = badge.driver_add('location', 0).driver
var = drv.variables.new(); var.targets[0].id = body; var.targets[0].data_path = 'location.x'; drv.expression = 'v'
partial = bridge.inspect_impact({{'object': 'Body', 'operation': 'transform'}})
assert not partial['coverage']['complete'] and partial['coverage']['unanalyzed'][0]['kind'] == 'driver'

capped = bridge.inspect_impact({{'object': 'Body', 'operation': 'material', 'max_results': 1}})
assert len(capped['dependents']) == 1 and capped['truncated'] == 1

def refused(payload, text):
    try: bridge.inspect_impact(payload)
    except ValueError as exc: assert text in str(exc), exc
    else: raise AssertionError('accepted ' + repr(payload))
refused({{'object': 'Nobody'}}, 'not found')
refused({{'object': 'Body', 'scene': 'Other'}}, 'not the open scene')
refused({{'object': 'Body', 'frame': 9}}, 'current frame')
refused({{'object': 'Body', 'operation': 'delete'}}, 'operation must be')
extra = bpy.data.materials.new('Second'); body.data.materials.append(extra)
refused({{'object': 'Body', 'operation': 'material'}}, 'Pass material')
assert names(bridge.inspect_impact({{'object': 'Body', 'operation': 'material', 'material': 'Second'}})) == ['Spare']

# Nothing changed: snapshot, selection and frame are as they were.
body.data.materials.pop(index=1); bpy.data.materials.remove(extra)
bpy.data.objects[badge.name].animation_data_clear()
assert [o.name for o in bpy.context.view_layer.objects if o.select_get()] == selection
assert scene.frame_current == 4 and not bpy.data.filepath
assert bridge.compare_snapshot(before)['unchanged']

# The sidebar operator runs the same reader on the active object.
os.environ['FAUSTUS_BLENDER_DATA_DIR'] = {data!r}
bridge.register()
bpy.context.window_manager.faustus_impact_operation = 'transform'
assert bpy.ops.faustus.query_impact() == {{'FINISHED'}}
text = bpy.context.window_manager.faustus_impact_result
assert 'Body: 2 dependientes' in text and 'Lid' in text, text
assert Path({data!r}, 'impact', 'last.json').is_file()
Path({receipt!r}).write_text(json.dumps({{'version': bpy.app.version_string, 'summary': text}}))
'''


def test_impact_reads_three_distinct_sets_without_changing_the_scene(tmp_path):
    blender = Path(os.getenv("BLENDER_TEST_EXE", "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"))
    if not blender.is_file():
        pytest.skip("Set BLENDER_TEST_EXE to an installed Blender 5.1+")
    receipt, script = tmp_path / "proof.json", tmp_path / "impact.py"
    data = tmp_path / "data"
    script.write_text(SCRIPT.format(module=str(ROOT / "blender_addon/faustus_bridge.py"), receipt=str(receipt), data=str(data)),
                      encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1")
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(key, None)
    for key in ("BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES", "BLENDER_USER_EXTENSIONS", "TEMP", "TMP"):
        directory = tmp_path / key
        directory.mkdir()
        env[key] = str(directory)
    result = subprocess.run([str(blender), "--background", "--factory-startup", "--offline-mode", "--python-exit-code", "1",
                             "--python", str(script)], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Body: 2 dependientes" in json.loads(receipt.read_text())["summary"]


def test_impact_is_offered_by_every_surface():
    from src.blender_live.client import OPERATIONS
    assert "impact" in OPERATIONS
    server = (ROOT / "mcp_servers/blender_scene_server.py").read_text(encoding="utf-8")
    assert "impact(" in server
