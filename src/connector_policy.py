"""src/connector_policy.py — CONTRATO_CONECTORES Lote F2 (Fase E).

Single point of truth for two questions asked all over the tool-execution
path (see ``docs/api/tool_selection.md`` §Trace for exactly where):

1. "Which MCP servers (``McpServer.id`` values) may this turn call?" —
   ``resolve_allowed_servers``.
2. "Is this one qualified tool name inside that set?" — ``is_tool_allowed``.

Both are pure functions with no I/O, on purpose (F2.5 exercises them with
plain lists/sets, no fixtures, no DB). The one non-trivial lookup —
"given a session_id, what are its three tiers' stored values?" — lives in
``resolve_allowed_servers_for_session`` below, the single helper every
call site in ``src/agent_loop.py`` and ``src/tool_execution.py`` actually
calls. Keeping the lookup and the precedence rule in two separate
functions means the precedence rule (the part F2.5 wants pinned exactly)
never has to touch a database.

Model (F2.2): ``connector_ids: list[str] | None`` at each of three tiers —
task, session, project. ``None`` at a tier means "this tier never set
anything"; an explicit ``[]`` means "this tier allows zero connectors" and
is a real, different answer that must never be upgraded to "unrestricted".
Precedence: task/session (whichever tier applies to this run) > project >
unrestricted (every enabled connector — today's behavior, unchanged).
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)


def resolve_allowed_servers(
    session: Optional[Iterable[str]] = None,
    project: Optional[Iterable[str]] = None,
    task: Optional[Iterable[str]] = None,
) -> Optional[Set[str]]:
    """The effective set of allowed ``McpServer.id`` values, or ``None``.

    ``None`` return value = no restriction at all (every enabled connector
    may be called — the behavior every session/project/task had before this
    lot existed, and what an undeclared tier still produces).

    Each argument is that tier's OWN stored value: already read from
    ``Session.connector_ids`` (session), ``projects.json``'s ``connectors``
    field (project) or ``task_policies.connector_ids`` (task) — this
    function does no lookup of its own, so it cannot get the lookup wrong
    and is trivial to unit-test. Precedence is task/session (a scheduled
    task's own dedicated session normally carries only one of the two) over
    project over unrestricted: the first of ``(task, session, project)``
    that is not ``None`` wins outright, even when it is an empty list.
    """

    for tier in (task, session, project):
        if tier is not None:
            return {str(item) for item in tier if item}
    return None


def is_tool_allowed(tool_name: str, allowed: Optional[Set[str]]) -> bool:
    """Whether ``tool_name`` may execute under ``allowed``.

    ``allowed=None`` (no restriction declared) always returns True — this is
    the non-regression path every session/project/task had before F2.2.

    Only names shaped ``mcp__<server_id>__<tool>`` are gated. Every
    built-in tool (``bash``, ``send_email``, ``read_email``, ...) — i.e.
    any name that does not start with ``mcp__`` — is untouched by connector
    policy: this lot's minimum scope is MCP servers (see
    ``docs/api/tool_selection.md`` §Decisions for why built-in email/
    calendar tools are deliberately out of scope). A ``mcp__``-prefixed
    name that does not parse into at least ``mcp__<server>__<tool>`` (a
    bare ``"mcp__"`` , or ``"mcp__" `` with no tool segment) is let through
    here too — it cannot correspond to any real connector, and the
    dispatcher's own "Unknown tool" / arg-parse-error path is where a
    malformed name is properly rejected, not this one.
    """

    if allowed is None:
        return True
    name = str(tool_name or "")
    if not name.startswith("mcp__"):
        return True
    # maxsplit=2: a browser-style tool name carries its own "__" past the
    # server id (e.g. "mcp__srv__browser_click") — splitting on the first
    # two "__" only keeps that suffix intact as one segment.
    parts = name.split("__", 2)
    if len(parts) < 3 or not parts[1] or not parts[2]:
        return True
    return parts[1] in allowed


def _task_connector_ids_for_session(session_id: str) -> Optional[list]:
    """The declared ``connector_ids`` of the scheduled task this session is
    the dedicated working session OF, or ``None`` when this session is not
    a task's session (the overwhelmingly common case) or has no such
    declaration. Never raises."""

    try:
        from core.database import SessionLocal, ScheduledTask
        from src.task_scheduler import get_task_policy

        db = SessionLocal()
        try:
            task = (
                db.query(ScheduledTask.id)
                .filter(ScheduledTask.session_id == session_id)
                .first()
            )
            if task is None:
                return None
            return get_task_policy(task[0]).get("connector_ids")
        finally:
            db.close()
    except Exception:
        logger.debug("connector_policy: task lookup failed for session=%s", session_id, exc_info=True)
        return None


def _project_connector_ids_for_session(session_id: str, owner: Optional[str]) -> Optional[list]:
    """The ``connectors`` field of the project this session belongs to, or
    ``None`` when the session has no project or the project never set one.
    Never raises."""

    try:
        from services.projects import project_for_session

        project = project_for_session(session_id, owner)
        if not project:
            return None
        value = project.get("connectors")
        return value if isinstance(value, list) else None
    except Exception:
        logger.debug("connector_policy: project lookup failed for session=%s", session_id, exc_info=True)
        return None


def tool_support_notice(
    session_id: Optional[str],
    endpoint_url: Optional[str],
    owner: Optional[str] = None,
) -> Optional[str]:
    """CONTRATO_CONECTORES F2.4: a user-facing notice for a reply whose
    endpoint/model cannot use tools at all AND whose chat has an explicit
    connector selection somewhere in its chain (session/project/task) — the
    one case where a real person picked specific connectors that then
    silently did nothing, with no visible sign why.

    Deliberately NARROW: today the only "cannot use tools at all" transport
    is the text-only CLI one (``faustus-cli://`` — see
    ``src.agent_loop._agent_route_tool_mode``'s early return and
    ``text_only_transport`` in ``_build_route_request_state``). A model
    that merely lacks NATIVE function-calling still gets tools through
    Faustus's own fenced-block calling, so it is not "unsupported" for this
    purpose and gets no notice.

    Returns ``None`` when there is nothing to say (either tools genuinely
    work here, or this chat never narrowed its connectors, so "nothing
    happened" is not a surprise). Advisory only — this function does not,
    and must not, change any endpoint or model; see the "no silent
    fallback" principle in ``docs/api/tool_selection.md``.
    """

    if not str(endpoint_url or "").lower().startswith("faustus-cli://"):
        return None
    try:
        allowed = resolve_allowed_servers_for_session(session_id, owner)
    except Exception:
        return None
    if allowed is None:
        return None
    return (
        "This chat's model/endpoint is a text-only transport and cannot use "
        "tools, so the MCP connectors selected for it were not called this "
        "turn. The model/endpoint was not changed."
    )


def resolve_allowed_servers_for_session(
    session_id: Optional[str], owner: Optional[str] = None
) -> Optional[Set[str]]:
    """``resolve_allowed_servers`` for one live session_id — the one helper
    every real call site uses (``src.agent_loop._build_system_prompt``,
    ``src.tool_execution._execute_tool_block_impl``).

    This is what makes background runs, resumed runs (``GET
    /api/chat/resume/{id}``) and scheduled-task runs all covered by the
    SAME two enforcement points without threading a new parameter through
    every layer in between (``stream_agent_loop`` → ``_stream_agent_loop_
    body`` → ``execute_tool_block``, and the scheduler's own
    ``_run_agent_loop``): every one of those already carries ``session_id``
    end to end, and a scheduled task's run IS that session's run (``task.
    session_id`` — see ``src/task_scheduler.py::_execute_llm_task``), so
    resolving fresh from ``session_id`` at each use finds the task's
    declared policy exactly when a task is the one running, with no extra
    wiring and no snapshot to go stale across a pause/resume. Documented as
    a deliberate deviation in ``docs/api/tool_selection.md`` §Decisions.

    Never raises: any lookup failure degrades to "that tier contributed
    nothing" (all three lookups are already individually best-effort), and
    an empty ``session_id`` returns ``None`` (unrestricted) outright.
    """

    if not session_id:
        return None
    try:
        from core.database import get_session_connector_ids

        session_ids = get_session_connector_ids(session_id)
    except Exception:
        logger.debug("connector_policy: session lookup failed for session=%s", session_id, exc_info=True)
        session_ids = None
    task_ids = _task_connector_ids_for_session(session_id)
    project_ids = _project_connector_ids_for_session(session_id, owner)
    return resolve_allowed_servers(session=session_ids, project=project_ids, task=task_ids)
