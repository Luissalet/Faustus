"""Lote 92 (OBJ-6) -- ``/api/projects/{project_id}/board/*`` HTTP surface.

Modeled on tests/test_l82_git_create_repo.py's FastAPI TestClient pattern:
monkeypatch effective_user for owner-header-based auth, and monkeypatch
services.projects' module-level store to a tmp ProjectStore so board_key
lookups and project ownership checks run against real (isolated) data.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes import board_routes  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from src import constants as constants_mod  # noqa: E402
from src import project_board  # noqa: E402

OWNER = "luis"
OTHER = "mallory"


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    yield


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    monkeypatch.setattr(board_routes, "get_project_store", lambda: st)
    return st


@pytest.fixture()
def client(store, monkeypatch):
    def _owner_from_header(request):
        return request.headers.get("x-test-owner") or None
    monkeypatch.setattr(board_routes, "effective_user", _owner_from_header)
    app = FastAPI()
    app.include_router(board_routes.setup_board_routes())
    return TestClient(app)


def _hdr(owner=OWNER):
    return {"x-test-owner": owner}


def _project(store, tmp_path, *, owner=OWNER, name="Faustus"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return store.create(name=name, folder=name, workspace=str(root),
                         owner=owner, scaffold_memory=False)


# ---------------------------------------------------------------------------
# Ownership / not-found
# ---------------------------------------------------------------------------
def test_unknown_project_is_404(client):
    resp = client.get("/api/projects/does-not-exist/board/issues", headers=_hdr())
    assert resp.status_code == 404


def test_another_owners_project_is_404_not_403(client, store, tmp_path):
    project = _project(store, tmp_path, owner=OWNER)
    resp = client.get(f"/api/projects/{project['id']}/board/issues", headers=_hdr(OTHER))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Create / list / get
# ---------------------------------------------------------------------------
def test_create_list_get_issue_round_trip(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]

    resp = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "bug", "title": "retry leak", "body_md": "sessions never expire",
    }, headers=_hdr())
    assert resp.status_code == 201, resp.text
    issue = resp.json()["issue"]
    assert issue["id"].startswith("FAU-")
    assert issue["status"] == "open"

    resp = client.get(f"/api/projects/{pid}/board/issues", headers=_hdr())
    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["issues"]]
    assert issue["id"] in ids

    resp = client.get(f"/api/projects/{pid}/board/issues/{issue['id']}", headers=_hdr())
    assert resp.status_code == 200
    assert resp.json()["issue"]["body_md"] == "sessions never expire"


def test_get_missing_issue_is_404_with_error_class(client, store, tmp_path):
    project = _project(store, tmp_path)
    resp = client.get(f"/api/projects/{project['id']}/board/issues/FAU-999", headers=_hdr())
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "board.not_found"


def test_issue_from_another_project_is_404_here(client, store, tmp_path):
    p1 = _project(store, tmp_path, name="Faustus")
    p2 = _project(store, tmp_path, name="Marlowe")
    r = client.post(f"/api/projects/{p1['id']}/board/issues", json={
        "type": "task", "title": "only in p1",
    }, headers=_hdr())
    issue_id = r.json()["issue"]["id"]
    resp = client.get(f"/api/projects/{p2['id']}/board/issues/{issue_id}", headers=_hdr())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Update / invalid transition
# ---------------------------------------------------------------------------
def test_update_issue_status(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    issue_id = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "task", "title": "x",
    }, headers=_hdr()).json()["issue"]["id"]

    resp = client.patch(f"/api/projects/{pid}/board/issues/{issue_id}",
                         json={"status": "in_progress"}, headers=_hdr())
    assert resp.status_code == 200
    assert resp.json()["issue"]["status"] == "in_progress"


def test_update_with_empty_body_is_400(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    issue_id = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "task", "title": "x",
    }, headers=_hdr()).json()["issue"]["id"]
    resp = client.patch(f"/api/projects/{pid}/board/issues/{issue_id}", json={}, headers=_hdr())
    assert resp.status_code == 400


def test_invalid_status_transition_from_terminal_is_400(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    issue_id = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "task", "title": "x",
    }, headers=_hdr()).json()["issue"]["id"]
    client.patch(f"/api/projects/{pid}/board/issues/{issue_id}", json={"status": "wontfix"}, headers=_hdr())
    # terminal -> terminal (not a reopen to open/in_progress) is refused
    resp = client.patch(f"/api/projects/{pid}/board/issues/{issue_id}",
                         json={"status": "duplicate"}, headers=_hdr())
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "board.invalid_transition"


# ---------------------------------------------------------------------------
# Claim -- 409 on conflict
# ---------------------------------------------------------------------------
def test_claim_then_conflicting_claim_is_409(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    issue_id = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "task", "title": "x",
    }, headers=_hdr()).json()["issue"]["id"]

    resp = client.post(f"/api/projects/{pid}/board/issues/{issue_id}/claim",
                        json={"assignee": "alice"}, headers=_hdr())
    assert resp.status_code == 200
    assert resp.json()["issue"]["assignee"] == "alice"
    assert resp.json()["issue"]["status"] == "in_progress"

    resp = client.post(f"/api/projects/{pid}/board/issues/{issue_id}/claim",
                        json={"assignee": "bob"}, headers=_hdr())
    assert resp.status_code == 409
    assert resp.json()["error_class"] == "board.claimed"


# ---------------------------------------------------------------------------
# Comments / links / refs
# ---------------------------------------------------------------------------
def test_add_comment(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    issue_id = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "task", "title": "x",
    }, headers=_hdr()).json()["issue"]["id"]
    resp = client.post(f"/api/projects/{pid}/board/issues/{issue_id}/comments",
                        json={"body_md": "looking into it"}, headers=_hdr())
    assert resp.status_code == 201
    assert resp.json()["comment"]["body_md"] == "looking into it"


def test_add_and_remove_link(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    a = client.post(f"/api/projects/{pid}/board/issues", json={"type": "task", "title": "a"}, headers=_hdr()).json()["issue"]["id"]
    b = client.post(f"/api/projects/{pid}/board/issues", json={"type": "task", "title": "b"}, headers=_hdr()).json()["issue"]["id"]

    resp = client.post(f"/api/projects/{pid}/board/issues/{a}/links",
                        json={"kind": "blocks", "target": b}, headers=_hdr())
    assert resp.status_code == 201
    link_id = resp.json()["link"]["id"]

    # mirrored: b is now blocked_by a
    b_full = client.get(f"/api/projects/{pid}/board/issues/{b}", headers=_hdr()).json()["issue"]
    assert a in b_full.get("blocked_by", [])

    resp = client.delete(f"/api/projects/{pid}/board/issues/{a}/links/{link_id}", headers=_hdr())
    assert resp.status_code == 200


def test_add_ref(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    issue_id = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "task", "title": "x",
    }, headers=_hdr()).json()["issue"]["id"]
    resp = client.post(f"/api/projects/{pid}/board/issues/{issue_id}/refs",
                        json={"kind": "url", "value": "https://example.com/x", "label": "spec"},
                        headers=_hdr())
    assert resp.status_code == 201
    assert resp.json()["ref"]["value"] == "https://example.com/x"


def test_delete_issue(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    issue_id = client.post(f"/api/projects/{pid}/board/issues", json={
        "type": "task", "title": "x",
    }, headers=_hdr()).json()["issue"]["id"]
    resp = client.delete(f"/api/projects/{pid}/board/issues/{issue_id}", headers=_hdr())
    assert resp.status_code == 200
    resp = client.get(f"/api/projects/{pid}/board/issues/{issue_id}", headers=_hdr())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Ready / summary / export
# ---------------------------------------------------------------------------
def test_ready_excludes_blocked_issues(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    a = client.post(f"/api/projects/{pid}/board/issues", json={"type": "task", "title": "a"}, headers=_hdr()).json()["issue"]["id"]
    b = client.post(f"/api/projects/{pid}/board/issues", json={"type": "task", "title": "b"}, headers=_hdr()).json()["issue"]["id"]
    client.post(f"/api/projects/{pid}/board/issues/{b}/links", json={"kind": "blocked_by", "target": a}, headers=_hdr())

    resp = client.get(f"/api/projects/{pid}/board/ready", headers=_hdr())
    ready_ids = [i["id"] for i in resp.json()["issues"]]
    assert a in ready_ids
    assert b not in ready_ids


def test_summary_includes_key(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    resp = client.get(f"/api/projects/{pid}/board/summary", headers=_hdr())
    assert resp.status_code == 200
    assert resp.json()["key"] == "FAU"


def test_export_md_is_plaintext(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    client.post(f"/api/projects/{pid}/board/issues", json={"type": "task", "title": "export me"}, headers=_hdr())
    resp = client.get(f"/api/projects/{pid}/board/export.md", headers=_hdr())
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "export me" in resp.text


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def test_import_dry_run_reports_without_creating(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    workspace = project["workspace"]
    with open(os.path.join(workspace, "OBJETIVOS.md"), "w", encoding="utf-8") as f:
        f.write("# Objetivos\n\n## OBJ-1: Ship the board\n\nGet the backend working end to end.\n")

    resp = client.post(f"/api/projects/{pid}/board/import",
                        json={"sources": ["objetivos"], "dry_run": True}, headers=_hdr())
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 0
    assert len(body["preview"]) == 1

    resp = client.get(f"/api/projects/{pid}/board/issues", headers=_hdr())
    assert resp.json()["issues"] == []  # dry run creates nothing


def test_import_then_reimport_is_idempotent(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    workspace = project["workspace"]
    with open(os.path.join(workspace, "OBJETIVOS.md"), "w", encoding="utf-8") as f:
        f.write("# Objetivos\n\n## OBJ-1: Ship the board\n\nGet the backend working end to end.\n")

    client.post(f"/api/projects/{pid}/board/import",
                json={"sources": ["objetivos"], "dry_run": False}, headers=_hdr())
    first = client.get(f"/api/projects/{pid}/board/issues", headers=_hdr()).json()["issues"]
    assert len(first) == 1

    client.post(f"/api/projects/{pid}/board/import",
                json={"sources": ["objetivos"], "dry_run": False}, headers=_hdr())
    second = client.get(f"/api/projects/{pid}/board/issues", headers=_hdr()).json()["issues"]
    assert len(second) == 1  # no duplicate on re-import


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------
def test_set_key_changes_future_ids(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    resp = client.put(f"/api/projects/{pid}/board/key", json={"key": "TASK"}, headers=_hdr())
    assert resp.status_code == 200
    assert resp.json()["key"] == "TASK"

    resp = client.post(f"/api/projects/{pid}/board/issues", json={"type": "task", "title": "x"}, headers=_hdr())
    assert resp.json()["issue"]["id"].startswith("TASK-")


def test_set_key_rejects_invalid_format(client, store, tmp_path):
    project = _project(store, tmp_path)
    pid = project["id"]
    # within the field's 2-5 char length bound but not 2-5 uppercase letters
    resp = client.put(f"/api/projects/{pid}/board/key", json={"key": "ab1"}, headers=_hdr())
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "board.invalid_key"
