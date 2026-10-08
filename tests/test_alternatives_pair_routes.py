"""tests/test_alternatives_pair_routes.py -- OBJ-47: the pairwise comparison
over HTTP (`GET /api/projects/{pid}/alternatives/{exp}/compare/{a}/{b}`),
through the real router: status codes, error vocabulary, query-parameter
bounds, owner scoping, and that the existing `/compare` still answers.

Run: python -m pytest tests/test_alternatives_pair_routes.py -q -p no:cacheprovider -W ignore
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import alternatives  # noqa: E402
from src import constants as constants_mod  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

OWNER = "luis"


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _write(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path_factory, monkeypatch):
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(tmp_path_factory.mktemp("data")))
    yield


@pytest.fixture()
def project_store(tmp_path, monkeypatch):
    from services import projects as projects_mod
    st = projects_mod.ProjectStore(str(tmp_path / "projects_data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(projects_mod, "get_store", lambda: st)
    return st


@pytest.fixture()
def client(project_store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import alternatives_routes

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(alternatives_routes, "effective_user", lambda request: request.headers.get("x-test-owner") or None)
    monkeypatch.setattr(alternatives_routes, "get_project_store", lambda: project_store)
    app = FastAPI()
    app.include_router(alternatives_routes.setup_alternatives_routes())
    return TestClient(app)


@pytest.fixture()
def world(tmp_path, project_store, client):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.com"], root)
    lines = "".join(f"l{i}\n" for i in range(1, 11))
    _write(root / "a.txt", lines)
    _git(["add", "-A"], root)
    _git(["commit", "-m", "init"], root)
    project = project_store.create(name="P", folder="P", workspace=str(root), owner=OWNER, scaffold_memory=False)
    exp = alternatives.create_experiment(OWNER, project["id"], "goal", str(root))
    a = alternatives.add_alternative(OWNER, exp["id"], "A")
    b = alternatives.add_alternative(OWNER, exp["id"], "B")
    _write(os.path.join(a["path"], "a.txt"), lines.replace("l2\n", "L2\n"))
    _write(os.path.join(b["path"], "a.txt"), lines.replace("l9\n", "L9\n"))
    _write(os.path.join(b["path"], "extra.txt"), "x\n")
    base = f"/api/projects/{project['id']}/alternatives/{exp['id']}"
    return {"base": base, "a": a["id"], "b": b["id"], "exp": exp["id"], "project": project["id"]}


H = {"x-test-owner": OWNER}


def test_pair_route_returns_the_diff_and_the_overlap(client, world):
    resp = client.get(f"{world['base']}/compare/{world['a']}/{world['b']}", headers=H)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["a"]["id"] == world["a"] and body["b"]["id"] == world["b"]
    files = {f["path"]: f for f in body["files"]}
    assert set(files) == {"a.txt", "extra.txt"}
    assert files["a.txt"]["overlap"] == "mergeable"
    assert files["extra.txt"]["status"] == "added"
    assert body["identical"] is False
    assert body["limits"]["max_files"] == alternatives.MAX_PAIR_FILES


def test_pair_route_honours_the_limit_parameters(client, world):
    resp = client.get(f"{world['base']}/compare/{world['a']}/{world['b']}",
                      params={"max_files": 1, "max_diff_lines": 20, "max_file_bytes": 2000}, headers=H)
    assert resp.status_code == 200
    body = resp.json()
    assert body["limits"] == {**body["limits"], "max_files": 1, "max_diff_lines_per_file": 20, "max_file_bytes": 2000}
    assert len(body["files"]) == 1
    assert body["truncation"]["files"] is True and body["truncation"]["files_omitted"] == 1


@pytest.mark.parametrize("param,value", [("max_files", 0), ("max_files", 99999), ("max_diff_lines", 1), ("max_file_bytes", 5)])
def test_pair_route_rejects_out_of_range_limits(client, world, param, value):
    resp = client.get(f"{world['base']}/compare/{world['a']}/{world['b']}", params={param: value}, headers=H)
    assert resp.status_code == 422


def test_same_alternative_twice_is_a_400_with_error_class(client, world):
    resp = client.get(f"{world['base']}/compare/{world['a']}/{world['a']}", headers=H)
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "alternatives.invalid_request"


def test_unknown_alternative_is_a_404(client, world):
    resp = client.get(f"{world['base']}/compare/{world['a']}/alt-doesnotexist", headers=H)
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "alternatives.alt_not_found"


def test_another_owner_cannot_compare(client, world):
    resp = client.get(f"{world['base']}/compare/{world['a']}/{world['b']}", headers={"x-test-owner": "mallory"})
    assert resp.status_code in (404,)


def test_wrong_project_is_a_404(client, world, project_store):
    other = project_store.create(name="Q", folder="Q", workspace="", owner=OWNER, scaffold_memory=False)
    url = f"/api/projects/{other['id']}/alternatives/{world['exp']}/compare/{world['a']}/{world['b']}"
    assert client.get(url, headers=H).status_code == 404


def test_the_base_compare_route_still_answers(client, world):
    resp = client.get(f"{world['base']}/compare", headers=H)
    assert resp.status_code == 200
    body = resp.json()
    assert [a["id"] for a in body["alternatives"]] == [world["a"], world["b"]]
    assert "a.txt" in body["contested_files"]
