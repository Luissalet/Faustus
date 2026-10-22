"""Check editable checkpoint/recovery files in the installed engine, without a model."""
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_checkpoint_restores_assets_and_preserves_later_manual_work(tmp_path):
    blender = Path(os.getenv("BLENDER_TEST_EXE", "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe"))
    if not blender.is_file():
        pytest.skip("Set BLENDER_TEST_EXE to an installed Blender 5.1+")
    module = Path(__file__).resolve().parents[1] / "blender_addon/faustus_bridge.py"
    script = tmp_path / "checkpoint_proof.py"
    script.write_text('''
import bpy, importlib.util, json
from pathlib import Path
ROOT = Path(__ROOT__)
spec=importlib.util.spec_from_file_location('faustus_checkpoint_test', __MODULE__)
bridge=importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add(); cube=bpy.context.object; cube.name='Hero'
modifier=cube.modifiers.new('Subdivision', 'SUBSURF'); modifier.levels=1
material=bpy.data.materials.new('Editable material'); material.use_nodes=True
cube.data.materials.append(material)
texture=bpy.data.images.new('External texture', width=2, height=2)
texture.pixels[:]=[1.,0.,0.,1.]*4
texture.filepath_raw=str(ROOT/'texture.png'); texture.file_format='PNG'; texture.save()
texture.source='FILE'; texture.reload()
node=material.node_tree.nodes.new('ShaderNodeTexImage'); node.image=texture
group=bpy.data.node_groups.new('Nested shader', 'ShaderNodeTree')
math=group.nodes.new('ShaderNodeMath'); math.operation='ADD'
group_node=material.node_tree.nodes.new('ShaderNodeGroup'); group_node.node_tree=group
bpy.ops.mesh.primitive_uv_sphere_add(location=(4,0,0)); bpy.context.object.name='Prop'
bpy.ops.object.armature_add(); rig=bpy.context.object; rig.name='Rig'
rig.pose.bones[0].rotation_mode='XYZ'
rig.pose.bones[0].rotation_euler.z=.25
rig.pose.bones[0].keyframe_insert(data_path='rotation_euler', frame=1)
rig.pose.bones[0].rotation_euler.z=.75
rig.pose.bones[0].keyframe_insert(data_path='rotation_euler', frame=12)
bpy.context.scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=str(ROOT/'working.blend'))
baseline=bridge.scene_snapshot()
modifier.levels=2
diff=bridge.compare_snapshot(baseline)
assert any(row['name']=='Hero' and 'modifiers' in row['fields'] for row in diff['changed']),diff
modifier.levels=1
math.operation='MULTIPLY'
assert any(row['name']=='Hero' and 'materials_sha256' in row['fields'] for row in bridge.compare_snapshot(baseline)['changed'])
math.operation='ADD'
texture.pixels[:]=[0.,1.,0.,1.]*4; texture.save()
assert 'External texture' in bridge.compare_snapshot(baseline)['images_changed']
texture.pixels[:]=[1.,0.,0.,1.]*4; texture.save()
checkpoint=bridge.save_checkpoint(ROOT, {'label':'Original assets','conversation_id':'scene-chat','turn_id':'turn-one'})
assert bpy.data.filepath==str(ROOT/'working.blend')
assert not texture.packed_file
cube.location.x=9
rig.pose.bones[0].rotation_euler.z=1.2
rig.pose.bones[0].keyframe_insert(data_path='rotation_euler', frame=1)
manual=bridge.scene_snapshot()
metadata_path=bridge.checkpoint_record(ROOT,checkpoint['checkpoint_id'])
damaged={**checkpoint,'sha256':'0'*64}; bridge.write_json(metadata_path,damaged)
try:
    bridge.restore_checkpoint(ROOT, {'checkpoint_id':checkpoint['checkpoint_id']})
except ValueError as exc:
    assert 'digest' in str(exc)
else: raise AssertionError('Changed checkpoint digest was accepted')
assert bpy.data.objects['Hero'].location.x==9 and len(bridge.list_checkpoints(ROOT))==1
bridge.write_json(metadata_path,checkpoint)
restored=bridge.restore_checkpoint(ROOT, {'checkpoint_id':checkpoint['checkpoint_id']})
assert bpy.data.filepath==str(ROOT/'working.blend'),bpy.data.filepath
assert bpy.data.objects['Hero'].location.x==0
assert bpy.data.objects['Hero'].modifiers['Subdivision'].levels==1
assert bpy.data.objects['Prop'].type=='MESH'
assert bpy.data.objects['Rig'].animation_data.action
assert bpy.data.images['External texture'].packed_file
assert restored['conversation_id']=='scene-chat' and restored['turn_id']=='turn-one'
recovery=json.loads(bridge.checkpoint_record(ROOT,restored['recovery_checkpoint_id']).read_text(encoding='utf-8'))
assert Path(recovery['destination_before']['backup']).is_file()
bpy.data.objects['Prop'].location.y=7
undo=bridge.restore_checkpoint(ROOT, restored['undo_restore']['arguments'])
assert bpy.data.objects['Hero'].location.x==9
assert abs(bpy.data.objects['Rig'].pose.bones[0].rotation_euler.z-1.2)<1e-5
assert bpy.data.objects['Prop'].location.y==0
after_undo=json.loads(bridge.checkpoint_record(ROOT,undo['recovery_checkpoint_id']).read_text(encoding='utf-8'))
assert after_undo['snapshot']['objects']['Prop']['location'][1]==7
assert len(bridge.list_checkpoints(ROOT))==3
bridge.register()
bpy.context.window_manager.faustus_checkpoint=checkpoint['checkpoint_id']
bpy.context.window_manager.faustus_checkpoint_label='Sidebar saved'
bpy.ops.faustus.save_checkpoint()
selected_id=bpy.context.window_manager.faustus_checkpoint
bridge.save_checkpoint(ROOT, {'label':'Created from another tool'})
assert bpy.context.window_manager.faustus_checkpoint==selected_id
bridge.unregister()
bpy.ops.wm.open_mainfile(filepath=str(ROOT/'working.blend'),load_ui=False)
assert bpy.data.objects['Hero'].location.x==9
assert abs(bpy.data.objects['Rig'].pose.bones[0].rotation_euler.z-1.2)<1e-5
Path(ROOT/'proof.json').write_text(json.dumps({'version':bpy.app.version_string,'checkpoint':checkpoint['checkpoint_id'],'restored':restored,'undo':undo}),encoding='utf-8')
'''.replace("__ROOT__", repr(str(tmp_path))).replace("__MODULE__", repr(str(module))), encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1", FAUSTUS_BLENDER_DATA_DIR=str(tmp_path))
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        env.pop(key, None)
    for key in ("BLENDER_USER_CONFIG", "BLENDER_USER_SCRIPTS", "BLENDER_USER_DATAFILES", "BLENDER_USER_EXTENSIONS", "TEMP", "TMP"):
        folder = tmp_path / key
        folder.mkdir()
        env[key] = str(folder)
    result = subprocess.run([str(blender), "--background", "--factory-startup", "--offline-mode", "--python-exit-code", "1", "--python", str(script)], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((tmp_path / "proof.json").read_text(encoding="utf-8"))["restored"]["restore_saved"]


def test_chat_checkpoint_links_session_without_rewriting_explicit_reference(monkeypatch):
    import asyncio
    from src.blender_scene.tool import BlenderSceneTool
    from src.blender_live import service

    calls = []
    async def execute(action, arguments):
        calls.append(arguments)
        return {"live": {}}
    monkeypatch.setattr(service, "execute_live", execute)
    tool = BlenderSceneTool()
    original = {"operation": "checkpoint_save", "arguments": {"label": "Before sculpt"}}
    asyncio.run(tool._live("live_submit", original, {"session_id": "local-chat", "turn_id": "turn-2"}))
    assert calls[0]["arguments"]["conversation_id"] == "local-chat"
    assert calls[0]["arguments"]["turn_id"] == "turn-2"
    assert original["arguments"] == {"label": "Before sculpt"}
    asyncio.run(tool._live("live_submit", {"operation": "checkpoint_restore", "arguments": {"conversation_id": "saved-chat"}}, {"session_id": "current-chat"}))
    assert calls[1]["arguments"]["conversation_id"] == "saved-chat"
