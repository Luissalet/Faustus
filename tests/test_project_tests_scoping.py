"""Regression tests for src/project_tests.py: which interpreter the pytest
runner is built with (never the frozen Faustus.exe) and how a missing pytest
is reported (inconclusive, not "your change broke the tests")."""

import os
import shutil
import sys

import pytest

from src import project_tests as pt


@pytest.fixture
def pyws(tmp_path):
    """A minimal workspace pytest detection recognises (tests/ with a test)."""
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_ok.py").write_bytes(b"def test_ok():\n    assert True\n")
    return ws


def test_detect_test_command_uses_the_host_interpreter(pyws):
    spec = pt.detect_test_command(str(pyws))
    assert spec["kind"] == "pytest"
    assert spec["argv"][0] == sys.executable and spec["argv"][1:3] == ["-m", "pytest"]


def test_detect_test_command_never_runs_the_frozen_executable(pyws, monkeypatch):
    """In the PyInstaller build `sys.executable` is dist\\Faustus\\Faustus.exe,
    which ignores `-m pytest` and boots a whole second copy of the app
    (splash + tray + another server on 7000) instead of running the tests."""
    frozen = os.path.join("C:\\", "Faustus", "Faustus.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", frozen, raising=False)
    spec = pt.detect_test_command(str(pyws))
    assert spec is not None and spec["kind"] == "pytest"
    assert frozen not in (spec.get("argv") or []), spec
    assert spec.get("python") != frozen
    # A real interpreter is on PATH here, so *one of them* is used. Which one
    # is not the point and must not be pinned: on Windows `python3` is often
    # the Microsoft Store stub in WindowsApps while `python` is the real
    # install, so pinning an order asserts the worse choice.
    candidates = [c for c in (shutil.which("python3"), shutil.which("python")) if c]
    if candidates:
        assert spec["argv"][0] in candidates, (spec["argv"][0], candidates)
        assert spec["argv"][1:3] == ["-m", "pytest"]


def test_frozen_build_without_any_interpreter_is_inconclusive(pyws, monkeypatch):
    """No real python anywhere → "could not run", never a test failure."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", os.path.join("C:\\", "Faustus", "Faustus.exe"), raising=False)
    monkeypatch.setattr(pt.shutil, "which", lambda *_a, **_k: None)
    spec = pt.detect_test_command(str(pyws))
    assert spec is not None and not spec.get("argv")
    res = pt.run_tests(str(pyws), spec)
    assert res["inconclusive"] is True
    assert res["ok"] is not False and res["ran"] is False
    assert "interpreter" in (res["summary"] or "").lower()


def test_project_venv_still_wins_over_the_host_interpreter(pyws, monkeypatch):
    venv_py = pyws / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    spec = pt.detect_test_command(str(pyws))
    assert spec["argv"][0] == str(venv_py) and spec["note"] == "project venv"


def test_missing_pytest_is_inconclusive_not_a_broken_change():
    """`python -m pytest` without pytest exits 1 with a message that never
    contains the word "error", so the run was scored as "your changes broke the
    tests" and the agent burned its fix round on failures that do not exist."""
    out = "/home/u/proj/.venv/bin/python: No module named pytest\n"
    res = pt.parse_output("pytest", 1, out)
    assert res["inconclusive"] is True
    assert "pytest is not installed" in res["summary"]
    assert res["failures"] == []

    win = "C:\\proj\\.venv\\Scripts\\python.exe: No module named pytest\n"
    assert pt.parse_output("pytest", 1, win)["inconclusive"] is True

    tb = ("Traceback (most recent call last):\n"
          '  File "<frozen runpy>", line 189, in _run_module_as_main\n'
          "ModuleNotFoundError: No module named 'pytest'\n")
    res_tb = pt.parse_output("pytest", 1, tb)
    assert res_tb["inconclusive"] is True
    assert "pytest is not installed" in res_tb["summary"]

    # A real failing suite is still a real failure.
    real = pt.parse_output("pytest", 1, "FAILED tests/test_a.py::test_x - AssertionError\n= 1 failed in 0.1s =")
    assert real["inconclusive"] is False and real["ok"] is False
