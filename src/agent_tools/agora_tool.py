"""The Agora for a Faustus conversation, wherever Faustus runs.

The Agora (servers, channels, threads, the task board, who is around) lives in the family hub, on the person's PC. The
hub's own connector is a process of that PC, so a Faustus that runs on a Spark has no such tools. These two go to the
hub the way the Agora page does (``src/hub_link``): next to the hub when there is one, through its link when not.

They carry the hub's own Agora tools; they do not copy them. ``help`` lists them as the hub offers them now, and any
of them is called by its name with its arguments, so what the hub learns to do is here the same day:

* ``agora_read``  -- what only looks: the hub's read-only tools, and ``channel_read`` as a peek. Nothing is marked as
  read by reading.
* ``agora_write`` -- everything else: saying something, reacting, tasks, threads, members, channels, files.

The conversation speaks as Faustus (``faustus``), or as the actor of the project's coordination binding when the chat
has one. It never speaks as the person or as another agent: ``agent`` is set here, whatever the model sends.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from src import hub_link

PREFIX = "hub_agora_"
DEFAULT_ACTOR = "faustus"
CATALOGUE_TTL_S = 300.0
MAX_OUTPUT = 24_000
MAX_WAIT_S = 20
#: Not read-only for the hub (it moves the reader's mark), but as a peek it only looks.
READ_AS_PEEK = frozenset({"channel_read"})
#: Read-only for the hub, yet reading them with a reader's name counts as that reader having read the conversation.
READS_THAT_MARK = frozenset({"thread", "task"})
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_cache: Dict[str, Any] = {"at": 0.0, "url": "", "tools": {}}


class AgoraUnavailable(RuntimeError):
    pass


def _hub(method: str, path: str, body: Optional[dict] = None, timeout: float = 45.0) -> Tuple[Optional[int], Any]:
    """``(status, json)`` from the hub; ``(None, None)`` when nothing answered. Never raises."""
    where = hub_link.location()
    headers = {"Accept": "application/json", "User-Agent": "faustus-agent"}
    token = hub_link.token(where)
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(where["url"] + path, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception:  # noqa: BLE001 - refused (the PC is off), timed out
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, None


def catalogue(force: bool = False) -> Dict[str, dict]:
    """The hub's Agora tools by their short name (``channel_post``), as it offers them now."""
    url = hub_link.location()["url"]
    if not force and _cache["tools"] and _cache["url"] == url and time.monotonic() - _cache["at"] < CATALOGUE_TTL_S:
        return _cache["tools"]
    status, payload = _hub("GET", "/api/agent/tools", timeout=15.0)
    if status is None:
        raise AgoraUnavailable("The family hub is not answering: the Agora lives there, on the person's PC, and comes "
                               "back when the hub does.")
    rows = payload.get("tools") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(rows, list):
        raise AgoraUnavailable(f"The family hub did not give its tools (HTTP {status}).")
    tools = {str(row["name"])[len(PREFIX):]: row for row in rows
             if isinstance(row, dict) and str(row.get("name") or "").startswith(PREFIX)}
    _cache.update(at=time.monotonic(), url=url, tools=tools)
    return tools


def only_looks(name: str, tool: dict) -> bool:
    return bool((tool.get("annotations") or {}).get("readOnlyHint")) or name in READ_AS_PEEK


def actor(ctx: dict) -> str:
    """Who this conversation is in the Agora."""
    binding = getattr(getattr(ctx.get("security_context"), "coordination_grant", None), "binding", None)
    return str(getattr(binding, "actor", "") or DEFAULT_ACTOR)


TASK_PREFIX = "[Task] "


def routine_of(ctx: dict) -> Optional[dict]:
    """The scheduled task this conversation is the run of, as the hub names a routine; ``None`` for any other chat.

    A scheduled task of Faustus runs in a session called ``[Task] <name>``: what it writes to the Agora is the work of
    that routine, and the hub shows it next to the author."""
    try:
        from src.ai_interaction import get_session_manager
        manager = get_session_manager()
        session = manager.sessions.get(str(ctx.get("session_id") or "")) if manager is not None else None
        name = str(getattr(session, "name", "") or "")
    except Exception:  # noqa: BLE001 - tagging is a courtesy: a chat without a session manager is a chat without routine
        return None
    if not name.startswith(TASK_PREFIX) or not name[len(TASK_PREFIX):].strip():
        return None
    return {"name": " ".join(name[len(TASK_PREFIX):].split())[:80]}


def _first_sentence(text: str, limit: int = 150) -> str:
    text = " ".join(str(text or "").split())
    cut = text.find(". ")
    text = text[:cut + 1] if 0 < cut < limit else text
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _about(name: str, tool: dict) -> str:
    schema = tool.get("inputSchema") or {}
    required = set(schema.get("required") or ())
    lines = [name + ": " + " ".join(str(tool.get("description") or "").split()), "args:"]
    for key, prop in (schema.get("properties") or {}).items():
        if key == "agent" or not isinstance(prop, dict):
            continue
        kind = str(prop.get("type") or "any")
        if prop.get("enum"):
            kind += " (" + ", ".join(str(v) for v in prop["enum"]) + ")"
        line = f"- {key}: {kind}" + (", required" if key in required else "")
        if prop.get("description"):
            line += " -- " + " ".join(str(prop["description"]).split())
        lines.append(line)
    return "\n".join(lines)


class AgoraTool:
    def __init__(self, write: bool):
        self.write = write
        self.name = "agora_write" if write else "agora_read"

    async def execute(self, content, ctx):
        return await asyncio.to_thread(self._run, content, ctx or {})

    def _run(self, content, ctx: dict) -> dict:
        try:
            given = json.loads(content or "{}") if isinstance(content, str) else content
        except ValueError:
            return {"error": 'Arguments must be JSON: {"action": "...", "args": {...}}.', "exit_code": 1}
        if not isinstance(given, dict):
            return {"error": 'Arguments must be an object: {"action": "...", "args": {...}}.', "exit_code": 1}
        action = str(given.get("action") or "help").strip().lower()
        if action.startswith(PREFIX):
            action = action[len(PREFIX):]
        args = dict(given["args"]) if isinstance(given.get("args"), dict) else {}
        for key, value in given.items():          # a model that puts the arguments beside the action is understood
            if key not in ("action", "args"):
                args.setdefault(key, value)
        try:
            tools = catalogue()
        except AgoraUnavailable as exc:
            return {"error": str(exc), "exit_code": 1}
        mine = {name: tool for name, tool in tools.items() if only_looks(name, tool) != self.write}
        who = actor(ctx)
        if action == "help":
            about = str(args.get("about") or "").strip().lower().removeprefix(PREFIX)
            if about:
                if about in mine:
                    return {"output": _about(about, mine[about]), "exit_code": 0}
                other = "agora_read" if self.write else "agora_write"
                return {"error": f"{about} is {'an action of ' + other if about in tools else 'not an Agora action'}.",
                        "exit_code": 1}
            head = (f"Agora actions of {self.name} (you are {who}). "
                    f'{{"action": "help", "args": {{"about": "<action>"}}}} gives the arguments of one.')
            return {"output": head + "\n" + "\n".join(
                f"- {name}: {_first_sentence(tool.get('description'))}" for name, tool in mine.items()), "exit_code": 0}
        tool = mine.get(action)
        if tool is None:
            if action in tools:
                return {"error": f"{action} is an action of {'agora_read' if self.write else 'agora_write'}.",
                        "exit_code": 1}
            near = difflib.get_close_matches(action, list(mine), n=4, cutoff=0.5)
            return {"error": f"There is no Agora action called {action}." + (" Close: " + ", ".join(near) + "." if near else "")
                             + ' {"action": "help"} lists them.', "exit_code": 1}
        if getattr(ctx.get("tool_policy"), "read_only", False) and self.write:
            return {"error": "This conversation only reads: nothing is written to the Agora from it.", "exit_code": 1,
                    "blocked": True}
        props = (tool.get("inputSchema") or {}).get("properties") or {}
        args.pop("agent", None)
        if "agent" in props:
            args["agent"] = who
        if not self.write:
            if "peek" in props:
                args["peek"] = True
            elif action in READS_THAT_MARK:
                # A hub that cannot peek here marks the conversation as read for whoever it is told reads: read
                # as nobody rather than move Faustus's mark.
                args.pop("agent", None)
        if self.write and "routine" in props and not args.get("routine"):
            routine = routine_of(ctx)
            if routine:
                args["routine"] = routine           # what a scheduled task of Faustus says is told as that routine's
        if action == "file_add":
            # A file goes as its bytes. A path would be read on the hub's machine, which is not this one's disk
            # nor this conversation's to read.
            args.pop("path", None)
        if "wait_s" in args:
            try:
                args["wait_s"] = max(0, min(float(args["wait_s"]), MAX_WAIT_S))
            except (TypeError, ValueError):
                args.pop("wait_s")
        if "space_id" in props and not args.get("space_id"):
            binding = getattr(getattr(ctx.get("security_context"), "coordination_grant", None), "binding", None)
            if getattr(binding, "space_id", ""):
                args["space_id"] = binding.space_id
        status, payload = _hub("POST", "/api/agent/call", {"tool": PREFIX + action, "arguments": args}, timeout=60.0)
        if status is None:
            return {"error": "The family hub is not answering: nothing was read or written.", "exit_code": 1,
                    **({"outcome_unknown": True} if self.write else {})}
        result = payload.get("result") if isinstance(payload, dict) and "result" in payload else payload
        failed = status >= 400 or (isinstance(payload, dict) and payload.get("ok") is False)
        if failed:
            why = (result.get("error") if isinstance(result, dict) else None) \
                or (payload.get("error") if isinstance(payload, dict) else None) or f"the hub answered HTTP {status}"
            return {"error": f"{action}: {str(why)[:1500]}", "exit_code": 1}
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + "… [cut here: ask for less with limit, or for one thing at a time]"
        return {"output": text, "exit_code": 0, "untrusted_content": True}
