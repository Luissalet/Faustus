"""project_tests.py — run the project's own tests after an agent turn that changed files.

The reliability harness (src/agent_harness.py) proves that the model's *claims*
match what it did and that the changed files still parse. This module adds the
missing step: does the change *work*? It detects the project's test runner,
runs it bounded (timeout, output cap, no interactive input), and returns a
structured verdict the agent loop can act on (one fix round) and the UI can
show in the Verified card.

Detection (first match wins):
  1. an explicit command (project setting / global setting);
  2. pytest   — pytest.ini, pyproject [tool.pytest…], setup.cfg [tool:pytest],
                conftest.py or a tests/ folder with test files;
  3. npm test — package.json with a real "test" script;
  4. cargo test / go test ./... / make test.

Scope: with `scope="related"` (default) pytest only runs the test files that
name a changed module (`test_<stem>*.py`, `<stem>_test.py`, changed test files
themselves); node tests (`test_*.mjs` / `*.test.js`) for changed JS sources
run via `node --test` in the same turn. When nothing Python-related matches,
pytest does not fall back to the whole suite just because a node test exists.
Other runners always run their whole suite.

Stdlib only; never raises.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional

from src import output_oracle
from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300
OUTPUT_CAP = 200_000
TAIL_CHARS = 3_000
_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.py|tests?\.py)$", re.I)
_JS_TEST_FILE_RE = re.compile(r"\.(?:test|spec)\.(?:[cm]?js|[jt]sx?)$", re.I)
# node --test convention (Silhouettes tests/editor/test_gestures.mjs), not Jest's *.test.js.
_NODE_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]*\.(?:mjs|cjs|js)|[^/]*_test\.(?:mjs|cjs|js))$", re.I)


def _is_node_test_file(path: str) -> bool:
    rel = (path or "").replace("\\", "/")
    return bool(_NODE_TEST_FILE_RE.search(rel) or _JS_TEST_FILE_RE.search(rel))


def _is_any_test_file(path: str) -> bool:
    rel = (path or "").replace("\\", "/")
    return bool(_TEST_FILE_RE.search(rel) or _is_node_test_file(rel))


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _python_for(workspace: str) -> Optional[str]:
    """The project's own interpreter when it has a venv (its deps live there);
    None when there is none (the caller decides whether to fall back)."""
    candidates = (
        (".venv", "Scripts", "python.exe"), ("venv", "Scripts", "python.exe"),
        (".venv", "bin", "python"), ("venv", "bin", "python"),
        ("env", "Scripts", "python.exe"), ("env", "bin", "python"),
    )
    for parts in candidates:
        p = os.path.join(workspace, *parts)
        if os.path.isfile(p):
            return p
    return None


def _fallback_python(workspace: str = "") -> Optional[str]:
    """The same interpreter the `python` tool would pick when there is no
    project venv: PATH after Faustus's own venv is scrubbed, never the frozen
    app.

    Preferring `sys.executable` first made pytest run under Faustus's
    site-packages. A workspace that had installed jsonschema into the host
    Python then failed collection, and the agent burned a fix round on a
    ModuleNotFoundError that was not its change. `project_python` is the
    single picker so the two paths cannot drift.

    Never the frozen executable: there it is the app's own Faustus.exe, which
    ignores `-m pytest` and boots a second copy of the application."""
    try:
        from src.agent_tools.subprocess_tools import project_python
        py = project_python(workspace or "", native_host_environment())
    except Exception:                                   # pragma: no cover
        py = None
    if not py:
        try:
            from src.agent_harness import host_python
            py = host_python()
        except Exception:                               # pragma: no cover
            py = None if getattr(sys, "frozen", False) else sys.executable
    if not py:
        return None
    if getattr(sys, "frozen", False):
        try:
            if os.path.realpath(py) == os.path.realpath(sys.executable):
                return None
        except (OSError, ValueError):                   # pragma: no cover
            return None
    return py


def _has_pytest_config(workspace: str) -> bool:
    if os.path.isfile(os.path.join(workspace, "pytest.ini")):
        return True
    if os.path.isfile(os.path.join(workspace, "conftest.py")):
        return True
    for name, marker in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]")):
        p = os.path.join(workspace, name)
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if marker in f.read(200_000):
                        return True
        except OSError:
            continue
    return False


def _has_python_tests(workspace: str) -> bool:
    for d in ("tests", "test"):
        p = os.path.join(workspace, d)
        if not os.path.isdir(p):
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [x for x in dirnames if not x.startswith(".") and x != "__pycache__"]
                if any(_TEST_FILE_RE.search(fn) for fn in filenames):
                    return True
        except OSError:
            continue
    try:
        return any(_TEST_FILE_RE.search(fn) for fn in os.listdir(workspace) if fn.startswith("test_"))
    except OSError:
        return False


def _npm_test_script(workspace: str) -> Optional[str]:
    p = os.path.join(workspace, "package.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    script = (data.get("scripts") or {}).get("test") if isinstance(data, dict) else None
    if not isinstance(script, str) or not script.strip():
        return None
    if "no test specified" in script:
        return None
    return script.strip()


def _makefile_has_test(workspace: str) -> bool:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        p = os.path.join(workspace, name)
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if re.search(r"^test\s*:", f.read(200_000), re.M):
                        return True
        except OSError:
            continue
    return False


def _npm_script_named(workspace: str, name: str) -> Optional[str]:
    """Like `_npm_test_script` but for an arbitrary package.json script name.
    Kept as a separate function (not a shared helper the two funnel through)
    so `_npm_test_script`/`detect_test_command` stay byte-for-byte what they
    were — VER-02 (`detect_command` below) is additive, not a refactor of the
    "tests" path every existing caller already depends on."""
    p = os.path.join(workspace, "package.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    script = (data.get("scripts") or {}).get(name) if isinstance(data, dict) else None
    if not isinstance(script, str) or not script.strip():
        return None
    return script.strip()


def _makefile_target(workspace: str, name: str) -> bool:
    """Like `_makefile_has_test` but for an arbitrary target name."""
    pattern = re.compile(r"^" + re.escape(name) + r"\s*:", re.M)
    for fname in ("Makefile", "makefile", "GNUmakefile"):
        p = os.path.join(workspace, fname)
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    if pattern.search(f.read(200_000)):
                        return True
        except OSError:
            continue
    return False


#: The four kinds VER-02 asks `run_verifier` (src/verification.py) to cover.
VERIFIER_KINDS = ("tests", "lint", "build", "typecheck")


def detect_command(kind: str, workspace: str, override: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """VER-02's single "what command proves `kind`" front door.

    "tests" is exactly `detect_test_command` (unchanged — every existing
    caller of that function is untouched by this one). For "lint"/"build"/
    "typecheck" there is no equivalent of pytest's config-file sniffing to
    fall back on, so detection stays deliberately narrow: an explicit
    `override`, or an npm script / Makefile target NAMED after the kind. No
    heuristic guesses a project's lint command from file extensions or
    installed linters — a wrong guess here would silently "verify" nothing.
    """
    if kind not in VERIFIER_KINDS:
        raise ValueError(f"unknown verifier kind {kind!r}; expected one of {VERIFIER_KINDS}")
    if kind == "tests":
        return detect_test_command(workspace, override)
    if not workspace or not os.path.isdir(workspace):
        return None
    override = (override or "").strip()
    if override:
        return {"kind": "custom", "shell": override, "label": f"{kind}: {override}"}
    script = _npm_script_named(workspace, kind)
    if script:
        npm = shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm")
        npm = npm or ("npm.cmd" if os.name == "nt" else "npm")
        return {"kind": "npm", "argv": [npm, "run", kind, "--silent"], "label": f"npm run {kind} ({script[:60]})"}
    if _makefile_target(workspace, kind) and shutil.which("make"):
        return {"kind": "make", "argv": ["make", kind], "label": f"make {kind}"}
    return None


def detect_test_command(workspace: str, override: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return {"kind", "argv"|"shell", "label", "python"} or None when no runner
    is recognised. `override` is a user-provided shell command (project or
    global setting) and always wins."""
    if not workspace or not os.path.isdir(workspace):
        return None
    override = (override or "").strip()
    if override:
        return {"kind": "custom", "shell": override, "label": override}
    if _has_pytest_config(workspace) or _has_python_tests(workspace):
        py = _python_for(workspace)
        if py is None:
            py = _fallback_python(workspace)
            kind_note = "host python"
        else:
            kind_note = "project venv"
        if py is None:
            # Frozen build with no real interpreter anywhere: "could not run"
            # (inconclusive in run_tests), never "your change broke the tests".
            return {
                "kind": "pytest", "python": None, "note": "no interpreter", "argv": [],
                "label": "pytest -x -q",
                "unavailable": "no Python interpreter available to run pytest",
            }
        return {
            "kind": "pytest", "python": py, "note": kind_note,
            "argv": [py, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider", "--color=no"],
            "label": "pytest -x -q",
        }
    npm_script = _npm_test_script(workspace)
    if npm_script:
        npm = shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm")
        npm = npm or ("npm.cmd" if os.name == "nt" else "npm")
        return {"kind": "npm", "argv": [npm, "test", "--silent"], "label": f"npm test ({npm_script[:60]})"}
    if os.path.isfile(os.path.join(workspace, "Cargo.toml")) and shutil.which("cargo"):
        return {"kind": "cargo", "argv": ["cargo", "test", "-q"], "label": "cargo test"}
    if os.path.isfile(os.path.join(workspace, "go.mod")) and shutil.which("go"):
        return {"kind": "go", "argv": ["go", "test", "./..."], "label": "go test ./..."}
    if _makefile_has_test(workspace) and shutil.which("make"):
        return {"kind": "make", "argv": ["make", "test"], "label": "make test"}
    return None


# ---------------------------------------------------------------------------
# Scoping: which tests relate to the changed files
# ---------------------------------------------------------------------------

_IMPORT_SCAN_MAX_FILES = 400
_IMPORT_SCAN_MAX_BYTES = 96_000


def _imports_any(path: str, stems: List[str]) -> bool:
    """True when the test file imports / names one of the changed modules
    (`import server`, `from src.calc import add`, `importlib.import_module("server")`,
    `server.app`)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(_IMPORT_SCAN_MAX_BYTES)
    except OSError:
        return False
    low = text.lower()
    for s in stems:
        if s not in low:
            continue
        if re.search(r"(?:^|\n)\s*(?:from\s+[\w.]*\b" + re.escape(s) + r"\b[\w.]*\s+import|import\s+[\w.]*\b" + re.escape(s) + r"\b)", low):
            return True
        if re.search(r"import_module\(\s*['\"][\w.]*\b" + re.escape(s) + r"\b", low):
            return True
        if re.search(r"\b" + re.escape(s) + r"\.[a-z_]", low):
            return True
    return False


def related_test_files(workspace: str, changed: Iterable[str], limit: int = 12) -> List[str]:
    """Test files (relative, forward slashes) that relate to the changed paths:
    the changed test files themselves + `test_<stem>*.py` / `<stem>_test.py`
    for every changed source module + test files that import / name the
    changed module (a `tests/test_api.py` exercising `server.py` — seen on the
    bench: the name-only match ran test_server.py and missed the failing
    test_api.py)."""
    if not workspace:
        return []
    stems: List[str] = []
    out: List[str] = []
    for raw in changed:
        if not raw:
            continue
        rel = raw.replace("\\", "/")
        if os.path.isabs(raw):
            try:
                rel = os.path.relpath(raw, workspace).replace(os.sep, "/")
            except ValueError:
                continue
        if rel.startswith("../"):
            continue
        base = rel.rsplit("/", 1)[-1]
        if _is_any_test_file(rel):
            if os.path.isfile(os.path.join(workspace, rel)) and rel not in out:
                out.append(rel)
            continue
        stem = base.rsplit(".", 1)[0] if "." in base else base
        if stem and stem not in ("__init__", "index", "main", "app") and len(stem) >= 3:
            stems.append(stem.lower())
    if not stems:
        return out[:limit]
    roots = [d for d in ("tests", "test") if os.path.isdir(os.path.join(workspace, d))]
    if not roots:
        roots = ["."]
    by_content: List[str] = []
    scanned = 0
    for d in roots:
        try:
            for dirpath, dirnames, filenames in os.walk(os.path.join(workspace, d)):
                dirnames[:] = [x for x in dirnames if not x.startswith(".") and x not in ("__pycache__", "node_modules", "venv", ".venv")]
                for fn in filenames:
                    if not _is_any_test_file(fn):
                        continue
                    low = fn.lower()
                    rel = os.path.relpath(os.path.join(dirpath, fn), workspace).replace(os.sep, "/")
                    if any(
                        low in (f"test_{s}.py", f"{s}_test.py", f"test_{s}.mjs", f"test_{s}.js",
                                f"{s}_test.mjs", f"{s}.test.js", f"{s}.spec.js")
                        or low.startswith(f"test_{s}_")
                        or low.startswith(f"test_{s}.")
                        for s in stems
                    ):
                        if rel not in out:
                            out.append(rel)
                    elif scanned < _IMPORT_SCAN_MAX_FILES:
                        scanned += 1
                        if _imports_any(os.path.join(dirpath, fn), stems) and rel not in by_content:
                            by_content.append(rel)
                if d == ".":
                    break  # top-level only when there is no tests dir
        except OSError:
            continue
    for rel in by_content:
        if rel not in out:
            out.append(rel)
    return out[:limit]


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _clean_env() -> Dict[str, str]:
    # The project's tests must run against the PROJECT's interpreter. Faustus
    # lives in a virtualenv, so its own environment names that venv in
    # VIRTUAL_ENV, leads PATH with our bin/, and may carry a PYTHONPATH:
    # inherited, all three make the suite's `python`, `pip` and imports resolve
    # to OUR site-packages, and the failure is silent — green here, ImportError
    # on the user's machine. Nothing here depends on that inheritance: the
    # runner is always an absolute path (`_python_for` / `_fallback_python`),
    # and static_checks resolves its tools with `shutil.which` in THIS process
    # before the child environment is built, so neither can become unfindable.
    env = native_host_environment()
    env.setdefault("CI", "1")                   # jest/vitest: no watch mode
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("FORCE_COLOR", "0")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("PYTEST_ADDOPTS", None)
    return env


def run_tests(
    workspace: str,
    spec: Dict[str, Any],
    *,
    changed: Optional[Iterable[str]] = None,
    scope: str = "related",
    timeout_s: Optional[float] = None,
    test_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the detected command. Returns a verdict dict (see module docstring).
    `test_files` forces the exact pytest files to run (relative to `workspace`;
    missing ones are dropped) — used for the baseline run at the checkpoint."""
    t0 = time.time()
    try:
        timeout = float(timeout_s if timeout_s is not None else _setting("agent_project_tests_timeout_seconds", DEFAULT_TIMEOUT_S) or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TIMEOUT_S)
    timeout = max(10.0, min(timeout, 3600.0))
    result: Dict[str, Any] = {
        "ran": False, "kind": spec.get("kind"), "label": spec.get("label"), "scope": "all",
        "ok": None, "exit_code": None, "timed_out": False, "duration_s": 0.0,
        # None = nothing was declared for this run, so nothing was checked.
        # Never collapse it to True: that reads "we never looked" as "it passed".
        "output_matched": None,
        "summary": "", "failures": [], "output_tail": "", "inconclusive": False,
        "command": "", "cwd": workspace,
    }
    argv: Optional[List[str]] = None
    shell_cmd: Optional[str] = None
    if spec.get("shell"):
        shell_cmd = str(spec["shell"])
        result["command"] = shell_cmd
    else:
        argv = list(spec.get("argv") or [])
        if not argv:
            # Nothing runnable (e.g. no interpreter for pytest): that is
            # "could not verify", not a failing suite.
            result["summary"] = str(spec.get("unavailable") or "no command")
            result["inconclusive"] = True
            return result
        if spec.get("kind") == "pytest" and test_files is not None:
            rel = [f for f in test_files if os.path.isfile(os.path.join(workspace, *f.split("/")))]
            if not rel:
                result["summary"] = "none of the test files exist at this state"
                result["inconclusive"] = True
                return result
            # The baseline must see EVERY failure of these files, not stop at
            # the first one (-x) like the post-turn run does.
            argv = [a for a in argv if a != "-x"] + ["--"] + rel
            result["scope"] = "related"
            result["related_files"] = rel
        elif spec.get("kind") == "pytest" and scope == "related" and changed is not None:
            rel = related_test_files(workspace, changed)
            py_rel = [f for f in rel if f.endswith(".py")]
            if py_rel:
                argv = argv + ["--"] + py_rel
                result["scope"] = "related"
                result["related_files"] = rel
            elif rel or list(changed):
                # JS/node tests only: do not fall back to the whole pytest
                # suite (Silhouettes d20e933f verified 82 unrelated python
                # tests and never ran test_gestures.mjs).
                #
                # 20-09-2026 — same rule when NOTHING maps: a turn that wrote
                # data files in a folder that merely happens to contain a
                # `tests/` directory (a Blender add-on's, in the case that
                # found this) ran that whole stranger suite, for minutes,
                # having changed nothing it covers. "Related" has to mean
                # related: with changes but no related test, there is
                # nothing to verify here. `scope="all"` still runs
                # everything, and a turn with no changed files at all is
                # unaffected.
                result["scope"] = "related"
                result["related_files"] = rel
                result["ran"] = False
                result["ok"] = True
                result["summary"] = (
                    "no related python tests" if rel else "no test covers the files this turn changed"
                )
                result["duration_s"] = round(time.time() - t0, 1)
                return result
        result["command"] = " ".join(shlex.quote(a) if " " in a else a for a in argv)
    kwargs: Dict[str, Any] = dict(
        cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=_clean_env(), stdin=subprocess.DEVNULL,
    )
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    # EXEC-04: consult the shared `cpu_heavy` budget (src/bg_jobs.py) before
    # spawning — the same pool a `#!bg` build or a local research run draws
    # from, so a test suite does not start alongside several others and a
    # model generation all fighting for the same cores. Best-effort: bg_jobs
    # being unimportable, or the admission wait itself timing out, must never
    # be why a test run silently never happens (this module "never raises"),
    # so a wait that times out is reported as inconclusive rather than as a
    # failing suite, and an unavailable admission module just runs the tests
    # unthrottled, exactly as before EXEC-04 existed.
    _release_cpu_heavy = lambda _ticket: None  # noqa: E731 - overwritten below on success
    cpu_heavy_ticket = None
    try:
        from src.bg_jobs import acquire_cpu_heavy_sync, release_cpu_heavy as _release_cpu_heavy
        wait_budget = max(60.0, min(timeout, 1800.0))
        cpu_heavy_ticket = acquire_cpu_heavy_sync("test_suite", owner=workspace, timeout=wait_budget)
    except TimeoutError as e:
        result.update(ran=False, summary=str(e)[:300], inconclusive=True)
        result["duration_s"] = round(time.time() - t0, 1)
        return result
    except Exception as e:  # noqa: BLE001 - admission is best-effort, never blocking
        logger.debug("project_tests: cpu_heavy admission unavailable, running unthrottled: %s", e)

    try:
        try:
            if shell_cmd is not None:
                proc = subprocess.Popen(shell_cmd, shell=True, **kwargs)
            else:
                proc = subprocess.Popen(argv, **kwargs)
        except (OSError, subprocess.SubprocessError) as e:
            result.update(ran=False, summary=f"could not run: {e}"[:300], inconclusive=True)
            result["duration_s"] = round(time.time() - t0, 1)
            return result
        exit_code: Optional[int] = None
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
            result.update(ran=True, exit_code=exit_code)
        except subprocess.TimeoutExpired:
            # Kill the whole tree: with shell=True (custom commands) the direct
            # child is a shell, and on Windows killing it leaves the real test
            # process running — and holding the pipes — until it finishes on its own.
            _kill_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                stdout, stderr = "", ""
            result.update(ran=True, timed_out=True, exit_code=None, ok=False, inconclusive=True,
                          summary=f"timed out after {int(timeout)} s")
    finally:
        _release_cpu_heavy(cpu_heavy_ticket)
    out = (stdout or "") + (("\n" + stderr) if stderr else "")
    result["duration_s"] = round(time.time() - t0, 1)
    out = out[-OUTPUT_CAP:]
    result["output_tail"] = out[-TAIL_CHARS:].strip()
    if not result["timed_out"]:
        parsed = parse_output(spec.get("kind") or "", exit_code, out)
        result.update(parsed)
        # Exit 0 is not evidence the suite ran: a collection that found nothing
        # and a custom command that succeeded at doing nothing both report it.
        # `expected_output_contains` was declared with the plan, before this
        # run, so its absence is evidence and forces exit 65.
        result["exit_code"], result["output_matched"] = output_oracle.apply(
            result.get("exit_code") or 0, out, spec.get("expected_output_contains"))
        if result["output_matched"] is False:
            # `ok` came from parse_output, which only saw the runner's own
            # exit 0. Left alone it would contradict the code just forced, and
            # a verification layer the verdict disagrees with is worthless.
            result["ok"] = False
    return result


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Best-effort kill of `proc` and everything it spawned."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.debug("[project-tests] kill tree failed: %s", e)
    try:
        proc.kill()
    except (OSError, ProcessLookupError):
        pass


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

_PYTEST_SUMMARY_RE = re.compile(r"^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s\s*(?:\(.*\))?\s*=*\s*$", re.M)
_PYTEST_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)(?: - (.*))?$", re.M)
_PYTEST_IMPORT_RE = re.compile(r"(ModuleNotFoundError|ImportError|No module named)", re.M)
# `python -m pytest` without pytest exits 1 with a message that never contains
# the word "error" — it used to be scored as "your changes broke the tests".
_PYTEST_MISSING_RE = re.compile(r"No module named ['\"]?pytest\b", re.M)
_JEST_RE = re.compile(r"^Tests:\s+(.*)$", re.M)
_MOCHA_RE = re.compile(r"^\s*(\d+) (passing|failing|pending)\b", re.M)
_VITEST_RE = re.compile(r"^\s*Tests\s+(.*)$", re.M)
_CARGO_RE = re.compile(r"^test result: (\w+)\. (\d+) passed; (\d+) failed", re.M)
_GO_FAIL_RE = re.compile(r"^(?:--- FAIL: (\S+)|FAIL\s+(\S+))", re.M)


def parse_output(kind: str, exit_code: Optional[int], out: str) -> Dict[str, Any]:
    ok = exit_code == 0
    summary = ""
    failures: List[str] = []
    inconclusive = False
    if kind == "pytest":
        m = None
        for m in _PYTEST_SUMMARY_RE.finditer(out):
            pass
        if m:
            summary = m.group(1).strip()
        for fm in _PYTEST_FAILED_RE.finditer(out):
            item = fm.group(1) + (f" — {fm.group(2).strip()}" if fm.group(2) else "")
            if item not in failures:
                failures.append(item)
        if exit_code == 5:                     # no tests collected
            ok, inconclusive, summary = True, True, summary or "no tests collected"
        elif exit_code not in (0, 1) and exit_code is not None:
            # 2 = interrupted/usage, 3 = internal error, 4 = usage error
            inconclusive = True
            summary = summary or f"pytest exited with {exit_code}"
        # Collection-time missing modules (jsonschema, a pytest plugin, …)
        # are the runner's environment, not the change. pytest reports them
        # as `ERROR tests/foo.py` and often exits 1 — the same code as a
        # real assertion failure — so the node kind has to be read from the
        # output, not from the exit status. A `FAILED` test that happens to
        # mention ModuleNotFoundError in a traceback is still a real fail.
        has_error_node = bool(re.search(r"^ERROR \S+", out, re.M))
        has_failed_node = bool(re.search(r"^FAILED \S+", out, re.M))
        if not ok and _PYTEST_IMPORT_RE.search(out) and has_error_node and not has_failed_node:
            inconclusive = True
            summary = (summary + " — " if summary else "") + "collection errors (missing modules): environment, not the change"
        elif not ok and not failures and _PYTEST_IMPORT_RE.search(out) and "error" in out.lower():
            inconclusive = True
            summary = summary or "import error during collection (environment?)"
        if _PYTEST_MISSING_RE.search(out):
            # Any exit code: the runner itself never started, so nothing was
            # verified — never charge the agent a fix round for it.
            inconclusive = True
            summary = "pytest is not installed in the project's interpreter"
            failures = []
    elif kind == "node":
        m = re.search(
            r"# tests\s+(\d+).*?# pass\s+(\d+).*?# fail\s+(\d+)",
            out, re.S,
        )
        if m:
            n_tests, n_pass, n_fail = m.group(1), m.group(2), m.group(3)
            summary = f"{n_pass} passed"
            if n_fail != "0":
                summary += f", {n_fail} failed"
            elif n_tests != n_pass:
                summary += f" of {n_tests}"
        else:
            summary = "passed" if ok else "failed"
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("not ok ") or "AssertionError" in s:
                if s not in failures:
                    failures.append(s[:200])
            if len(failures) >= 20:
                break
    elif kind == "npm":
        m = _JEST_RE.search(out) or _VITEST_RE.search(out)
        if m:
            summary = m.group(1).strip()
        else:
            counts = {k: int(n) for n, k in _MOCHA_RE.findall(out)}
            if counts:
                summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        for line in out.splitlines():
            s = line.strip()
            if s.startswith(("✕", "×", "✗", "FAIL ")) or re.match(r"^\d+\) ", s):
                if s not in failures:
                    failures.append(s[:200])
            if len(failures) >= 20:
                break
    elif kind == "cargo":
        m = None
        for m in _CARGO_RE.finditer(out):
            pass
        if m:
            summary = f"{m.group(2)} passed, {m.group(3)} failed"
        for line in out.splitlines():
            if line.startswith("test ") and line.rstrip().endswith("FAILED"):
                failures.append(line.strip()[:200])
    elif kind == "go":
        for gm in _GO_FAIL_RE.finditer(out):
            failures.append((gm.group(1) or gm.group(2) or "").strip()[:200])
        summary = "ok" if ok else f"{len(failures)} failing" if failures else "FAIL"
    if not summary:
        summary = "passed" if ok else f"exit code {exit_code}"
    return {"ok": ok, "summary": summary[:300], "failures": failures[:20], "inconclusive": inconclusive}


# ---------------------------------------------------------------------------
# Glue for the agent loop
# ---------------------------------------------------------------------------

def _failure_id(item: str) -> str:
    return (item or "").split(" — ", 1)[0].strip()


def _name_related_test(rel: str, changed: Iterable[str]) -> bool:
    """True when the test file is tied to a changed file by NAME (test_<stem>*.py,
    <stem>_test.py) or is itself one of the changed files — i.e. the test the
    user most plausibly asked about. Pre-existing failures there still get the
    fix round: "fix add()" fails before and after a wrong fix, and that is not
    'somebody else's broken test'."""
    low = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for raw in changed:
        if not raw:
            continue
        crel = raw.replace("\\", "/")
        if crel.lower().endswith("/" + rel.lower()) or crel.lower() == rel.lower():
            return True
        base = crel.rsplit("/", 1)[-1]
        stem = (base.rsplit(".", 1)[0] if "." in base else base).lower()
        if stem and (low in (f"test_{stem}.py", f"{stem}_test.py") or low.startswith(f"test_{stem}_") or low.startswith(f"test_{stem}.")):
            return True
    return False


def _test_files_from_failures(failures: Iterable[str]) -> List[str]:
    """`tests/test_a.py::test_x — AssertionError` → `tests/test_a.py`."""
    out: List[str] = []
    for item in failures:
        node = (item or "").split(" — ", 1)[0].strip()
        path = node.split("::", 1)[0].strip().replace("\\", "/")
        if path and path not in out:
            out.append(path)
    return out


def compare_with_baseline(workspace: str, checkpoint_sha: Optional[str], spec: Dict[str, Any],
                          res: Dict[str, Any], changed: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Tests failed after the turn: run the SAME test files against the
    checkpoint tree (exported to a temp dir) and split the failures into
    `new_failures` (caused by this change) and `pre_existing` (failed before
    too). A pre-existing failure in a test file that is not tied by name to
    the changed files is *exempt*: when every failure is exempt the run is
    flagged `pre_existing_only` and costs no fix round. Only for pytest with a
    known test-file list; never raises.

    A full-suite run has no `related_files`. Without a fallback the star IoU
    (or any other pre-existing fail) was re-charged as a new failure on every
    turn — seen live, surviving three task gates. The files that actually
    failed are enough to re-run against the checkpoint."""
    res.setdefault("new_failures", list(res.get("failures") or []))
    res.setdefault("pre_existing", [])
    changed = list(changed or [])
    files = list(res.get("related_files") or []) or _test_files_from_failures(res.get("failures") or [])
    if not checkpoint_sha or spec.get("kind") != "pytest" or not files:
        return res
    if not bool(_setting("agent_project_tests_baseline", True)):
        return res
    import tempfile
    try:
        from src import workspace_checkpoints as wc
    except Exception:
        return res
    tmp = tempfile.mkdtemp(prefix="odysseus-baseline-")
    try:
        if not wc.export_tree(workspace, checkpoint_sha, tmp):
            res["baseline"] = {"ran": False, "summary": "checkpoint export failed"}
            return res
        base_spec = dict(spec)
        base = run_tests(tmp, base_spec, test_files=files)
        res["baseline"] = compact(base)
        if not base.get("ran") or base.get("inconclusive"):
            return res
        before = {_failure_id(f) for f in (base.get("failures") or [])}
        cur = list(res.get("failures") or [])
        cur_ids = {_failure_id(f) for f in cur}
        res["pre_existing"] = [f for f in cur if _failure_id(f) in before]
        res["new_failures"] = [f for f in cur if _failure_id(f) not in before]
        # Additive (VER-02): the baseline failures that do NOT reappear now —
        # what this turn actually fixed, not just what it left broken.
        res["fixed"] = [f for f in (base.get("failures") or []) if _failure_id(f) not in cur_ids]
        exempt = [f for f in res["pre_existing"]
                  if not _name_related_test(_failure_id(f).split("::", 1)[0], changed)]
        res["exempt"] = exempt
        if cur and not res["new_failures"] and len(exempt) == len(cur):
            res["pre_existing_only"] = True
            res["summary"] = (res.get("summary") or "failed") + " — all failing before this change too (pre-existing)"
    except Exception as e:  # noqa: BLE001
        logger.debug("[project-tests] baseline comparison failed: %s", e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def _node_executable() -> Optional[str]:
    return shutil.which("node") or shutil.which("node.exe")


def run_node_tests(
    workspace: str,
    files: Iterable[str],
    *,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Run `node --test` on the given related files. Never raises."""
    rel: List[str] = []
    for raw in files:
        if not raw:
            continue
        path = raw.replace("\\", "/")
        if os.path.isfile(os.path.join(workspace, *path.split("/"))) and path not in rel:
            rel.append(path)
    node = _node_executable()
    if not rel:
        return {
            "ran": False, "kind": "node", "label": "node --test", "scope": "related",
            "ok": None, "inconclusive": True, "summary": "no node test files",
            "failures": [], "related_files": [], "command": "", "duration_s": 0.0,
        }
    if not node:
        return {
            "ran": False, "kind": "node", "label": "node --test", "scope": "related",
            "ok": None, "inconclusive": True, "summary": "node is not available",
            "failures": [], "related_files": rel, "command": "", "duration_s": 0.0,
        }
    spec = {
        "kind": "node",
        "argv": [node, "--test", *rel],
        "label": "node --test",
    }
    res = run_tests(workspace, spec, timeout_s=timeout_s)
    res["scope"] = "related"
    res["related_files"] = rel
    return res


def _merge_node_tests(primary: Optional[Dict[str, Any]], node: Dict[str, Any]) -> Dict[str, Any]:
    """Pytest result plus `node --test`. Overall ok is AND of both runs."""
    related = list(dict.fromkeys(
        list((primary or {}).get("related_files") or [])
        + list(node.get("related_files") or [])
    ))
    if not primary or not primary.get("ran"):
        out = dict(node)
        out["related_files"] = related
        if primary and primary.get("summary"):
            out["pytest_skipped"] = primary.get("summary")
        return out
    out = dict(primary)
    out["node_tests"] = {
        k: node.get(k) for k in (
            "ran", "kind", "ok", "summary", "exit_code", "command",
            "failures", "inconclusive", "duration_s", "related_files",
        ) if k in node
    }
    out["related_files"] = related
    node_ran = bool(node.get("ran"))
    node_ok = bool(node.get("ok")) and not node.get("inconclusive")
    node_sum = node.get("summary") or ("passed" if node_ok else "failed")
    if node_ran:
        out["summary"] = (out.get("summary") or "") + f"; node --test: {node_sum}"
        if node.get("command"):
            prev = out.get("command") or ""
            out["command"] = (prev + " && " if prev else "") + node["command"]
        if not node_ok:
            out["ok"] = False
            out["failures"] = list(out.get("failures") or []) + list(node.get("failures") or [])
        if out.get("kind") == "pytest":
            out["kind"] = "pytest+node"
    elif node.get("inconclusive"):
        note = node.get("summary") or "node tests skipped"
        out["summary"] = (out.get("summary") or "") + f"; {note}"
    return out


def run_for_turn(workspace: str, changed: Iterable[str], *, override: Optional[str] = None,
                 checkpoint_sha: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Detect + run. None when the feature is off or no runner exists. With a
    checkpoint, failures are compared against the pre-turn state."""
    if not workspace:
        return None
    if not bool(_setting("agent_project_tests", True)):
        return None
    override = override or str(_setting("agent_project_test_command", "") or "").strip() or None
    spec = detect_test_command(workspace, override)
    changed_list = list(changed)
    scope = str(_setting("agent_project_tests_scope", "related") or "related")
    res: Optional[Dict[str, Any]] = None
    if spec:
        res = run_tests(workspace, spec, changed=changed_list, scope=scope)
        logger.info("[harness] project tests (%s, %s): ok=%s %s in %ss", spec.get("kind"), res.get("scope"),
                    res.get("ok"), res.get("summary"), res.get("duration_s"))
        if res.get("ran") and res.get("ok") is False and not res.get("inconclusive"):
            res = compare_with_baseline(workspace, checkpoint_sha, spec, res, changed=changed_list)
            if res.get("pre_existing_only"):
                logger.info("[harness] project tests: every failure is pre-existing (failed at the checkpoint too)")
    node_files: List[str] = []
    if scope == "related":
        node_files = [f for f in related_test_files(workspace, changed_list) if _is_node_test_file(f)]
    if node_files:
        node_res = run_node_tests(workspace, node_files)
        logger.info("[harness] node tests: ok=%s %s in %ss",
                    node_res.get("ok"), node_res.get("summary"), node_res.get("duration_s"))
        res = _merge_node_tests(res, node_res)
    return res


def failure_message(res: Dict[str, Any]) -> str:
    """The bounded fix-round instruction for the model."""
    lines = [
        "[Harness check — automatic message from the runtime, not from the user]",
        f"The project's tests FAILED after your changes ({res.get('label') or res.get('command')}"
        + (f", scope: {', '.join(res.get('related_files') or [])}" if res.get("related_files") else "") + "):",
        f"Result: {res.get('summary') or 'failed'}",
    ]
    pre = list(res.get("pre_existing") or [])
    for f in (res.get("failures") or [])[:8]:
        tag = " (this one already failed before your change)" if f in pre else ""
        lines.append(f"- {f}{tag}")
    if pre:
        lines.append("A test that already failed before your change may be what the user asked you to fix, or "
                     "someone else's broken test: decide from the request. If it is unrelated, leave it and say so.")
    tail = (res.get("output_tail") or "").strip()
    if tail:
        lines.append("Output (tail):")
        lines.append(tail[-2500:])
    lines.append(
        "Fix the CAUSE with edit_file (read the failing test and the code it exercises first). "
        "Do NOT delete, skip or weaken tests to make them pass, and do not re-run the whole suite "
        "yourself — the runtime re-runs it when you finish. If the failure is unrelated to your "
        "change (pre-existing), name the test in ONE sentence and continue the remaining plan. "
        "Do not ask the user whether to investigate it versus accepting it as baseline — that "
        "is not a blocker."
    )
    return "\n".join(lines)


def compact(res: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What gets persisted with the message / emitted to the UI."""
    if not res:
        return None
    keys = ("ran", "kind", "label", "scope", "ok", "exit_code", "output_matched", "timed_out", "duration_s",
            "summary", "failures", "inconclusive", "command", "related_files",
            "new_failures", "pre_existing", "pre_existing_only", "exempt", "baseline", "fixed",
            "node_tests", "ui_verify")
    out = {k: res.get(k) for k in keys if k in res}
    out["output_tail"] = (res.get("output_tail") or "")[-1500:]
    return out
