"""blender_scene_server.py

MCP server for typed 3D scenes rendered headless with Blender
(src/blender_scene). Tools: `blender_probe`, `blender_scene_schema`,
`blender_scene_validate`, `blender_scene_run`, `blender_smoke`.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported, like the other built-in servers.
* A scene is typed JSON, validated before Blender starts; there is no op that
  runs code. `blender_scene_run` and `blender_smoke` write files, but only
  inside this server's own folder under the data directory
  (`BLENDER_SCENES_DIR/mcp[/<folder>]`); paths in a scene cannot leave it.
* No owner-scoped store, so no owner environment variable.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("blender_scene")

_SCENE_PROPS = {
    "scene": {"type": "object", "description": (
        "The scene: {\"meta\": {name, description, stop_on_error}, \"ops\": [{\"op\": \"add_mesh\", ...}, ...]}. "
        "Call blender_scene_schema for every op and field.")},
    "ops": {"type": "array", "items": {"type": "object"},
            "description": "The list of ops, instead of scene.ops."},
    "meta": {"type": "object", "description": "Scene metadata when ops is given directly."},
    "folder": {"type": "string",
               "description": "Sub-folder of the server's project folder to work in; use one per model."},
}


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def live_tools():
    from src.blender_live.client import OPERATIONS
    shared = {"port": {"type": "integer", "description": "Optional local bridge port; normally discovered"}}
    return [
        Tool(name="blender_live_probe", description="Inspect connection to the open Blender scene through our local Faustus add-on. No external API.", inputSchema={"type":"object", "properties":shared}),
        Tool(name="blender_live_submit", description="Run native asset work through the official Blender Lab main-thread server. scene(objects?); execute(code,bpy,result); api(identifier:bpy.types.Name or bpy.ops.category.operator, installed RNA); nodes(owner,name,modifier?,ops?:add/remove/set/link/unlink); animation(object, action slots/curves/NLA/drivers); asset_check(objects?, topology/UV/weights/materials/shape keys, read-only); views(objects?,angles?,frames?,width?,height?,sheet); save/load(path); export(path,format,objects?,actions?:{rig:[clips]}); recipe_save(name,steps:[{name,code}]); recipe_list; recipe_run(name,stages?). Returns persistent job ID; read blender_live_job. Python permits full bpy modeling, UV, rigging, baking and scene control; no hosted asset generator.", inputSchema={"type":"object", "properties":{**shared,"operation":{"type":"string","enum":list(OPERATIONS)},"arguments":{"type":"object"}},"required":["operation"]}),
        Tool(name="blender_live_job", description="Read a live Blender job, frozen recipe, per-stage progress/errors and review contact sheet. Omit job_id to list jobs. Interrupted jobs do not replay automatically.", inputSchema={"type":"object", "properties":{**shared,"job_id":{"type":"string"}}}),
        Tool(name="blender_live_cancel", description="Cancel queued work, or request cancellation between recipe stages/render views. A single Python operation or render finishes before cancellation is checked; never kills the user's Blender.", inputSchema={"type":"object", "properties":{**shared,"job_id":{"type":"string"}},"required":["job_id"]}),
    ]


@server.list_tools()
async def list_tools() -> list[Tool]:
    try:
        from src.blender_live.official import tools as official_tools
        official = [tool.model_copy(update={"name": "blender_official_" + tool.name}) for tool in await official_tools()]
    except RuntimeError:
        official = []
    return [
        Tool(
            name="blender_probe",
            description=(
                "Check whether Blender is installed and which version: looks at the blender_path "
                "setting, BLENDER_PATH, PATH, the Windows install folders (newest first) and the usual "
                "Linux/macOS locations. Returns the path and version, or a clear not-installed message. "
                "Comprueba si Blender está instalado. Read-only."),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="blender_scene_schema",
            description=(
                "The typed scene format: every op (new_scene, load_blend, save, delete, set_world, "
                "add_mesh, set_transform, add_empty, add_material, assign_material, add_modifier, add_light, "
                "add_camera, track_to, add_text, set_render, render, compositor_glare, import_model, "
                "export_model, assert) with its fields, types and ranges. format 'summary' is compact text, "
                "'json_schema' the full JSON Schema. Read-only."),
            inputSchema={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["summary", "json_schema"],
                               "description": "Compact text (default) or the full JSON Schema."},
                    "op": {"type": "string", "description": "Show only this op (summary format)."},
                },
            },
        ),
        Tool(
            name="blender_scene_validate",
            description=(
                "Check a scene without starting Blender: unknown ops or fields, wrong types, out-of-range "
                "values and paths outside the project folder are returned as precise errors (op index, "
                "field, reason). Read-only."),
            inputSchema={"type": "object", "properties": dict(_SCENE_PROPS)},
        ),
        Tool(
            name="blender_scene_run",
            description=(
                "Validate a scene and run it headless in Blender: ops apply in order, a failing op is "
                "reported and the run continues unless meta.stop_on_error. Returns Blender's version, "
                "per-op status and timing, assertion results (observed vs expected), the files written "
                "with their sizes (render PNG/JPEG, .blend, STL/OBJ/FBX/glTF) and an error count. Files "
                "are written only inside this server's project folder. Renderiza una escena 3D."),
            inputSchema={
                "type": "object",
                "properties": {**_SCENE_PROPS,
                               "timeout": {"type": "number", "minimum": 5, "maximum": 3600,
                                           "description": "Seconds Blender may run (default 300)."}},
            },
        ),
        Tool(
            name="blender_smoke",
            description=(
                "Run the built-in smoke scene (ground, a beveled cube with a material, a sphere, a sun, a "
                "camera tracking the cube, a 320x240 render, a saved .blend, assertions) and then re-open "
                "the saved .blend and assert again. Proves Blender, the runner and the folder work."),
            inputSchema={"type": "object", "properties": {
                "engine": {"type": "string", "enum": ["workbench", "eevee", "cycles"],
                           "description": "Render engine (default workbench, works without a GPU)."}}},
        ),
    ] + live_tools() + official


def _context() -> dict:
    # One fixed folder for this server: the session id of a chat is not known here.
    return {"session_id": "mcp", "no_gallery": True}


async def _dispatch(name: str, args: dict) -> dict:
    if name.startswith("blender_live_"):
        from src.blender_live.service import execute_live
        return await execute_live(name.removeprefix("blender_"), args)
    from src.blender_scene.tool import BlenderSceneTool

    tool = BlenderSceneTool()
    payload = dict(args)
    if name == "blender_probe":
        payload = {"action": "probe"}
    elif name == "blender_scene_schema":
        payload["action"] = "schema"
    elif name == "blender_scene_validate":
        payload["action"] = "validate"
    elif name == "blender_scene_run":
        payload["action"] = "run"
    elif name == "blender_smoke":
        payload["action"] = "smoke"
    elif name in {"blender_live_probe", "blender_live_submit", "blender_live_job", "blender_live_cancel"}:
        payload["action"] = name.removeprefix("blender_")
    else:
        raise KeyError(name)
    return await tool.execute(json.dumps(payload), _context())


_KNOWN = {"blender_probe", "blender_scene_schema", "blender_scene_validate", "blender_scene_run", "blender_smoke", "blender_live_probe", "blender_live_submit", "blender_live_job", "blender_live_cancel"}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name.startswith("blender_official_"):
        try:
            from src.blender_live.official import call
            content = await call(name.removeprefix("blender_official_"), arguments or {})
            if isinstance(content, tuple) and content[1].get("status") == "error":
                return CallToolResult(isError=True, content=content[0], structuredContent=content[1])
            return content
        except Exception as exc:
            return CallToolResult(isError=True, content=_text_result(f"Error in {name}: {type(exc).__name__}: {exc}"))
    if name not in _KNOWN:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        result = await _dispatch(name, args)
    except Exception as exc:  # noqa: BLE001
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")
    if result.get("error"):
        return _text_result(str(result["error"]))
    payload = {"ok": not result.get("exit_code"), "output": result.get("output")}
    for key in ("probe", "validation", "run", "reload", "project_root", "schema", "live"):
        if key in result:
            payload[key] = result[key]
    if name == "blender_scene_schema" and "schema" in payload:
        payload.pop("output", None)
    content: list = _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    images = result.get("images") or []
    if images:
        content.append(ImageContent(type="image", data=images[-1]["data"], mimeType=images[-1]["mimeType"]))
    return content


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
