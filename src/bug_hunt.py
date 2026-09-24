"""bug_hunt.py — an autonomous bug hunter for a module/function/directory.

Given a target in the workspace (a file, a `path::symbol`, or a directory),
this module:

  1. understands the code (`plan_targets`, `ast`-based for Python; file-level
     for everything else, with a few callers pulled from `src.code_graph`
     when the resolved call graph knows the symbol, else a plain grep);
  2. asks a small utility model to generate edge-case pytest tests for it
     (`generate_tests`) — normal, boundary, invalid-input and
     idempotency/ordering cases, each carrying its own expected behaviour and
     "assumption" tests flagged as such. A deterministic template suite is
     produced instead when no model is reachable or the model's tests fail
     validation, so the tool always returns something;
  3. runs that suite isolated under `<workspace>/.faustus/bughunt/`
     (`run_suite`);
  4. triages every failing test — real bug in the code, or a wrong
     expectation in the generated test (`triage`) — with a model when one is
     reachable and a deterministic heuristic otherwise;
  5. assembles a structured `Report` (`hunt`), optionally keeping the tests
     that expose a real bug as regression tests under `tests/`.

Stdlib + `ast` only for analysis; the LLM call is optional and best-effort —
every public function still returns something useful with no model
reachable and no network. Nothing here ever touches the network directly:
outbound calls only go through `src.llm_core.llm_call_async` against a
resolved local/remote endpoint, exactly like `src.instincts.extract_from_turn`.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

SCRATCH_DIRNAME = os.path.join(".faustus", "bughunt")
MAX_SOURCE_CHARS = 4000
MAX_TARGETS_PER_HUNT = 6
REPORT_KEEP = 50

_DANGEROUS_IMPORTS = {
    "requests", "httpx", "aiohttp", "urllib.request", "urllib3",
    "socket", "ftplib", "smtplib", "telnetlib", "paramiko",
}
_DANGEROUS_CALLS = {
    ("os", "remove"), ("os", "unlink"), ("os", "rmdir"), ("os", "removedirs"),
    ("shutil", "rmtree"), ("shutil", "move"),
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"),
    ("subprocess", "check_call"), ("subprocess", "check_output"),
}


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return DATA_DIR
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data")


def _norm_root(workspace: str) -> str:
    return os.path.realpath(os.path.expanduser(workspace or ""))


def _rel(root: str, path: str) -> Optional[str]:
    try:
        candidate = path if os.path.isabs(path) else os.path.join(root, path)
        real = os.path.realpath(candidate)
    except (OSError, ValueError):
        return None
    root_cmp, real_cmp = (root, real) if os.name != "nt" else (root.lower(), real.lower())
    if real_cmp != root_cmp and not real_cmp.startswith(root_cmp.rstrip(os.sep) + os.sep):
        return None
    return os.path.relpath(real, root).replace(os.sep, "/")


def _slug(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", text or "").strip("_").lower()
    return (s or "target")[:limit]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Target:
    path: str                      # relative to workspace, forward slashes
    symbol: str                    # function/class name, "" for whole file
    kind: str                      # "function" | "method" | "class" | "file"
    source: str                    # snippet, <= MAX_SOURCE_CHARS
    signature: str = ""
    docstring: str = ""
    callers: List[str] = field(default_factory=list)

    @property
    def qualname(self) -> str:
        return f"{self.path}::{self.symbol}" if self.symbol else self.path

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GeneratedSuite:
    code: str
    test_names: List[str]
    source: str                    # "model" | "fallback"
    model_used: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RunResult:
    ran: bool
    ok: Optional[bool]
    inconclusive: bool
    exit_code: Optional[int]
    tests: List[Dict[str, Any]]
    summary: str
    file_path: str
    duration_s: float
    output_tail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    test_name: str
    verdict: str                   # "bug" | "test_wrong" | "unclear"
    root_cause: str
    fix_suggestion: str
    severity: str                  # "low" | "medium" | "high"
    traceback: str = ""
    target: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    workspace: str
    target: str
    owner: str
    suite_source: str
    targets_scanned: List[Dict[str, Any]]
    findings: List[Finding]
    tests_run: int
    tests_failed: int
    kept_tests_path: Optional[str]
    generated_at: float
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d

    def to_markdown(self) -> str:
        lines = [
            f"# Bug hunt: {self.target}",
            "",
            f"Workspace: `{self.workspace}`",
            f"Targets scanned: {len(self.targets_scanned)}  ·  "
            f"Tests run: {self.tests_run}  ·  Failing: {self.tests_failed}  ·  "
            f"Test source: {self.suite_source}",
            "",
        ]
        bugs = [f for f in self.findings if f.verdict == "bug"]
        wrong = [f for f in self.findings if f.verdict == "test_wrong"]
        unclear = [f for f in self.findings if f.verdict == "unclear"]
        if not self.findings:
            lines.append("No failing tests — nothing to triage.")
        for label, group in (("Real bugs", bugs), ("Wrong test expectations", wrong), ("Unclear", unclear)):
            if not group:
                continue
            lines.append(f"## {label} ({len(group)})")
            for f in group:
                lines.append(f"- **{f.test_name}** [{f.severity}] — {f.root_cause}")
                if f.fix_suggestion:
                    lines.append(f"  - fix: {f.fix_suggestion}")
            lines.append("")
        if self.kept_tests_path:
            lines.append(f"Regression tests kept at `{self.kept_tests_path}`.")
        if self.notes:
            lines.append("")
            lines.append("Notes: " + "; ".join(self.notes))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. plan_targets — understand the code
# ---------------------------------------------------------------------------

_PY_EXT = ".py"
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__",
              ".pytest_cache", ".mypy_cache", "dist", "build", ".faustus"}


def _clip(text: str, limit: int = MAX_SOURCE_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n# ... truncated"


def _node_source(lines: List[str], node: ast.AST) -> str:
    start = getattr(node, "lineno", 1) - 1
    end = getattr(node, "end_lineno", start + 1)
    return _clip("\n".join(lines[start:end]))


def _signature(node: ast.FunctionDef) -> str:
    try:
        args_txt = ast.unparse(node.args)
    except Exception:  # noqa: BLE001
        args_txt = ""
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = " -> " + ast.unparse(node.returns)
        except Exception:  # noqa: BLE001
            ret = ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({args_txt}){ret}:"


def _grep_callers(workspace: str, symbol: str, exclude_path: str, limit: int = 5) -> List[str]:
    out: List[str] = []
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\s*\(")
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(_PY_EXT):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, workspace).replace(os.sep, "/")
            if rel == exclude_path:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            out.append(f"{rel}:{i}: {line.strip()[:120]}")
                            if len(out) >= limit:
                                return out
            except OSError:
                continue
    return out


def _callers_for(workspace: str, path: str, symbol: str, limit: int = 5) -> List[str]:
    if not symbol:
        return []
    try:
        from src import code_graph
        result = code_graph.callers(symbol, workspace=workspace, limit=limit)
        hits = result.get("hits") or []
        out = []
        for h in hits:
            if h.get("resolved"):
                out.append(f"{h.get('path')}:{h.get('start_line')} {h.get('qualname')}")
        if out:
            return out[:limit]
    except Exception as e:  # noqa: BLE001
        logger.debug("[bug_hunt] code_graph.callers unavailable: %s", e)
    return _grep_callers(workspace, symbol, path, limit)


def _targets_from_python_file(workspace: str, root: str, rel: str, only_symbol: str = "") -> List[Target]:
    abs_path = os.path.join(root, *rel.split("/"))
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError:
        return [Target(path=rel, symbol="", kind="file", source=_clip(text),
                        signature="", docstring="")]
    out: List[Target] = []

    def add(node, symbol_name: str, kind: str):
        sig = _signature(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else f"class {node.name}:"
        doc = ast.get_docstring(node) or ""
        out.append(Target(
            path=rel, symbol=symbol_name, kind=kind,
            source=_node_source(lines, node), signature=sig, docstring=doc[:500],
            callers=_callers_for(workspace, rel, symbol_name),
        ))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and not only_symbol:
                continue
            if only_symbol and node.name != only_symbol:
                continue
            add(node, node.name, "function")
        elif isinstance(node, ast.ClassDef):
            if only_symbol and node.name != only_symbol and not only_symbol.startswith(f"{node.name}."):
                continue
            if not only_symbol:
                add(node, node.name, "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{node.name}.{sub.name}"
                    if sub.name.startswith("_") and sub.name not in ("__init__",):
                        if only_symbol != qual:
                            continue
                    if only_symbol and only_symbol not in (qual, sub.name):
                        continue
                    add(sub, qual, "method")
    if only_symbol:
        out = [t for t in out if t.symbol == only_symbol]
    return out[:30]


def plan_targets(workspace: str, target: str) -> List[Target]:
    """Enumerate the functions/classes (Python, via `ast`) or file-level
    targets (everything else) that `target` refers to."""
    root = _norm_root(workspace)
    if not root or not os.path.isdir(root):
        return []
    raw = (target or "").strip()
    if not raw:
        return []
    symbol = ""
    if "::" in raw:
        raw, symbol = raw.split("::", 1)
        symbol = symbol.strip()
    raw = raw.strip().replace("\\", "/").lstrip("/")
    abs_target = os.path.join(root, *raw.split("/")) if raw else root

    if os.path.isdir(abs_target):
        out: List[Target] = []
        for dirpath, dirnames, filenames in os.walk(abs_target):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = _rel(root, full)
                if not rel:
                    continue
                if fn.endswith(_PY_EXT):
                    out.extend(_targets_from_python_file(workspace, root, rel))
                elif fn.endswith((".js", ".ts", ".jsx", ".tsx")):
                    out.append(_file_level_target(root, rel))
                if len(out) >= 60:
                    return out[:60]
        return out[:60]

    if os.path.isfile(abs_target):
        rel = _rel(root, abs_target)
        if not rel:
            return []
        if abs_target.endswith(_PY_EXT):
            found = _targets_from_python_file(workspace, root, rel, only_symbol=symbol)
            if found:
                return found
            return [_file_level_target(root, rel)]
        return [_file_level_target(root, rel)]

    return []


def _file_level_target(root: str, rel: str) -> Target:
    abs_path = os.path.join(root, *rel.split("/"))
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        text = ""
    return Target(path=rel, symbol="", kind="file", source=_clip(text))


# ---------------------------------------------------------------------------
# 2. generate_tests — model-driven, with a deterministic fallback
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.S)
_TEST_DEF_RE = re.compile(r"^def (test_\w+)", re.M)


def _extract_fenced_code(text: str) -> str:
    m = _FENCE_RE.search(text or "")
    if m:
        return m.group(1).strip()
    return (text or "").strip()


def _scan_dangerous(code: str) -> Optional[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"not valid Python: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if alias.name in _DANGEROUS_IMPORTS or top in _DANGEROUS_IMPORTS:
                    return f"imports disallowed module {alias.name!r}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            if mod in _DANGEROUS_IMPORTS or top in _DANGEROUS_IMPORTS:
                return f"imports disallowed module {mod!r}"
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in _DANGEROUS_CALLS:
                    return f"calls disallowed {pair[0]}.{pair[1]}(...)"
    return None


def _build_generation_prompt(target: Target, max_cases: int) -> str:
    callers_txt = "\n".join(f"  - {c}" for c in target.callers) or "  (none found)"
    return f"""You are generating a pytest test file to hunt for real bugs in one piece of code.

Target: {target.qualname} (kind: {target.kind})
Signature: {target.signature or '(file-level target, no single signature)'}
Docstring: {target.docstring or '(none)'}
Known callers:
{callers_txt}

Source:
```python
{target.source}
```

Write ONE pytest file (a single fenced ```python block, nothing else) with up to
{max_cases} test functions covering: normal cases, boundary cases (empty input,
None, zero, negative numbers, very large input, unicode), invalid-input
handling, and idempotency/ordering when relevant to this code.

Rules:
- Import the target with `import ast, importlib.util, os, sys` and
  `importlib.util.spec_from_file_location` against the absolute path
  {os.path.join('<WORKSPACE>', *target.path.split('/'))!r} — do not guess a
  package import path.
- Every test function name starts with `test_`.
- Before each test function, add a one-line comment stating the EXACT
  expected behaviour that test checks.
- Any test whose expectation is a guess rather than something stated by the
  docstring or the obvious contract of the code MUST have `assumption` in its
  function name (e.g. `test_negative_input_assumption`).
- Never import or use the network (`requests`, `httpx`, sockets, ...), never
  delete or move files (`os.remove`, `shutil.rmtree`, ...), never spawn a
  subprocess.
- No fixtures beyond plain pytest, no external test data files.
"""


def _fallback_signature_params(target: Target) -> List[str]:
    try:
        tree = ast.parse(f"{target.signature}\n    pass" if target.signature.rstrip().endswith(":") else target.source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
            return names
    return []


def _placeholder_for(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("count", "num", "size", "index", "idx", "n")):
        return "0"
    if any(k in low for k in ("items", "list", "values", "rows")):
        return "[]"
    if any(k in low for k in ("map", "dict", "options", "kwargs")):
        return "{}"
    return '""'


def _fallback_suite(target: Target) -> GeneratedSuite:
    root_hint = target.path
    module_var = "_bh_mod"
    header = f'''import importlib.util
import os
import sys

import pytest

_MODULE_PATH = os.path.join(os.environ["BUG_HUNT_WORKSPACE"], *{target.path.split('/')!r})
_spec = importlib.util.spec_from_file_location("bh_target_module", _MODULE_PATH)
{module_var} = importlib.util.module_from_spec(_spec)
sys.path.insert(0, os.path.dirname(_MODULE_PATH))
_spec.loader.exec_module({module_var})
'''
    tests: List[str] = []
    names: List[str] = []
    if target.kind == "function" and target.symbol:
        params = _fallback_signature_params(target)
        tests.append(
            f"def test_bh_target_exists():\n"
            f"    # expected: the target function exists and is callable\n"
            f"    assert callable(getattr({module_var}, {target.symbol!r}))\n"
        )
        names.append("test_bh_target_exists")
        if not params:
            tests.append(
                f"def test_bh_call_no_args():\n"
                f"    # expected: calling with no arguments does not crash unexpectedly\n"
                f"    {module_var}.{target.symbol}()\n"
            )
            names.append("test_bh_call_no_args")
        else:
            for p in params[:6]:
                defaults = ", ".join(
                    f"{other}={_placeholder_for(other)}" if other != p else f"{other}=None"
                    for other in params
                )
                tname = f"test_bh_none_{_slug(p, 20)}_assumption"
                tests.append(
                    f"def {tname}():\n"
                    f"    # assumption: passing None for '{p}' should be handled cleanly, not crash\n"
                    f"    {module_var}.{target.symbol}({defaults})\n"
                )
                names.append(tname)
    else:
        tests.append(
            "def test_bh_module_imports():\n"
            "    # expected: the target file imports without raising\n"
            f"    assert {module_var} is not None\n"
        )
        names.append("test_bh_module_imports")
    code = header + "\n\n" + "\n\n".join(tests) + "\n"
    return GeneratedSuite(code=code, test_names=names, source="fallback",
                           notes=["no reachable model or model output rejected — deterministic smoke suite"])


async def generate_tests(target: Target, *, owner: str = "", model: Optional[str] = None,
                          max_cases: int = 12) -> GeneratedSuite:
    """Ask the utility model for one pytest file for `target`; fall back to a
    deterministic template suite when no model is reachable or its output
    does not pass validation."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, resolved_model, headers = resolve_endpoint("utility", owner=owner or None)
    except Exception as e:  # noqa: BLE001
        suite = _fallback_suite(target)
        suite.notes.append(f"endpoint resolution failed: {e}")
        return suite
    use_model = model or resolved_model
    if not url or not use_model:
        suite = _fallback_suite(target)
        suite.notes.append("no utility model endpoint configured")
        return suite

    prompt = _build_generation_prompt(target, max_cases)
    try:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=url, model=use_model, messages=[{"role": "user", "content": prompt}],
            headers=headers, temperature=0.2, max_tokens=3000, timeout=60,
            max_retries=1, workload="background",
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[bug_hunt] generation model call failed", exc_info=True)
        suite = _fallback_suite(target)
        suite.notes.append(f"model call failed: {e}")
        return suite
    if isinstance(raw, tuple):
        raw = raw[0]
    if not isinstance(raw, str) or not raw.strip():
        suite = _fallback_suite(target)
        suite.notes.append("model returned an empty reply")
        return suite

    code = _extract_fenced_code(raw)
    problem = _scan_dangerous(code)
    if problem:
        suite = _fallback_suite(target)
        suite.notes.append(f"model output rejected: {problem}")
        return suite
    names = _TEST_DEF_RE.findall(code)
    if not names:
        suite = _fallback_suite(target)
        suite.notes.append("model output had no test_ functions")
        return suite
    return GeneratedSuite(code=code, test_names=names, source="model", model_used=use_model)


# ---------------------------------------------------------------------------
# 3. run_suite — isolated pytest run
# ---------------------------------------------------------------------------

_OUTCOME_RE = re.compile(r"^(\S+\.py::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)\b", re.M)
_FAILURE_BLOCK_RE = re.compile(r"^_{5,} (.+?) _{5,}\s*$\n(.*?)(?=^_{5,}|^={3,}|\Z)", re.M | re.S)
_SUMMARY_RE = re.compile(r"^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s\s*(?:\(.*\))?\s*=*\s*$", re.M)


def _scratch_dir(workspace: str) -> str:
    d = os.path.join(_norm_root(workspace), *SCRATCH_DIRNAME.split(os.sep))
    os.makedirs(d, exist_ok=True)
    return d


def _project_python(workspace: str, env: Dict[str, str]) -> str:
    try:
        from src.agent_tools.subprocess_tools import project_python
        return project_python(workspace, env)
    except Exception:  # noqa: BLE001
        return sys.executable or "python"


def run_suite(workspace: str, suite: GeneratedSuite, *, timeout_s: int = 120,
              slug_hint: str = "target") -> RunResult:
    root = _norm_root(workspace)
    scratch = _scratch_dir(root)
    digest = hashlib.sha1(suite.code.encode("utf-8", "replace")).hexdigest()[:10]
    file_name = f"test_bh_{_slug(slug_hint)}_{digest}.py"
    file_path = os.path.join(scratch, file_name)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(suite.code)
    except OSError as e:
        return RunResult(ran=False, ok=None, inconclusive=True, exit_code=None, tests=[],
                          summary=f"could not write scratch test file: {e}", file_path=file_path,
                          duration_s=0.0)

    env = native_host_environment()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + py_path if py_path else "")
    env["BUG_HUNT_WORKSPACE"] = root
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    python = _project_python(root, env)
    argv = [python, "-m", "pytest", "-v", "-p", "no:cacheprovider", "--no-header",
            "--color=no", file_path]

    t0 = time.time()
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=max(5, int(timeout_s)), env=env)
    except subprocess.TimeoutExpired:
        return RunResult(ran=True, ok=False, inconclusive=True, exit_code=None, tests=[],
                          summary=f"timed out after {timeout_s}s", file_path=file_path,
                          duration_s=round(time.time() - t0, 1))
    except (OSError, subprocess.SubprocessError) as e:
        return RunResult(ran=False, ok=None, inconclusive=True, exit_code=None, tests=[],
                          summary=f"could not run pytest: {e}", file_path=file_path,
                          duration_s=round(time.time() - t0, 1))
    duration = round(time.time() - t0, 1)
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if "No module named pytest" in out or re.search(r"No module named ['\"]?pytest\b", out):
        return RunResult(ran=False, ok=None, inconclusive=True, exit_code=proc.returncode, tests=[],
                          summary="pytest is not installed in the project's interpreter",
                          file_path=file_path, duration_s=duration, output_tail=out[-1500:])

    failure_blocks: Dict[str, str] = {}
    for name, body in _FAILURE_BLOCK_RE.findall(out):
        failure_blocks[name.strip()] = body.strip()

    tests: List[Dict[str, Any]] = []
    for nodeid, outcome in _OUTCOME_RE.findall(out):
        short = nodeid.split("::")[-1]
        tb = failure_blocks.get(short, "") if outcome in ("FAILED", "ERROR") else ""
        tests.append({"name": nodeid, "outcome": outcome.lower(), "traceback": tb[-2000:]})

    m = None
    for m in _SUMMARY_RE.finditer(out):
        pass
    summary = m.group(1).strip() if m else ("passed" if proc.returncode == 0 else f"exit code {proc.returncode}")
    ok = proc.returncode == 0
    inconclusive = proc.returncode not in (0, 1) and proc.returncode is not None
    return RunResult(ran=True, ok=ok, inconclusive=inconclusive, exit_code=proc.returncode,
                      tests=tests, summary=summary, file_path=file_path, duration_s=duration,
                      output_tail=out[-3000:])


# ---------------------------------------------------------------------------
# 4. triage — bug vs. wrong test
# ---------------------------------------------------------------------------

_SEVERITIES = ("low", "medium", "high")
_VERDICTS = ("bug", "test_wrong", "unclear")


def _triage_heuristic(test_name: str, traceback: str) -> Finding:
    tb = traceback or ""
    is_assumption = "assumption" in test_name.lower()
    if is_assumption and "AssertionError" in tb:
        return Finding(test_name=test_name, verdict="unclear", severity="low",
                       root_cause="an 'assumption' test failed — its expectation may not hold for this code",
                       fix_suggestion="review whether the assumption is correct; adjust the test or the code",
                       traceback=tb[-1500:])
    if re.search(r"\b(TypeError|AttributeError)\b", tb) and "None" in tb:
        return Finding(test_name=test_name, verdict="bug", severity="medium",
                       root_cause="unhandled None input reaches an attribute/type operation with no guard",
                       fix_suggestion="validate or default the argument before using it",
                       traceback=tb[-1500:])
    if re.search(r"\bZeroDivisionError\b", tb):
        return Finding(test_name=test_name, verdict="bug", severity="medium",
                       root_cause="a zero/empty input is not guarded before a division",
                       fix_suggestion="check the divisor/length before dividing",
                       traceback=tb[-1500:])
    if re.search(r"\b(IndexError|KeyError)\b", tb):
        return Finding(test_name=test_name, verdict="bug", severity="medium",
                       root_cause="an empty/short input is not guarded before an index/key access",
                       fix_suggestion="check bounds/membership before accessing",
                       traceback=tb[-1500:])
    return Finding(test_name=test_name, verdict="unclear", severity="low",
                   root_cause="failure does not match a known pattern — needs a human look",
                   fix_suggestion="", traceback=tb[-1500:])


def _build_triage_prompt(target: Target, failing: List[Dict[str, Any]]) -> str:
    items = []
    for t in failing:
        items.append(f"- test: {t['name']}\n  traceback:\n{t.get('traceback', '')[-800:]}")
    return f"""You are triaging failing tests generated to hunt bugs in this code:

```python
{target.source}
```

For each failing test below, decide: is this a real BUG in the code, or is
the TEST's expectation WRONG (a wrong assumption the generator made), or is
it UNCLEAR? Reply with ONLY a JSON array, one object per test, in this exact
shape:
[{{"test": "<test name>", "verdict": "bug"|"test_wrong"|"unclear",
  "root_cause": "<one sentence>", "fix_suggestion": "<one sentence, may be empty>",
  "severity": "low"|"medium"|"high"}}]

Failing tests:
{os.linesep.join(items)}
"""


def _parse_triage_json(raw: str) -> List[Dict[str, Any]]:
    text = _extract_fenced_code(raw) if "```" in (raw or "") else (raw or "")
    text = text.strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


async def triage(target: Target, run_result: RunResult, *, owner: str = "",
                 model: Optional[str] = None) -> List[Finding]:
    failing = [t for t in run_result.tests if t.get("outcome") in ("failed", "error")]
    if not failing:
        return []
    heuristics = {t["name"]: _triage_heuristic(t["name"], t.get("traceback", "")) for t in failing}

    try:
        from src.endpoint_resolver import resolve_endpoint
        url, resolved_model, headers = resolve_endpoint("utility", owner=owner or None)
    except Exception:  # noqa: BLE001
        return list(heuristics.values())
    use_model = model or resolved_model
    if not url or not use_model:
        return list(heuristics.values())

    prompt = _build_triage_prompt(target, failing)
    try:
        from src.llm_core import llm_call_async
        raw = await llm_call_async(
            url=url, model=use_model, messages=[{"role": "user", "content": prompt}],
            headers=headers, temperature=0.1, max_tokens=1500, timeout=45,
            max_retries=1, workload="background",
        )
    except Exception:  # noqa: BLE001
        logger.debug("[bug_hunt] triage model call failed", exc_info=True)
        return list(heuristics.values())
    if isinstance(raw, tuple):
        raw = raw[0]
    items = _parse_triage_json(raw if isinstance(raw, str) else "")
    if not items:
        return list(heuristics.values())

    out: List[Finding] = []
    seen = set()
    for item in items:
        name = str(item.get("test") or "").strip()
        if not name or name not in heuristics:
            continue
        verdict = str(item.get("verdict") or "unclear").strip()
        if verdict not in _VERDICTS:
            verdict = "unclear"
        severity = str(item.get("severity") or "low").strip()
        if severity not in _SEVERITIES:
            severity = "low"
        out.append(Finding(
            test_name=name, verdict=verdict,
            root_cause=str(item.get("root_cause") or "")[:400],
            fix_suggestion=str(item.get("fix_suggestion") or "")[:400],
            severity=severity, traceback=heuristics[name].traceback,
        ))
        seen.add(name)
    for name, finding in heuristics.items():
        if name not in seen:
            out.append(finding)
    return out


# ---------------------------------------------------------------------------
# 5. hunt — orchestration + persistence
# ---------------------------------------------------------------------------

def _existing_test_names(path: str) -> set:
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return set()
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")}


def _extract_test_source(code: str, name: str) -> Optional[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    lines = code.splitlines()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = node.lineno - 1
            end = getattr(node, "end_lineno", start + 1)
            return "\n".join(lines[start:end])
    return None


def _keep_regression_tests(workspace: str, slug: str, suite: GeneratedSuite,
                           bug_findings: List[Finding]) -> Optional[str]:
    if not bug_findings:
        return None
    root = _norm_root(workspace)
    tests_dir = os.path.join(root, "tests")
    os.makedirs(tests_dir, exist_ok=True)
    dest = os.path.join(tests_dir, f"test_bughunt_{slug}.py")
    existing_names = _existing_test_names(dest)
    header_needed = not os.path.isfile(dest)
    blocks: List[str] = []
    for finding in bug_findings:
        short = finding.test_name.split("::")[-1]
        if short in existing_names:
            continue
        src = _extract_test_source(suite.code, short)
        if not src:
            continue
        blocks.append(src)
        existing_names.add(short)
    if not blocks and not header_needed:
        return dest if os.path.isfile(dest) else None
    if not blocks:
        return None
    mode = "w" if header_needed else "a"
    with open(dest, mode, encoding="utf-8") as f:
        if header_needed:
            f.write(_header_for_kept_tests(suite))
        f.write("\n\n" + "\n\n".join(blocks) + "\n")
    return dest


def _header_for_kept_tests(suite: GeneratedSuite) -> str:
    header_lines = []
    for line in suite.code.splitlines():
        if line.startswith(("def test_", "async def test_")):
            break
        header_lines.append(line)
    return "\n".join(header_lines).rstrip() + "\n"


def _report_dir(owner: str) -> str:
    return os.path.join(_data_dir(), "bug_hunt", owner or "_shared")


def _persist_report(owner: str, slug: str, report: Report) -> str:
    d = _report_dir(owner)
    os.makedirs(d, exist_ok=True)
    ts = int(report.generated_at)
    path = os.path.join(d, f"{ts}_{slug}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning("[bug_hunt] could not persist report: %s", e)
        return ""
    _rotate_reports(d)
    return path


def _rotate_reports(d: str) -> None:
    try:
        files = sorted(
            (os.path.join(d, fn) for fn in os.listdir(d) if fn.endswith(".json")),
            key=lambda p: os.path.getmtime(p),
        )
    except OSError:
        return
    for old in files[:-REPORT_KEEP] if len(files) > REPORT_KEEP else []:
        try:
            os.remove(old)
        except OSError:
            pass


def list_reports(owner: str, limit: int = 50) -> List[Dict[str, Any]]:
    d = _report_dir(owner)
    if not os.path.isdir(d):
        return []
    out: List[Dict[str, Any]] = []
    try:
        files = sorted(
            (os.path.join(d, fn) for fn in os.listdir(d) if fn.endswith(".json")),
            key=lambda p: os.path.getmtime(p), reverse=True,
        )
    except OSError:
        return []
    for p in files[:max(1, limit)]:
        try:
            with open(p, "r", encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


async def hunt(workspace: str, target: str, *, owner: str = "", keep_tests: bool = False,
               keep_scratch: bool = False, model: Optional[str] = None,
               max_cases: Optional[int] = None, timeout_s: Optional[int] = None) -> Report:
    max_cases = max_cases if max_cases is not None else int(_setting("bug_hunt_max_cases", 12) or 12)
    timeout_s = timeout_s if timeout_s is not None else int(_setting("bug_hunt_timeout_seconds", 120) or 120)
    model = model or (str(_setting("bug_hunt_model", "") or "").strip() or None)

    targets = plan_targets(workspace, target)
    notes: List[str] = []
    all_findings: List[Finding] = []
    tests_run = 0
    tests_failed = 0
    suite_sources: List[str] = []
    kept_paths: List[str] = []

    if not targets:
        notes.append(f"nothing found for target {target!r}")

    for t in targets[:MAX_TARGETS_PER_HUNT]:
        suite = await generate_tests(t, owner=owner, model=model, max_cases=max_cases)
        suite_sources.append(suite.source)
        result = run_suite(workspace, suite, timeout_s=timeout_s, slug_hint=t.qualname)
        if result.ran:
            tests_run += len(result.tests)
            tests_failed += len([x for x in result.tests if x.get("outcome") in ("failed", "error")])
        findings = await triage(t, result, owner=owner, model=model)
        for f in findings:
            f.target = t.qualname
        all_findings.extend(findings)
        if keep_tests:
            bug_findings = [f for f in findings if f.verdict == "bug"]
            kept = _keep_regression_tests(workspace, _slug(t.qualname), suite, bug_findings)
            if kept:
                kept_paths.append(kept)
        if not keep_scratch and result.file_path and os.path.isfile(result.file_path):
            try:
                os.remove(result.file_path)
            except OSError:
                pass
        if result.inconclusive and result.summary:
            notes.append(f"{t.qualname}: {result.summary}")

    suite_source = "model" if "model" in suite_sources else ("fallback" if suite_sources else "none")
    report = Report(
        workspace=_norm_root(workspace), target=target, owner=owner,
        suite_source=suite_source, targets_scanned=[t.to_dict() for t in targets[:MAX_TARGETS_PER_HUNT]],
        findings=all_findings, tests_run=tests_run, tests_failed=tests_failed,
        kept_tests_path=kept_paths[0] if kept_paths else None,
        generated_at=time.time(), notes=notes,
    )
    _persist_report(owner, _slug(target), report)
    return report
