"""Per-turn tool policy composition for agent execution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Set, Tuple


GUIDE_ONLY_DIRECTIVE = (
    "## GUIDE-ONLY MODE - TOOL POLICY\n"
    "The latest user turn explicitly forbids tool use. Do not call tools, do not "
    "run shell commands, and do not inspect local files or the environment. "
    "Respond in normal text by guiding the user or asking them to paste the "
    "output they will produce locally."
)

WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})
MCP_ONLY_HELPERS = frozenset({"lookup_tools", "todowrite", "update_plan", "ask_user"})


@lru_cache(maxsize=1)
def _mcp_helper_policy_names() -> frozenset[str]:
    # The gate checks registered aliases and semantic keys as well as the
    # surface spelling. They must have the same scope decision.
    from src.tool_security import email_tool_policy_names
    return frozenset(name for helper in MCP_ONLY_HELPERS for name in email_tool_policy_names(helper))


MCP_ONLY_DIRECTIVE = (
    "## MCP-ONLY TOOL POLICY\n"
    "The user restricted this turn to MCP tools. Use lookup_tools to find them. "
    "Native shell, Python, filesystem, web and delegation tools are disabled; "
    "do not simulate a service or use a fallback through them. If the MCP service "
    "is unavailable, report that limitation. Existing approval requirements still apply."
)


def detect_mcp_only_turn(message: object) -> bool:
    """Recognise explicit scope restrictions in the user's own prose only."""
    from src.reply_language import instruction_text_for_language
    text = instruction_text_for_language(message)
    # Code, quotations and quoted lines are reference data, not authorization.
    text = re.sub(r"```[\s\S]*?```|`[^`]*`|\"[^\"]*\"|“[^”]*”|(?<!\w)'[^']*'", "", text)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r"\s+", " ", text).lower()
    exclusive = (
        r"\b(?:usa|use|utiliza|utilice)\s+(?:solo|sólo|únicamente|exclusivamente|only)\s+"
        r"(?:las?\s+)?(?:herramientas?\s+(?:nativas?\s+)?|native\s+tools?\s+)?mcp\b"
        r"|\bonly\s+(?:native\s+)?mcp\s+tools?\b"
    )
    scope_requested = any(
        not re.search(r"(?:\bnot|\bno|\bnever|\bdon't|\bdo not)\s*(?:use\s*)?$", text[:match.start()])
        for match in re.finditer(exclusive, text)
    )
    return bool(scope_requested or (
        re.search(r"\bmcp\b", text)
        and re.search(r"\bno uses archivos,?\s*shell ni otros servicios\b", text)
    ))


def tool_toggle_enabled(value: object) -> bool:
    """Return true only for explicit true-like tool toggle values."""

    return str(value).lower() == "true"


def tool_toggle_explicitly_denied(value: object) -> bool:
    """Return true when a caller explicitly supplied a non-true toggle value."""

    return value is not None and not tool_toggle_enabled(value)


def is_web_search_explicitly_denied(allow_web_search: object) -> bool:
    """Whether the web-search agent toggle was explicitly set to false."""

    return tool_toggle_explicitly_denied(allow_web_search)


def web_search_enabled_for_turn(allow_web_search: object, use_web: object = None) -> bool:
    """Return true only when this request explicitly enables web search.

    Agent mode sends ``allow_web_search``; chat-mode pre-search sends
    ``use_web``. If both are present, an explicit ``allow_web_search=false``
    wins so a stale or conflicting intent path cannot re-enable web tools.
    """

    if is_web_search_explicitly_denied(allow_web_search):
        return False
    return tool_toggle_enabled(allow_web_search) or tool_toggle_enabled(use_web)


_COMMON_TOOL_NAMES = {
    "api_call",
    "app_api",
    "archive_email",
    "ask_teacher",
    "ask_user",
    "bash",
    "bulk_email",
    "builtin_browser",
    "builtin_devtools",
    "cancel_download",
    "chat_with_model",
    "create_document",
    "create_session",
    "delete_email",
    "download_model",
    "edit_document",
    "edit_file",
    "edit_image",
    "generate_image",
    "glob",
    "grep",
    "list_cached_models",
    "list_cookbook_servers",
    "list_downloads",
    "list_emails",
    "list_models",
    "list_serve_presets",
    "list_served_models",
    "list_sessions",
    "ls",
    "manage_calendar",
    "manage_contact",
    "review_candidature_mail",
    "whatsapp_read",
    "whatsapp_send",
    "whatsapp_react",
    "manage_documents",
    "manage_endpoints",
    "manage_mcp",
    "manage_memory",
    "manage_notes",
    "manage_research",
    "manage_session",
    "manage_settings",
    "manage_skills",
    "manage_tasks",
    "manage_tokens",
    "manage_webhooks",
    "mark_email_read",
    "pipeline",
    "powershell",
    "python",
    "read_email",
    "read_file",
    "reply_to_email",
    "resolve_contact",
    "search_chats",
    "search_project_chats",
    "project_context",
    "manage_project_context",
    "search_hf_models",
    "send_email",
    "send_to_session",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "suggest_document",
    "trigger_research",
    "ui_control",
    "update_document",
    "update_plan",
    "vault_get",
    "vault_search",
    "vault_unlock",
    "web_fetch",
    "web_search",
    "write_file",
}


_GUIDE_ONLY_PATTERNS: Tuple[Tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\bguide[-\s]?only mode\b", "guide-only mode requested"),
        (r"\bno[-\s]?tools? mode\b", "no-tools mode requested"),
        (r"\bdo not use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bdon'?t use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to use (?:any )?tools?\b", "user forbade tool use"),
        (r"\bnot allowed to:?.{0,120}\buse (?:any )?tools?\b", "user forbade tool use"),
        (r"\bask (?:me )?(?:for confirmation )?before using tools?\b", "user requested confirmation before tools"),
        (r"\bno uses (?:ninguna |más )?herramientas?\b", "user forbade tool use"),
        (r"\bno ejecutes (?:más |ninguna )?herramientas?\b", "user forbade tool use"),
    )
)


@dataclass(frozen=True)
class ToolPolicy:
    """Effective tool behavior for one agent turn."""

    disabled_tools: frozenset[str] = frozenset()
    hidden_tools: frozenset[str] = frozenset()
    reasons: Mapping[str, str] = field(default_factory=dict)
    mode: str = "normal"
    block_all_tool_calls: bool = False
    disable_mcp: bool = False
    read_only: bool = False

    def all_disabled_names(self) -> Set[str]:
        return set(self.disabled_tools) | set(self.hidden_tools)

    def blocks(self, tool_name: Optional[str]) -> bool:
        if not tool_name:
            return False
        scope_denied = (self.mode == "mcp_only" and not tool_name.startswith("mcp__")
                        and tool_name not in _mcp_helper_policy_names())
        named_denial = scope_denied or self.block_all_tool_calls or tool_name in self.disabled_tools or tool_name in self.hidden_tools
        if named_denial or not self.read_only:
            return named_denial
        from src.autonomy_budget import is_read_only_tool
        return not is_read_only_tool(tool_name)

    def blocks_action(self, tool_name: Optional[str], content: object) -> bool:
        """Recheck effects at execution, even for an unenumerated tool."""
        if replace(self, read_only=False).blocks(tool_name):
            return True
        if not self.read_only:
            return False
        from src.autonomy_budget import is_read_only_action
        return not is_read_only_action(tool_name, content)

    def reason_for(self, tool_name: Optional[str]) -> str:
        if tool_name and tool_name in self.reasons:
            return self.reasons[tool_name]
        if self.block_all_tool_calls and self.mode == "guide_only":
            return "Tool use is disabled for this guide-only turn."
        if self.mode == "mcp_only":
            return "The user restricted this turn to MCP tools; native fallbacks are disabled."
        if self.read_only:
            return "The read-only autonomy preset forbids this tool action."
        return "Tool use is disabled for this turn."

    def exempting(self, names: Iterable[str]) -> "ToolPolicy":
        """This policy with `names` lifted out of every denial it carries.

        The loop's workspace floor and this policy are two answers to one
        question — "may the agent read a file in the folder it is bound to?" —
        and until this existed they could give different answers to the same
        turn. The floor decided what the model was SHOWN; the policy decided
        what it could RUN. Live, with `read_file` on both the floor and the
        route's denylist, the model was handed a `read_file` schema and then
        told "Tool is disabled for this request." eight times in one turn.

        Offering a tool and then refusing it is worse than never offering it:
        the schema is a promise, and a 9B model spends the whole turn trying to
        get through a wall the same runtime advertised a door in. So the floor
        is reconciled into the policy ONCE, before either surface is built, and
        both then read the same set.

        This never widens an authorization. The caller hands in a set from
        which every authorization denial has already been subtracted — see
        `_resolve_workspace_floor` in `src/agent_loop.py`, which drops
        guide-only, block-all, the non-admin denylist, plan mode's read-only
        allowlist and the operator's own `disabled_tools` setting before the
        floor exists at all. And a turn that blocks every tool outright is
        returned unchanged here as well, because there is no exemption from
        "no tools at all".
        """
        if self.block_all_tool_calls or self.mode == "mcp_only":
            return self
        exempt = {str(name) for name in (names or ()) if name}
        if not exempt or not (exempt & self.all_disabled_names()):
            return self
        return ToolPolicy(
            disabled_tools=frozenset(self.disabled_tools - exempt),
            hidden_tools=frozenset(self.hidden_tools - exempt),
            reasons=MappingProxyType(
                {k: v for k, v in self.reasons.items() if k not in exempt}
            ),
            mode=self.mode,
            block_all_tool_calls=self.block_all_tool_calls,
            disable_mcp=self.disable_mcp,
            read_only=self.read_only,
        )


def detect_guide_only_turn(message: object) -> Optional[str]:
    """Return a reason when the latest user turn strongly requests no tools."""

    if not isinstance(message, str) or not message.strip():
        return None
    text = re.sub(r"\s+", " ", message.strip())
    for pattern, reason in _GUIDE_ONLY_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def known_tool_names() -> Set[str]:
    """Best-effort set of native tool names for prompt hiding and denylisting."""

    names = set(_COMMON_TOOL_NAMES)
    try:
        from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

        for schema in FUNCTION_TOOL_SCHEMAS:
            name = (schema.get("function") or {}).get("name") or schema.get("name")
            if name:
                names.add(name)
    except Exception:
        pass
    try:
        from src.agent_loop import TOOL_SECTIONS

        names.update(TOOL_SECTIONS.keys())
    except Exception:
        pass
    try:
        from src.tool_security import PLAN_MODE_READONLY_TOOLS, _PLAN_MODE_KNOWN_MUTATORS

        names.update(PLAN_MODE_READONLY_TOOLS)
        names.update(_PLAN_MODE_KNOWN_MUTATORS)
    except Exception:
        pass
    return names


def build_effective_tool_policy(
    *,
    disabled_tools: Optional[Iterable[str]] = None,
    last_user_message: object = "",
) -> ToolPolicy:
    """Compose the effective policy for one agent turn.

    Existing callers still provide the already-composed disabled-tool denylist.
    This function adds higher-level turn policy on top so enforcement is not
    delegated to prompt compliance.
    """

    disabled = {str(t) for t in (disabled_tools or []) if t}
    hidden: Set[str] = set()
    reasons = {tool: "Tool is disabled for this request." for tool in disabled}

    guide_reason = detect_guide_only_turn(last_user_message)
    if guide_reason:
        all_tools = known_tool_names()
        disabled.update(all_tools)
        hidden.update(all_tools)
        reasons.update({tool: f"{guide_reason}." for tool in all_tools})
        return ToolPolicy(
            disabled_tools=frozenset(disabled),
            hidden_tools=frozenset(hidden),
            reasons=MappingProxyType(dict(reasons)),
            mode="guide_only",
            block_all_tool_calls=True,
            disable_mcp=True,
        )

    if detect_mcp_only_turn(last_user_message):
        native = {name for name in known_tool_names()
                  if not name.startswith("mcp__") and name not in _mcp_helper_policy_names()}
        disabled.update(native)
        hidden.update(native)
        reasons.update({name: "The user restricted this turn to MCP tools." for name in native})
        return ToolPolicy(disabled_tools=frozenset(disabled), hidden_tools=frozenset(hidden),
                          reasons=MappingProxyType(reasons), mode="mcp_only")

    return ToolPolicy(
        disabled_tools=frozenset(disabled),
        hidden_tools=frozenset(hidden),
        reasons=MappingProxyType(dict(reasons)),
    )
