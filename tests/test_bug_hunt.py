"""tests/test_bug_hunt.py — src/bug_hunt.py (autonomous bug hunter) and
src/agent_tools/bug_hunt_tools.py / routes/bug_hunt_routes.py.

No network: every LLM call goes through `src.endpoint_resolver.resolve_endpoint`
and `src.llm_core.llm_call_async`, both monkeypatched here. Real pytest
subprocesses are used for `run_suite` (fast, isolated, deterministic).
"""
from __future__ import annotations

import json
import os

import pytest

from src import bug_hunt

SAMPLE_MODULE = '''"""Tiny sample module for the bug hunter's own tests."""


def divide(a, b):
    """Divide a by b. Bug: no guard for b == 0 or a/b being None."""
    return a / b


def safe_len(items):
    """Length of items, 0 for None. Handles the None case correctly."""
    if items is None:
        return 0
    return len(items)
'''


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mathy.py").write_text(SAMPLE_MODULE, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("bug_hunt_data")
    monkeypatch.setattr(bug_hunt, "_data_dir", lambda: str(data_dir))
    return data_dir


def _no_model(monkeypatch):
    """Force generate_tests/triage down the deterministic fallback/heuristic
    path: resolve_endpoint resolves nothing usable."""
    import src.endpoint_resolver as ep

    monkeypatch.setattr(ep, "resolve_endpoint", lambda *a, **k: (None, None, None))


# ---------------------------------------------------------------------------
# plan_targets
# ---------------------------------------------------------------------------

def test_plan_targets_enumerates_functions_in_a_file(workspace):
    targets = bug_hunt.plan_targets(str(workspace), "pkg/mathy.py")
    names = {t.symbol for t in targets}
    assert "divide" in names
    assert "safe_len" in names
    divide = next(t for t in targets if t.symbol == "divide")
    assert divide.kind == "function"
    assert "def divide" in divide.signature
    assert "Bug" in divide.docstring


def test_plan_targets_single_symbol(workspace):
    targets = bug_hunt.plan_targets(str(workspace), "pkg/mathy.py::safe_len")
    assert len(targets) == 1
    assert targets[0].symbol == "safe_len"


def test_plan_targets_directory_walks_python_files(workspace):
    (workspace / "pkg" / "extra.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    targets = bug_hunt.plan_targets(str(workspace), "pkg")
    names = {t.symbol for t in targets}
    assert {"divide", "safe_len", "helper"} <= names


def test_plan_targets_unknown_path_returns_empty(workspace):
    assert bug_hunt.plan_targets(str(workspace), "pkg/does_not_exist.py") == []


# ---------------------------------------------------------------------------
# generate_tests — dangerous-code rejection and deterministic fallback
# ---------------------------------------------------------------------------

def test_scan_dangerous_flags_network_import():
    code = "import requests\n\ndef test_x():\n    requests.get('http://x')\n"
    assert bug_hunt._scan_dangerous(code) is not None


def test_scan_dangerous_flags_file_deletion():
    code = "import os\n\ndef test_x():\n    os.remove('/tmp/x')\n"
    assert bug_hunt._scan_dangerous(code) is not None


def test_scan_dangerous_flags_subprocess():
    code = "import subprocess\n\ndef test_x():\n    subprocess.run(['ls'])\n"
    assert bug_hunt._scan_dangerous(code) is not None


def test_scan_dangerous_allows_plain_test():
    code = "def test_x():\n    assert 1 + 1 == 2\n"
    assert bug_hunt._scan_dangerous(code) is None


@pytest.mark.asyncio
async def test_generate_tests_falls_back_without_a_model(workspace, monkeypatch):
    _no_model(monkeypatch)
    targets = bug_hunt.plan_targets(str(workspace), "pkg/mathy.py::divide")
    suite = await bug_hunt.generate_tests(targets[0], owner="tester")
    assert suite.source == "fallback"
    assert suite.test_names
    assert "def test_" in suite.code
    # Must always be valid, safe python.
    assert bug_hunt._scan_dangerous(suite.code) is None


@pytest.mark.asyncio
async def test_generate_tests_rejects_unparseable_model_output(workspace, monkeypatch):
    import src.endpoint_resolver as ep
    import src.llm_core as llm

    monkeypatch.setattr(ep, "resolve_endpoint", lambda *a, **k: ("http://x", "m", {}))

    async def _bad_call(*a, **k):
        return "not python at all {{{"

    monkeypatch.setattr(llm, "llm_call_async", _bad_call)
    targets = bug_hunt.plan_targets(str(workspace), "pkg/mathy.py::divide")
    suite = await bug_hunt.generate_tests(targets[0], owner="tester")
    assert suite.source == "fallback"
    assert any("rejected" in n or "no test_" in n for n in suite.notes)


@pytest.mark.asyncio
async def test_generate_tests_rejects_dangerous_model_output(workspace, monkeypatch):
    import src.endpoint_resolver as ep
    import src.llm_core as llm

    monkeypatch.setattr(ep, "resolve_endpoint", lambda *a, **k: ("http://x", "m", {}))

    async def _dangerous_call(*a, **k):
        return "```python\nimport subprocess\n\ndef test_x():\n    subprocess.run(['ls'])\n```"

    monkeypatch.setattr(llm, "llm_call_async", _dangerous_call)
    targets = bug_hunt.plan_targets(str(workspace), "pkg/mathy.py::divide")
    suite = await bug_hunt.generate_tests(targets[0], owner="tester")
    assert suite.source == "fallback"


@pytest.mark.asyncio
async def test_generate_tests_accepts_valid_model_output(workspace, monkeypatch):
    import src.endpoint_resolver as ep
    import src.llm_core as llm

    monkeypatch.setattr(ep, "resolve_endpoint", lambda *a, **k: ("http://x", "m", {}))
    canned = (
        "```python\n"
        "import importlib.util, os, sys\n\n"
        "_MODULE_PATH = os.path.join(os.environ['BUG_HUNT_WORKSPACE'], 'pkg', 'mathy.py')\n"
        "_spec = importlib.util.spec_from_file_location('bh_target_module', _MODULE_PATH)\n"
        "_bh_mod = importlib.util.module_from_spec(_spec)\n"
        "sys.path.insert(0, os.path.dirname(_MODULE_PATH))\n"
        "_spec.loader.exec_module(_bh_mod)\n\n"
        "def test_divide_normal():\n"
        "    # expected: 10 / 2 == 5\n"
        "    assert _bh_mod.divide(10, 2) == 5\n\n"
        "def test_divide_by_zero_assumption():\n"
        "    # assumption: dividing by zero should raise a clean error, not crash the process\n"
        "    _bh_mod.divide(1, 0)\n"
        "```"
    )

    async def _good_call(*a, **k):
        return canned

    monkeypatch.setattr(llm, "llm_call_async", _good_call)
    targets = bug_hunt.plan_targets(str(workspace), "pkg/mathy.py::divide")
    suite = await bug_hunt.generate_tests(targets[0], owner="tester")
    assert suite.source == "model"
    assert "test_divide_normal" in suite.test_names
    assert "test_divide_by_zero_assumption" in suite.test_names


# ---------------------------------------------------------------------------
# run_suite
# ---------------------------------------------------------------------------

def test_run_suite_parses_pass_and_fail(workspace):
    code = (
        "def test_bh_pass():\n"
        "    assert 1 == 1\n\n"
        "def test_bh_fail():\n"
        "    assert 1 == 2\n"
    )
    suite = bug_hunt.GeneratedSuite(code=code, test_names=["test_bh_pass", "test_bh_fail"], source="fallback")
    result = bug_hunt.run_suite(str(workspace), suite, timeout_s=60, slug_hint="sample")
    assert result.ran is True
    assert result.ok is False
    outcomes = {t["name"].split("::")[-1]: t["outcome"] for t in result.tests}
    assert outcomes.get("test_bh_pass") == "passed"
    assert outcomes.get("test_bh_fail") == "failed"
    failing = [t for t in result.tests if t["outcome"] == "failed"]
    assert failing and "assert" in failing[0]["traceback"].lower()
    assert os.path.isfile(result.file_path)
    os.remove(result.file_path)


def test_run_suite_writes_under_faustus_bughunt_dir(workspace):
    code = "def test_bh_ok():\n    assert True\n"
    suite = bug_hunt.GeneratedSuite(code=code, test_names=["test_bh_ok"], source="fallback")
    result = bug_hunt.run_suite(str(workspace), suite, slug_hint="loc")
    assert ".faustus" in result.file_path.replace(os.sep, "/")
    assert "bughunt" in result.file_path.replace(os.sep, "/")
    os.remove(result.file_path)


# ---------------------------------------------------------------------------
# triage heuristics (no model)
# ---------------------------------------------------------------------------

def test_triage_heuristic_none_input_is_a_bug():
    finding = bug_hunt._triage_heuristic(
        "test_bh_none_a_assumption",
        "TypeError: unsupported operand type(s) for /: 'NoneType' and 'int'",
    )
    assert finding.verdict == "bug"
    assert finding.severity == "medium"


def test_triage_heuristic_assumption_assertion_is_unclear():
    finding = bug_hunt._triage_heuristic(
        "test_divide_by_zero_assumption",
        "AssertionError: expected a ValueError but none was raised",
    )
    assert finding.verdict == "unclear"


def test_triage_heuristic_zero_division_is_a_bug():
    finding = bug_hunt._triage_heuristic("test_bh_zero", "ZeroDivisionError: division by zero")
    assert finding.verdict == "bug"


@pytest.mark.asyncio
async def test_triage_without_model_uses_heuristics_only(workspace, monkeypatch):
    _no_model(monkeypatch)
    targets = bug_hunt.plan_targets(str(workspace), "pkg/mathy.py::divide")
    code = "def test_bh_zero():\n    1 / 0\n"
    suite = bug_hunt.GeneratedSuite(code=code, test_names=["test_bh_zero"], source="fallback")
    result = bug_hunt.run_suite(str(workspace), suite, slug_hint="divide")
    findings = await bug_hunt.triage(targets[0], result, owner="tester")
    os.remove(result.file_path)
    assert len(findings) == 1
    assert findings[0].verdict == "bug"


# ---------------------------------------------------------------------------
# hunt — end to end, LLM monkeypatched
# ---------------------------------------------------------------------------

_CANNED_SUITE = (
    "```python\n"
    "import importlib.util, os, sys\n\n"
    "_MODULE_PATH = os.path.join(os.environ['BUG_HUNT_WORKSPACE'], 'pkg', 'mathy.py')\n"
    "_spec = importlib.util.spec_from_file_location('bh_target_module', _MODULE_PATH)\n"
    "_bh_mod = importlib.util.module_from_spec(_spec)\n"
    "sys.path.insert(0, os.path.dirname(_MODULE_PATH))\n"
    "_spec.loader.exec_module(_bh_mod)\n\n"
    "def test_divide_none_a_assumption():\n"
    "    # assumption: passing None should not crash with a raw TypeError\n"
    "    _bh_mod.divide(None, 1)\n\n"
    "def test_divide_normal():\n"
    "    # expected: 10 / 2 == 5\n"
    "    assert _bh_mod.divide(10, 2) == 5\n"
    "```"
)

_CANNED_TRIAGE = json.dumps([
    {"test": "test_divide_none_a_assumption", "verdict": "bug",
     "root_cause": "divide() does not guard against a None operand",
     "fix_suggestion": "raise a clear ValueError for non-numeric input", "severity": "medium"},
])


@pytest.fixture()
def _canned_model(monkeypatch):
    import src.endpoint_resolver as ep
    import src.llm_core as llm

    monkeypatch.setattr(ep, "resolve_endpoint", lambda *a, **k: ("http://x", "canned-model", {}))
    calls = {"n": 0}

    async def _call(*a, **k):
        calls["n"] += 1
        # First call per target = generation, second = triage.
        return _CANNED_SUITE if calls["n"] % 2 == 1 else _CANNED_TRIAGE

    monkeypatch.setattr(llm, "llm_call_async", _call)
    return calls


@pytest.mark.asyncio
async def test_hunt_end_to_end_with_canned_model(workspace, _canned_model):
    report = await bug_hunt.hunt(str(workspace), "pkg/mathy.py::divide", owner="tester")
    assert report.suite_source == "model"
    assert report.tests_run == 2
    assert report.tests_failed == 1
    bug_findings = [f for f in report.findings if f.verdict == "bug"]
    assert bug_findings and bug_findings[0].test_name.endswith("test_divide_none_a_assumption")
    assert "Bug hunt" in report.to_markdown()
    # Scratch file must be cleaned up by default.
    scratch_dir = os.path.join(str(workspace), ".faustus", "bughunt")
    assert not os.path.isdir(scratch_dir) or not os.listdir(scratch_dir)


@pytest.mark.asyncio
async def test_hunt_keep_tests_writes_regression_file(workspace, _canned_model):
    report = await bug_hunt.hunt(str(workspace), "pkg/mathy.py::divide", owner="tester", keep_tests=True)
    assert report.kept_tests_path
    assert os.path.isfile(report.kept_tests_path)
    content = open(report.kept_tests_path, encoding="utf-8").read()
    assert "def test_divide_none_a_assumption" in content
    # A passing test must never be kept as a "bug" regression test.
    assert "def test_divide_normal" not in content


@pytest.mark.asyncio
async def test_hunt_keep_tests_dedupes_on_second_run(workspace, _canned_model):
    r1 = await bug_hunt.hunt(str(workspace), "pkg/mathy.py::divide", owner="tester", keep_tests=True)
    r2 = await bug_hunt.hunt(str(workspace), "pkg/mathy.py::divide", owner="tester", keep_tests=True)
    assert r1.kept_tests_path == r2.kept_tests_path
    content = open(r2.kept_tests_path, encoding="utf-8").read()
    assert content.count("def test_divide_none_a_assumption") == 1


@pytest.mark.asyncio
async def test_hunt_persists_report_to_disk(workspace, _canned_model, _isolated_data_dir):
    await bug_hunt.hunt(str(workspace), "pkg/mathy.py::divide", owner="tester")
    owner_dir = os.path.join(str(_isolated_data_dir), "bug_hunt", "tester")
    assert os.path.isdir(owner_dir)
    files = [f for f in os.listdir(owner_dir) if f.endswith(".json")]
    assert len(files) == 1
    saved = json.load(open(os.path.join(owner_dir, files[0]), encoding="utf-8"))
    assert saved["target"] == "pkg/mathy.py::divide"


def test_report_rotation_keeps_last_50(tmp_path):
    d = tmp_path / "owner"
    d.mkdir()
    for i in range(60):
        p = d / f"{1000 + i}_slug.json"
        p.write_text("{}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))
    bug_hunt._rotate_reports(str(d))
    remaining = sorted(os.listdir(d))
    assert len(remaining) == 50
    # The oldest ones were the ones removed.
    assert "1000_slug.json" not in remaining
    assert "1059_slug.json" in remaining


# ---------------------------------------------------------------------------
# Route smoke
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AUTH_ENABLED", "false")
    from routes.bug_hunt_routes import setup_bug_hunt_routes

    app = FastAPI()
    app.include_router(setup_bug_hunt_routes())
    with TestClient(app) as c:
        yield c


def test_route_rejects_missing_workspace(api_client):
    resp = api_client.post("/api/bug-hunt", json={"workspace": "/does/not/exist", "target": "x.py"})
    assert resp.status_code == 400


def test_route_runs_and_returns_report(api_client, workspace, _canned_model):
    resp = api_client.post("/api/bug-hunt", json={
        "workspace": str(workspace), "target": "pkg/mathy.py::divide",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["target"] == "pkg/mathy.py::divide"
    assert "findings" in data


def test_route_lists_reports(api_client, workspace, _canned_model):
    api_client.post("/api/bug-hunt", json={"workspace": str(workspace), "target": "pkg/mathy.py::divide"})
    resp = api_client.get("/api/bug-hunt/reports")
    assert resp.status_code == 200
    assert len(resp.json()["reports"]) >= 1


async def test_static_prepass_reports_undefined_names_first(tmp_path, monkeypatch):
    """A name that does not exist is a bug a linter proves in a fraction of a
    second; the hunt reports it before (and besides) any generated test."""
    import importlib.util
    if importlib.util.find_spec("pyflakes") is None:
        pytest.skip("no pyflakes")
    _no_model(monkeypatch)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "broken.py").write_text(
        "def total(xs):\n    return sum(xs) + offset\n", encoding="utf-8")
    report = await bug_hunt.hunt(str(tmp_path), "pkg/broken.py", owner="tester")
    static = [f for f in report.findings if f.test_name.startswith("static:")]
    assert static, [f.to_dict() for f in report.findings]
    assert static[0].verdict == "bug" and static[0].severity == "high"
    assert "offset" in static[0].root_cause and static[0].traceback.startswith("pkg/broken.py:2")
    assert any(n.startswith("static pre-pass") for n in report.notes)
