"""Server-side tool safety policy."""

from __future__ import annotations

import logging
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Machine-readable error codes
# ---------------------------------------------------------------------------
#
# The browser used to infer *why* a request failed by searching the error text
# for the substring "tool". Every message the approval gate raises contains
# that word, so the three explanations that exist to tell a user why their
# action was refused were exactly the ones that got swallowed — replaced by a
# fabricated "this model doesn't support agent tools", plus a persisted mode
# switch the user never asked for.
#
# The cure is a code the server states outright. Text is for humans and may be
# reworded freely; the code is the contract the client branches on. A client
# that does not recognise a code must fall back to *showing the message*, never
# to guessing.

#: The endpoint/model genuinely cannot run agent tools. This is the ONLY signal
#: that may move a user out of agent mode. It is deliberately narrow: see
#: ``agent_tools_unsupported_reason`` for why almost nothing qualifies.
MODEL_NO_TOOLS_CODE = "model_no_tools"

#: The approval id is unknown to this owner/session (never issued, already
#: consumed, or another thread's).
TOOL_APPROVAL_INVALID_CODE = "tool_approval_invalid"

#: The approval existed and its TTL elapsed before the user answered. Distinct
#: from "invalid" because it is the one refusal with an obvious way out: rerun
#: the turn and answer the fresh card.
TOOL_APPROVAL_EXPIRED_CODE = "tool_approval_expired"

#: Plan mode was switched on between the card appearing and the click.
TOOL_APPROVAL_PLAN_MODE_CODE = "tool_approval_plan_mode"

#: The approval passed the ownership check but could not be consumed (a race
#: with another tab, or a superseding turn).
TOOL_APPROVAL_UNAVAILABLE_CODE = "tool_approval_unavailable"


def tool_error_detail(code: str, message: str) -> dict:
    """Build an HTTPException detail carrying both a code and a message.

    FastAPI serialises this as ``{"detail": {"code": ..., "message": ...}}``.
    The message stays first-class: a client that cannot interpret the code
    still has something true to show.
    """
    return {"code": str(code or ""), "message": str(message or "")}


def error_code(detail: Any) -> str:
    """Read the machine-readable code out of an error detail, if any.

    Plain-string details (every other ``HTTPException`` in the app) have no
    code — they return ``""`` and are handled by showing the message.
    """
    if isinstance(detail, dict):
        return str(detail.get("code") or "")
    return ""


def error_message(detail: Any) -> str:
    """Read the human-readable message out of an error detail."""
    if isinstance(detail, dict):
        return str(detail.get("message") or "")
    return str(detail or "")


def agent_tools_unsupported_reason(
    *,
    endpoint_supports_tools: Optional[bool] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """Why this endpoint/model cannot run agent tools at all, or ``None``.

    This is the authoritative answer to the question the browser used to guess
    at. It returns ``None`` today for every endpoint the app can reach, and
    that is the correct answer rather than a gap:

    ``ModelEndpoint.supports_tools is False`` (set by the operator in Settings,
    or derived from a provider's model list) and the no-native-tools model list
    in ``src/agent_loop._resolve_native_tool_support`` both rule out *native*
    function calling only. When they fire, the agent loop drops to XML tool
    fences, which every text model can emit — the run still works, tools and
    all. So neither fact justifies taking a user out of agent mode, and the
    live evidence agrees: no ``/api/chat_stream`` failure has ever meant "this
    model can't do tools".

    The seam exists so that a provider which truly refuses to carry tool output
    can say so here, in one place, instead of every caller re-deriving it from
    error prose.
    """
    # Explicitly evaluated and explicitly not disqualifying — keep the inputs
    # named so the reasoning above stays attached to real values.
    _ = (endpoint_supports_tools, str(model or "").strip().lower())
    return None


# Every tool exposed by the built-in email MCP server
# (mcp_servers/email_server.py). Single source of truth: the fence tags
# (TOOL_TAGS), bare-name dispatch (tool_execution), native-call mapping
# (tool_schemas), and the non-admin blocklist below all derive from this set,
# so a tool added to the email server can't become reachable under its bare
# name without also being blocked for non-admins.
BUILTIN_EMAIL_TOOLS = frozenset({
    "list_email_accounts",
    "list_emails",
    "read_email",
    "search_emails",
    "scan_email_unsubscribes",
    "unsubscribe_email",
    "send_email",
    "reply_to_email",
    "draft_email",
    "draft_email_reply",
    "ai_draft_email_reply",
    "archive_email",
    "delete_email",
    "mark_email_read",
    "bulk_email",
    "download_attachment",
})


# Tools regular/public users must not execute directly. These either expose
# server/runtime access, sensitive user data, external messaging, persistent
# state changes, or generic loopback/integration surfaces. All email tools are
# included (SECURITY.md: email/MCP capabilities are privileged admin
# functionality).
NON_ADMIN_BLOCKED_TOOLS = BUILTIN_EMAIL_TOOLS | {
    "bash",
    "python",
    "delegate_agents",
    "manage_bg_jobs",
    "read_file",
    "write_file",
    "edit_file",
    "apply_patch",
    "grep",
    "glob",
    "ls",
    "get_workspace",
    "search_chats",
    "search_project_chats",
    "project_context",
    "manage_memory",
    "manage_skills",
    "manage_tasks",
    "manage_endpoints",
    "manage_mcp",
    "manage_webhooks",
    "manage_tokens",
    "manage_documents",
    "manage_settings",
    "api_call",
    "app_api",
    "resolve_contact",
    "manage_contact",
    "manage_calendar",
    "vault_search",
    "vault_get",
    "vault_unlock",
    "download_model",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "cancel_download",
    "adopt_served_model",
    # Desktop control (FAUSTUS): sees and drives the screen of the machine
    # the server runs on — the owner's desktop, never a public user's.
    "desktop_screenshot",
    "desktop_list_windows",
    "desktop_focus_window",
    "desktop_click",
    "desktop_type",
    "desktop_key",
    "desktop_scroll",
}


# Plan mode: the agent may investigate but must not mutate anything. Only these
# read-only/inspection tools stay enabled; everything else (writes, sends,
# manage_*, model serving, MCP, etc.) is blocked. Allowlist rather than blocklist
# so any newly added tool defaults to BLOCKED in plan mode — fail safe.
#
# bash/python are deliberately NOT here: the shell can mutate (write files, hit
# the network) and can't be constrained to read-only at the tool layer, so plan
# mode blocks it outright rather than relying on a prompt to keep it well-behaved.
# Code/file discovery is covered by the dedicated read-only tools below
# (read_file, grep, glob, ls) instead of freestyle shell.
PLAN_MODE_READONLY_TOOLS = {
    "read_file",
    "grep",
    "glob",
    "ls",
    "get_workspace",
    "web_search",
    "web_fetch",
    "search_chats",
    "search_project_chats",
    "project_context",
    "list_models",
    "list_sessions",
    # Read-only email tools. list_email_accounts must be here because the
    # bare/qualified alias gate in execute_tool_block works both ways: it has
    # a native function schema, so plan mode's schema-derived bare denylist
    # contains it — and without this allowlist entry that bare entry would
    # also block the qualified mcp__email__list_email_accounts call that the
    # MCP read-only filter deliberately allows.
    "list_email_accounts",
    "list_emails",
    "read_email",
    # Explicitly read-only rather than allowed-by-omission: this PR makes
    # every BUILTIN_EMAIL_TOOLS name fence-taggable, so each one must be
    # classified — see the plan-mode partition test in
    # tests/test_email_registry_sync.py.
    "search_emails",
    "scan_email_unsubscribes",
    "list_served_models",
    "list_downloads",
    "list_cached_models",
    "search_hf_models",
    "list_serve_presets",
    "list_cookbook_servers",
    "resolve_contact",
    "chat_with_model",
    "ask_teacher",
}


# The agent's tool gate is a DENYLIST: execute_tool_block blocks any tool whose
# name is in `disabled_tools`. Plan mode's policy is the opposite — an allowlist
# (PLAN_MODE_READONLY_TOOLS). To apply an allowlist through a denylist, plan mode
# returns the inverse: every known tool name minus the allowlist.
#
# Known tool names come from FUNCTION_TOOL_SCHEMAS, but that source is imperfect:
# some tools are only XML-invocable (e.g. manage_notes, generate_image) and never
# appear there, and the import can fail outright. Either gap would drop a mutating
# tool from the subtraction and silently leave it enabled. This set is the static
# backstop for both: union it in so known mutators are always subtracted, and so a
# failed import still blocks them (fail closed, never open). Only mutators belong
# here — read-only tools are covered by the allowlist. Keep in sync when adding
# new mutating tools.
_PLAN_MODE_KNOWN_MUTATORS = {
    "write_file", "edit_file", "apply_patch", "todowrite", "delegate_agents",
    "create_document", "edit_document", "update_document",
    "suggest_document", "manage_documents", "create_session", "manage_session",
    "send_to_session", "pipeline", "manage_memory", "manage_skills",
    "manage_tasks", "manage_notes", "manage_endpoints", "manage_mcp",
    "manage_webhooks", "manage_tokens", "manage_settings", "manage_contact",
    "manage_calendar", "api_call", "app_api", "ui_control",
    "send_email", "reply_to_email", "bulk_email", "delete_email",
    "archive_email", "mark_email_read", "unsubscribe_email",
    # The draft tools create documents and download_attachment writes to
    # disk — mutating. They have no native schemas (yet), so without these
    # static entries plan-mode safety for their bare fence tags would depend
    # entirely on the MCP read-only inventory being present and current.
    "draft_email", "draft_email_reply", "ai_draft_email_reply",
    "download_attachment",
    "download_model", "serve_model",
    "stop_served_model", "cancel_download", "adopt_served_model", "serve_preset",
    "generate_image", "edit_image", "trigger_research", "manage_research",
    # Shell is never read-only-safe; block it explicitly so it stays out of plan
    # mode even if the schema list fails to load.
    "bash", "python",
    # Controls shell processes (kill); plan mode can't run bash anyway.
    "manage_bg_jobs",
}


def plan_mode_disabled_tools() -> Set[str]:
    """Tool names to add to the denylist in plan mode.

    Plan mode allows only PLAN_MODE_READONLY_TOOLS. The gate is a denylist, so
    return the inverse: every known tool name minus the allowlist. Known names
    come from the function-tool schemas, backstopped by _PLAN_MODE_KNOWN_MUTATORS
    (see above) so XML-only tools and a failed schema import can't leave a mutator
    enabled. MCP tools are handled separately — the loop drops the MCP manager
    entirely in plan mode."""
    try:
        # agent_tools / tool_parsing / tool_schemas form a mutually-circular
        # cluster that only resolves cleanly when entered via agent_tools.
        # Import it first so the lazy schema import works even from a cold
        # import (e.g. tests) — not just after the app has wired everything up.
        import src.agent_tools  # noqa: F401
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

        all_names = {
            (t.get("function") or {}).get("name")
            for t in FUNCTION_TOOL_SCHEMAS
        }
        all_names.discard(None)
    except Exception as exc:
        logger.warning("Unable to load tool schemas for plan-mode gating: %s", exc)
        all_names = set()
    # Subtract the allowlist from all known tool names (schema-derived plus the
    # static mutator backstop). Fail closed: if the schema import failed above,
    # the backstop alone still blocks known mutators.
    return (all_names | _PLAN_MODE_KNOWN_MUTATORS) - PLAN_MODE_READONLY_TOOLS


def email_tool_policy_names(tool_name: str) -> frozenset:
    """All policy-equivalent spellings of a tool name.

    A bare built-in email tool name and its MCP-qualified mcp__email__<name>
    form dispatch to the same email server tool, but policy sources spell
    them either way — plan mode and the MCP settings toggle write qualified
    names into denylists, chat-level toggles write bare ones. Every gate must
    match against the full alias set, or a call in one spelling slips past a
    denylist entry written in the other. Non-email names alias only to
    themselves.
    """
    if not isinstance(tool_name, str):
        return frozenset((tool_name,))
    if tool_name in BUILTIN_EMAIL_TOOLS:
        return frozenset((tool_name, f"mcp__email__{tool_name}"))
    if tool_name.startswith("mcp__email__"):
        bare = tool_name[len("mcp__email__"):]
        if bare in BUILTIN_EMAIL_TOOLS:
            return frozenset((tool_name, bare))
    return frozenset((tool_name,))


def is_public_blocked_tool(tool_name: Optional[str]) -> bool:
    """Return True when a non-admin/public user must not execute this tool.

    This is a security gate, so it fails CLOSED: a malformed non-string tool
    name can't be matched against the blocklist or the ``mcp__`` namespace, so
    it is treated as blocked rather than silently allowed through. ``None`` /
    empty string means there is no tool to gate.
    """
    if tool_name is None or tool_name == "":
        return False
    if not isinstance(tool_name, str):
        return True
    return tool_name in NON_ADMIN_BLOCKED_TOOLS or tool_name.startswith("mcp__")


def owner_is_admin_or_single_user(owner: Optional[str]) -> bool:
    """Return True for admins, or in intentional single-user mode.

    Single-user mode means the operator explicitly disabled auth
    (``AUTH_ENABLED=false``) — the local/self-host default where the owner has
    full access to their own box.

    The pre-setup window (auth ENABLED but no admin created yet) is treated as
    NON-admin: returning True there would hand server-execution tools
    (``bash``/``python``) to any caller before setup completes. The auth
    middleware already 401s ``/api/`` requests pre-setup, so this is
    defense-in-depth for callers that bypass it (e.g. trusted loopback).
    """
    try:
        from src.auth_helpers import _auth_disabled

        if _auth_disabled():
            return True

        from core.auth import AuthManager

        auth = AuthManager()
        if not auth.is_configured:
            return False
        return bool(owner and auth.is_admin(owner))
    except Exception as exc:
        logger.warning("Unable to evaluate owner admin status: %s", exc)
        return False


def blocked_tools_for_owner(owner: Optional[str]) -> Set[str]:
    """Tools to hide/disable for this owner under public-user policy."""
    if owner_is_admin_or_single_user(owner):
        return set()
    return set(NON_ADMIN_BLOCKED_TOOLS)
