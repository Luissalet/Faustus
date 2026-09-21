"""20-09-2026: a turn that only wrote data files ran a stranger's suite.

The workspace was a folder of Pokemon silhouettes that happens to contain a
`tests/` directory belonging to a Blender add-on. The turn wrote `*.json`
files; nothing mapped them to a test, and `run_tests` then fell through to
`pytest -x -q` over the whole folder — minutes of someone else's suite to
"verify" changes it does not cover. With `scope="related"`, changes with no
related test now verify nothing, which is the honest answer.
"""
import json
import os

from src import project_tests


def _workspace(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_blender_smoke.py").write_text(
        "def test_smoke():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "1-3").mkdir()
    (tmp_path / "1-3" / "listing.json").write_text(json.dumps({"title": "x"}), encoding="utf-8")
    return str(tmp_path)


def test_data_only_changes_do_not_run_the_folders_own_suite(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    spec = project_tests.detect_test_command(workspace)
    assert spec and spec["kind"] == "pytest"

    def _never(*args, **kwargs):  # pragma: no cover - the point of the test
        raise AssertionError("the suite must not be spawned")

    monkeypatch.setattr(project_tests.subprocess, "Popen", _never)
    res = project_tests.run_tests(workspace, spec, changed=["1-3/listing.json"], scope="related")
    assert res["ran"] is False
    assert res["ok"] is True
    assert res["scope"] == "related"
    assert "no test covers" in res["summary"]


def test_scope_all_still_runs_everything(tmp_path):
    workspace = _workspace(tmp_path)
    spec = project_tests.detect_test_command(workspace)
    res = project_tests.run_tests(workspace, spec, changed=["1-3/listing.json"], scope="all")
    # Either it really ran the suite, or the sandbox had no interpreter for it;
    # what must NOT happen is the "related" short-circuit above.
    assert res["ran"] is True or res["inconclusive"] is True
    assert "no test covers" not in (res["summary"] or "")


def test_a_changed_test_file_is_still_run(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    spec = project_tests.detect_test_command(workspace)
    seen = {}

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return ("1 passed", "")

        def poll(self):
            return 0

        def kill(self):
            pass

    def _popen(argv, **kwargs):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(project_tests.subprocess, "Popen", _popen)
    res = project_tests.run_tests(
        workspace, spec, changed=[os.path.join("tests", "test_blender_smoke.py")], scope="related"
    )
    assert res["ran"] is True
    assert any("test_blender_smoke.py" in str(a) for a in seen.get("argv", []))
