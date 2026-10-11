"""The lifecycle-hook side of observations.

Four in-process listeners (``src/lifecycle_hooks.py::register_listener``):

* ``turn_start``   — opens a turn: a turn id, the user's request, a counter;
* ``post_tool``    — after every tool call of the agent loop and of its
  sub-agents (they run the same loop): pre-filter, then queue for the worker;
  and, when asked to, answer a file read with the notes that touched the file;
* ``turn_end``     — queue one summary job if the turn left any note;
* ``session_start``— the recap block (opt-in).

Each callback only reads settings, does string work and appends to a deque:
they run inside the agent's event loop and must not wait. Every callback
catches its own errors.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, Optional

from . import capture, retrieval, store, worker

logger = logging.getLogger(__name__)

LISTENER_ID = "observations"
MAX_TRACKED_TURNS = 256
REQUEST_CHARS = 400

_TURNS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_LOCK = threading.Lock()


def _turn(session_id: str, *, request: Optional[str] = None, fresh: bool = False) -> Dict[str, Any]:
    with _LOCK:
        state = _TURNS.get(session_id)
        if state is None or fresh:
            state = {"turn_id": f"{int(time.time())}-{uuid.uuid4().hex[:6]}", "request": "", "count": 0, "owner": ""}
            _TURNS[session_id] = state
            while len(_TURNS) > MAX_TRACKED_TURNS:
                _TURNS.popitem(last=False)
        else:
            _TURNS.move_to_end(session_id)
        if request:
            state["request"] = " ".join(str(request).split())[:REQUEST_CHARS]
        return state


def reset_for_tests() -> None:
    with _LOCK:
        _TURNS.clear()
    capture.REPEATS.clear()


def _bool_setting(key: str, default: bool) -> bool:
    return bool(worker._setting(key, default))


def on_turn_start(ctx: Dict[str, Any]) -> None:
    session_id = str(ctx.get("session_id") or "")
    if not session_id or not worker.enabled():
        return None
    _turn(session_id, request=ctx.get("user_message"), fresh=True)
    return None


def on_post_tool(ctx: Dict[str, Any]) -> Optional[str]:
    """Queue the call for the worker (unless noise) and, for a file read, return
    the notes about that file. Never raises."""
    try:
        text = _file_context(ctx)
    except Exception:  # noqa: BLE001
        logger.debug("observations: file context failed", exc_info=True)
        text = None
    try:
        _record(ctx)
    except Exception:  # noqa: BLE001
        logger.debug("observations: capture failed", exc_info=True)
    return text


def _record(ctx: Dict[str, Any]) -> None:
    if not worker.enabled() or ctx.get("incognito"):
        return
    session_id = str(ctx.get("session_id") or "")
    reason = capture.noise_reason(ctx.get("tool"), ctx.get("content"), ctx.get("result"), session_id=session_id)
    if reason:
        worker.count_prefiltered(reason)
        return
    state = _turn(session_id)
    cap = worker._int_setting("agent_observations_max_per_turn", 40, 1, 500)
    if state["count"] >= cap:
        worker.count_prefiltered("turn_cap")
        return
    job = capture.build_job(ctx, turn_id=state["turn_id"], ts=time.time())
    job["request"] = state["request"]
    if worker.enqueue(job):
        state["count"] += 1
        if job["owner"]:
            state["owner"] = job["owner"]


def _file_context(ctx: Dict[str, Any]) -> Optional[str]:
    if ctx.get("incognito") or not _bool_setting("agent_observations_file_context", False):
        return None
    tool = str(ctx.get("tool") or "")
    if tool != "read_file":
        return None
    result = ctx.get("result")
    if isinstance(result, dict) and (result.get("error") or result.get("approval_required")):
        return None
    paths = [p for p in (ctx.get("paths") or []) if isinstance(p, str) and p.strip()]
    if not paths:
        return None
    text = retrieval.file_context_text(
        paths[0], owner=str(ctx.get("owner") or ""), workspace=str(ctx.get("workspace") or ""))
    return text or None


def on_turn_end(ctx: Dict[str, Any]) -> None:
    session_id = str(ctx.get("session_id") or "")
    if not session_id:
        return None
    try:
        with _LOCK:
            state = _TURNS.pop(session_id, None)
        if not state or not state["count"] or not worker.summaries_enabled():
            return None
        worker.enqueue({
            "kind": "summary", "ts": time.time(), "session_id": session_id, "turn_id": state["turn_id"],
            "request": state["request"] or " ".join(str(ctx.get("user_message") or "").split())[:REQUEST_CHARS],
            "owner": state["owner"],
        })
    except Exception:  # noqa: BLE001
        logger.debug("observations: turn_end failed", exc_info=True)
    return None


def on_session_start(ctx: Dict[str, Any]) -> Optional[str]:
    try:
        if ctx.get("incognito") or not _bool_setting("agent_observations_inject_session_start", False):
            return None
        project = str(ctx.get("workspace") or ctx.get("project_id") or "")
        if not project:
            return None
        budget = worker._int_setting("agent_observations_inject_max_chars", retrieval.DEFAULT_CONTEXT_CHARS, 500, 20000)
        text = retrieval.context_block(project, budget, owner=str(ctx.get("owner") or ""))
        return text or None
    except Exception:  # noqa: BLE001
        logger.debug("observations: session recap failed", exc_info=True)
        return None


MCP_SERVER_ID = "observations"
MCP_SERVER_ENTRY = ("mcp_servers/observations_server.py", "Built-in: Observations")


def register_mcp_server() -> bool:
    """Make the built-in MCP server known to ``src/builtin_mcp.py`` at runtime.

    ``register_builtin_servers`` connects whatever ``_BUILTIN_SERVERS`` holds when
    the app starts; adding the entry here, before that runs, avoids editing that
    file. ``setdefault``: once the entry is written there by hand this is a no-op.
    Returns True when the entry is present afterwards."""
    try:
        from src import builtin_mcp
        servers = builtin_mcp._BUILTIN_SERVERS
        servers.setdefault(MCP_SERVER_ID, MCP_SERVER_ENTRY)
        return MCP_SERVER_ID in servers
    except Exception:  # noqa: BLE001 - no MCP server is not a broken app
        logger.debug("observations: MCP registration failed", exc_info=True)
        return False


def install() -> None:
    """Register the listeners and the MCP server. Idempotent."""
    register_mcp_server()
    from src import lifecycle_hooks as hooks
    hooks.register_listener("turn_start", on_turn_start, listener_id=LISTENER_ID, name="Observations")
    hooks.register_listener("post_tool", on_post_tool, listener_id=LISTENER_ID, name="Observations")
    hooks.register_listener("turn_end", on_turn_end, listener_id=LISTENER_ID, name="Observations")
    hooks.register_listener("session_start", on_session_start, listener_id=LISTENER_ID, name="Recent observations")


def uninstall() -> None:
    from src import lifecycle_hooks as hooks
    hooks.unregister_listener(LISTENER_ID)
