"""Desktop-app discovery must not inspect unrelated process command lines."""
import os
import sys
import time
from types import SimpleNamespace

import pytest
from src import host_tools as ht


class Process:
    def __init__(self, name, *, exe="", cmdline=None, unavailable=False):
        self.info = {"name": name}
        self.details = {"exe": exe, "cmdline": cmdline or []}
        self.unavailable = unavailable
        self.reads = []

    def as_dict(self, attrs=None):
        self.reads.append(set(attrs or []))
        if self.unavailable:
            raise RuntimeError("Process exited or access denied")
        return {name: self.details[name] for name in attrs}


def install_processes(monkeypatch, processes):
    requested = []
    def iterate(attrs=None):
        requested.append(set(attrs or []))
        return iter(processes)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(process_iter=iterate))
    return requested


def test_non_candidates_never_read_executable_or_command(monkeypatch):
    processes = [Process("python.exe", unavailable=True), Process("node.exe"), Process("chrome.exe")]
    requested = install_processes(monkeypatch, processes)
    assert ht._app_process_running() is False
    assert not any(p.reads for p in processes)
    assert all(not attrs.intersection({"exe", "cmdline"}) for attrs in requested)


@pytest.mark.parametrize("name,own,expected", [
    ("Electron.exe", True, True), ("faustus.exe", True, True),
    ("electron.exe", False, False), ("Faustus.exe", False, False),
])
def test_only_this_apps_candidate_paths_are_accepted(monkeypatch, name, own, expected):
    root = os.path.dirname(os.path.dirname(os.path.abspath(ht.__file__)))
    location = os.path.join(root, "desktop") if own else os.path.join(os.path.dirname(root), "other-app")
    process = Process(name, exe=os.path.join(location, name), cmdline=[location])
    install_processes(monkeypatch, [process])
    assert ht._app_process_running() is expected
    assert process.reads == [{"exe", "cmdline"}]


def test_gone_candidate_does_not_hide_a_later_own_app(monkeypatch):
    root = os.path.dirname(os.path.dirname(os.path.abspath(ht.__file__)))
    gone = Process("electron.exe", unavailable=True)
    own = Process("faustus.exe", exe=os.path.join(root, "Faustus.exe"))
    install_processes(monkeypatch, [gone, own])
    assert ht._app_process_running() is True
    assert len(gone.reads) == len(own.reads) == 1


def test_unreadable_candidate_is_not_reported_as_running(monkeypatch):
    install_processes(monkeypatch, [Process("electron.exe", unavailable=True)])
    assert ht._app_process_running() is False


def test_slow_result_has_a_full_cache_ttl(monkeypatch):
    clock = [100.0]
    calls = []
    def probe():
        calls.append(clock[0])
        clock[0] += 6
        return False
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(ht, "_app_cache", [0.0, False])
    monkeypatch.setattr(ht, "_heartbeat_fresh", lambda now: False)
    monkeypatch.setattr(ht, "_app_process_running", probe)
    if ht.os.name != "nt":
        pytest.skip("Windows desktop-app cache")
    assert ht.desktop_app_running() is False
    assert ht.desktop_app_running() is False
    assert len(calls) == 1
    clock[0] += ht._APP_CHECK_TTL_S + 0.01
    assert ht.desktop_app_running() is False
    assert len(calls) == 2
