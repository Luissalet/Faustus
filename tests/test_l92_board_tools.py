"""Lote 92 (OBJ-6) -- agent-facing board tools: src/agent_tools/board_tools.py.

Direct `await Tool().execute(content, ctx)` calls, same pattern as
tests/test_l87_git_tools.py's `_run_async` helper. No git/subprocess needed
here -- these tools sit directly on `src.project_board`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import projects as projects_mod  # noqa: E402
from src import constants as constants_mod, project_board  # noqa: E402
from src.agent_tools.board_tools import (  # noqa: E402
    BoardClaimTool, BoardCommentTool, BoardCreateTool, BoardGetTool,
    BoardLinkTool, BoardListTool, BoardReadyTool, BoardUpdateTool,
)

OWNER = "luis"


def _run_async(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    yield


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


@pytest.fixture()
def project(store, tmp_path):
    root = tmp_path / "Faustus"
    root.mkdir()
    return store.create(name="Faustus", folder="Faustus", workspace=str(root), owner=OWNER,
                         scaffold_memory=False)


def _ctx(project_id, owner=OWNER):
    return {"owner": owner, "project_id": project_id}


# ---------------------------------------------------------------------------
# No-project refusal -- every tool, not just one
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool_cls,content", [
    (BoardListTool, "{}"),
    (BoardReadyTool, "{}"),
    (BoardGetTool, json.dumps({"id": "FAU-1"})),
    (BoardCreateTool, json.dumps({"title": "x"})),
    (BoardUpdateTool, json.dumps({"id": "FAU-1", "status": "done"})),
    (BoardCommentTool, json.dumps({"id": "FAU-1", "body": "hi"})),
    (BoardLinkTool, json.dumps({"id": "FAU-1", "kind": "blocks", "target": "FAU-2"})),
    (BoardClaimTool, json.dumps({"id": "FAU-1"})),
])
def test_every_tool_refuses_without_a_project(tool_cls, content):
    result = _run_async(tool_cls().execute(content, {"owner": OWNER}))
    assert result["exit_code"] == 1
    assert result["error_class"] == "board.no_project"


# ---------------------------------------------------------------------------
# Create / list / get / ready
# ---------------------------------------------------------------------------
def test_create_then_list_and_get(project):
    ctx = _ctx(project["id"])
    created = _run_async(BoardCreateTool().execute(
        json.dumps({"type": "bug", "title": "retry leak", "body": "sessions leak"}), ctx))
    assert created["exit_code"] == 0
    issue_id = created["issue"]["id"]
    assert issue_id.startswith("FAU-")

    listed = _run_async(BoardListTool().execute("{}", ctx))
    assert issue_id in [i["id"] for i in listed["issues"]]

    got = _run_async(BoardGetTool().execute(json.dumps({"id": issue_id}), ctx))
    assert got["exit_code"] == 0
    assert got["issue"]["body_md"] == "sessions leak"


def test_create_requires_title(project):
    result = _run_async(BoardCreateTool().execute(json.dumps({"type": "task"}), _ctx(project["id"])))
    assert result["exit_code"] == 1
    assert "title" in result["error"]


def test_ready_excludes_blocked(project):
    ctx = _ctx(project["id"])
    a = _run_async(BoardCreateTool().execute(json.dumps({"title": "a"}), ctx))["issue"]["id"]
    b = _run_async(BoardCreateTool().execute(json.dumps({"title": "b"}), ctx))["issue"]["id"]
    _run_async(BoardLinkTool().execute(
        json.dumps({"id": b, "kind": "blocked_by", "target": a}), ctx))

    ready = _run_async(BoardReadyTool().execute("{}", ctx))
    ready_ids = [i["id"] for i in ready["issues"]]
    assert a in ready_ids
    assert b not in ready_ids


# ---------------------------------------------------------------------------
# Update / claim / comment
# ---------------------------------------------------------------------------
def test_update_changes_status(project):
    ctx = _ctx(project["id"])
    issue_id = _run_async(BoardCreateTool().execute(json.dumps({"title": "x"}), ctx))["issue"]["id"]
    result = _run_async(BoardUpdateTool().execute(
        json.dumps({"id": issue_id, "status": "in_progress"}), ctx))
    assert result["exit_code"] == 0
    assert result["issue"]["status"] == "in_progress"


def test_update_invalid_transition_is_refused(project):
    ctx = _ctx(project["id"])
    issue_id = _run_async(BoardCreateTool().execute(json.dumps({"title": "x"}), ctx))["issue"]["id"]
    _run_async(BoardUpdateTool().execute(json.dumps({"id": issue_id, "status": "wontfix"}), ctx))
    result = _run_async(BoardUpdateTool().execute(
        json.dumps({"id": issue_id, "status": "duplicate"}), ctx))
    assert result["exit_code"] == 1
    assert result["error_class"] == "board.invalid_transition"


def test_claim_then_conflicting_claim_is_refused(project):
    ctx = _ctx(project["id"])
    issue_id = _run_async(BoardCreateTool().execute(json.dumps({"title": "x"}), ctx))["issue"]["id"]
    first = _run_async(BoardClaimTool().execute(
        json.dumps({"id": issue_id, "assignee": "alice"}), ctx))
    assert first["exit_code"] == 0
    assert first["issue"]["status"] == "in_progress"

    second = _run_async(BoardClaimTool().execute(
        json.dumps({"id": issue_id, "assignee": "bob"}), ctx))
    assert second["exit_code"] == 1
    assert second["error_class"] == "board.claimed"


def test_comment_is_recorded(project):
    ctx = _ctx(project["id"])
    issue_id = _run_async(BoardCreateTool().execute(json.dumps({"title": "x"}), ctx))["issue"]["id"]
    result = _run_async(BoardCommentTool().execute(
        json.dumps({"id": issue_id, "body": "on it"}), ctx))
    assert result["exit_code"] == 0
    got = _run_async(BoardGetTool().execute(json.dumps({"id": issue_id}), ctx))
    assert got["issue"]["comments"][0]["body_md"] == "on it"


# ---------------------------------------------------------------------------
# Cross-project isolation -- a tool must never touch another project's issue
# ---------------------------------------------------------------------------
def test_tools_never_reach_across_projects(store, project, tmp_path):
    other_root = tmp_path / "Marlowe"
    other_root.mkdir()
    store.create(name="Marlowe", folder="Marlowe", workspace=str(other_root), owner=OWNER,
                 scaffold_memory=False)
    projects = {p["name"]: p["id"] for p in store.list(owner=OWNER)}
    faustus_id = projects["Faustus"]
    marlowe_id = projects["Marlowe"]

    foreign_issue = _run_async(BoardCreateTool().execute(
        json.dumps({"title": "only in marlowe"}), _ctx(marlowe_id)))["issue"]["id"]

    for tool_cls, content in [
        (BoardGetTool, json.dumps({"id": foreign_issue})),
        (BoardUpdateTool, json.dumps({"id": foreign_issue, "status": "done"})),
        (BoardCommentTool, json.dumps({"id": foreign_issue, "body": "hi"})),
        (BoardClaimTool, json.dumps({"id": foreign_issue})),
        (BoardLinkTool, json.dumps({"id": foreign_issue, "kind": "relates_to", "target": foreign_issue})),
    ]:
        result = _run_async(tool_cls().execute(content, _ctx(faustus_id)))
        assert result["exit_code"] == 1
        assert result["error_class"] == "board.not_found"

    # the issue itself is untouched
    still_open = project_board.get(foreign_issue)
    assert still_open["status"] == "open"
