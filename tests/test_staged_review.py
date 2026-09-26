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
