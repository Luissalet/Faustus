"""Shared agent, REST and MCP surface; no editor or model is started by discovery."""
import asyncio
import json

from .client import OPERATIONS, UnityClient
from .project import profile

DESCRIPTION = ("Read and edit the open local Unity editor project through its native bridge. "
    "Inspect scenes, uniquely identify objects, read serialized component properties, edit fields with Unity undo, "
    "create/assign HDRP materials, create/instantiate prefabs, save/load scenes, select objects and control Play mode. "
    "Run real EditMode/PlayMode tests and read retained results. No hosted API or model. "
    "Use schema first. project is the Unity project folder. Pending responses require job, never resubmission. "
    "project_profile detects local Unity, Godot, Unreal or Three.js metadata without starting them.")
SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["schema", "project_profile", "operation", "job", "diagnostics"]},
    "project": {"type": "string", "description": "Unity project folder; the editor writes Logs/bridge_port.txt here"},
    "port": {"type": "integer", "description": "Override the discovered loopback port"},
    "operation": {"type": "string", "enum": list(OPERATIONS)},
    "arguments": {"type": "object"}, "job_id": {"type": "integer"}, "since": {"type": "integer"}
}, "required": ["action"]}
OPERATION_HELP = {
    "object": "object: exact hierarchy path, global_id or object_id from scene. Ambiguous names are errors.",
    "set_properties": "object, component (full type name), optional component_id, properties:{serialized.path:value}. Read object first. Scalars and color/vector arrays, enum name/index, object-reference asset path/object_id/null.",
    "create_object": "name, optional primitive:Cube|Sphere|Capsule|Cylinder|Plane|Quad and parent selector.",
    "add_component": "object, component: full installed component type name.",
    "create_material": "path:Assets/...mat, shader: installed name (HDRP/Lit), properties:{shader property:value}.",
    "modify_material": "path:Assets/...mat, properties:{shader property:value}.",
    "material": "path:Assets/...mat; reads installed shader properties and current values.",
    "assign_material": "object, path:Assets/...mat, slot:0.",
    "create_prefab": "object, path:Assets/...prefab (new asset).",
    "instantiate_prefab": "path:Assets/...prefab.",
    "load_scene": "path:Assets/...unity, additive:true by default. Non-additive needs saved open scenes.",
    "save_scene": "optional path:Assets/...unity, required for an unsaved active scene.",
    "active_scene": "path of a loaded scene.", "unload_scene": "path of a loaded saved scene.",
    "playmode": "action:play|stop|pause|resume|step; requested state is distinct from observed state.",
    "run_tests": "mode:EditMode|PlayMode, optional filter: full test name; returns a persistent test job_id.",
    "tests_job": "job_id: hex ID from run_tests; includes actual pass/fail/skip counts and tests.",
}


def execute(args):
    action = args.get("action", "schema")
    if action == "schema":
        return {"ok": True, "operations": list(OPERATIONS), "arguments": OPERATION_HELP,
                "identity": "Global IDs require saved scenes; object IDs last only for this editor domain.",
                "job": "Pending bridge jobs use numeric IDs; Test Runner jobs use hex IDs with tests_job."}
    if action == "project_profile":
        return profile(args["project"])
    client = UnityClient(args["project"], port=args.get("port"))
    if action == "operation":
        return client.operation(args["operation"], args.get("arguments"))
    if action == "job":
        return client.job(args["job_id"])
    if action == "diagnostics":
        return {"ok": True, **client.diagnostics(args.get("since", 0))}
    raise ValueError("Unknown Unity action: " + str(action))


class UnityTool:
    async def execute(self, content, ctx=None):
        try:
            args = json.loads(content) if isinstance(content, str) else content
            result = await asyncio.to_thread(execute, args)
            return {"output": json.dumps(result, ensure_ascii=False), "exit_code": 0 if result.get("ok") else 1,
                    "structuredContent": result}
        except Exception as exc:
            return {"output": str(exc), "exit_code": 1, "error": str(exc)}
