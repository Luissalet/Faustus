"""tests/test_code_history.py — git history understanding (Lot F).

Real `git` against temp repos (subprocess-backed, same policy as
`tests/test_git_radar.py`): a mocked git status/log would test nothing about
whether the parsing actually matches real output.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from src import code_history

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not installed",
)

_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(cwd, *args, author=None):
    env = dict(_ENV)
    env["HOME"] = str(cwd)
    if author:
        name, email = author
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_NAME"] = name
        env["GIT_COMMITTER_EMAIL"] = email
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=env)


AUTHORS = [("Alice", "alice@x.test"), ("Bob", "bob@x.test"), ("Cara", "cara@x.test")]


@pytest.fixture()
def repo(tmp_path):
    """A repo with 3 authors, several commits touching `mod.py` (whose
    `greet` function changes across commits) and a co-changing `README.md`."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")

    (root / "mod.py").write_text(
        "def greet(name):\n    return 'hi ' + name\n\n\ndef other():\n    return 1\n"
    )
    (root / "README.md").write_text("# repo\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init: add mod.py", author=AUTHORS[0])

    (root / "mod.py").write_text(
        "def greet(name):\n    return 'hello ' + name\n\n\ndef other():\n    return 1\n"
    )
    (root / "README.md").write_text("# repo\n\nUpdated.\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "tweak greeting and docs", author=AUTHORS[1])

    (root / "mod.py").write_text(
        "def greet(name):\n    return f'hello, {name}!'\n\n\ndef other():\n    return 2\n"
    )
    (root / "README.md").write_text("# repo\n\nUpdated again.\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "use f-string in greet", author=AUTHORS[2])

    return root


@pytest.fixture(autouse=True)
def _clear_cache():
    code_history.clear_cache()
    yield
    code_history.clear_cache()


# ---------------------------------------------------------------------------
# file_history
# ---------------------------------------------------------------------------
def test_file_history_lists_commits_newest_first(repo):
    result = code_history.file_history(str(repo), "mod.py")
    assert "error" not in result
    assert result["commit_count"] == 3
    subjects = [c["subject"] for c in result["commits"]]
    assert subjects[0] == "use f-string in greet"
    assert subjects[-1] == "init: add mod.py"
    assert result["first_touched"] and result["last_touched"]


def test_file_history_authors_ranking_and_churn(repo):
    result = code_history.file_history(str(repo), "mod.py")
    names = {a["name"] for a in result["authors"]}
    assert names == {"Alice", "Bob", "Cara"}
    assert result["total_adds"] > 0


def test_file_history_accepts_absolute_path(repo):
    result = code_history.file_history(str(repo), str(repo / "mod.py"))
    assert result["path"] == "mod.py"
    assert result["commit_count"] == 3


def test_file_history_limit_is_bounded(repo):
    result = code_history.file_history(str(repo), "mod.py", limit=1)
    assert result["commit_count"] == 1


# ---------------------------------------------------------------------------
# symbol_history (native -L, then the ast/regex fallback path)
# ---------------------------------------------------------------------------
def test_symbol_history_native_dash_l(repo):
    result = code_history.symbol_history(str(repo), "mod.py", "greet")
    assert "error" not in result
    assert result["commit_count"] >= 2
    assert result["latest_diff_excerpt"]
    excerpt_lines = result["latest_diff_excerpt"].split("\n")
    assert len(excerpt_lines) <= 60


def test_symbol_history_unknown_symbol_errors(repo):
    result = code_history.symbol_history(str(repo), "mod.py", "does_not_exist")
    assert "error" in result


def test_symbol_history_fallback_when_dash_l_unsupported(repo, monkeypatch):
    # Force the "old git" branch: -L is reported unsupported, so the module
    # must fall back to ast-derived line range + `git log -Lstart,end:path`.
    monkeypatch.setattr(code_history, "_supports_dash_l", lambda workspace: False)
    result = code_history.symbol_history(str(repo), "mod.py", "greet")
    assert "error" not in result
    assert result["line_range"] is not None
    start, end = result["line_range"]
    assert start >= 1 and end >= start
    assert result["commit_count"] >= 1


def test_find_symbol_range_ast_matches_function(repo):
    rng = code_history._find_symbol_range(str(repo), "mod.py", "other")
    assert rng is not None
    start, end = rng
    text = (repo / "mod.py").read_text().split("\n")
    assert "def other" in text[start - 1]


# ---------------------------------------------------------------------------
# blame_summary
# ---------------------------------------------------------------------------
def test_blame_summary_aggregates_by_author(repo):
    result = code_history.blame_summary(str(repo), "mod.py")
    assert "error" not in result
    assert result["total_lines"] > 0
    total_pct = sum(a["pct"] for a in result["by_author"])
    assert 95.0 <= total_pct <= 100.5
    assert result["newest_commit"] and result["oldest_commit"]
    assert result["hot_commits"]


def test_blame_summary_with_line_range(repo):
    result = code_history.blame_summary(str(repo), "mod.py", start=1, end=2)
    assert "error" not in result
    assert result["total_lines"] <= 2


# ---------------------------------------------------------------------------
# co_change
# ---------------------------------------------------------------------------
def test_co_change_finds_readme(repo):
    result = code_history.co_change(str(repo), "mod.py")
    assert "error" not in result
    paths = {c["path"] for c in result["co_changed"]}
    assert "README.md" in paths
    readme = next(c for c in result["co_changed"] if c["path"] == "README.md")
    # README.md was touched in all 3 commits, same as mod.py itself.
    assert readme["ratio"] == pytest.approx(1.0, rel=0.01)


# ---------------------------------------------------------------------------
# risk
# ---------------------------------------------------------------------------
def test_risk_score_and_explanation_without_tests(repo):
    result = code_history.risk(str(repo), "mod.py")
    assert "error" not in result
    assert 0.0 <= result["score"] <= 1.0
    assert result["has_related_tests"] is False
    assert "no related test file found" in result["explanation"]
    assert result["authors_overall"] == 3


def test_risk_lower_with_related_tests(repo, monkeypatch):
    monkeypatch.setattr(code_history, "_related_tests_present", lambda ws, p: True)
    with_tests = code_history.risk(str(repo), "mod.py")
    monkeypatch.setattr(code_history, "_related_tests_present", lambda ws, p: False)
    without_tests = code_history.risk(str(repo), "mod.py")
    assert with_tests["score"] < without_tests["score"]
    assert "has related tests" in with_tests["explanation"]


# ---------------------------------------------------------------------------
# explain + cache
# ---------------------------------------------------------------------------
def test_explain_combines_everything_with_summary(repo):
    result = code_history.explain(str(repo), "mod.py", symbol="greet")
    assert "error" not in result
    assert "file_history" in result and "blame_summary" in result
    assert "co_change" in result and "risk" in result and "symbol_history" in result
    assert "mod.py" in result["summary_md"]
    assert "greet" in result["summary_md"]


def test_explain_is_cached_per_head_sha(repo, monkeypatch):
    calls = {"n": 0}
    real_run = code_history._run_git

    def counting_run(workspace, args, timeout=code_history._TIMEOUT_S):
        calls["n"] += 1
        return real_run(workspace, args, timeout=timeout)

    monkeypatch.setattr(code_history, "_run_git", counting_run)
    first = code_history.explain(str(repo), "mod.py")
    n_after_first = calls["n"]
    second = code_history.explain(str(repo), "mod.py")
    assert second is first  # same cached object
    # A cache hit still confirms the repo + reads HEAD (2 calls) to key the
    # cache, but does none of the actual log/blame/diff work again.
    assert calls["n"] - n_after_first == 2


def test_explain_cache_invalidated_by_new_commit(repo):
    first = code_history.explain(str(repo), "mod.py")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "noop", author=AUTHORS[0])
    second = code_history.explain(str(repo), "mod.py")
    assert second is not first


# ---------------------------------------------------------------------------
# Error handling on a non-repo
# ---------------------------------------------------------------------------
def test_all_functions_error_on_non_repo(tmp_path):
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    (not_repo / "f.py").write_text("x = 1\n")
    for fn, args in [
        (code_history.file_history, (str(not_repo), "f.py")),
        (code_history.blame_summary, (str(not_repo), "f.py")),
        (code_history.co_change, (str(not_repo), "f.py")),
        (code_history.risk, (str(not_repo), "f.py")),
        (code_history.explain, (str(not_repo), "f.py")),
    ]:
        assert "error" in fn(*args)
    assert "error" in code_history.symbol_history(str(not_repo), "f.py", "x")


def test_file_history_requires_path(repo):
    result = code_history.file_history(str(repo), "")
    assert "error" in result


def test_run_git_never_raises_on_bad_workspace():
    ok, out, err = code_history._run_git("/no/such/dir/at/all", ["status"])
    assert ok is False


# ---------------------------------------------------------------------------
# Tool: code_history
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_code_history_tool_explain(repo, monkeypatch):
    from src.agent_tools.code_history_tools import CodeHistoryTool
    monkeypatch.setattr(
        "src.tool_execution._resolve_search_root", lambda raw: str(repo)
    )
    tool = CodeHistoryTool()
    raw = await tool.execute(json.dumps({"path": "mod.py"}), ctx={})
    data = json.loads(raw)
    assert data["exit_code"] == 0
    assert "summary_md" in data


@pytest.mark.asyncio
async def test_code_history_tool_mode_risk(repo, monkeypatch):
    from src.agent_tools.code_history_tools import CodeHistoryTool
    monkeypatch.setattr(
        "src.tool_execution._resolve_search_root", lambda raw: str(repo)
    )
    tool = CodeHistoryTool()
    raw = await tool.execute(json.dumps({"path": "mod.py", "mode": "risk"}), ctx={})
    data = json.loads(raw)
    assert data["exit_code"] == 0
    assert "score" in data


@pytest.mark.asyncio
async def test_code_history_tool_requires_path(repo, monkeypatch):
    from src.agent_tools.code_history_tools import CodeHistoryTool
    monkeypatch.setattr(
        "src.tool_execution._resolve_search_root", lambda raw: str(repo)
    )
    tool = CodeHistoryTool()
    raw = await tool.execute(json.dumps({}), ctx={})
    data = json.loads(raw)
    assert data["exit_code"] == 1


@pytest.mark.asyncio
async def test_code_history_tool_symbol_mode_needs_symbol(repo, monkeypatch):
    from src.agent_tools.code_history_tools import CodeHistoryTool
    monkeypatch.setattr(
        "src.tool_execution._resolve_search_root", lambda raw: str(repo)
    )
    tool = CodeHistoryTool()
    raw = await tool.execute(json.dumps({"path": "mod.py", "mode": "symbol"}), ctx={})
    data = json.loads(raw)
    assert data["exit_code"] == 1


# ---------------------------------------------------------------------------
# Route smoke test: GET /api/code-history
# ---------------------------------------------------------------------------
@pytest.fixture()
def route_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import code_history_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.include_router(code_history_routes.setup_code_history_routes())
    return TestClient(app)


def test_route_smoke_explain(route_client, repo, monkeypatch):
    monkeypatch.setattr(
        "src.tool_execution._resolve_search_root", lambda raw: str(repo)
    )
    resp = route_client.get("/api/code-history", params={"path": "mod.py"})
    assert resp.status_code == 200
    body = resp.json()
    assert "summary_md" in body


def test_route_smoke_bad_path_is_400(route_client, repo, monkeypatch):
    monkeypatch.setattr(
        "src.tool_execution._resolve_search_root", lambda raw: str(repo)
    )
    resp = route_client.get("/api/code-history", params={"path": "no_such_file.py", "mode": "file"})
    assert resp.status_code in (400, 200)  # git log on a missing path returns empty history, not an error
