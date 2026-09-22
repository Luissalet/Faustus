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
        if message.get("_agent_injected") or message.get("_harness_note"):
            continue
        # The runtime talks to the model in user-role messages too (test
        # failures, loop recovery, "the list is done but..."). Those are not
        # the user asking for anything: read as the request, a nudge that
        # mentions tests could approve a test run the user never asked for,
        # and hides the request the user did make. The flag is set where
        # agent_loop appends them; the frame catches any other module.
        head = str(message.get("content") or "").lstrip()[:40]
        if head.startswith(("[Harness", "[Runtime")):
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
_NOT_AN_ORDER_AFTER = {"que", "si", "cuando", "como", "donde", "quien", "lo", "la", "se"}


def _ordered(text: str, verbs: Iterable[str]) -> bool:
    """One of `verbs` appears as a word and is not negated just before."""
    for verb in verbs:
        for match in re.finditer(rf"\b{verb}\b", text):
            before = text[: match.start()].split()[-2:]
            if set(before) & _NEGATION:
                continue
            # "¿qué cambia si…?", "si lo arranca…": the imperative and the
            # third person are the same word in Spanish; after an
            # interrogative or a conditional it is not an order.
            if before and before[-1] in _NOT_AN_ORDER_AFTER:
                continue
            return True
    return False


def _plugin_app(user_text: str, content: Any, workspace: str = "") -> bool:
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


def _memory_read(user_text: str, content: Any, workspace: str = "") -> bool:
    from src.tool_capabilities import _action_from_content
    from src import plugins as plugins_mod

    if _action_from_content("manage_memory", content) not in ("list", "search"):
        return False
    return bool(_ASKS_WHAT_IS_REMEMBERED.search(plugins_mod.fold(user_text)))


# "Los tests fallan, averigua por qué" asks, in so many words, for the tests
# to be run. Live, the first `python -m pytest ... | tail -20` of that task
# stopped at the card (the gate had been armed by reading the project's own
# files). Only a test runner, invoked plainly: no `;`, `&&`, redirection into
# a file, substitution or backticks, optionally piped into head/tail to trim
# its output. Anything more is a shell command like any other.
_ASKS_ABOUT_TESTS = re.compile(r"\b(?:tests?|pruebas?|pytest|testea\w*|testing|suite)\b")
_ARG = r"[\w./\\:=\[\]\-]+"
_TEST_RUNNER = re.compile(
    r"^(?:"
    r"(?:python3?|py|(?:\.?venv[\\/])?(?:scripts|bin)[\\/]python(?:\.exe)?)\s+-m\s+(?:pytest|unittest)"
    r"|pytest"
    r"|(?:npm|pnpm|yarn)\s+(?:run\s+)?test[\w:-]*(?:\s+--)?"
    r"|cargo\s+test|go\s+test"
    rf")(?:\s+{_ARG})*$",
    re.IGNORECASE,
)
_OUTPUT_TRIM = re.compile(
    r"^(?:(?:tail|head)(?:\s+-n)?\s+-?\d+|select-object\s+-(?:first|last)\s+\d+)$",
    re.IGNORECASE,
)


def _command_of(content: Any) -> str:
    if isinstance(content, Mapping):
        return str(content.get("command") or content.get("cmd") or "")
    text = str(content or "").strip()
    if text.startswith("{"):
        try:
            import json

            parsed = json.loads(text)
        except ValueError:
            return ""
        if isinstance(parsed, dict):
            return str(parsed.get("command") or parsed.get("cmd") or "")
        return ""
    return text


def _same_dir(path: str, workspace: str) -> bool:
    import os

    if not workspace or not path:
        return False
    # The shell Faustus runs on Windows is bash: the same folder arrives as
    # "D:/x", "D:\\x", "/d/x" (seen live), "/mnt/d/x" or "/cygdrive/d/x".
    drive = re.match(r"^/(?:mnt/|cygdrive/)?([a-zA-Z])(?:/(.*))?$", path)
    if drive and os.name == "nt":
        path = f"{drive.group(1)}:/{drive.group(2) or ''}"
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.abspath(str(p)))).rstrip("\\/")  # noqa: E731
    return norm(path) == norm(workspace)


def _runs_the_tests(user_text: str, content: Any, workspace: str = "") -> bool:
    from src import plugins as plugins_mod

    if not _ASKS_ABOUT_TESTS.search(plugins_mod.fold(user_text)):
        return False
    command = " ".join(_command_of(content).replace("2>&1", " ").split())
    # `cd "<workspace>" && pytest ...` is how models run tests from the
    # project root (seen live). Allowed only when the directory IS the
    # workspace this turn is bound to.
    lead = re.match(r"^cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*&&\s*", command)
    if lead:
        if not _same_dir(lead.group(1).strip("\"'"), workspace):
            return False
        command = command[lead.end():]
    if not command or re.search(r"[;&<>`$()]|\|\|", command):
        return False
    stages = [s.strip() for s in command.split("|")]
    if not _TEST_RUNNER.match(stages[0]):
        return False
    # Options that load code from elsewhere or move where the run happens are
    # not "run the tests": a plugin by name, another config, another root.
    if re.search(r"(?:^|\s)(?:-p|-c|--pyargs|--rootdir\S*|--basetemp\S*|--confcutdir\S*)(?:\s|$|=)",
                 stages[0]):
        return False
    return all(_OUTPUT_TRIM.match(s) for s in stages[1:])


# A command the user wrote themselves, in code format, is the plainest way to
# ask for it. Seen live: the message quoted `python -m billing.cli report
# data.json`, said it crashed and asked for the report's output at the end;
# after fixing it the agent ran exactly that command and stopped at the card.
# Only the literal command passes: the same words, optionally run from this
# workspace (`cd <workspace> &&`) and with its output trimmed. Inline code
# spans and one-line code blocks count; a multi-line block is pasted
# material (a traceback, a script), not an order. A span the user negated
# ("no ejecutes `...`") does not count.
_CODE_SPAN = re.compile(r"```[^\n`]*\n([^\n`]+)\n```|`([^`\n]+)`")
_NEGATED_BEFORE = re.compile(
    r"\b(?:no|nunca|jamas|jamás|sin|not|never|don'?t|do\s+not|avoid|evita)\b[^.`\n]{0,30}$",
    re.IGNORECASE,
)


def _shell_words(command: str) -> str:
    return " ".join(command.replace("2>&1", " ").split())


def _runs_what_the_user_wrote(user_text: str, content: Any, workspace: str = "") -> bool:
    command = _shell_words(_command_of(content))
    lead = re.match(r"^cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*&&\s*", command)
    if lead:
        if not _same_dir(lead.group(1).strip("\"'"), workspace):
            return False
        command = command[lead.end():]
    stages = [s.strip() for s in command.split("|")]
    while len(stages) > 1 and _OUTPUT_TRIM.match(stages[-1]):
        stages.pop()
    command = " | ".join(stages)
    if not command:
        return False
    for match in _CODE_SPAN.finditer(str(user_text or "")):
        written = _shell_words(match.group(1) or match.group(2) or "")
        if written != command:
            continue
        if _NEGATED_BEFORE.search(user_text[:match.start()]):
            return False
        return True
    return False


_PLAIN_ECHO = re.compile(r"^echo(?:\s+(?:[\w.,:=+/\- ]+|\"[\w.,:=+/\- ]*\"|'[\w.,:=+/\- ]*'))?$")


def _one_command(user_text: str, command: str, workspace: str) -> bool:
    return (_runs_the_tests(user_text, command, workspace)
            or _runs_what_the_user_wrote(user_text, command, workspace))


def _shell_matcher(user_text: str, content: Any, workspace: str = "") -> bool:
    command = _shell_words(_command_of(content))
    if _one_command(user_text, command, workspace):
        return True
    # Seen live: `cd <ws> && python -m pytest -q && echo --- && <the command
    # the user quoted>`. Each step is one the user asked for; `&&` only runs
    # them in order and stops at the first failure. So a chain passes when
    # every step passes on its own (a plain `echo` separator included).
    # `;`, `||` and `&` are not split here and keep the gate.
    lead = re.match(r"^cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*&&\s*", command)
    if lead:
        if not _same_dir(lead.group(1).strip("\"'"), workspace):
            return False
        command = command[lead.end():]
    steps = [s.strip() for s in command.split("&&")]
    if len(steps) < 2 or not all(steps):
        return False
    asked = [s for s in steps if not _PLAIN_ECHO.match(s)]
    return bool(asked) and all(_one_command(user_text, s, workspace) for s in asked)


# "Arréglalo" asks for the project to be changed. Live, after reading the
# code and running the tests, the one-line fix to inventario.py stopped at
# the card. An edit passes when: a workspace is bound, the user's words
# order a change (imperative or infinitive, not negated), and every target
# resolves inside that workspace. Deletions never pass (apply_patch with a
# Delete hunk has no determinable targets). And when the user said to
# leave the tests alone, a test file is not a target this rule allows.
_ORDERS_A_CHANGE = (
    r"arregla(?:lo|la|los|las)?", r"arreglar(?:lo|la)?", r"corrige(?:lo|la)?", r"corregir(?:lo|la)?",
    r"repara(?:lo|la)?", r"soluciona(?:lo|la)?", r"cambia(?:lo|la)?", r"modifica(?:lo|la)?",
    r"implementa(?:lo|la)?", r"implementar(?:lo|la)?", r"anade(?:lo|la|le)?", r"agrega(?:lo|la)?",
    r"crea(?:lo|la|me)?", r"escribe(?:lo|la|me)?", r"refactoriza(?:lo|la)?", r"actualiza(?:lo|la)?",
    r"fix", r"repair", r"change", r"modify", r"implement", r"add", r"create", r"write",
    r"refactor", r"update",
)
_LEAVE_TESTS_ALONE = re.compile(
    r"\b(?:sin\s+(?:tocar|modificar|cambiar)|no\s+(?:toques|modifiques|cambies))\s+(?:los\s+|las\s+)?(?:tests?|pruebas?)"
    r"|\b(?:without\s+(?:touching|changing|modifying)|(?:don\s*t|do\s+not)\s+(?:touch|change|modify))\s+(?:the\s+)?tests?"
)
_TEST_FILE = re.compile(
    r"(?:^|[\\/])(?:tests?[\\/]|test_[^\\/]*$|[^\\/]*_test\.\w+$|[^\\/]*\.(?:test|spec)\.\w+$|conftest\.py$)",
    re.IGNORECASE,
)


def _edits_the_project(user_text: str, content: Any, workspace: str = "", tool: str = "") -> bool:
    from src import plugins as plugins_mod
    from src.tool_capabilities import _write_targets, path_inside_trusted

    if not workspace:
        return False
    text = plugins_mod.fold(user_text)
    if not _ordered(text, _ORDERS_A_CHANGE):
        return False
    targets = _write_targets(tool, content)
    if not targets or not all(path_inside_trusted(workspace, t) for t in targets):
        return False
    if _LEAVE_TESTS_ALONE.search(text) and any(_TEST_FILE.search(t.replace("\\", "/")) for t in targets):
        return False
    return True


def _edit_matcher(tool: str) -> Callable[..., bool]:
    return lambda user_text, content, workspace="": _edits_the_project(user_text, content, workspace, tool)


#: tool name -> matcher(user_text, call_content). A tool that is not here is
#: never let through by this rule.
MATCHERS: Dict[str, Callable[..., bool]] = {
    "plugin_app": _plugin_app,
    "manage_memory": _memory_read,
    "bash": _shell_matcher,
    "powershell": _shell_matcher,
    "edit_file": _edit_matcher("edit_file"),
    "write_file": _edit_matcher("write_file"),
    "apply_patch": _edit_matcher("apply_patch"),
}


def allows(tool_name: Any, content: Any, user_text: str, workspace: str = "") -> bool:
    """True when the user's latest message asked for exactly this call."""
    matcher = MATCHERS.get(tool_name) if isinstance(tool_name, str) else None
    if matcher is None or not str(user_text or "").strip():
        return False
    try:
        ok = bool(matcher(user_text, content, workspace or ""))
    except Exception as exc:  # noqa: BLE001 - a matcher failure keeps the gate
        logger.info("[gate] user request matcher for %s failed: %s", tool_name, exc)
        return False
    if ok:
        logger.info("[gate] %s is what the user asked for in this turn: passes the gate", tool_name)
    return ok
