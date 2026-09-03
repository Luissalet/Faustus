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
themselves); when nothing matches it falls back to the whole suite, still
bounded by the timeout. Other runners always run their whole suite.

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


def _fallback_python() -> Optional[str]:
    """A real interpreter for `-m pytest` when the project has no venv, or None.

    Never `sys.executable` in the frozen build: there it is the app's own
    Faustus.exe, which ignores `-m` and boots a second copy of the application
    (splash + tray + another server) instead of running pytest — see
    agent_harness.host_python()."""
    try:
        from src.agent_harness import host_python
        py = host_python()
    except Exception:                                   # pragma: no cover
        py = None if getattr(sys, "frozen", False) else sys.executable
    if py:
        return py
    names = ("python", "python3") if os.name == "nt" else ("python3", "python")
    for name in names:
        found = shutil.which(name)
        if not found:
            continue
        try:
            if os.path.realpath(found) == os.path.realpath(sys.executable):
                continue                                # that is the frozen app again
        except (OSError, ValueError):                   # pragma: no cover
            pass
        return found
    return None


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
            py = _fallback_python()
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
        if _TEST_FILE_RE.search(rel):
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
                    if not _TEST_FILE_RE.search(fn):
                        continue
                    low = fn.lower()
                    rel = os.path.relpath(os.path.join(dirpath, fn), workspace).replace(os.sep, "/")
                    if any(low in (f"test_{s}.py", f"{s}_test.py") or low.startswith(f"test_{s}_") or low.startswith(f"test_{s}.") for s in stems):
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
            if rel:
                argv = argv + ["--"] + rel
                result["scope"] = "related"
                result["related_files"] = rel
        result["command"] = " ".join(shlex.quote(a) if " " in a else a for a in argv)
    kwargs: Dict[str, Any] = dict(
        cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", env=_clean_env(), stdin=subprocess.DEVNULL,
    )
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
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
        if not ok and failures and all("error" in f.lower() and _PYTEST_IMPORT_RE.search(f) for f in failures):
            inconclusive = True
            summary = (summary + " — " if summary else "") + "collection errors (missing modules): environment, not the change"
        if not ok and not failures and _PYTEST_IMPORT_RE.search(out) and "error" in out.lower():
            inconclusive = True
            summary = summary or "import error during collection (environment?)"
        if _PYTEST_MISSING_RE.search(out):
            # Any exit code: the runner itself never started, so nothing was
            # verified — never charge the agent a fix round for it.
            inconclusive = True
            summary = "pytest is not installed in the project's interpreter"
            failures = []
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


def compare_with_baseline(workspace: str, checkpoint_sha: Optional[str], spec: Dict[str, Any],
                          res: Dict[str, Any], changed: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Tests failed after the turn: run the SAME test files against the
    checkpoint tree (exported to a temp dir) and split the failures into
    `new_failures` (caused by this change) and `pre_existing` (failed before
    too). A pre-existing failure in a test file that is not tied by name to
    the changed files is *exempt*: when every failure is exempt the run is
    flagged `pre_existing_only` and costs no fix round. Only for pytest with a
    known test-file list; never raises."""
    res.setdefault("new_failures", list(res.get("failures") or []))
    res.setdefault("pre_existing", [])
    changed = list(changed or [])
    if not checkpoint_sha or spec.get("kind") != "pytest" or not res.get("related_files"):
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
        base = run_tests(tmp, base_spec, test_files=list(res.get("related_files") or []))
        res["baseline"] = compact(base)
        if not base.get("ran") or base.get("inconclusive"):
            return res
        before = {_failure_id(f) for f in (base.get("failures") or [])}
        cur = list(res.get("failures") or [])
        res["pre_existing"] = [f for f in cur if _failure_id(f) in before]
        res["new_failures"] = [f for f in cur if _failure_id(f) not in before]
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
    if not spec:
        return None
    scope = str(_setting("agent_project_tests_scope", "related") or "related")
    res = run_tests(workspace, spec, changed=list(changed), scope=scope)
    logger.info("[harness] project tests (%s, %s): ok=%s %s in %ss", spec.get("kind"), res.get("scope"),
                res.get("ok"), res.get("summary"), res.get("duration_s"))
    if res.get("ran") and res.get("ok") is False and not res.get("inconclusive"):
        res = compare_with_baseline(workspace, checkpoint_sha, spec, res, changed=list(changed))
        if res.get("pre_existing_only"):
            logger.info("[harness] project tests: every failure is pre-existing (failed at the checkpoint too)")
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
        "change (pre-existing), say so explicitly in your final answer, naming the test."
    )
    return "\n".join(lines)


def compact(res: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What gets persisted with the message / emitted to the UI."""
    if not res:
        return None
    keys = ("ran", "kind", "label", "scope", "ok", "exit_code", "output_matched", "timed_out", "duration_s",
            "summary", "failures", "inconclusive", "command", "related_files",
            "new_failures", "pre_existing", "pre_existing_only", "exempt", "baseline")
    out = {k: res.get(k) for k in keys if k in res}
    out["output_tail"] = (res.get("output_tail") or "")[-1500:]
    return out
