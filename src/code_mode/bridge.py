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

import asyncio
import json
import logging
import time
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# The tool that hosts Code Mode itself. Left out of tools.list()/tools.call()
# so generated code cannot recursively spawn another isolated subprocess
# from inside this one.
_SELF_TOOL_NAME = "run_code"

# How often the approval-pause poll checks whether the human has answered.
_APPROVAL_POLL_INTERVAL_S = 0.5
DEFAULT_APPROVAL_WAIT_SECONDS = 300


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


def _requires_approval(name: str) -> bool:
    """Whether `name` is the narrow, code-mode-approvable case: a per-call
    desktop-input tool (`ALWAYS_APPROVE_TOOLS`) while `desktop_control_mode`
    asks on every call. See `_pausable_approval_block` below for why this is
    the ONLY case `dispatch_call` pauses and asks a human about -- a script
    that checks this flag before calling can expect exactly that call, and
    no other, to block on a question."""
    try:
        from src.tool_capabilities import tool_requires_per_call_approval
        return bool(tool_requires_per_call_approval(name))
    except Exception:  # noqa: BLE001 - a catalogue row must never fail to build
        return False


def list_tools(disabled_tools: Optional[Iterable[str]] = None, detail: str = "catalog") -> list[dict]:
    schemas = _schema_by_name()
    rows = []
    for name in allowed_tool_names(disabled_tools):
        fn = schemas.get(name) or {"name": name, "description": ""}
        row = {
            "name": name,
            "description": fn.get("description", ""),
            "requires_approval": _requires_approval(name),
        }
        if detail == "schema":
            row["parameters"] = fn.get("parameters") or {"type": "object", "properties": {}}
        rows.append(row)
    return rows


def _decision_from_answer(answer: Any) -> str:
    """"approve" or "deny" from a resolved question's stored answer.

    Two answer shapes reach here, both built by `routes/chat_routes.py`
    (the ordinary `POST /api/chat` question_id path and the synthetic-
    session `POST /api/questions/{id}/answer` route this feature added):
    `option_ids` (a picked AskOption's `id`, from the tray's option
    buttons -- see the `options=` passed to `question_store.open_question`
    above) and a free-typed `text`. Either can carry the decision, so both
    are checked, case-insensitively; `option_ids` wins when both are
    present since it is the deliberate button click. Anything else (a
    stray/garbled answer) is a deny, never a silent approve -- an
    unparseable "yes" must not run the sealed action."""
    if not isinstance(answer, Mapping):
        return "deny"
    option_ids = [str(o).strip().lower() for o in (answer.get("option_ids") or [])]
    if "approve" in option_ids:
        return "approve"
    if "deny" in option_ids:
        return "deny"
    text = str(answer.get("text") or "").strip().lower()
    if text == "approve":
        return "approve"
    return "deny"


def _pausable_approval_block(tool_name: str, result: Any) -> bool:
    """Whether `result` is exactly the "this call needs a human's approval"
    shape from `execute_tool_block` for a case Code Mode can actually pause
    and resolve on its own -- the per-call ALWAYS_APPROVE_TOOLS gate
    (`ToolRunSecurityContext.decision_for` / `tool_requires_per_call_approval`).

    Deliberately narrow. `decision_for` funnels THREE different denials
    through the identical `blocked=True, policy="external_untrusted_context"`
    shape (`blocked_tool_result` in src/tool_capabilities.py): the per-call
    desktop-input gate, the destructive-command guard (DANGEROUS/CRITICAL
    shell), and the general post-external-context effect gate. Only the
    first is approvable here:

    * the destructive-command guard's DENY is a hard policy denial for Code
      Mode on purpose -- a generated script must not be able to sit there
      and wait for a human to bless an `rm -rf`/`DROP TABLE` it composed
      from whatever it just read; that stays a plain rejection the script
      has to handle itself (existing behaviour, tests/acceptance/
      test_a10_code_mode_policy_gate.py pins this).
    * the general external-context gate needs a resolved `active_document`
      (edit_document/suggest_document/update_document) or other turn-scoped
      state Code Mode's dispatch has no access to build correctly; sealing a
      wrong or stale target would be worse than refusing. Left as the
      original denial, same as before this feature existed.

    A plain error (`result["error"]` with no `blocked`/`policy`) or any
    other rejection shape is never approvable either -- only this one exact
    shape, and only for a tool `tool_requires_per_call_approval` names.
    """
    if not isinstance(result, dict) or result.get("blocked") is not True:
        return False
    if result.get("policy") != "external_untrusted_context":
        return False
    return _requires_approval(tool_name)


async def _pause_for_approval(
    *,
    name: str,
    raw_args: dict,
    block: Any,
    original_result: dict,
    session_id: Optional[str],
    owner: Optional[str],
    workspace: Optional[str],
    workspace_roots: Optional[list],
    disabled_tools: Optional[Iterable[str]],
    tool_policy: Any,
    security_context: Any,
    call_id: str,
    wall_clock: Any = None,
    approvals_log: Optional[list] = None,
) -> dict:
    """Ask a human to approve or deny `block`, blocking this coroutine (and
    so the guest subprocess, which is sitting on its blocking `recv()`)
    until they answer, the wait times out, or the question is cancelled.

    Mirrors `src.mcp_manager.make_elicitation_callback`'s round trip: open a
    `question_store` question, poll it, act on the terminal status. The
    question is opened under a SYNTHETIC session id (`code_mode:<session>`),
    the same trick `make_elicitation_callback` uses (`mcp:<server_id>`) --
    the real chat session is still mid-turn inside this very tool call, and
    Studio answers a question by posting a new chat turn to its session
    (routes/chat_routes.py); posting that to the REAL session would race the
    in-flight turn. The synthetic session is visible and answerable in the
    same Activity-tray "open questions" list (GET /api/questions) and
    answered through the exact same UI flow (`answerQuestion` in
    studio/src/adapters/activity.ts) -- it just lands on a throwaway session
    nothing else reads, same as an MCP elicitation question does today.
    """
    from src.settings import get_setting
    from src.tool_approvals import tool_approval_store
    from src.tool_approval_scopes import TASK_APPROVAL_DECISION
    from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
    from src.tool_execution import execute_tool_block
    from src import question_store

    wait_seconds = int(
        get_setting("agent_code_mode_approval_wait_seconds", DEFAULT_APPROVAL_WAIT_SECONDS)
        or DEFAULT_APPROVAL_WAIT_SECONDS
    )
    wait_seconds = max(1, wait_seconds)

    capabilities = capabilities_for_action(name, block.content)
    origin_run_id = (
        security_context.run_id if isinstance(security_context, ToolRunSecurityContext) else ""
    )
    external_seen = bool(
        getattr(security_context, "external_untrusted_context_seen", False)
    )
    pending = tool_approval_store.create(
        owner=owner,
        session_id=session_id,
        origin_run_id=origin_run_id,
        tool_name=name,
        content=block.content,
        workspace=workspace,
        external_untrusted_context_seen=external_seen,
        selected_tools=[name],
        continuation_query="",
        capabilities=capabilities,
    )

    args_preview = json.dumps(raw_args, default=str, ensure_ascii=False)[:300]
    message = (
        f"A running Code Mode script wants to call '{name}' with {args_preview}. "
        "This action needs your approval before the script can continue."
    )
    question_session_id = f"code_mode:{session_id or 'headless'}"
    opened = question_store.open_question(
        message,
        session_id=question_session_id,
        owner=owner or "",
        options=[
            # Same option shape `ask_user` uses (AskOption: label/description/
            # id, studio/src/adapters/chat.ts) so the Activity tray renders
            # these as the usual option buttons (questionFrom in
            # studio/src/adapters/activity.ts maps `id` straight through).
            {"label": "Approve", "description": "", "id": "approve"},
            {"label": "Deny", "description": "", "id": "deny"},
        ],
        allow_free_text=False,
        ttl_seconds=wait_seconds,
        supersede_open=False,
    )
    question_id = str(opened.get("question_id") or "")

    # A11's wall-time quota is a SCRIPT budget: the clock must not run while
    # this coroutine is doing nothing but waiting on a person. Pre-extend the
    # shared deadline by the full wait window before blocking, then give back
    # whatever of it went unused once the human answers (or the timeout
    # fires) -- net effect is the deadline moved forward by exactly the time
    # actually spent paused, never more, never less.
    if wall_clock is not None:
        wall_clock.extend(wait_seconds)
    pause_started = time.monotonic()
    decision = "timeout"
    try:
        deadline = pause_started + wait_seconds
        while True:
            row = question_store.get_question(question_id)
            if row is None:
                decision = "timeout"
                break
            status = row.get("status")
            if status == "answered":
                decision = _decision_from_answer(row.get("answer"))
                break
            if status in ("cancelled", "expired"):
                decision = "timeout" if status == "expired" else "deny"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                decision = "timeout"
                break
            await asyncio.sleep(min(_APPROVAL_POLL_INTERVAL_S, remaining))
    finally:
        waited_s = time.monotonic() - pause_started
        if wall_clock is not None:
            # Give back the unused slack of the pre-extension above.
            wall_clock.extend(waited_s - wait_seconds)
        if decision == "timeout":
            try:
                question_store.cancel_question(question_id, reason="code_mode_approval_timeout")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("code_mode: could not cancel timed-out approval question", exc_info=True)

    if approvals_log is not None:
        approvals_log.append({
            "tool": name,
            "decision": decision,
            "waited_ms": round(waited_s * 1000.0, 1),
        })

    if decision != "approve":
        # DENY_APPROVAL_DECISION consumes the pending card too, so a leftover
        # sealed action for this exact call can never be replayed later.
        try:
            tool_approval_store.consume(
                pending.approval_id, decision="deny", owner=owner, session_id=session_id,
                allow_continuation=False,
            )
        except Exception:  # noqa: BLE001 - best-effort; the pending card also expires on its own
            pass
        if decision == "deny":
            return {"error": f"{name}: approval declined by user.", "exit_code": 1, "approval": "denied"}
        return {"error": f"{name}: approval timed out.", "exit_code": 1, "approval": "timeout"}

    exact_approval = tool_approval_store.consume(
        pending.approval_id,
        decision=TASK_APPROVAL_DECISION,
        owner=owner,
        session_id=session_id,
        # SINGLE_ACTION: this approval runs exactly this one call and nothing
        # else -- a code-mode script cannot turn one human "yes" into a
        # standing bypass for the rest of the run.
        allow_continuation=False,
    )
    if exact_approval is None:
        # Lost a race (expired/consumed between "answered" and here) -- the
        # honest answer is the original denial, not a fabricated approval.
        return {"error": f"{name}: approval could not be claimed (expired).", "exit_code": 1, "approval": "timeout"}

    _desc, result = await execute_tool_block(
        block,
        session_id=session_id,
        disabled_tools=set(disabled_tools or ()),
        owner=owner,
        workspace=workspace,
        workspace_roots=workspace_roots,
        tool_policy=tool_policy,
        security_context=security_context,
        exact_approval=exact_approval,
        call_id=call_id,
    )
    if (
        isinstance(result, dict)
        and result.get("blocked") is True
        and result.get("policy") == "exact_tool_approval"
    ):
        # The sealed-approval preconditions (armed run, document target,
        # workspace) were not satisfiable from Code Mode's dispatch for this
        # call after all -- don't fabricate progress, hand back the original
        # denial rather than this confusing second one.
        return original_result
    return result if isinstance(result, dict) else {"output": str(result), "exit_code": 0}


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
    tool_policy: Any = None,
    security_context: Any = None,
    wall_clock: Any = None,
    approvals_log: Optional[list] = None,
) -> dict:
    """Run one guest ``tools.call(tool_name, args)`` through the real
    dispatcher and return the same result shape ``execute_tool_block``
    returns to any other caller.

    When the call comes back as the narrow approval-pausable shape (see
    `_pausable_approval_block`) and `agent_code_mode_pause_for_approval` is
    on, this PAUSES here -- the guest subprocess is already blocked on its
    own `recv()` for this exact call, so pausing the host coroutine that
    awaits it is the whole mechanism -- asks a human, and resumes the SAME
    call (approved) or returns a clean denial, never re-running anything
    the script already did.
    """
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

    # The run's own posture when the tool handed it down (ctx["security_context"]
    # / ctx["tool_policy"] from execute_tool_block's ctx); a fresh one only for
    # headless callers, which have no run history to forget.
    if security_context is None:
        security_context = ToolRunSecurityContext()
    try:
        _desc, result = await execute_tool_block(
            block,
            session_id=session_id,
            disabled_tools=set(disabled_tools or ()),
            owner=owner,
            workspace=workspace,
            workspace_roots=workspace_roots,
            tool_policy=tool_policy,
            security_context=security_context,
            call_id=call_id,
        )
    except Exception as e:  # noqa: BLE001 - a guest call must never crash the runner
        logger.exception("code_mode: dispatch failed for tool=%s", name)
        return {"error": f"{name}: {e}", "exit_code": 1}
    result = result if isinstance(result, dict) else {"output": str(result), "exit_code": 0}

    if _pausable_approval_block(name, result):
        try:
            from src.settings import get_setting
            pause_on = bool(get_setting("agent_code_mode_pause_for_approval", True))
        except Exception:  # noqa: BLE001 - settings unavailable: keep old behaviour
            pause_on = True
        if pause_on and session_id:
            try:
                return await _pause_for_approval(
                    name=name,
                    raw_args=raw_args,
                    block=block,
                    original_result=result,
                    session_id=session_id,
                    owner=owner,
                    workspace=workspace,
                    workspace_roots=workspace_roots,
                    disabled_tools=disabled_tools,
                    tool_policy=tool_policy,
                    security_context=security_context,
                    call_id=call_id,
                    wall_clock=wall_clock,
                    approvals_log=approvals_log,
                )
            except Exception:  # noqa: BLE001 - a broken pause must not crash the run
                logger.exception("code_mode: approval pause failed for tool=%s", name)
                return result
    return result
