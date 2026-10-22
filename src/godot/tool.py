"""Native scene files and debug processes, shared by chat, HTTP and MCP."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

from filelock import FileLock
from src.unity.project import profile

OPERATIONS = ("create_scene", "inspect_scene", "add_node", "set_properties", "attach_script", "class_properties")
DESCRIPTION = ("Work on a local Godot4 project using its installed engine: create/read saved scenes, add nodes, "
    "edit typed properties and attach existing project scripts. Saves to disk, not a live editor's undo stack. "
    "Query class properties before editing. Runs are explicit local debug processes; read/stop their exact handles. "
    "Scene operations return a durable job_id; poll job after pending or a lost response, never replay the write. "
    "No hosted API or model. Schema/configuration/discovery do not start a project.")
SCHEMA = {"type":"object", "properties": {
    "action":{"type":"string", "enum":["schema", "configure", "profile", "version", "operation", "job", "run", "debug", "stop"]},
    "project":{"type":"string"}, "executable":{"type":"string"},
    "operation":{"type":"string", "enum":list(OPERATIONS)}, "arguments":{"type":"object"},
    "job_id":{"type":"string", "description":"Reuse for a timed-out scene request; an existing ID is read, not dispatched again"},
    "handle":{"type":"string"}, "session_id":{"type":"string"},
    "scene":{"type":"string"}, "headless":{"type":"boolean", "default":True},
    "frames":{"type":"integer", "minimum":0, "description":"Optional explicit quit-after frames; zero leaves it running"},
    "cursor":{"type":"integer"}, "wait_ms":{"type":"integer"}
}, "required":["action"], "additionalProperties":False}
HELP = {
    "create_scene":"arguments:{scene:'res://Stage.tscn',class:'Node2D',name:'Stage',properties:{...}}. New file only.",
    "inspect_scene":"arguments:{scene:'res://Stage.tscn',fields:['position','modulate']} returns saved hierarchy, typed storage fields and SHA256.",
    "add_node":"scene,node:parent path relative to root ('.'),class,name,properties. Correct scene ownership is saved.",
    "set_properties":"scene,node,properties:{name:value}; optional expected_sha256 from prior read. Whole write commits only on success.",
    "attach_script":"scene,node,script:'res://existing.gd'. Script must parse. Existing script is not rewritten.",
    "class_properties":"arguments:{class:'Sprite2D'} uses the installed ClassDB.",
    "types":"Scalars and {$type:'Vector2|Vector3|Vector4|Color',value:[...]}, NodePath/StringName(value), Resource(path:'res://...'). Query types first.",
    "run":"project and optional scene. Returns normal process handle; debug/stop reuse it. Existing process-manager policy applies.",
    "files":"Only saved scene resources; external editor buffers and editor undo are independent. Reload changed files in an open editor. UID preserved when editing.",
}


def atomic_write_json(path, value):
    from core.atomic_io import atomic_write_json as write
    write(path, value)


def _store():
    from src.constants import DATA_DIR
    path = Path(DATA_DIR) / "godot_operations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _engine():
    from src.settings import get_setting
    value = os.environ.get("FAUSTUS_GODOT") or get_setting("godot_executable", "") or shutil.which("godot") or shutil.which("godot4")
    if not value or not Path(value).is_file():
        raise ValueError("Configure the installed Godot4 executable or FAUSTUS_GODOT first")
    return str(Path(value).resolve())


def _project(value):
    root = Path(value).expanduser().resolve()
    if not (root / "project.godot").is_file():
        raise ValueError("Choose a saved Godot project directory (project.godot)")
    if not re.search(r"^config_version\s*=\s*5\s*$", (root / "project.godot").read_text(encoding="utf-8-sig"), re.M):
        raise ValueError("Use a Godot4 project; this tool does not migrate Godot3 projects")
    return root


def _resource(root, value):
    if not isinstance(value, str) or not value.startswith("res://"):
        raise ValueError("A project res:// resource path is required")
    path = (root / value[6:]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Resource path leaves the project")
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _caller(args, ctx, root=None):
    from src.process_manager import Caller
    ctx = dict(ctx or {})
    if args.get("session_id"):
        if ctx.get("session_id") and args["session_id"] != ctx["session_id"]:
            raise ValueError("Use the current conversation's process scope")
        ctx["session_id"] = args["session_id"]
    if not ctx.get("session_id") and root is not None:
        ctx["session_id"] = "godot:" + str(root)
    return Caller.from_ctx(ctx)


def _check_resources(root, value):
    if isinstance(value, dict):
        if "$type" in value and not isinstance(value["$type"], str):
            raise ValueError("A tagged property type must be a string")
        if value.get("$type") == "Resource":
            _resource(root, value.get("path"))
        for child in value.values(): _check_resources(root, child)
    elif isinstance(value, list):
        for child in value: _check_resources(root, child)


def job(identity, caller):
    from src.process_manager import manager
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Invalid scene job ID")
    folder = _store() / identity
    with FileLock(str(folder) + ".lock"):
        meta = json.loads((folder / "job.json").read_text(encoding="utf-8"))
        if meta["owner"] != caller.owner or meta["session_id"] != caller.session_id:
            raise ValueError("Scene job belongs to another process scope")
        if "result" in meta:
            return meta["result"]
        status = manager().read(meta["handle"], caller, wait_ms=0) if meta.get("handle") else {"state":"dispatching"}
        response = folder / "result.json"
        if not response.is_file():
            if status.get("completed") or status.get("state") in {"exited", "stopped", "timed_out", "lost", "uncertain", "failed_to_start"}:
                result = {"ok":False, "state":"failed", "error":"Engine did not retain a successful operation result", "process":status}
            else:
                return {"ok":True, "state":"pending", "job_id":identity, "handle":meta["handle"], "process":status}
        else:
            result = json.loads(response.read_text(encoding="utf-8"))
            if result.get("ok") and meta.get("target"):
                target, staged = Path(meta["target"]), Path(meta["staged"])
                expected = meta["before_sha256"]
                with FileLock(str(_store() / (hashlib.sha256(str(target).encode()).hexdigest() + ".scene.lock"))):
                    current = _digest(target)
                    written = _digest(staged) or meta.get("staged_sha256")
                    if written and meta.get("staged_sha256") != written:
                        meta["staged_sha256"] = written
                        atomic_write_json(str(folder / "job.json"), meta)
                    if staged.is_file() and written and current == expected:
                        os.replace(staged, target)
                    elif current != written or current is None:
                        result = {"ok":False, "error":"Scene changed on disk before commit; no overwrite performed", "code":"scene_changed", "expected_sha256":expected, "current_sha256":current}
                    if result.get("ok"):
                        result["sha256"] = _digest(target)
            elif result.get("ok") and meta.get("source"):
                if _digest(Path(meta["source"])) != meta["before_sha256"]:
                    result = {"ok":False, "error":"Scene changed during inspection; inspect it again", "code":"scene_changed"}
                else:
                    result["sha256"] = meta["before_sha256"]
            result["state"] = "complete" if result.get("ok") else "failed"
        result["job_id"] = identity
        meta["result"] = result
        atomic_write_json(str(folder / "job.json"), meta)
        if meta.get("staged"):
            Path(meta["staged"]).unlink(missing_ok=True)
        return result


def _watch(identity, caller):
    from src.process_manager import manager
    folder = _store() / identity
    meta = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    while True:
        result = job(identity, caller)
        if result.get("state") != "pending": return
        manager().read(meta["handle"], caller, wait_ms=1000)


def execute(args, ctx=None):
    if not isinstance(args, dict): raise ValueError("JSON object required")
    action = args.get("action", "schema")
    if action == "schema": return {"ok":True, "operations":list(OPERATIONS), "arguments":HELP}
    from src.process_manager import manager
    if action == "configure":
        value = args.get("executable", "")
        if not isinstance(value, str) or not Path(value).expanduser().is_file(): raise ValueError("Installed executable not found")
        from src.settings import update_settings
        value = str(Path(value).expanduser().resolve())
        update_settings({"godot_executable":value})
        return {"ok":True, "executable":value, "started":False}
    root = _project(args["project"])
    caller = _caller(args, ctx, root)
    if action == "profile":
        try: executable = _engine()
        except ValueError: executable = None
        return {**profile(str(root)), "executable":executable}
    if action == "job": return job(args["job_id"], caller)
    if action in {"debug", "stop"}:
        if action == "debug":
            return manager().read(args["handle"], caller, cursor=args.get("cursor"), wait_ms=args.get("wait_ms", 0))
        return manager().stop(args["handle"], caller, reason="Explicit Godot debug stop")
    executable = _engine()
    if action == "version":
        return manager().start(argv=[executable,"--version"], caller=caller, cwd=str(root), yield_ms=1000)
    if action == "run":
        argv = [executable, "--path", str(root), "--audio-driver", "Dummy"]
        if args.get("headless", True): argv.append("--headless")
        if args.get("scene"):
            path = _resource(root, args["scene"])
            if not path.is_file(): raise ValueError("Scene not found")
            argv.append(args["scene"])
        frames = args.get("frames", 0)
        if type(frames) is not int or frames < 0: raise ValueError("frames must be a nonnegative integer")
        if frames: argv += ["--quit-after", str(frames)]
        return manager().start(argv=argv, caller=caller, cwd=str(root), yield_ms=1000)
    if action != "operation" or args.get("operation") not in OPERATIONS:
        raise ValueError("Unknown Godot action or operation")
    return _scene_operation(args, root, caller, executable)


def _scene_operation(args, root, caller, executable):
    identity = args.get("job_id") or uuid.uuid4().hex
    if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Invalid scene job ID")
    with FileLock(str(_store() / (identity + ".dispatch.lock"))):
        return _dispatch(args, root, caller, executable, identity)


def _dispatch(args, root, caller, executable, identity):
    from src.process_manager import manager
    folder = _store() / identity
    if folder.exists(): return job(identity, caller)
    spec = args.get("arguments") or {}
    if not isinstance(spec, dict): raise ValueError("arguments must be an object")
    allowed = {"scene", "node", "class", "name", "properties", "fields", "script", "expected_sha256"}
    if set(spec) - allowed: raise ValueError("Unknown scene argument: " + ", ".join(sorted(set(spec)-allowed)))
    if any(key in spec and not isinstance(spec[key], str) for key in ("scene", "node", "class", "name", "script")):
        raise ValueError("Scene, node, class, name and script fields must be strings")
    if not isinstance(spec.get("properties", {}), dict): raise ValueError("properties must be an object")
    if not isinstance(spec.get("fields", []), list) or any(not isinstance(v, str) for v in spec.get("fields", [])):
        raise ValueError("fields must be a list of property names")
    _check_resources(root, spec)
    if str(spec.get("node", ".")).startswith("/"):
        raise ValueError("Node path must be relative to the saved scene root")
    if args["operation"] == "attach_script": _resource(root, spec.get("script"))
    target = None
    if args["operation"] != "class_properties":
        source = _resource(root, spec.get("scene"))
        if source.suffix != ".tscn": raise ValueError("Choose a .tscn scene; binary scene editing is not supported")
        before = _digest(source)
        if args["operation"] == "create_scene" and source.exists(): raise ValueError("Scene already exists; inspect and edit it")
        if args["operation"] != "create_scene" and before is None: raise ValueError("Scene not found")
        if "expected_sha256" in spec and spec["expected_sha256"] != before: raise ValueError("Scene changed since the supplied SHA256")
        if not source.parent.is_dir(): raise ValueError("Scene's parent directory does not exist")
        if args["operation"] != "inspect_scene": target = source
    else:
        source, before = None, None
    staged = target.with_name(".faustus-" + identity + target.suffix) if target else None
    folder.mkdir()
    request = {"operation":args["operation"], "arguments":spec, "scene":spec.get("scene", ""),
               "staged":str(staged) if staged else "", "result_file":str(folder / "result.json")}
    if source and source.is_file():
        header = source.read_text(encoding="utf-8-sig").splitlines()[0]
        uid = re.search(r'\buid="(uid://[^"]+)"', header)
        request["uid"] = uid.group(1) if uid else ""
    atomic_write_json(str(folder / "request.json"), request)
    meta = {"owner":caller.owner, "session_id":caller.session_id, "handle":None,
            "source":str(source) if source else None, "target":str(target) if target else None,
            "staged":str(staged) if staged else None, "before_sha256":before}
    atomic_write_json(str(folder / "job.json"), meta)
    started = manager().start(argv=[executable,"--headless","--audio-driver","Dummy","--path",str(root),
        "--script",str(Path(__file__).with_name("scene.gd")),"--",str(folder / "request.json")],
        caller=caller, cwd=str(root), yield_ms=1000)
    if not started.get("ok"):
        meta["result"] = {**started, "job_id":identity, "state":"failed"}
        atomic_write_json(str(folder / "job.json"), meta)
        return meta["result"]
    meta["handle"] = started["handle"]
    atomic_write_json(str(folder / "job.json"), meta)
    result = job(identity, caller)
    if result.get("state") == "pending":
        threading.Thread(target=_watch, args=(identity, caller), daemon=True, name="godot-scene-commit").start()
    return result


class GodotTool:
    async def execute(self, content, ctx=None):
        try:
            args = json.loads(content) if isinstance(content, str) else content
            result = await asyncio.to_thread(execute, args, ctx)
            return {"output":json.dumps(result, ensure_ascii=False), "structuredContent":result,
                    "exit_code":0 if result.get("ok") else 1}
        except Exception as exc:
            return {"output":str(exc), "error":str(exc), "exit_code":1}
