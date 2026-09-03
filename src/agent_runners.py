"""agent_runners.py — the catalogue of CLI agents Faustus can run as workers.

    "quiero que sea versátil para que se puedan usar distintos modelos,
     agentes etc. Claude, qwen, openclaw, lo que sea, piezas modulares e
     intercambiables."

Ollama 0.33 ships ``ollama launch <integration>``, which installs, configures
and starts a coding agent pointed at the local Ollama. This module is the
other half of that idea: **a table that says how to run ONE task with each of
them**, so a different agent is a row of data, not a branch of code.

Two sources, merged, in this order:

* the **live help** — ``ollama launch --help`` is parsed at runtime for the
  integrations this machine's Ollama knows, with their labels and aliases. It
  is the source of the LIST, so a new integration Ollama adds appears here the
  day it is added, without touching this file;
* the **built-in table** below adds what a help text cannot know: the licence
  word, the argv that runs one task non-interactively, the environment, and
  whether the thing is a CLI at all.

A name in the help with no built-in row still appears — with ``argv: ()``,
``licence: "unknown"`` and a note saying it is known to Ollama but not
runnable as a worker yet. A built-in row whose name the help does not list
still appears too (an older Ollama, or none installed). Neither source is
allowed to be the only one: a hardcoded list would rot, and the help alone
cannot say how to run anything.

Three rules this file keeps:

* **A licence word is never invented.** ``open`` / ``subscription`` /
  ``unknown``; when the licence of an integration is not something this table
  can state, it says ``unknown`` and the UI prints that word. "Unknown" is a
  real answer here, not a placeholder for a guess.
* **An argv is never invented either.** A runner is only ``runnable_as_worker``
  when this table carries a non-interactive invocation for it that the tool
  itself documents. Everything else is listed, is honestly marked, and waits
  for someone to add its row.
* **Nothing here runs an installer.** :func:`launch_argv` BUILDS the
  ``ollama launch`` command and returns it; running it is a separate,
  explicit act by the user (routes/agent_runner_routes.py).

Pure and stdlib-only. Every entry point is total: an Ollama that is not
installed, a help text in a shape this parser has never seen, a junk key —
none of them raise, they degrade to the built-in table alone.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The licence words this module may print. Anything else is a bug.
LICENCES: Tuple[str, ...] = ("open", "subscription", "unknown")
#: ``cli`` can be a worker; ``app`` is a GUI and never can be.
KINDS: Tuple[str, ...] = ("cli", "app")

#: How long the parsed help is reused before ``ollama launch --help`` is run
#: again. The catalogue is read by a page that polls; this keeps that from
#: spawning a process per request.
HELP_TTL_S = 300.0
#: Bound on the help subprocess itself.
HELP_TIMEOUT_S = 8.0
#: Bound on a ``--version`` probe (only run when a caller asks for versions).
VERSION_TIMEOUT_S = 5.0
_VERSION_CHARS = 120

#: Default hard bound on one external agent's run, in seconds.
DEFAULT_TIMEOUT_S = 900


@dataclass(frozen=True)
class Runner:
    """One agent, as data.

    ``argv`` placeholders: ``{task}`` (the instruction), ``{model}``,
    ``{cwd}``, ``{endpoint}``. A token that resolves to an empty string is
    dropped — and so is the ``-flag`` immediately before it, because
    ``--model`` with nothing after it is not a command. ``env`` values take
    the same placeholders and a value that would be empty is left unset,
    which is what keeps a runner from being pointed at a nonexistent
    endpoint by default.
    """

    key: str
    label: str
    aliases: Tuple[str, ...] = ()
    kind: str = "cli"                       # "cli" | "app" (app = GUI, never a worker)
    licence: str = "unknown"                # "open" | "subscription" | "unknown"
    install: str = ""                       # how it is installed, for the UI
    argv: Tuple[str, ...] = ()              # {task} {model} {cwd} {endpoint}
    stdin_task: bool = False                # pass the task on stdin instead of argv
    env: Dict[str, str] = field(default_factory=dict)
    cwd_is_workspace: bool = True
    detect: Tuple[str, ...] = ()            # executables to look for on PATH
    notes: str = ""

    def runnable_as_worker(self) -> bool:
        """A GUI is never a worker, and neither is a row with no invocation."""
        return self.kind == "cli" and bool(self.argv)


# ── the built-in table ──────────────────────────────────────────────────────
# What `ollama launch --help` cannot say. Ordered as the help lists them so a
# reader can hold the two side by side.
#
# On the licence word, spelled out because the UI prints it verbatim:
#   * `subscription` — the agent needs a paid account with its vendor to do
#     anything (Anthropic, OpenAI, GitHub). Pointing it at a local Ollama is
#     exactly what `ollama launch` is for, but the binary is still theirs.
#   * `open`         — an openly licensed agent you can run without buying an
#     account from anyone.
#   * `unknown`      — this table does not know. `vscode` ships as a
#     proprietary Microsoft build of an open source tree (two different
#     licences, and the one you get depends on where you downloaded it), and
#     `hermes-desktop` is a desktop shell this table has no licence for. They
#     say `unknown` rather than borrow a neighbour's word.
#
# On argv: only invocations the tool itself documents as its non-interactive
# form are here. The rest are listed with `argv: ()` — visible, honest, and one
# table row away from working.
_BUILTIN: Tuple[Runner, ...] = (
    Runner(
        key="claude", label="Claude Code", kind="cli", licence="subscription",
        install="ollama launch claude",
        argv=("claude", "-p", "{task}", "--model", "{model}"),
        env={"ANTHROPIC_BASE_URL": "{endpoint}"},
        detect=("claude",),
        notes="Print mode (`claude -p`) runs one prompt and exits. `ollama launch claude` "
              "points it at the local Ollama; without an endpoint it uses whatever the "
              "user's own Claude Code config says.",
    ),
    Runner(
        key="chatgpt", label="ChatGPT", aliases=("codex-app", "codex-desktop", "codex-gui"),
        kind="app", licence="subscription", install="ollama launch chatgpt",
        detect=("chatgpt",),
        notes="The desktop app. A GUI cannot be a worker: there is no one-task, "
              "one-exit invocation to give it.",
    ),
    Runner(
        key="hermes", label="Hermes Agent", kind="cli", licence="open",
        install="ollama launch hermes", detect=("hermes",),
        notes="Known to Ollama. No non-interactive invocation is recorded here yet, so it "
              "is not runnable as a worker — add its argv to the table in src/agent_runners.py.",
    ),
    Runner(
        key="openclaw", label="OpenClaw", aliases=("clawdbot", "moltbot"),
        kind="cli", licence="open", install="ollama launch openclaw", detect=("openclaw",),
        notes="Known to Ollama. No non-interactive invocation is recorded here yet.",
    ),
    Runner(
        key="opencode", label="OpenCode", kind="cli", licence="open",
        install="ollama launch opencode",
        argv=("opencode", "run", "{task}", "--model", "{model}"),
        detect=("opencode",),
        notes="`opencode run <message>` is its documented headless form: one message, "
              "then exit.",
    ),
    Runner(
        key="codex", label="Codex", kind="cli", licence="subscription",
        install="ollama launch codex",
        argv=("codex", "exec", "{task}", "--model", "{model}"),
        env={"OPENAI_BASE_URL": "{endpoint}"},
        detect=("codex",),
        notes="`codex exec` is the non-interactive subcommand. It still runs its own "
              "shell for every command it decides to run.",
    ),
    Runner(
        key="hermes-desktop", label="Hermes Desktop", kind="app", licence="unknown",
        install="ollama launch hermes-desktop", detect=("hermes-desktop",),
        notes="A desktop shell: a GUI is never a worker. The licence word is `unknown` "
              "because this table has not verified one for it.",
    ),
    Runner(
        key="copilot", label="Copilot CLI", aliases=("copilot-cli",),
        kind="cli", licence="subscription", install="ollama launch copilot",
        detect=("copilot",),
        notes="Needs a GitHub Copilot subscription. No non-interactive invocation is "
              "recorded here yet.",
    ),
    Runner(key="omp", label="OMP", kind="cli", licence="open", install="ollama launch omp",
           detect=("omp",), notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(key="droid", label="Droid", kind="cli", licence="open", install="ollama launch droid",
           detect=("droid",), notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(key="dsh", label="DeepSeek Harness", aliases=("deepseek-harness",), kind="cli",
           licence="open", install="ollama launch dsh", detect=("dsh",),
           notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(key="kimi", label="Kimi Code CLI", kind="cli", licence="open", install="ollama launch kimi",
           detect=("kimi",), notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(key="muse", label="Muse Code", aliases=("muse-code",), kind="cli", licence="open",
           install="ollama launch muse", detect=("muse",),
           notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(key="pi", label="Pi", kind="cli", licence="open", install="ollama launch pi",
           detect=("pi",), notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(key="pool", label="Pool", kind="cli", licence="open", install="ollama launch pool",
           detect=("pool",), notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(key="cline", label="Cline", kind="cli", licence="open", install="ollama launch cline",
           detect=("cline",), notes="Known to Ollama. No non-interactive invocation recorded here yet."),
    Runner(
        key="qwen", label="Qwen Code", kind="cli", licence="open",
        install="ollama launch qwen",
        argv=("qwen", "-p", "{task}", "-m", "{model}"),
        detect=("qwen",),
        notes="`qwen -p <prompt>` is its documented non-interactive form.",
    ),
    Runner(
        key="vscode", label="VS Code", aliases=("code",), kind="app", licence="unknown",
        install="ollama launch vscode", detect=("code",),
        notes="An editor, not an agent runner: it opens a window and stays open. The "
              "licence word is `unknown` because the Microsoft build and the open source "
              "tree it is built from do not share one.",
    ),
)

_BY_KEY: Dict[str, Runner] = {r.key: r for r in _BUILTIN}
_BY_ALIAS: Dict[str, str] = {}
for _r in _BUILTIN:
    _BY_ALIAS[_r.key] = _r.key
    for _a in _r.aliases:
        _BY_ALIAS[_a] = _r.key

#: The note every runner without an argv carries in the catalogue.
NOT_RUNNABLE_NOTE = "known to Ollama, not runnable as a worker yet"


# ── settings ────────────────────────────────────────────────────────────────

def enabled() -> bool:
    """``agent_external_runners``. **Default off**: this feature runs
    third-party binaries on the user's machine, so it ships turned off and the
    user turns it on."""
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_external_runners", False))
    except Exception:  # noqa: BLE001 - never raise into a hot path
        return False


def timeout_s() -> int:
    """``agent_external_runner_timeout_s`` — the hard bound on one run."""
    try:
        from src.settings import get_setting
        value = int(get_setting("agent_external_runner_timeout_s", DEFAULT_TIMEOUT_S))
        return max(10, min(value, 7200))
    except Exception:  # noqa: BLE001
        return DEFAULT_TIMEOUT_S


# ── parsing the live help ───────────────────────────────────────────────────

# "  claude          Claude Code"
# "  openclaw        OpenClaw (aliases: clawdbot, moltbot)"
_ROW_RE = re.compile(r"^[ \t]{2,}([A-Za-z][\w.-]*)[ \t]{2,}(\S.*?)[ \t]*$")
_ALIAS_RE = re.compile(r"\((?:alias|aliases)\s*:\s*([^)]*)\)\s*$", re.IGNORECASE)
# The heading the integration list sits under, and the headings that end it.
_LIST_HEAD_RE = re.compile(r"^\s*(?:supported\s+)?integrations\s*:?\s*$", re.IGNORECASE)
_OTHER_HEAD_RE = re.compile(r"^\s*(?:flags|options|usage|examples|arguments|commands)\b.*:?\s*$",
                            re.IGNORECASE)


def parse_help(text: Any) -> Dict[str, Dict[str, Any]]:
    """``ollama launch --help`` → ``{key: {"label", "aliases"}}``.

    Reads the block under the integrations heading and stops at the next
    heading, so ``Flags:`` and its ``--model string`` lines are never mistaken
    for integrations. Total: junk in, empty dict out.
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        body = text if isinstance(text, str) else ("" if text is None else str(text))
        inside = False
        for raw in body.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            if _LIST_HEAD_RE.match(line):
                inside = True
                continue
            if not inside:
                continue
            if _OTHER_HEAD_RE.match(line) or not line[:1].isspace():
                break                      # the list is over
            m = _ROW_RE.match(line)
            if not m:
                continue
            key = m.group(1).strip()
            label = m.group(2).strip()
            aliases: List[str] = []
            am = _ALIAS_RE.search(label)
            if am:
                aliases = [a.strip() for a in am.group(1).split(",") if a.strip()]
                label = label[: am.start()].strip()
            if not key or key in out:
                continue
            out[key] = {"label": label or key, "aliases": tuple(aliases)}
    except Exception as e:  # noqa: BLE001 - an unparseable help is not an error
        logger.debug("agent_runners: could not parse the launch help: %s", e)
        return out
    return out


_help_cache: Dict[str, Any] = {"at": 0.0, "text": "", "ok": False}


def help_text(*, refresh: bool = False) -> str:
    """The live ``ollama launch --help`` output, cached for HELP_TTL_S.

    Never raises and never blocks for long: no ollama on PATH, a non-zero
    exit, a timeout — all answer with an empty string, and the caller falls
    back to the built-in table alone.
    """
    now = time.time()
    if not refresh and _help_cache["ok"] and (now - float(_help_cache["at"])) < HELP_TTL_S:
        return str(_help_cache["text"])
    text = ""
    try:
        exe = shutil.which("ollama")
        if exe:
            proc = subprocess.run([exe, "launch", "--help"], capture_output=True, text=True,
                                  timeout=HELP_TIMEOUT_S,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            text = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    except Exception as e:  # noqa: BLE001 - the catalogue never fails over this
        logger.debug("agent_runners: `ollama launch --help` unavailable: %s", e)
        text = ""
    _help_cache.update(at=now, text=text, ok=True)
    return text


def reset_cache() -> None:
    """Forget the cached help (tests, and the page's explicit refresh)."""
    _help_cache.update(at=0.0, text="", ok=False)


# ── the catalogue ───────────────────────────────────────────────────────────

def _which(names: Any, which: Any = None) -> Optional[str]:
    finder = which or shutil.which
    for name in names or ():
        try:
            found = finder(str(name))
        except Exception:  # noqa: BLE001
            found = None
        if found:
            return str(found)
    return None


def _merged(help_map: Dict[str, Dict[str, Any]]) -> List[Runner]:
    """Built-in rows first, in table order, enriched with the live label and
    aliases; then every integration the help knows and the table does not."""
    rows: List[Runner] = []
    for r in _BUILTIN:
        live = help_map.get(r.key)
        if live:
            aliases = tuple(dict.fromkeys(tuple(r.aliases) + tuple(live.get("aliases") or ())))
            rows.append(Runner(key=r.key, label=str(live.get("label") or r.label), aliases=aliases,
                               kind=r.kind, licence=r.licence, install=r.install, argv=r.argv,
                               stdin_task=r.stdin_task, env=dict(r.env),
                               cwd_is_workspace=r.cwd_is_workspace, detect=r.detect, notes=r.notes))
        else:
            rows.append(r)
    for key, live in help_map.items():
        if key in _BY_KEY or key in _BY_ALIAS:
            continue
        rows.append(Runner(
            key=key, label=str(live.get("label") or key), aliases=tuple(live.get("aliases") or ()),
            kind="cli", licence="unknown", install=f"ollama launch {key}",
            argv=(), detect=(key,),
            notes=f"{NOT_RUNNABLE_NOTE}: this Ollama lists it, and Faustus has no row saying how to "
                  f"run one task with it. Its licence is `unknown` for the same reason.",
        ))
    return rows


def runners(*, help_source: Any = None) -> List[Runner]:
    """Every known runner, merged. ``help_source`` is the help TEXT (a string)
    for tests and for a caller that already has it."""
    text = help_source if isinstance(help_source, str) else help_text()
    return _merged(parse_help(text))


def get(key: Any, *, help_source: Any = None) -> Optional[Runner]:
    """One runner by key or alias, or None. Never raises."""
    try:
        name = str(key or "").strip().lower()
        if not name:
            return None
        for r in runners(help_source=help_source):
            if r.key.lower() == name or name in {a.lower() for a in r.aliases}:
                return r
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("agent_runners: lookup of %r failed: %s", key, e)
        return None


def version_of(runner: Runner, *, which: Any = None) -> str:
    """``<bin> --version``, bounded — empty when it is not installed, is a
    GUI, or does not answer quickly. Only called when a caller asks for
    versions: it is one process per runner."""
    if runner.kind != "cli":
        return ""
    exe = _which(runner.detect, which)
    if not exe:
        return ""
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              timeout=VERSION_TIMEOUT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        out = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
        return " ".join(out.split())[:_VERSION_CHARS]
    except Exception as e:  # noqa: BLE001 - a version probe never fails a read
        logger.debug("agent_runners: `%s --version` unavailable: %s", exe, e)
        return ""


def to_row(runner: Runner, *, which: Any = None, versions: bool = False) -> Dict[str, Any]:
    """One catalogue row: the table's data plus what this machine has."""
    exe = _which(runner.detect, which)
    row: Dict[str, Any] = {
        "key": runner.key,
        "label": runner.label,
        "aliases": list(runner.aliases),
        "kind": runner.kind,
        "licence": runner.licence if runner.licence in LICENCES else "unknown",
        "install": runner.install or f"ollama launch {runner.key}",
        "launch_command": " ".join(launch_argv(runner.key, runner=runner)),
        "argv": list(runner.argv),
        "stdin_task": bool(runner.stdin_task),
        "env": dict(runner.env),
        "cwd_is_workspace": bool(runner.cwd_is_workspace),
        "detect": list(runner.detect),
        "installed": bool(exe),
        "path": exe or "",
        "runnable_as_worker": bool(runner.runnable_as_worker() and exe),
        "invocation_known": bool(runner.argv),
        "notes": runner.notes,
    }
    if versions:
        row["version"] = version_of(runner, which=which)
    return row


def catalogue(*, help_source: Any = None, which: Any = None, versions: bool = False) -> List[Dict[str, Any]]:
    """Every runner as a row: the table's data, ``installed`` resolved over
    this machine's PATH, ``runnable_as_worker``, and (on request) ``version``.

    Never raises: with no Ollama and no PATH it still answers the built-in
    table with everything marked not installed.
    """
    try:
        return [to_row(r, which=which, versions=versions) for r in runners(help_source=help_source)]
    except Exception as e:  # noqa: BLE001
        logger.debug("agent_runners: catalogue unavailable: %s", e)
        return []


def summary(*, help_source: Any = None, which: Any = None, versions: bool = False) -> Dict[str, Any]:
    """The catalogue plus the two facts a page needs above it: whether the
    feature is on, and the one thing it cannot promise."""
    rows = catalogue(help_source=help_source, which=which, versions=versions)
    return {
        "runners": rows,
        "enabled": enabled(),
        "timeout_s": timeout_s(),
        "installed_count": sum(1 for r in rows if r["installed"]),
        "runnable_count": sum(1 for r in rows if r["runnable_as_worker"]),
        "ollama_help": bool(help_text() if help_source is None else str(help_source or "").strip()),
        "guard_note": GUARD_NOTE,
    }


#: Said on the page, in the module docstring of src/external_worker.py, and in
#: every proof of a job that used one. One sentence, one meaning.
GUARD_NOTE = ("an external agent runs its own shell: Faustus's command guard does not see the "
              "commands it runs, only what changed on disk afterwards")


# ── the launch command (built, never run here) ──────────────────────────────

def launch_argv(key: Any, *, model: Optional[str] = None, config_only: bool = False,
                runner: Optional[Runner] = None, help_source: Any = None) -> List[str]:
    """The ``ollama launch …`` command for one integration.

    ``config_only`` uses ``--config`` (configure without launching); otherwise
    ``-y`` answers the installer's prompts, which is the only way a launch
    started from a web page can finish. **This function does not run
    anything.** An unknown key yields ``[]``.
    """
    try:
        r = runner or get(key, help_source=help_source)
        if r is None:
            return []
        argv = ["ollama", "launch", r.key]
        if config_only:
            argv.append("--config")
        else:
            argv.append("-y")
        m = str(model or "").strip()
        if m:
            argv += ["--model", m]
        return argv
    except Exception as e:  # noqa: BLE001
        logger.debug("agent_runners: launch command for %r unavailable: %s", key, e)
        return []


# ── building the command that runs ONE task ─────────────────────────────────

def _fill(token: str, values: Dict[str, str]) -> Tuple[str, bool]:
    """Substitute ``{name}`` placeholders. Returns (text, empty_placeholder):
    the flag is True when a placeholder in the token resolved to nothing, which
    is what makes the token — and its flag — droppable."""
    empty = False
    out = token
    for name, value in values.items():
        needle = "{" + name + "}"
        if needle in out:
            if not value:
                empty = True
            out = out.replace(needle, value)
    return out, empty


def build_argv(runner: Runner, task: str, *, model: Optional[str] = None,
               cwd: Optional[str] = None, endpoint: Optional[str] = None) -> List[str]:
    """The argv that runs ONE task with this runner.

    A token whose placeholder resolved to nothing is dropped, and so is the
    ``-flag`` right before it: ``--model {model}`` with no model must not
    become ``--model`` with nothing after it. With ``stdin_task`` the
    ``{task}`` token is dropped from the argv (the caller writes the task to
    stdin instead).
    """
    values = {"task": str(task or ""), "model": str(model or ""),
              "cwd": str(cwd or ""), "endpoint": str(endpoint or "")}
    out: List[str] = []
    for token in runner.argv:
        if runner.stdin_task and token.strip() == "{task}":
            continue
        text, empty = _fill(str(token), values)
        if empty:
            if out and out[-1].startswith("-"):
                out.pop()
            continue
        out.append(text)
    return out


def build_env(runner: Runner, *, base: Optional[Dict[str, str]] = None, model: Optional[str] = None,
              cwd: Optional[str] = None, endpoint: Optional[str] = None) -> Dict[str, str]:
    """The environment for one run: ``base`` (the process environment by
    default) plus the table's entries with placeholders filled. An entry whose
    value would be empty is LEFT UNSET — a runner is never pointed at an
    endpoint that was not given."""
    env = dict(os.environ if base is None else base)
    values = {"task": "", "model": str(model or ""), "cwd": str(cwd or ""),
              "endpoint": str(endpoint or "")}
    for name, raw in (runner.env or {}).items():
        text, empty = _fill(str(raw), values)
        if empty or not text:
            continue
        env[str(name)] = text
    return env


def table_env(runner: Runner, *, model: Optional[str] = None, cwd: Optional[str] = None,
              endpoint: Optional[str] = None) -> Dict[str, str]:
    """Only the entries this table adds (what ``argv_shown`` reports), not the
    whole inherited environment."""
    return build_env(runner, base={}, model=model, cwd=cwd, endpoint=endpoint)


__all__ = [
    "DEFAULT_TIMEOUT_S", "GUARD_NOTE", "KINDS", "LICENCES", "NOT_RUNNABLE_NOTE", "Runner",
    "build_argv", "build_env", "catalogue", "enabled", "get", "help_text", "launch_argv",
    "parse_help", "reset_cache", "runners", "summary", "table_env", "timeout_s", "to_row",
    "version_of",
]
