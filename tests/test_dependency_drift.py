"""Tests for src/dependency_drift.py (H2).

Real subprocess checks throughout: `_run_python_check` runs the actual
current interpreter (`sys.executable`) against real, synthetic
`.dist-info` directories placed on `PYTHONPATH` and read by the real
`importlib.metadata` — no fakes for the Python-installed check itself.
Only the LLM/system-note wiring is out of scope for a subprocess.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from src import dependency_drift as dd


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_dist_info(site_dir, name, version="1.0.0"):
    di = os.path.join(site_dir, f"{name}-{version}.dist-info")
    os.makedirs(di, exist_ok=True)
    with open(os.path.join(di, "METADATA"), "w", encoding="utf-8") as f:
        f.write(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    with open(os.path.join(di, "INSTALLER"), "w", encoding="utf-8") as f:
        f.write("pip\n")


@pytest.fixture
def site_env(tmp_path):
    """A synthetic site-packages dir with a couple of real dist-info
    records, wired onto PYTHONPATH so the real interpreter's
    importlib.metadata actually finds them."""
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    _make_dist_info(str(site_dir), "requests", "2.31.0")
    _make_dist_info(str(site_dir), "python-dotenv", "1.0.0")  # normalized name check
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site_dir)
    return str(site_dir), env


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Never touch the real DATA_DIR from tests."""
    monkeypatch.setattr(dd, "DATA_DIR", str(tmp_path / "data"))


# ---------------------------------------------------------------------------
# requirements*.txt parsing
# ---------------------------------------------------------------------------

def test_parse_requirements_basic(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "fastapi\n"
        "# a comment\n"
        "\n"
        "httpx>=1.0,<2.0\n"
        "pydantic==2.13.4\n"
        "-e ./local-pkg\n"
        "-r other.txt\n"
    )
    names = dd.parse_requirements_file(str(req))
    assert names == ["fastapi", "httpx", "pydantic"]


def test_requirements_marker_ignored_off_windows(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "somepkg; sys_platform == 'win32'\n"
        "alwayspkg\n"
    )
    names = dd.parse_requirements_file(str(req))
    if sys.platform == "win32":
        assert "somepkg" in names
    else:
        assert "somepkg" not in names
    assert "alwayspkg" in names


def test_requirements_marker_fallback_matches_packaging(tmp_path, monkeypatch):
    """Force the minimal fallback marker evaluator and confirm it agrees
    with the real environment for a win32-only marker."""
    monkeypatch.setattr(dd, "_HAS_PACKAGING", False)
    req = tmp_path / "requirements.txt"
    req.write_text("winpkg; sys_platform == 'win32'\nposixpkg; sys_platform != 'win32'\n")
    names = dd.parse_requirements_file(str(req))
    if sys.platform == "win32":
        assert names == ["winpkg"]
    else:
        assert names == ["posixpkg"]


def test_find_requirements_files_multiple(tmp_path):
    (tmp_path / "requirements.txt").write_text("a\n")
    (tmp_path / "requirements-dev.txt").write_text("b\n")
    (tmp_path / "requirements-voice.txt").write_text("c\n")
    (tmp_path / "not_requirements.txt").write_text("z\n")
    found = dd.find_requirements_files(str(tmp_path))
    names = [os.path.basename(p) for p in found]
    assert set(names) == {"requirements.txt", "requirements-dev.txt", "requirements-voice.txt"}


# ---------------------------------------------------------------------------
# pyproject.toml parsing
# ---------------------------------------------------------------------------

def test_parse_pyproject_dependencies(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1"\n'
        'dependencies = ["h2fakepkg-shapely>=2.0", "h2fakepkg-jsonschema"]\n'
    )
    names = dd.parse_pyproject_dependencies(str(tmp_path))
    assert names == ["h2fakepkg-shapely", "h2fakepkg-jsonschema"]


def test_parse_pyproject_missing_file(tmp_path):
    assert dd.parse_pyproject_dependencies(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# package.json parsing + node_modules check
# ---------------------------------------------------------------------------

def test_declared_node_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^18.0.0", "@scope/pkg": "1.0.0"},
        "devDependencies": {"vite": "^5.0.0"},
    }))
    names = set(dd.declared_node_dependencies(str(tmp_path)))
    assert names == {"react", "@scope/pkg", "vite"}


def test_missing_node_dependencies(tmp_path):
    node_modules = tmp_path / "node_modules"
    (node_modules / "react").mkdir(parents=True)
    (node_modules / "react" / "package.json").write_text("{}")
    (node_modules / "@scope" / "pkg").mkdir(parents=True)
    (node_modules / "@scope" / "pkg" / "package.json").write_text("{}")
    missing = dd.missing_node_dependencies(str(tmp_path), ["react", "@scope/pkg", "vite"])
    assert missing == ["vite"]


# ---------------------------------------------------------------------------
# Python installed-check (real subprocess + real importlib.metadata)
# ---------------------------------------------------------------------------

def test_run_python_check_real_subprocess(tmp_path, site_env):
    site_dir, env = site_env
    missing = dd._run_python_check(
        sys.executable, ["requests", "python-dotenv", "h2fakepkg-shapely", "h2fakepkg-jsonschema"],
        cwd=str(tmp_path), env=env,
    )
    assert set(missing) == {"h2fakepkg-shapely", "h2fakepkg-jsonschema"}


def test_run_python_check_empty_list_short_circuits(tmp_path):
    assert dd._run_python_check(sys.executable, [], cwd=str(tmp_path)) == []


def test_run_python_check_bad_interpreter_fails_open(tmp_path):
    missing = dd._run_python_check("/no/such/python-xyz", ["h2fakepkg-shapely"], cwd=str(tmp_path), timeout=5)
    assert missing == ["h2fakepkg-shapely"]


# ---------------------------------------------------------------------------
# check_drift end-to-end + cache
# ---------------------------------------------------------------------------

def _write_project(tmp_path, requirements="h2fakepkg-shapely\nh2fakepkg-jsonschema\n"):
    (tmp_path / "requirements.txt").write_text(requirements)
    return tmp_path


def test_check_drift_reports_missing_python(tmp_path, site_env):
    site_dir, env = site_env
    _write_project(tmp_path)
    report = dd.check_drift(str(tmp_path), python_exe=sys.executable, env=env)
    assert not report.ok
    assert set(report.missing_python) == {"h2fakepkg-shapely", "h2fakepkg-jsonschema"}
    assert report.missing_node == ()
    assert report.from_cache is False


def test_check_drift_cache_hit_on_second_call(tmp_path, site_env):
    site_dir, env = site_env
    _write_project(tmp_path)
    first = dd.check_drift(str(tmp_path), python_exe=sys.executable, env=env)
    assert first.from_cache is False
    second = dd.check_drift(str(tmp_path), python_exe=sys.executable, env=env)
    assert second.from_cache is True
    assert second.missing_python == first.missing_python


def test_check_drift_cache_invalidated_on_requirements_change(tmp_path, site_env):
    site_dir, env = site_env
    _write_project(tmp_path, requirements="h2fakepkg-shapely\n")
    first = dd.check_drift(str(tmp_path), python_exe=sys.executable, env=env)
    assert set(first.missing_python) == {"h2fakepkg-shapely"}

    # Change requirements.txt: cache must miss and re-check.
    (tmp_path / "requirements.txt").write_text("h2fakepkg-shapely\nh2fakepkg-jsonschema\n")
    second = dd.check_drift(str(tmp_path), python_exe=sys.executable, env=env)
    assert second.from_cache is False
    assert set(second.missing_python) == {"h2fakepkg-shapely", "h2fakepkg-jsonschema"}


def test_check_drift_package_json_missing_dep(tmp_path, site_env):
    site_dir, env = site_env
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"left-pad": "1.0.0"}}))
    report = dd.check_drift(str(tmp_path), python_exe=sys.executable, env=env)
    assert "left-pad" in report.missing_node


def test_check_drift_no_deps_files_is_ok(tmp_path):
    report = dd.check_drift(str(tmp_path))
    assert report.ok
    assert report is dd._EMPTY_REPORT


def test_check_drift_missing_project_root(tmp_path):
    report = dd.check_drift(str(tmp_path / "does-not-exist"))
    assert report.ok


def test_clear_cache(tmp_path, site_env):
    site_dir, env = site_env
    _write_project(tmp_path, requirements="h2fakepkg-shapely\n")
    dd.check_drift(str(tmp_path), python_exe=sys.executable, env=env)
    assert os.path.isfile(dd._cache_path(str(tmp_path)))
    dd.clear_cache(str(tmp_path))
    assert not os.path.isfile(dd._cache_path(str(tmp_path)))


# ---------------------------------------------------------------------------
# system_note
# ---------------------------------------------------------------------------

def test_system_note_empty_when_ok():
    assert dd.system_note(dd.DriftReport()) == ""


def test_system_note_english():
    report = dd.DriftReport(missing_python=("h2fakepkg-shapely",), missing_node=("left-pad",))
    note = dd.system_note(report, "en")
    assert "h2fakepkg-shapely" in note
    assert "left-pad" in note
    assert "install_dependencies" in note


def test_system_note_spanish():
    report = dd.DriftReport(missing_python=("h2fakepkg-jsonschema",))
    note = dd.system_note(report, "es")
    assert "h2fakepkg-jsonschema" in note
    assert "install_dependencies" in note
    assert "Faltan" in note


# ---------------------------------------------------------------------------
# auto_install — always via EXEC-05, never a bare pip/npm call
# ---------------------------------------------------------------------------

def test_auto_install_noop_when_report_ok():
    result = asyncio.run(dd.auto_install(dd.DriftReport(), "/tmp/whatever"))
    assert result.attempted is False
    assert result.ok is True


def test_auto_install_skipped_when_setting_off(tmp_path):
    _write_project(tmp_path, requirements="h2fakepkg-shapely\n")
    report = dd.DriftReport(missing_python=("h2fakepkg-shapely",))
    result = asyncio.run(dd.auto_install(report, str(tmp_path), enabled=False))
    assert result.attempted is False
    assert "off" in result.reason


def test_auto_install_uses_exec05_plan_and_never_calls_pip_directly(tmp_path, monkeypatch):
    """The install must go through execute_dependency_install (EXEC-05) with
    an injected runner — never subprocess/pip called directly from this
    module."""
    _write_project(tmp_path, requirements="h2fakepkg-shapely\n")

    from src import tool_execution as te

    calls = []

    async def fake_runner(command, cwd):
        calls.append((command, cwd))
        return 0, "installed", ""

    monkeypatch.setattr(te, "_default_runner", fake_runner)
    te.reset_install_approvals()

    async def run():
        report = dd.DriftReport(missing_python=("h2fakepkg-shapely",))

        # Patch execute_dependency_install to use our fake runner via a thin
        # wrapper, since dependency_drift imports it fresh inside auto_install.
        orig = te.execute_dependency_install

        async def wrapped(plan, *, approved=False, owner="", runner=fake_runner):
            return await orig(plan, approved=approved, owner=owner, runner=runner)

        monkeypatch.setattr(te, "execute_dependency_install", wrapped)
        return await dd.auto_install(report, str(tmp_path), owner="test", enabled=True)

    result = asyncio.run(run())
    assert result.attempted is True
    assert result.ok is True
    assert len(calls) == 1
    command, cwd = calls[0]
    assert "h2fakepkg-shapely" in command
    assert command[0] != "pip"  # goes through python -m pip / venv pip, not a bare "pip" shell call
