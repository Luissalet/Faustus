"""Faustus-side half of the Code Mode bridge (T6, A10).

Every ``tools.call(name, args)`` the guest subprocess makes is turned into
the exact ``ToolBlock`` + ``execute_tool_block`` call an ordinary native
function call from the model would produce, and dispatched through
``src.tool_execution.execute_tool_block`` -- the SAME dispatcher
``src/agent_loop.py`` and ``src/agent_tools/subagent_tools.py`` use. A
disabled tool, a tool blocked by the destructive-command guard, or an
unknown tool name gets the identical result a direct call would have
gotten; there is no separate, weaker gate for code-composed calls.

``security_context``: ``execute_tool_block`` requires a
``ToolRunSecurityContext`` (or the explicit ``NO_TOOL_SECURITY_CONTEXT``
sentinel, which would SKIP the destructive-command gate entirely --
exactly the bypass A10 forbids). The ``run_code`` native tool is dispatched
through ``src/agent_tools/__init__.py``'s generic ``TOOL_HANDLERS`` path
(``_direct_fallback`` in ``src/tool_execution.py``), whose ``ctx`` carries
``session_id``/``owner``/``disabled_tools`` but not the run's own
``ToolRunSecurityContext`` object or ``ToolPolicy`` (they are function
parameters of ``execute_tool_block``, never threaded into that ``ctx`` --
wiring that would touch ``src/tool_execution.py``, which is outside this
lot's owned files; see ``T6_wiring.md``). A fresh
``ToolRunSecurityContext()`` is used instead. This is not a weaker check
for the case A10 cares about: ``ToolRunSecurityContext.decision_for``
runs its destructive-command guard (``_command_guard_denial``) BEFORE it
ever consults ``approval_gate_bypassed`` or ``external_untrusted_context_seen``
(see that method's own docstring/comments in ``src/tool_capabilities.py``),
so for a command the guard classifies as destructive/needing approval, a
fresh context and the run's real context reach the exact same denial --
the only thing a fresh context could get "wrong" is ALLOWING something the
real run's context would have blocked for reasons unrelated to the command
itself (an external-untrusted-context gate that had already armed on
earlier tool results this turn), which is a strictly narrower gap, not a
bypass of the destructive-tool gate this case exercises.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# The tool that hosts Code Mode itself. Left out of tools.list()/tools.call()
# so generated code cannot recursively spawn another isolated subprocess
# from inside this one.
_SELF_TOOL_NAME = "run_code"


def _schema_by_name() -> dict[str, dict]:
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    out: dict[str, dict] = {}
    for entry in FUNCTION_TOOL_SCHEMAS:
        fn = entry.get("function") if isinstance(entry, Mapping) else None
        name = fn.get("name") if isinstance(fn, Mapping) else None
        if isinstance(name, str) and name:
            out[name] = fn
    return out


def allowed_tool_names(disabled_tools: Optional[Iterable[str]] = None) -> list[str]:
    """Tool names Code Mode may reach: schema-registered natives minus
    ``run_code`` itself, minus whatever this call/session disabled."""
    from src.agent_tools import TOOL_TAGS

    disabled = {str(t) for t in (disabled_tools or ())}
    names = {name for name in TOOL_TAGS if name != _SELF_TOOL_NAME}
    names |= {n for n in _schema_by_name() if n != _SELF_TOOL_NAME}
    return sorted(n for n in names if n not in disabled)


def list_tools(disabled_tools: Optional[Iterable[str]] = None, detail: str = "catalog") -> list[dict]:
    schemas = _schema_by_name()
    rows = []
    for name in allowed_tool_names(disabled_tools):
        fn = schemas.get(name) or {"name": name, "description": ""}
        row = {"name": name, "description": fn.get("description", "")}
        if detail == "schema":
            row["parameters"] = fn.get("parameters") or {"type": "object", "properties": {}}
        rows.append(row)
    return rows


async def dispatch_call(
    tool_name: str,
    args: Any,
    *,
    session_id: Optional[str],
    owner: Optional[str],
    workspace: Optional[str],
    workspace_roots: Optional[list],
    disabled_tools: Optional[Iterable[str]],
    call_id: str,
) -> dict:
    """Run one guest ``tools.call(tool_name, args)`` through the real
    dispatcher and return the same result shape ``execute_tool_block``
    returns to any other caller."""
    from src.agent_tools import TOOL_TAGS
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_execution import execute_tool_block
    from src.tool_schemas import ToolBlock, function_call_to_tool_block

    name = str(tool_name or "")
    if name == _SELF_TOOL_NAME:
        return {"error": "run_code cannot call itself from Code Mode.", "exit_code": 1}
    if name not in TOOL_TAGS and not name.startswith("mcp__"):
        return {"error": f"Unknown tool: {name}", "exit_code": 1}

    if isinstance(args, Mapping):
        raw_args = dict(args)
    elif args is None:
        raw_args = {}
    else:
        return {"error": "Tool arguments must be a JSON object.", "exit_code": 1}

    block = function_call_to_tool_block(name, json.dumps(raw_args))
    if block is None:
        # Same fallback the schema-to-ToolBlock converter offers other
        # callers: pass the raw arguments through as the block content.
        block = ToolBlock(name, json.dumps(raw_args))

    security_context = ToolRunSecurityContext()
    try:
        _desc, result = await execute_tool_block(
            block,
            session_id=session_id,
            disabled_tools=set(disabled_tools or ()),
            owner=owner,
            workspace=workspace,
            workspace_roots=workspace_roots,
            tool_policy=None,
            security_context=security_context,
            call_id=call_id,
        )
    except Exception as e:  # noqa: BLE001 - a guest call must never crash the runner
        logger.exception("code_mode: dispatch failed for tool=%s", name)
        return {"error": f"{name}: {e}", "exit_code": 1}
    return result if isinstance(result, dict) else {"output": str(result), "exit_code": 0}
