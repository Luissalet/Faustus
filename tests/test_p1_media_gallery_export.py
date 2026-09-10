"""MEDIA-06 — "borrar ubicación EXIF opcional", over the real HTTP route.

`gallery_download_zip` (routes/gallery/gallery_routes.py) gets an opt-in
`redact_location` flag: off by default (every existing caller of this
endpoint keeps getting byte-identical files, no capability lost), and when
set, every image in the bundle has its GPS EXIF removed via
`routes.gallery.gallery_helpers.strip_location_exif` before being written to
the zip.

Runs a real TestClient against the real router and a real SQLite database
(rule: no mocks across an HTTP boundary) with an actual JPEG carrying a GPS
tag on disk, per gallery's own precedent in `tests/test_gallery_null_user_routes.py`.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import GalleryImage
import routes.gallery_routes as gallery_routes


def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (4, 4), "green")
    exif = img.getexif()
    exif[0x8825] = {1: "N", 2: (40, 0, 0.0), 3: "W", 4: (74, 0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def _client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gallery.db'}",
                           connect_args={"check_same_thread": False}, poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(gallery_routes, "SessionLocal", session_factory)

    images_dir = tmp_path / "generated"
    images_dir.mkdir()
    monkeypatch.setattr(gallery_routes, "GALLERY_IMAGE_DIR", images_dir)
    filename = "with-gps.jpg"
    (images_dir / filename).write_bytes(_jpeg_with_gps())

    db = session_factory()
    try:
        db.add(GalleryImage(id="img-1", filename=filename, prompt="a place",
                            model="test", tags="", ai_tags="", owner="alice",
                            is_active=True, file_size=10))
        db.commit()
    finally:
        db.close()

    app = FastAPI()

    @app.middleware("http")
    async def stamp_user(request: Request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    app.include_router(gallery_routes.setup_gallery_routes())
    return TestClient(app)


def _unzip_first(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        assert len(names) == 1
        return zf.read(names[0])


def test_download_zip_defaults_to_byte_identical_files(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    original = (tmp_path / "generated" / "with-gps.jpg").read_bytes()
    resp = client.post("/api/gallery/download-zip", json={"ids": ["img-1"]})
    assert resp.status_code == 200
    assert _unzip_first(resp.content) == original
    assert Image.open(io.BytesIO(original)).getexif().get(0x8825) is not None


def test_download_zip_redacts_location_when_asked(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.post("/api/gallery/download-zip",
                       json={"ids": ["img-1"], "redact_location": True})
    assert resp.status_code == 200
    bundled = _unzip_first(resp.content)
    assert Image.open(io.BytesIO(bundled)).getexif().get(0x8825) is None
    original = (tmp_path / "generated" / "with-gps.jpg").read_bytes()
    assert bundled != original
