"""Shared wire values and scope markers for tool approval continuations."""

from __future__ import annotations

from enum import Enum


# Keep the existing wire values so the current route and no-build frontend do
# not need a second protocol migration. ``approve`` no longer means one action;
# it now selects chat-session scope.
TASK_APPROVAL_DECISION = "approve_task"
CHAT_SESSION_APPROVAL_DECISION = "approve"
# 14-09-2026: remembered per workspace folder across chats
# (src/tool_approval_grants.py). Same grant as the chat scope, kept on disk.
WORKSPACE_APPROVAL_DECISION = "approve_workspace"
DENY_APPROVAL_DECISION = "deny"

# Session.get_context_messages() adds this server-owned marker only when the
# session history contains a matching, resolved chat-session approval.
CHAT_SESSION_APPROVAL_CONTEXT_MARKER = "_tool_approval_chat_session_granted"


class ToolApprovalScope(str, Enum):
    # Surfaces without a resumable chat (the skill tester, unattended audits)
    # keep the original one-use meaning: the sealed action runs and the gate
    # re-arms immediately for anything after it.
    SINGLE_ACTION = "single_action"
    TASK = "task"
    CHAT_SESSION = "chat_session"
    WORKSPACE = "workspace"


def scope_for_decision(decision: object) -> ToolApprovalScope | None:
    normalized = str(decision or "").strip().lower()
    if normalized == TASK_APPROVAL_DECISION:
        return ToolApprovalScope.TASK
    if normalized == CHAT_SESSION_APPROVAL_DECISION:
        return ToolApprovalScope.CHAT_SESSION
    if normalized == WORKSPACE_APPROVAL_DECISION:
        return ToolApprovalScope.WORKSPACE
    return None
