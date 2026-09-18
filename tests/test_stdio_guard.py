"""StdioProtectionWrapper (src/stdio_guard.py) — a stray print() must not
corrupt an MCP stdio stream.

The built-in servers (mcp_servers/*.py) share their process with app code:
src.memory, src.rag_manager, the email stack. stdout there is the JSON-RPC
stream, and one print() in any of that code — or in a library it imports —
makes the client fail to parse a message and the session dies. The guard swaps
sys.stdout for a proxy that writes to stderr while the session runs, and puts
the real object back afterwards, however the block ends.

`agent_mcp_stdio_guard` off = stdout is left exactly where it was.
"""
from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path

import pytest

from src import stdio_guard

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean_guard():
    yield
    stdio_guard.reset_for_tests()


@pytest.fixture
def settings(monkeypatch):
    import src.settings as settings_mod
    values = {}
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda key, default=None: values[key] if key in values else real(key, default))
    return values


def _streams(monkeypatch):
    """A fake protocol stream (stdout) and log stream (stderr).

    Called from the test BODY, never from a fixture: pytest's own capture
    re-installs sys.stdout between setup and the call phase, so a swap made in
    a fixture would be undone before the test runs.
    """
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def test_a_print_lands_on_stderr_and_the_protocol_stream_stays_clean(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = True
    out, err = _streams(monkeypatch)
    protocol = sys.stdout                     # what stdio_server() captured on entry
    with stdio_guard.guard() as up:
        assert up is True and stdio_guard.active() is True
        print("a debug line nobody meant to send")
        sys.stdout.write("another one")
        protocol.write('{"jsonrpc":"2.0","id":1}\n')      # the server writes to its own handle
    assert stdio_guard.active() is False
    assert out.getvalue() == '{"jsonrpc":"2.0","id":1}\n'
    assert "a debug line nobody meant to send" in err.getvalue()
    assert "another one" in err.getvalue()
    # restored: a print after the session goes back to stdout
    print("after")
    assert "after" in out.getvalue()


def test_nested_activations_only_restore_at_the_outermost(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = True
    out, _ = _streams(monkeypatch)
    real = sys.stdout
    with stdio_guard.guard():
        assert stdio_guard.depth() == 1
        with stdio_guard.guard():
            assert stdio_guard.depth() == 2 and sys.stdout is not real
            print("inner")
        assert stdio_guard.depth() == 1, "the inner exit must not restore stdout"
        assert sys.stdout is not real
        print("outer")
    assert stdio_guard.depth() == 0 and sys.stdout is real
    assert out.getvalue() == ""


def test_an_exception_inside_the_block_still_restores(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = True
    _streams(monkeypatch)
    real = sys.stdout
    with pytest.raises(ValueError):
        with stdio_guard.guard():
            print("before the boom")
            raise ValueError("boom")
    assert sys.stdout is real and stdio_guard.active() is False


def test_with_the_setting_off_nothing_is_swapped(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = False
    out, err = _streams(monkeypatch)
    real = sys.stdout
    with stdio_guard.guard() as up:
        assert up is False and sys.stdout is real and stdio_guard.active() is False
        print("straight to stdout, exactly as before")
    assert "straight to stdout" in out.getvalue() and err.getvalue() == ""


def test_force_activates_even_with_the_setting_off(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = False
    out, err = _streams(monkeypatch)
    with stdio_guard.guard(force=True):
        print("forced")
    assert out.getvalue() == "" and "forced" in err.getvalue()


def test_the_proxy_behaves_like_a_text_stream(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = True
    out, err = _streams(monkeypatch)
    with stdio_guard.guard():
        proxy = sys.stdout
        proxy.writelines(["a", "b"])
        proxy.flush()
        assert proxy.writable() is True and proxy.readable() is False and proxy.seekable() is False
        assert proxy.isatty() is False and proxy.closed is False
        assert isinstance(proxy.encoding, str)
        proxy.close()                     # must NOT close the process's stderr
        assert err.closed is False
        assert stdio_guard.original_stdout() is out
    assert err.getvalue() == "ab"


def test_a_dead_stderr_swallows_the_write_instead_of_raising(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = True
    out = io.StringIO()
    dead = io.StringIO()
    dead.close()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", dead)
    with stdio_guard.guard():
        print("nowhere to go")            # ValueError: I/O operation on closed file — swallowed
        sys.stdout.flush()
    assert out.getvalue() == ""


def test_it_is_thread_safe(settings, monkeypatch):
    """Threads activating and deactivating concurrently leave the counter at
    zero and stdout restored."""
    settings["agent_mcp_stdio_guard"] = True
    _streams(monkeypatch)
    real = sys.stdout
    errors = []

    def worker():
        try:
            for _ in range(50):
                with stdio_guard.guard():
                    print("x")
        except Exception as e:              # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert stdio_guard.depth() == 0 and sys.stdout is real


def test_no_stdout_at_all_is_survivable(settings, monkeypatch):
    settings["agent_mcp_stdio_guard"] = True
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    with stdio_guard.guard():
        sys.stdout.write("into the void")
        sys.stdout.flush()
        assert sys.stdout.isatty() is False
        with pytest.raises(OSError):
            sys.stdout.fileno()
    assert sys.stdout is None


# ── the servers that speak stdio raise it ───────────────────────────────────

@pytest.mark.parametrize("name", ["memory_server.py", "rag_server.py", "image_gen_server.py", "email_server.py"])
def test_every_built_in_stdio_server_guards_its_session(name):
    """The guard must go up INSIDE `stdio_server()`: that context manager wraps
    the real sys.stdout.buffer on entry, so the protocol keeps the handle."""
    source = (REPO / "mcp_servers" / name).read_text(encoding="utf-8")
    assert "from src.stdio_guard import guard as stdout_guard" in source
    body = source[source.index("async def run("):]
    stdio_at = body.index("async with stdio_server()")
    guard_at = body.index("with stdout_guard():")
    run_at = body.index("await server.run(")
    assert stdio_at < guard_at < run_at, f"{name}: the guard is not inside the stdio session"


_STRAY_PRINT_SERVER = '''
import asyncio, sys
sys.path.insert(0, {root!r})
import mcp_servers.memory_server as m

_real_run = m.server.run


async def run(*a, **kw):
    # app code printing on stdout in the middle of a live session
    print("STRAY PRINT FROM APP CODE")
    sys.stdout.write("and a bare write\\n")
    return await _real_run(*a, **kw)


m.server.run = run
asyncio.run(m.run())
'''


@pytest.mark.slow
def test_a_real_stdio_session_survives_a_stray_print(tmp_path):
    """The whole point, end to end: a built-in server answers `initialize`
    while app code in the same process prints to stdout. The protocol stream
    holds exactly one JSON line; the print is on stderr."""
    import json
    import subprocess
    pytest.importorskip("mcp")
    script = tmp_path / "server_with_a_stray_print.py"
    script.write_text(_STRAY_PRINT_SERVER.format(root=str(REPO)), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(REPO), "ODYSSEUS_DATA_DIR": str(tmp_path / "data")}
    proc = subprocess.Popen([sys.executable, str(script)], cwd=str(REPO), env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                          "clientInfo": {"name": "t", "version": "1"}}}
    try:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
    finally:
        proc.kill()
        stderr = proc.stderr.read()
    answer = json.loads(line)                       # would raise on a corrupted stream
    assert answer["id"] == 1 and answer["result"]["serverInfo"]["name"] == "memory"
    assert "STRAY PRINT FROM APP CODE" in stderr and "and a bare write" in stderr
