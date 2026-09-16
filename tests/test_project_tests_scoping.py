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


def test_detect_test_command_uses_the_same_interpreter_as_the_python_tool(pyws):
    """A workspace with no venv must run pytest with the same interpreter the
    `python` tool would pick — not Faustus's own venv. Seen live: jsonschema
    was installed on the host Python, missing in ours, and collection failed."""
    from src.agent_tools.subprocess_tools import project_python
    from src.native_env import native_host_environment
    spec = pt.detect_test_command(str(pyws))
    expected = project_python(str(pyws), native_host_environment())
    assert spec["kind"] == "pytest"
    assert spec["argv"][0] == expected and spec["argv"][1:3] == ["-m", "pytest"]
    assert spec["note"] == "host python"


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
    import src.agent_tools.subprocess_tools as st
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", os.path.join("C:\\", "Faustus", "Faustus.exe"), raising=False)
    monkeypatch.setattr(pt.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(st.shutil, "which", lambda *_a, **_k: None)
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


def test_test_files_from_failures_feeds_full_suite_baseline():
    """A full-suite run has no related_files; the files that failed are what
    the checkpoint re-run must cover, or a pre-existing star IoU is billed
    as a new failure every turn."""
    assert pt._test_files_from_failures([
        "tests/test_pipeline.py::test_real_end_to_end[star] — AssertionError",
        "tests/test_pipeline.py::test_other",
        "tests/editor/test_models.py",
    ]) == ["tests/test_pipeline.py", "tests/editor/test_models.py"]
    res = {
        "ran": True, "ok": False,
        "failures": ["tests/test_pipeline.py::test_star — IoU 0.97"],
    }
    out = pt.compare_with_baseline(".", None, {"kind": "pytest"}, dict(res))
    assert out.get("pre_existing_only") is not True
    assert out.get("new_failures") == res["failures"]


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_js_editor_change_runs_node_tests_not_unrelated_pytest(tmp_path, monkeypatch):
    """Silhouettes d20e933f stamped verified on pytest test_import/jobs/models/store
    (82 passed) while the actual gate was `node --test tests/editor/test_gestures.mjs`.
    A JS-only mutation must run the node tests; pytest related files must stay .py."""
    ws = tmp_path / "ws"
    (ws / "tests" / "editor").mkdir(parents=True)
    (ws / "static" / "editor").mkdir(parents=True)
    (ws / "tests" / "test_store.py").write_text(
        "def test_store():\n    assert True\n", encoding="utf-8",
    )
    (ws / "static" / "editor" / "interactions2d.js").write_text(
        "export function drag() { return 1; }\n", encoding="utf-8",
    )
    (ws / "tests" / "editor" / "test_gestures.mjs").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { drag } from '../../static/editor/interactions2d.js';\n"
        "test('drag commits once', () => { assert.equal(drag(), 1); });\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pt, "_setting", lambda key, default=None: {
        "agent_project_tests": True,
        "agent_project_tests_scope": "related",
        "agent_project_test_command": "",
        "agent_project_tests_timeout_seconds": 60,
        "agent_project_tests_baseline": False,
    }.get(key, default), raising=False)

    changed = ["static/editor/interactions2d.js", "tests/editor/test_gestures.mjs",
               "silhouettes/editor/api.py"]
    (ws / "silhouettes" / "editor").mkdir(parents=True)
    (ws / "silhouettes" / "editor" / "api.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "tests" / "test_import.py").write_text(
        "def test_import():\n    assert True\n", encoding="utf-8",
    )

    res = pt.run_for_turn(str(ws), changed)
    assert res is not None
    related = res.get("related_files") or []
    assert "tests/editor/test_gestures.mjs" in related
    assert "tests/test_import.py" not in related
    assert "tests/test_store.py" not in related
    assert res.get("ok") is True
    assert res.get("kind") == "node"
    cmd = res.get("command") or ""
    assert "--test" in cmd and "test_gestures.mjs" in cmd
