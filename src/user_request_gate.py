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


# Looking at a file in the workspace with the shell is what read_file does,
# which never needs the card. Seen live: `wc -l ventas_junio.csv && tail -n
# +31 ventas_junio.csv | head -60` on the user's own export stopped the
# analysis. A step passes when every command in its pipeline is one of these
# read-only ones, with none of the options that write, follow, run a program
# or read a list of names from elsewhere, and every path it names is inside
# the workspace. Quoted text is taken literally, as bash does (`cut -d';'`).
_READ_ONLY_COMMANDS = {
    "cat": (), "head": (), "wc": ("--files0-from",), "ls": (), "cut": (), "nl": (),
    "stat": (), "du": ("--files0-from", "-X", "--exclude-from"), "uniq": (),
    "tail": ("-f", "-F", "--follow", "--retry"),
    "grep": ("-f", "--file", "--exclude-from"),
    "sort": ("-o", "--output", "-T", "--temporary-directory", "--compress-program", "--random-source", "--files0-from"),
    # the program is checked on its own below (_harmless_awk)
    # `-e`/`--source`/`-W` could carry a program inside the option itself
    "awk": ("-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec", "-o", "--pretty-print",
            "-p", "--profile", "-d", "--dump-variables", "-D", "--debug", "-e", "--source", "-W"),
    "sed": (),  # only `sed -n '<range>p'`, checked below
}


def _harmless_awk(program: str) -> bool:
    """An awk program that only reads and prints: no `system()`, no
    `getline` (reads files or runs commands), no pipes to commands, no
    `print > file`, no extension loading. Seen live: `awk -F';' 'NR>1
    {print $2}' ventas.csv | sort | uniq -c` to count stores."""
    if re.search(r"\b(?:system|getline)\b|[|@`]", program):
        return False
    return not re.search(r"\bprintf?\b[^;}]*>", program)


def _host_path(token: str) -> str:
    import os

    drive = re.match(r"^/(?:mnt/|cygdrive/)?([a-zA-Z])(?:/(.*))?$", token)
    if drive and os.name == "nt":
        return f"{drive.group(1)}:/{drive.group(2) or ''}"
    return token


def _bad_option(word: str, bad: tuple) -> bool:
    if word.startswith("--"):
        return word.split("=", 1)[0] in bad
    # a short cluster: `-no` is -n and -o; stop at the first option that
    # takes a value in the same word (`-t;`, `-d,`, `-n20`)
    for i, ch in enumerate(word[1:], 1):
        if f"-{ch}" in bad:
            return True
        if ch in "tdkncCsSe":
            break
    return False


def _inspects_the_workspace(step: str, workspace: str) -> bool:
    import os
    import shlex

    from src.tool_capabilities import path_inside_trusted

    if not workspace:
        return False
    step = step.replace("2>/dev/null", " ")
    # `$`, backticks and backslashes expand or escape outside single quotes.
    if re.search(r"[`$\\]", re.sub(r"'[^']*'", "", step)):
        return False
    try:
        lexer = shlex.shlex(step, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    stages, current = [], []
    for token in tokens:
        if token == "|":
            stages.append(current)
            current = []
        elif token and set(token) <= set("|&;<>()"):
            return False  # any other operator: redirection, ;, &, ||, subshell
        else:
            current.append(token)
    stages.append(current)
    for words in stages:
        if not words or words[0] not in _READ_ONLY_COMMANDS:
            return False
        bad = _READ_ONLY_COMMANDS[words[0]]
        # Every word that is not an option is checked as a path, option
        # values included: skipping "the value after -n" would skip the file
        # in `cat -n /etc/x`. A value like `;` or `2` reads as a harmless
        # name inside the workspace.
        positional, awk_assignment = [], False
        for word in words[1:]:
            if awk_assignment:
                awk_assignment = False
                if not re.match(r"^[A-Za-z_]\w*=", word):
                    return False
                continue  # `awk -v x=1`: a variable, not a file or the program
            if word.startswith("-") and not re.match(r"^-\d+$", word):
                if _bad_option(word, bad):
                    return False
                awk_assignment = words[0] == "awk" and word == "-v"
                continue
            positional.append(word)
        if words[0] == "grep" and positional:
            positional = positional[1:]  # the pattern, not a path
        if words[0] == "sed":
            # only printing a range of lines (`sed -n '1,40p'`, seen live):
            # sed can also write files (w), run commands (e) and edit in place
            if [w for w in words[1:] if w.startswith("-")] != ["-n"] or not positional or not re.fullmatch(
                    r"(?:\d+|\$)(?:,(?:\d+|\$))?p", positional[0]):
                return False
            positional = positional[1:]
        if words[0] == "awk":
            if not positional or not _harmless_awk(positional[0]):
                return False
            # the program, then `var=value` assignments awk reads as such
            positional = [w for w in positional[1:] if not re.match(r"^[A-Za-z_]\w*=", w)]
        if words[0] == "uniq" and len(positional) > 1:
            return False  # `uniq in out` writes its second name
        for word in positional:
            if re.match(r"^[+-]?\d+$", word):
                continue
            # bash expands `~` and `{a,b}` into other paths than the text says
            if re.search(r"[~{}]", word) or re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", word):
                return False
            path = _host_path(word)
            target = os.path.normpath(path if os.path.isabs(path) else os.path.join(workspace, path))
            if re.search(r"[*?\[]", word):
                import glob

                # a glob is checked by what it matches: `link*` could be a
                # symlink that leaves the workspace
                if not all(path_inside_trusted(workspace, m) for m in glob.glob(target)):
                    return False
            elif not path_inside_trusted(workspace, target):
                return False
    return True


_PLAIN_ECHO = re.compile(r"^echo(?:\s+(?:[\w.,:=+/\- ]+|\"[\w.,:=+/\- ]*\"|'[\w.,:=+/\- ]*'))?$")


def _python_dash_c(user_text: str, command: str, workspace: str) -> bool:
    """`python -c "<code>"` from the shell is the python tool by another
    door; it gets the same judgment (`python -c "import pandas"` to see
    whether a package is there)."""
    import shlex

    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) != 3 or words[1] != "-c" or not re.fullmatch(r"(?:python3?|py)(?:\.exe)?", words[0]):
        return False
    return _analyses_the_data(user_text, words[2], workspace)


def _one_command(user_text: str, command: str, workspace: str) -> bool:
    return (_runs_the_tests(user_text, command, workspace)
            or _runs_what_the_user_wrote(user_text, command, workspace)
            or _inspects_the_workspace(command, workspace)
            or _python_dash_c(user_text, command, workspace))


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
    if not all(steps) or (len(steps) < 2 and not lead):
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
    # "Hazme un resumen en informe.md" (seen live) orders a file as much as "créalo"
    r"haz(?:lo|la|me)?", r"hacer(?:lo|la|me)?", r"genera(?:lo|la|me)?", r"generar(?:lo|la)?",
    r"prepara(?:lo|la|me)?", r"redacta(?:lo|la|me)?", r"guarda(?:lo|la|me)?",
    r"fix", r"repair", r"change", r"modify", r"implement", r"add", r"create", r"write",
    r"refactor", r"update", r"make", r"generate", r"draft", r"save",
)
_LEAVE_TESTS_ALONE = re.compile(
    r"\b(?:sin\s+(?:tocar|modificar|cambiar)|no\s+(?:toques|modifiques|cambies))\s+(?:los\s+|las\s+)?(?:tests?|pruebas?)"
    r"|\b(?:without\s+(?:touching|changing|modifying)|(?:don\s*t|do\s+not)\s+(?:touch|change|modify))\s+(?:the\s+)?tests?"
)
_TEST_FILE = re.compile(
    r"(?:^|[\\/])(?:tests?[\\/]|test_[^\\/]*$|[^\\/]*_test\.\w+$|[^\\/]*\.(?:test|spec)\.\w+$|conftest\.py$)",
    re.IGNORECASE,
)


def _names_every_file(user_text: str, targets: Any) -> bool:
    """The user's message names each new file being written ("un resumen en
    informe_junio.md"), not after a negation ("no toques app.py"). Only new
    files: naming the export you hand over is not asking to overwrite it."""
    import os

    lowered = str(user_text or "").lower()
    for target in targets:
        if os.path.exists(str(target)):
            return False
        name = os.path.basename(str(target)).lower()
        at = lowered.find(name) if len(name) >= 4 and "." in name else -1
        if at < 0 or _NEGATED_BEFORE.search(user_text[:at]):
            return False
    return True


def _edits_the_project(user_text: str, content: Any, workspace: str = "", tool: str = "") -> bool:
    from src import plugins as plugins_mod
    from src.tool_capabilities import _write_targets, path_inside_trusted

    if not workspace:
        return False
    text = plugins_mod.fold(user_text)
    targets = _write_targets(tool, content)
    if not targets or not all(path_inside_trusted(workspace, t) for t in targets):
        return False
    if not _ordered(text, _ORDERS_A_CHANGE) and not _names_every_file(user_text, targets):
        return False
    if _LEAVE_TESTS_ALONE.search(text) and any(_TEST_FILE.search(t.replace("\\", "/")) for t in targets):
        return False
    return True


# "Analyse my sales CSV and make a chart" is code the user asked for. Seen
# live: after reading the user's own CSV the first `python` call stopped at
# the card, so the task could not start. Python is the widest door the gate
# guards, so this matcher reads the code and lets it through only when every
# name it reaches is on a list: a few standard modules, and for pandas,
# numpy and matplotlib only the functions data work uses - not their I/O
# internals (`pd.io`, `np.lib`), not backends or options that import modules
# by name. No dunders, private attributes or lookups by string; annotations
# only plain type names; every file it touches is a literal path inside the
# workspace, and writes only create output files (a chart, a table, a
# summary), never overwrite the user's. A first version listed what to
# refuse instead; three rounds of adversarial review got past it through
# `pd.io.common.get_handle`, `np.lib.format.open_memmap`,
# `matplotlib.use("module://...")` and annotation strings evaluated by
# `typing.get_type_hints` / `functools.singledispatch`. Anything not proven
# safe keeps the card.
_ASKS_FOR_DATA_WORK = re.compile(
    r"\b(?:grafic\w*|chart\w*|plot\w*|analiz\w*|analisis|analy[sz]\w*|calcul\w*|estadistic\w*|statistic\w*"
    r"|media|promedio|average|mean|suma|sum|totales|factur\w*|cifras|figures|numbers|numeros|datos|data"
    r"|csv|excel|xlsx|informe|report|resumen|summary|ventas|sales)\b"
)
# Standard modules whose whole public surface is computation or works on
# file objects `open()` already vetted.
_OPEN_MODULES = frozenset({
    "csv", "json", "math", "statistics", "collections", "datetime", "decimal", "fractions", "re",
    "itertools", "operator", "string", "textwrap", "unicodedata", "calendar",
    "enum", "random", "pprint", "heapq", "bisect",
    "matplotlib.ticker", "matplotlib.dates",
})
# Libraries with I/O and import machinery inside: only these names.
_LISTED_MODULES = {
    "pandas": frozenset({
        "DataFrame", "Series", "Index", "MultiIndex", "Categorical", "CategoricalDtype", "Timestamp",
        "Timedelta", "Period", "NA", "NaT", "read_csv", "read_excel", "read_json", "read_parquet",
        "read_table", "to_datetime", "to_numeric", "to_timedelta", "concat", "merge", "merge_asof",
        "pivot", "pivot_table", "crosstab", "melt", "cut", "qcut", "date_range", "period_range",
        "isna", "isnull", "notna", "notnull", "unique", "get_dummies", "factorize", "Grouper",
        "IndexSlice", "DateOffset", "offsets", "NamedAgg",
    }),
    "numpy": frozenset({
        "array", "asarray", "arange", "linspace", "zeros", "ones", "full", "empty", "eye", "mean",
        "median", "average", "sum", "prod", "std", "var", "min", "max", "amin", "amax", "argmin",
        "argmax", "round", "around", "abs", "absolute", "sqrt", "exp", "log", "log10", "log2",
        "power", "where", "nan", "inf", "pi", "e", "isnan", "isfinite", "isinf", "nanmean",
        "nansum", "nanmedian", "nanstd", "nanmin", "nanmax", "percentile", "quantile", "cumsum",
        "cumprod", "diff", "sort", "argsort", "unique", "concatenate", "stack", "vstack", "hstack",
        "clip", "histogram", "bincount", "corrcoef", "cov", "polyfit", "polyval", "floor", "ceil",
        "trunc", "sign", "maximum", "minimum", "int32", "int64", "float32", "float64", "bool_",
        "datetime64", "timedelta64", "random", "dot", "matmul", "transpose", "reshape", "ravel",
        "count_nonzero", "any", "all", "allclose", "isclose", "digitize", "interp", "gradient",
        "convolve", "repeat", "tile", "meshgrid", "ndarray", "dtype", "newaxis", "save", "savetxt",
        "load", "loadtxt", "genfromtxt",
    }),
    "matplotlib": frozenset({"use", "pyplot", "ticker", "dates", "cm", "colors", "colormaps"}),
    # `typing.get_type_hints` evaluates annotation strings: names only.
    "typing": frozenset({
        "Any", "Dict", "List", "Tuple", "Set", "Optional", "Union", "Iterable", "Iterator",
        "Sequence", "Mapping", "Callable", "NamedTuple", "TypedDict", "Literal",
    }),
    "dataclasses": frozenset({"dataclass", "field", "fields", "asdict", "astuple"}),
    # `singledispatch` evaluates annotations like `get_type_hints` does.
    "functools": frozenset({"reduce", "partial", "lru_cache", "cache", "cmp_to_key", "total_ordering", "wraps"}),
    "matplotlib.pyplot": frozenset({
        "figure", "subplots", "subplot", "bar", "barh", "plot", "pie", "hist", "scatter", "boxplot",
        "stackplot", "step", "fill_between", "errorbar", "title", "suptitle", "xlabel", "ylabel",
        "xticks", "yticks", "xlim", "ylim", "legend", "grid", "text", "annotate", "axhline",
        "axvline", "tight_layout", "savefig", "close", "gca", "gcf", "table", "colorbar", "twinx",
        "figtext", "margins", "ticklabel_format", "subplots_adjust", "cm", "get_cmap", "bar_label",
    }),
}
_NON_GUI_BACKENDS = frozenset({"agg", "svg", "pdf", "ps", "cairo"})
_KNOWN_ENGINES = frozenset({"c", "python", "pyarrow", "openpyxl", "odf", "xlrd", "xlsxwriter", "calamine"})
# Builtins and attribute names that evaluate code, look names up by string,
# reach modules re-exported inside libraries, or hand out raw memory.
_DYNAMIC = frozenset({
    "eval", "exec", "compile", "getattr", "setattr", "delattr", "globals", "locals", "vars", "input",
    "breakpoint", "help", "exit", "quit", "license", "memoryview", "attrgetter", "methodcaller",
    "query", "open_code", "system", "popen", "ctypes", "ctypeslib", "DataSource",
})
_MODULE_ATTRS = frozenset({
    "os", "sys", "subprocess", "shutil", "socket", "importlib", "builtins", "pickle", "marshal",
    "urllib", "http", "request", "requests", "pathlib", "glob", "tempfile", "platform", "signal",
    "threading", "multiprocessing", "asyncio", "webbrowser", "runpy", "code", "inspect", "gc",
    "zipfile", "tarfile", "posix", "nt", "winreg", "msvcrt", "mmap", "pydoc", "io", "lib",
    "clipboard", "clipboards", "sqlalchemy", "sqlite3", "rcParams", "rc", "rc_file", "style",
    "switch_backend", "set_option", "options", "plotting", "api", "testing", "compat", "core",
})
_UNSAFE_FORMATS = frozenset({
    "read_pickle", "to_pickle", "read_sql", "read_sql_query", "read_sql_table", "to_sql", "read_html",
    "read_xml", "read_clipboard", "to_clipboard", "read_hdf", "to_hdf", "HDFStore", "to_gbq",
    "read_gbq", "memmap", "ExcelWriter", "open_memmap", "get_handle",
})
_READ_CALLS = frozenset({
    "read_csv", "read_excel", "read_json", "read_table", "read_parquet", "read_fwf", "read_feather",
    "read_orc", "read_stata", "read_spss", "read_sas", "ExcelFile", "imread", "loadtxt", "genfromtxt",
    "fromfile", "load",
})
_WRITE_CALLS = frozenset({
    "to_csv", "to_excel", "to_json", "to_parquet", "to_feather", "to_markdown", "to_html", "to_latex",
    "to_string", "to_stata", "to_xml", "to_orc", "savefig", "imsave", "savetxt", "tofile", "save",
    "savez", "savez_compressed", "dump", "print_figure", "print_png", "print_pdf", "print_svg",
})
_PATH_KEYWORDS = frozenset({
    "path", "path_or_buf", "path_or_buffer", "filepath_or_buffer", "fname", "file", "filename",
    "excel_writer", "buf", "io",
})
_OUTPUT_SUFFIXES = (".png", ".jpg", ".jpeg", ".svg", ".pdf", ".html", ".md", ".txt", ".csv", ".xlsx", ".json")
_RECENT_OUTPUT_S = 30 * 60


def _literal_path(node: Any, workspace: str) -> Optional[str]:
    """The absolute path a literal names inside the workspace, else None."""
    import ast
    import os

    from src.tool_capabilities import path_inside_trusted

    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or not node.value.strip():
        return None
    raw = node.value.strip()
    target = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(workspace, raw))
    return target if path_inside_trusted(workspace, target) else None


def _writable_output(path: str) -> bool:
    """A new output file, or one this task wrote minutes ago (a re-run)."""
    import os
    import time

    if not path.lower().endswith(_OUTPUT_SUFFIXES):
        return False
    if not os.path.exists(path):
        return True
    return os.path.isfile(path) and time.time() - os.path.getmtime(path) < _RECENT_OUTPUT_S


def _suspicious_string(value: str, workspace: str) -> bool:
    import os

    from src.tool_capabilities import path_inside_trusted

    text = value.strip()
    if "__" in text or "://" in text or text.startswith(("\\\\", "//", "~")):
        return True
    if re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", text):
        return True
    if re.match(r"^(?:[A-Za-z]:[\\/]|/[A-Za-z0-9_.-]+/)", text):
        return not path_inside_trusted(workspace, os.path.normpath(text))
    return False


def _workspace_shadows(module: str, workspace: str) -> bool:
    import os

    return (os.path.exists(os.path.join(workspace, module + ".py"))
            or os.path.isdir(os.path.join(workspace, module)))


def _code_of(content: Any) -> str:
    if isinstance(content, Mapping):
        return str(content.get("code") or content.get("content") or "")
    text = str(content or "")
    if text.lstrip().startswith("{"):
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
                return parsed["code"]
        except ValueError:
            pass
    return text


def _open_writes(call: Any) -> bool:
    import ast

    mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
    if mode is None:
        return False
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return True  # a mode it cannot read counts as a write
    return any(c in mode.value for c in "wax+")


def _callee(node: Any) -> str:
    import ast

    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


_PLAIN_TYPES = frozenset({"int", "float", "str", "bool", "bytes", "list", "dict", "tuple", "set", "object"})


def _unsafe_annotation(annotation: Any, type_names: set, modules: Optional[dict] = None) -> bool:
    """An annotation is code in waiting: `typing.get_type_hints` and
    `functools.singledispatch` evaluate strings in it. An adversarial review
    ran shell commands through `def f(x: "exec(...)")`, then through `x: s`
    with `s` holding that string. So annotations may only be plain type
    names, names imported from `typing`, listed module names, and
    subscripts or unions of those."""
    import ast

    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant):
        return annotation.value is not None
    if isinstance(annotation, ast.Name):
        return annotation.id not in _PLAIN_TYPES and annotation.id not in type_names
    if isinstance(annotation, ast.Attribute) and isinstance(annotation.value, ast.Name):
        module = (modules or {}).get(annotation.value.id)
        return not (module and _module_allows(module, annotation.attr))
    if isinstance(annotation, ast.Subscript):
        inner = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
        return _unsafe_annotation(annotation.value, type_names, modules) or any(
            _unsafe_annotation(e, type_names, modules) for e in inner)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return (_unsafe_annotation(annotation.left, type_names, modules)
                or _unsafe_annotation(annotation.right, type_names, modules))
    return True


def _module_allows(module: str, name: str) -> bool:
    if module in _LISTED_MODULES:
        return name in _LISTED_MODULES[module]
    return module in _OPEN_MODULES and not name.startswith("_")


def _analyses_the_data(user_text: str, content: Any, workspace: str = "") -> bool:
    import ast

    from src import plugins as plugins_mod

    if not workspace or not _ASKS_FOR_DATA_WORK.search(plugins_mod.fold(user_text)):
        return False
    try:
        tree = ast.parse(_code_of(content))
    except (SyntaxError, ValueError):
        return False
    known = _OPEN_MODULES | set(_LISTED_MODULES)
    # Local name -> module it is bound to, from the imports.
    modules: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in known:
                    return False
                if a.asname:
                    modules[a.asname] = a.name
                else:
                    root = a.name.split(".")[0]
                    if root not in known:
                        return False
                    modules[root] = root
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or node.module not in known:
                return False
            for a in node.names:
                if a.name == "*" or not _module_allows(node.module, a.name):
                    return False
                sub = f"{node.module}.{a.name}"
                if sub in known:
                    modules[a.asname or a.name] = sub
                elif a.name in _READ_CALLS | _WRITE_CALLS:
                    return False  # `from numpy import load` would hide what is called
    type_names = {a.asname or a.name for n in ast.walk(tree)
                  if isinstance(n, ast.ImportFrom) and n.module == "typing" for a in n.names}
    type_names |= {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    for mod in set(modules.values()):
        if _workspace_shadows(mod.split(".")[0], workspace):
            return False

    def dotted(node: Any) -> Optional[str]:
        if isinstance(node, ast.Name):
            return modules.get(node.id)
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    file_callees = _READ_CALLS | _WRITE_CALLS | {"open"}
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    # `out = r'...\chart.png'; plt.savefig(out)` (seen live): a name bound
    # exactly once in the whole code, to a string literal, stands for it.
    stores: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
            stores[n.id] = stores.get(n.id, 0) + 1
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stores[n.name] = stores.get(n.name, 0) + 2
        elif isinstance(n, ast.arg):
            stores[n.arg] = stores.get(n.arg, 0) + 2
        # bindings that are not Name nodes: `except E as out`, `import x as
        # out`, `case {"k": out}` / `case [*out]`
        for bound in (getattr(n, "name", None) if isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) else None,
                      getattr(n, "rest", None) if isinstance(n, ast.MatchMapping) else None,
                      (n.asname or n.name.split(".")[0]) if isinstance(n, ast.alias) else None):
            if bound:
                stores[bound] = stores.get(bound, 0) + 2
    literals = {
        n.targets[0].id: n.value for n in ast.walk(tree)
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
        and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
        and stores.get(n.targets[0].id) == 1
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if "__" in node.id or node.id in _DYNAMIC:
                return False
            if isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in modules:
                return False  # rebinding a module name would defeat the lists
            if node.id == "open" and id(node) not in called:
                return False  # `f = open` would hide the call
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in modules or _unsafe_annotation(getattr(node, "returns", None), type_names, modules):
                return False
        elif isinstance(node, ast.AnnAssign):
            if _unsafe_annotation(node.annotation, type_names, modules):
                return False
        elif isinstance(node, ast.arg):
            if node.arg in modules or _unsafe_annotation(node.annotation, type_names, modules):
                return False
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr.startswith("_") or attr in _DYNAMIC | _MODULE_ATTRS | _UNSAFE_FORMATS:
                return False
            base = dotted(node.value)
            if base is not None and base in known and not _module_allows(base, attr):
                return False
            if base is not None and base not in known and base.split(".")[0] in known:
                # An attribute of a listed name (`pd.DataFrame.from_dict`) is
                # fine; a namespace below a module that is not listed is not.
                parent = base.rsplit(".", 1)[0]
                if parent in known and not _module_allows(parent, base.rsplit(".", 1)[1]):
                    return False
            if attr in file_callees and id(node) not in called:
                return False  # `r = pd.read_csv` would hide the call
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            return False
        elif isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            value = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
            if _suspicious_string(value, workspace):
                return False
        elif isinstance(node, ast.keyword):
            if node.arg is None or node.arg in {"allow_pickle", "backend"}:
                return False  # **kwargs could carry anything; backend= imports by name
            if node.arg == "engine" and not (
                    isinstance(node.value, ast.Constant) and str(node.value.value).lower() in _KNOWN_ENGINES):
                return False
        elif isinstance(node, ast.Call):
            name = _callee(node.func)
            if dotted(node.func) == "matplotlib.use":
                arg = node.args[0] if node.args else None
                if not (isinstance(arg, ast.Constant) and str(arg.value).lower() in _NON_GUI_BACKENDS):
                    return False
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) and name in file_callees:
                return False
            if name not in file_callees:
                continue
            receiver = dotted(node.func.value) if isinstance(node.func, ast.Attribute) else None
            if name in {"load", "dump"} and receiver == "json":
                continue  # json.load/dump take a file object from a vetted open()
            paths = list(node.args[:1]) + [k.value for k in node.keywords if k.arg in _PATH_KEYWORDS]
            if name == "open" and not paths:
                return False
            writes = name in _WRITE_CALLS or (name == "open" and _open_writes(node))
            for p in paths:
                if isinstance(p, ast.Name) and p.id in literals:
                    p = literals[p.id]
                target = _literal_path(p, workspace)
                if target is None or (writes and not _writable_output(target)):
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
    "python": _analyses_the_data,
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
