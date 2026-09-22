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
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)


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

    if plugin.id not in {p.id for p in plugins_mod.named_in(user_text)}:
        return False
    text = plugins_mod.fold(user_text)
    if action == "start":
        return _ordered(text, _START)
    if action == "show":
        return _ordered(text, _SHOW)
    # start_and_show: "open it" asks for both; otherwise both must be asked.
    return _ordered(text, _OPEN) or (_ordered(text, _START) and _ordered(text, _SHOW))


# "What do you remember about me?" is a request to read the memory store, in
# so many words. Live it stopped at the card on `manage_memory list`. Only the
# two read actions; adding, editing or deleting a memory is never inferred
# from a question.
_ASKS_WHAT_IS_REMEMBERED = re.compile(
    r"\b(?:"
    r"que (?:recuerdas|sabes|tienes guardado|has guardado|te acuerdas|memorias tienes)"
    r"(?: tu)? (?:de|sobre) mi"
    r"|que memorias tienes"
    r"|(?:ensename|muestrame|lista|listame|dime) (?:mis|tus|las) (?:memorias|recuerdos)"
    r"|what do you (?:remember|know) about me"
    r"|what have you (?:saved|stored|remembered) about me"
    r"|(?:show|list) (?:me )?(?:my|your) memor(?:y|ies)"
    r")\b"
)


def _memory_read(user_text: str, content: Any) -> bool:
    from src.tool_capabilities import _action_from_content
    from src import plugins as plugins_mod

    if _action_from_content("manage_memory", content) not in ("list", "search"):
        return False
    return bool(_ASKS_WHAT_IS_REMEMBERED.search(plugins_mod.fold(user_text)))


#: tool name -> matcher(user_text, call_content). A tool that is not here is
#: never let through by this rule.
MATCHERS: Dict[str, Callable[[str, Any], bool]] = {
    "plugin_app": _plugin_app,
    "manage_memory": _memory_read,
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
