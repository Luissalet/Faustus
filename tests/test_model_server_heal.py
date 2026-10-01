"""A local model server stuck answering garbage is checked and restarted
(src/model_server_heal.py). Live 01-10: the 8081 llama-server answered
"////" to everything three times in a day and the helper on 8082 "????" for
hours, until someone restarted them by hand."""
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

from src import engine_swap
from src import model_server_heal as heal

URL = "http://127.0.0.1:8081/v1"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    heal._INFLIGHT.clear()
    heal._LAST_RESTART.clear()
    heal._EVENTS.clear()
    monkeypatch.setattr(heal.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(heal, "_settings", lambda: {"enabled": True, "commands": [], "timeout_s": 5.0})
    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda url: None)
    yield
    heal._INFLIGHT.clear()
    heal._LAST_RESTART.clear()


def _sanity(monkeypatch, answers):
    seq = list(answers)

    async def sane(url, model, timeout_s=30.0):
        return seq.pop(0) if seq else True
    monkeypatch.setattr(engine_swap, "generates_sanely", sane)


def test_the_restart_command_is_found_by_port():
    cmds = ["127.0.0.1:8082=helper.cmd", "localhost:8081=powershell -File restart.ps1", "nonsense"]
    assert heal.command_for(8081, cmds) == "powershell -File restart.ps1"
    assert heal.command_for(8082, cmds) == "helper.cmd"
    assert heal.command_for(9999, cmds) is None


def test_a_server_that_answers_normally_is_left_alone(monkeypatch):
    _sanity(monkeypatch, [True])
    monkeypatch.setattr(heal, "_end_process", lambda pid: pytest.fail("must not end a healthy server"))
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "healthy"


def test_a_remote_server_is_never_touched(monkeypatch):
    out = asyncio.run(heal.heal("https://api.example.com/v1", "m"))
    assert out["action"] == "skipped"


def test_a_supervised_server_is_ended_and_comes_back(monkeypatch):
    _sanity(monkeypatch, [False])
    ended = []
    monkeypatch.setattr(heal, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(heal, "supervisor_of", lambda pid: {"pid": 7, "name": "powershell.exe", "cmdline": "x"})

    async def end(pid):
        ended.append(pid)
        return True

    async def back(url, model, port, timeout_s):
        return True
    monkeypatch.setattr(heal, "_end_process", end)
    monkeypatch.setattr(heal, "_wait_back", back)
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "restarted" and ended == [4242] and "launcher" in out["how"]
    assert not os.path.exists(heal._lock_path(8081))           # the lock is released
    assert heal.status()["recent"][-1]["action"] == "restarted"


def test_a_supervised_server_goes_back_through_its_launcher_not_a_managed_engine(monkeypatch):
    """Live 01-10: a q4 server kept alive by its watchdog got "restarted" through a managed engine registered for
    the same port with the q8 command, and both models loaded at once."""
    _sanity(monkeypatch, [False])
    ended = []
    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda url: {"id": "engine-q8"})

    async def no_engine(engine):
        pytest.fail("the managed engine describes another model; the live launcher restarts this one")
    monkeypatch.setattr(engine_swap, "restart_managed_engine", no_engine)
    monkeypatch.setattr(heal, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(heal, "supervisor_of", lambda pid: {"pid": 7, "name": "powershell.exe", "cmdline": "x"})

    async def end(pid):
        ended.append(pid)
        return True

    async def back(url, model, port, timeout_s):
        return True
    monkeypatch.setattr(heal, "_end_process", end)
    monkeypatch.setattr(heal, "_wait_back", back)
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "restarted" and ended == [4242] and "launcher" in out["how"]


def test_without_a_launcher_the_managed_engine_restarts_it(monkeypatch):
    _sanity(monkeypatch, [False, True])
    used = []
    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda url: {"id": "engine-1"})

    async def restart(engine):
        used.append(engine["id"])
        return True
    monkeypatch.setattr(engine_swap, "restart_managed_engine", restart)
    monkeypatch.setattr(heal, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(heal, "supervisor_of", lambda pid: None)
    monkeypatch.setattr(heal, "_end_process", lambda pid: pytest.fail("a managed engine is restarted, not killed"))
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "restarted" and used == ["engine-1"]


def test_a_broken_server_nothing_can_restart_is_reported_not_killed(monkeypatch):
    _sanity(monkeypatch, [False])
    monkeypatch.setattr(heal, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(heal, "supervisor_of", lambda pid: None)
    monkeypatch.setattr(heal, "_end_process", lambda pid: pytest.fail("no launcher: ending it would leave it down"))
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "manual"


def test_a_configured_command_is_run(monkeypatch):
    _sanity(monkeypatch, [False])
    ran = []
    monkeypatch.setattr(heal, "_settings", lambda: {"enabled": True, "timeout_s": 5.0,
                                                    "commands": ["127.0.0.1:8081=restart-it"]})
    monkeypatch.setattr(heal, "listener_pid", lambda port: None)
    monkeypatch.setattr(heal, "_run_command", lambda cmd: ran.append(cmd) or True)

    async def back(url, model, port, timeout_s):
        return True
    monkeypatch.setattr(heal, "_wait_back", back)
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "restarted" and ran == ["restart-it"]


def test_a_server_still_broken_right_after_a_restart_is_not_restarted_in_a_loop(monkeypatch):
    _sanity(monkeypatch, [False])
    heal._LAST_RESTART["8081"] = time.time() - 10
    monkeypatch.setattr(heal, "_end_process", lambda pid: pytest.fail("restart loop"))
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "failed" and "not restarting again" in out["detail"]


def test_another_instance_restarting_it_is_waited_for(monkeypatch):
    _sanity(monkeypatch, [False])
    monkeypatch.setattr(heal, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(heal, "supervisor_of", lambda pid: {"pid": 7, "name": "powershell.exe", "cmdline": "x"})
    with open(heal._lock_path(8081), "w") as fh:            # held by the other instance
        fh.write("1 0\n")
    monkeypatch.setattr(heal, "_end_process", lambda pid: pytest.fail("the other instance is restarting it"))

    async def back(url, model, port, timeout_s):
        return True
    monkeypatch.setattr(heal, "_wait_back", back)
    out = asyncio.run(heal.heal(URL, "m"))
    assert out["action"] == "waited"


def test_concurrent_checks_of_one_server_share_one_attempt(monkeypatch):
    _sanity(monkeypatch, [False])
    calls = []
    monkeypatch.setattr(heal, "listener_pid", lambda port: 4242)
    monkeypatch.setattr(heal, "supervisor_of", lambda pid: {"pid": 7, "name": "powershell.exe", "cmdline": "x"})

    async def end(pid):
        calls.append(pid)
        await asyncio.sleep(0.05)
        return True

    async def back(url, model, port, timeout_s):
        return True
    monkeypatch.setattr(heal, "_end_process", end)
    monkeypatch.setattr(heal, "_wait_back", back)

    async def main():
        return await asyncio.gather(heal.heal(URL, "m"), heal.heal(URL, "m"), heal.heal(URL, "m"))
    results = asyncio.run(main())
    assert calls == [4242] and all(r["action"] == "restarted" for r in results)


def test_switched_off_it_does_nothing(monkeypatch):
    monkeypatch.setattr(heal, "_settings", lambda: {"enabled": False, "commands": [], "timeout_s": 5.0})
    assert asyncio.run(heal.heal(URL, "m"))["action"] == "off"
    assert heal.schedule(URL, "m") is False


def test_the_agent_harness_restart_goes_through_the_heal(monkeypatch):
    seen = []

    async def fake_heal(url, model, reason=""):
        seen.append((url, model))
        return {"action": "restarted"}
    monkeypatch.setattr(heal, "heal", fake_heal)
    assert asyncio.run(engine_swap.restart_if_garbled(URL, "m")) is True
    assert seen == [(URL, "m")]


def test_a_symbol_run_in_a_stream_schedules_a_check_of_that_server(monkeypatch):
    from src import llm_core
    scheduled = []
    monkeypatch.setattr(heal, "schedule", lambda url, model, reason="": scheduled.append((url, model)) or True)
    exc = llm_core.DegenerateOutput("repeated unit '/' 120 times (120 chars)", "q", URL)
    chunk = llm_core._degenerate_output_error_chunk(exc)
    assert scheduled == [(URL, "q")] and "will restart it" in chunk
    # a reasoning loop is the model's own doing: no server check
    scheduled.clear()
    llm_core._degenerate_output_error_chunk(llm_core.DegenerateOutput("reasoning loop: x", "q", URL))
    assert scheduled == []


def test_a_whole_reply_of_question_marks_schedules_a_check(monkeypatch):
    from src import llm_core
    scheduled = []
    monkeypatch.setattr(heal, "schedule", lambda url, model, reason="": scheduled.append(url) or True)
    llm_core._schedule_heal_on_garbage("http://127.0.0.1:8082/v1/chat/completions", "h", "????????????????????")
    llm_core._schedule_heal_on_garbage("http://127.0.0.1:8082/v1/chat/completions", "h", "Un título normal")
    llm_core._schedule_heal_on_garbage("https://api.example.com/v1", "h", "????????????????????")
    assert scheduled == ["http://127.0.0.1:8082/v1/chat/completions"]


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="POSIX shell launcher")
def test_a_launcher_shell_is_found_as_the_supervisor_and_the_listener_by_port():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        port = srv.getsockname()[1]
        assert heal.listener_pid(port) == os.getpid()
    finally:
        srv.close()
    # bash keeps running after its child (the trailing command stops an exec)
    launcher = subprocess.Popen(["bash", "-c", f"{sys.executable} -c 'import time; time.sleep(20)'; true"])
    try:
        import psutil
        child = None
        for _ in range(50):
            kids = psutil.Process(launcher.pid).children()
            if kids:
                child = kids[0]
                break
            time.sleep(0.1)
        assert child is not None
        sup = heal.supervisor_of(child.pid)
        assert sup is not None and sup["pid"] == launcher.pid
        assert heal.supervisor_of(launcher.pid) is None        # pytest's own python is no launcher
    finally:
        for p in psutil.Process(launcher.pid).children(recursive=True):
            p.kill()
        launcher.kill()
