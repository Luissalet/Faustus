"""agent_tools/brain_tools.py — the `brain` tool executor.

One tool, one `action` discriminator, over `src.brain.*` — the model's own
hands for the second brain: a markdown vault of notes plus typed entities
(people, places, tools, projects) with facts and relations over time.

    brain  search    {query}                       -> notes + entities
    brain  read      {path}                         -> one note, in full
    brain  write     {path?, title?, folder?, content} -> create or edit
    brain  append    {path, content}                -> add without erasing
    brain  entity    {entity_id | name, as_of?}      -> a profile
    brain  timeline  {entity_id | name | query?}     -> dated history
    brain  neighbors {path?, scope?, depth?}         -> the local graph
    brain  daily     {date?}                         -> today's daily note

The owner comes from the tool context (the runtime), never from the
arguments — same discipline as `context_recall` and `manage_memory`: a model
cannot ask to write into somebody else's vault by naming them in an argument.

Writing a mirrored note's title is refused by `notes.rename_note` upstream
(unaffected here — this tool edits content, not titles), and editing a
mirrored note through `write`/`append` only ever touches its user-editable
zone: the generated section a sync last wrote is preserved verbatim, same
rule the HTTP routes and the MCP server both follow.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)

_ACTIONS = ("search", "read", "write", "append", "entity", "timeline",
           "neighbors", "daily")


def _limit(value: Any, default: int, ceiling: int) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(1, min(wanted, ceiling))


def _resolve_entity_id(entities_mod, owner: str, args: Dict[str, Any]) -> str:
    entity_id = str(args.get("entity_id") or "").strip()
    if entity_id:
        return entity_id
    name = str(args.get("name") or "").strip()
    if not name:
        raise LookupError("give either entity_id or name")
    hits = entities_mod.list_entities(owner, q=name, limit=1)
    if not hits:
        raise LookupError(f"no entity matches {name!r}")
    return hits[0]["id"]


def _compose_edit(notes_mod, render_mod, fm_mod, db_mod, owner: str, path: str,
                  content: str) -> str:
    """Text for `notes.write_note`: a mirrored note keeps its generated
    section, a free note keeps only its frontmatter. Raises
    `FileNotFoundError` (via `read_note`) when `path` does not exist."""
    existing = notes_mod.read_note(owner, path)
    fields = dict(existing["frontmatter"])
    fields["updated"] = db_mod.now_iso()
    if existing.get("source"):
        return render_mod.compose(fields, content, existing.get("generated", ""))
    body = "\n" + str(content or "").strip("\n") + "\n"
    return fm_mod.join(fields, body)


class BrainTool:
    """`brain` {action, ...}: search, read and write the second brain."""

    async def execute(self, content: Any, ctx: dict) -> Dict[str, Any]:
        try:
            args = _parse_tool_args(content)
        except ValueError as exc:
            return {"error": f"brain: invalid arguments ({exc})", "exit_code": 1}

        action = str(args.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            return {"error": f"brain: unknown action {action!r}; expected one "
                             f"of {', '.join(_ACTIONS)}", "exit_code": 1}

        owner = str((ctx or {}).get("owner") or "")

        try:
            from src.brain import db, entities, notes, temporal, vault
            from src.brain import frontmatter as fm
            from src.brain import render
        except Exception as exc:  # noqa: BLE001
            return {"error": f"brain: the second brain is unavailable: {exc}",
                    "exit_code": 1}

        try:
            if action == "search":
                query = str(args.get("query") or "")
                if not query:
                    return {"error": "brain search: `query` is required", "exit_code": 1}
                limit = _limit(args.get("limit"), 20, 50)
                note_hits = notes.search(owner, query, limit=limit)
                entity_hits = entities.list_entities(owner, q=query, limit=limit)
                return {
                    "output": f"{len(note_hits)} note(s), {len(entity_hits)} "
                             f"entit(y/ies) matching {query!r}",
                    "notes": note_hits,
                    "entities": [
                        {"id": e["id"], "name": e["name"], "type": e["type"],
                         "summary": e.get("summary", "")}
                        for e in entity_hits
                    ],
                    "exit_code": 0,
                }

            if action == "read":
                path = str(args.get("path") or "")
                if not path:
                    return {"error": "brain read: `path` is required", "exit_code": 1}
                note = notes.read_note(owner, path)
                return {"output": note.get("user_zone") or note.get("content", ""),
                        "note": note, "exit_code": 0}

            if action == "write":
                path = str(args.get("path") or "").strip()
                text = str(args.get("content") or "")
                if path:
                    composed = _compose_edit(notes, render, fm, db, owner, path, text)
                    result = notes.write_note(owner, path, composed)
                    return {"output": f"updated {result['note']['path']}",
                            "action": "edited", **result, "exit_code": 0}
                title = str(args.get("title") or "").strip()
                if not title:
                    return {"error": "brain write: give `path` to edit an "
                                     "existing note, or `title` to create one",
                            "exit_code": 1}
                note = notes.create_note(owner, title,
                                         folder=str(args.get("folder") or "Notes"),
                                         content=text)
                return {"output": f"created {note['path']}", "action": "created",
                        "note": note, "exit_code": 0}

            if action == "append":
                path = str(args.get("path") or "")
                if not path:
                    return {"error": "brain append: `path` is required", "exit_code": 1}
                addition = str(args.get("content") or "")
                existing = notes.read_note(owner, path)
                merged = (existing["user_zone"] + "\n\n" + addition).strip("\n") \
                    if existing["user_zone"] else addition
                composed = _compose_edit(notes, render, fm, db, owner, path, merged)
                result = notes.write_note(owner, path, composed)
                return {"output": f"appended to {result['note']['path']}",
                        **result, "exit_code": 0}

            if action == "entity":
                entity_id = _resolve_entity_id(entities, owner, args)
                profile = entities.profile(entity_id, as_of=args.get("as_of") or None)
                name = (profile.get("entity") or {}).get("name", entity_id)
                return {"output": profile.get("summary", "") or f"profile for {name}",
                        "profile": profile, "exit_code": 0}

            if action == "timeline":
                if args.get("entity_id") or args.get("name"):
                    entity_id = _resolve_entity_id(entities, owner, args)
                    profile = entities.profile(entity_id)
                    events = profile.get("timeline", [])
                    return {"output": f"{len(events)} event(s)", "entity_id": entity_id,
                            "timeline": events, "exit_code": 0}
                events = temporal.timeline(owner, query=str(args.get("query") or ""),
                                           limit=_limit(args.get("limit"), 200, 500))
                return {"output": f"{len(events)} event(s)", "timeline": events,
                        "exit_code": 0}

            if action == "neighbors":
                path = str(args.get("path") or "").strip()
                scope = str(args.get("scope") or ("notes" if path else "entities"))
                depth = _limit(args.get("depth"), 1, 4)
                graph = (entities.graph(owner) if scope == "entities"
                        else notes.graph(owner, center=path or None, depth=depth))
                return {"output": f"{len(graph.get('nodes', []))} node(s), "
                                  f"{len(graph.get('edges', []))} edge(s)",
                        "scope": scope, "graph": graph, "exit_code": 0}

            if action == "daily":
                date = str(args.get("date") or "").strip() or None
                note = notes.daily_note(owner, date)
                return {"output": f"daily note: {note['path']}", "note": note,
                        "exit_code": 0}

        except LookupError as exc:
            return {"error": f"brain {action}: {exc}", "exit_code": 1}
        except FileNotFoundError as exc:
            return {"error": f"brain {action}: no such note: {exc}", "exit_code": 1}
        except ValueError as exc:
            return {"error": f"brain {action}: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - a tool call must never raise
            logger.warning("brain tool failed (%s): %s", action, exc, exc_info=True)
            return {"error": f"brain {action}: {type(exc).__name__}: {exc}",
                    "exit_code": 1}

        return {"error": f"brain: unhandled action {action!r}", "exit_code": 1}


__all__ = ["BrainTool"]
