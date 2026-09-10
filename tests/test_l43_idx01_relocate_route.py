"""L43 · IDX-01 UI — POST /api/projects/{id}/relocate (routes/project_routes.py),
wired to src.project_identity.relocate.

Before this lote the only way to point a project at a new folder was
`PATCH /api/projects/{id}` with a bare `workspace` field — which never
refuses an absent path and never writes/refreshes the destination's
`.faustus/project.json` marker. This route is the explicit, dedicated
"the folder has moved" action instead.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import middleware  # noqa: E402
from routes import project_routes  # noqa: E402
from services import projects as projects_mod  # noqa: E402
from services.projects import ProjectStore  # noqa: E402

OWNER = "luis"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = ProjectStore(str(tmp_path / "data"))
    monkeypatch.setattr(projects_mod, "_store", st)
    monkeypatch.setattr(project_routes, "get_store", lambda: st)
    return st


@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    monkeypatch.setattr(project_routes, "effective_user", lambda request: OWNER)
    app = FastAPI()
    app.include_router(project_routes.setup_project_routes())
    return TestClient(app)


@pytest.fixture()
def old_ws(tmp_path):
    ws = tmp_path / "old_home" / "Faustus"
    ws.mkdir(parents=True)
    return str(ws)


@pytest.fixture()
def project(store, old_ws):
    return store.create("Faustus", folder="Faustus", workspace=old_ws, owner=OWNER)


def test_relocate_moves_the_workspace_and_keeps_identity(client, project, tmp_path):
    new_ws = tmp_path / "new_disk" / "Faustus"
    new_ws.mkdir(parents=True)
    r = client.post(f"/api/projects/{project['id']}/relocate", json={"new_path": str(new_ws)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"]["id"] == project["id"]
    assert body["project"]["workspace"] == str(new_ws)
    assert body["old_path_missing"] is False
    assert body["marker_conflict"] is False
    # The marker was actually written at the destination.
    assert os.path.isfile(os.path.join(str(new_ws), ".faustus", "project.json"))


def test_relocate_refuses_a_path_that_does_not_exist(client, project, tmp_path):
    missing = str(tmp_path / "nowhere" / "Faustus")
    r = client.post(f"/api/projects/{project['id']}/relocate", json={"new_path": missing})
    assert r.status_code == 400
    assert "does not exist" in r.json()["detail"]
    # Refused: the project's workspace is untouched.
    still = projects_mod.get_store().get(project["id"], owner=OWNER)
    assert still["workspace"] == project["workspace"]


def test_relocate_reports_when_the_old_folder_is_already_gone(client, project, tmp_path):
    """This is usually exactly WHY relocate is being called — informational,
    not a reason to refuse."""
    import shutil
    shutil.rmtree(project["workspace"])
    new_ws = tmp_path / "new_disk2" / "Faustus"
    new_ws.mkdir(parents=True)
    r = client.post(f"/api/projects/{project['id']}/relocate", json={"new_path": str(new_ws)})
    assert r.status_code == 200, r.text
    assert r.json()["old_path_missing"] is True


def test_relocate_on_an_unknown_project_is_a_404(client, tmp_path):
    ws = tmp_path / "somewhere"
    ws.mkdir()
    r = client.post("/api/projects/does-not-exist/relocate", json={"new_path": str(ws)})
    assert r.status_code == 404


def test_relocate_requires_a_non_empty_path(client, project):
    r = client.post(f"/api/projects/{project['id']}/relocate", json={"new_path": ""})
    assert r.status_code == 422  # pydantic min_length=1


def test_relocate_on_someone_elses_project_is_a_404(client, store, tmp_path, monkeypatch):
    """`_get_or_404` scopes by owner exactly like every other route here."""
    other_ws = tmp_path / "other_owner_ws"
    other_ws.mkdir()
    other = store.create("Someone else's project", workspace=str(other_ws), owner="mallory")
    ws = tmp_path / "target"
    ws.mkdir()
    r = client.post(f"/api/projects/{other['id']}/relocate", json={"new_path": str(ws)})
    assert r.status_code == 404


def test_relocate_requires_admin(client, project, tmp_path, monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    ws = tmp_path / "target2"
    ws.mkdir()
    r = client.post(f"/api/projects/{project['id']}/relocate", json={"new_path": str(ws)})
    assert r.status_code in (401, 403)
