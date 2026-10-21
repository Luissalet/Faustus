"""#1114: the bash/python tools return when the command itself exits, even if a child it started keeps
the tool's stdout/stderr open.

asyncio's Process.wait() only returns after every pipe closed, so `server &` without redirection (or a
Popen that inherits the streams) kept the tool waiting until the idle watchdog killed the whole tree,
deliberate server included. The command's own exit status is known as soon as it exits.
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
import time

import psutil
import pytest

from src import process_manager as pm

PY = sys.executable


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: str(tmp_path))
    monkeypatch.setenv("FAUSTUS_PROCESS_STORE", str(tmp_path / "handles.json"))
    values = {}
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: values.get(key, default))
    pm.reset_for_tests()
    yield
    for handle, rec in list(pm.manager()._records.items()):
        if rec.get("state") in pm.LIVE_STATES:
            pm.manager().stop(handle, pm.Caller(owner=rec.get("owner", ""), session_id=rec.get("session_id", "")))
    pm.reset_for_tests()


def _kill_tree(pid):
    try:
        p = psutil.Process(pid)
        for c in p.children(recursive=True):
            c.kill()
        p.kill()
    except psutil.Error:
        pass


def test_python_tool_returns_although_a_child_holds_its_output():
    from src.agent_tools.subprocess_tools import PythonTool
    code = textwrap.dedent("""
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"])
        print("child", p.pid, flush=True)
        print("parent done", flush=True)
    """)
    started = time.monotonic()
    result = asyncio.run(PythonTool().execute(code, {"owner": "luis", "session_id": "s-1114"}))
    elapsed = time.monotonic() - started
    out = str(result.get("output") or "")
    child = int(out.split("child", 1)[1].split()[0])
    try:
        assert elapsed < 15, elapsed                       # not the 40 s of the child, not the idle timeout
        assert result.get("exit_code") == 0
        assert "parent done" in out                        # output written before the exit is kept
        assert psutil.pid_exists(child)                    # the child was not killed
    finally:
        _kill_tree(child)


def test_bash_background_server_without_redirection_returns():
    from core.platform_compat import find_bash
    from src.agent_tools.subprocess_tools import BashTool
    if sys.platform == "win32" and not find_bash():
        pytest.skip("Git Bash not installed")
    py = PY.replace("\\", "/")
    started = time.monotonic()
    result = asyncio.run(BashTool()._on_host(f'"{py}" -c "import time; time.sleep(40)" &\necho launched',
                                             None, None, "s-1114", caller=pm.Caller(owner="luis", session_id="s-1114")))
    elapsed = time.monotonic() - started
    assert elapsed < 15, elapsed
    assert result.get("exit_code") == 0 and "launched" in str(result.get("output"))
    if sys.platform == "win32":
        still = result.get("still_running") or []
        assert still                                       # #1105 reports the survivor
        for s in still:
            _kill_tree(s["pid"])


def test_a_command_that_finishes_normally_keeps_all_its_output():
    from src.agent_tools.subprocess_tools import PythonTool
    code = "import sys\nfor i in range(300):\n    print('line', i)\nsys.stderr.write('done\\n')"
    result = asyncio.run(PythonTool().execute(code, {"owner": "luis", "session_id": "s-1114"}))
    out = str(result.get("output") or "")
    assert result.get("exit_code") == 0
    assert "line 299" in out and "done" in out and "may be incomplete" not in out
