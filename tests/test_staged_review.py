"""src/staged_review.py — a staged review of a real (throwaway) git worktree.

Cheap deterministic stages (diff, static analysis on added lines only,
related tests) run before any model call, and the model stage — when
configured — is handed their verdicts as tool-confirmed facts. These tests
build real `tmp_path` git repos (never touching the actual project repo) and
exercise the pipeline end to end.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

_HAS_GIT = shutil.which("git") is not None


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=True)


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _has_python_checker(workspace) -> bool:
    from src import static_checks as sc
    return any(".py" in (c.get("exts") or ()) for c in sc.detect_checkers(str(workspace)))


MOD_INITIAL = (
    "import os  # unused, pre-existing\n"
    "\n\n"
    "def add(a, b):\n"
    "    return a + b\n"
)

MOD_MODIFIED = (
    "import os  # unused, pre-existing\n"
    "\n\n"
    "def add(a, b):\n"
    "    return a - b  # bug introduced this turn\n"
    "\n\n"
    "def broken():\n"
    "    return undefined_name_here  # undefined name introduced this turn\n"
)

TEST_MOD = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
    "from src.mod import add\n\n\n"
    "def test_add():\n"
    "    assert add(1, 2) == 3\n"
)


@pytest.fixture
def repo(tmp_path):
    if not _HAS_GIT:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/mod.py", MOD_INITIAL)
    _write(root, "src/__init__.py", "")
    _write(root, "tests/test_mod.py", TEST_MOD)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def dirty_repo(repo):
    """`repo` with the working-tree modification the tests exercise (not committed)."""
    _write(repo, "src/mod.py", MOD_MODIFIED)
    return repo


# ---------------------------------------------------------------------------
# Stage 1 — diff
# ---------------------------------------------------------------------------

def test_diff_stage_lists_changed_file_and_added_lines(dirty_repo):
    from src import staged_review as sr

    res = sr.collect_diff(str(dirty_repo), base="HEAD")
    assert not res.get("error")
    assert "src/mod.py" in res["files"]
    added = res["added_lines"]["src/mod.py"]
    # the rewritten `add` line and the two new `broken()` lines must be added;
    # the pre-existing `import os` line must NOT be (it was not touched).
    modified_lines = MOD_MODIFIED.splitlines()
    bug_line = modified_lines.index("    return a - b  # bug introduced this turn") + 1
    broken_def_line = modified_lines.index("def broken():") + 1
    undefined_line = modified_lines.index("    return undefined_name_here  # undefined name introduced this turn") + 1
    assert bug_line in added
    assert broken_def_line in added
    assert undefined_line in added
    import_line = modified_lines.index("import os  # unused, pre-existing") + 1
    assert import_line not in added


def test_untracked_new_file_appears_as_new_file_diff(dirty_repo):
    from src import staged_review as sr

    _write(dirty_repo, "src/extra.py", "def helper():\n    return 1\n")
    res = sr.collect_diff(str(dirty_repo), base="HEAD")
    assert not res.get("error")
    assert "src/extra.py" in res["files"]
    assert set(res["added_lines"]["src/extra.py"]) == {1, 2}
    assert "src/extra.py" in res["diff"]


def test_base_validation_rejects_unsafe_refs(dirty_repo):
    from src import staged_review as sr

    for bad_base in ("--output=x", "; rm", "-x"):
        res = sr.collect_diff(str(dirty_repo), base=bad_base)
        assert res.get("error"), f"expected an error for base={bad_base!r}"
        assert "src/mod.py" not in res["files"]


def test_not_a_git_repo_returns_error_without_raising(tmp_path):
    from src import staged_review as sr

    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")
    res = sr.collect_diff(str(plain), base="HEAD")
    assert res.get("error")
    assert res["files"] == []


# ---------------------------------------------------------------------------
# Stage 2 — static analysis, added lines only
# ---------------------------------------------------------------------------

def test_static_stage_reports_new_undefined_name_not_the_preexisting_issue(dirty_repo):
    if not _has_python_checker(dirty_repo):
        pytest.skip("neither ruff nor pyflakes is available")
    from src import staged_review as sr

    diff_res = sr.collect_diff(str(dirty_repo), base="HEAD")
    static_res = sr.run_static(str(dirty_repo), diff_res["files"], diff_res["added_lines"])
    assert static_res["ran"] is True
    codes = {f["code"] for f in static_res["findings"]}
    assert "F821" in codes
    for f in static_res["findings"]:
        assert f["file"] == "src/mod.py"
        assert f["code"] != "F401"          # the pre-existing unused import
        assert f["severity"] in ("error", "warning")
        if f["code"] == "F821":
            assert f["severity"] == "error"


# ---------------------------------------------------------------------------
# Stage 3 — only the related tests
# ---------------------------------------------------------------------------

def test_tests_stage_runs_related_test_and_reports_failure(dirty_repo):
    from src import staged_review as sr

    diff_res = sr.collect_diff(str(dirty_repo), base="HEAD")
    tests_res = sr.run_affected_tests(str(dirty_repo), diff_res["files"], timeout=60)
    assert tests_res["ran"] is True
    assert tests_res["ok"] is False
    assert any("test_add" in fid for fid in tests_res["failing"])
    assert any(f["source"] == "tests" for f in tests_res["findings"])


def test_tests_stage_reports_no_related_tests_when_nothing_matches(repo):
    from src import staged_review as sr

    _write(repo, "docs/notes.md", "hello\n")
    res = sr.run_affected_tests(str(repo), ["docs/notes.md"], timeout=30)
    assert res["ran"] is False
    assert res.get("reason") == "no related tests"


# ---------------------------------------------------------------------------
# Stage 4 — the model, told what the tools already proved
# ---------------------------------------------------------------------------

async def test_model_stage_receives_static_findings_and_merges_grounded_results(dirty_repo, monkeypatch):
    from src import auto_review, staged_review as sr

    captured = {}

    async def fake_call_reviewer(*, diff, files, user_text, endpoint_url, reviewer, headers, tests, timeout, workload):
        captured["diff"] = diff
        captured["files"] = files
        captured["tests"] = tests
        parsed = {
            "verdict": "issues",
            "summary": "one real defect",
            "findings": [{
                "severity": "error", "file": "src/mod.py", "line": None,
                "issue": "add() now subtracts instead of adding, contradicting its name",
                "evidence": "return a - b  # bug introduced this turn",
                "grounded": True,
            }],
            "ungrounded": 0,
        }
        return parsed, None

    monkeypatch.setattr(auto_review, "_call_reviewer", fake_call_reviewer)

    result = await sr.review_worktree(
        str(dirty_repo), base="HEAD", request="fix add() if needed",
        endpoint_url="http://127.0.0.1:9/v1/chat/completions", model="test-model",
        stages=("diff", "static", "tests", "model"),
    )

    assert result["stages"]["model"]["ran"] is True
    assert "diff" in captured and captured["diff"]
    # the augmented diff handed to the reviewer must carry the static stage's
    # tool-confirmed facts, not just the raw diff.
    if _has_python_checker(dirty_repo):
        assert "Static analysis" in captured["diff"]
    sources = {f["source"] for f in result["findings"]}
    assert "model" in sources
    model_findings = [f for f in result["findings"] if f["source"] == "model"]
    assert model_findings and model_findings[0]["issue"].startswith("add() now subtracts")
    assert result["verdict"] == "issues"
    assert "**model**" in result["markdown"]
    assert "**diff**" in result["markdown"]
    assert "**static**" in result["markdown"]
    assert "**tests**" in result["markdown"]


async def test_model_stage_is_skipped_without_endpoint_or_model(dirty_repo):
    from src import staged_review as sr

    result = await sr.review_worktree(str(dirty_repo), base="HEAD", stages=("diff", "static", "tests", "model"))
    assert result["stages"]["model"]["ran"] is False
    assert all(f["source"] != "model" for f in result["findings"])


async def test_review_worktree_not_a_repo_is_an_error_verdict(tmp_path):
    from src import staged_review as sr

    plain = tmp_path / "not_a_repo2"
    plain.mkdir()
    result = await sr.review_worktree(str(plain), base="HEAD")
    assert result["verdict"] == "error"
    assert result["stages"]["diff"]["ok"] is False
    assert "error" in result["stages"]["diff"]


def test_route_is_admin_only_and_never_takes_an_endpoint_from_the_body(tmp_path, monkeypatch):
    """The stages run the project's tooling (its code): admins only. The diff
    only goes to the owner's own endpoints, never to a URL in the body."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import routes.staged_review_routes as rr
    from src import staged_review

    seen = {}

    async def fake_review(workspace, **kw):
        seen.update(kw)
        return {"stages": {}, "findings": [], "verdict": "ok", "markdown": ""}
    monkeypatch.setattr(staged_review, "review_worktree", fake_review)
    monkeypatch.setattr(rr, "_resolve_model", lambda owner, wanted: ("http://own-endpoint/v1", "m", {}))
    app = FastAPI()
    app.include_router(rr.setup_staged_review_routes())
    app.dependency_overrides[rr.require_user] = lambda: "luis"
    client = TestClient(app)

    def deny():
        raise HTTPException(403, "Admin only")
    app.dependency_overrides[rr.require_admin] = deny
    assert client.post("/api/review/worktree", json={"workspace": str(tmp_path)}).status_code == 403
    app.dependency_overrides[rr.require_admin] = lambda: None
    r = client.post("/api/review/worktree", json={"workspace": str(tmp_path), "endpoint_url": "http://evil/v1"})
    assert r.status_code == 200, r.text
    assert seen["endpoint_url"] == "http://own-endpoint/v1" and "tests" in seen["stages"]
    assert client.post("/api/review/worktree", json={"workspace": "relative/path"}).status_code == 400


def test_model_stage_without_a_request_is_told_so(monkeypatch):
    import asyncio
    from src import auto_review, staged_review
    seen = {}

    async def fake_call(**kw):
        seen.update(kw)
        return {"verdict": "ok", "summary": "", "findings": []}, None
    monkeypatch.setattr(auto_review, "_call_reviewer", fake_call)
    asyncio.run(staged_review._run_model_stage("/tmp", {"diff": "+x = 1\n", "files": ["a.py"]}, None, None, "",
                                               "http://e/v1", "m", None, 10.0))
    assert seen["user_text"] == staged_review.NO_REQUEST


@pytest.fixture
def reviewer_transport(monkeypatch):
    """Only the completion transport is private; parsing/grounding stay real."""
    from src import llm_core
    calls = []

    def install(reply):
        async def complete(**kwargs):
            calls.append(kwargs)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        monkeypatch.setattr(llm_core, "llm_call_async", complete)
        return calls
    return install


@pytest.mark.parametrize("prior_findings", [False, True])
@pytest.mark.parametrize("reply,error", [
    ("", "the reviewer returned an empty answer"),
    ("not a review JSON document", "the reviewer returned an unparsed review"),
    (TimeoutError(), "review timed out after 37 s"),
])
async def test_failed_model_review_is_error_and_retains_prior_findings(
        dirty_repo, reviewer_transport, reply, error, prior_findings):
    from src import staged_review as sr
    calls = reviewer_transport(reply)
    stages = ("diff", "static", "model") if prior_findings else ("diff", "model")
    result = await sr.review_worktree(str(dirty_repo), stages=stages, request="Return the sum.",
        endpoint_url="https://reviewer-fixture.invalid/v1", model="private-reviewer", timeout_s=7)
    stage = result["stages"]["model"]
    assert stage["ran"] is False and stage["ok"] is None and stage["error"] == error
    assert result["verdict"] == "error"
    assert "clean across" not in result["summary"]
    assert error in result["summary"]
    assert "**model**: error" in result["markdown"] and error in result["markdown"]
    if prior_findings:
        assert result["findings"] == result["stages"]["static"]["findings"]
        assert any(f["code"] == "F821" for f in result["findings"])
        assert "## Findings" in result["markdown"]
    else:
        assert result["findings"] == []
    assert len(calls) == 1
    assert calls[0]["max_retries"] == 1 and calls[0]["temperature"] == 0.1
    assert calls[0]["max_tokens"] == 1200 and calls[0]["workload"] == "foreground"


@pytest.mark.parametrize("issues", [False, True])
async def test_valid_model_review_preserves_clean_and_issues(dirty_repo, reviewer_transport, issues):
    import json
    from src import staged_review as sr
    findings = ([{"severity": "error", "file": "src/mod.py", "line": 5,
                  "issue": "Subtraction contradicts the requested sum", "evidence": "return a - b"}]
                if issues else [])
    calls = reviewer_transport(json.dumps({"verdict": "issues" if issues else "ok",
        "summary": "Actual private response", "findings": findings}))
    result = await sr.review_worktree(str(dirty_repo), stages=("diff", "model"), request="Return the sum.",
        endpoint_url="https://reviewer-fixture.invalid/v1", model="private-reviewer")
    assert result["verdict"] == ("issues" if issues else "ok")
    assert result["stages"]["model"]["ran"] is True
    assert "error" not in result["stages"]["model"]
    assert len(calls) == 1
    if issues:
        assert result["findings"][0]["grounded"] is True
        assert "## Findings" in result["markdown"]
    else:
        assert result["summary"] == "clean across 1 file(s)"
        assert result["findings"] == []


@pytest.mark.parametrize("configured", [False, True])
async def test_model_skip_is_not_promoted_to_error(dirty_repo, reviewer_transport, configured):
    from src import staged_review as sr
    calls = reviewer_transport(AssertionError("a skipped stage must not call the transport"))
    result = await sr.review_worktree(str(dirty_repo),
        stages=("diff",) if configured else ("diff", "model"),
        endpoint_url="https://reviewer-fixture.invalid/v1" if configured else None,
        model="private-reviewer" if configured else None)
    assert result["verdict"] == "ok" and result["findings"] == []
    assert result["stages"]["model"]["ran"] is False
    assert result["stages"]["model"]["reason"] == (
        "stage skipped" if configured else "no model/endpoint configured")
    assert "**model**: skipped" in result["markdown"] and calls == []


@pytest.mark.parametrize("stage_name", ["static", "tests"])
async def test_explicit_stage_exception_survives_a_clean_later_model(
        dirty_repo, monkeypatch, reviewer_transport, stage_name):
    from src import staged_review as sr
    def unavailable(*args, **kwargs):
        raise OSError("private checker unavailable")
    monkeypatch.setattr(sr, "run_static" if stage_name == "static" else "run_affected_tests", unavailable)
    calls = reviewer_transport('{"verdict":"ok","summary":"No model findings","findings":[]}')
    result = await sr.review_worktree(str(dirty_repo), stages=("diff", stage_name, "model"),
        endpoint_url="https://reviewer-fixture.invalid/v1", model="private-reviewer")
    assert result["verdict"] == "error" and result["findings"] == []
    assert result["stages"]["model"]["ran"] is True and len(calls) == 1
    assert result["stages"][stage_name]["error"] == "OSError: private checker unavailable"
    assert "private checker unavailable" in result["summary"]
    assert f"**{stage_name}**: error" in result["markdown"]
