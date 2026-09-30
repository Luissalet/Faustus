"""
workflows/agent_turn.py — one headless Faustus agent turn, for an `agent` node.

There is exactly one agent loop in this code base (`agent_loop.stream_agent_loop`)
and this module does not add a second. It is the thinnest honest adapter from a
workflow node to that loop, built the way the two existing headless callers
build theirs — the background-job follow-up (`bg_monitor._drain_agent`) and a
delegated worker (`agent_tools.subagent_tools._run_subagent`):

* the model is the one the workers use (`dispatch.resolve_route`: the dispatch
  endpoint, then the utility model, then the default chat model), so it runs on
  the local model with no extra setup;
* the tool set is a *denylist* computed the way a worker's is
  (`worker_disabled_tools`), narrowed further to the node's own `tools`
  allowlist, and an agent profile (`src/agent_defs.py`) contributes its prompt
  and its permissions through `subagent_permissions.derive`;
* the turn is bounded by `max_rounds` and a wall-clock `timeout_s`, and stops
  promptly when the workflow's lease is lost (`cancel_requested`).

The function a node calls is :func:`run`. Its contract is a plain dict in and a
plain dict out so the handler can be tested with a fake that never imports the
agent loop (see `tests/test_workflow_model_nodes.py`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional

from .model_calls import run_coroutine

logger = logging.getLogger(__name__)

__all__ = ["run", "AgentTurnUnavailable", "DEFAULT_MAX_ROUNDS", "DEFAULT_TIMEOUT_S",
           "MAX_ROUNDS_CEILING", "MAX_TIMEOUT_S"]

#: A workflow step is a bounded job, not a conversation: eight rounds is
#: enough for "read two files and answer", and the ceiling keeps a typo in
#: `max_rounds` from becoming an hour of GPU.
DEFAULT_MAX_ROUNDS = 8
MAX_ROUNDS_CEILING = 50
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 3600
#: How much of the final answer is kept. The node's result lands in a database
#: row and in the prompt of whatever reads it next.
MAX_TEXT_CHARS = 60_000
MAX_TOOL_EVENTS_KEPT = 40


class AgentTurnUnavailable(RuntimeError):
    """The turn could not start (no model, unknown agent profile, a workspace
    that is no longer allowed). Raised BEFORE anything ran, so the node can
    fail without claiming an effect happened."""


def _workspace_for(owner: str, project_id: str) -> Optional[str]:
    if not project_id:
        return None
    from services.projects import get_store
    project = get_store().get(project_id, owner=owner)
    workspace = str((project or {}).get("workspace") or "")
    if not workspace:
        return None
    from pathlib import Path
    if not Path(workspace).is_dir():
        raise AgentTurnUnavailable("the workflow project's workspace folder is missing")
    from src.tool_execution import vet_workspace
    if not vet_workspace(workspace):
        raise AgentTurnUnavailable("the project workspace is no longer allowed")
    return str(Path(workspace).resolve())


def _profile(slug: str, workspace: Optional[str]):
    if not slug:
        return None
    from src import agent_defs
    definition = agent_defs.get(slug, workspace)
    if definition is None:
        raise AgentTurnUnavailable(f"no agent profile named {slug!r}")
    return definition


def _disabled_tools(prompt: str, definition: Any, tools: Optional[List[str]]) -> set:
    from src.agent_tools.subagent_tools import worker_disabled_tools
    permissions = None
    if definition is not None:
        from src import agent_defs
        from src.subagent_permissions import derive
        permissions = derive(None, definition, parent_depth=0,
                             vocabulary=agent_defs.known_tools())
    disabled = set(worker_disabled_tools(prompt, permissions))
    if tools is not None:
        # The node's own allowlist is a statement about THIS step: anything
        # the catalogue knows that it does not list is off, `delegate_agents`
        # included (a workflow step does not spawn a workforce by itself).
        from src import agent_defs
        disabled |= {t for t in agent_defs.known_tools() if t not in set(tools)}
    disabled.add("delegate_agents")
    return disabled


#: How often the reader wakes up to check the deadline and the cancel flag.
_WAKE_S = 30.0


async def _consume(stream, *, deadline: float, cancelled: Callable[[], bool]) -> Dict[str, Any]:
    text = ""
    tool_events: List[Dict[str, Any]] = []
    rounds = 0
    tool_calls = 0
    error = ""
    stop_reason = ""
    approvals = 0
    # The stream is consumed by ONE task for its whole life: the agent loop
    # sets and resets context variables across its steps, so every step must
    # run in the same context (a task per `__anext__` gives each step its own
    # copy and the reset fails). The reader here only waits on a queue, and
    # its periodic wake-up never touches the stream.
    queue: asyncio.Queue = asyncio.Queue()
    _END = object()

    async def _pump() -> None:
        try:
            async for item in stream:
                await queue.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced as the turn's error
            await queue.put(("__error__", f"{type(exc).__name__}: {exc}"))
        finally:
            await queue.put(_END)

    pump = asyncio.ensure_future(_pump())
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop_reason = "timeout"
            break
        if cancelled():
            stop_reason = "cancelled"
            break
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=min(remaining, _WAKE_S))
        except asyncio.TimeoutError:
            continue                       # re-check the deadline and the lease
        if chunk is _END:
            break
        if isinstance(chunk, tuple) and len(chunk) == 2 and chunk[0] == "__error__":
            error = str(chunk[1])[:300]
            continue
        if not isinstance(chunk, str):
            continue
        if chunk.startswith("event: error"):
            error = chunk[:300]
            continue
        if not chunk.startswith("data: "):
            continue
        body = chunk[6:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            event = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if "delta" in event:
            delta = event.get("delta")
            if isinstance(delta, str) and not event.get("thinking"):
                text += delta
        elif event.get("type") == "agent_step":
            rounds = max(rounds, int(event.get("round") or 0))
        elif event.get("type") == "tool_output":
            tool_calls += 1
            asked = isinstance(event.get("ask_user"), dict)
            approvals += 1 if asked else 0
            if len(tool_events) < MAX_TOOL_EVENTS_KEPT:
                tool_events.append({"round": rounds, "tool": event.get("tool"),
                                    "exit_code": event.get("exit_code"),
                                    **({"needs_approval": True} if asked else {})})
        elif event.get("type") == "error" and event.get("message"):
            error = str(event.get("message"))[:300]
    if not pump.done():                  # deadline or cancel: stop the producer
        pump.cancel()
    try:
        await pump
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    try:
        await stream.aclose()
    except Exception:  # noqa: BLE001 - closing a finished generator is best-effort
        pass
    return {"text": text, "tool_events": tool_events, "rounds": rounds,
            "tool_calls": tool_calls, "error": error, "stop_reason": stop_reason,
            "approvals_requested": approvals}


def run(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Run one agent turn and return
    `{text, rounds, tool_calls, tool_events, approvals_requested, model, stop_reason,
    error, session_id}`.

    `spec` keys: `prompt`, `system`, `agent` (profile slug), `tools` (None =
    the profile's/worker default, a list = exactly these), `max_rounds`,
    `timeout_s`, `owner`, `project_id`, `run_id`, `node_id`, `cancel_requested`
    (callable), `begin_effect` (callable returning False to abort — called once,
    immediately before the loop starts, so a turn that never began leaves no
    effect behind)."""
    owner = str(spec.get("owner") or "")
    project_id = str(spec.get("project_id") or "")
    prompt = str(spec.get("prompt") or "")
    max_rounds = max(1, min(int(spec.get("max_rounds") or DEFAULT_MAX_ROUNDS), MAX_ROUNDS_CEILING))
    timeout_s = max(5.0, min(float(spec.get("timeout_s") or DEFAULT_TIMEOUT_S), float(MAX_TIMEOUT_S)))
    tools = spec.get("tools")
    tools = [str(t) for t in tools] if isinstance(tools, (list, tuple)) else None

    from src import dispatch
    try:
        url, model, headers = dispatch.resolve_route(owner or None, str(spec.get("model") or "") or None)
    except ValueError as exc:
        raise AgentTurnUnavailable(str(exc))

    workspace = _workspace_for(owner, project_id)
    definition = _profile(str(spec.get("agent") or ""), workspace)
    disabled = _disabled_tools(prompt, definition, tools)
    system = str(spec.get("system") or "")
    if not system and definition is not None:
        system = str(getattr(definition, "prompt", "") or "")
    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    begin = spec.get("begin_effect")
    if callable(begin) and begin() is False:
        raise AgentTurnUnavailable("the workflow stopped or lost its claim before the agent turn")

    cancel_check = spec.get("cancel_requested")
    cancelled: Callable[[], bool] = (lambda: bool(cancel_check())) if callable(cancel_check) else (lambda: False)
    session_id = f"wf-{str(spec.get('run_id') or 'run')[:24]}-{str(spec.get('node_id') or 'node')[:24]}-{uuid.uuid4().hex[:6]}"

    async def turn() -> Dict[str, Any]:
        from src.agent_loop import stream_agent_loop
        stream = stream_agent_loop(
            url, model, messages, headers=headers, temperature=0.3, max_tokens=0,
            max_rounds=max_rounds, session_id=session_id, owner=owner or None,
            workspace=workspace, workspace_roots=[workspace] if workspace else None,
            disabled_tools=disabled, security_gate_bypass=False,
            workload="background",
            pending_cancel=lambda: ("the workflow lost its claim" if cancelled() else None),
        )
        return await _consume(stream, deadline=time.monotonic() + timeout_s, cancelled=cancelled)

    outcome = run_coroutine(turn())
    text = str(outcome.get("text") or "")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[truncated]"
    return {**outcome, "text": text, "model": model, "session_id": session_id,
            "profile": getattr(definition, "slug", "") if definition is not None else ""}
