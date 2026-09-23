"""
brain_server.py

MCP server exposing the second brain: search the markdown vault and typed
entities, read and write notes, look up an entity's profile and its
neighbourhood in the note graph, and read the cross-entity timeline.

Same two constraints as `context_engine_server.py`, for the same reasons:

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported.
* **the vault is scoped by owner.** `ODYSSEUS_MCP_BRAIN_OWNER` says whose
  vault this server may touch. Without it, reads degrade to install-wide
  (a single-user install's normal state), but a write is refused by name:
  a note written into the wrong owner's vault is a file nobody there wrote,
  sitting in their own notes as if they had.
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("brain")

_engine: dict = {}
_initialized = False

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_BRAIN_OWNER", "ODYSSEUS_BRAIN_OWNER")
_OWNER_SCOPE_ERROR = (
    "Error: the brain MCP server has no owner configured, so it cannot tell "
    "whose vault this is. Set ODYSSEUS_MCP_BRAIN_OWNER for this server. A "
    "write without one would land in nobody's vault in particular."
)

#: Tools whose actions write. Reads degrade to install-wide without a
#: configured owner; every write here is refused instead, same policy as
#: the context engine and memory servers.
_WRITE_ACTIONS = {"brain_write_note", "brain_append_note", "brain_sync"}


def _configured_owner() -> str:
    for key in _OWNER_ENV_KEYS:
        owner = os.environ.get(key, "").strip()
        if owner:
            return owner
    return ""


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload) -> list[TextContent]:
    import json

    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False,
                                   default=str))


def _ensure_init() -> None:
    """Import the brain modules on first use, not at server start — a server
    that is only ever asked to list its tools should not pay for it."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    from src.brain import db, entities, notes, temporal, vault
    from src.brain import frontmatter as fm
    from src.brain import render

    _engine.update(db=db, entities=entities, notes=notes, temporal=temporal,
                   vault=vault, fm=fm, render=render)


def _limit(value, default: int, ceiling: int) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(1, min(wanted, ceiling))


def _resolve_entity_id(owner: str, args: dict) -> str:
    """`entity_id` if given; otherwise the best `list_entities(q=name)` hit.

    Raises `LookupError` when a `name` matches nothing, so the caller sees a
    named failure instead of a profile for id `""`."""
    entity_id = str(args.get("entity_id") or "").strip()
    if entity_id:
        return entity_id
    name = str(args.get("name") or "").strip()
    if not name:
        raise LookupError("give either entity_id or name")
    hits = _engine["entities"].list_entities(owner, q=name, limit=1)
    if not hits:
        raise LookupError(f"no entity matches {name!r}")
    return hits[0]["id"]


# ── the tool surface ───────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="brain_search",
            description=(
                "Search the markdown vault's free notes and typed entities by "
                "text. Busca en tus notas y entidades / search your notes and "
                "what you know about people, places and things. Use it before "
                "asking the user something the vault may already answer, or "
                "before creating a note that might duplicate one that exists. "
                "Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Free text to search for."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="brain_read_note",
            description=(
                "Read one note by its vault path, with its frontmatter, tags, "
                "links and backlinks. Lee una nota del segundo cerebro / read "
                "a note from the second brain. Use it after `brain_search` "
                "finds the path you want in full. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                              "description": "Vault-relative path, e.g. "
                                             "'Notes/Coffee ideas.md'."},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="brain_write_note",
            description=(
                "Create a new free note, or replace the editable part of an "
                "existing one. Apunta algo en mis notas / note this in my "
                "brain, write it to my wiki. Give `path` to edit a note that "
                "exists (a mirrored note's generated section is preserved; "
                "only its user-editable zone is replaced); omit `path` and "
                "give `title` to create a new one under `folder` (default "
                "'Notes'). Refused without a configured owner."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                              "description": "Existing note to edit. Omit to create."},
                    "title": {"type": "string",
                              "description": "Required when creating a new note."},
                    "folder": {"type": "string",
                               "description": "Folder for a new note (default 'Notes')."},
                    "content": {"type": "string",
                                "description": "The note's text (its editable zone)."},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="brain_append_note",
            description=(
                "Append text to the end of an existing note's editable zone "
                "without replacing what is already there. Añade esto a mi "
                "nota / add this to my notes, keep a running log in my "
                "brain. Use it for a running log or journal-style note "
                "instead of read-modify-writing the whole thing yourself. "
                "Refused without a configured owner."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "The note to append to."},
                    "content": {"type": "string", "description": "Text to add."},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="brain_entity",
            description=(
                "The full profile of a person, place, tool, project or other "
                "tracked thing: its current facts, relations and timeline. "
                "Qué sé de <persona> / what do I know about <person>. Give "
                "either `entity_id` or `name` (a fuzzy match on name/aliases). "
                "Pass `as_of` (an ISO date) to see the profile as it stood "
                "then, not now. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "name": {"type": "string",
                              "description": "Used when entity_id is not known."},
                    "as_of": {"type": "string",
                              "description": "ISO date/time; omit for the current profile."},
                },
            },
        ),
        Tool(
            name="brain_timeline",
            description=(
                "What happened and when: one entity's dated facts and "
                "relations if `entity_id` or `name` is given, otherwise a "
                "cross-entity timeline of recent memories and relation "
                "changes. Mi linea de tiempo / when did I say that, what "
                "changed recently. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "name": {"type": "string"},
                    "query": {"type": "string",
                              "description": "Filter text for the cross-entity view."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
            },
        ),
        Tool(
            name="brain_graph_neighbors",
            description=(
                "The local note or entity graph around one node: what links "
                "to it and what it links to, a few steps out. El mapa de mis "
                "notas / how is this connected to what else I know. Give "
                "`path` for a note-graph neighbourhood or leave it empty for "
                "the whole entity graph. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                              "description": "Note path to center on. Omit for "
                                             "the full entity graph."},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 4},
                    "scope": {"type": "string", "enum": ["notes", "entities"],
                              "description": "Which graph. Default 'notes' when "
                                             "'path' is given, else 'entities'."},
                },
            },
        ),
        Tool(
            name="brain_sync",
            description=(
                "Sync the markdown vault now: import any file a person edited "
                "by hand, re-render the ones the brain owns, and reindex "
                "search. Sincroniza mi wiki / re-sync my notes. Use it right "
                "after telling the user you wrote or changed a note, so the "
                "vault on disk reflects it immediately instead of waiting for "
                "the next background pass. Refused without a configured owner."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ── the handlers ───────────────────────────────────────────────────────────

def _tool_search(owner: str, args: dict) -> list[TextContent]:
    notes, entities = _engine["notes"], _engine["entities"]
    query = str(args.get("query") or "")
    limit = _limit(args.get("limit"), 20, 50)
    note_hits = notes.search(owner, query, limit=limit)
    entity_hits = entities.list_entities(owner, q=query, limit=limit)
    return _json_result({
        "ok": True,
        "notes": note_hits,
        "entities": [
            {"id": e["id"], "name": e["name"], "type": e["type"],
             "summary": e.get("summary", "")}
            for e in entity_hits
        ],
    })


def _tool_read_note(owner: str, args: dict) -> list[TextContent]:
    note = _engine["notes"].read_note(owner, str(args.get("path") or ""))
    return _json_result({"ok": True, "note": note})


def _compose_edit(owner: str, path: str, content: str) -> str:
    """The text `notes.write_note` should receive for an edit: a mirrored
    note keeps its generated section and frontmatter, a free note keeps its
    frontmatter and gets a new body. Raises `FileNotFoundError` via
    `read_note` when `path` does not exist."""
    notes, render = _engine["notes"], _engine["render"]
    existing = notes.read_note(owner, path)
    fields = dict(existing["frontmatter"])
    fields["updated"] = _engine["db"].now_iso()
    if existing.get("source"):
        return render.compose(fields, content, existing.get("generated", ""))
    body = "\n" + str(content or "").strip("\n") + "\n"
    return _engine["fm"].join(fields, body)


def _tool_write_note(owner: str, args: dict) -> list[TextContent]:
    notes = _engine["notes"]
    path = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if path:
        composed = _compose_edit(owner, path, content)
        result = notes.write_note(owner, path, composed)
        return _json_result({"ok": True, "action": "edited", **result})
    title = str(args.get("title") or "").strip()
    if not title:
        return _text_result("brain_write_note: give 'path' to edit an "
                            "existing note, or 'title' to create one")
    note = notes.create_note(owner, title,
                             folder=str(args.get("folder") or "Notes"),
                             content=content)
    return _json_result({"ok": True, "action": "created", "note": note})


def _tool_append_note(owner: str, args: dict) -> list[TextContent]:
    notes = _engine["notes"]
    path = str(args.get("path") or "")
    existing = notes.read_note(owner, path)
    addition = str(args.get("content") or "")
    merged = (existing["user_zone"] + "\n\n" + addition).strip("\n") \
        if existing["user_zone"] else addition
    composed = _compose_edit(owner, path, merged)
    result = notes.write_note(owner, path, composed)
    return _json_result({"ok": True, **result})


def _tool_entity(owner: str, args: dict) -> list[TextContent]:
    entities = _engine["entities"]
    try:
        entity_id = _resolve_entity_id(owner, args)
    except LookupError as exc:
        return _text_result(f"brain_entity: {exc}")
    profile = entities.profile(entity_id, as_of=args.get("as_of") or None)
    return _json_result({"ok": True, "profile": profile})


def _tool_timeline(owner: str, args: dict) -> list[TextContent]:
    entities, temporal = _engine["entities"], _engine["temporal"]
    if args.get("entity_id") or args.get("name"):
        try:
            entity_id = _resolve_entity_id(owner, args)
        except LookupError as exc:
            return _text_result(f"brain_timeline: {exc}")
        profile = entities.profile(entity_id)
        return _json_result({"ok": True, "entity_id": entity_id,
                             "timeline": profile.get("timeline", [])})
    events = temporal.timeline(owner, query=str(args.get("query") or ""),
                               limit=_limit(args.get("limit"), 200, 500))
    return _json_result({"ok": True, "timeline": events})


def _tool_neighbors(owner: str, args: dict) -> list[TextContent]:
    notes, entities = _engine["notes"], _engine["entities"]
    path = str(args.get("path") or "").strip()
    scope = str(args.get("scope") or ("notes" if path else "entities"))
    depth = _limit(args.get("depth"), 2, 4)
    if scope == "entities":
        graph = entities.graph(owner)
    else:
        graph = notes.graph(owner, center=path or None, depth=depth)
    return _json_result({"ok": True, "scope": scope, "graph": graph})


def _tool_sync(owner: str, args: dict) -> list[TextContent]:
    report = _engine["vault"].sync(owner, budget_s=25.0)
    return _json_result({"ok": True, "report": report})


_HANDLERS = {
    "brain_search": _tool_search,
    "brain_read_note": _tool_read_note,
    "brain_write_note": _tool_write_note,
    "brain_append_note": _tool_append_note,
    "brain_entity": _tool_entity,
    "brain_timeline": _tool_timeline,
    "brain_graph_neighbors": _tool_neighbors,
    "brain_sync": _tool_sync,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise — an exception here would kill every other
    tool on this server, not just the one call that failed."""
    if name not in _HANDLERS:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}

    owner = _configured_owner()
    if not owner and name in _WRITE_ACTIONS:
        return _text_result(f"{_OWNER_SCOPE_ERROR}\n(refused: {name})")

    try:
        _ensure_init()
    except Exception as exc:  # noqa: BLE001 - an unimportable engine is a message
        return _text_result(f"Error: the brain could not be loaded: {exc}")

    try:
        return _HANDLERS[name](owner, args)
    except FileNotFoundError as exc:
        return _text_result(f"{name}: no such note: {exc}")
    except ValueError as exc:
        return _text_result(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
