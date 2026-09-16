"""tests/test_creator_wp13_routes.py — WP13: GET .../timeline/{view,validate,edl},
GET .../revisions/{n} and POST .../undo, over a real ``DocumentStore`` and
``TestClient``. Same ``AUTH_ENABLED=false`` no-login pattern every other
Creator route test in this repo uses (``tests/test_creator_wp02.py``).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.creator.store import DocumentStore
from src.creator.timeline import tracks


def _timeline_content():
    return {
        "clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
        "duration_ticks": "100",
        "tracks": [{
            "id": "t0", "kind": "video", "locked": False,
            "clips": [{
                "id": "a", "asset_ref": "occ_x", "timeline_start_ticks": "0",
                "timeline_duration_ticks": "10",
                "source_range": {"start_ticks": "0", "duration_ticks": "10"},
                "source_clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
            }],
        }],
    }


@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    import src.creator.store as store_mod
    doc_store = DocumentStore(str(tmp_path / "creator_docs.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", doc_store)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: True if key == "creator_enabled" else default)

    from routes.creator_timeline_routes import setup_creator_timeline_routes
    app = FastAPI()
    app.include_router(setup_creator_timeline_routes())
    client = TestClient(app)

    doc = doc_store.create("__odysseus_local__", "prj_1", "timeline", _timeline_content())
    return client, doc, doc_store


def test_flag_off_is_404_for_every_route(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import src.creator.store as store_mod
    doc_store = DocumentStore(str(tmp_path / "off.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", doc_store)
    doc = doc_store.create("__odysseus_local__", "prj_1", "timeline", _timeline_content())

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: False if key == "creator_enabled" else default)

    from routes.creator_timeline_routes import setup_creator_timeline_routes
    app = FastAPI()
    app.include_router(setup_creator_timeline_routes())
    client = TestClient(app)

    assert client.get(f"/api/creator/documents/{doc.id}/timeline/view").status_code == 404
    assert client.get(f"/api/creator/documents/{doc.id}/timeline/validate").status_code == 404
    assert client.get(f"/api/creator/documents/{doc.id}/timeline/edl").status_code == 404
    assert client.get(f"/api/creator/documents/{doc.id}/revisions/1").status_code == 404
    assert client.post(f"/api/creator/documents/{doc.id}/undo",
                        json={"command_id": "c", "target_revision": 1, "expected_revision": 1}).status_code == 404


def test_timeline_view_projection(route_client):
    client, doc, _store = route_client
    resp = client.get(f"/api/creator/documents/{doc.id}/timeline/view")
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 1
    assert body["view"]["tracks"][0]["clips"][0]["id"] == "a"


def test_timeline_validate_report(route_client):
    client, doc, _store = route_client
    resp = client.get(f"/api/creator/documents/{doc.id}/timeline/validate")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_timeline_edl_export(route_client):
    client, doc, _store = route_client
    resp = client.get(f"/api/creator/documents/{doc.id}/timeline/edl")
    assert resp.status_code == 200
    assert "TITLE:" in resp.text


def test_non_timeline_document_is_400(route_client, tmp_path):
    client, doc, store = route_client
    canvas = store.create("__odysseus_local__", "prj_1", "canvas", {
        "width": 10, "height": 10, "layers": [], "base_asset_ref": "occ_1",
    })
    resp = client.get(f"/api/creator/documents/{canvas.id}/timeline/view")
    assert resp.status_code == 400


def test_revision_snapshot_and_undo_round_trip(route_client):
    client, doc, store = route_client
    # apply_command lives on the WP02 router (routes/creator_routes.py, not
    # this file's); advance the revision directly through the store, the
    # same effect that route has.
    result = store.apply_command("__odysseus_local__", doc.id, "c1", 1, {
        "type": "timeline.add_marker", "marker_id": "m1", "at_ticks": "5",
    })
    assert result["doc"].revision == 2

    snap = client.get(f"/api/creator/documents/{doc.id}/revisions/1")
    assert snap.status_code == 200
    assert tracks.get_markers(snap.json()["content"]) == []

    missing = client.get(f"/api/creator/documents/{doc.id}/revisions/99")
    assert missing.status_code == 404

    undo = client.post(f"/api/creator/documents/{doc.id}/undo", json={
        "command_id": "undo-1", "target_revision": 1, "expected_revision": 2,
    })
    assert undo.status_code == 200
    body = undo.json()
    assert body["document"]["revision"] == 3
    assert tracks.get_markers(body["document"]["content"]) == []

    conflict = client.post(f"/api/creator/documents/{doc.id}/undo", json={
        "command_id": "undo-2", "target_revision": 1, "expected_revision": 2,
    })
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == 3


def test_undo_unknown_document_is_404(route_client):
    client, _doc, _store = route_client
    resp = client.post("/api/creator/documents/doc_missing/undo", json={
        "command_id": "x", "target_revision": 1, "expected_revision": 1,
    })
    assert resp.status_code == 404
