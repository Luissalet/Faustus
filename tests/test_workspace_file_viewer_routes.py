"""File viewer routes behind the chat's "Edited N files" chips: confined to the
bound workspace, text + git diff, admin gate."""

import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.workspace_routes as wr


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    return TestClient(app)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    (tmp_path / "src" / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("hello\n", encoding="utf-8")
    return tmp_path


def test_file_and_diff_inside_workspace(client, repo):
    r = client.get("/api/workspace/file", params={"workspace": str(repo), "path": "src/a.py"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["text"] == "x = 1\ny = 2\n" and d["lines"] == 2 and d["rel"] == "src/a.py" and not d["binary"]
    r = client.get("/api/workspace/file_diff", params={"workspace": str(repo), "path": "src/a.py"})
    assert r.status_code == 200
    d = r.json()
    assert d["git"] is True and "+y = 2" in d["diff"] and d["status"] == "M"
    # untracked file → whole file as added
    r = client.get("/api/workspace/file_diff", params={"workspace": str(repo), "path": "new.txt"})
    assert r.status_code == 200 and "+hello" in r.json()["diff"]


def test_file_outside_workspace_is_refused(client, repo, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    r = client.get("/api/workspace/file", params={"workspace": str(repo), "path": str(outside)})
    assert r.status_code == 400
    r = client.get("/api/workspace/file", params={"workspace": str(repo), "path": "../outside.txt"})
    assert r.status_code == 400
    r = client.get("/api/workspace/file", params={"workspace": str(repo), "path": "src/missing.py"})
    assert r.status_code == 404


def test_non_admin_is_refused(monkeypatch, repo):
    monkeypatch.setattr(wr, "get_current_user", lambda request: "bob")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: False)
    app = FastAPI()
    app.include_router(wr.setup_workspace_routes())
    c = TestClient(app)
    assert c.get("/api/workspace/file", params={"workspace": str(repo), "path": "src/a.py"}).status_code == 403


def test_revert_restores_tracked_and_deletes_untracked(client, repo):
    r = client.post("/api/workspace/revert", params={"workspace": str(repo), "path": "src/a.py"})
    assert r.status_code == 200 and r.json()["action"] == "restored"
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    r = client.post("/api/workspace/revert", params={"workspace": str(repo), "path": "new.txt"})
    assert r.status_code == 200 and r.json()["action"] == "deleted_untracked"
    assert not (repo / "new.txt").exists()
    r = client.post("/api/workspace/revert", params={"workspace": str(repo), "path": "src/a.py"})
    assert r.json()["action"] == "unchanged"


def test_revert_refuses_outside_git(client, tmp_path):
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "f.txt").write_text("x", encoding="utf-8")
    r = client.post("/api/workspace/revert", params={"workspace": str(ws), "path": "f.txt"})
    assert r.status_code == 400
    assert (ws / "f.txt").exists()
