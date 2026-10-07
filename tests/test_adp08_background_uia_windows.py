"""Physical Windows proof for explicit non-active UIA ValuePattern."""
import asyncio
import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes

import psutil
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows UI Automation")


def _foreground_cursor():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL
    point = POINT()
    assert user32.GetCursorPos(ctypes.byref(point))
    return int(user32.GetForegroundWindow()), (int(point.x), int(point.y))


def test_targeted_snapshot_and_set_value_keep_real_foreground_and_cursor(monkeypatch, tmp_path):
    pytest.importorskip("pywinauto", reason="optional Windows UIA backend dependency")
    from src.desktop_semantics import reset_state
    from src.agent_tools import desktop_semantic_tools as tools
    from src.agent_tools.desktop_tools import WindowsBackend

    before = _foreground_cursor()
    helper = os.path.join(os.path.dirname(__file__), "fixtures", "uia_background_windows.py")
    proc = subprocess.Popen([sys.executable, helper], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    launcher_create_time = psutil.Process(proc.pid).create_time()
    fixture_pid = None
    fixture_create_time = None
    payload = None
    try:
        payload = json.loads(proc.stdout.readline())
        pid = int(payload["pid"])
        fixture_pid = pid
        fixture_process = psutil.Process(pid)
        fixture_create_time = fixture_process.create_time()
        assert os.path.normcase(helper) in os.path.normcase(" ".join(fixture_process.cmdline()))
        assert _foreground_cursor() == before, "fixture creation changed foreground or cursor"

        # Build the real backend without its constructor's process-wide DPI
        # awareness call; the fixture must not change the test desktop setup.
        backend = object.__new__(WindowsBackend)
        backend.ctypes = ctypes
        backend.wintypes = wintypes
        backend.user32 = ctypes.WinDLL("user32", use_last_error=True)
        listed = {item["title"]: item for item in backend.list_windows()
                  if item["title"].startswith("Faustus UIA fixture ")}
        assert set(listed) == {"Faustus UIA fixture target", "Faustus UIA fixture bystander"}
        target = {key: listed["Faustus UIA fixture target"][key]
                  for key in ("hwnd", "pid", "create_time")}
        bystander = {key: listed["Faustus UIA fixture bystander"][key]
                     for key in ("hwnd", "pid", "create_time")}
        assert target["pid"] == pid and bystander["pid"] == pid
        assert target["hwnd"] != before[0] and bystander["hwnd"] != before[0]
        from src import desktop_control_session
        monkeypatch.setattr(desktop_control_session, "RUNTIME", tmp_path)
        monkeypatch.setattr(tools, "get_backend", lambda: backend)
        monkeypatch.setattr(tools, "desktop_control_mode", lambda: "on")
        reset_state()
        ctx = {"session_id": "background-uia-fixture"}
        results = {}

        @desktop_control_session.desktop_control_run
        async def run_tool_flow():
            _, snapshot = await tools.DesktopSnapshotTool().execute({"target_window": target}, ctx)
            results["snapshot"] = snapshot
            if snapshot["exit_code"] == 0:
                _, found = await tools.DesktopFindTool().execute({"query": "edit", "role": "Edit"}, ctx)
                results["found"] = found
                edit = next(e for e in snapshot["elements"] if e["value"] == "initial target")
                _, action = await tools.DesktopActTool().execute(
                    {"ref": edit["ref"], "op": "set_value", "value": "background changed"}, ctx)
                results["action"] = action
                _, fresh = await tools.DesktopSnapshotTool().execute({"target_window": target}, ctx)
                locked = next(e for e in fresh["elements"] if e["value"] == "locked text")
                _, refused = await tools.DesktopActTool().execute(
                    {"ref": locked["ref"], "op": "set_value", "value": "must stay locked"}, ctx)
                results["refused"] = refused
            yield "desktop action checks complete"

        async def ack_synthetic_desktop():
            stop = asyncio.Event()
            async def acknowledge():
                last = None
                while not stop.is_set():
                    state = desktop_control_session._read("desktop-control.json")
                    token = state.get("token")
                    if token and token != last:
                        desktop_control_session._write("desktop-control-ack.json", {
                            "token": token, "ready": True, "displays": 1,
                        })
                        last = token
                    await asyncio.sleep(0.01)
            ack_task = asyncio.create_task(acknowledge())
            try:
                async for _ in run_tool_flow():
                    pass
            finally:
                stop.set()
                await ack_task
        asyncio.run(ack_synthetic_desktop())
        snapshot = results["snapshot"]
        assert snapshot["exit_code"] == 0, snapshot
        assert snapshot["target_window"] == target
        assert _foreground_cursor() == before
        found = results["found"]
        assert found["exit_code"] == 0 and found["elements"]
        action = results["action"]
        assert action["exit_code"] == 0, action
        assert action["delivery"] == "delivered"
        assert action["verified"] is True, action
        assert action["observed_after"]["value"] == "background changed"
        assert action["observed_after"]["foreground_unchanged"] is True
        assert action["observed_after"]["cursor_unchanged"] is True
        assert _foreground_cursor() == before

        refused = results["refused"]
        assert refused["delivery"] == "not_delivered", refused
        assert refused["verified"] is False
        after_refusal = backend.semantic().snapshot(session_id=ctx["session_id"], target_window=target)
        assert any(e["value"] == "locked text" for e in after_refusal["elements"])

        bystander_snapshot = backend.semantic().snapshot(session_id=ctx["session_id"],
                                                   target_window=bystander)
        bystander_edit = next(e for e in bystander_snapshot["elements"]
                              if e["role"].lower() == "edit")
        assert bystander_edit["value"] == "untouched bystander"

        from src.desktop_semantics import act
        with pytest.raises(Exception, match="only set_value"):
            act(session_id=ctx["session_id"], ref=found["elements"][0]["ref"],
                op="invoke", backend=backend)
        stale = dict(target, create_time=target["create_time"] - 10.0)
        with pytest.raises(RuntimeError, match="stale or no longer matches"):
            backend.semantic().snapshot(session_id=ctx["session_id"], target_window=stale)
        assert _foreground_cursor() == before
    finally:
        try:
            if fixture_pid is not None and payload:
                process = psutil.Process(fixture_pid)
                if process.pid == int(payload["pid"]) and abs(process.create_time() - fixture_create_time) < 0.01:
                    user32 = ctypes.WinDLL("user32", use_last_error=True)
                    for item in payload["windows"]:
                        user32.PostMessageW(int(item["hwnd"]), 0x0010, 0, 0)
                    proc.wait(timeout=5)
        finally:
            if fixture_pid is not None and psutil.pid_exists(fixture_pid):
                process = psutil.Process(fixture_pid)
                if abs(process.create_time() - fixture_create_time) < 0.01:
                    process.kill()
                    process.wait(timeout=5)
            if proc.poll() is None:
                launcher = psutil.Process(proc.pid)
                if abs(launcher.create_time() - launcher_create_time) < 0.01:
                    proc.wait(timeout=5)
