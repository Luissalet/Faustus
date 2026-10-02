"""Lean mode, pinned per chat.

A local model reads the whole prompt again whenever its prefix changes, so
every block a simple chat does not need costs seconds on every turn, and a
settings change made from another chat must not change the prefix of a chat
that is already running. This module is the one switch for both problems.

* **Mode.** A chat is ``normal`` or ``lean``. Lean turns off, for that chat
  only, the optional prompt blocks and tools a plain conversation does not
  need (``LEAN_DROPS`` names them; the agent loop applies them through
  ``is_lean``) and keeps the core tools (``LEAN_CORE_TOOLS``): read, write and
  edit files, search, the shell, Python, web search and fetch, and asking the
  user. The mode is stored with the session (``sessions.chat_profile``), so it
  survives a restart. A chat that never chose one takes the global default
  ``agent_default_chat_mode`` ("normal" or "lean") at its first turn and keeps
  it from then on.

* **Pin.** When the mode is resolved (first turn, or an explicit ``/mode``),
  the values of the prompt-shaping settings in ``PINNED_SETTINGS`` are frozen
  with it. Readers of those settings go through ``get_setting`` here, which
  answers from the pinned values while a turn of that chat runs and from the
  live settings otherwise. A later change of a global setting therefore
  applies to new chats; ``/mode`` re-resolves the pin explicitly.

What is never pinned: anything that decides what a chat is *allowed* to do
(``disabled_tools``, approval and autonomy settings, the non-admin
denylist). Authorization changes take effect at once, in every chat. Pinning
only freezes how the prompt is shaped.

Nothing in this module reads a model or touches a prompt; it resolves a
profile and exposes it to the turn through a ``ContextVar`` that the agent
loop's entry point sets and resets (``activate`` / ``deactivate``).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import time
from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional, Set, Tuple

logger = logging.getLogger(__name__)

MODE_NORMAL = "normal"
MODE_LEAN = "lean"
MODES: Tuple[str, ...] = (MODE_NORMAL, MODE_LEAN)

#: Global default for a chat that has not chosen a mode ("normal" or "lean").
DEFAULT_SETTING = "agent_default_chat_mode"

PROFILE_VERSION = 1

_ALIASES = {
    "lean": MODE_LEAN, "ligero": MODE_LEAN, "light": MODE_LEAN, "slim": MODE_LEAN,
    "normal": MODE_NORMAL, "full": MODE_NORMAL, "completo": MODE_NORMAL,
}

#: Prompt-shaping settings frozen with the mode. Each one changes the bytes of
#: the prompt or of the tool list a turn sends; none of them is an
#: authorization decision.
PINNED_SETTINGS: Tuple[str, ...] = (
    # which tools carry a schema, and how
    "agent_tool_exposure", "agent_tool_catalog", "agent_tool_schema_slim",
    "agent_tool_preflight", "agent_sticky_toolset", "agent_sticky_toolset_max",
    "agent_context_tools_enabled",
    # optional context blocks
    "agent_repo_map", "agent_repo_map_tokens",
    "agent_mcp_prompt_budget_tokens", "agent_mcp_prompt_full_listing",
    "instincts_enabled",
    "fix_memory_enabled", "fix_memory_auto_recall", "fix_memory_prompt_budget_tokens",
    "agent_project_concepts_inject", "agent_project_concepts_inject_k",
    "skill_list_budget_tokens", "skill_max_injected", "skill_body_budget_tokens",
    "agent_instruction_hierarchy",
    # standing rules and stance
    "agent_harness_checks", "behavior_mode_default",
)

#: What a plain conversation keeps in lean mode. Tools outside this set stay
#: executable only when the request forces them (a delegation the user typed).
LEAN_CORE_TOOLS: FrozenSet[str] = frozenset({
    "read_file", "write_file", "edit_file", "apply_patch",
    "ls", "grep", "glob",
    "bash", "python", "powershell",
    "web_search", "web_fetch",
    "ask_user",
})

#: Human-readable list of what lean mode turns off (id, what). Shown by the
#: API and by ``/mode``; the loop applies each one where the block is built.
LEAN_DROPS: Tuple[Tuple[str, str], ...] = (
    ("skills_index", "the skills index, the matched-skills block and manage_skills"),
    ("repo_map", "the repository map"),
    ("instincts", "learned instincts"),
    ("fix_memory", "recalled fixes from earlier sessions"),
    ("project_concepts", "project concepts injection"),
    ("mcp_tools", "MCP and plugin tools and their prompt blocks"),
    ("tool_catalog", "the tool catalog, lookup_tools and every tool schema outside the core set"),
    ("turn_strategy", "the per-turn strategy and big-task blocks"),
    ("project_blocks", "the project repositories and board blocks"),
)

_MISSING = object()

_PROFILE: "contextvars.ContextVar[Optional[Mapping[str, Any]]]" = contextvars.ContextVar(
    "chat_mode_profile", default=None)


# -- vocabulary ------------------------------------------------------------------

def normalize(value: Any) -> Optional[str]:
    """``"lean"`` / ``"normal"`` for a known word (either language), else None."""
    if not isinstance(value, str):
        return None
    return _ALIASES.get(value.strip().lower())


def default_mode() -> str:
    """The global default for chats that have not chosen: setting, else normal."""
    try:
        from src.settings import get_setting as _live
        return normalize(_live(DEFAULT_SETTING, MODE_NORMAL)) or MODE_NORMAL
    except Exception:  # noqa: BLE001 - an unreadable setting means the old behaviour
        return MODE_NORMAL

# -- settings overlay ---------------------------------------------------------------

def get_setting(key: str, default: Any = None) -> Any:
    """``src.settings.get_setting``, answering from the pinned values while a
    pinned chat's turn runs. Same signature, so a module swaps one import."""
    profile = _PROFILE.get()
    if profile is not None:
        pinned = profile.get("pinned")
        if isinstance(pinned, Mapping) and key in pinned:
            return pinned[key]
    from src.settings import get_setting as _live
    return _live(key, default)


def profile_setting(profile: Optional[Mapping[str, Any]], key: str, default: Any = None) -> Any:
    """A setting as a given profile sees it (for code that runs before the
    turn's ContextVar is set, such as building a chat's context)."""
    pinned = (profile or {}).get("pinned")
    if isinstance(pinned, Mapping) and key in pinned:
        return pinned[key]
    from src.settings import get_setting as _live
    return _live(key, default)


def snapshot() -> Dict[str, Any]:
    """The live values of the pinned settings (a key the settings layer does
    not know is left out, so it keeps following the live value)."""
    from src.settings import get_setting as _live
    out: Dict[str, Any] = {}
    for key in PINNED_SETTINGS:
        try:
            value = _live(key, _MISSING)
        except Exception:  # noqa: BLE001
            continue
        if value is _MISSING:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        out[key] = value
    return out


def pinned_sha256(pinned: Optional[Mapping[str, Any]]) -> str:
    """Short hash of a pin, for logs, metrics and tests."""
    blob = json.dumps(dict(pinned or {}), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# -- profile ----------------------------------------------------------------------

def _record(mode: str, pinned: Mapping[str, Any]) -> Dict[str, Any]:
    return {"v": PROFILE_VERSION, "mode": mode, "pinned": dict(pinned), "pinned_at": int(time.time())}


def _view(record: Mapping[str, Any], *, persisted: bool) -> Dict[str, Any]:
    mode = normalize(record.get("mode")) or MODE_NORMAL
    pinned = record.get("pinned") if isinstance(record.get("pinned"), Mapping) else {}
    return {
        "mode": mode,
        "lean": mode == MODE_LEAN,
        "pinned": dict(pinned),
        "pinned_at": record.get("pinned_at"),
        "pinned_sha256": pinned_sha256(pinned),
        "persisted": bool(persisted),
    }


def stored_profile(session_id: str) -> Optional[Dict[str, Any]]:
    """The session's stored record, or None."""
    sid = str(session_id or "")
    if not sid:
        return None
    from core.database import get_session_chat_profile
    return get_session_chat_profile(sid)


def turn_profile(session_id: Optional[str]) -> Dict[str, Any]:
    """The profile a turn of this chat runs under; pins it on the first turn.

    A stored profile is returned as it is: a later change of the global
    default or of a pinned setting does not reach it. A chat with none
    resolves one now (its stored mode if one was set early, else the global
    default) and stores it. A session id with no row (an incognito wrapper)
    gets the same resolution without persistence.
    """
    sid = str(session_id or "")
    stored = stored_profile(sid)
    if stored and normalize(stored.get("mode")) and isinstance(stored.get("pinned"), Mapping):
        return _view(stored, persisted=True)
    mode = normalize((stored or {}).get("mode")) or default_mode()
    record = _record(mode, snapshot())
    persisted = False
    if sid:
        from core.database import set_session_chat_profile
        persisted = set_session_chat_profile(sid, record)
    return _view(record, persisted=persisted)


def set_mode(session_id: str, mode: str) -> Dict[str, Any]:
    """Set the chat's mode and re-resolve its pin from the live settings."""
    resolved = normalize(mode)
    if resolved is None:
        raise ValueError("mode must be 'lean' or 'normal'")
    record = _record(resolved, snapshot())
    from core.database import set_session_chat_profile
    persisted = set_session_chat_profile(str(session_id or ""), record)
    return _view(record, persisted=persisted)


def describe(session_id: str) -> Dict[str, Any]:
    """Read-only view for the API: never pins."""
    stored = stored_profile(session_id)
    default = default_mode()
    drops = [{"id": i, "what": w} for i, w in LEAN_DROPS]
    if stored and normalize(stored.get("mode")):
        view = _view(stored, persisted=True)
        return {
            "mode": view["mode"], "stored": True, "default": default,
            "pinned": bool(stored.get("pinned")), "pinned_at": view["pinned_at"],
            "pinned_sha256": view["pinned_sha256"], "pinned_keys": sorted(view["pinned"]),
            "drops": drops,
        }
    return {
        "mode": default, "stored": False, "default": default,
        "pinned": False, "pinned_at": None, "pinned_sha256": "", "pinned_keys": [],
        "drops": drops,
    }


# -- the turn's view ---------------------------------------------------------------

def activate(profile: Optional[Mapping[str, Any]]) -> "contextvars.Token":
    """Make ``profile`` the one the running turn reads (None = no profile)."""
    return _PROFILE.set(profile if isinstance(profile, Mapping) else None)


def deactivate(token: "contextvars.Token") -> None:
    try:
        _PROFILE.reset(token)
    except (ValueError, RuntimeError):  # a token from another context
        _PROFILE.set(None)


def active_profile() -> Optional[Mapping[str, Any]]:
    return _PROFILE.get()


def is_lean() -> bool:
    """True while a lean chat's turn runs."""
    profile = _PROFILE.get()
    return bool(profile and profile.get("mode") == MODE_LEAN)


def lean_tool_names(offered: Optional[Iterable[str]], disabled: Iterable[str] = (),
                    keep: Iterable[str] = ()) -> Set[str]:
    """The tools a lean turn keeps out of what normal selection offered.

    Never adds authority: a core tool is kept only if selection offered it
    (``offered=None`` means selection offered everything) and nothing denied
    it; ``keep`` (tools the request forced) survives whatever the set.
    """
    denied = {str(n) for n in (disabled or ())}
    core = set(LEAN_CORE_TOOLS) - denied
    kept = core if offered is None else core & {str(n) for n in offered}
    kept |= {str(n) for n in (keep or ()) if str(n) not in denied}
    return kept


def metrics_block() -> Optional[Dict[str, Any]]:
    """What a turn reports about its profile (None when it ran without one)."""
    profile = _PROFILE.get()
    if not profile:
        return None
    pinned = profile.get("pinned") if isinstance(profile.get("pinned"), Mapping) else {}
    return {"mode": profile.get("mode"),
            "pinned_sha256": profile.get("pinned_sha256") or pinned_sha256(pinned),
            "pinned_keys": len(pinned), "pinned_at": profile.get("pinned_at")}


__all__ = [
    "MODE_NORMAL", "MODE_LEAN", "MODES", "DEFAULT_SETTING", "PINNED_SETTINGS",
    "LEAN_CORE_TOOLS", "LEAN_DROPS", "normalize", "default_mode", "get_setting",
    "profile_setting", "snapshot", "pinned_sha256", "stored_profile", "turn_profile", "set_mode",
    "describe", "activate", "deactivate", "active_profile", "is_lean",
    "lean_tool_names", "metrics_block",
]