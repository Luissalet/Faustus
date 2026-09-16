"""tests/test_creator_wp20_canvas_routes.py — WP20 canvas HTTP surface:
GET .../canvas/{doc_id}/render, POST .../canvas/{doc_id}/export,
POST .../canvas/import-legacy, POST .../canvas/{doc_id}/inpaint-request.

Same ``AUTH_ENABLED=false`` no-login pattern as every other Creator route
test (``tests/test_creator_wp13_routes.py``); real ``DocumentStore``, real
``artifact_store``/``artifact_identity`` (own sqlite engine), real PNGs.
"""
from __future__ import annotations

import base64
import io
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod
from src import artifact_store
from src import constants as constants_mod
from src import media_edit_projects as legacy
from src.contracts import ExecutionResult
from src.creator.store import DocumentStore

OWNER = "__odysseus_local__"


@pytest.fixture()
def route_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    url = "sqlite:///" + (tmp_path / "canvas_routes.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    db_mod.Base.metadata.create_all(bind=engine)

    store_dir = str(tmp_path / "store")
    os.makedirs(store_dir, exist_ok=True)
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", store_dir)
    monkeypatch.setattr(constants_mod, "ARTIFACT_STORE_DIR", store_dir)

    legacy_dir = str(tmp_path / "legacy_projects")
    monkeypatch.setattr(legacy, "PROJECTS_DIR", legacy_dir)

    import src.creator.store as store_mod
    doc_store = DocumentStore(str(tmp_path / "creator_docs.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", doc_store)

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: True if key == "creator_enabled" else default)

    from routes.creator_canvas_routes import setup_creator_canvas_routes
    app = FastAPI()
    app.include_router(setup_creator_canvas_routes())
    client = TestClient(app)

    # A real base image occurrence.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    Image.new("RGB", (16, 16), (10, 20, 30)).save(src_dir / "base.png")
    result = ExecutionResult.parse({
        "run_id": "run1", "backend": "docker_workspace", "status": "completed",
        "exit_code": 0, "artifact_filenames": ["base.png"],
    })
    collected = artifact_store.collect(result, source_dir=str(src_dir), owner=OWNER,
                                       project_id="p1", store_dir=store_dir)
    artifact_store.persist(collected.artifacts)
    base = collected.artifacts[0]

    content = {
        "width": 16, "height": 16, "operation_semantics_version": 1,
        "base_asset_ref": base.id, "layers": [],
    }
    doc = doc_store.create(OWNER, "p1", "canvas", content)
    return client, doc, doc_store, legacy_dir


def test_flag_off_is_404_for_every_route(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import src.creator.store as store_mod
    doc_store = DocumentStore(str(tmp_path / "off.sqlite3"))
    monkeypatch.setattr(store_mod, "_store", doc_store)
    doc = doc_store.create(OWNER, "p1", "canvas", {
        "width": 4, "height": 4, "operation_semantics_version": 1,
        "base_asset_ref": "none", "layers": [],
    })

    from src import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_setting",
                         lambda key, default=None: False if key == "creator_enabled" else default)

    from routes.creator_canvas_routes import setup_creator_canvas_routes
    app = FastAPI()
    app.include_router(setup_creator_canvas_routes())
    client = TestClient(app)

    assert client.get(f"/api/creator/canvas/{doc.id}/render").status_code == 404
    assert client.post(f"/api/creator/canvas/{doc.id}/export", json={}).status_code == 404
    assert client.post("/api/creator/canvas/import-legacy", json={}).status_code == 404
    assert client.post(f"/api/creator/canvas/{doc.id}/inpaint-request", json={}).status_code == 404


def test_render_returns_png_bytes(route_client):
    client, doc, _store, _legacy_dir = route_client
    resp = client.get(f"/api/creator/canvas/{doc.id}/render")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (16, 16)


def test_render_unknown_document_is_404(route_client):
    client, _doc, _store, _legacy_dir = route_client
    assert client.get("/api/creator/canvas/doc_does_not_exist/render").status_code == 404


def test_render_wrong_kind_is_400(route_client):
    client, _doc, doc_store, _legacy_dir = route_client
    tl = doc_store.create(OWNER, "p1", "timeline", {
        "clock": {"ticks_per_second_numerator": "30", "ticks_per_second_denominator": "1"},
        "duration_ticks": "10",
        "tracks": [{"id": "t0", "kind": "video", "locked": False, "clips": []}],
    })
    assert client.get(f"/api/creator/canvas/{tl.id}/render").status_code == 400


def test_export_publishes_and_returns_occurrence(route_client):
    client, doc, _store, _legacy_dir = route_client
    resp = client.post(f"/api/creator/canvas/{doc.id}/export", json={"format": "png"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["format"] == "png"
    assert "occurrence_id" in body


def test_import_legacy_then_render(route_client):
    client, _doc, doc_store, legacy_dir = route_client
    src_img_dir = os.path.dirname(legacy_dir)
    src_img = os.path.join(src_img_dir, "legacy_source.png")
    Image.new("RGB", (12, 8), (1, 2, 3)).save(src_img)
    record = legacy.create(src_img, owner=OWNER, directory=legacy_dir)

    layer_buf = io.BytesIO()
    Image.new("RGBA", (12, 8), (250, 0, 0, 255)).save(layer_buf, format="PNG")
    legacy.add_layer(record["id"], owner=OWNER,
                     image_b64=base64.b64encode(layer_buf.getvalue()).decode("ascii"),
                     x=0, y=0, directory=legacy_dir)

    resp = client.post("/api/creator/canvas/import-legacy",
                       json={"edit_project_id": record["id"], "project_id": "p1"})
    assert resp.status_code == 200
    new_doc = resp.json()
    assert new_doc["kind"] == "canvas"
    assert new_doc["content"]["width"] == 12
    assert len(new_doc["content"]["layers"]) == 1

    render_resp = client.get(f"/api/creator/canvas/{new_doc['id']}/render")
    assert render_resp.status_code == 200
    img = Image.open(io.BytesIO(render_resp.content)).convert("RGB")
    assert img.getpixel((5, 4)) == (250, 0, 0)


def test_inpaint_request_returns_recipe_params(route_client):
    client, doc, _store, _legacy_dir = route_client
    resp = client.post(f"/api/creator/canvas/{doc.id}/inpaint-request", json={
        "prompt": "add a sunset",
        "region": {"x": 2, "y": 2, "width": 6, "height": 6, "source_w": 16, "source_h": 16},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["recipe_id"] == "inpaint"
    assert body["params"]["prompt"] == "add a sunset"
    assert "reference_image" in body["params"]
    assert "mask" in body["params"]


def test_inpaint_request_missing_region_is_400(route_client):
    client, doc, _store, _legacy_dir = route_client
    resp = client.post(f"/api/creator/canvas/{doc.id}/inpaint-request", json={"prompt": "x"})
    assert resp.status_code == 400
