"""
tests/test_media_routes.py — the HTTP surface, and the surface it deliberately
does not have.

The assertion that carries the phase is the last one in this file: there is no
endpoint that takes a graph. Everything else follows from that — a caller
names a template and fills the inputs it declares, and an input it does not
declare is a 400 that names the field.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from routes.media_routes import setup_media_routes
from tests.test_comfyui_backend import FakeComfy


@pytest.fixture()
def client(tmp_path, monkeypatch):
    url = "sqlite:///" + (tmp_path / "media_routes.db").as_posix()
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(autocommit=False, autoflush=False, bind=engine))
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    from src import artifact_store
    monkeypatch.setattr(artifact_store, "ARTIFACT_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(artifact_store, "ARTIFACT_RUNS_DIR", str(tmp_path / "runs"))

    fake = FakeComfy()
    monkeypatch.setenv("COMFYUI_URL", fake.start())
    app = FastAPI()
    app.include_router(setup_media_routes())
    try:
        yield TestClient(app), fake
    finally:
        fake.stop()
        engine.dispose()


def test_the_catalogue_lists_the_recipes_with_their_licences(client):
    http, fake = client
    body = http.get("/api/media/workflows").json()
    assert body["ok"] and body["broken"] == []
    ids = {w["id"] for w in body["workflows"]}
    assert {"image.product", "image.reference-edit"} <= ids
    product = next(w for w in body["workflows"] if w["id"] == "image.product")
    assert product["models"][0]["license"]
    assert "no endpoint that accepts a graph" in body["note"]


def test_the_engine_endpoint_says_what_is_actually_installed(client):
    http, fake = client
    body = http.get("/api/media/engine").json()
    assert body["ok"] and "RTX 4070 Ti" in body["detail"]
    assert "sd_xl_base_1.0.safetensors" in body["checkpoints"]


def test_planning_shows_the_seed_and_queues_nothing(client):
    http, fake = client
    body = http.post("/api/media/plan",
                     json={"workflow": "image.product",
                           "inputs": {"prompt": "a mug"}}).json()
    assert body["ok"] and isinstance(body["values"]["seed"], int)
    assert body["models"][0]["license"] == "CreativeML Open RAIL++-M"
    assert fake.submitted == []


def test_an_input_the_template_rejects_is_a_400_naming_the_field(client):
    http, fake = client
    out = http.post("/api/media/runs",
                    json={"workflow": "image.product",
                          "inputs": {"prompt": "a mug", "steps": 999}})
    assert out.status_code == 400
    assert "inputs.steps" in out.json()["detail"]
    assert fake.submitted == []


def test_a_template_that_does_not_exist_is_a_400(client):
    http, fake = client
    out = http.post("/api/media/runs", json={"workflow": "image.nope"})
    assert out.status_code == 400


def test_a_render_runs_end_to_end_over_http(client):
    http, fake = client
    started = http.post("/api/media/runs",
                        json={"workflow": "image.product",
                              "inputs": {"prompt": "a ceramic mug"},
                              "owner": "luis"}).json()
    assert started["ok"] and started["status"] == "queued"
    run_id = started["run_id"]

    waiting = http.post(f"/api/media/runs/{run_id}/poll").json()
    assert waiting["status"] in ("queued", "running")

    fake.finish(started["engine_job_id"])
    done = http.post(f"/api/media/runs/{run_id}/poll").json()
    assert done["status"] == "completed"
    assert done["artifacts"][0]["provenance"]["model_license"]

    listed = http.get("/api/media/runs?owner=luis").json()
    assert listed["count"] == 1 and listed["runs"][0]["id"] == run_id
    assert http.get(f"/api/media/runs/{run_id}").json()["run"]["status"] == "completed"


def test_cancelling_over_http_frees_the_engine(client):
    http, fake = client
    started = http.post("/api/media/runs",
                        json={"workflow": "image.product",
                              "inputs": {"prompt": "a mug"}}).json()
    out = http.post(f"/api/media/runs/{started['run_id']}/cancel").json()
    assert out["ok"] and out["status"] == "cancelled"
    assert fake.pending == []


def test_a_run_that_does_not_exist_is_a_404(client):
    http, fake = client
    assert http.get("/api/media/runs/mrun_nope").status_code == 404
    assert http.post("/api/media/runs/mrun_nope/poll").status_code == 404


def test_there_is_no_route_that_accepts_a_graph(client):
    """The claim of the phase, asserted against the router itself rather than
    against a docstring. If a future change adds one, this fails."""
    http, fake = client
    # From the OpenAPI schema, which is what the app actually exposes — not
    # from the router object, whose `routes` list nests.
    paths = {p for p in http.app.openapi()["paths"] if p.startswith("/api/media")}
    assert paths == {
        "/api/media/workflows", "/api/media/engine", "/api/media/plan",
        "/api/media/runs", "/api/media/runs/{run_id}",
        "/api/media/runs/{run_id}/poll", "/api/media/runs/{run_id}/cancel",
    }, f"the media router grew a route: {sorted(paths)}"

    # And the one that starts a render ignores a graph if one is smuggled in:
    # the body's other keys are not read at all.
    started = http.post("/api/media/runs", json={
        "workflow": "image.product", "inputs": {"prompt": "a mug"},
        "graph": {"666": {"class_type": "ExecuteWhateverYouLike", "inputs": {}}},
    }).json()
    assert started["ok"]
    sent = fake.submitted[0]["prompt"]
    assert "666" not in sent
    assert {n["class_type"] for n in sent.values()} == {
        "CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage",
        "KSampler", "VAEDecode", "SaveImage"}
