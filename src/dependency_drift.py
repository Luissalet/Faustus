"""dependency_drift.py — catch missing deps before a traceback does (H2).

Forensic finding (silhouettes_analysis.md #3, #9, #21): a local 27B model
discovers a missing package (`shapely`, then two days later `jsonschema`) by
running code, reading the traceback, diagnosing it, and fixing it — burning
whole rounds on something a deterministic check settles in milliseconds.
Chat #21 is the sharpest example: 10 rounds just to notice `jsonschema` was
never installed, something `importlib.metadata` answers in one call.

This module is the harness's compensation, not the model's: at the start of
a code turn on a workspace, read the project's declared dependencies
(`requirements*.txt`, `pyproject.toml [project.dependencies]`,
`package.json` `dependencies`/`devDependencies`), check them against what is
ACTUALLY installed (the project's own Python interpreter via
`importlib.metadata`, and `node_modules/<pkg>/package.json` for Node), and
hand back a small, cached report plus a ready-to-inject system note. Nothing
here ever runs `pip install`/`npm install` directly — `auto_install()` goes
through `src.tool_execution`'s EXEC-05 plan/approve/execute flow, the same
gate any other dependency install goes through.

Stdlib + `packaging` (already vendored via the `pip` venv virtually every
Python install carries — see `_HAS_PACKAGING` below) only. Never raises out
of its public functions; a parse or subprocess failure degrades to "nothing
found" rather than blocking the turn.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

try:  # pragma: no cover — exercised indirectly; packaging is normally present
    from packaging.requirements import InvalidRequirement, Requirement
    _HAS_PACKAGING = True
except Exception:  # pragma: no cover
    _HAS_PACKAGING = False

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover — < 3.11 host
    tomllib = None  # type: ignore[assignment]


PYTHON_CHECK_TIMEOUT_S = 20
CACHE_DIRNAME = "dependency_drift"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DriftReport:
    """What the harness needs to decide "install before running anything"."""

    missing_python: Tuple[str, ...] = ()
    missing_node: Tuple[str, ...] = ()
    venv: Optional[str] = None
    hint: str = ""
    from_cache: bool = False
    checked_python: Tuple[str, ...] = ()
    checked_node: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing_python and not self.missing_node

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "missing_python": list(self.missing_python),
            "missing_node": list(self.missing_node),
            "venv": self.venv,
            "hint": self.hint,
            "from_cache": self.from_cache,
            "ok": self.ok,
        }


_EMPTY_REPORT = DriftReport()


# ---------------------------------------------------------------------------
# requirements*.txt parsing
# ---------------------------------------------------------------------------

# Minimal fallback parser used only when `packaging` is unavailable: strips
# comments/whitespace, then splits "name[extra1,extra2]<spec>; marker".
_REQ_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;#]*?)\s*(?:;\s*(.*))?\s*$"
)
_MARKER_CLAUSE_RE = re.compile(
    r"(sys_platform|platform_system|os_name)\s*(==|!=)\s*['\"]([^'\"]+)['\"]"
)


def _current_marker_env() -> Dict[str, str]:
    plat = sys.platform  # 'win32', 'linux', 'darwin', ...
    return {
        "sys_platform": plat,
        "platform_system": "Windows" if plat == "win32" else ("Darwin" if plat == "darwin" else "Linux"),
        "os_name": "nt" if plat == "win32" else "posix",
    }


def _marker_matches_fallback(marker: str) -> bool:
    """Minimal environment-marker evaluator for when `packaging` is absent.

    Only understands the `sys_platform`/`platform_system`/`os_name`
    `==`/`!=` clauses joined by `and`/`or` — exactly what real requirements
    files use for "windows only" pins (the case the H2 spec calls out
    explicitly). Anything it cannot parse is treated as matching (fail
    open: better to over-check a package than silently skip a real dep).
    """
    marker = (marker or "").strip()
    if not marker:
        return True
    env = _current_marker_env()
    # Evaluate 'and'/'or' left-to-right without precedence games — real
    # requirements files use at most one boolean operator per marker.
    for op_word, combine in (("or", any), ("and", all)):
        if f" {op_word} " in marker:
            parts = [p.strip() for p in marker.split(f" {op_word} ")]
            results = [_marker_matches_fallback(p) for p in parts]
            return combine(results)
    m = _MARKER_CLAUSE_RE.match(marker)
    if not m:
        return True
    key, op, value = m.group(1), m.group(2), m.group(3)
    actual = env.get(key, "")
    matches = (actual == value)
    return matches if op == "==" else not matches


def _dist_name_from_req_line(line: str) -> Optional[str]:
    """Best-effort distribution name from one non-comment requirement line
    (handles `-e`/`-r`/URL/local-path lines by returning None — those are
    not installable-by-name and out of scope for a drift check)."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "-e ", "-r ", "--")):
        return None
    if "://" in stripped or stripped.startswith((".", "/")):
        return None
    if _HAS_PACKAGING:
        try:
            req = Requirement(stripped)
        except InvalidRequirement:
            return None
        if req.marker is not None:
            try:
                if not req.marker.evaluate():
                    return None
            except Exception:  # pragma: no cover
                pass
        return req.name
    match = _REQ_LINE_RE.match(stripped)
    if not match:
        return None
    name, _extras, _spec, marker = match.groups()
    if marker and not _marker_matches_fallback(marker):
        return None
    return name


def parse_requirements_file(path: str) -> List[str]:
    """Distribution names declared in one `requirements*.txt`, environment
    markers already applied (a Windows-only pin is dropped on Linux)."""
    names: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                name = _dist_name_from_req_line(raw_line)
                if name:
                    names.append(name)
    except OSError:
        return []
    return names


def find_requirements_files(project_root: str) -> List[str]:
    try:
        entries = os.listdir(project_root)
    except OSError:
        return []
    out = []
    for name in sorted(entries):
        if re.match(r"^requirements(-[\w.]+)?\.txt$", name):
            out.append(os.path.join(project_root, name))
    return out


def parse_pyproject_dependencies(project_root: str) -> List[str]:
    """`[project].dependencies` (PEP 621) — `[tool.poetry.dependencies]`
    and friends are a different schema and out of scope here."""
    path = os.path.join(project_root, "pyproject.toml")
    if tomllib is None or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):  # type: ignore[attr-defined]
        return []
    deps = ((data.get("project") or {}).get("dependencies")) or []
    names: List[str] = []
    for entry in deps:
        name = _dist_name_from_req_line(str(entry))
        if name:
            names.append(name)
    return names


def declared_python_dependencies(project_root: str) -> List[str]:
    """All Python distribution names this project declares, deduped,
    order-preserving (requirements*.txt first, then pyproject)."""
    seen: Dict[str, None] = {}
    for req_path in find_requirements_files(project_root):
        for name in parse_requirements_file(req_path):
            seen.setdefault(_norm_dist_name(name), None)
    for name in parse_pyproject_dependencies(project_root):
        seen.setdefault(_norm_dist_name(name), None)
    return list(seen.keys())


def _norm_dist_name(name: str) -> str:
    # PEP 503 normalization (case/._- collapse) — "python-dotenv" and
    # "Python_Dotenv" must hash and compare as the same distribution.
    return re.sub(r"[-_.]+", "-", name).strip().lower()


# ---------------------------------------------------------------------------
# package.json parsing
# ---------------------------------------------------------------------------

def declared_node_dependencies(project_root: str) -> List[str]:
    path = os.path.join(project_root, "package.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    names: List[str] = []
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            names.extend(str(n) for n in section.keys())
    return names


def missing_node_dependencies(project_root: str, declared: Sequence[str]) -> List[str]:
    """A declared package is "missing" when `node_modules/<pkg>/package.json`
    does not exist — scoped packages (`@scope/name`) use the nested
    `node_modules/@scope/name` layout npm/yarn/pnpm all produce."""
    node_modules = os.path.join(project_root, "node_modules")
    missing: List[str] = []
    for name in declared:
        candidate = os.path.join(node_modules, *name.split("/"), "package.json")
        if not os.path.isfile(candidate):
            missing.append(name)
    return missing


# ---------------------------------------------------------------------------
# Python installed-check (via the project's own interpreter, out of process)
# ---------------------------------------------------------------------------

_CHECK_SCRIPT = """
import importlib.metadata as m
import json
import sys
names = json.loads(sys.argv[1])
missing = []
for n in names:
    try:
        m.distribution(n)
    except m.PackageNotFoundError:
        missing.append(n)
    except Exception:
        missing.append(n)
print(json.dumps(missing))
"""


def _run_python_check(python_exe: str, names: Sequence[str], *, cwd: str,
                       env: Optional[Mapping[str, str]] = None,
                       timeout: int = PYTHON_CHECK_TIMEOUT_S) -> List[str]:
    """Runs `_CHECK_SCRIPT` under `python_exe` as a real subprocess (no
    shell, argv list) and returns the names it reports missing. Never
    raises: any failure (interpreter missing, timeout, bad JSON) is treated
    as "could not verify" and returns the FULL list — fail toward "tell the
    model to check", never silently claim everything is present."""
    if not names:
        return []
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as handle:
            handle.write(_CHECK_SCRIPT)
            script_path = handle.name
        try:
            proc = subprocess.run(
                [python_exe, script_path, json.dumps(list(names))],
                cwd=cwd or None,
                env=dict(env) if env else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass
        if proc.returncode != 0:
            logger.debug("dependency_drift: python check exit=%s stderr=%s", proc.returncode, proc.stderr[:500])
            return list(names)
        return json.loads(proc.stdout.strip() or "[]")
    except (subprocess.TimeoutExpired, OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("dependency_drift: python check failed: %s", exc)
        return list(names)


# ---------------------------------------------------------------------------
# Cache: (deps-file sha, site-packages mtime) keyed per workspace
# ---------------------------------------------------------------------------

def _cache_dir() -> str:
    return os.path.join(DATA_DIR, CACHE_DIRNAME)


def _workspace_hash(project_root: str) -> str:
    root = os.path.realpath(os.path.expanduser(project_root or ""))
    key = root.replace("\\", "/")
    if os.name == "nt":
        key = key.lower()
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def _cache_path(project_root: str) -> str:
    return os.path.join(_cache_dir(), f"{_workspace_hash(project_root)}.json")


def _deps_files_sha(project_root: str) -> str:
    """One sha1 over every deps-declaring file's content (requirements*.txt,
    pyproject.toml, package.json) so any change to any of them — a package
    added, a version pinned differently — invalidates the cache."""
    hasher = hashlib.sha1()
    paths = sorted(find_requirements_files(project_root))
    paths.append(os.path.join(project_root, "pyproject.toml"))
    paths.append(os.path.join(project_root, "package.json"))
    for path in paths:
        try:
            with open(path, "rb") as handle:
                hasher.update(path.encode("utf-8", "replace"))
                hasher.update(handle.read())
        except OSError:
            continue
    return hasher.hexdigest()


def _site_packages_mtime(python_exe: str, project_root: str) -> float:
    """A cheap proxy for "has anything been installed/uninstalled since the
    last check": the mtime of the venv's site-packages dir (or, lacking a
    project venv, of `node_modules` alongside it) — installing a package
    always touches its parent directory's mtime."""
    candidates = []
    for name in (".venv", "venv", "env", ".env"):
        for rel in (("Lib", "site-packages"), ("lib", "site-packages")):
            candidates.append(os.path.join(project_root, name, *rel))
    # A python3.X-named site-packages dir (posix venvs) — glob loosely.
    for name in (".venv", "venv", "env", ".env"):
        libdir = os.path.join(project_root, name, "lib")
        if os.path.isdir(libdir):
            try:
                for sub in os.listdir(libdir):
                    candidates.append(os.path.join(libdir, sub, "site-packages"))
            except OSError:
                pass
    best = 0.0
    for cand in candidates:
        try:
            best = max(best, os.path.getmtime(cand))
        except OSError:
            continue
    node_modules = os.path.join(project_root, "node_modules")
    try:
        best = max(best, os.path.getmtime(node_modules))
    except OSError:
        pass
    return best


def _load_cache(project_root: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_cache_path(project_root), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _save_cache(project_root: str, entry: Dict[str, Any]) -> None:
    try:
        os.makedirs(_cache_dir(), exist_ok=True)
        path = _cache_path(project_root)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(entry, handle)
        os.replace(tmp, path)
    except OSError as exc:  # pragma: no cover — best-effort cache
        logger.debug("dependency_drift: cache write failed: %s", exc)


def clear_cache(project_root: str) -> None:
    """Test hook / manual invalidation."""
    try:
        os.unlink(_cache_path(project_root))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_drift(
    project_root: str,
    *,
    python_exe: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    use_cache: bool = True,
) -> DriftReport:
    """The one function callers need: declared vs. actually-installed, for
    both Python and Node, cached by (deps-file sha, site-packages mtime).

    `python_exe` defaults to `src.agent_tools.subprocess_tools.project_python`
    (the same interpreter picker `project_tests.py`/the `python` tool use) —
    pass it explicitly only in tests, to avoid depending on a real venv.
    Never raises: any internal failure degrades to an empty/partial report
    rather than blocking the turn it is guarding.
    """
    root = project_root or ""
    if not root or not os.path.isdir(root):
        return _EMPTY_REPORT

    py_names = declared_python_dependencies(root)
    node_names = declared_node_dependencies(root)
    if not py_names and not node_names:
        return _EMPTY_REPORT

    if python_exe is None:
        try:
            from src.agent_tools.subprocess_tools import project_python
            from src.native_env import native_host_environment
            python_exe = project_python(root, env or native_host_environment())
        except Exception:  # pragma: no cover
            python_exe = sys.executable or "python"

    deps_sha = _deps_files_sha(root)
    sp_mtime = _site_packages_mtime(python_exe, root)

    if use_cache:
        cached = _load_cache(root)
        if cached and cached.get("deps_sha") == deps_sha and cached.get("sp_mtime") == sp_mtime:
            return DriftReport(
                missing_python=tuple(cached.get("missing_python") or ()),
                missing_node=tuple(cached.get("missing_node") or ()),
                venv=cached.get("venv"),
                hint=cached.get("hint") or "",
                from_cache=True,
                checked_python=tuple(py_names),
                checked_node=tuple(node_names),
            )

    missing_py = _run_python_check(python_exe, py_names, cwd=root, env=env)
    missing_node = missing_node_dependencies(root, node_names) if node_names else []

    hint = ""
    if missing_py or missing_node:
        parts = []
        if missing_py:
            parts.append(f"pip: {', '.join(missing_py)}")
        if missing_node:
            parts.append(f"npm: {', '.join(missing_node)}")
        hint = "; ".join(parts)

    report = DriftReport(
        missing_python=tuple(missing_py),
        missing_node=tuple(missing_node),
        venv=python_exe,
        hint=hint,
        from_cache=False,
        checked_python=tuple(py_names),
        checked_node=tuple(node_names),
    )

    _save_cache(root, {
        "deps_sha": deps_sha,
        "sp_mtime": sp_mtime,
        "missing_python": list(missing_py),
        "missing_node": list(missing_node),
        "venv": python_exe,
        "hint": hint,
    })
    return report


# ---------------------------------------------------------------------------
# System note (ES/EN) — for injection before the last user message
# ---------------------------------------------------------------------------

def system_note(report: DriftReport, language: str = "en") -> str:
    """Text for a system-role message telling the model what is missing and
    how to fix it (via `install_dependencies`, never a bare `pip install` in
    a `bash`/`python` call). Empty string when `report.ok` — callers should
    not inject anything in that case."""
    if not report or report.ok:
        return ""
    lang = "es" if str(language or "").lower().startswith("es") else "en"
    if lang == "es":
        lines = ["Faltan dependencias declaradas por el proyecto que aún no están instaladas:"]
        if report.missing_python:
            lines.append(f"- Python (pip): {', '.join(report.missing_python)}")
        if report.missing_node:
            lines.append(f"- Node (npm): {', '.join(report.missing_node)}")
        lines.append(
            "Instálalas con la herramienta install_dependencies antes de ejecutar o importar "
            "ese código — no ejecutes nada que las use hasta entonces, y no llames a pip/npm "
            "directamente por bash."
        )
    else:
        lines = ["The project declares dependencies that are not installed yet:"]
        if report.missing_python:
            lines.append(f"- Python (pip): {', '.join(report.missing_python)}")
        if report.missing_node:
            lines.append(f"- Node (npm): {', '.join(report.missing_node)}")
        lines.append(
            "Install them with the install_dependencies tool before running or importing that "
            "code — do not run anything that needs them yet, and do not call pip/npm directly "
            "via bash."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Optional auto-install — always through EXEC-05, never a bare pip/npm call
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AutoInstallResult:
    attempted: bool
    ok: bool
    outcomes: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    reason: str = ""

    def to_mapping(self) -> Dict[str, Any]:
        return {"attempted": self.attempted, "ok": self.ok, "outcomes": list(self.outcomes), "reason": self.reason}


async def auto_install(
    report: DriftReport,
    project_root: str,
    *,
    owner: str = "",
    enabled: Optional[bool] = None,
) -> AutoInstallResult:
    """Installs `report`'s missing packages through
    `src.tool_execution`'s EXEC-05 plan/approve/execute flow — the same
    "detect manager, hash the plan, require approval" gate any other
    dependency install goes through. Gated by the
    `agent_auto_install_missing_deps` setting (default False); pass
    `enabled=True` to force it (tests) or `enabled=False` to force-skip.

    This function NEVER shells out to `pip`/`npm` itself — it only builds a
    plan and hands it to `execute_dependency_install`, which is the sole
    place a package-manager subprocess is spawned.
    """
    if report is None or report.ok:
        return AutoInstallResult(attempted=False, ok=True, reason="nothing missing")

    if enabled is None:
        try:
            from src.settings import get_setting
            enabled = bool(get_setting("agent_auto_install_missing_deps", False))
        except Exception:
            enabled = False
    if not enabled:
        return AutoInstallResult(attempted=False, ok=False, reason="agent_auto_install_missing_deps is off")

    from src.tool_execution import (
        execute_dependency_install,
        plan_dependency_install,
        record_plan_approval,
    )

    outcomes: List[Dict[str, Any]] = []
    ok = True
    if report.missing_python:
        try:
            plan = plan_dependency_install(project_root, list(report.missing_python))
            record_plan_approval(owner, plan.plan_hash)  # auto-install implies approval
            outcome = await execute_dependency_install(plan, approved=True, owner=owner)
            outcomes.append({"manager": "pip", **outcome.to_mapping()})
            ok = ok and outcome.ok
        except (ValueError, PermissionError) as exc:
            outcomes.append({"manager": "pip", "ok": False, "error": str(exc)})
            ok = False
    if report.missing_node:
        try:
            plan = plan_dependency_install(project_root, list(report.missing_node))
            record_plan_approval(owner, plan.plan_hash)
            outcome = await execute_dependency_install(plan, approved=True, owner=owner)
            outcomes.append({"manager": plan.manager, **outcome.to_mapping()})
            ok = ok and outcome.ok
        except (ValueError, PermissionError) as exc:
            outcomes.append({"manager": "npm", "ok": False, "error": str(exc)})
            ok = False

    return AutoInstallResult(attempted=True, ok=ok, outcomes=tuple(outcomes))
