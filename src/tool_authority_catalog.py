"""Registers every tool the authority does not own yet (H05) and sets exposure (H17).

`src.tool_authority_specs` registers the migrated families as `authored` or
`contract` tools. This module registers the rest as `wrapped`: the descriptor
is read from the catalogue that still owns the spec (`FUNCTION_TOOL_SCHEMAS`
for native schemas, `BUILTIN_TOOL_DESCRIPTIONS` for fence-only tools), effects
stay in `src.tool_capabilities`, and the parser contract is NOT claimed
(`origin="wrapped"` means `encode_native` is not available and the parity
report does not assert it). What every tool gets here is what the runtime
needs from any descriptor: a canonical id, its aliases, limits, resources and
an exposure.

Exposure defaults live in one table, `DEFERRED_FAMILIES`: heavy, narrow tools
that are listed in the compact catalog instead of carrying a native schema
every turn unless the request names them. The table only decides a default;
a registration can carry any exposure and a setting turns the use of it off.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from src.tool_authority import (
    AUTHORITY, Exposure, ParserContract, ToolAuthorityError, ToolEffects, ToolLimits, ToolResources, make_tool,
)

logger = logging.getLogger(__name__)

#: family name -> tool-name predicate. A tool matching no family is DIRECT.
#: These families are large schemas used by a narrow kind of request; the
#: request that needs them names them (or a keyword hint does), and every
#: other turn loses their schema but keeps a one-line catalog entry.
DEFERRED_FAMILIES = {
    "code_graph": re.compile(r"^code_graph_"),
    "swarm": re.compile(r"^swarm_"),
    "fanout": re.compile(r"^fanout_"),
    "alternatives": re.compile(r"^alt_(start|compare|apply)$"),
    "requirements": re.compile(r"^req_"),
    "project_concepts": re.compile(r"^concepts?_"),
    "goals": re.compile(r"^goal_"),
    "plan_tracker": re.compile(r"^plan_(status|task|done|skip|next)$"),
    "project_board": re.compile(r"^(board_|meeting_actions_to_board$)"),
    "autonomy": re.compile(r"^(night_shift|bug_hunt|ci_failures|prior_art|doc_claims_check|recall_fixes)$"),
    "structural": re.compile(r"^structural_(search|rewrite)$"),
    "scene_3d": re.compile(r"^blender_scene$"),
    "documents_ops": re.compile(r"^(pdf_ops|page_prune|check_score|research_podcast|design_canvas)$"),
    "self_management": re.compile(r"^(manage_instincts|memory_rules|branch_futures|capability_health|manage_teach_mode)$"),
    "messaging": re.compile(r"^whatsapp_"),
}


def default_family(name: str) -> str:
    for family, pattern in DEFERRED_FAMILIES.items():
        if pattern.search(name):
            return family
    for prefix in ("git_", "desktop_", "context_", "browser_", "manage_", "list_", "email_"):
        if name.startswith(prefix):
            return prefix.rstrip("_")
    return "legacy"


def default_exposure(name: str) -> Exposure:
    return Exposure.DEFERRED if any(p.search(name) for p in DEFERRED_FAMILIES.values()) else Exposure.DIRECT


def _no_native_parser(args) -> str:
    raise ToolAuthorityError("this tool is wrapped: its native parser is still owned by src.tool_schemas")


_WRAPPED_PARSER = ParserContract(_no_native_parser)
_ready = False


def _builtin_names() -> list:
    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = set(TOOL_TAGS)
    for entry in FUNCTION_TOOL_SCHEMAS:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            names.add(fn["name"])
    return sorted(names)


def ensure_catalog() -> None:
    """Register a `wrapped` entry for every builtin tool not yet registered.

    Idempotent. Safe to call from any thread after import; the registry only
    grows, and each registration is validated.
    """
    global _ready
    if _ready:
        return
    import src.tool_authority_specs  # noqa: F401  (migrated families first)
    from src.agent_tools import TOOL_HANDLERS  # noqa: F401  (import order: handlers first)
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_parsing import _TOOL_NAME_MAP
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    native = {}
    for entry in FUNCTION_TOOL_SCHEMAS:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            native[fn["name"]] = fn
    by_target: dict = {}
    for alias, target in _TOOL_NAME_MAP.items():
        if alias != target:
            by_target.setdefault(target, []).append(alias)

    for name in _builtin_names():
        if name in AUTHORITY:
            continue
        fn = native.get(name) or {}
        description = str(fn.get("description") or BUILTIN_TOOL_DESCRIPTIONS.get(name) or "")
        parameters = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}}
        aliases = [a for a in by_target.get(name, []) if not AUTHORITY.is_spelling(a) and re.match(r"^[A-Za-z][A-Za-z0-9_\-]*$", a)]
        try:
            AUTHORITY.register(make_tool(
                name=name, canonical_id=f"legacy.{name.lower()}", family=default_family(name),
                description=description, parameters=parameters, parser=_WRAPPED_PARSER,
                effects=ToolEffects(), resources=ToolResources(), limits=ToolLimits(),
                exposure=default_exposure(name), aliases=aliases, origin="wrapped"))
        except ToolAuthorityError as exc:
            # A name the descriptor rules refuse stays unregistered (DIRECT by
            # default) rather than breaking the catalogue; the parity test
            # lists any builtin that ends up here.
            logger.warning("tool authority: %s not registered (%s)", name, exc)
    _ready = True


def unregistered_builtins() -> list:
    """Builtin names that could not be registered (empty when healthy)."""
    ensure_catalog()
    return [name for name in _builtin_names() if name not in AUTHORITY]


def exposure_map() -> dict:
    ensure_catalog()
    return AUTHORITY.exposure_map()


def policy_spellings(name: str) -> frozenset:
    """Every spelling a revocation of `name` must cover: its authority
    spellings (wire name, canonical id, aliases) and the email MCP pair."""
    from src.tool_security import email_tool_policy_names
    out = set(AUTHORITY.spellings(name)) | set(email_tool_policy_names(name))
    return frozenset(out)


def families(names: Optional[Iterable[str]] = None) -> dict:
    ensure_catalog()
    out: dict = {}
    for name in (names if names is not None else AUTHORITY.names()):
        tool = AUTHORITY.get(name)
        if tool is not None:
            out.setdefault(tool.family, []).append(name)
    return {k: sorted(v) for k, v in sorted(out.items())}


def install_populator() -> None:
    """Make the authority fill in the wrapped catalogue on its first lookup."""
    AUTHORITY.set_populator(ensure_catalog)
