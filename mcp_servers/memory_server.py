"""
memory_server.py

MCP server exposing memory management (list, add, edit, delete, search).
Imports MemoryManager and MemoryVectorStore from the Faustus codebase.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# stdout belongs to the JSON-RPC stream: one print() from the app code this
# server imports would corrupt it and kill the session. The guard
# (src/stdio_guard.py) sends stdout writes to stderr while the session runs.
try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

from src.memory import MemoryStoreUnreadable
from src import memory_leases

server = Server("memory")

# Late-initialized managers (set during first tool call)
_memory_manager = None
_memory_vector = None
_initialized = False

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_MEMORY_OWNER", "ODYSSEUS_MEMORY_OWNER")
_OWNER_SCOPE_ERROR = (
    "Error: Memory MCP owner is not configured for an owner-scoped memory store. "
    "Set ODYSSEUS_MCP_MEMORY_OWNER for this server or use the owner-aware native memory tool."
)
_UNREADABLE_STORE_ERROR = (
    "Error: Memory store is temporarily unreadable — nothing was saved. "
    "Repair or restore memory.json, then retry."
)


def _configured_owner() -> str | None:
    for key in _OWNER_ENV_KEYS:
        owner = os.environ.get(key, "").strip()
        if owner:
            return owner
    return None


def _entry_owner(entry: dict) -> str | None:
    owner = entry.get("owner")
    if owner is None:
        return None
    owner_text = str(owner).strip()
    return owner_text or None


def _owner_scoped_store(entries: list[dict]) -> bool:
    return any(_entry_owner(entry) for entry in entries if isinstance(entry, dict))


def _scope_entries(for_update: bool = False) -> tuple[str | None, list[dict], list[dict], str | None]:
    """Return configured owner, all entries, visible entries, and optional error.

    ``for_update=True`` is for read-modify-write callers. They save the ``all
    entries`` list back, so an unreadable store must be reported as an error
    instead of degrading to ``[]`` — otherwise the save writes their one new
    entry over the whole store (issue #5673).
    """
    if for_update:
        try:
            entries = _memory_manager.load_all_for_update()
        except MemoryStoreUnreadable as e:
            return None, [], [], f"{_UNREADABLE_STORE_ERROR} ({e})"
    else:
        entries = _memory_manager.load_all()
    owner = _configured_owner()
    if owner is None and _owner_scoped_store(entries):
        return None, entries, [], _OWNER_SCOPE_ERROR
    if owner is None:
        visible = [
            entry for entry in entries
            if isinstance(entry, dict) and _entry_owner(entry) is None
        ]
    else:
        visible = [
            entry for entry in entries
            if isinstance(entry, dict) and _entry_owner(entry) == owner
        ]
    return owner, entries, visible, None


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _ensure_init():
    """Lazy-init memory managers on first use."""
    global _memory_manager, _memory_vector, _initialized
    if _initialized:
        return
    _initialized = True

    from src.constants import DATA_DIR
    from src.memory import MemoryManager
    _memory_manager = MemoryManager(DATA_DIR)

    try:
        from src.memory_vector import MemoryVectorStore
        _memory_vector = MemoryVectorStore(DATA_DIR)
        if not _memory_vector.healthy:
            _memory_vector = None
    except Exception:
        _memory_vector = None


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="manage_memory",
            description="Manage the user's memory system: list, add, edit, delete, or search memories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "add", "edit", "delete", "search"],
                        "description": "The action to perform",
                    },
                    "text": {"type": "string", "description": "Memory text (add/edit) or search query (search)"},
                    "memory_id": {"type": "string", "description": "Memory ID (edit/delete)"},
                    "category": {
                        "type": "string",
                        "enum": ["fact", "event", "contact", "preference"],
                        "description": "Memory category (add/list filter)",
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(memory_leases.KINDS),
                        "description": "Optional (add): what the fact is about, which sets its lease. Left out, it is read from the text.",
                    },
                    "lease_days": {
                        "type": "integer",
                        "description": "Optional (add): days the fact stays current before it is marked as possibly outdated; 0 = no lease.",
                    },
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="memory_leases",
            description=(
                "Facts with a lease: a fact about a moment (where they live, who they work for, what they are "
                "doing) is only current for a while. review lists the facts whose lease expired or is about to "
                "expire, and the ones a newer fact replaced; renew confirms a fact is still true (restarts its "
                "lease); forget deletes it. An expired fact is still shown to the model, marked as possibly outdated."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["review", "renew", "forget"], "description": "What to do"},
                    "memory_id": {"type": "string", "description": "Memory ID or its first 8 characters (renew/forget)"},
                    "days": {"type": "integer", "description": "renew: new lease length in days (default: the one for its kind)"},
                    "window_days": {"type": "integer", "description": "review: how many days ahead count as about to expire"},
                },
                "required": ["action"],
            },
        ),
    ]


def _lease_line(row: dict) -> str:
    left = row.get("days_left")
    when = ""
    if row.get("state") == "expired" and left is not None:
        when = f", expired {abs(left):.0f} days ago"
    elif row.get("state") == "expiring" and left is not None:
        when = f", expires in {max(left, 0):.0f} days"
    elif row.get("state") == "closed":
        when = ", replaced by a newer fact"
    return f"- [{row.get('kind') or '-'}] `{str(row.get('id') or '?')[:8]}` {row.get('text', '')}{when}"


def _call_memory_leases(arguments: dict) -> list[TextContent]:
    action = arguments.get("action", "")
    if action == "review":
        _owner, _all, visible, scope_error = _scope_entries()
        if scope_error:
            return _text_result(scope_error)
        window = arguments.get("window_days")
        report = memory_leases.review(visible, window=int(window) if window is not None else None)
        counts = report["counts"]
        if not any(counts.values()):
            return _text_result("No memory leases to review.")
        lines = [f"Lease review: {counts['expired']} expired, {counts['expiring']} expiring, {counts['closed']} replaced."]
        for title, key in (("Expired", "expired"), ("Expiring", "expiring"), ("Replaced by newer facts", "closed")):
            if report[key]:
                lines.append(f"\n{title}:")
                lines.extend(_lease_line(row) for row in report[key])
        return _text_result("\n".join(lines))

    if action in ("renew", "forget"):
        memory_id = arguments.get("memory_id", "")
        if not memory_id:
            return _text_result(f"Error: {action} needs memory_id")
        owner, memories, visible, scope_error = _scope_entries(for_update=True)
        if scope_error:
            return _text_result(scope_error)
        target = next((m for m in visible if str(m.get("id", "")).startswith(memory_id)), None)
        if not target:
            return _text_result(f"Error: Memory '{memory_id}' not found")
        if action == "forget":
            memories = [m for m in memories if m.get("id") != target["id"]]
            _memory_manager.save(memories)
            if _memory_vector and _memory_vector.healthy:
                try:
                    _memory_vector.remove(target["id"])
                except Exception:
                    pass
            return _text_result(f"Memory forgotten: {target.get('text', '')} (id: {str(target['id'])[:8]})")
        try:
            memory_leases.renew(target, arguments.get("days"))
        except ValueError as exc:
            return _text_result(f"Error: {exc}")
        _memory_manager.save(memories)
        info = memory_leases.state(target)
        return _text_result(
            f"Lease renewed: {target.get('text', '')} (id: {str(target['id'])[:8]}, valid {info['days_left']:.0f} more days)"
        )

    return _text_result(f"Error: Unknown action '{action}'. Use: review, renew, forget")


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in ("manage_memory", "memory_leases"):
        return _text_result(f"Unknown tool: {name}")

    _ensure_init()
    if not _memory_manager:
        return _text_result("Error: Memory manager not available")
    if name == "memory_leases":
        return _call_memory_leases(arguments)

    action = arguments.get("action", "")

    if action == "list":
        category_filter = arguments.get("category", "")
        _owner, _all_memories, memories, scope_error = _scope_entries()
        if scope_error:
            return _text_result(scope_error)
        memories = memory_leases.prompt_view(memories)
        if category_filter:
            memories = [m for m in memories if m.get("category", "").lower() == category_filter.lower()]
        if not memories:
            msg = "No memories found"
            if category_filter:
                msg += f" in category '{category_filter}'"
            return _text_result(msg + ".")

        lines = [f"Found {len(memories)} memory entries:\n"]
        for m in memories:
            cat = m.get("category", "fact")
            mid = m.get("id", "?")[:8]
            text = m.get("text", "")
            if len(text) > 150:
                text = text[:150] + "..."
            lines.append(f"- [{cat}] `{mid}` — {text}")
        return _text_result("\n".join(lines))

    elif action == "add":
        text = arguments.get("text", "")
        category = arguments.get("category", "fact")
        if not text:
            return _text_result("Error: Memory text cannot be empty")
        owner, memories, _visible, scope_error = _scope_entries(for_update=True)
        if scope_error:
            return _text_result(scope_error)
        try:
            entry = _memory_manager.add_entry(
                text, source="ai_agent", category=category, owner=owner,
                kind=arguments.get("kind") or None, lease_days=arguments.get("lease_days"),
            )
        except ValueError as exc:
            return _text_result(f"Error: {exc}")
        memories.append(entry)
        _memory_manager.save(memories)
        if _memory_vector and _memory_vector.healthy:
            try:
                _memory_vector.add(entry["id"], text)
            except Exception:
                pass
        lease_note = ""
        if entry.get("lease_until"):
            lease_note = f" [{entry.get('kind')}, lease {entry.get('lease_days')} days]"
        return _text_result(f"Memory added: [{category}] {text} (id: {entry['id'][:8]}){lease_note}")

    elif action == "edit":
        memory_id = arguments.get("memory_id", "")
        new_text = arguments.get("text", "")
        if not memory_id or not new_text:
            return _text_result("Error: edit needs memory_id and text")
        _owner, memories, visible, scope_error = _scope_entries()
        if scope_error:
            return _text_result(scope_error)
        full_id = None
        for m in visible:
            if m.get("id", "").startswith(memory_id):
                full_id = m["id"]
                break
        if not full_id:
            return _text_result(f"Error: Memory '{memory_id}' not found")
        for m in memories:
            if m.get("id") == full_id:
                m["text"] = new_text
                m["timestamp"] = int(time.time())
                memory_leases.reassign(m)
                break
        _memory_manager.save(memories)
        if _memory_vector and _memory_vector.healthy and full_id:
            try:
                _memory_vector.remove(full_id)
                _memory_vector.add(full_id, new_text)
            except Exception:
                pass
        return _text_result(f"Memory updated: {new_text}")

    elif action == "delete":
        memory_id = arguments.get("memory_id", "")
        if not memory_id:
            return _text_result("Error: delete needs memory_id")
        _owner, memories, visible, scope_error = _scope_entries()
        if scope_error:
            return _text_result(scope_error)
        full_id = None
        deleted_text = ""
        deleted_category = ""
        for m in visible:
            if m.get("id", "").startswith(memory_id):
                full_id = m["id"]
                deleted_text = m.get("text", "")
                deleted_category = m.get("category", "")
                break
        if not full_id:
            return _text_result(f"Error: Memory '{memory_id}' not found")
        memories = [m for m in memories if m.get("id") != full_id]
        _memory_manager.save(memories)
        if _memory_vector and _memory_vector.healthy and full_id:
            try:
                _memory_vector.remove(full_id)
            except Exception:
                pass
        cat = f"[{deleted_category}] " if deleted_category else ""
        snippet = deleted_text if len(deleted_text) <= 120 else deleted_text[:117] + "..."
        return _text_result(f"Memory deleted: {cat}{snippet} (id: {memory_id})")

    elif action == "search":
        query = arguments.get("text", "")
        if not query:
            return _text_result("Error: search needs text (query)")
        _owner, _all_memories, memories, scope_error = _scope_entries()
        if scope_error:
            return _text_result(scope_error)
        memories = memory_leases.prompt_view(memories)
        if hasattr(_memory_manager, 'get_relevant_memories'):
            results = _memory_manager.get_relevant_memories(query, memories, threshold=0.05, max_items=20)
        else:
            query_lower = query.lower()
            results = [m for m in memories if query_lower in m.get("text", "").lower()][:20]
        if not results:
            return _text_result(f"No memories found matching '{query}'.")
        lines = [f"Found {len(results)} matching memories:\n"]
        for m in results:
            cat = m.get("category", "fact")
            mid = m.get("id", "?")[:8]
            text = m.get("text", "")
            lines.append(f"- [{cat}] `{mid}` — {text}")
        return _text_result("\n".join(lines))

    else:
        return _text_result(f"Error: Unknown action '{action}'. Use: list, add, edit, delete, search")


async def run():
    # The guard goes up INSIDE stdio_server(): that context manager wraps the
    # real sys.stdout.buffer when it is entered, so the protocol keeps the
    # handle and everything else is diverted to stderr.
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
