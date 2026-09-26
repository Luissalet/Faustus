"""Shared test configuration - ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
import pytest
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


@pytest.fixture(autouse=True)
def isolated_changeset_receipts(tmp_path_factory):
    """New completion hooks must never write test receipts to the real app.

    Allocate lazily: most tests don't use evidence history at all.
    """
    from src import changeset_store
    allocated = []
    def path():
        if not allocated:
            allocated.append(tmp_path_factory.mktemp("receipts") / "receipts.sqlite3")
        return allocated[0]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(changeset_store, "default_path", path)
        yield


@pytest.fixture(autouse=True)
def clean_dead_host_cooldown():
    """`llm_core` remembers which hosts stopped answering, in module-level
    dicts that outlive a test.

    That memory is the point in production -- it is what stops a turn paying
    a full timeout per round for an engine that is not there. In a test run
    it is cross-contamination: one test pointing at an unreachable endpoint
    cools that host, and a later test that expects a probe silently gets the
    cooldown path instead. It was always possible through `llm_call`; it
    became common once the keep-alive ping and the context-length probe
    started feeding the same table, and it showed as two route tests that
    pass alone and fail in the suite.
    """
    from src import llm_core
    llm_core._dead_hosts.clear()
    llm_core._host_fails.clear()
    yield
    llm_core._dead_hosts.clear()
    llm_core._host_fails.clear()


@pytest.fixture(autouse=True)
def clean_bash_probe_cache():
    """`find_bash` probes once and keeps the answer in a module global.

    Right again in production -- the probe walks a handful of install paths
    and the answer does not change while the process lives. In a test run it
    is cross-contamination: any test that fakes `which` (the stray-tmux test
    fakes it for every name) poisons the cache with a path that does not
    exist, and the next test that really runs a command spawns it and dies
    with WinError 2. It shows as a test that passes alone and fails in the
    suite -- and it crosses files, so the file it fails in is innocent.
    """
    from core import platform_compat
    platform_compat._BASH_PROBED = False
    platform_compat._BASH_CACHE = None
    yield
    platform_compat._BASH_PROBED = False
    platform_compat._BASH_CACHE = None


@pytest.fixture(autouse=True)
def isolated_settings_file(tmp_path_factory, monkeypatch):
    """No test may write into the checkout's real data/settings.json.

    `src/settings.py`'s `SETTINGS_FILE` was never isolated here, so any test
    that reaches the real `set_setting`/`save_settings` -- directly, or
    through a helper that writes as a side effect (`src.safe_mode.quarantine`,
    `src.extension_manifest.record_install_or_update`, the `manage_settings`
    agent tool, ...) -- wrote into the actual checkout's `data/settings.json`.
    A stray `disabled_tools` entry left there then broke unrelated tests
    depending on run order (`tests/test_browser_mcp_reconnect.py`'s
    fake-server tests read that same real setting through
    `src.mcp_manager.builtin_browser_policy_disabled`) -- not reproducible
    from any single test in isolation, hence isolating every test by default
    here rather than chasing one "guilty" test file.
    """
    from src import settings as settings_mod
    path = tmp_path_factory.mktemp("settings") / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(path))
    settings_mod._invalidate_caches()
    yield
    settings_mod._invalidate_caches()


@pytest.fixture(autouse=True)
def isolated_managed_objectives(tmp_path_factory):
    from services import objective_locations
    allocated = []
    def path():
        if not allocated:
            allocated.append(tmp_path_factory.mktemp('managed-objectives'))
        return str(allocated[0])
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(objective_locations, 'managed_root', path)
        yield

@pytest.fixture(autouse=True)
def isolated_kv_rate_history(tmp_path_factory, monkeypatch):
    """`vram_fit.remember_kv_rate` persists accumulated KV-cache-rate
    observations (`KV_HISTORY`) to `DATA_DIR/kv_rate_history.json` so a slow,
    occasional session survives a restart instead of losing every point it
    built up. Several existing tests (`test_vram_admission.py`,
    `test_l66_hw01_concurrent_admit.py`) call `remember_kv_rate` directly and
    never isolate `DATA_DIR`; without this they would write into the real
    checkout's data dir on every run, and leak points between tests besides.
    Point the file at a scratch path and start each test with clean
    in-memory tables. `.clear()`, not reassignment: `routes/model_routes.py`
    aliases `_KV_RATES = vram_fit.KV_RATES` at import time, and swapping in a
    new dict object here would leave that alias pointing at the old one."""
    from src import vram_fit
    path = tmp_path_factory.mktemp("kv_history") / "kv_rate_history.json"
    monkeypatch.setattr(vram_fit, "_kv_history_path", lambda: str(path))
    monkeypatch.setattr(vram_fit, "_kv_history_loaded", True)
    vram_fit.KV_HISTORY.clear()
    vram_fit.KV_RATES.clear()
    yield
    vram_fit.KV_HISTORY.clear()
    vram_fit.KV_RATES.clear()


@pytest.fixture(autouse=True)
def fresh_ports_cache():
    """`process_center._ports_by_pid_cached()` keeps one process-wide, ~2.5s
    cached scan (PENDIENTES §101). Without a reset, a test that populates it
    could feed a stale or fake scan to the next test that runs within that
    window — cross-contamination the same shape as `clean_dead_host_cooldown`
    above."""
    import sys as _sys
    pc = _sys.modules.get("src.process_center")
    if pc is not None:
        pc._PORTS_CACHE.update({"at": 0.0, "value": None})
    yield
    if pc is not None:
        pc._PORTS_CACHE.update({"at": 0.0, "value": None})


@pytest.fixture(autouse=True)
def fresh_session_toolsets():
    """The agent loop remembers each chat's last tool set; tests reuse session
    ids freely, so each starts without that memory."""
    import sys as _sys
    loop = _sys.modules.get("src.agent_loop")
    if loop is not None and hasattr(loop, "_SESSION_TOOLSETS"):
        loop._SESSION_TOOLSETS.clear()
    yield

@pytest.fixture(autouse=True)
def fresh_local_model_slot(monkeypatch):
    """Every test gets its own local-model slot, and the servers it names
    have one generation pipe unless the test says otherwise: the real
    llama-server on a developer machine (four slots on 8081) must not decide
    whether two calls share it, and a test that leaves the process-wide lock
    held must not break the next one's event loop."""
    import sys as _sys
    core = _sys.modules.get("src.llm_core")
    if core is not None and hasattr(core, "_LOCAL_MODEL_LOCK"):
        import asyncio as _asyncio
        monkeypatch.setattr(core, "_LOCAL_MODEL_LOCK", _asyncio.Lock())
        monkeypatch.setattr(core, "_LOCAL_MODEL_CURRENT", {})
        monkeypatch.setattr(core, "_LOCAL_MODEL_WAITING_FOREGROUND", 0)
        if hasattr(core, "_LOCAL_MODEL_SHARED"):
            monkeypatch.setattr(core, "_LOCAL_MODEL_SHARED", {"host": "", "model": "", "count": 0})

            async def _one_slot(url):
                return 1
            monkeypatch.setattr(core, "_server_slots", _one_slot)
    yield


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
