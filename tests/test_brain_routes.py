"""routes/brain_routes.py — the second brain's HTTP surface.

Bare FastAPI + TestClient over just this router, following
tests/test_memory_grounding.py's disposable-store pattern and
tests/test_l92_board_routes.py's owner-header auth override: AUTH_ENABLED is
turned off so `require_user`/`require_admin` are no-ops, and
`routes.brain_routes.effective_user` is monkeypatched to read the owner from
a test header, so ownership checks run against real isolated data instead of
a mocked dependency.
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.middleware import require_admin  # noqa: E402
from routes import brain_routes  # noqa: E402
from src import memory_engine as engine  # noqa: E402
from src.brain import db as brain_db  # noqa: E402

OWNER = "luis"
OTHER = "mallory"


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    data_dir = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(engine, "DATA_DIR", str(data_dir))
    engine.set_vector_store(None)
    engine.clear_injected()

    brain_dir = tmp_path_factory.mktemp("brain")
    brain_db.use_dir(str(brain_dir))

    import src.constants as constants_mod
    monkeypatch.setattr(constants_mod, "DATA_DIR", str(data_dir))

    monkeypatch.setenv("AUTH_ENABLED", "false")
    yield
    engine.reset_vector_store()
    engine.clear_injected()
    brain_db.use_dir(None)


@pytest.fixture()
def client(monkeypatch):
    def _owner_from_header(request):
        return request.headers.get("x-test-owner") or ""

    monkeypatch.setattr(brain_routes, "effective_user", _owner_from_header)
    app = FastAPI()
    app.include_router(brain_routes.setup_brain_routes())
    return TestClient(app)


def _hdr(owner=OWNER):
    return {"x-test-owner": owner}


# ── status & sync ────────────────────────────────────────────────────────

def test_status_shape(client):
    resp = client.get("/api/brain/status", headers=_hdr())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("enabled", "vault_dir", "notes", "entities", "relations",
               "last_sync", "extraction", "wiki"):
        assert key in body, key
    assert set(body["extraction"]) == {"pending", "llm_enabled"}
    assert set(body["wiki"]) == {"enabled"}
    assert body["last_sync"] is None  # nothing synced yet


def test_sync_returns_a_report(client):
    resp = client.post("/api/brain/sync", headers=_hdr())
    assert resp.status_code == 200, resp.text
    report = resp.json()
    for key in ("at", "exported", "imported", "created", "suppressed",
               "conflicts", "guard_tripped", "errors", "duration_ms", "notes"):
        assert key in report, key

    # A second status call now sees the sync that just ran.
    status = client.get("/api/brain/status", headers=_hdr()).json()
    assert status["last_sync"] is not None
    assert status["last_sync"]["at"] == report["at"]


def test_sync_mirrors_a_memory_into_a_note(client):
    engine.add_item("Faustus prefers tabs.", owner=OWNER, trust_class="human_explicit")
    client.post("/api/brain/sync", headers=_hdr())
    tree = client.get("/api/brain/tree", headers=_hdr()).json()
    paths = [n["path"] for n in tree["notes"]]
    assert any(p.startswith("Memories/") for p in paths), paths


# ── notes CRUD ───────────────────────────────────────────────────────────

def test_create_read_write_rename_delete_restore_note(client):
    created = client.post("/api/brain/note", headers=_hdr(),
                          json={"title": "Ada's plan", "folder": "Notes",
                                "content": "First line."}).json()
    path = created["path"]
    assert created["title"] == "Ada's plan"
    assert created["content"].__contains__("First line.")

    read_back = client.get("/api/brain/note", headers=_hdr(), params={"path": path}).json()
    assert read_back["path"] == path

    written = client.put("/api/brain/note", headers=_hdr(),
                         json={"path": path, "content": read_back["content"] + "\nSecond line."})
    assert written.status_code == 200, written.text
    assert "note" in written.json() and "applied" in written.json()
    assert "Second line." in written.json()["note"]["content"]

    renamed = client.post("/api/brain/note/rename", headers=_hdr(),
                          json={"path": path, "new_title": "Ada's real plan"})
    assert renamed.status_code == 200, renamed.text
    new_path = renamed.json()["note"]["path"]
    assert new_path != path

    deleted = client.delete("/api/brain/note", headers=_hdr(), params={"path": new_path})
    assert deleted.status_code == 200, deleted.text
    trash_id = deleted.json()["trash_id"]
    assert deleted.json()["effect"] == "removed"

    trash = client.get("/api/brain/trash", headers=_hdr()).json()
    assert any(item["id"] == trash_id for item in trash["items"])

    restored = client.post(f"/api/brain/trash/{trash_id}/restore", headers=_hdr())
    assert restored.status_code == 200, restored.text
    assert restored.json()["title"] == "Ada's real plan"


def test_unknown_note_is_404(client):
    resp = client.get("/api/brain/note", headers=_hdr(), params={"path": "Notes/nope.md"})
    assert resp.status_code == 404


def test_bad_path_is_400(client):
    resp = client.get("/api/brain/note", headers=_hdr(), params={"path": "../etc/passwd"})
    assert resp.status_code == 400
    resp2 = client.get("/api/brain/note", headers=_hdr(), params={"path": "notes.txt"})
    assert resp2.status_code == 400


def test_renaming_a_memory_note_is_refused(client):
    engine.add_item("Bruno lives in Bluehaven.", owner=OWNER, trust_class="human_explicit")
    client.post("/api/brain/sync", headers=_hdr())
    tree = client.get("/api/brain/tree", headers=_hdr()).json()
    mem_path = next(n["path"] for n in tree["notes"] if n["path"].startswith("Memories/"))
    resp = client.post("/api/brain/note/rename", headers=_hdr(),
                       json={"path": mem_path, "new_title": "Renamed"})
    assert resp.status_code == 400


# ── search / graph / tags / unresolved / daily ──────────────────────────

def test_search_finds_a_created_note(client):
    client.post("/api/brain/note", headers=_hdr(),
               json={"title": "Villanueva contract", "content": "Cordera Labs terms."})
    resp = client.get("/api/brain/search", headers=_hdr(), params={"q": "Cordera"})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert any("Villanueva" in r["title"] for r in results)


def test_graph_notes_scope(client):
    a = client.post("/api/brain/note", headers=_hdr(),
                    json={"title": "Page A", "content": "See [[Page B]]."}).json()
    client.post("/api/brain/note", headers=_hdr(), json={"title": "Page B", "content": "hi"})
    resp = client.get("/api/brain/graph", headers=_hdr(), params={"scope": "notes"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "nodes" in body and "edges" in body
    node_ids = {n["id"] for n in body["nodes"]}
    assert a["path"] in node_ids


def test_graph_entities_scope(client):
    from src.brain import entities as ent

    e1 = ent.upsert_entity(OWNER, "Ada", type="person")
    e2 = ent.upsert_entity(OWNER, "Cordera Labs", type="organization")
    ent.add_relation(OWNER, e1["id"], "works_at", dst_id=e2["id"])
    resp = client.get("/api/brain/graph", headers=_hdr(), params={"scope": "entities"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {n["id"] for n in body["nodes"]}
    assert f"ent:{e1['id']}" in ids and f"ent:{e2['id']}" in ids


def test_tags_and_unresolved_links(client):
    client.post("/api/brain/note", headers=_hdr(),
               json={"title": "Tagged", "content": "#project note with [[Missing Page]]"})
    tags = client.get("/api/brain/tags", headers=_hdr()).json()["tags"]
    assert any(t["tag"] == "project" for t in tags)
    unresolved = client.get("/api/brain/unresolved", headers=_hdr()).json()["links"]
    assert any(u["target"] == "Missing Page" for u in unresolved)


def test_daily_note_created_on_first_call(client):
    resp = client.get("/api/brain/daily", headers=_hdr(), params={"date": "2026-09-23"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "Daily/2026-09-23.md"
    resp2 = client.get("/api/brain/daily", headers=_hdr(), params={"date": "2026-09-23"})
    assert resp2.json()["path"] == "Daily/2026-09-23.md"


def test_daily_note_bad_date_is_400(client):
    resp = client.get("/api/brain/daily", headers=_hdr(), params={"date": "not-a-date"})
    assert resp.status_code == 400


# ── entities ─────────────────────────────────────────────────────────────

def _entity(owner=OWNER, name="Ada", type="person"):
    from src.brain import entities as ent
    return ent.upsert_entity(owner, name, type=type)


def test_list_entities(client):
    _entity()
    resp = client.get("/api/brain/entities", headers=_hdr())
    assert resp.status_code == 200, resp.text
    names = [e["name"] for e in resp.json()["entities"]]
    assert "Ada" in names


def test_entity_profile_has_path_after_sync(client):
    e = _entity()
    client.post("/api/brain/sync", headers=_hdr())
    resp = client.get(f"/api/brain/entities/{e['id']}", headers=_hdr())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entity"]["id"] == e["id"]
    assert isinstance(body["entity"]["mentions"], list)
    assert isinstance(body["entity"]["relations"], list)
    assert body["path"] and body["path"].startswith("Entities/")


def test_unknown_entity_is_404(client):
    resp = client.get("/api/brain/entities/does-not-exist", headers=_hdr())
    assert resp.status_code == 404


def test_patch_entity(client):
    e = _entity()
    resp = client.patch(f"/api/brain/entities/{e['id']}", headers=_hdr(),
                        json={"summary": "A pioneering programmer.", "type": "person"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"] == "A pioneering programmer."


def test_merge_entities(client):
    from src.brain import entities as ent
    keep = ent.upsert_entity(OWNER, "Ada Lovelace", type="person")
    dupe = ent.upsert_entity(OWNER, "A. Lovelace", type="person")
    resp = client.post("/api/brain/entities/merge", headers=_hdr(),
                       json={"keep": keep["id"], "merge": dupe["id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == keep["id"]


def test_entity_owner_isolation(client):
    e = _entity(owner=OTHER)
    resp = client.get(f"/api/brain/entities/{e['id']}", headers=_hdr(OWNER))
    assert resp.status_code == 404


# ── timeline ─────────────────────────────────────────────────────────────

def test_timeline_cross_entity(client):
    engine.add_item("Ada joined in March 2025.", owner=OWNER, trust_class="human_explicit")
    resp = client.get("/api/brain/timeline", headers=_hdr())
    assert resp.status_code == 200, resp.text
    events = resp.json()["events"]
    assert isinstance(events, list)
    assert any(e.get("kind") == "created" for e in events)


def test_timeline_for_one_entity(client):
    e = _entity()
    resp = client.get("/api/brain/timeline", headers=_hdr(), params={"entity": e["id"]})
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["events"], list)


# ── background passes ────────────────────────────────────────────────────

def test_extract_endpoint(client):
    engine.add_item("Cordera Labs uses Python.", owner=OWNER, trust_class="human_explicit")
    resp = client.post("/api/brain/extract", headers=_hdr(), json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("processed", "entities", "relations", "errors"):
        assert key in body
    assert isinstance(body["errors"], list)


def test_wiki_refresh_endpoint_no_entity(client):
    resp = client.post("/api/brain/wiki/refresh", headers=_hdr(), json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("refreshed", "skipped", "errors"):
        assert key in body
    assert isinstance(body["errors"], list)


def test_wiki_refresh_endpoint_one_entity(client):
    e = _entity()
    resp = client.post("/api/brain/wiki/refresh", headers=_hdr(), json={"entity_id": e["id"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("refreshed", "skipped", "errors"):
        assert key in body


def test_wiki_refresh_unknown_entity_is_404(client):
    resp = client.post("/api/brain/wiki/refresh", headers=_hdr(),
                       json={"entity_id": "nope"})
    assert resp.status_code == 404


# ── settings ─────────────────────────────────────────────────────────────

def test_get_settings_shape(client):
    resp = client.get("/api/brain/settings", headers=_hdr())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("memory_temporal_parse", "memory_temporal_supersede", "brain_enabled",
               "brain_vault_dir", "brain_vault_sync_seconds", "brain_entity_extraction",
               "brain_llm_extraction", "brain_wiki_summaries", "brain_context_source"):
        assert key in body, key


def test_put_settings_updates_a_value(client, tmp_path, monkeypatch):
    from src import constants as constants_mod

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(constants_mod, "SETTINGS_FILE", str(settings_file))
    resp = client.put("/api/brain/settings", headers=_hdr(),
                      json={"brain_vault_sync_seconds": 300})
    assert resp.status_code == 200, resp.text
    assert resp.json()["brain_vault_sync_seconds"] == 300


def test_put_settings_is_admin_only(client):
    app = client.app
    app.dependency_overrides[require_admin] = _raise_403

    resp = client.put("/api/brain/settings", headers=_hdr(),
                      json={"brain_vault_sync_seconds": 60})
    assert resp.status_code == 403
    app.dependency_overrides.pop(require_admin, None)


def _raise_403():
    raise HTTPException(status_code=403, detail="Admin only")
