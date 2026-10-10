"""Literal user shell commands through the ordinary bash executor, without a model."""
from __future__ import annotations

import json
import uuid
from dataclasses import replace
from typing import Any

from core.models import ChatMessage
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
from src.tool_execution import execute_tool_block, vet_workspace
from src.tool_parsing import ToolBlock
from src.tool_approvals import tool_approval_store
from src.tool_policy import build_effective_tool_policy


#: How much of the output the saved reply shows (its tail); the tool event
#: keeps what the executor returned.
_REPLY_OUTPUT_CHARS = 6000


class ShellCommandError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _manager():
    from src.ai_interaction import get_session_manager
    manager = get_session_manager()
    if manager is None:
        raise ShellCommandError(503, "The session manager is still starting.")
    return manager


def _owned_session(manager, sid: str, owner: str):
    from src.owner_identity import auth_disabled
    try:
        session = manager.get_session(sid)
    except KeyError:  # the real manager raises for an unknown id
        session = None
    local_operator = not owner and auth_disabled()
    if session is None or (not local_operator and str(session.owner or "") != str(owner or "")):
        raise ShellCommandError(404, "Session not found.")
    return session


def _policy(owner: str, session, requested: str):
    from services.projects import project_for_session
    from src.settings import get_user_setting
    project = project_for_session(session.id, owner or None) or {}
    preset = (project.get("execution_defaults") or {}).get("autonomy_preset", "supervised")
    disabled = get_user_setting("disabled_tools", owner, []) or []
    if isinstance(disabled, str):
        try:
            disabled = json.loads(disabled)
        except ValueError:
            disabled = [disabled]
    policy = build_effective_tool_policy(disabled_tools=disabled)
    return replace(policy, read_only=preset == "read_only" or requested == "read_only"), project


def pending_for_session(sid: str, owner: str) -> dict | None:
    session = _owned_session(_manager(), sid, owner)
    owner = str(owner or session.owner or "")
    for message in reversed(session.history):
        key = (message.metadata or {}).get("literal_shell_approval_id")
        pending = tool_approval_store.peek(key) if key else None
        if pending and pending.owner == str(owner or "") and pending.session_id == sid:
            return {"approval_id": pending.approval_id, "session_id": sid,
                    "command": pending.content, "reason": "This shell command needs approval."}
    return None


def _save_result(manager, session, command: str, result: dict, approval_id: str = ""):
    output = str(result.get("output") or result.get("stdout") or result.get("error") or "(no output)")
    if result.get("stderr"):
        output += "\n" + str(result["stderr"])
    event = {"round": 0, "tool": "bash", "command": command, "output": output,
             "exit_code": result.get("exit_code", 1), "blocked": bool(result.get("blocked")),
             "execution_target": result.get("execution_target")}
    metadata = {"source": "literal_shell", "tool_events": [event],
                "trusted": False, "tool_gate_untrusted": True}
    if approval_id:
        metadata["literal_shell_approval_id"] = approval_id
    # The output IS the answer to a literal command: it goes in the reply
    # itself (a fenced block, tail-bounded), not only behind the collapsed
    # tool card, and the head line is the same in every interface language.
    state = ("approval needed" if approval_id else "blocked" if result.get("blocked")
             else f"exit {result.get('exit_code', 1)}")
    shown = output if len(output) <= _REPLY_OUTPUT_CHARS else "…" + output[-_REPLY_OUTPUT_CHARS:]
    fence = "````" if "```" in shown else "```"
    text = f"`$ {command}` · {state}\n\n{fence}text\n{shown.rstrip()}\n{fence}"
    manager.add_message(session.id, ChatMessage("assistant", text, metadata=metadata))
    return {"session_id": session.id, "result": result, "approval_id": approval_id or None}


async def run_command(command: str, *, owner: str, session_id: str = "", workspace: str = "",
                      project_id: str = "", autonomy_preset: str = "") -> dict[str, Any]:
    command = command.strip()
    if not command:
        raise ShellCommandError(400, "A shell command is required.")
    if autonomy_preset not in ("", "supervised", "read_only", "bounded_autonomous"):
        raise ShellCommandError(400, "Unknown autonomy preset.")
    manager = _manager()
    project = None
    if session_id:
        session = _owned_session(manager, session_id, owner)
        owner = str(owner or session.owner or "")
    else:
        if project_id:
            from services.projects import get_store
            project = get_store().get(project_id, owner or None)
            if not project or not project.get("enabled", True):
                raise ShellCommandError(404, "Project not found.")
        session = manager.create_session(str(uuid.uuid4()), "Shell: " + command[:80], "", "",
                                         owner=owner or None, project_id=project_id or None,
                                         folder=(project or {}).get("folder"))
    policy, project = _policy(owner, session, autonomy_preset)
    wanted_workspace = workspace or (project or {}).get("workspace") or ""
    resolved = vet_workspace(wanted_workspace) if wanted_workspace else None
    if wanted_workspace and not resolved:
        raise ShellCommandError(400, "The working folder is not a valid safe directory.")
    security = ToolRunSecurityContext(user_request="!!" + command, workspace=resolved or "",
        run_id="literal_shell:" + ("read_only:" if policy.read_only else "standard:") + uuid.uuid4().hex)
    manager.add_message(session.id, ChatMessage("user", "!!" + command, metadata={"source": "literal_shell"}))
    _, result = await execute_tool_block(ToolBlock(tool_type="bash", content=command),
        owner=owner or None, session_id=session.id, workspace=resolved,
        tool_policy=policy, security_context=security)
    approval_id = ""
    # The dispatcher returns a blocked result; the gate owns whether that
    # denial offers approval (the ordinary agent loop asks this same gate).
    needs_approval = bool(result.get("approval_required"))
    if result.get("blocked") and result.get("policy") == "external_untrusted_context":
        decision = security.decision_for("bash", command)
        needs_approval = not decision.allowed and decision.approval_required
    if needs_approval:
        result = {**result, "approval_required": True}
        existing = pending_for_session(session.id, owner)
        if session.id in tool_approval_store.pending_session_ids(owner=owner) and not existing:
            return _save_result(manager, session, command, {
                "blocked": True, "exit_code": 1,
                "error": "Resolve the session's existing approval before approving this command."})
        pending = tool_approval_store.create(owner=owner, session_id=session.id,
            origin_run_id=security.run_id, tool_name="bash", content=command, workspace=resolved,
            external_untrusted_context_seen=False,
            capabilities=capabilities_for_action("bash", command), continuation_query="!!" + command)
        approval_id = pending.approval_id
    return _save_result(manager, session, command, result, approval_id)


async def answer_approval(approval_id: str, *, owner: str, decision: str) -> dict:
    if decision not in ("approve_action", "deny"):
        raise ShellCommandError(400, "Choose approve_action or deny.")
    pending = tool_approval_store.peek(approval_id)
    if (not pending or not pending.origin_run_id.startswith("literal_shell:") or pending.tool_name != "bash"):
        raise ShellCommandError(404, "Shell approval not found or expired.")
    manager = _manager()
    session = _owned_session(manager, pending.session_id, owner)
    owner = str(owner or session.owner or "")
    if pending.owner != owner:
        raise ShellCommandError(404, "Shell approval not found or expired.")
    policy, _ = _policy(owner, session,
                        "read_only" if pending.origin_run_id.startswith("literal_shell:read_only:") else "")
    claim = tool_approval_store.consume(approval_id, decision=decision, owner=owner,
        session_id=session.id, allow_continuation=False)
    if decision == "deny":
        return _save_result(manager, session, pending.content,
                            {"blocked": True, "exit_code": 1, "error": "The shell command was declined."})
    if claim is None:
        raise ShellCommandError(409, "The approval was already answered.")
    security = ToolRunSecurityContext(user_request=pending.continuation_query,
                                     workspace=pending.workspace or "", run_id=pending.origin_run_id)
    _, result = await execute_tool_block(ToolBlock(tool_type="bash", content=pending.content),
        owner=owner or None, session_id=session.id, workspace=pending.workspace,
        tool_policy=policy, security_context=security, exact_approval=claim)
    return _save_result(manager, session, pending.content, result)
