"""tests/test_ci_failures.py -- src/ci_failures.py + src/agent_tools/ci_tools.py
+ routes/ci_failures_routes.py (lot C).

No real network: `src.reach.http_client.make_client` is monkeypatched to an
`httpx.AsyncClient` wired to `httpx.MockTransport`, the same discipline
`tests/test_reach_backends.py` already uses for Reach's own HTTP calls.
`gh` CLI fallback is exercised by monkeypatching `subprocess.run` directly.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess

import httpx
import pytest

from src import ci_failures


# ---------------------------------------------------------------------------
# repo_from_workspace: remote URL parsing
# ---------------------------------------------------------------------------

def _init_repo(tmp_path, remote_url):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=repo, check=True)
    return repo


@pytest.mark.parametrize("url,expected", [
    ("git@github.com:anomalyco/opencode.git", ("anomalyco", "opencode")),
    ("git@my-alias.internal:Team/Proj.git", ("Team", "Proj")),
    ("https://github.com/anomalyco/opencode.git", ("anomalyco", "opencode")),
    ("https://github.com/anomalyco/opencode", ("anomalyco", "opencode")),
])
def test_repo_from_workspace_parses_remote_forms(tmp_path, url, expected):
    repo = _init_repo(tmp_path, url)
    assert ci_failures.repo_from_workspace(str(repo)) == expected


def test_repo_from_workspace_no_remote(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert ci_failures.repo_from_workspace(str(repo)) is None


def test_repo_from_workspace_not_a_repo(tmp_path):
    d = tmp_path / "not_a_repo"
    d.mkdir()
    assert ci_failures.repo_from_workspace(str(d)) is None


def test_repo_from_workspace_unrecognized_url(tmp_path):
    repo = _init_repo(tmp_path, "s3://some-bucket/not-a-repo")
    assert ci_failures.repo_from_workspace(str(repo)) is None


# ---------------------------------------------------------------------------
# extract_failures: one fixture per ecosystem
# ---------------------------------------------------------------------------

PYTEST_LOG = """\
some setup noise
============================= FAILURES =============================
FAILED tests/test_math.py::test_add - AssertionError: assert 1 == 2
FAILED tests/test_math.py::test_sub - ValueError: bad input
============================= short summary =============================
"""

JEST_LOG = """\
FAIL src/sum.test.js
  ● sum › adds two numbers
    expect(received).toBe(expected)
    Expected: 3
    Received: 4

  ● sum › handles negatives
    TypeError: cannot read property
"""

TSC_LOG = """\
src/index.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.
src/other.ts(4,1): error TS2304: Cannot find name 'Foo'.
"""

ESLINT_LOG = """\
/repo/src/app.js
  12:5  error  'x' is defined but never used  no-unused-vars
  20:1  error  Missing semicolon  semi

/repo/src/util.js
  3:2  error  Unexpected console statement  no-console
"""

CARGO_LOG = """\
error[E0308]: mismatched types
 --> src/main.rs:10:5
  |
10|     let x: i32 = "hi";
  |
error: aborting due to previous error
"""

GO_LOG = """\
--- FAIL: TestAdd (0.00s)
    add_test.go:12: expected 3, got 4
FAIL
FAIL	example.com/pkg	0.003s
"""

NPM_LOG = """\
npm ERR! code ENOENT
npm ERR! syscall open
npm ERR! path /repo/package.json
"""

GH_ANNOTATION_LOG = """\
##[error]Process completed with exit code 1.
"""

GENERIC_LOG = """\
Running build step
Error: could not resolve module 'left-pad'
Traceback (most recent call last):
  File "build.py", line 3, in <module>
    raise RuntimeError("boom")
RuntimeError: boom
"""


def test_extract_pytest():
    blocks = ci_failures.extract_failures(PYTEST_LOG)
    kinds = {b.kind for b in blocks}
    assert "pytest" in kinds
    pytest_blocks = [b for b in blocks if b.kind == "pytest"]
    assert len(pytest_blocks) == 2
    assert pytest_blocks[0].file == "tests/test_math.py"
    assert pytest_blocks[0].test == "test_add"
    assert "AssertionError" in pytest_blocks[0].message


def test_extract_jest():
    blocks = ci_failures.extract_failures(JEST_LOG)
    jest_blocks = [b for b in blocks if b.kind == "jest"]
    assert len(jest_blocks) == 2
    assert "adds two numbers" in jest_blocks[0].test
    assert "Expected" in jest_blocks[0].excerpt


def test_extract_tsc():
    blocks = ci_failures.extract_failures(TSC_LOG)
    tsc_blocks = [b for b in blocks if b.kind == "tsc"]
    assert len(tsc_blocks) == 2
    assert tsc_blocks[0].file == "src/index.ts"
    assert tsc_blocks[0].line == 12
    assert "not assignable" in tsc_blocks[0].message


def test_extract_eslint():
    blocks = ci_failures.extract_failures(ESLINT_LOG)
    eslint_blocks = [b for b in blocks if b.kind == "eslint"]
    assert len(eslint_blocks) == 3
    files = {b.file for b in eslint_blocks}
    assert files == {"/repo/src/app.js", "/repo/src/util.js"}
    assert eslint_blocks[0].line == 12


def test_extract_cargo():
    blocks = ci_failures.extract_failures(CARGO_LOG)
    cargo_blocks = [b for b in blocks if b.kind == "cargo"]
    assert len(cargo_blocks) == 1
    assert cargo_blocks[0].file == "src/main.rs"
    assert cargo_blocks[0].line == 10
    assert "mismatched types" in cargo_blocks[0].message


def test_extract_go():
    blocks = ci_failures.extract_failures(GO_LOG)
    go_blocks = [b for b in blocks if b.kind == "go"]
    assert len(go_blocks) == 1
    assert go_blocks[0].test == "TestAdd"


def test_extract_npm_and_annotation_and_generic():
    npm_blocks = [b for b in ci_failures.extract_failures(NPM_LOG) if b.kind == "npm"]
    assert npm_blocks and "ENOENT" in npm_blocks[0].message

    gh_blocks = [b for b in ci_failures.extract_failures(GH_ANNOTATION_LOG) if b.kind == "gh_annotation"]
    assert gh_blocks and "exit code 1" in gh_blocks[0].message

    generic_blocks = ci_failures.extract_failures(GENERIC_LOG)
    kinds = {b.kind for b in generic_blocks}
    assert "generic" in kinds
    assert "traceback" in kinds


def test_extract_strips_ansi_and_timestamps():
    raw = "2026-01-02T03:04:05.1234567Z \x1b[31mFAILED tests/x.py::test_y - boom\x1b[0m"
    blocks = ci_failures.extract_failures(raw)
    assert len(blocks) == 1
    assert blocks[0].file == "tests/x.py"
    assert "\x1b" not in blocks[0].message


def test_extract_dedupes_repeated_blocks():
    doubled = PYTEST_LOG + "\n" + PYTEST_LOG
    blocks = ci_failures.extract_failures(doubled)
    pytest_blocks = [b for b in blocks if b.kind == "pytest"]
    assert len(pytest_blocks) == 2  # not 4


def test_extract_empty_log():
    assert ci_failures.extract_failures("") == []
    assert ci_failures.extract_failures(None) == []


# ---------------------------------------------------------------------------
# map_to_workspace
# ---------------------------------------------------------------------------

def _git_repo_with_file(tmp_path):
    repo = tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Author"], cwd=repo, check=True)
    (repo / "src").mkdir()
    (repo / "src" / "thing.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_map_to_workspace_resolves_existing_file(tmp_path):
    repo = _git_repo_with_file(tmp_path)
    block = ci_failures.FailureBlock(kind="pytest", file="src/thing.py", test="test_x", message="boom")
    mapped = ci_failures.map_to_workspace([block], str(repo))
    assert len(mapped) == 1
    row = mapped[0]
    assert row["resolved_path"] == "src/thing.py"
    assert row["last_touched"]["author"] == "Test Author"


def test_map_to_workspace_missing_file_returns_no_resolution(tmp_path):
    repo = _git_repo_with_file(tmp_path)
    block = ci_failures.FailureBlock(kind="pytest", file="src/does_not_exist.py", message="boom")
    mapped = ci_failures.map_to_workspace([block], str(repo))
    assert mapped[0]["resolved_path"] is None
    assert mapped[0]["last_touched"] is None


def test_map_to_workspace_refuses_path_escape(tmp_path):
    repo = _git_repo_with_file(tmp_path)
    block = ci_failures.FailureBlock(kind="generic", file="../../etc/passwd", message="boom")
    mapped = ci_failures.map_to_workspace([block], str(repo))
    assert mapped[0]["resolved_path"] is None


def test_map_to_workspace_accepts_plain_dicts(tmp_path):
    repo = _git_repo_with_file(tmp_path)
    mapped = ci_failures.map_to_workspace([{"kind": "pytest", "file": "src/thing.py", "message": "boom"}], str(repo))
    assert mapped[0]["resolved_path"] == "src/thing.py"


# ---------------------------------------------------------------------------
# analyze(): httpx monkeypatched (fake run/jobs/log)
# ---------------------------------------------------------------------------

def _mock_client_factory(handler):
    def _make_client(**kwargs):
        headers = kwargs.pop("headers", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers, **kwargs)
    return _make_client


def _github_handler(run_id=555, job_id=999, log_text=PYTEST_LOG, job_conclusion="failure"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/actions/runs") and request.method == "GET":
            return httpx.Response(200, json={"workflow_runs": [
                {"id": run_id, "head_branch": "main", "conclusion": "failure",
                 "status": "completed", "html_url": "https://example.invalid/run", "created_at": "2026-01-01T00:00:00Z"},
            ]})
        if path.endswith(f"/actions/runs/{run_id}") and request.method == "GET":
            return httpx.Response(200, json={"id": run_id, "head_branch": "main", "conclusion": "failure",
                                              "status": "completed"})
        if path.endswith(f"/actions/runs/{run_id}/jobs"):
            return httpx.Response(200, json={"jobs": [
                {"id": job_id, "name": "tests", "conclusion": job_conclusion, "status": "completed"},
            ]})
        if path.endswith(f"/actions/jobs/{job_id}/logs"):
            return httpx.Response(200, text=log_text)
        return httpx.Response(404, json={"message": "not found"})
    return handler


def test_analyze_latest_failed_run(tmp_path, monkeypatch):
    repo = _git_repo_with_file(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(_github_handler(log_text=PYTEST_LOG)))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")

    analysis = asyncio.run(ci_failures.analyze(str(repo)))
    assert analysis.owner == "acme"
    assert analysis.repo == "widgets"
    assert analysis.run["id"] == 555
    assert len(analysis.blocks) == 2
    assert "acme/widgets" in analysis.summary_md()


def test_analyze_by_run_id(tmp_path, monkeypatch):
    repo = _git_repo_with_file(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/widgets.git"], cwd=repo, check=True)
    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(_github_handler(run_id=777, log_text=CARGO_LOG)))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")

    analysis = asyncio.run(ci_failures.analyze(str(repo), run_id=777))
    assert analysis.run["id"] == 777
    assert analysis.blocks[0]["kind"] == "cargo"


def test_analyze_no_failed_run_raises(tmp_path, monkeypatch):
    repo = _git_repo_with_file(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow_runs": []})

    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(handler))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")
    with pytest.raises(ci_failures.CiFailuresError):
        asyncio.run(ci_failures.analyze(str(repo)))


def test_analyze_no_remote_raises(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    with pytest.raises(ci_failures.CiFailuresError):
        asyncio.run(ci_failures.analyze(str(repo)))


def test_rest_failure_falls_back_to_gh_cli(tmp_path, monkeypatch):
    repo = _git_repo_with_file(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(boom))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")
    monkeypatch.setattr(ci_failures.shutil, "which", lambda name: "/usr/bin/gh")

    def fake_run(cmd, capture_output, text, timeout):
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        if cmd[1:3] == ["run", "list"]:
            P.stdout = json.dumps([{"databaseId": 42, "status": "completed", "conclusion": "failure",
                                     "headBranch": "main", "displayTitle": "t", "createdAt": "2026-01-01",
                                     "headSha": "abc", "event": "push", "url": "https://x"}])
        return P()

    monkeypatch.setattr(ci_failures.subprocess, "run", fake_run)
    runs = asyncio.run(ci_failures.list_runs("acme", "widgets"))
    assert runs[0]["id"] == 42


def test_rest_and_gh_both_fail_raises_clear_error(tmp_path, monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(boom))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")
    monkeypatch.setattr(ci_failures.shutil, "which", lambda name: None)

    with pytest.raises(ci_failures.CiFailuresError) as exc:
        asyncio.run(ci_failures.list_runs("acme", "widgets"))
    assert "gh" in str(exc.value)


# ---------------------------------------------------------------------------
# Cache: write/read/rotation
# ---------------------------------------------------------------------------

def test_cache_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    ci_failures._cache_write("acme", 123, {"run": {"id": 123}, "jobs": [], "blocks": []})
    got = ci_failures._cache_read("acme", 123)
    assert got["run"]["id"] == 123


def test_cache_rotation_keeps_last_30(tmp_path, monkeypatch):
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path))
    for i in range(35):
        ci_failures._cache_write("acme", i, {"run": {"id": i}, "jobs": [], "blocks": []})
    d = ci_failures._cache_dir("acme")
    files = [f for f in os.listdir(d) if f.endswith(".json")]
    assert len(files) <= 30


def test_analyze_uses_cache_on_second_call(tmp_path, monkeypatch):
    repo = _git_repo_with_file(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    monkeypatch.setattr("src.constants.DATA_DIR", str(tmp_path / "data"))
    calls = {"jobs": 0}

    handler = _github_handler(log_text=PYTEST_LOG)

    def counting_handler(request):
        if request.url.path.endswith("/jobs"):
            calls["jobs"] += 1
        return handler(request)

    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(counting_handler))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")

    asyncio.run(ci_failures.analyze(str(repo), run_id=555))
    asyncio.run(ci_failures.analyze(str(repo), run_id=555))
    assert calls["jobs"] == 1  # second call served from cache


# ---------------------------------------------------------------------------
# propose_fixes: no model configured -> mapping-only, never raises
# ---------------------------------------------------------------------------

def test_propose_fixes_without_model_returns_mapping_only():
    from src.ci_failures import Analysis
    analysis = Analysis(owner="acme", repo="widgets", run={"id": 1}, jobs=[],
                         blocks=[{"file": "a.py", "resolved_path": "a.py", "kind": "pytest", "message": "boom"}])
    out = asyncio.run(ci_failures.propose_fixes(analysis))
    assert out[0]["file"] == "a.py"
    assert out[0]["cause"] is None
    assert "note" in out[0]


def test_propose_fixes_empty_blocks_returns_empty():
    from src.ci_failures import Analysis
    analysis = Analysis(owner="acme", repo="widgets", run={"id": 1}, jobs=[], blocks=[])
    assert asyncio.run(ci_failures.propose_fixes(analysis)) == []


# ---------------------------------------------------------------------------
# Tool output (JSON-shaped dict)
# ---------------------------------------------------------------------------

def test_tool_no_workspace_errors():
    from src.agent_tools.ci_tools import CiFailuresTool
    result = asyncio.run(CiFailuresTool().execute("{}", {}))
    assert result["exit_code"] == 1
    assert "workspace" in result["error"]


def test_tool_success_shape(tmp_path, monkeypatch):
    repo = _git_repo_with_file(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(_github_handler(log_text=PYTEST_LOG)))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")

    from src.agent_tools.ci_tools import CiFailuresTool
    result = asyncio.run(CiFailuresTool().execute(json.dumps({}), {"workspace": str(repo)}))
    assert result["exit_code"] == 0
    assert result["owner"] == "acme"
    assert isinstance(result["failures"], list)
    assert len(result["failures"]) == 2
    assert "output" in result and "acme/widgets" in result["output"]


def test_tool_reports_clear_error_on_failure(tmp_path):
    from src.agent_tools.ci_tools import CiFailuresTool
    d = tmp_path / "not_a_repo"
    d.mkdir()
    result = asyncio.run(CiFailuresTool().execute(json.dumps({}), {"workspace": str(d)}))
    assert result["exit_code"] == 1
    assert "ci_failures" in result["error"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_tool_registered_everywhere():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_capabilities import TOOL_CAPABILITIES
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_index_examples import EXAMPLES
    from src.tool_security import PLAN_MODE_READONLY_TOOLS
    import ast

    assert "ci_failures" in TOOL_HANDLERS
    assert "ci_failures" in TOOL_TAGS
    assert "ci_failures" in TOOL_CAPABILITIES
    assert "ci_failures" in BUILTIN_TOOL_DESCRIPTIONS
    assert "ci_failures" in EXAMPLES and len(EXAMPLES["ci_failures"]) >= 3
    assert "ci_failures" in PLAN_MODE_READONLY_TOOLS

    src_text = open("src/tool_schemas.py", encoding="utf-8").read()
    tree = ast.parse(src_text)
    mod_ns: dict = {}
    exec(compile(tree, "tool_schemas.py", "exec"), mod_ns)
    names = {s["function"]["name"] for s in mod_ns["FUNCTION_TOOL_SCHEMAS"]}
    assert "ci_failures" in names


# ---------------------------------------------------------------------------
# Route smoke test
# ---------------------------------------------------------------------------

@pytest.fixture()
def route_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import ci_failures_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(ci_failures_routes.setup_ci_failures_routes())
    return TestClient(app)


def test_route_requires_workspace(route_client):
    resp = route_client.get("/api/ci/failures")
    assert resp.status_code == 422


def test_route_returns_error_payload_for_bad_workspace(route_client, tmp_path):
    d = tmp_path / "not_a_repo"
    d.mkdir()
    resp = route_client.get("/api/ci/failures", params={"workspace": str(d)})
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_route_success(route_client, tmp_path, monkeypatch):
    repo = _git_repo_with_file(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
    monkeypatch.setattr(ci_failures, "make_client", _mock_client_factory(_github_handler(log_text=PYTEST_LOG)))
    monkeypatch.setattr(ci_failures.credentials, "get_token", lambda channel: "")

    resp = route_client.get("/api/ci/failures", params={"workspace": str(repo)})
    assert resp.status_code == 200
    data = resp.json()
    assert data["owner"] == "acme"
    assert len(data["failures"]) == 2
    assert "summary_md" in data
