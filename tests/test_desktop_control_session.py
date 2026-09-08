import asyncio
import json

import pytest

from src import desktop_control_session as control


@pytest.mark.asyncio
async def test_escape_cancels_pending_model_and_cleans_indicator(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    closed = []

    @control.desktop_control_run
    async def run():
        try:
            await control.ensure_indicator()
            yield "ready"
            await asyncio.sleep(30)
            yield "must not execute"
        finally:
            closed.append(True)

    async def host():
        while not control._read("desktop-control.json").get("token"):
            await asyncio.sleep(0.01)
        token = control._read("desktop-control.json")["token"]
        control._write("desktop-control-ack.json", {"token": token, "ready": True})
        await asyncio.sleep(0.2)
        control._write("desktop-cancel.json", {"token": token})

    host_task = asyncio.create_task(host())
    result = [chunk async for chunk in run()]
    await host_task
    assert result[0] == "ready"
    assert "detenido con Esc" in "".join(result)
    assert "must not execute" not in result
    assert closed == [True]
    assert control._read("desktop-control.json") == {}


@pytest.mark.asyncio
async def test_control_requires_host_and_rejects_failed_escape_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    with pytest.raises(RuntimeError, match="active agent run"):
        await control.ensure_indicator()
    state = {"token": "test", "active": False}
    token = control._run.set(state)
    try:
        control._write("desktop-control-ack.json", {"token": "test", "ready": False, "error": "Escape unavailable"})
        with pytest.raises(RuntimeError, match="Escape unavailable"):
            await control.ensure_indicator()
    finally:
        control._run.reset(token)


@pytest.mark.asyncio
async def test_keyboard_refocuses_observed_target_after_approval(monkeypatch):
    from src.agent_tools import desktop_tools as dt
    calls = []

    class Backend(dt.WindowsBackend):
        def __init__(self):
            pass
        def available(self):
            return True, ""
        def focus_target(self, target):
            calls.append(("focus", target["handle"]))
        def key_combo(self, keys):
            calls.append(("keys", keys))

    async def ready():
        calls.append(("indicator",))

    monkeypatch.setattr(control, "ensure_indicator", ready)
    monkeypatch.setattr(dt, "get_backend", Backend)
    monkeypatch.setattr(dt, "_control_mode_guard", lambda _: None)
    monkeypatch.setattr(dt, "_focused_targets", {"test": {"handle": 123, "title": "Test editor"}})
    _, result = await dt.DesktopTool("desktop_key").execute('{"combo":"alt+f4"}', {"session_id": "test"})
    assert result["exit_code"] == 0
    assert calls == [("indicator",), ("focus", 123), ("keys", ["alt", "f4"])]
    calls.clear()
    _, result = await dt.DesktopTool("desktop_key").execute('{"combo":"alt+f4"}', {"session_id": "other"})
    assert result["exit_code"] == 1
    assert not any(call[0] == "keys" for call in calls)


def test_ambiguous_or_closed_window_never_receives_input():
    from src.agent_tools import desktop_tools as dt
    backend = object.__new__(dt.WindowsBackend)
    backend.list_windows = lambda: [{"title": "ChatGPT one", "handle": 1}, {"title": "ChatGPT two", "handle": 2}]
    with pytest.raises(dt.DesktopError, match="ambiguous"):
        backend.focus_window("ChatGPT")
    with pytest.raises(dt.DesktopError, match="changed or closed"):
        backend.focus_target({"title": "Old window", "handle": 1})
