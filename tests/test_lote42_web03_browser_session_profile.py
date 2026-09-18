"""WEB-03 (lote 42): wire `src.browser_sessions`' per-(owner, task) profile
into the actual Playwright MCP launch args (`src.builtin_mcp`).

Before this lote `src/browser_sessions.py` existed, fully tested, but
nothing in `src/builtin_mcp.py` ever called it -- every launch used the one
shared `_browser_profile_dir()` regardless of caller (MAPA_REUTILIZACION.md's
WEB-03 row: "builtin_mcp.py sigue lanzando el navegador con el perfil
global"). This only adds the OPTIONAL owner_id/task_id path: passing neither
must reproduce today's single-profile behaviour exactly (rule 3).
"""
import importlib.util
from pathlib import Path
import sys
import types

import pytest

from src import browser_sessions as bs

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_sessions(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    bs._SESSIONS.clear()
    yield
    bs._SESSIONS.clear()


def _load_builtin_mcp(monkeypatch):
    """Mirrors tests/test_builtin_mcp_npx_cache.py's isolated-load harness."""
    core = types.ModuleType("core")
    core.__path__ = []
    platform_compat = types.ModuleType("core.platform_compat")
    platform_compat.IS_WINDOWS = False
    platform_compat.which_tool = lambda name: None
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.platform_compat", platform_compat)

    spec = importlib.util.spec_from_file_location(
        "builtin_mcp_under_test_web03",
        ROOT / "src" / "builtin_mcp.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_without_owner_and_task_the_shared_profile_is_used(monkeypatch):
    """No capability lost (rule 3): a caller that never asks for a session
    keeps landing on the single global profile, unchanged."""
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    args = builtin_mcp._browser_mcp_args(["-y", "@playwright/mcp@latest"], settings={})

    idx = args.index("--user-data-dir")
    assert args[idx + 1] == builtin_mcp._browser_profile_dir()
    assert bs._SESSIONS == {}  # no session was opened as a side effect


def test_owner_and_task_use_the_session_profile_not_the_shared_one(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    args = builtin_mcp._browser_mcp_args(
        ["-y", "@playwright/mcp@latest"], settings={}, owner_id="alice", task_id="task-1",
    )

    idx = args.index("--user-data-dir")
    expected = bs.open_session("alice", "task-1").profile_dir
    assert args[idx + 1] == expected
    assert args[idx + 1] != builtin_mcp._browser_profile_dir()


def test_session_profile_is_isolated_per_owner_and_task(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    args_a = builtin_mcp._browser_mcp_args(
        ["-y", "@playwright/mcp@latest"], settings={}, owner_id="alice", task_id="task-1",
    )
    args_b = builtin_mcp._browser_mcp_args(
        ["-y", "@playwright/mcp@latest"], settings={}, owner_id="bob", task_id="task-1",
    )
    dir_a = args_a[args_a.index("--user-data-dir") + 1]
    dir_b = args_b[args_b.index("--user-data-dir") + 1]
    assert dir_a != dir_b


def test_isolated_profile_setting_still_wins_over_a_session(monkeypatch):
    """`browser_profile: isolated` means "no persisted profile at all" --
    that must still hold even when owner_id/task_id are passed; WEB-03 asks
    for isolation, not for overriding an explicit no-profile choice."""
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    monkeypatch.delenv("ODYSSEUS_BROWSER_ISOLATED", raising=False)
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    args = builtin_mcp._browser_mcp_args(
        ["-y", "@playwright/mcp@latest"],
        settings={"browser_profile": "isolated"},
        owner_id="alice", task_id="task-1",
    )
    assert "--isolated" in args
    assert "--user-data-dir" not in args
    assert bs._SESSIONS == {}  # isolated mode never opens a browser_sessions session


def test_npx_server_launch_threads_owner_and_task_through(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    args, _env = builtin_mcp._npx_server_launch(
        builtin_mcp.BROWSER_SERVER_ID, owner_id="alice", task_id="task-1",
    )
    expected = bs.open_session("alice", "task-1").profile_dir
    assert args[args.index("--user-data-dir") + 1] == expected


def test_close_browser_session_for_task_delegates_to_browser_sessions(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    assert builtin_mcp.close_browser_session_for_task("alice", "task-1") is False  # nothing open yet
    builtin_mcp._browser_mcp_args(
        ["-y", "@playwright/mcp@latest"], settings={}, owner_id="alice", task_id="task-1",
    )
    assert bs.get_session("alice", "task-1") is not None
    assert builtin_mcp.close_browser_session_for_task("alice", "task-1") is True
    assert bs.get_session("alice", "task-1").is_open is False
