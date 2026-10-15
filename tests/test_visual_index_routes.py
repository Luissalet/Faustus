"""tests/test_visual_index_routes.py -- REST surface of the visual index on a fake embedder (routes/visual_index_routes.py)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.visual_index_routes as route_module
from src.visual_index import service as svc

from tests.vi_helpers import BLUE, RED, fake_web, make_service, page, png_file


@pytest.fixture()
def client(tmp_path, monkeypatch):
    admin = {"is": True}

    def require_admin(request):
        if not admin["is"]:
            raise HTTPException(403, "admin only")

    monkeypatch.setattr(route_module, "require_admin", require_admin)
    monkeypatch.setattr(route_module, "require_user", lambda request: request.state.current_user)
    monkeypatch.setattr(route_module, "storage_owner_for_request", lambda request: request.state.current_user)
    shot = page([(1024, RED), (1024, BLUE)], 875)
    service, _ = make_service(tmp_path, renderers=fake_web({"https://example.org/p": [(0, shot)]}))
    previous = svc.set_service_for_tests(service)
    app = FastAPI()

    @app.middleware("http")
    async def stamp(request, call_next):
        request.state.current_user = request.headers.get("x-user", "ada")
        return await call_next(request)

    app.include_router(route_module.setup_visual_index_routes())
    c = TestClient(app)
    c.admin = admin  # type: ignore[attr-defined]
    c.tmp = tmp_path  # type: ignore[attr-defined]
    yield c
    svc.set_service_for_tests(previous)


def test_status_without_dependencies_still_answers(client):
    r = client.get("/api/visual-index/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True and body["geometry"]["strip_height"] == 1024
    assert "vision" in body and "collections" in body


def test_add_search_tile_and_remove_flow(client):
    added = client.post("/api/visual-index/add", json={"urls": ["https://example.org/p"], "collection": "web"})
    assert added.status_code == 200, added.text
    assert added.json()["results"][0]["total"] == 2
    found = client.post("/api/visual-index/search", json={"query": "blue", "k": 1, "collection": "web"}).json()
    top = found["results"][0]
    assert top["source"] == "https://example.org/p" and top["y_offset"] == 1024
    meta = client.get(f"/api/visual-index/tiles/{top['id']}")
    assert meta.status_code == 200 and meta.json()["height"] == 1024
    img = client.get(f"/api/visual-index/tiles/{top['id']}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content[:4] == b"\x89PNG"
    assert client.get("/api/visual-index/collections").json()["collections"][0]["collection"] == "web"
    assert len(client.get("/api/visual-index/collections/web").json()["sources"]) == 1
    gone = client.delete("/api/visual-index/collections/web", params={"source": "https://example.org/p"})
    assert gone.status_code == 200 and gone.json()["removed_strips"] == 2
    assert client.delete("/api/visual-index/collections/web").status_code == 200
    assert client.get("/api/visual-index/collections/web").status_code == 404


def test_paths_need_admin_urls_do_not(client):
    path = png_file(client.tmp, "a.png", page([(400, RED)], 300))
    client.admin["is"] = False
    assert client.post("/api/visual-index/add", json={"paths": [path]}).status_code == 403
    assert client.post("/api/visual-index/add", json={"urls": ["https://example.org/p"]}).status_code == 200
    client.admin["is"] = True
    assert client.post("/api/visual-index/add", json={"paths": [path]}).status_code == 200


def test_other_users_do_not_see_my_strips(client):
    client.post("/api/visual-index/add", json={"urls": ["https://example.org/p"]})
    sid = client.post("/api/visual-index/search", json={"query": "red"}).json()["results"][0]["id"]
    assert client.get(f"/api/visual-index/tiles/{sid}", headers={"x-user": "bob"}).status_code == 404
    assert client.get("/api/visual-index/collections", headers={"x-user": "bob"}).json()["collections"] == []


def test_errors_are_json_with_a_code(client):
    r = client.post("/api/visual-index/add", json={"urls": []})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_arguments"
    assert client.post("/api/visual-index/add", content=b"[1]", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/visual-index/search", json={"query": ""}).status_code == 400
    assert client.post("/api/visual-index/search", json={"query": "x", "collection": "nope"}).status_code == 404
    assert client.get("/api/visual-index/tiles/not-an-id").status_code == 400


def test_read_tile_says_so_when_there_is_no_vision_model(client, monkeypatch):
    from src.visual_index import vision

    client.post("/api/visual-index/add", json={"urls": ["https://example.org/p"]})
    sid = client.post("/api/visual-index/search", json={"query": "red"}).json()["results"][0]["id"]
    monkeypatch.setattr(vision, "vision_ready", lambda owner: (False, "no vision model is running", ""))
    r = client.post(f"/api/visual-index/tiles/{sid}/read", json={"question": "what does it say?"})
    assert r.status_code == 200
    assert r.json()["answered_by"] == "none" and r.json()["vision_available"] is False
    monkeypatch.setattr(vision, "vision_ready", lambda owner: (True, "", "qwen-vl"))
    monkeypatch.setattr(vision, "ask", lambda image, prompt, owner=None: {"text": "Q3: 42", "model": "qwen-vl"})
    r = client.post(f"/api/visual-index/tiles/{sid}/read", json={"question": "value?"})
    assert r.json()["answered_by"] == "qwen-vl" and r.json()["text"] == "Q3: 42"


def test_embedder_unavailable_is_a_503_with_the_install_hint(client, monkeypatch):
    from src.visual_index import embedder as emb_mod
    from src.visual_index.embedder import EmbedderPool

    monkeypatch.setattr(emb_mod, "dependencies_missing", lambda: ["torch", "transformers"])
    svc.get_service()._pool = EmbedderPool(lambda: emb_mod.Qwen3VLEmbedder(), idle_s=0)
    r = client.post("/api/visual-index/add", json={"urls": ["https://example.org/p"]})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "dependencies_missing" and "pip install" in r.json()["detail"]["error"]
    assert client.get("/api/visual-index/status").status_code == 200


def test_routes_are_in_the_real_app():
    import app as app_module

    paths = set(app_module.app.openapi()["paths"])
    assert "/api/visual-index/search" in paths and "/api/visual-index/tiles/{tile_id}/image" in paths


def test_status_accepts_probe(client):
    body = client.get("/api/visual-index/status?probe=1").json()
    assert "embedder" in body and "geometry" in body
