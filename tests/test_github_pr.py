"""src/github_pr.py -- "give the agent a GitHub issue and it ends with a
pull request": parsing/formatting helpers plus the two GitHub-reaching calls
(`fetch_issue`, `open_pull_request`), and the `github_issue`/`git_open_pr`
agent tools built on top of them.

No network: httpx is exercised through `httpx.MockTransport`, `gh`/`git`
subprocess calls are faked via monkeypatching `subprocess.run` (and
`shutil.which` where a fallback path needs `gh` to look "installed").
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections import namedtuple

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import github_pr  # noqa: E402

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])


def _run_async(coro):
    return asyncio.run(coro)


_RealAsyncClient = httpx.AsyncClient


def _mock_client(handler):
    """`httpx.AsyncClient` factory routed through a `MockTransport`, ignoring
    whatever timeout/headers `github_pr` passes (headers are forwarded so a
    test can assert on `Authorization`)."""
    transport = httpx.MockTransport(handler)

    def factory(*, timeout=None, headers=None, **_kw):
        return _RealAsyncClient(transport=transport, headers=headers)

    return factory


def _run_git(args, cwd, check=True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc


def _commit_empty(cwd, message):
    # `-c` global overrides must precede the subcommand.
    return _run_git(["-c", "user.name=t", "-c", "user.email=t@x",
                      "commit", "--allow-empty", "-q", "-m", message], cwd)


@pytest.fixture(autouse=True)
def isolated_git_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("gh_home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(home / "no-such-system-gitconfig"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# parse_issue_ref
# ---------------------------------------------------------------------------
def test_parse_full_issue_url():
    assert github_pr.parse_issue_ref("https://github.com/acme/widgets/issues/42") == \
        {"owner": "acme", "repo": "widgets", "number": 42}


def test_parse_full_pull_url():
    assert github_pr.parse_issue_ref("https://github.com/acme/widgets/pull/7") == \
        {"owner": "acme", "repo": "widgets", "number": 7}


def test_parse_owner_repo_hash_number():
    assert github_pr.parse_issue_ref("acme/widgets#5") == {"owner": "acme", "repo": "widgets", "number": 5}


def test_parse_bare_number_resolves_via_https_origin(tmp_path, monkeypatch):
    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, **_kw):
        assert cmd == ["git", "remote", "get-url", "origin"]
        return subprocess.CompletedProcess(cmd, 0, "https://github.com/acme/widgets.git\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert github_pr.parse_issue_ref("#9", workspace=str(tmp_path)) == \
        {"owner": "acme", "repo": "widgets", "number": 9}


def test_parse_bare_number_resolves_via_ssh_alias_origin(tmp_path, monkeypatch):
    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, **_kw):
        return subprocess.CompletedProcess(cmd, 0, "git@Luissalet:Owner/Repo.git\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert github_pr.parse_issue_ref("12", workspace=str(tmp_path)) == \
        {"owner": "Owner", "repo": "Repo", "number": 12}


def test_parse_bare_number_with_no_origin_returns_none(tmp_path, monkeypatch):
    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None, **_kw):
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: No such remote 'origin'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert github_pr.parse_issue_ref("#3", workspace=str(tmp_path)) is None


def test_parse_garbage_returns_none():
    assert github_pr.parse_issue_ref("not an issue reference at all") is None
    assert github_pr.parse_issue_ref("") is None


# ---------------------------------------------------------------------------
# fetch_issue
# ---------------------------------------------------------------------------
def test_fetch_issue_with_token_via_api(monkeypatch):
    seen_auth = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth["value"] = request.headers.get("authorization")
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[
                {"user": {"login": "bob"}, "body": "looks good", "created_at": "2026-01-01T00:00:00Z",
                 "reactions": {"total_count": 1}},
            ])
        return httpx.Response(200, json={
            "title": "Fix the thing", "body": "Please fix it.\n- [ ] add a test\n- [x] repro",
            "labels": [{"name": "bug"}], "state": "open", "comments": 1,
            "html_url": "https://github.com/acme/widgets/issues/42",
        })

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    issue = _run_async(github_pr.fetch_issue("acme", "widgets", 42, token="tok123"))
    assert seen_auth["value"] == "Bearer tok123"
    assert issue["title"] == "Fix the thing"
    assert issue["labels"] == ["bug"]
    assert issue["state"] == "open"
    assert len(issue["comments"]) == 1
    assert issue["comments"][0]["author"] == "bob"
    assert issue["url"] == "https://github.com/acme/widgets/issues/42"


def test_fetch_issue_falls_back_to_gh_when_no_token_and_api_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)

    def fake_run(cmd, capture_output=None, text=None, timeout=None, **_kw):
        assert cmd[:3] == ["/usr/bin/gh", "issue", "view"]
        payload = {
            "title": "From gh", "body": "body text", "labels": [{"name": "help wanted"}],
            "state": "OPEN", "comments": [{"author": {"login": "amy"}, "body": "ok", "createdAt": "x"}],
            "url": "https://github.com/acme/widgets/issues/42",
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    issue = _run_async(github_pr.fetch_issue("acme", "widgets", 42, token=""))
    assert issue["title"] == "From gh"
    assert issue["state"] == "open"
    assert issue["comments"][0]["author"] == "amy"


def test_fetch_issue_raises_with_no_token_and_no_gh(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(github_pr.GithubPRError):
        _run_async(github_pr.fetch_issue("acme", "widgets", 42, token=""))


def test_fetch_issue_raises_immediately_when_token_present_and_api_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad credentials")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    calls = {"gh": 0}
    monkeypatch.setattr(shutil, "which", lambda name: calls.update(gh=calls["gh"] + 1) or "/usr/bin/gh")
    with pytest.raises(github_pr.GithubPRError):
        _run_async(github_pr.fetch_issue("acme", "widgets", 42, token="tok"))
    assert calls["gh"] == 0  # never falls back to gh once a token was used


# ---------------------------------------------------------------------------
# issue_brief
# ---------------------------------------------------------------------------
def test_issue_brief_extracts_checklist_and_labels():
    issue = {
        "title": "Add dark mode", "state": "open", "labels": ["enhancement", "ui"],
        "body": "Users want dark mode.\n- [ ] add a toggle\n- [x] design mock",
    }
    brief = github_pr.issue_brief(issue)
    assert "# Add dark mode" in brief
    assert "Labels: enhancement, ui" in brief
    assert "State: open" in brief
    assert "- [ ] add a toggle" in brief
    assert "- [x] design mock" in brief


def test_issue_brief_trims_long_body():
    issue = {"title": "Long", "body": "x" * 5000, "labels": [], "state": "open"}
    brief = github_pr.issue_brief(issue)
    assert len(brief) < 2200
    assert "…" in brief


def test_issue_brief_handles_missing_body():
    brief = github_pr.issue_brief({"title": "Empty"})
    assert "(no description)" in brief


# ---------------------------------------------------------------------------
# suggest_branch_name
# ---------------------------------------------------------------------------
def test_suggest_branch_name_default_prefix():
    name = github_pr.suggest_branch_name({"number": 123, "title": "Login button is broken!", "labels": []})
    assert name == "fix/123-login-button-is-broken"


def test_suggest_branch_name_feature_label():
    name = github_pr.suggest_branch_name({"number": 9, "title": "Add export to CSV", "labels": ["enhancement"]})
    assert name.startswith("feat/9-")


# ---------------------------------------------------------------------------
# pr_body_from_turn
# ---------------------------------------------------------------------------
def test_pr_body_from_turn_includes_closes_and_sections():
    body = github_pr.pr_body_from_turn(
        {"number": 42}, "Fixed the off-by-one error.", ["src/foo.py", "tests/test_foo.py"], "pytest -q: 12 passed",
    )
    assert body.startswith("Closes #42")
    assert "Fixed the off-by-one error." in body
    assert "- src/foo.py" in body
    assert "pytest -q: 12 passed" in body


def test_pr_body_from_turn_without_issue():
    body = github_pr.pr_body_from_turn(None, "A small cleanup.", [], "")
    assert "Closes" not in body
    assert "A small cleanup." in body


# ---------------------------------------------------------------------------
# open_pull_request
# ---------------------------------------------------------------------------
@pytest.fixture
def origin_repo(tmp_path):
    """A local dir whose `origin` remote is a bare repo on disk -- enough
    for `_origin_owner_repo` to resolve owner/repo without any network."""
    bare = tmp_path / "acme" / "widgets.git"
    bare.mkdir(parents=True)
    _run_git(["init", "-q", "--bare"], cwd=bare)
    work = tmp_path / "work"
    work.mkdir()
    _run_git(["init", "-q", "-b", "main"], cwd=work)
    _run_git(["remote", "add", "origin", str(bare)], cwd=work)
    return work


pytestmark = [pytestmark_git]


def test_open_pull_request_creates_new(origin_repo, monkeypatch):
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={"html_url": "https://github.com/acme/widgets/pull/1", "number": 1})

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    result = _run_async(github_pr.open_pull_request(
        str(origin_repo), base="main", head="fix/1-thing", title="Fix thing", body="Closes #1", token="tok",
    ))
    assert result == {"url": "https://github.com/acme/widgets/pull/1", "number": 1, "created": True}
    assert posted["base"] == "main" and posted["head"] == "fix/1-thing"


def test_open_pull_request_returns_existing_without_creating(origin_repo, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[
                {"html_url": "https://github.com/acme/widgets/pull/5", "number": 5},
            ])
        raise AssertionError("must not POST when a PR already exists")

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(handler))
    result = _run_async(github_pr.open_pull_request(
        str(origin_repo), base="main", head="fix/1-thing", title="Fix thing", body="", token="tok",
    ))
    assert result == {"url": "https://github.com/acme/widgets/pull/5", "number": 5, "created": False}


def test_open_pull_request_no_token_and_no_gh_raises(origin_repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(github_pr.GithubPRError):
        _run_async(github_pr.open_pull_request(
            str(origin_repo), base="main", head="fix/1-thing", title="Fix thing", body="", token="",
        ))


def test_detect_default_branch_from_origin_head(origin_repo, monkeypatch):
    from src import git_panel

    monkeypatch.setattr(
        git_panel, "run_git",
        lambda repo, *args, **kw: subprocess.CompletedProcess(args, 0, "origin/develop\n", "")
        if args[:2] == ("symbolic-ref", "-q") else subprocess.CompletedProcess(args, 1, "", ""),
    )
    assert github_pr.detect_default_branch(str(origin_repo)) == "develop"


def test_detect_default_branch_falls_back_to_main(origin_repo):
    assert github_pr.detect_default_branch(str(origin_repo)) == "main"


# ---------------------------------------------------------------------------
# Tool refusal paths: git_open_pr
# ---------------------------------------------------------------------------
from src import agent_git_policy, constants as constants_mod  # noqa: E402
from src import settings as settings_mod  # noqa: E402
from src import tool_execution as te  # noqa: E402
from src.agent_tools.git_tools import GitOpenPrTool, GithubIssueTool  # noqa: E402

OWNER = "luis"


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(data_dir / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setattr(te, "owner_is_admin_or_single_user", lambda owner: True)
    yield


@pytest.fixture
def bound_workspace(origin_repo):
    ws_token = te._active_workspace.set(str(origin_repo))
    roots_token = te._active_workspace_roots.set((str(origin_repo),))
    try:
        yield origin_repo
    finally:
        te._active_workspace.reset(ws_token)
        te._active_workspace_roots.reset(roots_token)


def test_git_open_pr_refused_when_policy_denies(bound_workspace):
    result = _run_async(GitOpenPrTool().execute(json.dumps({"title": "Fix thing"}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result.get("git_policy_field") == "push"


def test_git_open_pr_refused_when_head_has_no_upstream(bound_workspace):
    agent_git_policy.set_global_policy({"push": True})
    result = _run_async(GitOpenPrTool().execute(json.dumps({"title": "Fix thing"}), {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "git.no_upstream"


def test_git_open_pr_success_path_pushed_head(bound_workspace, monkeypatch):
    _commit_empty(bound_workspace, "init")
    _run_git(["push", "-u", "origin", "main"], cwd=bound_workspace)
    agent_git_policy.set_global_policy({"push": True})

    calls = {}

    async def fake_open_pull_request(workspace, *, base, head, title, body, draft=False, token=None):
        calls.update(base=base, head=head, title=title, body=body)
        return {"url": "https://github.com/acme/widgets/pull/9", "number": 9, "created": True}

    monkeypatch.setattr(github_pr, "open_pull_request", fake_open_pull_request)
    result = _run_async(GitOpenPrTool().execute(
        json.dumps({"title": "Fix thing", "issue_ref": "acme/widgets#42"}), {"owner": OWNER},
    ))
    assert result["exit_code"] == 0, result
    assert result["number"] == 9 and result["created"] is True
    assert calls["base"] == "main" and calls["head"] == "main"
    assert calls["body"].startswith("Closes #42")


def test_git_open_pr_does_not_duplicate_existing_closes_line(bound_workspace, monkeypatch):
    _commit_empty(bound_workspace, "init")
    _run_git(["push", "-u", "origin", "main"], cwd=bound_workspace)
    agent_git_policy.set_global_policy({"push": True})

    calls = {}

    async def fake_open_pull_request(workspace, *, base, head, title, body, draft=False, token=None):
        calls["body"] = body
        return {"url": "https://github.com/acme/widgets/pull/9", "number": 9, "created": True}

    monkeypatch.setattr(github_pr, "open_pull_request", fake_open_pull_request)
    _run_async(GitOpenPrTool().execute(
        json.dumps({"title": "Fix thing", "issue_ref": "acme/widgets#42",
                    "body": "Closes #42\n\nAlready has it."}),
        {"owner": OWNER},
    ))
    assert calls["body"].count("Closes #42") == 1


def test_git_open_pr_requires_title():
    result = _run_async(GitOpenPrTool().execute("{}", {"owner": OWNER}))
    assert result["exit_code"] == 1 and result["error_class"] == "args.missing"


# ---------------------------------------------------------------------------
# Tool: github_issue
# ---------------------------------------------------------------------------
def test_github_issue_tool_success(monkeypatch):
    async def fake_fetch_issue(owner, repo, number, *, token=None):
        assert (owner, repo, number) == ("acme", "widgets", 42)
        return {"title": "Fix thing", "body": "- [ ] step one", "labels": ["bug"], "state": "open",
                "comments": [], "url": "https://github.com/acme/widgets/issues/42"}

    monkeypatch.setattr(github_pr, "fetch_issue", fake_fetch_issue)
    result = _run_async(GithubIssueTool().execute(
        json.dumps({"ref": "acme/widgets#42"}), {"owner": OWNER},
    ))
    assert result["exit_code"] == 0, result
    assert result["issue"]["title"] == "Fix thing"
    assert result["suggested_branch"] == "fix/42-fix-thing"
    assert "step one" in result["brief"]


def test_github_issue_tool_missing_ref():
    result = _run_async(GithubIssueTool().execute("{}", {"owner": OWNER}))
    assert result["exit_code"] == 1 and result["error_class"] == "args.missing"


def test_github_issue_tool_bare_number_without_workspace_fails():
    result = _run_async(GithubIssueTool().execute(json.dumps({"ref": "#5"}), {"owner": OWNER}))
    assert result["exit_code"] == 1 and result["error_class"] == "github.bad_ref"


def test_github_issue_tool_propagates_fetch_error(monkeypatch):
    async def fake_fetch_issue(owner, repo, number, *, token=None):
        raise github_pr.GithubPRError("boom")

    monkeypatch.setattr(github_pr, "fetch_issue", fake_fetch_issue)
    result = _run_async(GithubIssueTool().execute(json.dumps({"ref": "acme/widgets#1"}), {"owner": OWNER}))
    assert result["exit_code"] == 1 and result["error_class"] == "github.failed"
