"""static_checks.py — the static-analysis gate: names that do not exist.

`src/agent_harness.py` proves the files the turn changed still **parse**
(`py_compile`, `node --check`, `json.load`). Parsing is a very low bar. It
accepts `Depends(get_db)` with neither name imported, `self.metodo_que_no_existe`,
`from x import y` where `y` was never defined. And that — not broken syntax —
is the number-one failure of a small local model: compressing to 9B parameters
loses exactly the infrequent identifiers.

What it costs today: the model writes the route, `py_compile` says OK, the
project's tests run (40 s of wall clock at 4 tok/s), and it blows up with a
`NameError`. Or worse: no test covers that branch, the suite is green, the card
says *verified*, and the failure surfaces when the app boots.

This module closes that gap the way Aider does with `--auto-lint` on by
default: run the project's own correctness linter over the changed files and
feed the error back to the model. Three rules keep the signal honest.

**Correctness rules only, never style.** ruff runs with `--select F,E9` —
F821 undefined name, F811 redefinition, F401 unused import, E9 syntax/IO. An
unconfigured project has hundreds of style warnings and they would drown the
one line that matters. (This is also why `types` mode does not add mypy: on a
project without annotations it is a wall of `import-untyped` noise.)

**Only findings on lines this turn changed.** The checkpoint diff
(`src/workspace_checkpoints.diff_since`) says which lines of each file are new;
the `@@` headers are parsed and a finding is kept only when it sits on a line
the turn ADDED. A warning that was already there cannot spend a fix round —
exactly the rule `project_tests.compare_with_baseline` already applies to
failing tests, and for the same reason: a round spent on someone else's mess is
a round not spent on the user's request. The known cost of the strict rule: if
the turn deletes an import and the now-undefined use is 30 lines below, the
finding lands outside the diff and is not blamed on the turn. The project's
tests are the net for that; a false accusation is more expensive than a miss.

**Honest degradation.** With no tool installed the result is `unavailable`: no
fix round, no failure mark, and a message naming what to install. Never an
ImportError, never a silent false negative — a run we cannot interpret is
`inconclusive`, never "clean".

Order in the turn (`src/agent_loop.py`): syntax check → **static analysis** →
fix round if it fails → project tests. Failing in 0.2 s beats failing in 40 s.

The findings land in `TurnLedger.static_checks`, the seam
`static/js/agentHarnessUI.js` already paints and `src/scorecard.py` already
scores.

Stdlib only (plus an optional in-process `pyflakes`); never raises.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# The project's own interpreter (its venv holds its tools) and the kill-the-
# whole-tree timeout are already solved in project_tests — reused, not rewritten.
from src.project_tests import _clean_env, _kill_tree, _python_for

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
MAX_FILES = 20                 # changed files analysed per turn
MAX_FINDINGS = 25              # findings carried into the card / the fix message
MAX_MSG_CHARS = 300
_MAX_LINE_NO = 10_000_000      # anything above is parser garbage, not a line

RUFF_SELECT = "F,E9"           # F821/F811/F401 + syntax & IO errors. No style.

PY_EXTS = (".py", ".pyi")
JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
TS_EXTS = (".ts", ".tsx", ".mts", ".cts")
GO_EXTS = (".go",)
RS_EXTS = (".rs",)

MODES = ("off", "names", "types")


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def resolve_mode(value: Optional[str]) -> str:
    """`off` | `names` | `types`, falling back to the setting then to `names`."""
    raw = value if value not in (None, "") else _setting("agent_static_analysis", "names")
    mode = str(raw or "names").strip().lower()
    return mode if mode in MODES else "names"


# ---------------------------------------------------------------------------
# Where the tools live
# ---------------------------------------------------------------------------

def _find_tool(workspace: str, name: str) -> Optional[str]:
    """The project's own copy first (the venv `_python_for` points at), then PATH.
    A project that pinned ruff in its venv must be linted with THAT ruff."""
    try:
        py = _python_for(workspace)
    except Exception:                                   # pragma: no cover - defensive
        py = None
    if py:
        d = os.path.dirname(py)
        for cand in (name, name + ".exe", name + ".cmd"):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return shutil.which(name)


def _find_node_tool(workspace: str, name: str) -> Optional[str]:
    for cand in (name, name + ".cmd", name + ".exe"):
        p = os.path.join(workspace, "node_modules", ".bin", cand)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


def _pyflakes_importable() -> bool:
    """pyflakes runs IN-PROCESS: it only walks the AST (it never imports the
    code it checks), it is ~100 KB of pure Python with no dependencies, and it
    is the only path that also works in the frozen PyInstaller build, where
    `sys.executable -m pyflakes` would relaunch the whole app."""
    try:
        import importlib.util
        return importlib.util.find_spec("pyflakes.api") is not None
    except Exception:                                   # pragma: no cover - defensive
        return False


def _has_eslint_config(workspace: str) -> bool:
    for name in ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
                 ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
                 ".eslintrc.yml", ".eslintrc.yaml"):
        if os.path.isfile(os.path.join(workspace, name)):
            return True
    pkg = os.path.join(workspace, "package.json")
    try:
        if os.path.isfile(pkg):
            with open(pkg, "r", encoding="utf-8-sig", errors="replace") as f:
                return '"eslintConfig"' in f.read(200_000)
    except OSError:
        pass
    return False


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_checkers(workspace: str, *, mode: Optional[str] = None,
                    override: Optional[str] = None) -> List[Dict[str, Any]]:
    """What is actually available for this workspace, best first.

    Each entry: {"tool", "label", "exts", "argv"|"inprocess", "format",
    "whole_project", "cwd", "hint"}. `exts == ("*",)` means "every changed
    file" (the user's own override command). Never raises; empty means the
    gate is unavailable, which is not a failure."""
    if not workspace or not os.path.isdir(workspace):
        return []
    mode = resolve_mode(mode)
    if mode == "off":
        return []

    cmd = str(override if override not in (None, "") else
              _setting("agent_static_analysis_command", "") or "").strip()
    if cmd:
        try:
            argv = shlex.split(cmd, posix=(os.name != "nt"))
        except ValueError:
            argv = cmd.split()
        if argv:
            return [{
                "tool": argv[0], "label": cmd, "exts": ("*",), "argv": argv,
                "format": "generic", "whole_project": False, "cwd": workspace,
                "hint": "agent_static_analysis_command",
            }]

    out: List[Dict[str, Any]] = []

    # ── Python ────────────────────────────────────────────────────────────
    ruff = _find_tool(workspace, "ruff")
    if ruff:
        out.append({
            "tool": "ruff", "label": f"ruff check --select {RUFF_SELECT}", "exts": PY_EXTS,
            # --quiet drops the "Found N errors" footer, --no-cache keeps the
            # user's .ruff_cache out of it. The project's own config still
            # applies (excludes, per-file-ignores); only `select` is forced.
            "argv": [ruff, "check", "--select", RUFF_SELECT, "--output-format=concise",
                     "--no-cache", "--quiet"],
            "format": "generic", "whole_project": False, "cwd": workspace,
            "hint": "ruff", })
    elif _pyflakes_importable():
        out.append({
            "tool": "pyflakes", "label": "pyflakes", "exts": PY_EXTS,
            "inprocess": "pyflakes", "format": "generic", "whole_project": False,
            "cwd": workspace, "hint": "pyflakes", })
    else:
        pf = _find_tool(workspace, "pyflakes")
        if pf:
            out.append({
                "tool": "pyflakes", "label": "pyflakes", "exts": PY_EXTS, "argv": [pf],
                "format": "generic", "whole_project": False, "cwd": workspace,
                "hint": "pyflakes", })

    # ── JavaScript / TypeScript ───────────────────────────────────────────
    if _has_eslint_config(workspace):
        eslint = _find_node_tool(workspace, "eslint")
        if eslint:
            out.append({
                # --quiet = errors only: the project chose its rules, but its
                # warnings are still warnings and must not spend a fix round.
                "tool": "eslint", "label": "eslint --quiet", "exts": JS_EXTS,
                "argv": [eslint, "--format", "unix", "--no-color", "--quiet"],
                "format": "generic", "whole_project": False, "cwd": workspace,
                "hint": "eslint", })

    # ── Go ────────────────────────────────────────────────────────────────
    if os.path.isfile(os.path.join(workspace, "go.mod")):
        go = _find_tool(workspace, "go")
        if go:
            out.append({
                "tool": "go vet", "label": "go vet ./...", "exts": GO_EXTS,
                "argv": [go, "vet", "./..."], "format": "generic",
                "whole_project": True, "cwd": workspace, "hint": "go", })

    if mode != "types":
        return out

    # ── `types`: the whole-project type checkers (opt-in: they are slower) ──
    if os.path.isfile(os.path.join(workspace, "tsconfig.json")):
        tsc = _find_node_tool(workspace, "tsc")
        if tsc:
            out.append({
                "tool": "tsc", "label": "tsc --noEmit", "exts": TS_EXTS,
                "argv": [tsc, "--noEmit", "--pretty", "false"], "format": "tsc",
                "whole_project": True, "cwd": workspace, "hint": "typescript", })
    if os.path.isfile(os.path.join(workspace, "Cargo.toml")):
        cargo = _find_tool(workspace, "cargo")
        if cargo:
            out.append({
                # Only in `types`: cargo check COMPILES the crate — seconds to
                # minutes, not the 0.2 s this gate promises.
                "tool": "cargo check", "label": "cargo check", "exts": RS_EXTS,
                "argv": [cargo, "check", "--message-format", "short", "--quiet"],
                "format": "generic", "whole_project": True, "cwd": workspace,
                "hint": "cargo", })
    return out


def _install_hint(exts: Iterable[str]) -> str:
    """What to install so the gate works, named for the languages that changed."""
    exts = {e.lower() for e in exts}
    bits: List[str] = []
    if exts & set(PY_EXTS):
        bits.append("pip install pyflakes (or ruff) in the project's environment")
    if exts & set(JS_EXTS):
        bits.append("npm install --save-dev eslint plus an eslint config")
    if exts & set(GO_EXTS):
        bits.append("install the Go toolchain (go vet)")
    if exts & set(RS_EXTS):
        bits.append("install cargo and set agent_static_analysis to \"types\"")
    if not bits:
        bits.append("pip install pyflakes (or ruff) in the project's environment")
    return ("no static analysis tool available for the files this turn changed — "
            + "; ".join(bits) + ", or set agent_static_analysis_command")


# ---------------------------------------------------------------------------
# Output parsing — never trust the shape, never invent a finding
# ---------------------------------------------------------------------------

# Colour escapes must go before anything is matched: ruff (and eslint, and
# cargo) decide to colourise on heuristics we do not control — see _tool_env.
_ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", str(text or "")).replace("\x00", "")


# `path:line:col: rest`. The column is REQUIRED: without it "not:a:number: boom"
# and "a.py:not_a_line:1: F821 x" both parse into fictional findings. Every tool
# we drive (ruff, pyflakes, eslint --format unix, go vet, cargo short) emits it.
_GENERIC_RE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^:]+(?::[^:\d][^:]*)*):(?P<line>\d+):(?P<col>\d+):\s*(?P<rest>\S.*)$")
# `a.ts(1,7): error TS2322: message`
_TSC_RE = re.compile(r"^(?P<path>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*(?:error|warning)\s+(?P<code>TS\d+):\s*(?P<msg>\S.*)$")
# A leading rule id: ruff `F821 ...`, cargo `error[E0425]: ...`, eslint `... [Error/no-undef]`.
_CODE_RE = re.compile(r"^(?P<code>[A-Z]{1,4}\d{2,5})\b\s*(?:\[\*\])?\s*:?\s*(?P<rest>.*)$")
_RUSTC_RE = re.compile(r"^(?:error|warning)\[(?P<code>[A-Z]\d+)\]:?\s*(?P<rest>.*)$")
_ESLINT_RULE_RE = re.compile(r"^(?P<rest>.*?)\s*\[(?:Error|Warning)/(?P<code>[\w./@-]+)\]\s*$")

# pyflakes has no rule codes; its message set maps 1:1 onto ruff's F rules, so
# the model sees the same vocabulary whichever tool ran. Order matters.
_PYFLAKES_CODES: Tuple[Tuple[str, str], ...] = (
    ("is assigned to but never used", "F841"),
    ("referenced before assignment", "F821"),
    ("undefined name", "F821"),
    ("may be undefined, or defined from star imports", "F405"),
    ("unable to detect undefined names", "F403"),
    ("imported but unused", "F401"),
    ("redefinition of unused", "F811"),
    ("f-string is missing placeholders", "F541"),
    ("invalid syntax", "E999"),
)


def _norm_path(raw: str) -> str:
    p = (raw or "").strip().strip('"')
    if p.startswith("vet: "):            # `go vet` prefixes its own findings
        p = p[5:]
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _code_and_msg(rest: str) -> Tuple[str, str]:
    rest = (rest or "").strip()
    m = _RUSTC_RE.match(rest)
    if m:
        return m.group("code"), m.group("rest").strip()
    m = _ESLINT_RULE_RE.match(rest)
    if m:
        return m.group("code"), m.group("rest").strip()
    m = _CODE_RE.match(rest)
    if m:
        return m.group("code"), m.group("rest").strip()
    low = rest.lower()
    for needle, code in _PYFLAKES_CODES:
        if needle in low:
            return code, rest
    return "", rest


def parse_findings(text: str, fmt: str = "generic") -> List[Dict[str, Any]]:
    """Tool output → [{"path", "line", "col", "code", "msg"}]. Lines that do not
    look like a finding are dropped, not guessed at. Never raises."""
    out: List[Dict[str, Any]] = []
    if not text:
        return out
    try:
        for raw in _plain(text).splitlines():
            line = raw.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            if fmt == "tsc":
                m = _TSC_RE.match(line)
                if not m:
                    continue
                code, msg = m.group("code"), m.group("msg").strip()
            else:
                m = _GENERIC_RE.match(line)
                if not m:
                    continue
                code, msg = _code_and_msg(m.group("rest"))
            try:
                ln = int(m.group("line"))
            except (TypeError, ValueError):              # pragma: no cover - regex guarantees digits
                continue
            path = _norm_path(m.group("path"))
            if not path or not msg or ln < 1 or ln > _MAX_LINE_NO:
                continue
            try:
                col = int(m.group("col"))
            except (TypeError, ValueError):              # pragma: no cover
                col = 0
            out.append({"path": path, "line": ln, "col": col,
                        "code": code, "msg": msg[:MAX_MSG_CHARS]})
    except Exception as e:                               # noqa: BLE001 - never break the turn
        logger.debug("[static] parsing tool output failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# The turn's diff: which lines is this turn responsible for?
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text: str) -> Set[int]:
    """New-file line numbers this diff ADDED. Context lines do not count: they
    are lines the turn did not write, and blaming them would spend fix rounds
    on pre-existing warnings. A brand-new file is all `+`, so it is fully
    covered. Never raises."""
    added: Set[int] = set()
    if not diff_text:
        return added
    cur = 0
    in_hunk = False
    for line in str(diff_text).splitlines():
        m = _HUNK_RE.match(line)
        if m:
            try:
                cur = int(m.group(1))
            except (TypeError, ValueError):              # pragma: no cover
                in_hunk = False
                continue
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            if 0 < cur <= _MAX_LINE_NO:
                added.add(cur)
            cur += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        elif line.startswith(" ") or line == "":
            cur += 1
        else:
            in_hunk = False          # left the hunk body (next `diff --git`, etc.)
    return added


def changed_lines(workspace: str, checkpoint_sha: Optional[str],
                  paths: Sequence[str]) -> Dict[str, Set[int]]:
    """{rel path: lines this turn added}, from the checkpoint's per-file diff.
    Empty when there is no checkpoint — the caller must then treat the run as
    inconclusive rather than blame the turn for what it may not have written."""
    out: Dict[str, Set[int]] = {}
    if not workspace or not checkpoint_sha:
        return out
    try:
        from src import workspace_checkpoints as wc
    except Exception:                                    # pragma: no cover - import fallback
        return out
    for rel in paths:
        try:
            diff = wc.diff_since(workspace, checkpoint_sha, os.path.join(workspace, *rel.split("/")))
        except Exception as e:                           # noqa: BLE001
            logger.debug("[static] diff for %s failed: %s", rel, e)
            continue
        if diff:
            out[rel] = parse_hunks(diff)
    return out


# ---------------------------------------------------------------------------
# Running a checker
# ---------------------------------------------------------------------------

def _rel_files(workspace: str, paths: Iterable[str]) -> List[str]:
    """Existing, workspace-relative, de-duplicated, capped."""
    root = os.path.realpath(workspace)
    out: List[str] = []
    seen: Set[str] = set()
    for raw in paths or []:
        if not raw or len(out) >= MAX_FILES:
            continue
        try:
            p = raw if os.path.isabs(raw) else os.path.join(root, raw)
            p = os.path.realpath(p)
            rel = os.path.relpath(p, root)
        except (OSError, ValueError):
            continue
        if rel.startswith("..") or not os.path.isfile(p):
            continue
        rel = rel.replace(os.sep, "/")
        key = rel.lower() if os.name == "nt" else rel
        if key in seen:
            continue
        seen.add(key)
        out.append(rel)
    return out


def _matches(rel: str, checker: Dict[str, Any]) -> bool:
    exts = checker.get("exts") or ()
    if "*" in exts:
        return True
    return os.path.splitext(rel)[1].lower() in exts


def _run_inprocess_pyflakes(workspace: str, rels: Sequence[str]) -> Tuple[Optional[int], str, str]:
    """pyflakes over the changed files without spawning anything."""
    import io
    try:
        from pyflakes import api as _api
        from pyflakes import reporter as _reporter
    except Exception as e:                               # noqa: BLE001
        return None, "", f"pyflakes is not importable: {e}"
    warn, err = io.StringIO(), io.StringIO()
    rep = _reporter.Reporter(warn, err)
    ran = 0
    for rel in rels:
        p = os.path.join(workspace, *rel.split("/"))
        try:
            with open(p, "rb") as f:
                # utf-8-sig: Python's compiler accepts a UTF-8 BOM in bytes
                # but rejects the U+FEFF *character* of a decoded str
                # ("invalid non-printable character"). Files written by
                # PowerShell / Notepad carry one; it is not a defect.
                text = f.read().decode("utf-8-sig", "replace")
        except OSError:
            continue
        try:
            _api.check(text, rel, rep)
            ran += 1
        except Exception as e:                           # noqa: BLE001 - a broken file is not our crash
            logger.debug("[static] pyflakes failed on %s: %s", rel, e)
    if not ran:
        return None, "", "pyflakes could not read any of the changed files"
    return 0, warn.getvalue(), err.getvalue()


def _tool_env() -> Dict[str, str]:
    """project_tests._clean_env plus a hard colour-off.

    `_clean_env` sets FORCE_COLOR=0 for the test runners, but ruff's colour
    library (anstyle) treats FORCE_COLOR as *present = force colour on*
    regardless of its value — every finding came back as
    `\x1b[1msrc/api.py\x1b[0m\x1b[36m:\x1b[0m2…` and matched no regex.
    Measured against ruff 0.15, not assumed."""
    env = _clean_env()
    env.pop("FORCE_COLOR", None)
    env.pop("CLICOLOR_FORCE", None)
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb"})
    return env


def _run_tool(argv: Sequence[str], cwd: str, timeout: float) -> Tuple[Optional[int], str, str, str]:
    """(returncode, stdout, stderr, launch_error). returncode None = it did not
    finish. The timeout kills the whole tree (project_tests._kill_tree): a tool
    that shells out leaves children holding the pipes otherwise."""
    kwargs: Dict[str, Any] = dict(
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=_tool_env(), stdin=subprocess.DEVNULL,
    )
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(list(argv), **kwargs)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", "", f"could not run: {e}"[:200]
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or "", ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=15)
        except (subprocess.TimeoutExpired, OSError, ValueError):     # pragma: no cover
            pass
        return None, "", "", f"timed out after {int(timeout)} s"


def _relativise(workspace: str, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Absolute finding paths (eslint --format unix emits them) → workspace-relative,
    so they match the changed-file list exactly. Anything outside the workspace is
    left alone and will simply match nothing."""
    root = os.path.realpath(workspace).replace("\\", "/")
    low = root.lower() if os.name == "nt" else root
    for f in findings:
        p = f.get("path") or ""
        cand = p.lower() if os.name == "nt" else p
        if cand.startswith(low + "/"):
            f["path"] = p[len(root) + 1:]
    return findings


def _entry_from_run(path: str, tool: str, *, rc: Optional[int], out: str, err: str,
                    fmt: str = "generic", launch_error: str = "",
                    findings: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One `{path, tool, ok, errors}` entry from a checker run.

    `ok` is True (clean), False (findings) or **None** (we could not tell).
    None is the important one: a tool that exited non-zero with output we
    could not parse must never be reported as clean — that is the silent false
    negative this module exists to avoid."""
    if findings is None:
        findings = parse_findings((out or "") + ("\n" + err if err else ""), fmt)
    key = path.lower() if os.name == "nt" else path
    mine = [f for f in findings
            if (f["path"].lower() if os.name == "nt" else f["path"]) == key]
    entry: Dict[str, Any] = {"path": path, "tool": tool, "ok": True, "errors": [], "error": None}
    if mine:
        entry["ok"] = False
        entry["errors"] = [{"line": f["line"], "code": f["code"], "msg": f["msg"]}
                           for f in mine[:MAX_FINDINGS]]
        entry["error"] = _summarise_errors(tool, entry["errors"])
        return entry
    if launch_error:
        entry.update(ok=None, error=f"{tool}: {launch_error}")
    elif rc is None:
        entry.update(ok=None, error=f"{tool}: did not finish")
    elif rc != 0 and not findings:
        entry.update(ok=None, error=f"{tool} exited {rc} with no output we could parse: "
                                    + " ".join((_plain(err or out or "").strip().splitlines() or [""])[:2])[:200])
    return entry


def _summarise_errors(tool: str, errors: Sequence[Dict[str, Any]]) -> str:
    first = errors[0]
    head = f"{tool}: " + " ".join(x for x in (first.get("code"), first.get("msg")) if x)
    head += f" (line {first.get('line')})"
    if len(errors) > 1:
        head += f" +{len(errors) - 1} more"
    return head[:400]


def run_for_files(workspace: str, paths: Iterable[str], timeout: Optional[float] = None, *,
                  checkers: Optional[List[Dict[str, Any]]] = None,
                  mode: Optional[str] = None,
                  override: Optional[str] = None) -> List[Dict[str, Any]]:
    """Analyse `paths` and return one `{path, tool, ok, errors:[{line, code, msg}]}`
    per file a checker covered. Files no tool handles are skipped (not reported).
    Never raises."""
    if not workspace:
        return []
    rels = _rel_files(workspace, paths)
    if not rels:
        return []
    if checkers is None:
        checkers = detect_checkers(workspace, mode=mode, override=override)
    if not checkers:
        return []
    try:
        tmo = float(timeout if timeout is not None else DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        tmo = DEFAULT_TIMEOUT_S
    tmo = max(2.0, min(tmo, 900.0))

    results: List[Dict[str, Any]] = []
    done: Set[str] = set()
    for checker in checkers:
        mine = [r for r in rels if r not in done and _matches(r, checker)]
        if not mine:
            continue
        cwd = checker.get("cwd") or workspace
        fmt = checker.get("format") or "generic"
        if checker.get("inprocess") == "pyflakes":
            rc, out, err = _run_inprocess_pyflakes(workspace, mine)
            launch_error = err if rc is None else ""
            if rc is None:
                out, err = "", ""
        else:
            argv = list(checker.get("argv") or [])
            if not argv:
                continue
            if not checker.get("whole_project"):
                argv = argv + list(mine)
            rc, out, err, launch_error = _run_tool(argv, cwd, tmo)
        parsed = ([] if launch_error else
                  _relativise(workspace, parse_findings((out or "") + ("\n" + err if err else ""), fmt)))
        for rel in mine:
            results.append(_entry_from_run(rel, checker.get("tool") or "linter", rc=rc, out=out,
                                           err=err, fmt=fmt, launch_error=launch_error,
                                           findings=parsed))
            done.add(rel)
    return results


# ---------------------------------------------------------------------------
# The turn-level gate
# ---------------------------------------------------------------------------

def run_for_turn(workspace: str, changed: Iterable[str], *, checkpoint_sha: Optional[str] = None,
                 override: Optional[str] = None, mode: Optional[str] = None,
                 timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Detect, run, and adjudicate against the turn's diff.

    None when the feature is off. Otherwise a verdict:
      ran/available/inconclusive/ok, `findings` (the ones this turn owns),
      `ignored` (findings outside the diff), `results` (per file), `summary`.
    `ok is None` + `inconclusive` = could not tell; it must cost no fix round."""
    t0 = time.time()
    if not workspace:
        return None
    mode = resolve_mode(mode)
    if mode == "off":
        return None
    rels = _rel_files(workspace, changed)
    res: Dict[str, Any] = {
        "ran": False, "mode": mode, "available": False, "inconclusive": True, "ok": None,
        "tools": [], "findings": [], "ignored": 0, "results": [], "summary": "",
        "unavailable": "", "duration_s": 0.0,
    }
    if not rels:
        res["summary"] = "no analysable file changed"
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    checkers = detect_checkers(workspace, mode=mode, override=override)
    covered = [r for r in rels if any(_matches(r, c) for c in checkers)]
    if not checkers or not covered:
        # "No tool installed" and "nothing checkable was changed" are different
        # answers: only the first is worth telling the user to install something.
        exts = [os.path.splitext(r)[1].lower() for r in rels]
        checkable = set(PY_EXTS + JS_EXTS + GO_EXTS + RS_EXTS)
        if any(e in checkable for e in exts):
            res["unavailable"] = _install_hint(exts)
            logger.info("[harness] static analysis unavailable: %s", res["unavailable"])
        res["summary"] = res["unavailable"] or "no file with a static checker was changed"
        res["duration_s"] = round(time.time() - t0, 2)
        return res
    res["available"] = True
    res["tools"] = list(dict.fromkeys(c.get("tool") for c in checkers if any(_matches(r, c) for r in rels)))

    results = run_for_files(workspace, covered, timeout, checkers=checkers)
    res["results"] = results
    res["ran"] = bool(results)
    if not results:
        res["summary"] = "no file was analysed"
        res["duration_s"] = round(time.time() - t0, 2)
        return res

    # Adjudicate: only the lines this turn added can spend a fix round.
    lines = changed_lines(workspace, checkpoint_sha, [e["path"] for e in results])
    if not lines:
        res["inconclusive"] = True
        res["summary"] = (("no turn checkpoint" if not checkpoint_sha else
                           "no checkpoint diff for the changed files")
                          + " — findings are not attributable to this turn")
        res["duration_s"] = round(time.time() - t0, 2)
        logger.info("[harness] static analysis: %s", res["summary"])
        return res

    findings: List[Dict[str, Any]] = []
    ignored = 0
    inconclusive_files = 0
    for entry in results:
        owned = lines.get(entry["path"])
        if entry["ok"] is None:
            inconclusive_files += 1
            continue
        if owned is None:
            ignored += len(entry.get("errors") or [])
            entry["ok"] = True
            entry["errors"] = []
            entry["error"] = None
            continue
        keep = [e for e in (entry.get("errors") or []) if e.get("line") in owned]
        ignored += len(entry.get("errors") or []) - len(keep)
        entry["errors"] = keep
        entry["ok"] = not keep
        entry["error"] = _summarise_errors(entry["tool"], keep) if keep else None
        for e in keep:
            findings.append({"path": entry["path"], "line": e["line"], "code": e["code"],
                             "msg": e["msg"], "tool": entry["tool"]})

    res["findings"] = findings[:MAX_FINDINGS]
    res["ignored"] = ignored
    decided = [e for e in results if e["ok"] is not None]
    res["inconclusive"] = not decided
    res["ok"] = None if res["inconclusive"] else not findings
    n_files = len({f["path"] for f in findings})
    tools = ", ".join(t for t in res["tools"] if t)
    if res["inconclusive"]:
        res["summary"] = f"{tools or 'static analysis'} could not be interpreted"
    elif findings:
        res["summary"] = (f"{len(findings)} problem{'' if len(findings) == 1 else 's'} in "
                          f"{n_files} file{'' if n_files == 1 else 's'} ({tools})")
    else:
        res["summary"] = f"clean ({tools})" + (f", {ignored} pre-existing ignored" if ignored else "")
    res["duration_s"] = round(time.time() - t0, 2)
    logger.info("[harness] static analysis (%s, %s): ok=%s %s in %ss", mode, tools, res["ok"],
                res["summary"], res["duration_s"])
    if inconclusive_files and not res["inconclusive"]:
        logger.debug("[static] %s file(s) could not be interpreted", inconclusive_files)
    return res


def fix_message(res: Dict[str, Any]) -> str:
    """The bounded fix-round instruction for the model — the same envelope the
    syntax check and project_tests use, so the model reads one voice."""
    tools = ", ".join(t for t in (res.get("tools") or []) if t) or "static analysis"
    lines = [
        "[Harness check — automatic message from the runtime, not from the user]",
        f"A static check of the lines you changed FAILED ({tools}). These names/imports "
        "do not resolve — the file parses, but the code cannot run:",
    ]
    for f in (res.get("findings") or [])[:MAX_FINDINGS]:
        code = (f.get("code") or "").strip()
        lines.append(f"- {f.get('path')}:{f.get('line')}: " + (f"{code} " if code else "") + str(f.get("msg") or ""))
    lines.append(
        "Every one of these is on a line YOU wrote this turn (pre-existing warnings are "
        "already filtered out). Read the region with read_file (offset/limit) and fix the "
        "CAUSE with edit_file: add the missing import, correct the name, or remove the dead "
        "reference. Do not silence it with a noqa/eslint-disable comment, and do not describe "
        "the fix — apply it."
    )
    return "\n".join(lines)


def compact(res: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What gets persisted with the message / emitted to the UI."""
    if not res:
        return None
    out = {k: res.get(k) for k in ("ran", "mode", "available", "inconclusive", "ok", "tools",
                                   "summary", "ignored", "duration_s")}
    if res.get("unavailable"):
        out["unavailable"] = str(res["unavailable"])[:400]
    out["findings"] = [{"path": f.get("path"), "line": f.get("line"), "code": f.get("code"),
                        "msg": str(f.get("msg") or "")[:MAX_MSG_CHARS], "tool": f.get("tool")}
                       for f in (res.get("findings") or [])[:MAX_FINDINGS]]
    return out


def ledger_entries(res: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The per-file records for `TurnLedger.static_checks` — the seam the UI
    (static/js/agentHarnessUI.js) and the scorecard (src/scorecard.py) already
    read. Files we could not interpret get NO entry: `ok is None` would be
    counted as a broken file by the scorecard, and inconclusive is not broken."""
    if not res or not res.get("ran"):
        return []
    if res.get("inconclusive"):
        return []
    return [{"path": e["path"], "tool": e["tool"], "ok": bool(e["ok"]),
             "errors": list(e.get("errors") or []), "error": e.get("error")}
            for e in (res.get("results") or []) if e.get("ok") is not None]


def merge_static_checks(existing: Optional[List[Dict[str, Any]]],
                        entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold analysis entries into the syntax-check list IN PLACE of the entry
    for the same file. Appending a second entry per path would double every
    chip in the Verified card and double-count in the scorecard."""
    out: List[Dict[str, Any]] = [dict(e) for e in (existing or []) if isinstance(e, dict)]
    index = {}
    for i, e in enumerate(out):
        key = str(e.get("path") or "")
        index.setdefault(key.lower() if os.name == "nt" else key, i)
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        key = str(entry["path"])
        key = key.lower() if os.name == "nt" else key
        i = index.get(key)
        if i is None:
            out.append(dict(entry))
            index[key] = len(out) - 1
            continue
        cur = out[i]
        cur["tool"] = entry.get("tool")
        cur["errors"] = list(entry.get("errors") or [])
        if not entry.get("ok"):
            # A syntax failure already recorded for this file stays the
            # headline; otherwise the analysis result becomes it.
            cur["ok"] = False
            if cur.get("error"):
                cur["error"] = f"{cur['error']}; {entry.get('error')}"
            else:
                cur["error"] = entry.get("error")
    return out
