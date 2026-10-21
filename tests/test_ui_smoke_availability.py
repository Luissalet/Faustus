"""Chromium availability for the UI smoke pass (#1103, product side of #1077).

The sync SDK context returns as soon as the driver calls back and, on exit, cancels
its still-pending init task: ROOT saw "Task was destroyed but it is pending!" and an
unretrieved TargetClosedError from that path alone, while the public async context
(which awaits the initialised Playwright object) exited clean on the same install.
So the availability question goes through `async with async_playwright()`, and the
public helper stays a plain sync bool for collection-time `skipif` and the
`asyncio.to_thread` worker. A thread that already runs a loop still gets the real
answer (asked from a short worker thread), never a provisional False.

No real SDK here: fake `playwright.async_api` / `playwright.sync_api` modules.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import types
import warnings

import pytest

from src import ui_smoke


class _FakeAsyncPlaywright:
    def __init__(self, path: str, calls: list, fail: bool = False):
        self._path, self._calls, self._fail = path, calls, fail

    async def __aenter__(self):
        self._calls.append(("enter", threading.get_ident()))
        if self._fail:
            raise RuntimeError("driver did not start")
        return types.SimpleNamespace(chromium=types.SimpleNamespace(executable_path=self._path))

    async def __aexit__(self, *exc):
        self._calls.append(("exit", threading.get_ident()))
        return False


def _install_fakes(monkeypatch, path: str, *, fail: bool = False) -> list:
    calls: list = []
    fake_async = types.ModuleType("playwright.async_api")
    fake_async.async_playwright = lambda: _FakeAsyncPlaywright(path, calls, fail)
    fake_sync = types.ModuleType("playwright.sync_api")

    def _sync_playwright():
        raise AssertionError("availability must not use the sync SDK context")

    fake_sync.sync_playwright = _sync_playwright
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    monkeypatch.setattr(ui_smoke, "_CHROMIUM_INSTALLED", None)
    monkeypatch.setattr(ui_smoke, "_setting", lambda key, default=None: default)
    return calls


def test_cold_call_uses_the_async_context_and_caches_true(monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    calls = _install_fakes(monkeypatch, str(exe))
    assert ui_smoke.playwright_available() is True
    assert [c[0] for c in calls] == ["enter", "exit"]
    assert ui_smoke._CHROMIUM_INSTALLED is True
    assert ui_smoke.playwright_available() is True
    assert len(calls) == 2  # cached: no second SDK start


def test_missing_executable_is_false_and_cached(monkeypatch, tmp_path):
    calls = _install_fakes(monkeypatch, str(tmp_path / "absent.exe"))
    assert ui_smoke.playwright_available() is False
    assert ui_smoke._CHROMIUM_INSTALLED is False
    (tmp_path / "absent.exe").write_bytes(b"")
    assert ui_smoke.playwright_available() is False  # cached False is not re-asked
    assert len(calls) == 2


def test_sdk_failure_is_false(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, str(tmp_path / "x.exe"), fail=True)
    assert ui_smoke.playwright_available() is False
    assert ui_smoke._CHROMIUM_INSTALLED is False


def test_setting_off_asks_nothing_and_keeps_the_cache(monkeypatch, tmp_path):
    calls = _install_fakes(monkeypatch, str(tmp_path / "x.exe"))
    monkeypatch.setattr(ui_smoke, "_setting", lambda key, default=None: False)
    assert ui_smoke.playwright_available() is False
    assert calls == [] and ui_smoke._CHROMIUM_INSTALLED is None


def test_cold_call_inside_a_running_loop_gets_the_real_answer(monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    calls = _install_fakes(monkeypatch, str(exe))

    async def caller():
        return ui_smoke.playwright_available(), threading.get_ident()

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a never-awaited coroutine would fail here
        answer, loop_thread = asyncio.run(caller())
    assert answer is True
    assert ui_smoke._CHROMIUM_INSTALLED is True
    assert [c[0] for c in calls] == ["enter", "exit"]
    assert all(thread != loop_thread for _, thread in calls)  # asked off the busy loop


def test_answer_is_an_exact_bool_for_skipif(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, str(tmp_path / "absent.exe"))
    value = ui_smoke.playwright_available()
    assert type(value) is bool and value is False
