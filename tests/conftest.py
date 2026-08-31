"""Shared test configuration - ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing core.database below runs init_db() at import time, and its default
# (sqlite:///./data/app.db) can't be opened in a clean worktree because SQLite
# won't create the missing ./data parent dir - pytest then dies during
# collection, before any test module loads. Default to an in-memory DB for the
# test session so collection is deterministic and writes no repo-local
# artifacts. An explicit DATABASE_URL (a real test/CI database) is preserved.
# This only unblocks collection/import-time init; it does not provide a shared
# file-backed DB across processes - tests needing that must set DATABASE_URL.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Child processes the tests spawn (node for the JS contract tests, python for
# scripts) must speak UTF-8 regardless of the host code page: on Windows the
# default is cp1252, and every test that pipes an arrow or an accent through a
# subprocess used to die with a UnicodeEncode/DecodeError.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")

# On Windows `bash` on PATH is usually the WSL launcher in System32, which
# fails with "execvpe(/bin/bash) failed" when no distro is installed. Tests
# that exercise shell snippets need a real POSIX shell: put Git for Windows'
# bash first when it is installed (bash -n, pipes, `||` chains all work there).
if os.name == "nt":
    # usr\bin (the real MSYS bash + coreutils) before bin\bash.exe: the latter
    # is a launcher that puts Git's own usr\bin in front of PATH again, which
    # would shadow the fake `cat`/`python3` shims some tests prepend to PATH.
    _git_bash_dirs = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "usr", "bin"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "usr", "bin"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "bin"),
    ]
    _git_bash_exe = ""
    for _d in _git_bash_dirs:
        if _d and os.path.isfile(os.path.join(_d, "bash.exe")):
            os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
            _git_bash_exe = os.path.join(_d, "bash.exe")
            break
    # PATH order is not enough for `subprocess.run(["bash", ...])`: CreateProcess
    # searches System32 (the WSL stub) BEFORE the PATH directories. Resolve a
    # bare `bash` argv[0] to Git Bash for the test session so shell-snippet
    # tests run the same POSIX shell they run on Linux/macOS.
    if _git_bash_exe:
        import subprocess as _subprocess

        _orig_popen_init = _subprocess.Popen.__init__

        def _popen_init(self, args, *a, **kw):
            try:
                if (isinstance(args, (list, tuple)) and args and isinstance(args[0], str)
                        and args[0].lower() in ("bash", "bash.exe") and not kw.get("shell")):
                    args = [_git_bash_exe, *list(args[1:])]
            except Exception:
                pass
            return _orig_popen_init(self, args, *a, **kw)

        _subprocess.Popen.__init__ = _popen_init

# Pre-import real heavy modules BEFORE any test file's module-level stubs can
# replace them with MagicMock. Some test files (e.g. test_llm_core_sanitize_*)
# stub sqlalchemy/core.database at module scope with `if mod not in sys.modules`,
# which fires during collection. If the real module hasn't been imported yet,
# the stub wins and contaminates every subsequent test that needs the real ORM.
try:
    import sqlalchemy  # noqa: F401
    import sqlalchemy.orm  # noqa: F401
    import core.database  # noqa: F401
    import src.database
except ImportError:
    pass  # not installed - the stubs below will handle it

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db

# Pre-import core.models before test_agent_loop.py's module-level stubs
# run (it replaces sys.modules['core.models'] with a MagicMock during
# collection, which breaks session import in subsequent tests).
import core.models  # noqa: E402

def pytest_configure(config):
    """Register the dynamic taxonomy ``sub_*`` markers before collection.

    The stable ``area_*`` markers are declared in ``pyproject.toml``. The
    per-file ``sub_*`` markers are derived from the test filenames here so that
    unknown-mark warnings still surface genuine typos outside the taxonomy. This
    only registers marker names; it imports no production module.
    """
    import pathlib
    from tests._taxonomy import discover_markers

    tests_dir = pathlib.Path(__file__).parent
    paths = list(tests_dir.rglob("test_*.py")) + list(tests_dir.rglob("*_test.py"))
    for marker_name in discover_markers(paths):
        if marker_name.startswith("sub_"):
            config.addinivalue_line("markers", f"{marker_name}: taxonomy sub-area marker")


def pytest_collection_modifyitems(config, items):
    """Tag each collected test with its taxonomy ``area_*`` and ``sub_*`` markers.

    Collection-time only: this adds markers and nothing else. It does not skip,
    reorder, or deselect tests, mutate fixtures or the environment, or import any
    production module. See ``tests/_taxonomy.py`` for the classification rules.
    """
    import pytest
    from tests._taxonomy import markers_for_path

    for item in items:
        path = getattr(item, "path", None) or item.fspath
        for marker_name in markers_for_path(path):
            item.add_marker(getattr(pytest.mark, marker_name))
