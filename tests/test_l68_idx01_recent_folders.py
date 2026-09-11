"""IDX-01 · GET /api/projects/recent-folders — a folder picker with no native
`showOpenDialog`/`webkitdirectory` still needs something to suggest.

`src.project_identity.relocate` (verified separately by
`tests/test_l43_idx01_relocate_route.py` and `tests/qa/test_qa_48_migracion_
de_carpeta.py`) already refuses an absent path and keeps every memory keyed
by `project_id` across a workspace change — that half of IDX-01 was already
closed. What was missing is the list this file exercises: the owner's most
recently touched project folders, deduplicated and capped at 10, for the
selector to offer instead of an empty text box.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes import project_routes
from services import projects as projects_mod
from services.projects import ProjectStore

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


def _mkdir(tmp_path, *parts) -> str:
    p = tmp_path.joinpath(*parts)
    p.mkdir(parents=True)
    return str(p)


# ---------------------------------------------------------------------------
# services/projects.py::ProjectStore.recent_folders — pure logic
# ---------------------------------------------------------------------------

def test_recent_folders_orders_most_recently_touched_first(store, tmp_path):
    a = store.create("Alpha", workspace=_mkdir(tmp_path, "alpha"), owner=OWNER)
    b = store.create("Beta", workspace=_mkdir(tmp_path, "beta"), owner=OWNER)
    # Touch Alpha again after Beta so it sorts first.
    store.update(a["id"], {"instructions": "bump updated_at"}, owner=OWNER)

    folders = store.recent_folders(OWNER)
    assert [f["project_id"] for f in folders] == [a["id"], b["id"]]
    assert folders[0]["path"] == a["workspace"]


def test_recent_folders_deduplicates_by_normalised_path(store, tmp_path):
    """Two projects that (however unlikely) point at the same folder must
    not show the picker the same path twice."""
    shared = _mkdir(tmp_path, "shared")
    a = store.create("A", workspace=shared, owner=OWNER)
    b = store.create("B", workspace=_mkdir(tmp_path, "other"), owner=OWNER)
    # Force a duplicate by writing the same workspace directly (create()
    # itself refuses a folder name collision, not a workspace collision).
    rows = store._load()
    for r in rows:
        if r["id"] == b["id"]:
            r["workspace"] = os.path.normcase(shared) if os.name == "nt" else shared
    store._save(rows)

    folders = store.recent_folders(OWNER)
    paths = [f["path"] for f in folders]
    assert len(paths) == len(set(os.path.normcase(p) for p in paths))


def test_recent_folders_is_capped_at_the_given_limit(store, tmp_path):
    for i in range(15):
        store.create(f"P{i}", workspace=_mkdir(tmp_path, f"p{i}"), owner=OWNER)

    assert len(store.recent_folders(OWNER, limit=10)) == 10
    assert len(store.recent_folders(OWNER, limit=3)) == 3


def test_recent_folders_skips_projects_with_no_workspace(store, tmp_path):
    store.create("No folder yet", workspace="", owner=OWNER)
    with_ws = store.create("Has folder", workspace=_mkdir(tmp_path, "ws"), owner=OWNER)

    folders = store.recent_folders(OWNER)
    assert [f["project_id"] for f in folders] == [with_ws["id"]]


def test_recent_folders_is_owner_scoped(store, tmp_path):
    mine = store.create("Mine", workspace=_mkdir(tmp_path, "mine"), owner=OWNER)
    store.create("Someone else's", workspace=_mkdir(tmp_path, "theirs"), owner="mallory")

    folders = store.recent_folders(OWNER)
    assert [f["project_id"] for f in folders] == [mine["id"]]


def test_relocating_a_project_moves_its_folder_to_the_front(store, tmp_path):
    """The IDX-01 tie-in: relocate() calls ProjectStore.update(), which bumps
    updated_at — so a folder just relocated INTO sorts first here too."""
    from src import project_identity

    a = store.create("Alpha", workspace=_mkdir(tmp_path, "alpha_old"), owner=OWNER)
    store.create("Beta", workspace=_mkdir(tmp_path, "beta"), owner=OWNER)
    new_home = _mkdir(tmp_path, "alpha_new")

    project_identity.relocate(a["id"], new_home, owner=OWNER, store=store)

    folders = store.recent_folders(OWNER)
    assert folders[0]["project_id"] == a["id"]
    assert folders[0]["path"] == new_home


# ---------------------------------------------------------------------------
# route wiring
# ---------------------------------------------------------------------------

def test_route_returns_recent_folders_for_the_effective_user(client, store, tmp_path):
    a = store.create("Alpha", workspace=_mkdir(tmp_path, "alpha"), owner=OWNER)

    r = client.get("/api/projects/recent-folders")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["folders"][0]["project_id"] == a["id"]
    assert body["folders"][0]["path"] == a["workspace"]


def test_route_is_capped_at_ten_even_with_more_projects(client, store, tmp_path):
    for i in range(12):
        store.create(f"P{i}", workspace=_mkdir(tmp_path, f"p{i}"), owner=OWNER)

    r = client.get("/api/projects/recent-folders")
    assert r.status_code == 200
    assert r.json()["count"] == 10
    assert len(r.json()["folders"]) == 10


def test_the_literal_path_is_not_swallowed_by_the_project_id_route(client, store, tmp_path):
    """`/recent-folders` must be declared before `/{project_id}` — this is
    the regression that catches the ordering bug directly: a project whose
    id happens to be 'recent-folders' must never exist, and the route must
    answer the folders shape, not a 404 for an unknown project id."""
    r = client.get("/api/projects/recent-folders")
    assert r.status_code == 200
    assert "folders" in r.json()
    # And the ordinary project-by-id route is unaffected.
    missing = client.get("/api/projects/does-not-exist")
    assert missing.status_code == 404


def test_route_requires_admin(client, monkeypatch):
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    r = client.get("/api/projects/recent-folders")
    assert r.status_code in (401, 403)
