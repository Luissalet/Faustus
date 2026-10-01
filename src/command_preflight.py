"""Shell wrong-directory preflight.

A local model that was given a project root often runs ``python server.py``,
``npm run dev``, ``pytest tests/x.py`` or ``docker compose up`` from the root
while the target lives in a subfolder (``backend/server.py``). Every such miss
costs a full round trip at a few tokens per second.

``check`` parses the command (bash / PowerShell / cmd shaped), finds the file
or project marker the command needs, and when that target is missing relative
to the working directory it looks for it inside the workspace:

* exactly one directory has it  -> the command is rewritten to run from there
  (a ``cd`` is inserted right before the command) and a one-line note is added
  to the tool output;
* several directories have it   -> nothing runs; the caller returns an error
  that lists the candidates so the model can choose;
* none, or any doubt            -> the command runs unchanged (fail open).

Only the command shapes listed in ``_identify`` are ever looked at.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

MAX_CANDIDATES = 8
MAX_DEPTH = 5
MAX_DIRS = 6000
TIME_BUDGET_S = 1.5

SKIP_DIRS = frozenset({
    "node_modules", ".git", "venv", ".venv", "env", "dist", "build", "__pycache__",
    ".next", "target", ".tox", "site-packages", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", "coverage", ".cache", "out", "vendor",
})

_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
_COMPOSE_SUBS = frozenset({
    "up", "down", "build", "logs", "ps", "restart", "start", "stop", "run", "exec",
    "pull", "config", "rm", "kill", "top", "create",
})
_KNOWN_TOOL_MODULES = frozenset({
    "pip", "pytest", "venv", "uvicorn", "flask", "black", "ruff", "mypy", "isort",
    "build", "setuptools", "unittest", "coverage", "tox", "nox", "poetry", "pipx",
    "ensurepip", "http", "json", "idlelib",
})

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = re.compile(r"^\d*[<>]")
_MODULE_APP = re.compile(r"^([A-Za-z_][\w.]*):([A-Za-z_][\w.]*)$")
_DOUBT_CHARS = ("$", "*", "?", "%", "`", "{", "}", "~")


@dataclass
class PreflightResult:
    action: str = "none"            # "none" | "rewrite" | "refuse"
    command: str = ""               # the command to run (rewritten when action == "rewrite")
    note: str = ""                  # one-line note for the tool output
    candidates: List[str] = field(default_factory=list)   # workspace-relative dirs
    error: str = ""                 # message for action == "refuse"
    target: str = ""                # human label of what was searched for


# ---------------------------------------------------------------- parsing

def _split_segments(cmd: str) -> Optional[List[Tuple[int, int, str]]]:
    """Split on top-level ``&&``, ``||``, ``;``, ``|`` and newlines, honouring
    quotes. Returns ``(start, end, sep_before)`` triples or None on doubt."""
    out: List[Tuple[int, int, str]] = []
    n = len(cmd)
    i = 0
    seg_start = 0
    sep_before = ""
    quote = ""
    while i < n:
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        two = cmd[i:i + 2]
        sep = ""
        if two in ("&&", "||"):
            sep = two
        elif ch == ";":
            sep = ";"
        elif ch == "|":
            sep = "|"
        elif ch in ("\n", "\r"):
            sep = "\n"
        if sep:
            out.append((seg_start, i, sep_before))
            i += len(sep) if sep in ("&&", "||") else 1
            seg_start = i
            sep_before = sep
            continue
        i += 1
    if quote:
        return None
    out.append((seg_start, n, sep_before))
    return out


def _tokens(seg: str) -> Optional[List[str]]:
    toks: List[str] = []
    cur: List[str] = []
    quote = ""
    has = False
    for ch in seg:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            has = True
            continue
        if ch.isspace():
            if has or cur:
                toks.append("".join(cur))
                cur, has = [], False
            continue
        cur.append(ch)
        has = True
    if quote:
        return None
    if has or cur:
        toks.append("".join(cur))
    # Drop redirections (`> out`, `2>&1`, `>> f`).
    clean: List[str] = []
    skip = False
    for t in toks:
        if skip:
            skip = False
            continue
        if _REDIRECT.match(t):
            if re.fullmatch(r"\d*[<>]+", t):
                skip = True
            continue
        clean.append(t)
    return clean


def _norm_rel(token: str) -> Optional[str]:
    """A plain relative path (forward slashes) or None when it is absolute,
    dynamic, or otherwise not something to look for."""
    if not token:
        return None
    t = token.replace("\\", "/")
    if any(c in t for c in _DOUBT_CHARS):
        return None
    if t.startswith("/") or re.match(r"^[A-Za-z]:", t) or "://" in t:
        return None
    while t.startswith("./"):
        t = t[2:]
    if not t or t in (".", ".."):
        return None
    if t.startswith("../") or "/../" in t:
        return None
    return t.rstrip("/")


def _prog_name(tok: str) -> str:
    base = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    return base


def _strip_prefix(toks: List[str]) -> List[str]:
    i = 0
    while i < len(toks):
        t = toks[i]
        if _ENV_ASSIGN.match(t):
            if t.upper().startswith("COMPOSE_FILE=") or t.upper().startswith("COMPOSE_PROJECT_DIR"):
                return []
            i += 1
            continue
        if t in ("&", "time", "nohup", "sudo", "exec", "call", "command"):
            i += 1
            continue
        break
    return toks[i:]


Check = Callable[[str], bool]


def _file_check(rel: str) -> Check:
    parts = rel.split("/")
    return lambda d: os.path.isfile(os.path.join(d, *parts))


def _path_check(rel: str) -> Check:
    parts = rel.split("/")
    return lambda d: os.path.exists(os.path.join(d, *parts))


def _module_check(module: str) -> Check:
    parts = module.split(".")

    def check(d: str) -> bool:
        base = os.path.join(d, *parts)
        if os.path.isfile(base + ".py"):
            return True
        return os.path.isdir(base) and (
            os.path.isfile(os.path.join(base, "__init__.py"))
            or os.path.isfile(os.path.join(base, "__main__.py"))
        )
    return check


def _package_json_check(script: str, require_script: bool) -> Check:
    def check(d: str) -> bool:
        pj = os.path.join(d, "package.json")
        if not os.path.isfile(pj):
            return False
        if not require_script:
            return True
        try:
            with open(pj, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
            return script in (data.get("scripts") or {})
        except Exception:
            return False
    return check


def _compose_check(d: str) -> bool:
    return any(os.path.isfile(os.path.join(d, n)) for n in _COMPOSE_FILES)


@dataclass
class _Target:
    label: str
    exists_here: Check
    exists_there: Check


def _simple(label: str, chk: Check) -> _Target:
    return _Target(label, chk, chk)

def _py_script(args: List[str]) -> Optional[_Target]:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c":
            return None
        if a == "-m":
            if i + 1 >= len(args):
                return None
            return _py_module(args[i + 1], args[i + 2:])
        if a in ("-W", "-X", "-Q"):
            i += 2
            continue
        if a == "-":
            return None
        if a.startswith("-"):
            i += 1
            continue
        rel = _norm_rel(a)
        if rel is None or not rel.lower().endswith((".py", ".pyw")):
            return None
        return _simple(rel, _file_check(rel))
    return None


def _py_module(module: str, rest: List[str]) -> Optional[_Target]:
    if module in ("pytest", "py.test"):
        return _pytest(rest)
    if module == "uvicorn":
        return _uvicorn(rest)
    if not re.fullmatch(r"[A-Za-z_][\w.]*", module):
        return None
    top = module.split(".")[0]
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    if top in stdlib or top in _KNOWN_TOOL_MODULES:
        return None
    return _simple(module.replace(".", "/") + ".py", _module_check(module))


_PYTEST_VALUE_FLAGS = frozenset({
    "-k", "-m", "-p", "-o", "-n", "-W", "-r", "--ignore", "--deselect", "--durations",
    "--maxfail", "--tb", "--junitxml", "--basetemp", "--cov", "--log-level", "--ignore-glob",
    "--timeout", "--color", "--capture",
})


def _pytest(args: List[str]) -> Optional[_Target]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "--rootdir", "--confcutdir") or a.startswith(("--rootdir=", "--confcutdir=")):
            return None
        if a in _PYTEST_VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        path = a.split("::", 1)[0]
        if "/" in path or "\\" in path or path.lower().endswith(".py"):
            rel = _norm_rel(path)
            if rel is None:
                return None
            return _simple(rel, _path_check(rel))
        i += 1
    return None


def _uvicorn(args: List[str]) -> Optional[_Target]:
    if any(a == "--app-dir" or a.startswith("--app-dir=") for a in args):
        return None
    for a in args:
        if a.startswith("-"):
            continue
        m = _MODULE_APP.match(a)
        if m:
            mod = m.group(1)
            return _simple(mod.replace(".", "/") + ".py", _module_check(mod))
    return None


_NPM_FLAGS_STOP = ("--prefix", "-C", "--cwd", "--workspace", "-w", "--workspaces", "--dir", "--filter", "-F")


def _node_pm(pm: str, args: List[str]) -> Optional[_Target]:
    for a in args:
        if a in _NPM_FLAGS_STOP or a.startswith(("--prefix=", "--cwd=", "--workspace=", "--dir=", "--filter=")):
            return None
    pos = [a for a in args if not a.startswith("-")]
    if not pos:
        return None
    sub = pos[0]
    script = ""
    if sub in ("run", "run-script"):
        if len(pos) < 2:
            return None
        script = pos[1]
    elif pm == "npm" and sub in ("start", "test", "t"):
        script = "test" if sub == "t" else sub
    elif pm in ("yarn", "pnpm") and sub in ("start", "test", "dev", "build"):
        script = sub
    else:
        return None
    if any(c in script for c in _DOUBT_CHARS):
        return None
    return _Target("package.json", _package_json_check(script, False), _package_json_check(script, True))


def _compose(args: List[str]) -> Optional[_Target]:
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in ("-f", "--file", "--project-directory", "--env-file") or a.startswith(
                ("--file=", "--project-directory=", "--env-file=")):
            return None
        if a in ("-p", "--project-name", "--profile", "--ansi", "--parallel"):
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        if a in _COMPOSE_SUBS:
            return _simple("a docker compose file", _compose_check)
        return None
    return None


def _first_file_arg(args: List[str], exts: Tuple[str, ...]) -> Optional[_Target]:
    for a in args:
        if a.startswith("-"):
            continue
        rel = _norm_rel(a)
        if rel is None or not rel.lower().endswith(exts):
            return None
        return _simple(rel, _file_check(rel))
    return None


def _identify(toks: List[str]) -> Optional[_Target]:
    toks = _strip_prefix(toks)
    if not toks:
        return None
    prog_raw = toks[0]
    prog = _prog_name(prog_raw)
    args = toks[1:]
    if re.fullmatch(r"pythonw?(3(\.\d+)?)?|py", prog):
        return _py_script(args)
    if prog in ("pytest", "py.test"):
        return _pytest(args)
    if prog == "uvicorn":
        return _uvicorn(args)
    if prog == "node":
        for a in args:
            if a in ("-e", "--eval", "-p", "--print", "-"):
                return None
        return _first_file_arg(args, (".js", ".mjs", ".cjs", ".ts", ".jsx"))
    if prog in ("npm", "pnpm", "yarn"):
        return _node_pm(prog, args)
    if prog == "docker-compose":
        return _compose(args)
    if prog == "docker" and args and args[0] == "compose":
        return _compose(args[1:])
    if prog in ("bash", "sh", "zsh"):
        return _first_file_arg(args, (".sh",))
    if prog in ("powershell", "pwsh"):
        for i, a in enumerate(args):
            if a.lower() == "-file" and i + 1 < len(args):
                return _first_file_arg([args[i + 1]], (".ps1",))
        return None
    # ./script.sh  .\script.ps1
    if prog_raw.startswith(("./", ".\\")):
        rel = _norm_rel(prog_raw)
        if rel is not None and rel.lower().endswith((".sh", ".ps1", ".bat", ".cmd")):
            return _simple(rel, _file_check(rel))
    return None


# ---------------------------------------------------------------- search

def _search(root: str, check: Check, exclude: str) -> List[str]:
    """Directories under ``root`` (bounded) where ``check`` holds, shallowest first."""
    found: List[Tuple[int, str]] = []
    root = os.path.abspath(root)
    root_depth = root.rstrip("\\/").count(os.sep)
    t0 = time.monotonic()
    visited = 0
    excl = os.path.normcase(os.path.abspath(exclude))
    for dirpath, dirs, _files in os.walk(root, topdown=True):
        visited += 1
        depth = dirpath.rstrip("\\/").count(os.sep) - root_depth
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".") and d.lower() not in SKIP_DIRS
        )
        if depth >= MAX_DEPTH:
            dirs[:] = []
        if os.path.normcase(os.path.abspath(dirpath)) != excl:
            try:
                if check(dirpath):
                    found.append((depth, os.path.abspath(dirpath)))
            except Exception:
                pass
            if len(found) > MAX_CANDIDATES:
                break
        if visited >= MAX_DIRS or (time.monotonic() - t0) > TIME_BUDGET_S:
            break
    found.sort(key=lambda x: (x[0], x[1].lower()))
    return [p for _d, p in found]


def _fwd(p: str) -> str:
    return p.replace("\\", "/")


def _cd_arg(rel: str, shell: str) -> str:
    if shell == "powershell":
        return "'" + rel.replace("'", "''") + "'"
    if re.fullmatch(r"[A-Za-z0-9_./+@:-]+", rel):
        return rel
    return '"' + rel.replace('"', '\\"') + '"'


def _cd_prefix(rel: str, shell: str) -> str:
    if shell == "powershell":
        return f"Set-Location -LiteralPath {_cd_arg(rel, shell)}; "
    return f"cd {_cd_arg(rel, shell)} && "


def _resolve_cd(toks: List[str], vcwd: str) -> Optional[str]:
    """New virtual cwd after a ``cd``-like segment, or None on doubt."""
    args = [t for t in toks[1:] if t.lower() not in ("/d", "-literalpath", "-path")]
    if len(args) != 1:
        return None
    a = args[0]
    if any(c in a for c in _DOUBT_CHARS) or a == "-":
        return None
    a = a.replace("\\", "/")
    if re.match(r"^([A-Za-z]:)?/", a):
        new = os.path.normpath(a)
    else:
        new = os.path.normpath(os.path.join(vcwd, a))
    return new if os.path.isdir(new) else None


def _rel_display(path: str, root: str) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return _fwd(path)
    return _fwd(rel)

# ---------------------------------------------------------------- entry point

def check(command: str, cwd: str, shell: str = "bash") -> PreflightResult:
    """Inspect ``command`` for a wrong-directory miss. ``shell`` is ``bash`` or
    ``powershell`` (it only decides how an inserted ``cd`` is written). Never
    raises: any failure means "run it unchanged"."""
    try:
        return _check(command, cwd, shell)
    except Exception:
        return PreflightResult(command=command)


def _check(command: str, cwd: str, shell: str) -> PreflightResult:
    res = PreflightResult(command=command)
    if not command or not cwd or not os.path.isdir(cwd):
        return res
    if "<<" in command or "$(" in command:
        return res
    segs = _split_segments(command)
    if not segs:
        return res
    root = os.path.abspath(cwd)
    vcwd = root
    for start, end, sep_before in segs:
        text = command[start:end]
        toks = _tokens(text)
        if toks is None:
            return res
        if not toks:
            continue
        if _prog_name(toks[0]) in ("cd", "chdir", "set-location", "sl", "pushd"):
            nv = _resolve_cd(toks, vcwd)
            if nv is None:
                return res
            vcwd = nv
            continue
        target = _identify(toks)
        if target is None:
            continue
        # The first command-shaped segment decides; later ones are left alone.
        if sep_before in ("||", "|"):
            return res
        if target.exists_here(vcwd):
            return res
        cands = _search(root, target.exists_there, vcwd)
        if not cands:
            return res
        here = _rel_display(vcwd, root)
        if len(cands) > 1:
            shown = [_rel_display(c, root) for c in cands[:MAX_CANDIDATES]]
            more = len(cands) > len(shown)
            res.action = "refuse"
            res.candidates = shown
            res.target = target.label
            res.error = (
                f"preflight: {target.label} is not in {here} but it exists in several other "
                f"directories: {', '.join(shown)}{' (and more)' if more else ''}. "
                "Nothing was run. Re-run the command from the directory you mean "
                "(`cd <dir>` first)."
            )
            return res
        cand = cands[0]
        try:
            rel = _fwd(os.path.relpath(cand, vcwd))
        except ValueError:
            rel = _fwd(cand)
        lead = text[: len(text) - len(text.lstrip())]
        res.action = "rewrite"
        res.command = command[:start] + lead + _cd_prefix(rel, shell) + text.lstrip() + command[end:]
        shown_dir = _rel_display(cand, root)
        res.candidates = [shown_dir]
        res.target = target.label
        res.note = f"[preflight] ran from {shown_dir}/ because {target.label} is there"
        return res
    return res


# ---------------------------------------------------------------- tool glue

def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("shell_preflight_enabled", True))
    except Exception:
        return True


def apply(command: str, cwd: str, shell: str = "bash") -> PreflightResult:
    """``check`` behind the ``shell_preflight_enabled`` setting."""
    if not enabled():
        return PreflightResult(command=command)
    return check(command, cwd, shell)


def refusal(res: PreflightResult) -> dict:
    return {"error": res.error, "exit_code": 2,
            "preflight": {"action": "refuse", "candidates": res.candidates}}


def add_note(result: dict, note: str) -> dict:
    """Put the preflight note at the top of the tool output (in place)."""
    if not note or not isinstance(result, dict):
        return result
    result["preflight"] = {"action": "rewrite", "note": note}
    if isinstance(result.get("output"), str):
        result["output"] = note + "\n" + result["output"]
    elif isinstance(result.get("error"), str):
        result["error"] = note + "\n" + result["error"]
    else:
        result["output"] = note
    return result