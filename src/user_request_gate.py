"""An action the user asked for in so many words is not an injected one.

The external-context gate (`src.tool_capabilities.ToolRunSecurityContext`)
exists to stop content Faustus did not write -- a web page, an email, a tool
description -- from steering it into acting. It is armed on almost every
agent turn, because the MCP descriptions and the skill index travel in the
untrusted lane on purpose, and from then on every execution waits for a
card. So "Arranca Jobhunter's Hoard" stopped at "Allow this task to
continue?" to ask permission for exactly what had just been asked.

A call passes here only when the user's own latest message names both the
act and its target, word for word, for that one tool: the target is checked
against the call's arguments, not against what the model says it is doing.
Anything the matcher cannot tie to the user's words keeps the gate, and a
tool without a matcher is never let through. `delegate_agents` has the same
rule with its own, stricter comparison (`_user_delegation_allows`).

The user's words come from `user_request_text`: the last user turn that is
neither injected nor marked untrusted, with uploaded file bodies cut off --
an attachment that says "open Jobhunter" is content, not a request.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


def _fold(text: Any) -> str:
    """Lower case, accents off, apostrophes gone, everything else a space."""
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).casefold()
    raw = re.sub(r"['’`]", "", raw)
    return " ".join(re.sub(r"[^\w]+", " ", raw).split())


def user_request_text(messages: Optional[Iterable[Mapping[str, Any]]]) -> str:
    """The user's own words in the latest turn, or "" when there are none."""
    for message in reversed(list(messages or ())):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        if message.get("_agent_injected"):
            continue
        metadata = message.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("trusted") is False:
            continue
        try:
            from src.reply_language import instruction_text_for_language
            return instruction_text_for_language(message.get("content"))
        except Exception:  # noqa: BLE001 - no words is the safe answer
            return ""
    return ""


# Imperatives and infinitives only. A conjugated or negated form ("no la
# arranques", "¿la arrancaste?") is not an order and must not match.
_START = (r"arranca(?:lo|la|me)?", r"arrancar(?:lo|la)?", r"inicia(?:lo|la)?", r"iniciar(?:lo|la)?",
          r"lanza(?:lo|la)?", r"lanzar(?:lo|la)?", r"levanta(?:lo|la)?", r"levantar(?:lo|la)?",
          r"enciende(?:lo|la)?", r"encender(?:lo|la)?", r"ejecuta(?:lo|la)?", r"ejecutar(?:lo|la)?",
          r"abre(?:lo|la|me)?", r"abrir(?:lo|la)?", r"start", r"launch", r"open", r"run", r"boot")
_SHOW = (r"abre(?:lo|la|me)?", r"abrir(?:lo|la)?", r"muestra(?:lo|la|me)?", r"mostrar(?:lo|la)?",
         r"ensena(?:lo|la|me)?", r"ensenar(?:lo|la|me)?", r"ponme", r"open", r"show", r"display",
         r"bring up")
_OPEN = (r"abre(?:lo|la|me)?", r"abrir(?:lo|la)?", r"open")
_NEGATION = {"no", "nunca", "jamas", "ni", "dont", "not", "never"}


def _ordered(text: str, verbs: Iterable[str]) -> bool:
    """One of `verbs` appears as a word and is not negated just before."""
    for verb in verbs:
        for match in re.finditer(rf"\b{verb}\b", text):
            before = text[: match.start()].split()[-2:]
            if not (set(before) & _NEGATION):
                return True
    return False


def _names_for(plugin: Any) -> list:
    """How a person refers to this plugin: its id, its full name, and the
    name without its trailing word when that leaves something distinctive
    ("Jobhunter's Hoard" -> "jobhunters", "jobhunter")."""
    names = {_fold(getattr(plugin, "id", "")), _fold(getattr(plugin, "name", ""))}
    full = _fold(getattr(plugin, "name", ""))
    parts = full.split()
    if len(parts) > 1:
        head = parts[0]
        names.add(head)
        if head.endswith("s") and len(head) > 5:
            names.add(head[:-1])
    return [n for n in names if len(n) >= 4]


def _plugin_app(user_text: str, content: Any) -> bool:
    from src.agent_tools.plugin_tools import _args
    from src import plugins as plugins_mod

    args = _args(content)
    ref = str(args.get("plugin") or args.get("name") or args.get("id") or "").strip()
    action = str(args.get("action") or "start").strip().lower()
    if not ref or action not in ("start", "show", "start_and_show"):
        return False
    known = plugins_mod.load_plugins()
    plugin = known.get(ref)
    if plugin is None:
        low = ref.lower()
        plugin = next((p for p in known.values() if p.name.lower() == low), None)
    if plugin is None:
        # A connector id: resolve through the same function the tool uses,
        # so the gate and the tool cannot disagree about the target.
        try:
            from src.plugin_runtime import resolve
            plugin = resolve(ref).get("plugin")
        except Exception:  # noqa: BLE001
            plugin = None
    if plugin is None:
        return False

    text = _fold(user_text)
    if not any(re.search(rf"\b{re.escape(name)}\b", text) for name in _names_for(plugin)):
        return False
    if action == "start":
        return _ordered(text, _START)
    if action == "show":
        return _ordered(text, _SHOW)
    # start_and_show: "open it" asks for both; otherwise both must be asked.
    return _ordered(text, _OPEN) or (_ordered(text, _START) and _ordered(text, _SHOW))


#: tool name -> matcher(user_text, call_content). A tool that is not here is
#: never let through by this rule.
MATCHERS: Dict[str, Callable[[str, Any], bool]] = {
    "plugin_app": _plugin_app,
}


def allows(tool_name: Any, content: Any, user_text: str) -> bool:
    """True when the user's latest message asked for exactly this call."""
    matcher = MATCHERS.get(tool_name) if isinstance(tool_name, str) else None
    if matcher is None or not str(user_text or "").strip():
        return False
    try:
        ok = bool(matcher(user_text, content))
    except Exception as exc:  # noqa: BLE001 - a matcher failure keeps the gate
        logger.info("[gate] user request matcher for %s failed: %s", tool_name, exc)
        return False
    if ok:
        logger.info("[gate] %s is what the user asked for in this turn: passes the gate", tool_name)
    return ok
