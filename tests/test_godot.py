"""Scene semantics against an actual installed headless engine; no model/GPU."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import pytest

from src.godot import tool


def test_discovery_does_not_require_engine_or_start_project(monkeypatch):
    monkeypatch.setattr(tool, "_engine", lambda: pytest.fail("discovery launched engine"))
    assert tool.execute({"action":"schema"})["ok"]


@pytest.mark.parametrize("value", ["../outside.tscn", "res://../../outside.tscn", "C:/outside.tscn"])
def test_scene_resource_stays_inside_project(tmp_path, value):
    with pytest.raises(ValueError): tool._resource(tmp_path, value)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    binary=os.environ.get("FAUSTUS_GODOT")
    if not binary or not Path(binary).is_file(): pytest.skip("Set FAUSTUS_GODOT to run actual native engine tests")
    from src.process_manager import reset_for_tests
    reset_for_tests()
    project=tmp_path/"project";project.mkdir()
    (project/"project.godot").write_text('config_version=5\n',encoding="utf-8")
    store=tmp_path/"operations";store.mkdir()
    monkeypatch.setattr(tool,"_store",lambda:store)
    monkeypatch.setenv("FAUSTUS_PROCESS_STORE",str(tmp_path/"processes.json"))
    def call(action="operation",ctx=None,**args):
        result=tool.execute({"action":action,"project":str(project),**args},ctx)
        deadline=time.monotonic()+20
        while result.get("state")=="pending" and time.monotonic()<deadline:
            time.sleep(.05)
            result=tool.execute({"action":"job","project":str(project),"job_id":result["job_id"]},ctx)
        assert result.get("state")!="pending",result
        return result
    yield project,store,call
    reset_for_tests()


def test_native_nodes_properties_ownership_uid_and_invalid_batch(engine):
    project,store,call=engine
    scene="res://Main.tscn"
    created=call(operation="create_scene",arguments={"scene":scene,"name":"Main","class":"Node2D","fields":["position"]})
    assert created["ok"],created
    header=(project/"Main.tscn").read_text().splitlines()[0]
    assert "uid=" in header
    added=call(operation="add_node",arguments={"scene":scene,"node":".","name":"Player","class":"Sprite2D",
        "properties":{"position":{"$type":"Vector2","value":[12,34]},"z_index":3},"fields":["position","z_index"]})
    assert added["ok"],added
    assert (project/"Main.tscn").read_text().splitlines()[0]==header
    inspected=call(operation="inspect_scene",arguments={"scene":scene,"fields":["position","z_index"]})
    player=next(node for node in inspected["nodes"] if node["path"]=="Player")
    fields={item["name"]:item["value"] for item in player["properties"]}
    assert fields["position"]["value"]==[12,34] and fields["z_index"]==3
    before=(project/"Main.tscn").read_bytes()
    failed=call(operation="set_properties",arguments={"scene":scene,"node":"Player","properties":{"z_index":7,"missing_property":9}})
    assert not failed["ok"] and (project/"Main.tscn").read_bytes()==before
    failed=call(operation="set_properties",arguments={"scene":scene,"node":"Player","properties":{"position":"not a vector"}})
    assert not failed["ok"] and (project/"Main.tscn").read_bytes()==before
    assert not list(project.glob(".faustus-*"))
    body=call(operation="add_node",arguments={"scene":scene,"name":"Body","class":"CharacterBody2D","fields":["position"]})
    assert body["ok"],body
    velocity=call(operation="set_properties",arguments={"scene":scene,"node":"Body","properties":{"velocity":{"$type":"Vector2","value":[3,4]}},"fields":["velocity"]})
    assert velocity["ok"],velocity
    reopened=call(operation="inspect_scene",arguments={"scene":scene,"node":"Body","fields":["velocity"]})
    body=next(node for node in reopened["nodes"] if node["path"]=="Body")
    assert body["properties"][0]["value"]["value"]==[3,4]
    (project/"Runtime.gd").write_text('extends CharacterBody2D\nvar runtime_value: int = 0\n',encoding="utf-8")
    assert call(operation="attach_script",arguments={"scene":scene,"node":"Body","script":"res://Runtime.gd"})["ok"]
    before=(project/"Main.tscn").read_bytes()
    runtime=call(operation="set_properties",arguments={"scene":scene,"node":"Body","properties":{"runtime_value":7}})
    assert not runtime["ok"] and "not retained" in runtime["error"]
    assert (project/"Main.tscn").read_bytes()==before
    (project/"Incompatible.gd").write_text('extends Node3D\n',encoding="utf-8")
    incompatible=call(operation="attach_script",arguments={"scene":scene,"node":"Player","script":"res://Incompatible.gd"})
    assert not incompatible["ok"] and "incompatible" in incompatible["error"]
    assert (project/"Main.tscn").read_bytes()==before


def test_native_job_receipt_never_replays_and_scope_is_preserved(engine):
    project,store,call=engine
    identity=uuid.uuid4().hex
    created=call(job_id=identity,operation="create_scene",arguments={"scene":"res://Once.tscn","name":"Once","fields":["position"]})
    before=(project/"Once.tscn").read_bytes()
    again=call(job_id=identity,operation="create_scene",arguments={"scene":"res://Different.tscn","name":"Different"})
    assert again==created and not (project/"Different.tscn").exists()
    assert (project/"Once.tscn").read_bytes()==before
    with pytest.raises(ValueError,match="another process scope"):
        call("job",ctx={"owner":"somebody-else"},job_id=identity)
    with pytest.raises(ValueError,match="SHA256"):
        call(operation="set_properties",arguments={"scene":"res://Once.tscn","properties":{"visible":False},"expected_sha256":"stale"})


def test_commit_conflict_and_crash_recovery_preserve_saved_work(tmp_path,monkeypatch):
    store=tmp_path/"operations";store.mkdir();monkeypatch.setattr(tool,"_store",lambda:store)
    from src.process_manager import Caller
    caller=Caller(owner="qa",session_id="qa")
    for changed in (False,True):
        identity=uuid.uuid4().hex;folder=store/identity;folder.mkdir()
        target=tmp_path/(identity+".tscn");target.write_bytes(b"manual-change" if changed else b"new-scene")
        staged=tmp_path/(identity+"-staged.tscn")
        written=hashlib.sha256(b"new-scene").hexdigest()
        meta={"owner":"qa","session_id":"qa","handle":None,"target":str(target),"staged":str(staged),
              "before_sha256":hashlib.sha256(b"old-scene").hexdigest(),"staged_sha256":written}
        (folder/"job.json").write_text(json.dumps(meta));(folder/"result.json").write_text('{"ok":true}')
        result=tool.job(identity,caller)
        assert result["ok"] is not changed
        assert target.read_bytes()==(b"manual-change" if changed else b"new-scene")
        assert tool.job(identity,caller)==result


def test_native_mcp_retains_request_id_when_http_outcome_is_unknown(monkeypatch):
    import asyncio
    import httpx
    from mcp_servers import godot_server
    class Unreachable:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,*args,**kwargs): raise httpx.ReadTimeout("fixture lost response")
    monkeypatch.setattr(godot_server.httpx,"AsyncClient",lambda **kwargs:Unreachable())
    result=asyncio.run(godot_server.call_tool("godot",{"action":"operation","project":"local-project","operation":"create_scene"}))
    assert result.isError and result.structuredContent["outcome_unknown"]
    assert len(result.structuredContent["job_id"])==32


def test_dispatch_preserves_receipt_finalized_by_poll_while_start_yields(tmp_path,monkeypatch):
    store=tmp_path/"operations";store.mkdir();monkeypatch.setattr(tool,"_store",lambda:store)
    from src import process_manager
    from src.process_manager import Caller
    caller=Caller(owner="qa",session_id="qa")
    identity=uuid.uuid4().hex
    target=tmp_path/"New.tscn"
    polls=[]
    class Delayed:
        def start(self,**kwargs):
            folder=store/identity
            Path(json.loads((folder/"job.json").read_text())["staged"]).write_bytes(b"saved scene")
            (folder/"result.json").write_text('{"ok":true}')
            polls.append(tool.job(identity,caller))
            return {"ok":True,"handle":"native-handle"}
        def read(self,*args,**kwargs): return {"state":"exited","completed":True}
    monkeypatch.setattr(process_manager,"manager",lambda:Delayed())
    result=tool._scene_operation({"operation":"create_scene","job_id":identity,"arguments":{"scene":"res://New.tscn"}},tmp_path,caller,"unused")
    assert polls[0]["ok"] and result==polls[0]==tool.job(identity,caller)
    assert target.read_bytes()==b"saved scene" and result["sha256"]==hashlib.sha256(b"saved scene").hexdigest()


def test_native_debug_reports_real_ready_parse_error_and_explicit_stop(engine):
    project,store,call=engine
    scene="res://Debug.tscn"
    assert call(operation="create_scene",arguments={"scene":scene,"name":"Debug","fields":["script"]})["ok"]
    script=project/"Debug.gd"
    script.write_text('extends Node2D\nfunc _ready():\n\tprint("READY_GODOT_NATIVE")\n\tget_tree().quit()\n',encoding="utf-8")
    assert call(operation="attach_script",arguments={"scene":scene,"node":".","script":"res://Debug.gd","fields":["script"]})["ok"]
    running=call("run",scene=scene,frames=30)
    read=call("debug",handle=running["handle"],wait_ms=1000)
    assert "READY_GODOT_NATIVE" in (running.get("output","")+read.get("output",""))
    script.write_text('extends Node2D\nfunc broken(\n',encoding="utf-8")
    broken=call("run",scene=scene,frames=5)
    read=call("debug",handle=broken["handle"],wait_ms=1000)
    assert "Parse Error" in broken.get("output","")+read.get("output","")
    script.write_text('extends Node2D\nfunc _ready():\n\tprint("WAITING_FOR_EXPLICIT_STOP")\n',encoding="utf-8")
    running=call("run",scene=scene)
    assert not running["completed"]
    stopped=call("stop",handle=running["handle"])
    assert stopped["ok"] and stopped["state"]=="stopped"
