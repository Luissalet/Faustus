"""MEDIA-03 over HTTP — the minimal, real endpoints in routes/media_routes.py
that expose `src.media_edit_projects`. Same TestClient/auth-disabled fixture
shape as `tests/test_media_routes.py`, against the real router.
"""
from __future__ import annotations

import base64
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as db_mod, middleware
from core.database import Base
from routes.media_edit_routes import setup_media_edit_routes
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

    from src import media_edit_projects
    monkeypatch.setattr(media_edit_projects, "PROJECTS_DIR", str(tmp_path / "edit_projects"))

    fake = FakeComfy()
    monkeypatch.setenv("COMFYUI_URL", fake.start())
    app = FastAPI()
    app.include_router(setup_media_edit_routes())
    try:
        yield TestClient(app), tmp_path
    finally:
        fake.stop()


def _png_b64(color=(1, 2, 3)):
    buf = io.BytesIO()
    Image.new("RGBA", (3, 3), color + (255,)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_create_add_layer_and_export_over_http(client):
    c, tmp_path = client
    source = tmp_path / "photo.png"
    Image.new("RGBA", (5, 5), (9, 9, 9, 255)).save(source, format="PNG")

    created = c.post("/api/media/edit/projects", json={"source_path": str(source)})
    assert created.status_code == 200
    pid = created.json()["id"]

    layered = c.post(f"/api/media/edit/projects/{pid}/layers",
                     json={"image_b64": _png_b64(), "x": 1, "y": 1, "label": "patch"})
    assert layered.status_code == 200
    assert len(layered.json()["layers"]) == 1

    fetched = c.get(f"/api/media/edit/projects/{pid}")
    assert fetched.status_code == 200 and len(fetched.json()["layers"]) == 1

    dest = tmp_path / "out.png"
    exported = c.post(f"/api/media/edit/projects/{pid}/export", json={"path": str(dest)})
    assert exported.status_code == 200
    assert exported.json()["original_preserved"] is True
    assert dest.is_file()


def test_export_over_the_original_is_refused_without_confirmation(client):
    c, tmp_path = client
    source = tmp_path / "photo.png"
    Image.new("RGBA", (5, 5), (9, 9, 9, 255)).save(source, format="PNG")
    pid = c.post("/api/media/edit/projects", json={"source_path": str(source)}).json()["id"]

    refused = c.post(f"/api/media/edit/projects/{pid}/export", json={"path": str(source)})
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "would_overwrite_original"


def test_unknown_project_is_404(client):
    c, _ = client
    assert c.get("/api/media/edit/projects/edit_doesnotexist").status_code == 404
