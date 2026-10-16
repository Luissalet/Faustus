"""Native persistent working groups through Faustus's normal API."""
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("groups")
S = {"type": "string"}
GROUP = {"group_id": S}
PARTICIPANT = {'type': 'object', 'properties': {
    'route': {'type': 'object'}, 'personaName': S, 'personaPrompt': S, 'personaId': S, 'sessionId': S,
    'autonomyPreset': {'type': 'string', 'enum': ['supervised', 'bounded_autonomous', 'read_only']},
    'inputTokenBudget': {'type': 'integer', 'minimum': 4096, 'maximum': 200000},
}}
SPECS = {
    "group_options": ("GET", "/config", {}, [], True, "Read the configured global model (Sparks), supported working-group modes and native tool execution options."),
    "group_list": ("GET", "", {"space_id": S}, [], True, "List persistent groups of models/agents, optionally in one Agora space. Work continues when the observer disconnects."),
    "group_create": ("POST", "", {"participants": {"type": "array", "items": PARTICIPANT}, "name": S, "workspace": S, "space_id": S,
        "mode": {"type": "string", "enum": ["parallel", "round-robin"]}, "executionMode": {"type": "string", "enum": ["chat", "agent"]}},
        ["participants"], False, "Create a persistent group. Each participant may set a route/model, personaName/personaPrompt, autonomyPreset (read_only for reviewers), and inputTokenBudget. Omitted routes use the global configured model. Agent mode uses native tools; chat mode preserves plain conversation."),
    "group_get": ("GET", "/{group_id}", GROUP, ["group_id"], True, "Read persistent members, ordered turns, partial/completed replies, errors and actual native run IDs."),
    'group_update': ('PATCH', '/{group_id}', {**GROUP, 'name': S, 'participants': {'type': 'array', 'items': PARTICIPANT},
        'mode': {'type': 'string', 'enum': ['parallel', 'round-robin']}, 'executionMode': {'type': 'string', 'enum': ['chat', 'agent']},
        'workspace': S, 'expected_revision': {'type': 'integer'}}, ['group_id'], False,
        'Rename a group or edit its members without replacing the room or remaining member sessions. Removed members take no new turns; current work can finish. Use the observed revision for concurrent edits.'),
    "group_send": ("POST", "/{group_id}/messages", {**GROUP, "message": S, "request_id": S, "mentions": {"type": "array", "items": S}},
        ["group_id", "message"], False, "Queue an instruction without cancelling active participants. A repeated request_id reuses the same turn. Optional mentions select participant names/session IDs."),
    "group_stop": ("POST", "/{group_id}/stop", GROUP, ["group_id"], False, "Explicitly stop this group's queued/current work, preserving transcript and results. Other groups are unaffected."),
    "group_resume": ("POST", "/{group_id}/resume", {**GROUP, "message": S, "answer": {"type": "object"}}, ["group_id"], False,
        "Answer this group's actual pending question and continue the same participant. Optional answer fields carry the existing native question/approval ID and the user's explicit decision."),
    "group_archive": ("POST", "/{group_id}/archive", GROUP, ["group_id"], False, "Close this group explicitly while preserving its canonical transcript in Studio."),
}


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=spec[5], inputSchema={"type": "object", "properties": spec[2], "required": spec[3]},
                 annotations=ToolAnnotations(readOnlyHint=spec[4], destructiveHint=False)) for name, spec in SPECS.items()]


@server.call_tool()
async def call_tool(name, arguments):
    if name not in SPECS:
        return [TextContent(type="text", text=json.dumps({"error": "Unknown group tool", "exit_code": 1}))]
    method, path, *_ = SPECS[name]
    args = dict(arguments or {})
    if "{group_id}" in path:
        from urllib.parse import quote
        path = path.replace("{group_id}", quote(str(args.pop("group_id", "")), safe=""))
    try:
        base = os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()
        headers = {}
        token = os.environ.get("FAUSTUS_TOKEN", "")
        if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        internal = os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN", "")
        if internal:
            headers["X-Odysseus-Internal-Token"] = internal
        owner = os.environ.get("FAUSTUS_GROUPS_OWNER", "")
        if owner:
            headers["X-Faustus-Group-Owner"] = owner
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            response = await client.request(method, base.rstrip("/") + "/api/groups" + path, headers=headers,
                **({"params": args} if method == "GET" else {"json": args}))
        payload = response.json()
        if not response.is_success:
            payload = {"exit_code": 1, "status_code": response.status_code, "error": payload}
    except (httpx.HTTPError, ValueError, OSError) as error:
        payload = {"exit_code": 1, "error": "Group API request failed: " + type(error).__name__}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
